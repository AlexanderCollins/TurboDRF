"""
Unit tests for the predicate-based row-level access control system.

Covers each predicate class in isolation:
- Tenant: read filter, auto_fill, validate_write
- Owner: bypass behavior, multi-field OR, write rejection
- Members / Group: M2M-based filters
- Conditional: when-clause inversion, role override
- Either: OR composition
- Custom: callable hook
- Sugar parser: turbodrf() config → predicate list
"""

from unittest.mock import Mock

from django.db.models import Q
from django.test import TestCase, override_settings

from turbodrf.predicates import (
    Conditional,
    Custom,
    Either,
    Group,
    Members,
    Owner,
    Tenant,
    has_tenancy_declaration,
    parse_config,
)


def _make_request(user, authenticated=True):
    request = Mock()
    request.user = user
    request.user.is_authenticated = authenticated
    return request


def _make_user(user_id=1, brokerage=None, roles=()):
    """Create a Mock user. Configure `roles` (list) so backends.get_user_roles
    works — Mock auto-creates `.roles` as another Mock otherwise, breaking
    `list(user.roles)`. We use a Mock with spec to lock attributes down."""
    user = Mock(spec=["pk", "id", "is_authenticated", "brokerage", "roles"])
    user.pk = user_id
    user.id = user_id
    user.is_authenticated = True
    user.brokerage = brokerage
    user.roles = list(roles)
    return user


class TestTenantPredicate(TestCase):
    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_q_returns_user_tenant_filter(self):
        user = _make_user(brokerage=42)
        req = _make_request(user)
        pred = Tenant("brokerage")
        q = pred.q(req, user_roles=set())
        self.assertEqual(q, Q(brokerage=42))

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_q_returns_no_match_when_no_tenant(self):
        user = _make_user(brokerage=None)
        req = _make_request(user)
        pred = Tenant("brokerage")
        q = pred.q(req, user_roles=set())
        self.assertEqual(q, Q(pk__in=[]))

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_q_unauthenticated_returns_no_match(self):
        user = Mock()
        user.is_authenticated = False
        req = _make_request(user, authenticated=False)
        pred = Tenant("brokerage")
        q = pred.q(req, user_roles=set())
        self.assertEqual(q, Q(pk__in=[]))

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_auto_fill_sets_tenant(self):
        user = _make_user(brokerage=42)
        req = _make_request(user)
        pred = Tenant("brokerage")
        result = pred.auto_fill({"title": "Hello"}, req)
        self.assertEqual(result, {"title": "Hello", "brokerage": 42})

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_auto_fill_overwrites_user_value(self):
        # Defense in depth: even if client provides wrong tenant, auto_fill
        # overwrites with the correct one.
        user = _make_user(brokerage=42)
        req = _make_request(user)
        pred = Tenant("brokerage")
        result = pred.auto_fill({"title": "X", "brokerage": 99}, req)
        self.assertEqual(result["brokerage"], 42)

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_auto_fill_skips_nested_paths(self):
        user = _make_user(brokerage=42)
        req = _make_request(user)
        pred = Tenant("deal__brokerage")
        result = pred.auto_fill({"title": "X"}, req)
        self.assertNotIn("deal__brokerage", result)
        self.assertNotIn("brokerage", result)

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_validate_write_rejects_wrong_tenant(self):
        user = _make_user(brokerage=42)
        req = _make_request(user)
        pred = Tenant("brokerage")
        errors = pred.validate_write({"brokerage": 99}, None, req)
        self.assertEqual(len(errors), 1)
        self.assertIn("different tenant", errors[0])

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_validate_write_accepts_correct_tenant(self):
        user = _make_user(brokerage=42)
        req = _make_request(user)
        pred = Tenant("brokerage")
        errors = pred.validate_write({"brokerage": 42}, None, req)
        self.assertEqual(errors, [])

    @override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
    def test_validate_write_skips_nested(self):
        # We can't validate writes on chained paths
        user = _make_user(brokerage=42)
        req = _make_request(user)
        pred = Tenant("deal__brokerage")
        errors = pred.validate_write({"deal__brokerage": 99}, None, req)
        self.assertEqual(errors, [])


