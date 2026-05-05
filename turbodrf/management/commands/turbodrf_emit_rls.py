"""
Emit draft Postgres RLS policies based on declared predicates.

Usage:
    python manage.py turbodrf_emit_rls > rls.sql
    python manage.py turbodrf_emit_rls --model Deal

The output is a starting point — review it and migrate it in deliberately.
TurboDRF does not manage RLS policy lifecycle.

Predicates that don't cleanly map to RLS (Members, Group, Conditional with
arbitrary Q, Custom) are skipped with a comment so the dev knows to write
those policies manually.
"""

from django.apps import apps
from django.core.management.base import BaseCommand

from turbodrf.mixins import TurboDRFMixin


class Command(BaseCommand):
    help = "Emit draft Postgres RLS policies for TurboDRF models."

    def add_arguments(self, parser):
        parser.add_argument(
            "--model",
            type=str,
            help="Emit policies for a specific model only (by class name)",
        )

    def handle(self, *args, **options):
        from django.conf import settings

        from turbodrf.tenancy import resolve_tenancy_for_model

        tenant_model_setting = getattr(settings, "TURBODRF_TENANT_MODEL", None)
        autodetect = getattr(settings, "TURBODRF_AUTODETECT_TENANT", True)
        target = options.get("model")

        self.stdout.write("-- TurboDRF RLS draft — review before applying")
        self.stdout.write("-- Generated from predicates declared in turbodrf() configs")
        self.stdout.write("-- Caveats:")
        self.stdout.write(
            "--   * Members / Group / Conditional / Custom predicates are NOT emitted"
        )
        self.stdout.write(
            "--     and must be written manually (see docs/rls.md for templates)"
        )
        self.stdout.write(
            "--   * Chained tenant_field paths are not supported in RLS — add a"
        )
        self.stdout.write(
            "--     Tenant policy on each table referencing the closest tenant FK"
        )
        self.stdout.write("")
        self.stdout.write(
            "-- Required: install TurboDRFTenancyMiddleware so app.user_id /"
        )
        self.stdout.write(
            "--           app.tenant_id / app.user_roles are set per request"
        )
        self.stdout.write("")

        for model in apps.get_models():
            if not issubclass(model, TurboDRFMixin):
                continue
            config = model.turbodrf()
            if not config.get("enabled", True):
                continue
            if target and model.__name__ != target:
                continue

            try:
                tenant_field, predicates, _ = resolve_tenancy_for_model(
                    model, config, tenant_model_setting, autodetect=autodetect
                )
            except Exception as e:
                self.stdout.write(f"-- {model.__name__}: ERROR resolving tenancy — {e}")
                continue

            if not tenant_field and not predicates:
                continue

            self._emit_for_model(model, tenant_field, predicates)

    def _emit_for_model(self, model, tenant_field, predicates):
        """Emit RLS SQL for a single model.

        Tenant boundary is emitted as its own policy (mandatory layer).
        Within-tenant predicates emitted as additional policies.
        """
        from turbodrf.predicates import Tenant

        table = model._meta.db_table
        name = f"{model._meta.app_label}.{model.__name__}"

        self.stdout.write(f"-- {name}")
        self.stdout.write(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        self.stdout.write(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")

        if tenant_field:
            policy_name = f"{table}_tenant"
            try:
                clause = Tenant(tenant_field).to_rls_using_clause()
                self.stdout.write(
                    f"CREATE POLICY {policy_name} ON {table} USING ({clause});"
                )
            except NotImplementedError as e:
                self.stdout.write(f"-- {policy_name}: SKIPPED — {e}")
                self.stdout.write(
                    f"-- (write a manual policy on {table} for tenant field "
                    f"{tenant_field!r})"
                )

        for i, pred in enumerate(predicates):
            policy_name = f"{table}_{type(pred).__name__.lower()}_{i}"
            try:
                clause = pred.to_rls_using_clause()
            except NotImplementedError as e:
                self.stdout.write(f"-- {policy_name}: SKIPPED — {e}")
                self.stdout.write(
                    f"-- (write a manual policy on {table} for this predicate)"
                )
                continue
            self.stdout.write(
                f"CREATE POLICY {policy_name} ON {table} USING ({clause});"
            )

        self.stdout.write("")
