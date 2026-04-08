"""
Tests for TurboDRF management commands:
  - turbodrf_check
  - turbodrf_explain
  - turbodrf_benchmark
"""

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from tests.test_app.models import (
    Category,
    RelatedModel,
)


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

    def test_compiled_status_shown(self):
        """Compiled models should show 'compiled' status."""
        out, _ = self._call()
        # CompiledSampleModel has compiled=True
        # The output has the model name and status on the same line
        lines = out.split("\n")
        compiled_line = [line for line in lines if "CompiledSampleModel" in line][0]
        self.assertIn("compiled", compiled_line)

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

    def test_eligible_hint_for_non_compiled(self):
        """Non-compiled eligible models should get the hint to add compiled: True."""
        out, _ = self._call("--model", "SampleModel")
        # SampleModel doesn't set compiled=True
        self.assertIn("Eligible for compiled path", out)

    def test_compiled_model_no_eligible_hint(self):
        """Already-compiled model should NOT get the eligibility hint."""
        out, _ = self._call("--model", "CompiledSampleModel")
        self.assertNotIn("Eligible for compiled path", out)

    def test_models_sorted_alphabetically(self):
        """Output models should be sorted by name."""
        out, _ = self._call()
        lines = out.split("\n")
        model_lines = [line for line in lines if "test_app." in line]
        model_names = [line.split(".")[1].split()[0] for line in model_lines]
        self.assertEqual(model_names, sorted(model_names))


class TurboDRFExplainCommandTest(TestCase):
    """Tests for the turbodrf_explain management command."""

    def _call(self, *args, **kwargs):
        out = StringIO()
        call_command("turbodrf_explain", *args, stdout=out, **kwargs)
        return out.getvalue()

    def test_nonexistent_model_raises(self):
        """Explaining an unknown model should raise CommandError."""
        with self.assertRaises(CommandError) as ctx:
            self._call("NoSuchModel")
        self.assertIn("No TurboDRF model named 'NoSuchModel'", str(ctx.exception))

    def test_simple_fields_shown(self):
        """Simple DB fields should be listed under 'Simple fields'."""
        out = self._call("CompiledSampleModel")
        self.assertIn("Simple fields:", out)
        self.assertIn("title", out)
        self.assertIn("is_active", out)

    def test_decimal_coercion_annotation(self):
        """Decimal fields should show the (-> str) coercion marker."""
        out = self._call("CompiledSampleModel")
        # price is DecimalField and should be coerced
        self.assertIn("str", out)

    def test_fk_annotations_shown(self):
        """FK traversals should appear under 'FK annotations'."""
        out = self._call("CompiledSampleModel")
        self.assertIn("FK annotations:", out)
        self.assertIn("related_name", out)
        self.assertIn("related__name", out)

    def test_m2m_fields_shown(self):
        """M2M specs should appear under 'M2M fields'."""
        out = self._call("CompiledArticle")
        self.assertIn("M2M fields:", out)
        self.assertIn("categories", out)
        self.assertIn("Category", out)

    def test_m2m_sub_fields_shown(self):
        """M2M sub-fields should be listed beneath their parent."""
        out = self._call("CompiledArticle")
        self.assertIn(".name", out)

    def test_property_fields_shown(self):
        """Property fields should appear under 'Property fields'."""
        out = self._call("CompiledSampleModel")
        self.assertIn("Property fields:", out)
        self.assertIn("display_title", out)
        self.assertIn("DictProxy", out)

    def test_complexity_section(self):
        """Complexity section should show total fields, JOINs, and query counts."""
        out = self._call("CompiledSampleModel")
        self.assertIn("Complexity:", out)
        self.assertIn("Total fields:", out)
        self.assertIn("JOINs:", out)
        self.assertIn("Total queries:", out)

    def test_complexity_join_count(self):
        """CompiledSampleModel has 1 FK annotation, so JOINs should be 1."""
        out = self._call("CompiledSampleModel")
        lines = out.split("\n")
        joins_line = [line for line in lines if "JOINs:" in line][0]
        self.assertIn("1", joins_line)

    def test_complexity_m2m_query_count(self):
        """CompiledArticle has 1 M2M, so M2M queries should be 1."""
        out = self._call("CompiledArticle")
        lines = out.split("\n")
        m2m_line = [line for line in lines if "M2M queries:" in line][0]
        self.assertIn("1", m2m_line)
        total_line = [line for line in lines if "Total queries:" in line][0]
        self.assertIn("2", total_line)

    def test_sql_flag_shows_sql(self):
        """--sql flag should include SQL output."""
        out = self._call("CompiledSampleModel", "--sql")
        self.assertIn("SQL:", out)
        self.assertIn("SELECT", out)

    def test_sql_flag_absent_no_sql(self):
        """Without --sql, SQL section should not appear."""
        out = self._call("CompiledSampleModel")
        self.assertNotIn("SQL:", out)

    def test_compiled_status_shown(self):
        """Compiled status should be shown in the header."""
        out = self._call("CompiledSampleModel")
        self.assertIn("Compiled: True", out)

    def test_non_compiled_model_explains_anyway(self):
        """Non-compiled models should still be explainable (force-compiled)."""
        out = self._call("SampleModel")
        self.assertIn("SampleModel", out)
        self.assertIn("Simple fields:", out)
        self.assertIn("Compiled: False", out)

    def test_role_flag_shows_permissions(self):
        """--role viewer should show permission filtering section."""
        out = self._call("SampleModel", "--role", "viewer")
        self.assertIn("Permission filtering", out)
        self.assertIn("viewer", out)

    def test_role_unknown_shows_error(self):
        """--role with an unknown role should show an error message."""
        out = self._call("SampleModel", "--role", "nonexistent_role")
        self.assertIn("not found in TURBODRF_ROLES", out)

    def test_role_viewer_actions(self):
        """Viewer role should show read action for SampleModel."""
        out = self._call("SampleModel", "--role", "viewer")
        self.assertIn("Actions:", out)
        self.assertIn("read", out)

    def test_public_access_shown(self):
        """Public access status should appear in explain output."""
        out = self._call("CompiledSampleModel")
        self.assertIn("Public access: True", out)


