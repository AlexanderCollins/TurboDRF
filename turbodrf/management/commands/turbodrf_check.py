"""
Check which models are eligible for the compiled read path.

Usage:
    python manage.py turbodrf_check
    python manage.py turbodrf_check --model Book
"""

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.core.management.base import BaseCommand

from turbodrf.mixins import TurboDRFMixin, _resolve_configured_fields


class Command(BaseCommand):
    help = "Check which TurboDRF models are eligible for the compiled read path"

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            type=str,
            help="Check a specific model by name",
        )

    def handle(self, *args, **options):
        target_model = options.get("model")
        models = []

        for model in apps.get_models():
            if not issubclass(model, TurboDRFMixin):
                continue
            config = model.turbodrf()
            if not config.get("enabled", True):
                continue
            if target_model and model.__name__ != target_model:
                continue
            models.append(model)

        if not models:
            if target_model:
                self.stderr.write(f"No TurboDRF model named '{target_model}' found.")
            else:
                self.stderr.write("No TurboDRF models found.")
            return

        for model in sorted(models, key=lambda m: m.__name__):
            self._check_model(model)

    def _check_model(self, model):
        config = model.turbodrf()
        # Same default as the router: compiled unless opted out.
        opted_in = config.get("compiled", True)
        public_access = config.get("public_access", False)

        # The list fields the API serves (the compiled path serves lists).
        list_fields = _resolve_configured_fields(model, "list")

        # Check each field for eligibility
        issues = []
        field_summary = {"db": 0, "fk": 0, "m2m": 0, "reverse": 0, "property": 0}

        for field_name in list_fields:
            if "__" not in field_name:
                try:
                    model._meta.get_field(field_name)
                    field_summary["db"] += 1
                except FieldDoesNotExist:
                    attr = getattr(model, field_name, None)
                    if isinstance(attr, property):
                        field_summary["property"] += 1
                    else:
                        issues.append(f"  '{field_name}' is not a DB field or property")
            else:
                parts = field_name.split("__")
                try:
                    base_field = model._meta.get_field(parts[0])
                    if base_field.many_to_many:
                        field_summary["m2m"] += 1
                    elif getattr(base_field, "one_to_many", False):
                        # A reverse FK (``book_set__title``): many rows.
                        field_summary["reverse"] += 1
                    elif getattr(base_field, "related_model", None) is not None:
                        field_summary["fk"] += 1
                    else:
                        issues.append(f"  '{field_name}' traverses non-relation field")
                except FieldDoesNotExist:
                    issues.append(f"  '{parts[0]}' base field does not exist")

        # Compiled exactly when the router compiles it: opted in, and the
        # compiler can render every list field as the serializer path does.
        blocker = None
        if opted_in and not issues:
            from turbodrf.compiler import compile_blocker

            try:
                blocker = compile_blocker(model)
            except Exception as e:  # the router logs and falls back too
                blocker = str(e)
        compiled = opted_in and not issues and blocker is None

        # Output
        name = f"{model._meta.app_label}.{model.__name__}"
        status = self.style.SUCCESS("compiled") if compiled else "not compiled"

        if issues:
            symbol = self.style.ERROR("INELIGIBLE")
        else:
            symbol = self.style.SUCCESS("OK")

        self.stdout.write(f"\n{name} [{status}] {symbol}")
        reverse = (
            f", {field_summary['reverse']} reverse" if field_summary["reverse"] else ""
        )
        self.stdout.write(
            f"  Fields: {field_summary['db']} DB, {field_summary['fk']} FK, "
            f"{field_summary['m2m']} M2M{reverse}, "
            f"{field_summary['property']} property"
        )
        self.stdout.write(f"  Public access: {public_access}")
        unscoped = sorted(config.get("unscoped_properties") or ())
        if unscoped:
            self.stdout.write(
                self.style.WARNING(
                    f"  Unscoped properties (no sandbox): {', '.join(unscoped)}"
                )
            )

        # Tenancy / predicate report
        self._report_tenancy(model, config)

        if issues:
            self.stdout.write(self.style.ERROR("  Issues:"))
            for issue in issues:
                self.stdout.write(self.style.ERROR(issue))
        elif blocker:
            self.stdout.write(
                self.style.WARNING(
                    f"  Served by the serializer path, not compiled: {blocker}"
                )
            )
        elif not opted_in:
            self.stdout.write(
                self.style.WARNING(
                    "  Eligible for compiled path. "
                    "Remove 'compiled': False from the turbodrf() config."
                )
            )

    def _report_tenancy(self, model, config):
        """Print row-level access control configuration for the model."""
        from django.conf import settings

        from turbodrf.predicates import has_tenancy_declaration
        from turbodrf.tenancy import (
            AmbiguousTenantPath,
            resolve_tenancy_for_model,
        )

        tenant_model_setting = getattr(settings, "TURBODRF_TENANT_MODEL", None)
        # Same default as the router (turbodrf/settings.py).
        autodetect = getattr(settings, "TURBODRF_AUTODETECT_TENANT", False)
        require = getattr(settings, "TURBODRF_REQUIRE_TENANCY", True)

        if tenant_model_setting is None:
            self.stdout.write("  Tenancy: (TURBODRF_TENANT_MODEL not configured)")
            return

        try:
            tenant_field, predicates, autodetected = resolve_tenancy_for_model(
                model, config, tenant_model_setting, autodetect=autodetect
            )
        except AmbiguousTenantPath as e:
            self.stdout.write(self.style.ERROR(f"  Tenancy: AMBIGUOUS — {e}"))
            return
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"  Tenancy: ERROR — {e}"))
            return

        self.stdout.write(f"  Tenant model: {tenant_model_setting}")

        # The router's gate (TurboDRFRouter.discover_models): within-tenant
        # predicates such as an 'owner_field' don't count.
        if (
            require
            and tenant_field is None
            and not autodetected
            and not has_tenancy_declaration(config)
        ):
            owner_note = (
                " An 'owner_field' alone scopes rows within a tenant, not "
                "across tenants."
                if config.get("owner_field")
                else ""
            )
            self.stdout.write(
                self.style.ERROR(
                    "  Tenancy: NOT DECLARED — router will refuse to "
                    "register this model. Add 'tenant_field' (or "
                    "'tenancy': 'shared' for reference data)." + owner_note
                )
            )
            return

        if config.get("tenancy") == "shared":
            self.stdout.write("  Tenancy: shared (no row scoping)")
            return

        if not tenant_field and not predicates:
            self.stdout.write("  Tenancy: (none)")
            return

        if tenant_field:
            label = f"  Tenant field: {tenant_field}" + (
                " [auto-detected]" if autodetected else ""
            )
            self.stdout.write(self.style.SUCCESS(label))

        if predicates:
            names = [self._describe_predicate(p) for p in predicates]
            self.stdout.write(f"  Within-tenant predicates: {', '.join(names)}")

    def _describe_predicate(self, p):
        """One-line describe of a predicate for command output."""
        from turbodrf.predicates import (
            Conditional,
            Custom,
            Either,
            Group,
            Members,
            Owner,
        )

        if isinstance(p, Owner):
            fields = p.fields[0] if len(p.fields) == 1 else p.fields
            bypass = f", bypass={sorted(p.bypass)}" if p.bypass else ""
            return f"Owner({fields!r}{bypass})"
        if isinstance(p, Members):
            return f"Members({p.m2m_field!r})"
        if isinstance(p, Group):
            return f"Group({p.field!r})"
        if isinstance(p, Conditional):
            return f"Conditional(require={sorted(p.require_roles)})"
        if isinstance(p, Either):
            children = ", ".join(self._describe_predicate(c) for c in p.predicates)
            return f"Either({children})"
        if isinstance(p, Custom):
            return "Custom(...)"
        return type(p).__name__
