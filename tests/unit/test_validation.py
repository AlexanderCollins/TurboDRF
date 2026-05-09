"""Tests for the validation helpers — startup gates and runtime scope Qs."""

from unittest.mock import Mock

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import TestCase, override_settings

from tests.test_app.models import (
    CompiledArticle,
    RelatedModel,
    SampleModel,
)
from turbodrf.predicates import (
    Custom,
    Either,
    Owner,
    register_predicates,
    register_tenant_field,
    validate_permission_strings,
    validate_predicate_write_safety,
)
from turbodrf.validation import (
    build_traversal_scope_q,
    path_traverses_predicate_target,
    validate_searchable_fields_safety,
)


class _RegistrySnapshot:
    def __enter__(self):
        from turbodrf.predicates import _model_predicates, _model_tenant_fields

        self._saved_p = dict(_model_predicates)
        self._saved_t = dict(_model_tenant_fields)
        return self

    def __exit__(self, exc_type, exc, tb):
        from turbodrf.predicates import _model_predicates, _model_tenant_fields

        _model_predicates.clear()
        _model_predicates.update(self._saved_p)
        _model_tenant_fields.clear()
        _model_tenant_fields.update(self._saved_t)


# ---------------------------------------------------------------------------
# validate_searchable_fields_safety
# ---------------------------------------------------------------------------


class ValidateSearchableFieldsSafetyTests(TestCase):
    def test_no_searchable_fields_attr_is_noop(self):
        class NoSearch:
            pass

        validate_searchable_fields_safety(NoSearch)

    def test_empty_searchable_fields_is_noop(self):
        class EmptySearch:
            searchable_fields = []

        validate_searchable_fields_safety(EmptySearch)

    def test_flat_paths_pass(self):
        """SampleModel has flat searchable_fields — no __-traversal."""
        with _RegistrySnapshot():
            validate_searchable_fields_safety(SampleModel)

    def test_unresolvable_path_silently_skipped(self):
        """Unresolvable __-path is dropped at request time; gate skips it."""

        original = getattr(CompiledArticle, "searchable_fields", None)
        try:
            CompiledArticle.searchable_fields = ["does_not_exist__name"]
            with _RegistrySnapshot():
                validate_searchable_fields_safety(CompiledArticle)
        finally:
            if original is None:
                if hasattr(CompiledArticle, "searchable_fields"):
                    delattr(CompiledArticle, "searchable_fields")
            else:
                CompiledArticle.searchable_fields = original

    def test_traversal_to_safe_target_passes(self):
        """CompiledArticle.author -> RelatedModel (no predicates) — safe."""
        original = getattr(CompiledArticle, "searchable_fields", None)
        try:
            CompiledArticle.searchable_fields = ["author__name"]
            with _RegistrySnapshot():
                validate_searchable_fields_safety(CompiledArticle)
        finally:
            if original is None:
                if hasattr(CompiledArticle, "searchable_fields"):
                    delattr(CompiledArticle, "searchable_fields")
            else:
                CompiledArticle.searchable_fields = original

    def test_traversal_to_predicate_bearing_target_raises(self):
        """RelatedModel with predicates → SearchFilter would join unscoped."""
        original = getattr(CompiledArticle, "searchable_fields", None)
        try:
            CompiledArticle.searchable_fields = ["author__name"]
            with _RegistrySnapshot():
                register_predicates(
                    RelatedModel, [Custom(q_func=lambda r, ur: Q(pk=1))]
                )
                with self.assertRaises(ImproperlyConfigured) as cm:
                    validate_searchable_fields_safety(CompiledArticle)
                self.assertIn("author__name", str(cm.exception))
                self.assertIn("RelatedModel", str(cm.exception))
                self.assertIn(
                    "docs/security.md#search-field-target-bypass",
                    str(cm.exception),
                )
        finally:
            if original is None:
                if hasattr(CompiledArticle, "searchable_fields"):
                    delattr(CompiledArticle, "searchable_fields")
            else:
                CompiledArticle.searchable_fields = original

    def test_kill_switch_bypasses(self):
        original = getattr(CompiledArticle, "searchable_fields", None)
        try:
            CompiledArticle.searchable_fields = ["author__name"]
            with _RegistrySnapshot():
                register_predicates(
                    RelatedModel, [Custom(q_func=lambda r, ur: Q(pk=1))]
                )
                with override_settings(TURBODRF_ALLOW_UNSAFE_SEARCH_FIELDS=True):
                    validate_searchable_fields_safety(CompiledArticle)
        finally:
            if original is None:
                if hasattr(CompiledArticle, "searchable_fields"):
                    delattr(CompiledArticle, "searchable_fields")
            else:
                CompiledArticle.searchable_fields = original


