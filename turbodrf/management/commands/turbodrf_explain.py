"""
Explain the compiled query plan for a model.

Shows the field tree, permission pruning, SQL, and complexity for
a given model and optional role.

Usage:
    python manage.py turbodrf_explain ModelName
    python manage.py turbodrf_explain ModelName --role viewer
    python manage.py turbodrf_explain ModelName --fields "title,author.name"
    python manage.py turbodrf_explain ModelName --scope platform --role support
"""

from contextlib import contextmanager

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from turbodrf.backends import RoleOnlyIdentity
from turbodrf.compiler import compile_model
from turbodrf.mixins import TurboDRFMixin, _model_config
from turbodrf.scope import PLATFORM, SCOPES, TENANT, is_platform, use_scope

_MISSING = object()


@contextmanager
def _compiled_config(model):
    """Temporarily make ``model.turbodrf()`` opt in to the compiled path (in
    its platform block, on the platform scope), so a model that opts out can
    still be explained. Always restored."""
    original = model.__dict__.get("turbodrf", _MISSING)
    config = dict(model.turbodrf())
    if is_platform():
        config["platform"] = dict(config.get("platform") or {}, compiled=True)
    else:
        config["compiled"] = True
    model.turbodrf = classmethod(lambda cls: config)
    try:
        yield
    finally:
        if original is _MISSING:
            del model.turbodrf
        else:
            model.turbodrf = original


class _RoleUser(RoleOnlyIdentity):
    """A caller holding only ``role`` (static permission mode, or the
    platform scope). Never cached: no pk."""

    def __init__(self, role):
        super().__init__([role])


