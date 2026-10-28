"""Security review (0.6.x): predicate semantics, tenancy resolution and the
router's startup validation, asserted without HTTP."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import FieldError, ImproperlyConfigured, ValidationError
from django.db import models
from django.db.models import Q
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import isolate_apps

from tests.fixtures import router_apps
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    Holding,
    RbacBoard,
    RbacComment,
    SampleModel,
    ScopeFolder,
    ScopeItem,
    ScopeLoose,
    ScopeMultiOwner,
    Transaction,
)
from turbodrf.mixins import TurboDRFMixin
from turbodrf.predicates import (
    Conditional,
    Custom,
    Either,
    Group,
    Members,
    Owner,
    get_user_tenant,
    has_tenancy_declaration,
)
from turbodrf.tenancy import (
    AmbiguousTenantPath,
    _validate_predicate_paths,
    find_tenant_path,
    resolve_tenancy_for_model,
)

User = get_user_model()

TENANT = dict(TURBODRF_TENANT_MODEL="test_app.Brokerage", TURBODRF_REQUIRE_TENANCY=True)


@contextmanager
def _preserve_registries():
    import turbodrf.router as router_mod
    from turbodrf import predicates

    preds = dict(predicates._model_predicates)
    tenants = dict(predicates._model_tenant_fields)
    flag = router_mod._bypass_roles_validated
    try:
        yield
    finally:
        predicates._model_predicates.clear()
        predicates._model_predicates.update(preds)
        predicates._model_tenant_fields.clear()
        predicates._model_tenant_fields.update(tenants)
        router_mod._bypass_roles_validated = flag


def _router(*models_and_configs):
    """Build a router that sees only the given (model, config) pairs."""
    from turbodrf.router import TurboDRFRouter

    with _preserve_registries():
        patches = [
            mock.patch(
                "turbodrf.router.apps",
                router_apps([m for m, _ in models_and_configs]),
            )
        ] + [
            mock.patch.object(m, "turbodrf", classmethod(lambda cls, c=c: dict(c)))
            for m, c in models_and_configs
        ]
        for p in patches:
            p.start()
        try:
            return TurboDRFRouter()
        finally:
            for p in patches:
                p.stop()


def _user(pk=7, roles=()):
    user = mock.Mock(spec=["pk", "id", "is_authenticated", "roles"])
    user.pk = user.id = pk
    user.is_authenticated = True
    user.roles = list(roles)
    return user


def _request(user):
    return SimpleNamespace(user=user)


# ---------------------------------------------------------------------------
# Finding 1: Either runs every child's write check and auto-fill.
# ---------------------------------------------------------------------------
class TestEitherSemantics(SimpleTestCase):
    def test_any_objection_blocks(self):
        req = _request(_user())
        for case, either in enumerate(
            (
                Either(Owner("owner"), Members("watchers")),
                Either(Owner("owner"), Custom(lambda r, u: Q(), lambda d, i, r: [])),
                Either(Owner("owner"), Owner("editor")),
            )
        ):
            with self.subTest(case=case):
                errors = either.validate_write({"owner": mock.Mock(pk=99)}, None, req)
                self.assertEqual(errors, ["Cannot set owner to a different user."])

    def test_duplicate_errors_collapse(self):
        either = Either(Owner("owner"), Owner("owner"))
        errors = either.validate_write({"owner": None}, None, _request(_user()))
        self.assertEqual(errors, ["Cannot clear owner."])

    def test_auto_fill_delegates_to_children(self):
        user = _user()
        data = Either(Owner("owner"), Members("watchers")).auto_fill(
            {"title": "t"}, _request(user)
        )
        self.assertIs(data["owner"], user)


# ---------------------------------------------------------------------------
# Findings 2, 3, 4: Owner's write check.
# ---------------------------------------------------------------------------
class TestOwnerWriteCheck(SimpleTestCase):
    def test_update_rejects_changing_another_users_column(self):
        me = _user(pk=2)
        row = ScopeMultiOwner(owner_id=1, editor_id=2)
        errors = Owner(["owner", "editor"]).validate_write(
            {"owner": me}, row, _request(me)
        )
        self.assertEqual(
            errors, ["Cannot change owner: the row is assigned to another user."]
        )

    def test_own_row_can_be_rewritten(self):
        me = _user(pk=2)
        row = ScopeMultiOwner(owner_id=2, editor_id=None)
        errors = Owner(["owner", "editor"]).validate_write(
            {"owner": me, "editor": me}, row, _request(me)
        )
        self.assertEqual(errors, [])

    def test_dunder_path_does_not_disable_direct_columns(self):
        """Owner(['owner', 'deals__assigned_broker']) skipped every check."""
        pred = Owner(["owner", "deals__assigned_broker"])
        errors = pred.validate_write(
            {"owner": mock.Mock(pk=99)}, None, _request(_user())
        )
        self.assertEqual(errors, ["Cannot set owner to a different user."])

    def test_no_authenticated_user_is_an_error(self):
        anon = SimpleNamespace(user=SimpleNamespace(is_authenticated=False))
        for request in (anon, None):
            with self.subTest(request=repr(request)):
                errors = Owner("owner").validate_write(
                    {"owner": mock.Mock(pk=99)}, None, request
                )
                self.assertEqual(errors, ["Cannot set owner: no authenticated user."])


# ---------------------------------------------------------------------------
# Finding 5: visibility without a tenant_field counts only when it always
# restricts.
# ---------------------------------------------------------------------------
class TestTenancyDeclaration(SimpleTestCase):
    def test_lifting_predicates_do_not_count(self):
        lifting = (
            [Owner("o", bypass=["admin"])],
            [Conditional(when=Q(x=1), require_roles=["staff"])],
            [Either(Owner("o", bypass=["admin"]), Members("m"))],
            [Either(Conditional(when=Q(x=1), require_roles=["s"]), Owner("o"))],
        )
        for visibility in lifting:
            with self.subTest(visibility=repr(visibility)):
                self.assertFalse(has_tenancy_declaration({"visibility": visibility}))
                self.assertTrue(
                    has_tenancy_declaration(
                        {"visibility": visibility, "tenant_field": "brokerage"}
                    )
                )

    def test_restricting_predicates_count(self):
        for visibility in (
            [Owner("o")],
            [Members("m")],
            [Group("team")],
            [Either(Owner("o"), Members("m"))],
            [Owner("o", bypass=["admin"]), Members("m")],
            [Custom(lambda r, u: Q(), write_validator=lambda d, i, r: [])],
        ):
            with self.subTest(visibility=repr(visibility)):
                self.assertTrue(has_tenancy_declaration({"visibility": visibility}))


# ---------------------------------------------------------------------------
# Finding 6: a bare string is one role.
# ---------------------------------------------------------------------------
class TestRoleNames(SimpleTestCase):
    def test_bare_string_is_one_role(self):
        self.assertEqual(Owner("o", bypass="admin").bypass, {"admin"})
        cond = Conditional(when=Q(x=1), require_roles="staff")
        self.assertEqual(cond.require_roles, {"staff"})
        # A role named 'a' doesn't lift the restriction.
        self.assertNotEqual(Owner("o", bypass="admin").q(None, {"a"}), Q())

    def test_non_iterable_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            Owner("o", bypass=5)


# ---------------------------------------------------------------------------
# Finding 7: Group's user_via and Owner's columns are checked at startup.
# ---------------------------------------------------------------------------
class TestPredicatePathsReachTheUser(SimpleTestCase):
    def test_group_accessor_name_is_refused(self):
        """AGENTS.md's user_via='user_set' is the accessor; the lookup is
        'user', and 'user_set' raised FieldError on every request."""
        with self.assertRaises(FieldError):
            str(User.objects.filter(groups__user_set=1).query)
        with self.assertRaises(ImproperlyConfigured):
            _validate_predicate_paths(User, Group("groups", user_via="user_set"))
        _validate_predicate_paths(User, Group("groups", user_via="user"))

    def test_group_typo_and_non_user_target(self):
        for user_via in ("ownr", "brokerage"):
            with self.subTest(user_via=user_via):
                with self.assertRaises(ImproperlyConfigured):
                    _validate_predicate_paths(
                        ScopeItem, Group("folder", user_via=user_via)
                    )
        _validate_predicate_paths(ScopeItem, Group("folder", user_via="owner"))

    def test_owner_must_be_a_user_fk(self):
        for field in ("title", "brokerage"):
            with self.subTest(field=field):
                with self.assertRaises(ImproperlyConfigured):
                    resolve_tenancy_for_model(
                        Deal, {"tenant_field": "brokerage", "owner_field": field}, None
                    )
        with self.assertRaises(ImproperlyConfigured):
            _validate_predicate_paths(RbacBoard, Owner("deals"))  # M2M
        _validate_predicate_paths(RbacBoard, Owner(["owner", "deals__assigned_broker"]))


# ---------------------------------------------------------------------------
# Finding 8: get_user_tenant coerces an unsaved instance and a bool to None.
# ---------------------------------------------------------------------------
@override_settings(TURBODRF_TENANT_USER_FIELD="brokerage")
class TestUserTenantCoercion(TestCase):
    def test_coercion(self):
        saved = Brokerage.objects.create(name="A")
        cases = [
            (Brokerage(name="unsaved"), None),
            (True, None),
            (False, None),
            (7, 7),
            (saved, saved),
        ]
        for value, expected in cases:
            with self.subTest(value=repr(value)):
                tenant = get_user_tenant(SimpleNamespace(brokerage=value))
                self.assertIs(tenant, expected)


# ---------------------------------------------------------------------------
# Finding 9: auto-detection reports equal-length ambiguity and follows
# forward FKs only.
# ---------------------------------------------------------------------------
@isolate_apps("tests.test_app")
class TestAutodetectPaths(SimpleTestCase):
    def test_two_fks_to_one_model_are_ambiguous(self):
        class Firm(models.Model):
            class Meta:
                app_label = "test_app"

        class Acct(models.Model):
            firm = models.ForeignKey(Firm, models.CASCADE)

            class Meta:
                app_label = "test_app"

        class Transfer(models.Model):
            source = models.ForeignKey(Acct, models.CASCADE, related_name="+")
            dest = models.ForeignKey(Acct, models.CASCADE, related_name="+")

            class Meta:
                app_label = "test_app"

        with self.assertRaises(AmbiguousTenantPath) as cm:
            find_tenant_path(Transfer, Firm)
        self.assertIn("source__firm", str(cm.exception))
        self.assertIn("dest__firm", str(cm.exception))

    def test_reverse_one_to_one_is_not_followed(self):
        class Firm(models.Model):
            class Meta:
                app_label = "test_app"

        class Person(models.Model):
            class Meta:
                app_label = "test_app"

        class Badge(models.Model):
            person = models.OneToOneField(Person, models.CASCADE)
            firm = models.ForeignKey(Firm, models.CASCADE)

            class Meta:
                app_label = "test_app"

        self.assertIsNone(find_tenant_path(Person, Firm))
        self.assertEqual(find_tenant_path(Badge, Firm), "firm")

    def test_existing_chains_still_found(self):
        self.assertEqual(find_tenant_path(BankAccount, Brokerage), "deal__brokerage")
        self.assertEqual(
            find_tenant_path(Transaction, Brokerage), "bank_account__deal__brokerage"
        )


# ---------------------------------------------------------------------------
# Findings 10, 11: a tenant_field takes forward FK hops to the tenant model.
# ---------------------------------------------------------------------------
class TestTenantFieldPath(SimpleTestCase):
    def test_m2m_reverse_and_column_hops_are_refused(self):
        for model, path in (
            (RbacComment, "linked_deals__brokerage"),  # M2M
            (ScopeFolder, "items__brokerage"),  # reverse FK
            (Deal, "brokerage_id"),  # column, not relation
            (Deal, "title"),  # not a relation
        ):
            with self.subTest(path=path):
                with self.assertRaises(ImproperlyConfigured):
                    resolve_tenancy_for_model(model, {"tenant_field": path}, None)

    def test_path_must_end_at_the_tenant_model(self):
        """Holding.portfolio → Portfolio: Q(portfolio=<brokerage pk>) matched
        another tenant's rows."""
        with self.assertRaises(ImproperlyConfigured) as cm:
            resolve_tenancy_for_model(
                Holding, {"tenant_field": "portfolio"}, "test_app.Brokerage"
            )
        self.assertIn("test_app.Portfolio", str(cm.exception))
        tf, _preds, _auto = resolve_tenancy_for_model(
            Holding, {"tenant_field": "portfolio__brokerage"}, "test_app.Brokerage"
        )
        self.assertEqual(tf, "portfolio__brokerage")

    def test_tenant_model_typo_is_resolved_without_autodetect(self):
        with self.assertRaises(ImproperlyConfigured):
            resolve_tenancy_for_model(
                Deal, {"tenant_field": "brokerage"}, "test_app.Brokerge", False
            )