class TurboDRFBenchmarkCommandTest(TestCase):
    """Tests for the turbodrf_benchmark management command.

    Note: The benchmark command's DRF path creates a raw DRF serializer with
    the model's list fields. Models whose list fields include FK/M2M traversals
    (e.g. related__name) will fail on the DRF path because DRF ModelSerializer
    doesn't accept __ field names in Meta.fields. Tests here use models with
    simple-only list fields (RelatedModel, Category) to exercise the full path.
    """

    def _call(self, *args, **kwargs):
        out = StringIO()
        call_command("turbodrf_benchmark", *args, stdout=out, **kwargs)
        return out.getvalue()

    def test_nonexistent_model_raises(self):
        """Benchmarking an unknown model should raise CommandError."""
        with self.assertRaises(CommandError) as ctx:
            self._call("NoSuchModel")
        self.assertIn("No TurboDRF model named 'NoSuchModel'", str(ctx.exception))

    def test_empty_model_raises(self):
        """Benchmarking a model with no data should raise CommandError."""
        # RelatedModel has no rows yet
        with self.assertRaises(CommandError) as ctx:
            self._call("RelatedModel")
        self.assertIn("has no data", str(ctx.exception))

    def test_benchmark_produces_timing_output(self):
        """Benchmark should produce DRF/Compiled timing lines and speedup."""
        for i in range(5):
            RelatedModel.objects.create(name=f"Bench {i}", description="desc")
        out = self._call("RelatedModel", "--requests", "10", "--warmup", "2")
        self.assertIn("DRF", out)
        self.assertIn("Compiled", out)
        self.assertIn("Speedup", out)
        self.assertIn("ms", out)

    def test_benchmark_header(self):
        """Benchmark should show model name, object count, and page size."""
        for i in range(3):
            Category.objects.create(name=f"Cat {i}")
        out = self._call(
            "Category",
            "--requests",
            "5",
            "--warmup",
            "1",
            "--page-size",
            "10",
        )
        self.assertIn("Category", out)
        self.assertIn("3 objects", out)
        self.assertIn("page_size=10", out)
        self.assertIn("Requests: 5", out)

    def test_benchmark_non_compiled_model(self):
        """Non-compiled models should still be benchmarkable (force-compiled)."""
        for i in range(3):
            RelatedModel.objects.create(name=f"Bench {i}")
        # RelatedModel doesn't set compiled=True
        out = self._call("RelatedModel", "--requests", "5", "--warmup", "1")
        self.assertIn("Speedup", out)

    def test_benchmark_table_format(self):
        """Output should include the table header row with Path/Avg/p95."""
        Category.objects.create(name="Test")
        out = self._call("Category", "--requests", "5", "--warmup", "1")
        self.assertIn("Path", out)
        self.assertIn("Avg", out)
        self.assertIn("p95", out)

    def test_benchmark_warmup_shown(self):
        """Output header should show warmup count."""
        RelatedModel.objects.create(name="Warmup test")
        out = self._call("RelatedModel", "--requests", "5", "--warmup", "3")
        self.assertIn("3 warmup", out)