class TestOwnerPredicate(TestCase):
    def test_q_filters_by_user(self):
        user = _make_user(user_id=7)
        req = _make_request(user)
        pred = Owner("assigned_to")
        q = pred.q(req, user_roles={"underwriter"})
        self.assertEqual(q, Q(assigned_to=user))

    def test_q_bypassed_when_user_has_bypass_role(self):
        user = _make_user(user_id=7)
        req = _make_request(user)
        pred = Owner("assigned_to", bypass=["manager", "admin"])
        q = pred.q(req, user_roles={"manager"})
        self.assertEqual(q, Q())

    def test_q_not_bypassed_with_other_roles(self):
        user = _make_user(user_id=7)
        req = _make_request(user)
        pred = Owner("assigned_to", bypass=["manager"])
        q = pred.q(req, user_roles={"underwriter"})
        self.assertEqual(q, Q(assigned_to=user))

    def test_multi_owner_field_or(self):
        user = _make_user(user_id=7)
        req = _make_request(user)
        pred = Owner(["author", "editor", "reviewer"])
        q = pred.q(req, user_roles=set())
        expected = Q(author=user) | Q(editor=user) | Q(reviewer=user)
        self.assertEqual(q, expected)

    def test_auto_fill_sets_single_owner(self):
        user = _make_user(user_id=7)
        req = _make_request(user)
        pred = Owner("assigned_to")
        result = pred.auto_fill({"title": "X"}, req)
        self.assertEqual(result["assigned_to"], user)

    def test_auto_fill_skips_multi_owner(self):
        # Ambiguous which one to fill
        user = _make_user(user_id=7)
        req = _make_request(user)
        pred = Owner(["author", "editor"])
        result = pred.auto_fill({"title": "X"}, req)
        self.assertNotIn("author", result)
        self.assertNotIn("editor", result)

    def test_auto_fill_does_not_overwrite_explicit(self):
        # User provided a value (will be validated separately)
        user = _make_user(user_id=7)
        req = _make_request(user)
        pred = Owner("assigned_to")
        explicit = Mock()
        result = pred.auto_fill({"assigned_to": explicit}, req)
        self.assertIs(result["assigned_to"], explicit)

    def test_validate_write_rejects_assigning_to_another_user(self):
        user = _make_user(user_id=7, roles=["underwriter"])
        req = _make_request(user)
        other = Mock(pk=99)
        pred = Owner("assigned_to", bypass=["admin"])
        errors = pred.validate_write({"assigned_to": other}, None, req)
        self.assertEqual(len(errors), 1)
        self.assertIn("different user", errors[0])

    def test_validate_write_accepts_self_assignment(self):
        user = _make_user(user_id=7, roles=["underwriter"])
        req = _make_request(user)
        pred = Owner("assigned_to")
        errors = pred.validate_write({"assigned_to": user}, None, req)
        self.assertEqual(errors, [])

    def test_validate_write_bypass_role_can_assign_anyone(self):
        user = _make_user(user_id=7, roles=["manager"])
        req = _make_request(user)
        other = Mock(pk=99)
        pred = Owner("assigned_to", bypass=["manager"])
        errors = pred.validate_write({"assigned_to": other}, None, req)
        self.assertEqual(errors, [])


class TestMembersPredicate(TestCase):
    def test_q_filters_by_user_in_m2m(self):
        user = _make_user()
        req = _make_request(user)
        pred = Members("collaborators")
        q = pred.q(req, user_roles=set())
        self.assertEqual(q, Q(collaborators=user))


class TestGroupPredicate(TestCase):
    def test_q_default_user_via(self):
        user = _make_user()
        req = _make_request(user)
        pred = Group("team")
        q = pred.q(req, user_roles=set())
        self.assertEqual(q, Q(team__members=user))

    def test_q_custom_user_via(self):
        user = _make_user()
        req = _make_request(user)
        pred = Group("team", user_via="staff")
        q = pred.q(req, user_roles=set())
        self.assertEqual(q, Q(team__staff=user))


class TestConditionalPredicate(TestCase):
    def test_q_excludes_matching_when_user_lacks_role(self):
        req = _make_request(_make_user())
        pred = Conditional(when=Q(is_staff_loan=True), require_roles=["special_admin"])
        q = pred.q(req, user_roles={"underwriter"})
        self.assertEqual(q, ~Q(is_staff_loan=True))

    def test_q_unrestricted_when_user_has_role(self):
        req = _make_request(_make_user())
        pred = Conditional(when=Q(is_staff_loan=True), require_roles=["special_admin"])
        q = pred.q(req, user_roles={"special_admin"})
        self.assertEqual(q, Q())


class TestEitherPredicate(TestCase):
    def test_q_or_of_children(self):
        user = _make_user()
        req = _make_request(user)
        pred = Either(Owner("author"), Members("collaborators"))
        q = pred.q(req, user_roles=set())
        expected = Q(author=user) | Q(collaborators=user)
        self.assertEqual(q, expected)

    def test_validate_write_passes_if_any_child_passes(self):
        user = _make_user(roles=["manager"])
        req = _make_request(user)
        # First Owner has no bypass and would reject; second has manager bypass
        pred = Either(
            Owner("a"),
            Owner("b", bypass=["manager"]),
        )
        # Setting b to anyone is fine since user has bypass on second Owner
        other = Mock(pk=99)
        errors = pred.validate_write({"b": other}, None, req)
        self.assertEqual(errors, [])


