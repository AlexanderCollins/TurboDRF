"""
Explain the compiled query plan for a model.

Shows the field tree, permission pruning, SQL, and complexity for
a given model and optional role.

Usage:
    python manage.py turbodrf_explain ModelName
    python manage.py turbodrf_explain ModelName --role viewer
    python manage.py turbodrf_explain ModelName --fields "title,author.name"
"""

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from turbodrf.compiler import compile_model
from turbodrf.mixins import TurboDRFMixin


class Command(BaseCommand):
    help = "Explain the compiled query plan for a TurboDRF model"

    def add_arguments(self, parser):
        parser.add_argument("model_name", type=str, help="Model name to explain")
        parser.add_argument(
            "--role",
            type=str,
            help="Show plan as seen by this role (permission filtering)",
        )
        parser.add_argument(
            "--fields",
            type=str,
            help="Comma-separated field list (overrides model config)",
        )
        parser.add_argument(
            "--sql",
            action="store_true",
            help="Show the generated SQL query",
        )

    def handle(self, *args, **options):
        model_name = options["model_name"]
        role = options.get("role")
        show_sql = options.get("sql", False)

        # Find the model
        model = None
        for m in apps.get_models():
            if issubclass(m, TurboDRFMixin) and m.__name__ == model_name:
                model = m
                break

        if model is None:
            raise CommandError(f"No TurboDRF model named '{model_name}' found.")

        # Compile
        plan = compile_model(model)
        if plan is None:
            # Force compile for explanation
            config = model.turbodrf()
            config["compiled"] = True
            original_turbodrf = model.turbodrf
            model.turbodrf = classmethod(lambda cls: config).__get__(model, type(model))
            plan = compile_model(model)
            model.turbodrf = original_turbodrf

        if plan is None:
            raise CommandError(f"Could not compile {model_name}.")

        config = model.turbodrf()
        name = f"{model._meta.app_label}.{model.__name__}"
        self.stdout.write(f"\n{self.style.SUCCESS(name)}")
        self.stdout.write(f"  Compiled: {config.get('compiled', False)}")
        self.stdout.write(f"  Public access: {config.get('public_access', False)}")

        # Field tree
        self.stdout.write(f"\n  {self.style.NOTICE('Simple fields:')}")
        for f in plan.simple_fields:
            coerced = f" (→ str)" if f in plan.type_coercers else ""
            self.stdout.write(f"    {f}{coerced}")

        if plan.fk_annotations:
            self.stdout.write(f"\n  {self.style.NOTICE('FK annotations:')}")
            for output_key, f_expr in plan.fk_annotations.items():
                coerced = f" (→ str)" if output_key in plan.type_coercers else ""
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
            self.stdout.write(f"\n  {self.style.NOTICE('Property fields:')}")
            for prop_name in plan.property_fields:
                self.stdout.write(f"    {prop_name} (via DictProxy)")

        # Permission filtering
        if role:
            self._show_role_filtering(model, plan, role)

        # Complexity
        num_joins = len(plan.fk_annotations)
        num_m2m_queries = len(plan.m2m_specs)
        total_fields = (
            len(plan.simple_fields)
            + len(plan.fk_annotations)
            + sum(len(s["sub_fields"]) for s in plan.m2m_specs.values())
            + len(plan.property_fields)
        )
        self.stdout.write(f"\n  {self.style.NOTICE('Complexity:')}")
        self.stdout.write(f"    Total fields: {total_fields}")
        self.stdout.write(f"    JOINs: {num_joins}")
        self.stdout.write(f"    M2M queries: {num_m2m_queries}")
        self.stdout.write(f"    Total queries: {1 + num_m2m_queries}")

        # SQL
        if show_sql:
            self._show_sql(model, plan)

    def _show_role_filtering(self, model, plan, role_name):
        from django.conf import settings
        from turbodrf.settings import TURBODRF_ROLES as default_roles

        roles_config = getattr(settings, "TURBODRF_ROLES", default_roles)
        if role_name not in roles_config:
            self.stdout.write(
                self.style.ERROR(f"\n  Role '{role_name}' not found in TURBODRF_ROLES")
            )
            return

        # Build a mock snapshot for this role
        from turbodrf.backends import build_permission_snapshot_static

        class MockUser:
            is_authenticated = True
            id = 0
            roles = [role_name]
            _test_roles = [role_name]

        snapshot = build_permission_snapshot_static(MockUser(), model)

        self.stdout.write(f"\n  {self.style.NOTICE(f'Permission filtering (role: {role_name}):')}")
        self.stdout.write(f"    Actions: {snapshot.allowed_actions or 'none'}")

        if snapshot.readable_fields:
            # Show which fields survive
            all_fields = set(plan.simple_fields)
            all_fields.update(plan.fk_annotations.keys())
            all_fields.update(plan.m2m_specs.keys())
            all_fields.update(plan.property_fields.keys())

            permitted = all_fields & snapshot.readable_fields
            pruned = all_fields - snapshot.readable_fields

            if pruned:
                self.stdout.write(
                    f"    Permitted: {', '.join(sorted(permitted))}"
                )
                self.stdout.write(
                    self.style.ERROR(
                        f"    Pruned: {', '.join(sorted(pruned))}"
                    )
                )
            else:
                self.stdout.write(f"    All fields permitted")

    def _show_sql(self, model, plan):
        queryset = model.objects.all()
        compiled_qs, _ = plan.apply_to_queryset(queryset)
        sql = str(compiled_qs.query)

        self.stdout.write(f"\n  {self.style.NOTICE('SQL:')}")
        self.stdout.write(f"    {sql}")
