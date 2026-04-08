"""
Tests to improve coverage for turbodrf/mixins.py, turbodrf/utils.py,
and turbodrf/validation.py.
"""

from unittest.mock import MagicMock, patch

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.test import TestCase, override_settings

from tests.test_app.models import (
    ArticleWithCategories,
    Category,
    RelatedModel,
    SampleModel,
)
from turbodrf.mixins import TurboDRFMixin
from turbodrf.utils import create_options_metadata
from turbodrf.validation import (
    get_max_nesting_depth,
    get_nested_field_model,
    validate_filter_field,
    validate_nesting_depth,
)

# ---------------------------------------------------------------------------
# Mixin tests: get_api_fields, get_field_type
# ---------------------------------------------------------------------------


class TestGetApiFields(TestCase):
    """Test TurboDRFMixin.get_api_fields for all config shapes."""

    def test_dict_fields_list_view(self):
        """Dict config returns the 'list' key fields."""
        fields = SampleModel.get_api_fields("list")
        self.assertEqual(fields, ["title", "price", "related__name", "is_active"])

    def test_dict_fields_detail_view(self):
        """Dict config returns the 'detail' key fields."""
        fields = SampleModel.get_api_fields("detail")
        self.assertIn("description", fields)
        self.assertIn("secret_field", fields)
        self.assertIn("related__description", fields)

    def test_dict_fields_missing_view_type_returns_empty(self):
        """Requesting a view_type not in the dict returns []."""
        fields = SampleModel.get_api_fields("nonexistent")
        self.assertEqual(fields, [])

    def test_list_fields_returns_same_for_both_views(self):
        """A plain list config is returned for any view type."""
        fields_list = RelatedModel.get_api_fields("list")
        fields_detail = RelatedModel.get_api_fields("detail")
        self.assertEqual(fields_list, ["name", "description"])
        self.assertEqual(fields_detail, ["name", "description"])

    def test_all_fields_excludes_reverse_relations(self):
        """__all__ returns concrete fields, excluding m2m and one_to_many."""
        fields = Category.get_api_fields("list")
        # Category has name, description, id — but NOT articles (reverse m2m)
        self.assertIn("name", fields)
        self.assertIn("description", fields)
        self.assertNotIn("articles", fields)

    def test_default_turbodrf_config(self):
        """The base TurboDRFMixin.turbodrf returns __all__ and enabled=True."""
        # Cannot call TurboDRFMixin.turbodrf() directly (no _meta),
        # but Category uses __all__ via its own config with just fields list.
        # Instead, test the default on a model that doesn't override turbodrf.
        # Use Category which overrides — test the base config dict shape.
        config = TurboDRFMixin.turbodrf.__func__(Category)
        self.assertEqual(config["fields"], "__all__")
        self.assertTrue(config["enabled"])
        self.assertIn("endpoint", config)

    def test_default_view_type_is_list(self):
        """Calling get_api_fields() with no arg defaults to 'list'."""
        fields = SampleModel.get_api_fields()
        self.assertEqual(fields, ["title", "price", "related__name", "is_active"])


class TestGetFieldType(TestCase):
    """Test TurboDRFMixin.get_field_type for FK traversal and edge cases."""

    def test_simple_field(self):
        """Direct field returns the field instance."""
        field = SampleModel.get_field_type("title")
        self.assertIsNotNone(field)
        self.assertEqual(field.name, "title")

    def test_fk_nested_field(self):
        """FK traversal returns the remote field."""
        field = SampleModel.get_field_type("related__name")
        self.assertIsNotNone(field)
        self.assertEqual(field.name, "name")

    def test_nonexistent_leaf_field(self):
        """Invalid final field returns None."""
        result = SampleModel.get_field_type("related__nonexistent")
        self.assertIsNone(result)

    def test_nonexistent_intermediate_field(self):
        """Invalid intermediate relationship returns None."""
        result = SampleModel.get_field_type("bogus__name")
        self.assertIsNone(result)

    def test_non_relational_intermediate_raises(self):
        """A non-relational field with related_model=None in the middle causes error.

        CharField has related_model=None, so hasattr returns True but the
        model gets set to None, causing AttributeError on the next step.
        """
        with self.assertRaises(AttributeError):
            SampleModel.get_field_type("title__something")

    def test_deeper_nesting(self):
        """Two-level traversal: article -> author (FK) -> name."""
        field = ArticleWithCategories.get_field_type("author__name")
        self.assertIsNotNone(field)
        self.assertEqual(field.name, "name")

    def test_single_field_no_parts(self):
        """Single field path with no __ still works."""
        field = RelatedModel.get_field_type("description")
        self.assertIsNotNone(field)
        self.assertEqual(field.name, "description")