class TestTenancyGatesAtRouterBuild(SimpleTestCase):
    @override_settings(**TENANT)
    def test_tenant_field_to_another_model_is_refused(self):
        cfg = {"tenant_field": "portfolio", "fields": ["id"], "compiled": False}
        with self.assertRaises(ImproperlyConfigured):
            _router((Holding, cfg))

    @override_settings(TURBODRF_TENANT_USER_FIELD=None)
    def test_tenant_field_needs_tenant_user_field(self):
        with self.assertRaises(ImproperlyConfigured) as cm:
            _router((Deal, Deal.turbodrf()))
        self.assertIn("TURBODRF_TENANT_USER_FIELD", str(cm.exception))

    def test_disabled_model_config_is_validated_too(self):
        cfg = {"enabled": False, "tenant_field": "titel", "fields": ["id"]}
        with self.assertRaises(ImproperlyConfigured):
            _router((ScopeLoose, cfg))


# ---------------------------------------------------------------------------
# Finding 16: two models with one name in different apps.
# ---------------------------------------------------------------------------
@isolate_apps("turbodrf")
class TestSameModelNameInTwoApps(SimpleTestCase):
    def _other_deal(self, config):
        class Deal(TurboDRFMixin, models.Model):
            title = models.CharField(max_length=10)

            class Meta:
                app_label = "turbodrf"

            @classmethod
            def turbodrf(cls):
                return dict(config)

        return Deal

    def test_shared_endpoint_is_refused_with_a_hint(self):
        other = self._other_deal({"fields": ["id", "title"], "compiled": False})
        with self.assertRaises(ImproperlyConfigured) as cm:
            _router((Deal, Deal.turbodrf()), (other, other.turbodrf()))
        self.assertIn("Set a distinct 'endpoint'", str(cm.exception))

    def test_distinct_endpoint_boots(self):
        """Both basenames were 'deal': DRF refused the second at boot."""
        config = {"fields": ["id", "title"], "compiled": False, "endpoint": "o-deals"}
        other = self._other_deal(config)
        router = _router((Deal, Deal.turbodrf()), (other, config))
        basenames = {prefix: basename for prefix, _vs, basename in router.registry}
        self.assertEqual(basenames, {"deals": "deal", "o-deals": "o-deals"})


