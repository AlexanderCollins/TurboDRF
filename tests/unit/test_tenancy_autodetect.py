"""
Unit tests for tenant FK auto-detection and field path validation.
"""

from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    SampleModel,
    Transaction,
)
from turbodrf.tenancy import (
    AmbiguousTenantPath,
    find_tenant_path,
    validate_field_path,
)


class TestFindTenantPath(TestCase):
    def test_direct_fk_to_tenant(self):
        # Deal has a direct FK to Brokerage
        path = find_tenant_path(Deal, Brokerage)
        self.assertEqual(path, "brokerage")

    def test_two_hop_path(self):
        # BankAccount -> deal -> brokerage
        path = find_tenant_path(BankAccount, Brokerage)
        self.assertEqual(path, "deal__brokerage")

    def test_three_hop_path(self):
        # Transaction -> bank_account -> deal -> brokerage
        path = find_tenant_path(Transaction, Brokerage)
        self.assertEqual(path, "bank_account__deal__brokerage")

    def test_no_path_returns_none(self):
        # SampleModel has no FK chain to Brokerage
        path = find_tenant_path(SampleModel, Brokerage)
        self.assertIsNone(path)

    def test_resolve_tenant_model_string(self):
        # Should accept 'app.Model' strings
        path = find_tenant_path(Deal, "test_app.Brokerage")
        self.assertEqual(path, "brokerage")

    def test_invalid_tenant_model_string(self):
        with self.assertRaises(ImproperlyConfigured):
            find_tenant_path(Deal, "nonexistent.Model")

    def test_max_depth_limits_search(self):
        # With max_depth=1, Transaction (3 hops) should return None
        path = find_tenant_path(Transaction, Brokerage, max_depth=1)
        self.assertIsNone(path)


class TestValidateFieldPath(TestCase):
    def test_valid_simple_field(self):
        # Should not raise
        validate_field_path(Deal, "title")

    def test_valid_nested_path(self):
        # Should not raise
        validate_field_path(Transaction, "bank_account__deal__brokerage")

    def test_invalid_first_segment_raises(self):
        with self.assertRaises(ImproperlyConfigured) as cm:
            validate_field_path(Deal, "nonexistent_field")
        self.assertIn("nonexistent_field", str(cm.exception))
        self.assertIn("Deal", str(cm.exception))

    def test_invalid_nested_segment_raises(self):
        with self.assertRaises(ImproperlyConfigured) as cm:
            validate_field_path(Transaction, "bank_account__nonexistent")
        self.assertIn("nonexistent", str(cm.exception))

    def test_traverse_through_non_relation_raises(self):
        # `title` is a CharField, can't traverse through it
        with self.assertRaises(ImproperlyConfigured) as cm:
            validate_field_path(Deal, "title__something")
        self.assertIn("not a relation", str(cm.exception))

    def test_did_you_mean_suggestion(self):
        # Typo close to actual field name
        with self.assertRaises(ImproperlyConfigured) as cm:
            validate_field_path(Deal, "brokeragee")  # extra 'e'
        msg = str(cm.exception)
        self.assertIn("Did you mean", msg)
        self.assertIn("brokerage", msg)

    def test_empty_path_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_field_path(Deal, "")

    def test_non_string_path_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            validate_field_path(Deal, None)


class TestResolveTenantModel(TestCase):
    """Edge cases for _resolve_tenant_model."""

    def test_none_returns_none(self):
        from turbodrf.tenancy import _resolve_tenant_model

        self.assertIsNone(_resolve_tenant_model(None))

    def test_model_class_returned_directly(self):
        from turbodrf.tenancy import _resolve_tenant_model

        self.assertIs(_resolve_tenant_model(Brokerage), Brokerage)

    def test_invalid_app_label_raises(self):
        from turbodrf.tenancy import _resolve_tenant_model

        with self.assertRaises(ImproperlyConfigured):
            _resolve_tenant_model("not_an_app.NotAModel")