# ---------------------------------------------------------------------------
# Utils tests: create_options_metadata
# ---------------------------------------------------------------------------


class TestCreateOptionsMetadata(TestCase):
    """Test the OPTIONS metadata builder in utils.py."""

    def _make_user(self, roles):
        """Create a mock user with roles."""
        user = MagicMock()
        user.roles = roles
        return user

    def test_basic_metadata_structure(self):
        """Metadata has name, description, and fields keys."""
        user = self._make_user(["viewer"])
        with patch(
            "turbodrf.settings.TURBODRF_ROLES",
            {
                "viewer": [
                    "test_app.samplemodel.title.read",
                    "test_app.samplemodel.price.read",
                ]
            },
        ):
            meta = create_options_metadata(SampleModel, ["title", "price"], user)
        self.assertEqual(meta["name"], "sample model")
        self.assertIn("fields", meta)
        self.assertIn("title", meta["fields"])
        self.assertIn("price", meta["fields"])

    def test_nested_field_creates_nested_entry(self):
        """Fields with __ notation produce a nested-type stub."""
        user = self._make_user([])
        with patch("turbodrf.settings.TURBODRF_ROLES", {}):
            meta = create_options_metadata(SampleModel, ["related__name"], user)
        self.assertIn("related", meta["fields"])
        self.assertEqual(meta["fields"]["related"]["type"], "nested")

    def test_field_info_includes_type_and_label(self):
        """Each concrete field has type, label, required, etc."""
        user = self._make_user(["admin"])
        with patch(
            "turbodrf.settings.TURBODRF_ROLES",
            {
                "admin": [
                    "test_app.samplemodel.title.read",
                    "test_app.samplemodel.title.write",
                ]
            },
        ):
            meta = create_options_metadata(SampleModel, ["title"], user)
        title_info = meta["fields"]["title"]
        self.assertEqual(title_info["type"], "CharField")
        self.assertIn("label", title_info)
        self.assertIn("max_length", title_info)
        self.assertEqual(title_info["max_length"], 200)

    def test_field_with_no_permissions_is_readonly_writeonly(self):
        """A field with no matching perms is both read_only and write_only."""
        user = self._make_user(["nobody"])
        with patch("turbodrf.settings.TURBODRF_ROLES", {"nobody": []}):
            meta = create_options_metadata(SampleModel, ["title"], user)
        title_info = meta["fields"]["title"]
        self.assertTrue(title_info["read_only"])  # can't write
        self.assertTrue(title_info["write_only"])  # can't read

    def test_nonexistent_field_produces_unknown(self):
        """A field not on the model falls back to type=unknown."""
        user = self._make_user([])
        with patch("turbodrf.settings.TURBODRF_ROLES", {}):
            meta = create_options_metadata(SampleModel, ["does_not_exist"], user)
        self.assertEqual(meta["fields"]["does_not_exist"]["type"], "unknown")

    def test_description_from_docstring(self):
        """Model docstring is used as description."""
        user = self._make_user([])
        with patch("turbodrf.settings.TURBODRF_ROLES", {}):
            meta = create_options_metadata(SampleModel, [], user)
        self.assertIn("Main test model", meta["description"])

    def test_model_without_docstring(self):
        """Model with no docstring returns empty description."""
        user = self._make_user([])
        model = MagicMock()
        model._meta.verbose_name = "fake"
        model._meta.app_label = "fake"
        model._meta.model_name = "fake"
        model.__doc__ = None
        with patch("turbodrf.settings.TURBODRF_ROLES", {}):
            meta = create_options_metadata(model, [], user)
        self.assertEqual(meta["description"], "")

    def test_field_with_choices(self):
        """Fields with choices include choice list in metadata."""
        user = self._make_user([])
        mock_field = MagicMock()
        mock_field.__class__.__name__ = "CharField"
        mock_field.blank = False
        mock_field.verbose_name = "status"
        mock_field.help_text = ""
        mock_field.choices = [("a", "Active"), ("i", "Inactive")]
        mock_field.max_length = 1

        mock_model = MagicMock()
        mock_model._meta.verbose_name = "thing"
        mock_model._meta.app_label = "app"
        mock_model._meta.model_name = "thing"
        mock_model.__doc__ = ""
        mock_model._meta.get_field.return_value = mock_field

        with patch("turbodrf.settings.TURBODRF_ROLES", {}):
            meta = create_options_metadata(mock_model, ["status"], user)
        self.assertIn("choices", meta["fields"]["status"])
        self.assertEqual(len(meta["fields"]["status"]["choices"]), 2)
        self.assertEqual(
            meta["fields"]["status"]["choices"][0],
            {"value": "a", "display": "Active"},
        )

    def test_multiple_roles_union_permissions(self):
        """Permissions from multiple roles are unioned."""
        user = self._make_user(["role_a", "role_b"])
        with patch(
            "turbodrf.settings.TURBODRF_ROLES",
            {
                "role_a": ["test_app.samplemodel.title.read"],
                "role_b": ["test_app.samplemodel.title.write"],
            },
        ):
            meta = create_options_metadata(SampleModel, ["title"], user)
        title_info = meta["fields"]["title"]
        self.assertFalse(title_info["read_only"])
        self.assertFalse(title_info["write_only"])

    def test_help_text_included(self):
        """help_text from field is included in metadata."""
        user = self._make_user([])
        mock_field = MagicMock()
        mock_field.__class__.__name__ = "CharField"
        mock_field.blank = True
        mock_field.verbose_name = "name"
        mock_field.help_text = "Enter your name"
        mock_field.choices = None
        # No max_length attribute for this test
        del mock_field.max_length

        mock_model = MagicMock()
        mock_model._meta.verbose_name = "thing"
        mock_model._meta.app_label = "app"
        mock_model._meta.model_name = "thing"
        mock_model.__doc__ = ""
        mock_model._meta.get_field.return_value = mock_field

        with patch("turbodrf.settings.TURBODRF_ROLES", {}):
            meta = create_options_metadata(mock_model, ["name"], user)
        self.assertEqual(meta["fields"]["name"]["help_text"], "Enter your name")
        self.assertFalse(meta["fields"]["name"]["required"])

    def test_multiple_nested_fields_same_base(self):
        """Multiple nested fields with same base only create one entry."""
        user = self._make_user([])
        with patch("turbodrf.settings.TURBODRF_ROLES", {}):
            meta = create_options_metadata(
                SampleModel, ["related__name", "related__description"], user
            )
        # related should appear once with nested type
        self.assertEqual(meta["fields"]["related"]["type"], "nested")
        self.assertIn("fields", meta["fields"]["related"])


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestValidateNestingDepth(TestCase):
    """Test nesting depth validation."""

    def test_simple_field_passes(self):
        self.assertTrue(validate_nesting_depth("title", max_depth=3))

    def test_single_nesting_passes(self):
        self.assertTrue(validate_nesting_depth("author__name", max_depth=3))

    def test_max_depth_boundary_passes(self):
        self.assertTrue(validate_nesting_depth("a__b__c__d", max_depth=3))

    def test_exceeds_max_depth_raises(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_nesting_depth("a__b__c__d__e", max_depth=3)
        self.assertIn("exceeds maximum nesting depth", str(ctx.exception))

    @override_settings(TURBODRF_MAX_NESTING_DEPTH=None)
    def test_none_setting_allows_anything(self):
        """When the setting is None, unlimited nesting is allowed."""
        # max_depth=None triggers get_max_nesting_depth() which returns None
        self.assertTrue(validate_nesting_depth("a__b__c__d__e__f__g"))

    def test_uses_setting_when_no_max_depth_given(self):
        """When max_depth arg is omitted, uses the setting (default 3)."""
        with self.assertRaises(ValidationError):
            validate_nesting_depth("a__b__c__d__e")

    def test_zero_depth_rejects_any_nesting(self):
        with self.assertRaises(ValidationError):
            validate_nesting_depth("a__b", max_depth=0)

    def test_zero_depth_allows_simple_field(self):
        self.assertTrue(validate_nesting_depth("name", max_depth=0))

    def test_depth_1_allows_single_relation(self):
        self.assertTrue(validate_nesting_depth("author__name", max_depth=1))

    def test_depth_1_rejects_double_nesting(self):
        with self.assertRaises(ValidationError):
            validate_nesting_depth("a__b__c", max_depth=1)

    def test_error_message_includes_warning(self):
        """Error message contains the unsupported warning text."""
        with self.assertRaises(ValidationError) as ctx:
            validate_nesting_depth("a__b__c__d__e", max_depth=3)
        self.assertIn("UNSUPPORTED", str(ctx.exception))
        self.assertIn("Current depth: 4", str(ctx.exception))


class TestGetMaxNestingDepth(TestCase):
    """Test get_max_nesting_depth reads from settings."""

    @override_settings(TURBODRF_MAX_NESTING_DEPTH=10)
    def test_reads_from_django_settings(self):
        self.assertEqual(get_max_nesting_depth(), 10)

    @override_settings(TURBODRF_MAX_NESTING_DEPTH=None)
    def test_none_means_unlimited(self):
        self.assertIsNone(get_max_nesting_depth())

    def test_default_is_3(self):
        """Without override, the default from turbodrf.settings is used."""
        result = get_max_nesting_depth()
        self.assertEqual(result, 3)


class TestGetNestedFieldModel(TestCase):
    """Test relationship traversal in get_nested_field_model."""

    def test_simple_field(self):
        model, chain = get_nested_field_model(SampleModel, "title")
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0][2], "title")

    def test_fk_traversal(self):
        model, chain = get_nested_field_model(SampleModel, "related__name")
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0][2], "related")
        self.assertEqual(chain[1][2], "name")
        self.assertEqual(model, RelatedModel)

    def test_nonexistent_field_raises(self):
        with self.assertRaises(FieldDoesNotExist):
            get_nested_field_model(SampleModel, "nonexistent")

    def test_nonexistent_nested_field_raises(self):
        with self.assertRaises(FieldDoesNotExist):
            get_nested_field_model(SampleModel, "related__bogus")

    def test_chain_contains_correct_models(self):
        """Each chain entry references the correct model."""
        _, chain = get_nested_field_model(SampleModel, "related__name")
        self.assertEqual(chain[0][0], SampleModel)
        self.assertEqual(chain[1][0], RelatedModel)

    def test_article_author_name(self):
        """Multi-hop: ArticleWithCategories -> author -> name."""
        model, chain = get_nested_field_model(ArticleWithCategories, "author__name")
        self.assertEqual(model, RelatedModel)
        self.assertEqual(chain[0][2], "author")
        self.assertEqual(chain[1][2], "name")

    def test_error_message_includes_model_name(self):
        """FieldDoesNotExist error includes the model name."""
        with self.assertRaises(FieldDoesNotExist) as ctx:
            get_nested_field_model(SampleModel, "nonexistent")
        self.assertIn("SampleModel", str(ctx.exception))

    def test_error_on_nested_nonexistent_includes_related_model(self):
        """Error from a nested lookup mentions the related model."""
        with self.assertRaises(FieldDoesNotExist) as ctx:
            get_nested_field_model(SampleModel, "related__bogus")
        self.assertIn("RelatedModel", str(ctx.exception))