# ---------------------------------------------------------------------------
# Finding 17: fields and lookup_field are checked at startup.
# ---------------------------------------------------------------------------
class TestFieldsAndLookupAtStartup(SimpleTestCase):
    BASE = {"tenant_field": "brokerage", "compiled": False}

    def test_unknown_fields_are_refused(self):
        """A typo only logged "Could not compile" (nothing with compiled:
        False) and then every request returned 500."""
        for fields in (
            ["id", "titel"],
            {"list": ["id"], "detail": ["id", "ownr"]},
            ["id", "owner__usernme"],
            ["id", "title__x"],
        ):
            with self.subTest(fields=fields):
                with self.assertRaises(ImproperlyConfigured):
                    _router((ScopeLoose, {**self.BASE, "fields": fields}))

    def test_fields_that_exist_boot(self):
        for fields in (
            "__all__",
            ["id", "title", "owner__username"],
            {"list": "__all__", "detail": ["id", "title"]},
        ):
            with self.subTest(fields=fields):
                _router((ScopeLoose, {**self.BASE, "fields": fields}))
        # A @property and an FK path on the models that use them.
        from tests.test_app.models import Gadget

        _router((Gadget, Gadget.turbodrf()), (ScopeItem, ScopeItem.turbodrf()))

    def test_lookup_field(self):
        for lookup in ("slug", "title", "owner_id__x"):
            with self.subTest(lookup=lookup):
                with self.assertRaises(ImproperlyConfigured):
                    _router(
                        (
                            ScopeLoose,
                            {**self.BASE, "fields": ["id"], "lookup_field": lookup},
                        )
                    )
        for lookup in ("pk", "id"):
            with self.subTest(lookup=lookup):
                _router(
                    (
                        ScopeLoose,
                        {**self.BASE, "fields": ["id"], "lookup_field": lookup},
                    )
                )