# ---------------------------------------------------------------------------
# path_traverses_predicate_target
# ---------------------------------------------------------------------------


class PathTraversesPredicateTargetTests(TestCase):
    def test_flat_path_returns_false(self):
        self.assertFalse(path_traverses_predicate_target(CompiledArticle, "title"))

    def test_unresolvable_path_returns_false(self):
        self.assertFalse(
            path_traverses_predicate_target(CompiledArticle, "no_such__field")
        )

    def test_safe_traversal_returns_false(self):
        with _RegistrySnapshot():
            self.assertFalse(
                path_traverses_predicate_target(CompiledArticle, "author__name")
            )

    def test_predicate_on_target_returns_true(self):
        with _RegistrySnapshot():
            register_predicates(RelatedModel, [Custom(q_func=lambda r, ur: Q(pk=1))])
            self.assertTrue(
                path_traverses_predicate_target(CompiledArticle, "author__name")
            )

    def test_tenant_drift_returns_true(self):
        """Shared parent + tenanted target = tenant drift."""
        with _RegistrySnapshot():
            register_tenant_field(RelatedModel, "name")
            self.assertTrue(
                path_traverses_predicate_target(CompiledArticle, "author__name")
            )


# ---------------------------------------------------------------------------
# build_traversal_scope_q
# ---------------------------------------------------------------------------


class BuildTraversalScopeQTests(TestCase):
    def test_flat_path_returns_empty_q(self):
        q = build_traversal_scope_q(CompiledArticle, "title", request=None)
        self.assertEqual(q, Q())

    def test_unresolvable_path_returns_empty_q(self):
        q = build_traversal_scope_q(CompiledArticle, "no_such__field", request=None)
        self.assertEqual(q, Q())

    def test_safe_traversal_returns_empty_q(self):
        """Path through a target with no predicates / tenant_field is no-op."""
        with _RegistrySnapshot():
            q = build_traversal_scope_q(CompiledArticle, "author__name", request=None)
            self.assertEqual(q, Q())

    def test_predicate_target_emits_subquery(self):
        from django.contrib.auth import get_user_model

        with _RegistrySnapshot():
            register_predicates(RelatedModel, [Custom(q_func=lambda r, ur: Q(pk=1))])
            User = get_user_model()
            user = User(username="probe")
            request = Mock()
            request.user = user
            q = build_traversal_scope_q(
                CompiledArticle, "author__name", request=request
            )
            # Should be a non-empty Q.
            self.assertNotEqual(q, Q())
            # Stringified Q should mention the prefix.
            self.assertIn("author__in", str(q))

    def test_no_request_with_tenant_field_fails_closed(self):
        """No resolvable request → empty queryset on the target."""
        with _RegistrySnapshot():
            register_tenant_field(RelatedModel, "name")
            q = build_traversal_scope_q(CompiledArticle, "author__name", request=None)
            self.assertNotEqual(q, Q())


# ---------------------------------------------------------------------------
# validate_predicate_write_safety — Custom-without-write_validator gate
# ---------------------------------------------------------------------------