class TestValidateFilterField(TestCase):
    """Test filter parameter parsing and validation."""

    def test_simple_field_exact(self):
        field_path, lookup = validate_filter_field(SampleModel, "title")
        self.assertEqual(field_path, "title")
        self.assertEqual(lookup, "exact")

    def test_field_with_lookup(self):
        field_path, lookup = validate_filter_field(SampleModel, "price__gte")
        self.assertEqual(field_path, "price")
        self.assertEqual(lookup, "gte")

    def test_nested_field_with_lookup(self):
        field_path, lookup = validate_filter_field(
            SampleModel, "related__name__icontains"
        )
        self.assertEqual(field_path, "related__name")
        self.assertEqual(lookup, "icontains")

    def test_or_suffix_stripped(self):
        field_path, lookup = validate_filter_field(SampleModel, "title__icontains_or")
        self.assertEqual(field_path, "title")
        self.assertEqual(lookup, "icontains")

    def test_unknown_lookup_treated_as_field(self):
        """If last part isn't a known lookup, entire string is the field."""
        field_path, lookup = validate_filter_field(SampleModel, "title__foobar")
        self.assertEqual(field_path, "title__foobar")
        self.assertEqual(lookup, "exact")

    def test_exceeds_nesting_depth_raises(self):
        """Deep nesting in filter param triggers validation error."""
        with self.assertRaises(ValidationError):
            validate_filter_field(SampleModel, "a__b__c__d__e__icontains")

    def test_all_standard_lookups_recognized(self):
        """Spot-check several lookups are recognized."""
        for lkup in [
            "iexact",
            "contains",
            "in",
            "lt",
            "lte",
            "gt",
            "startswith",
            "endswith",
            "isnull",
            "regex",
        ]:
            field_path, lookup = validate_filter_field(SampleModel, f"title__{lkup}")
            self.assertEqual(lookup, lkup, f"Lookup '{lkup}' not recognized")
            self.assertEqual(field_path, "title")

    def test_date_lookups_recognized(self):
        for lkup in ["year", "month", "day", "week", "quarter"]:
            _, lookup = validate_filter_field(SampleModel, f"created_at__{lkup}")
            self.assertEqual(lookup, lkup)

    def test_or_suffix_with_nested_field(self):
        """_or suffix works on nested fields too."""
        field_path, lookup = validate_filter_field(
            SampleModel, "related__name__contains_or"
        )
        self.assertEqual(field_path, "related__name")
        self.assertEqual(lookup, "contains")

    def test_simple_or_suffix_no_lookup(self):
        """_or stripped from a simple field with no lookup."""
        field_path, lookup = validate_filter_field(SampleModel, "title_or")
        self.assertEqual(field_path, "title")
        self.assertEqual(lookup, "exact")
