"""Tests for the validation helpers — startup gates and runtime scope Qs."""

from unittest.mock import Mock

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import TestCase, override_settings

from tests.test_app.models import (
    CompiledArticle,
    RelatedModel,
)
from turbodrf.predicates import (
    Custom,
    register_predicates,
    register_tenant_field,
    validate_permission_strings,
)
from turbodrf.validation import (
    build_traversal_scope_q,
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
            register_predicates(
                RelatedModel,
                [
                    Custom(
                        q_func=lambda r, ur: Q(pk=1), write_validator=lambda d, i, r: []
                    )
                ],
            )
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