class TestCustomPredicate(TestCase):
    def test_q_calls_callable(self):
        user = _make_user()
        req = _make_request(user)

        def fn(request, roles):
            return Q(visibility="public")

        pred = Custom(fn)
        q = pred.q(req, user_roles=set())
        self.assertEqual(q, Q(visibility="public"))

    def test_validate_write_calls_validator(self):
        called = []

        def validator(data, instance, request):
            called.append(data)
            return ["nope"]

        pred = Custom(lambda r, u: Q(), write_validator=validator)
        errors = pred.validate_write({"x": 1}, None, _make_request(_make_user()))
        self.assertEqual(errors, ["nope"])
        self.assertEqual(called, [{"x": 1}])


class TestSugarParser(TestCase):
    """parse_config returns (tenant_field, predicates) — Tenant is a setting,
    not a predicate, in the new two-layer design."""

    def test_shared_returns_empty(self):
        tf, preds = parse_config({"tenancy": "shared"})
        self.assertIsNone(tf)
        self.assertEqual(preds, [])

    def test_no_keys_returns_empty(self):
        tf, preds = parse_config({})
        self.assertIsNone(tf)
        self.assertEqual(preds, [])

    def test_tenant_field_only(self):
        tf, preds = parse_config({"tenant_field": "brokerage"})
        self.assertEqual(tf, "brokerage")
        self.assertEqual(preds, [])  # no within-tenant predicates

    def test_owner_field_only_str(self):
        tf, preds = parse_config({"owner_field": "assigned_to"})
        self.assertIsNone(tf)
        self.assertEqual(len(preds), 1)
        self.assertIsInstance(preds[0], Owner)
        self.assertEqual(preds[0].fields, ["assigned_to"])

    def test_owner_field_list_single_becomes_owner(self):
        tf, preds = parse_config({"owner_field": ["assigned_to"]})
        self.assertIsNone(tf)
        self.assertEqual(len(preds), 1)
        self.assertIsInstance(preds[0], Owner)

    def test_owner_field_list_multiple_becomes_either(self):
        tf, preds = parse_config({"owner_field": ["author", "editor", "reviewer"]})
        self.assertEqual(len(preds), 1)
        self.assertIsInstance(preds[0], Either)
        self.assertEqual(len(preds[0].predicates), 3)

    def test_full_sugar_form(self):
        tf, preds = parse_config(
            {
                "tenant_field": "brokerage",
                "owner_field": "assigned_to",
                "bypass_owner_roles": ["admin", "manager"],
            }
        )
        self.assertEqual(tf, "brokerage")
        self.assertEqual(len(preds), 1)
        self.assertIsInstance(preds[0], Owner)
        self.assertEqual(preds[0].bypass, {"admin", "manager"})

    def test_visibility_power_form_extracts_tenant_to_setting(self):
        """Tenant() inside visibility is extracted to tenant_field setting
        with deprecation warning. Other predicates stay in the list."""
        import warnings

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            tf, preds = parse_config(
                {"visibility": [Tenant("brokerage"), Owner("assigned_to")]}
            )
        self.assertEqual(tf, "brokerage")
        self.assertEqual(len(preds), 1)
        self.assertIsInstance(preds[0], Owner)
        self.assertTrue(
            any(issubclass(w.category, DeprecationWarning) for w in captured),
            "Should warn when Tenant() used inside visibility",
        )

    def test_visibility_with_owner_only(self):
        tf, preds = parse_config({"visibility": [Owner("assigned_to")]})
        self.assertIsNone(tf)
        self.assertEqual(len(preds), 1)

    def test_tenant_inside_either_rejected(self):
        """Tenant inside Either is a config error.

        A developer writing `Either(Owner_with_bypass, Tenant)` for a
        'manager sees all in tenant OR own deals' use case would silently
        get cross-tenant access because `Q() OR anything == Q()`. The
        framework rejects the configuration at startup so this can't
        happen."""
        with self.assertRaises(Exception) as cm:
            parse_config(
                {
                    "visibility": [
                        Either(
                            Owner("assigned_to", bypass=["manager"]),
                            Tenant("brokerage"),
                        ),
                    ]
                }
            )
        self.assertIn("Tenant", str(cm.exception))
        self.assertIn("Either", str(cm.exception))

    def test_tenant_inside_nested_either_rejected(self):
        """Walk recursively into nested Either."""
        with self.assertRaises(Exception):
            parse_config(
                {
                    "visibility": [
                        Either(
                            Owner("a"),
                            Either(Tenant("brokerage"), Owner("b")),
                        ),
                    ]
                }
            )

    def test_visibility_validates_predicate_instances(self):
        with self.assertRaises(Exception):
            parse_config({"visibility": ["not a predicate"]})

    def test_invalid_owner_field_type(self):
        with self.assertRaises(Exception):
            parse_config({"owner_field": 42})

    def test_has_tenancy_declaration(self):
        self.assertTrue(has_tenancy_declaration({"tenancy": "shared"}))
        self.assertTrue(has_tenancy_declaration({"tenant_field": "x"}))
        self.assertTrue(has_tenancy_declaration({"owner_field": "x"}))
        self.assertTrue(has_tenancy_declaration({"visibility": []}))
        self.assertFalse(has_tenancy_declaration({}))
        self.assertFalse(has_tenancy_declaration({"fields": "__all__"}))

    def test_non_dict_config_raises(self):
        with self.assertRaises(Exception):
            parse_config("not a dict")

    def test_non_string_tenant_field_raises(self):
        with self.assertRaises(Exception):
            parse_config({"tenant_field": 42})

    def test_visibility_must_be_list(self):
        with self.assertRaises(Exception):
            parse_config({"visibility": "not a list"})