# ---------------------------------------------------------------------------
# Findings 14, 15: database-mode rows.
# ---------------------------------------------------------------------------
class TestRolePermissionValidation(TestCase):
    def setUp(self):
        from turbodrf.models import TurboDRFRole

        self.role = TurboDRFRole.objects.create(name="r")

    def _perm(self, **kw):
        from turbodrf.models import RolePermission

        fields = {
            "role": self.role,
            "app_label": "test_app",
            "model_name": "samplemodel",
        }
        return RolePermission(**{**fields, **kw})

    def test_rules_that_match_nothing_are_refused(self):
        """full_clean() passed, and the rule silently matched nothing: the
        field it meant to restrict stayed readable."""
        for kw in (
            {"app_label": "Test_App", "action": "read"},
            {"model_name": "SampleModel", "action": "read"},
            {"model_name": "samplemodl", "action": "read"},
            {"field_name": "secret_feild", "permission_type": "read"},
            {"field_name": "related_id", "permission_type": "read"},
            {"action": "Read"},
            {"field_name": "secret_field", "permission_type": "READ"},
        ):
            with self.subTest(**kw):
                with self.assertRaises(ValidationError):
                    self._perm(**kw).save()
                with self.assertRaises(ValidationError):
                    self._perm(**kw).full_clean()

    def test_valid_rules_save(self):
        self._perm(action="read").save()
        self._perm(field_name="secret_field", permission_type="read").save()
        self._perm(
            model_name="articlewithcategories",
            field_name="categories",
            permission_type="write",
        ).save()

    @override_settings(TURBODRF_ALLOW_UNKNOWN_PERMISSIONS=True)
    def test_unknown_model_allowed_when_opted_in(self):
        self._perm(app_label="plugin", model_name="thing", action="read").save()