class ValidatePredicateWriteSafetyTests(TestCase):
    def test_no_predicates_is_noop(self):
        with _RegistrySnapshot():
            validate_predicate_write_safety(SampleModel)

    def test_owner_alone_is_safe(self):
        with _RegistrySnapshot():
            register_predicates(SampleModel, [Owner("title")])
            validate_predicate_write_safety(SampleModel)

    def test_custom_with_write_validator_is_safe(self):
        with _RegistrySnapshot():
            register_predicates(
                SampleModel,
                [Custom(q_func=lambda r, ur: Q(pk=1), write_validator=lambda *a: [])],
            )
            validate_predicate_write_safety(SampleModel)

    def test_custom_without_write_validator_raises(self):
        with _RegistrySnapshot():
            register_predicates(SampleModel, [Custom(q_func=lambda r, ur: Q(pk=1))])
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_predicate_write_safety(SampleModel)
            msg = str(cm.exception)
            self.assertIn("Custom", msg)
            self.assertIn("write_validator", msg)
            self.assertIn("Either(Owner, Custom)", msg)

    def test_either_owner_custom_without_write_validator_raises(self):
        """The Either(Owner, Custom) pattern for "owner OR external grant"
        — Custom defaulting to no-op validate_write silently bypasses
        Owner's checks under Either."""
        with _RegistrySnapshot():
            register_predicates(
                SampleModel,
                [Either(Owner("title"), Custom(q_func=lambda r, ur: Q(pk=1)))],
            )
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_predicate_write_safety(SampleModel)
            self.assertIn("Either(...)", str(cm.exception))

    def test_either_with_safe_custom_passes(self):
        with _RegistrySnapshot():
            register_predicates(
                SampleModel,
                [
                    Either(
                        Owner("title"),
                        Custom(
                            q_func=lambda r, ur: Q(pk=1),
                            write_validator=lambda *a: ["read-only"],
                        ),
                    ),
                ],
            )
            validate_predicate_write_safety(SampleModel)

    def test_kill_switch_bypasses(self):
        with _RegistrySnapshot():
            register_predicates(SampleModel, [Custom(q_func=lambda r, ur: Q(pk=1))])
            with override_settings(TURBODRF_ALLOW_UNSAFE_CUSTOM_WRITE=True):
                validate_predicate_write_safety(SampleModel)


# ---------------------------------------------------------------------------
# validate_permission_strings — TURBODRF_ROLES typo gate
# ---------------------------------------------------------------------------


class ValidatePermissionStringsTests(TestCase):
    def test_empty_roles_is_noop(self):
        with override_settings(TURBODRF_ROLES={}):
            validate_permission_strings()

    def test_valid_model_action_passes(self):
        with override_settings(TURBODRF_ROLES={"r": ["test_app.samplemodel.read"]}):
            validate_permission_strings()

    def test_valid_field_action_passes(self):
        with override_settings(
            TURBODRF_ROLES={"r": ["test_app.samplemodel.title.read"]}
        ):
            validate_permission_strings()

    def test_unknown_model_raises(self):
        with override_settings(TURBODRF_ROLES={"r": ["test_app.notamodel.read"]}):
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_permission_strings()
            self.assertIn("notamodel", str(cm.exception))
            self.assertIn("not registered", str(cm.exception))

    def test_unknown_field_raises_with_close_match(self):
        with override_settings(
            TURBODRF_ROLES={"r": ["test_app.samplemodel.titel.read"]}
        ):
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_permission_strings()
            self.assertIn("titel", str(cm.exception))
            self.assertIn("close matches", str(cm.exception))

    def test_invalid_model_action_raises(self):
        with override_settings(TURBODRF_ROLES={"r": ["test_app.samplemodel.peruse"]}):
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_permission_strings()
            self.assertIn("peruse", str(cm.exception))

    def test_invalid_field_action_raises(self):
        # `delete` is not a valid field-level action
        with override_settings(
            TURBODRF_ROLES={"r": ["test_app.samplemodel.title.delete"]}
        ):
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_permission_strings()
            self.assertIn("title", str(cm.exception))

    def test_wrong_segment_count_raises(self):
        with override_settings(TURBODRF_ROLES={"r": ["test_app.samplemodel"]}):
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_permission_strings()
            self.assertIn("3 or 4 dot-separated", str(cm.exception))

    def test_kill_switch_skips_unknown_model(self):
        with override_settings(
            TURBODRF_ROLES={"r": ["plugin_app.dynamic.read"]},
            TURBODRF_ALLOW_UNKNOWN_PERMISSIONS=True,
        ):
            validate_permission_strings()

    def test_kill_switch_does_not_skip_typo_in_field(self):
        """Kill switch is for unknown models only, not for typo'd
        fields on real models."""
        with override_settings(
            TURBODRF_ROLES={"r": ["test_app.samplemodel.titel.read"]},
            TURBODRF_ALLOW_UNKNOWN_PERMISSIONS=True,
        ):
            with self.assertRaises(ImproperlyConfigured):
                validate_permission_strings()

    def test_non_list_perms_raises(self):
        with override_settings(TURBODRF_ROLES={"r": "test_app.samplemodel.read"}):
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_permission_strings()
            self.assertIn("must be a list", str(cm.exception))

    def test_non_string_perm_raises(self):
        with override_settings(TURBODRF_ROLES={"r": [42]}):
            with self.assertRaises(ImproperlyConfigured) as cm:
                validate_permission_strings()
            self.assertIn("must be a string", str(cm.exception))
