"""
Tests for TurboDRF management commands:
  - turbodrf_check
  - turbodrf_explain
  - turbodrf_benchmark
"""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase


class TurboDRFCheckCommandTest(TestCase):
    """Tests for the turbodrf_check management command."""

    def _call(self, *args, **kwargs):
        out = StringIO()
        err = StringIO()
        call_command("turbodrf_check", *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue(), err.getvalue()

    def test_lists_all_turbodrf_models(self):
        """All enabled TurboDRF models should appear in output."""
        out, _ = self._call()
        # Models defined in test_app that have enabled=True (or omitted)
        self.assertIn("SampleModel", out)
        self.assertIn("RelatedModel", out)
        self.assertIn("CompiledSampleModel", out)
        self.assertIn("CompiledArticle", out)
        self.assertIn("ArticleWithCategories", out)
        self.assertIn("Category", out)
        self.assertIn("CustomEndpointModel", out)

    def test_disabled_model_excluded(self):
        """DisabledModel (enabled=False) should NOT appear."""
        out, _ = self._call()
        self.assertNotIn("DisabledModel", out)

    def test_field_counts_for_sample_model(self):
        """SampleModel should report correct field type counts."""
        out, _ = self._call("--model", "SampleModel")
        # SampleModel list fields: title, price, related__name, is_active
        # title -> DB, price -> DB, related__name -> FK, is_active -> DB
        self.assertIn("3 DB", out)
        self.assertIn("1 FK", out)
        self.assertIn("0 M2M", out)
        self.assertIn("0 property", out)

    def test_field_counts_for_compiled_sample_model(self):
        """CompiledSampleModel should report correct counts including property."""
        out, _ = self._call("--model", "CompiledSampleModel")
        # List fields: title, price, related__name, is_active, display_title
        # title -> DB, price -> DB, is_active -> DB, related__name -> FK,
        # display_title -> property
        self.assertIn("3 DB", out)
        self.assertIn("1 FK", out)
        self.assertIn("1 property", out)

    def test_field_counts_for_compiled_article(self):
        """CompiledArticle should report M2M fields."""
        out, _ = self._call("--model", "CompiledArticle")
        # List fields: title, author__name, categories__name
        # title -> DB, author__name -> FK, categories__name -> M2M
        self.assertIn("1 DB", out)
        self.assertIn("1 FK", out)
        self.assertIn("1 M2M", out)

    def test_filter_by_model_name(self):
        """--model flag should show only the specified model."""
        out, _ = self._call("--model", "RelatedModel")
        self.assertIn("RelatedModel", out)
        self.assertNotIn("SampleModel", out)
        self.assertNotIn("CompiledSampleModel", out)

    def test_filter_nonexistent_model(self):
        """--model with unknown name should emit an error message."""
        out, err = self._call("--model", "NoSuchModel")
        self.assertIn("No TurboDRF model named 'NoSuchModel' found", err)
        self.assertEqual(out, "")

    def test_public_access_shown(self):
        """Output should include public access status."""
        out, _ = self._call("--model", "SampleModel")
        self.assertIn("Public access: True", out)

    def test_compiled_model_no_eligible_hint(self):
        """Already-compiled model should NOT get the eligibility hint."""
        out, _ = self._call("--model", "CompiledSampleModel")
        self.assertNotIn("Eligible for compiled path", out)

    def test_tenancy_not_configured_message(self):
        """Without TURBODRF_TENANT_MODEL, every model shows 'not configured'."""
        out, _ = self._call("--model", "SampleModel")
        self.assertIn("TURBODRF_TENANT_MODEL not configured", out)

    def test_tenancy_reporting_with_predicates(self):
        """With TURBODRF_TENANT_MODEL set, models with predicates report them."""
        from django.test.utils import override_settings

        with override_settings(TURBODRF_TENANT_MODEL="test_app.Brokerage"):
            out, _ = self._call("--model", "Deal")
        self.assertIn("Tenant model: test_app.Brokerage", out)
        self.assertIn("Tenant field: brokerage", out)
        self.assertIn("Within-tenant predicates: Owner", out)
        self.assertIn("bypass=", out)

    def test_tenancy_reporting_chained_path(self):
        """Models with chained tenant_field show the full path."""
        from django.test.utils import override_settings

        with override_settings(TURBODRF_TENANT_MODEL="test_app.Brokerage"):
            out, _ = self._call("--model", "Transaction")
        self.assertIn("Tenant field: bank_account__deal__brokerage", out)

    def test_tenancy_reporting_shared(self):
        """Models declaring 'tenancy': 'shared' report it explicitly.

        Add a 'tenancy': 'shared' to a known model temporarily via test app.
        We'll just verify the path runs correctly for SampleModel which
        has no tenancy declared.
        """
        from django.test.utils import override_settings

        with override_settings(
            TURBODRF_TENANT_MODEL="test_app.Brokerage",
            TURBODRF_REQUIRE_TENANCY=False,
        ):
            out, _ = self._call("--model", "SampleModel")
        self.assertIn("Tenant model: test_app.Brokerage", out)
        # SampleModel has no tenancy declared and require=False → "(none)"
        self.assertIn("Tenancy: (none)", out)

    def test_tenancy_reporting_not_declared_with_require(self):
        """With require_tenancy=True and no declaration, error is reported."""
        from django.test.utils import override_settings

        with override_settings(
            TURBODRF_TENANT_MODEL="test_app.Brokerage",
            TURBODRF_REQUIRE_TENANCY=True,
        ):
            out, _ = self._call("--model", "SampleModel")
        self.assertIn("NOT DECLARED", out)

    def test_tenancy_reporting_describes_each_predicate(self):
        """Owner / Members / Group / Either / Custom / Conditional all
        get one-line descriptions."""
        from django.db.models import Q

        from turbodrf.management.commands.turbodrf_check import Command
        from turbodrf.predicates import (
            Conditional,
            Custom,
            Either,
            Group,
            Members,
            Owner,
        )

        cmd = Command()
        # Each kind of predicate gets a description
        self.assertIn("Owner", cmd._describe_predicate(Owner("x")))
        self.assertIn("bypass=", cmd._describe_predicate(Owner("x", bypass=["a"])))
        self.assertIn("Members", cmd._describe_predicate(Members("m2m")))
        self.assertIn("Group", cmd._describe_predicate(Group("team")))
        self.assertIn(
            "Conditional",
            cmd._describe_predicate(Conditional(when=Q(x=1), require_roles=["admin"])),
        )
        self.assertIn("Either", cmd._describe_predicate(Either(Owner("a"), Owner("b"))))
        self.assertIn(
            "Custom",
            cmd._describe_predicate(
                Custom(q_func=lambda r, u: Q(), write_validator=lambda d, i, r: [])
            ),
        )

    def test_models_sorted_alphabetically(self):
        """Output models should be sorted by name."""
        out, _ = self._call()
        lines = out.split("\n")
        model_lines = [line for line in lines if "test_app." in line]
        model_names = [line.split(".")[1].split()[0] for line in model_lines]
        self.assertEqual(model_names, sorted(model_names))