class TestFindTenantPathEdgeCases(TestCase):
    """Edge cases for find_tenant_path."""

    def test_none_tenant_model_returns_none(self):
        self.assertIsNone(find_tenant_path(Deal, None))

    def test_self_tenant_returns_none(self):
        """When `model` IS the tenant model, return None — caller decides."""
        self.assertIsNone(find_tenant_path(Brokerage, Brokerage))


class TestResolveTenancyForModel(TestCase):
    """End-to-end resolve_tenancy_for_model coverage of the visibility branch
    and autodetection branch."""

    def test_visibility_form_returns_predicates_no_autodetect(self):
        from turbodrf.predicates import Owner
        from turbodrf.tenancy import resolve_tenancy_for_model

        config = {"visibility": [Owner("assigned_broker")]}
        tf, preds, autodetected = resolve_tenancy_for_model(
            Deal, config, "test_app.Brokerage", autodetect=True
        )
        self.assertIsNone(tf)
        self.assertEqual(len(preds), 1)
        self.assertFalse(autodetected)

    def test_visibility_form_with_tenant_extracted(self):
        from turbodrf.predicates import Owner, Tenant
        from turbodrf.tenancy import resolve_tenancy_for_model
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            config = {"visibility": [Tenant("brokerage"), Owner("assigned_broker")]}
            tf, preds, autodetected = resolve_tenancy_for_model(
                Deal, config, "test_app.Brokerage", autodetect=False
            )
        self.assertEqual(tf, "brokerage")
        self.assertEqual(len(preds), 1)  # Tenant extracted
        self.assertFalse(autodetected)

    def test_autodetect_finds_path(self):
        from turbodrf.tenancy import resolve_tenancy_for_model

        # BankAccount has tenant_field='deal__brokerage' explicitly. Without
        # that, autodetect should find the path.
        config = {"fields": ["name", "deal"]}
        tf, preds, autodetected = resolve_tenancy_for_model(
            BankAccount, config, "test_app.Brokerage", autodetect=True
        )
        self.assertEqual(tf, "deal__brokerage")
        self.assertTrue(autodetected)

    def test_autodetect_skipped_when_no_tenant_model(self):
        from turbodrf.tenancy import resolve_tenancy_for_model

        config = {"fields": ["name", "deal"]}
        tf, preds, autodetected = resolve_tenancy_for_model(
            BankAccount, config, None, autodetect=True
        )
        self.assertIsNone(tf)
        self.assertFalse(autodetected)

    def test_validates_predicate_paths_at_resolution(self):
        """resolve_tenancy_for_model walks predicate paths and rejects bad refs."""
        from turbodrf.predicates import Owner
        from turbodrf.tenancy import resolve_tenancy_for_model

        config = {"visibility": [Owner("nonexistent_field")]}
        with self.assertRaises(ImproperlyConfigured):
            resolve_tenancy_for_model(Deal, config, None, autodetect=False)


class TestPredicatePathValidation(TestCase):
    """Cover Members / Group / Either path validation branches."""

    def test_members_path_validated(self):
        from turbodrf.predicates import Members
        from turbodrf.tenancy import _validate_predicate_paths

        # invalid path raises
        with self.assertRaises(ImproperlyConfigured):
            _validate_predicate_paths(Deal, Members("nonexistent"))

    def test_group_path_validated(self):
        from turbodrf.predicates import Group
        from turbodrf.tenancy import _validate_predicate_paths

        with self.assertRaises(ImproperlyConfigured):
            _validate_predicate_paths(Deal, Group("nonexistent"))

    def test_either_walks_into_children(self):
        from turbodrf.predicates import Either, Owner
        from turbodrf.tenancy import _validate_predicate_paths

        with self.assertRaises(ImproperlyConfigured):
            _validate_predicate_paths(
                Deal, Either(Owner("title"), Owner("nonexistent"))
            )

    def test_custom_skipped(self):
        """Custom predicates have arbitrary q_funcs — path validation skips."""
        from turbodrf.predicates import Custom
        from turbodrf.tenancy import _validate_predicate_paths
        from django.db.models import Q

        # Should not raise
        _validate_predicate_paths(Deal, Custom(lambda r, u: Q()))
