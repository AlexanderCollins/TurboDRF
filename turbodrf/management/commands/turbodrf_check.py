"""
Check which models are eligible for the compiled read path.

Usage:
    python manage.py turbodrf_check
    python manage.py turbodrf_check --model Book
"""

from django.apps import apps
from django.core.exceptions import FieldDoesNotExist
from django.core.management.base import BaseCommand

from turbodrf.mixins import TurboDRFMixin


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
        fields_config = config.get("fields", "__all__")
        compiled = config.get("compiled", False)
        public_access = config.get("public_access", False)

        # Resolve list fields
        if isinstance(fields_config, dict):
            list_fields = fields_config.get("list", "__all__")
        else:
            list_fields = fields_config

        if list_fields == "__all__":
            list_fields = [
                f.name for f in model._meta.get_fields() if hasattr(f, "column")
            ]

        # Check each field for eligibility
        issues = []
        field_summary = {"db": 0, "fk": 0, "m2m": 0, "property": 0}

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
                        issues.append(
                            f"  '{field_name}' is not a DB field or property"
                        )
            else:
                parts = field_name.split("__")
                try:
                    base_field = model._meta.get_field(parts[0])
                    if base_field.many_to_many:
                        field_summary["m2m"] += 1
                    elif hasattr(base_field, "related_model"):
                        field_summary["fk"] += 1
                    else:
                        issues.append(
                            f"  '{field_name}' traverses non-relation field"
                        )
                except FieldDoesNotExist:
                    issues.append(f"  '{parts[0]}' base field does not exist")

        # Output
        name = f"{model._meta.app_label}.{model.__name__}"
        status = self.style.SUCCESS("compiled") if compiled else "not compiled"
        eligible = len(issues) == 0

        if eligible:
            symbol = self.style.SUCCESS("OK")
        else:
            symbol = self.style.ERROR("INELIGIBLE")

        self.stdout.write(f"\n{name} [{status}] {symbol}")
        self.stdout.write(
            f"  Fields: {field_summary['db']} DB, {field_summary['fk']} FK, "
            f"{field_summary['m2m']} M2M, {field_summary['property']} property"
        )
        self.stdout.write(f"  Public access: {public_access}")

        if issues:
            self.stdout.write(self.style.ERROR("  Issues:"))
            for issue in issues:
                self.stdout.write(self.style.ERROR(issue))
        elif not compiled:
            self.stdout.write(
                self.style.WARNING(
                    "  Eligible for compiled path. "
                    "Add 'compiled': True to turbodrf() config."
                )
            )