class Command(BaseCommand):
    help = "Explain the compiled query plan for a TurboDRF model"

    def add_arguments(self, parser):
        parser.add_argument("model_name", type=str, help="Model name to explain")
        parser.add_argument(
            "--role",
            type=str,
            help="Show the list response a caller with this role gets "
            "(static permission mode)",
        )
        parser.add_argument(
            "--fields",
            type=str,
            help="Comma-separated ?fields= selection, as a client sends it "
            "(only configured fields apply)",
        )
        parser.add_argument(
            "--sql",
            action="store_true",
            help="Show the generated SQL query",
        )
        parser.add_argument(
            "--scope",
            choices=SCOPES,
            default=TENANT,
            help="Explain the tenant API's list (default) or the platform "
            "API's (the model's platform block; --role is a platform role)",
        )

    def handle(self, *args, **options):
        scope = options.get("scope") or TENANT
        with use_scope(scope):
            return self._explain(**options)

    def _explain(self, **options):
        model_name = options["model_name"]
        role = options.get("role")
        fields = options.get("fields")
        show_sql = options.get("sql", False)

        # Find the model
        model = None
        for m in apps.get_models():
            if issubclass(m, TurboDRFMixin) and m.__name__ == model_name:
                model = m
                break

        if model is None:
            raise CommandError(f"No TurboDRF model named '{model_name}' found.")
        if is_platform() and "platform" not in model.turbodrf():
            raise CommandError(
                f"{model_name} has no platform block: the platform API doesn't "
                f"serve it."
            )

        # Compiled exactly when the router compiles it: opted in (the
        # default) and compilable. The current scope's config.
        config = _model_config(model)
        plan = compile_model(model) if config.get("compiled", True) else None
        compiled = plan is not None
        if plan is None:
            # Explain anyway: compile with the opt-in forced on.
            with _compiled_config(model):
                plan = compile_model(model)

        if plan is None:
            raise CommandError(f"Could not compile {model_name}.")

        name = f"{model._meta.app_label}.{model.__name__}"
        if is_platform():
            name += " (platform scope)"
        self.stdout.write(f"\n{self.style.SUCCESS(name)}")
        if compiled:
            self.stdout.write("  Compiled: True")
        elif not config.get("compiled", True):
            self.stdout.write("  Compiled: False (opted out with 'compiled': False)")
        else:
            self.stdout.write("  Compiled: False")
        if is_platform():
            self.stdout.write(
                "  Rows: every tenant (no tenant boundary, no predicates)"
            )
        else:
            self.stdout.write(f"  Public access: {config.get('public_access', False)}")

        # Field tree
        self.stdout.write(f"\n  {self.style.NOTICE('Simple fields:')}")
        for f in plan.simple_fields:
            coerced = " (→ str)" if f in plan.type_coercers else ""
            self.stdout.write(f"    {f}{coerced}")

        if plan.fk_annotations:
            self.stdout.write(f"\n  {self.style.NOTICE('FK annotations:')}")
            for output_key, f_expr in plan.fk_annotations.items():
                coerced = " (→ str)" if output_key in plan.type_coercers else ""
                self.stdout.write(f"    {output_key} ← {f_expr.name}{coerced}")

        if plan.m2m_specs:
            self.stdout.write(f"\n  {self.style.NOTICE('M2M fields:')}")
            for m2m_name, spec in plan.m2m_specs.items():
                self.stdout.write(
                    f"    {m2m_name} → {spec['related_model'].__name__}"
                    f" (via {spec['through_model'].__name__})"
                )
                for sub in spec["sub_fields"]:
                    self.stdout.write(f"      .{sub}")

        if plan.property_fields:
            from turbodrf.properties import property_relations, unscoped_properties

            relations = property_relations(model)
            unscoped = unscoped_properties(model)
            self.stdout.write(f"\n  {self.style.NOTICE('Property fields:')}")
            for prop_name in plan.property_fields:
                if prop_name in unscoped:
                    how = "unscoped"
                else:
                    loads = ", ".join(relations.get(prop_name) or ()) or "none"
                    how = f"sandboxed, relations: {loads}"
                self.stdout.write(f"    {prop_name} ({how})")

        # What a caller gets: the role's permissions and ?fields=.
        compiled_qs, active_plan = plan.apply_to_queryset(model._default_manager.all())
        if role or fields:
            selected = self._select(model, plan, role, fields)
            if selected is None:
                return
            compiled_qs, active_plan = selected

        # Complexity
        active_simple, active_fk, active_m2m, active_props = active_plan
        self.stdout.write(f"\n  {self.style.NOTICE('Complexity:')}")
        self.stdout.write(f"    Total fields: {len(_rendered_keys(plan, active_plan))}")
        self.stdout.write(f"    JOINs: {len(active_fk)}")
        self.stdout.write(f"    M2M queries: {len(active_m2m)}")
        # A paginated list runs a COUNT, the SELECT, and one query per M2M.
        self.stdout.write(f"    Total queries: {2 + len(active_m2m)}")
        if active_props:
            self.stdout.write(
                "    (+ property fields: one query per batch of rows, plus one "
                "per declared relation)"
            )

        # SQL
        if show_sql:
            self.stdout.write(f"\n  {self.style.NOTICE('SQL:')}")
            self.stdout.write(f"    {compiled_qs.query}")
            if not is_platform():
                self.stdout.write(
                    "    (before the per-request tenant / predicate filters)"
                )

    def _select(self, model, plan, role, fields):
        """The (queryset, active plan) the compiled list uses for a caller
        with ``role`` sending ``?fields=``, computed by the viewset's own
        field gates (TurboDRFViewSet._compiled_field_gates). None when the
        role can't be explained."""
        from django.conf import settings
        from rest_framework.request import Request
        from rest_framework.test import APIRequestFactory

        from turbodrf.backends import get_permission_mode, platform_roles_config
        from turbodrf.settings import TURBODRF_ROLES as default_roles
        from turbodrf.views import TurboDRFViewSet

        query = {"fields": fields} if fields else {}
        request = Request(APIRequestFactory().get("/", query))
        view = TurboDRFViewSet()
        view.model = model
        view.request = request
        view.format_kwarg = None
        view.action = "list"
        if is_platform():
            view.turbodrf_scope = PLATFORM

        if role:
            if is_platform():
                roles_config, setting = (
                    platform_roles_config(),
                    "TURBODRF_PLATFORM_ROLES",
                )
            elif get_permission_mode() != "static":
                self.stdout.write(
                    self.style.ERROR(
                        "\n  --role is supported in static permission mode only "
                        "(database mode reads roles per user)."
                    )
                )
                return None
            else:
                roles_config = getattr(settings, "TURBODRF_ROLES", default_roles)
                setting = "TURBODRF_ROLES"
            if role not in roles_config:
                self.stdout.write(
                    self.style.ERROR(f"\n  Role '{role}' not found in {setting}")
                )
                return None
            request.user = _RoleUser(role)
            snapshot = view._caller_snapshot(request)
            header = f"Permission filtering (role: {role}):"
            self.stdout.write(f"\n  {self.style.NOTICE(header)}")
            actions = ", ".join(sorted(snapshot.allowed_actions)) or "none"
            self.stdout.write(f"    Actions: {actions}")

            readable, allowed_fk, allowed_m2m = view._compiled_field_gates(
                request, plan
            )
        else:
            header = f"Field selection (?fields={fields}):"
            self.stdout.write(f"\n  {self.style.NOTICE(header)}")
            # No role: show the fields themselves, before any permission rule.
            readable, allowed_fk, allowed_m2m = view._compiled_field_gates(
                request, plan, permissions=False
            )

        manager = model._default_manager
        everything = _rendered_keys(plan, plan.apply_to_queryset(manager.none())[1])
        selected = plan.apply_to_queryset(
            manager.all(),
            readable,
            allowed_fk_keys=allowed_fk,
            allowed_m2m_subfields=allowed_m2m,
        )
        rendered = _rendered_keys(plan, selected[1])
        pruned = [key for key in everything if key not in rendered]
        self.stdout.write(f"    Rendered: {', '.join(rendered) or '(none)'}")
        if pruned:
            self.stdout.write(self.style.ERROR(f"    Pruned: {', '.join(pruned)}"))
        else:
            self.stdout.write("    All fields permitted")
        return selected


def _rendered_keys(plan, active_plan):
    """The keys a compiled list row has for ``active_plan`` (what
    CompiledQueryPlan.post_process leaves)."""
    active_simple, active_fk, active_m2m, active_props = active_plan
    keys = [
        f
        for f in active_simple
        if f != plan.pk_field or plan.pk_field in plan._original_fields
    ]
    return keys + list(active_fk) + list(active_m2m) + list(active_props)