@override_settings(
    TURBODRF_PERMISSION_MODE="database", TURBODRF_DISABLE_PERMISSIONS=False
)
class TestStaleRoleSave(TestCase):
    def test_stale_instance_does_not_restore_a_revoked_permission(self):
        """A stale TurboDRFRole wrote its in-memory version + 1, moving the
        version back onto a cached snapshot's key."""
        from turbodrf.backends import build_permission_snapshot
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        cache.clear()
        role = TurboDRFRole.objects.create(name="r")
        perm = dict(role=role, app_label="test_app", model_name="samplemodel")
        RolePermission.objects.create(action="read", **perm)
        stale = TurboDRFRole.objects.get(pk=role.pk)
        delete = RolePermission.objects.create(action="delete", **perm)
        user = User.objects.create_user(username="u")
        UserRole.objects.create(user=user, role=role)
        snap = build_permission_snapshot(user, SampleModel, use_cache=True)
        self.assertTrue(snap.can_perform_action("delete"))

        delete.delete()
        stale.description = "edited"
        stale.save()

        role.refresh_from_db()
        self.assertEqual(role.version, 5)
        self.assertEqual(stale.version, 5)
        self.assertEqual(role.description, "edited")
        snap = build_permission_snapshot(user, SampleModel, use_cache=True)
        self.assertFalse(snap.can_perform_action("delete"))