class TestPredicateDefensiveBranches(TestCase):
    """Cover defensive paths in predicates that surface on bad inputs."""

    def test_owner_empty_list_raises(self):
        with self.assertRaises(Exception):
            Owner([])

    def test_either_no_predicates_raises(self):
        with self.assertRaises(Exception):
            Either()

    def test_either_non_predicate_child_raises(self):
        with self.assertRaises(Exception):
            Either(Owner("a"), "not a predicate")

    def test_custom_non_callable_raises(self):
        with self.assertRaises(Exception):
            Custom(q_func=42)

    def test_conditional_non_q_when_raises(self):
        with self.assertRaises(Exception):
            Conditional(when="not a Q", require_roles=["admin"])

    def test_either_validate_write_aggregates_errors(self):
        """When ALL children's validate_write produce errors, Either returns
        the aggregated list."""
        from unittest.mock import Mock

        from turbodrf.predicates import Custom

        bad1 = Custom(
            q_func=lambda r, u: __import__("django.db.models").db.models.Q(),
            write_validator=lambda d, i, r: ["err1"],
        )
        bad2 = Custom(
            q_func=lambda r, u: __import__("django.db.models").db.models.Q(),
            write_validator=lambda d, i, r: ["err2"],
        )
        e = Either(bad1, bad2)
        errors = e.validate_write({}, None, Mock())
        self.assertEqual(set(errors), {"err1", "err2"})

    def test_custom_with_auto_filler(self):
        from unittest.mock import Mock

        from django.db.models import Q

        c = Custom(
            q_func=lambda r, u: Q(),
            auto_filler=lambda data, request: {**data, "added": "yes"},
        )
        result = c.auto_fill({"x": 1}, Mock())
        self.assertEqual(result, {"x": 1, "added": "yes"})

    def test_custom_without_auto_filler_returns_unchanged(self):
        from django.db.models import Q

        c = Custom(q_func=lambda r, u: Q())
        data = {"x": 1}
        self.assertEqual(c.auto_fill(data, None), data)

    def test_owner_no_fields_at_all_after_init(self):
        """Edge: Owner('x') with bypass actually checks bypass first."""
        from unittest.mock import Mock

        from django.db.models import Q

        # User with bypass — returns Q() unconditionally
        o = Owner("x", bypass=["admin"])
        q = o.q(Mock(), {"admin"})
        self.assertEqual(q, Q())

    def test_tenant_validate_write_no_user(self):
        """Tenant.validate_write returns auth error when no user."""
        from turbodrf.predicates import Tenant

        t = Tenant("brokerage")
        errors = t.validate_write({"brokerage": 1}, None, None)
        self.assertEqual(len(errors), 1)
        self.assertIn("no authenticated user", errors[0])

    def test_clear_predicates_resets_registries(self):
        from turbodrf.predicates import (
            _model_predicates,
            _model_tenant_fields,
            clear_predicates,
            get_predicates,
            get_tenant_field,
            register_predicates,
            register_tenant_field,
        )

        class Fake:
            pass

        # Snapshot registry so we can restore — clear_predicates() wipes
        # the global module state, which would break every subsequent
        # test on this xdist worker (FK injection guards stop firing,
        # tenant filters stop applying).
        saved_p = dict(_model_predicates)
        saved_t = dict(_model_tenant_fields)
        try:
            register_predicates(Fake, [Owner("x")])
            register_tenant_field(Fake, "brokerage")
            self.assertTrue(get_predicates(Fake))
            self.assertEqual(get_tenant_field(Fake), "brokerage")

            clear_predicates()
            self.assertEqual(get_predicates(Fake), [])
            self.assertIsNone(get_tenant_field(Fake))
        finally:
            _model_predicates.clear()
            _model_predicates.update(saved_p)
            _model_tenant_fields.clear()
            _model_tenant_fields.update(saved_t)
