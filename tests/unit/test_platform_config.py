"""
The platform scope without HTTP: router arguments, startup validation of
``"platform"`` blocks, role resolution, permission snapshots and their cache
keys, the scope-aware registries, and the management commands.
"""

from contextlib import contextmanager
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated

from tests.fixtures import router_apps
from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    Brokerage,
    Deal,
    PlatformAccount,
    PlatformJob,
    PlatformTicket,
    ScopeFolder,
    ScopeItem,
)
from turbodrf.backends import (
    _permission_memo,
    attach_snapshot_to_request,
    build_permission_snapshot,
    get_cache_key,
    get_user_roles,
)
from turbodrf.mixins import _resolve_configured_fields
from turbodrf.predicates import (
    get_predicates,
    get_tenant_field,
    get_user_tenant,
    registered_tenant_field,
)
from turbodrf.scope import PLATFORM, TENANT, current_scope, use_scope

User = get_user_model()

TENANT_MODEL = dict(TURBODRF_TENANT_MODEL="test_app.Brokerage")


@contextmanager
def _preserve_registries():
    import turbodrf.router as router_mod
    from turbodrf import compiler, predicates

    saved = [
        (predicates._model_predicates, dict(predicates._model_predicates)),
        (predicates._model_tenant_fields, dict(predicates._model_tenant_fields)),
        (compiler._compiled_plans, dict(compiler._compiled_plans)),
        (compiler._platform_compiled_plans, dict(compiler._platform_compiled_plans)),
    ]
    flag = router_mod._bypass_roles_validated
    try:
        yield
    finally:
        for registry, contents in saved:
            registry.clear()
            registry.update(contents)
        router_mod._bypass_roles_validated = flag


def _router(*models_and_configs, **router_kwargs):
    """A router that sees only the given (model, config) pairs."""
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
            return TurboDRFRouter(**router_kwargs)
        finally:
            for p in patches:
                p.stop()


def _platform(*models_and_configs, **router_kwargs):
    router_kwargs.setdefault("scope", "platform")
    router_kwargs.setdefault("gate", [IsAdminUser])
    return _router(*models_and_configs, **router_kwargs)


TICKET = {"tenant_field": "brokerage", "compiled": False, "fields": ["id", "subject"]}
JOB = {"tenant_field": "brokerage", "compiled": False, "fields": ["id", "kind"]}


def _ticket(platform, **top):
    return (PlatformTicket, {**TICKET, **top, "platform": platform})


# ---------------------------------------------------------------------------
# Router arguments
# ---------------------------------------------------------------------------
class TestRouterArguments(SimpleTestCase):
    def test_platform_router_requires_a_gate(self):
        for gate in (None, [], ()):
            with self.subTest(gate=gate):
                with self.assertRaisesMessage(ImproperlyConfigured, "requires a gate"):
                    _platform(_ticket({"fields": ["id", "brokerage"]}), gate=gate)

    def test_gate_takes_permission_classes(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "not instances"):
            _platform(gate=[IsAdminUser()])
        with self.assertRaisesMessage(ImproperlyConfigured, "permission classes"):
            _platform(gate=["IsAdminUser"])
        router = _platform(gate=[IsAuthenticated & IsAdminUser])
        self.assertEqual(len(router.gate), 1)
        self.assertEqual(_platform(gate=IsAdminUser).gate, (IsAdminUser,))

    def test_unknown_scope(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "scope must be one of"):
            _router(scope="staff")

    def test_class_attributes(self):
        from turbodrf.router import TurboDRFRouter

        class ConsoleRouter(TurboDRFRouter):
            scope = "platform"
            gate = [IsAdminUser]

        with _preserve_registries():
            with mock.patch("turbodrf.router.apps", router_apps([PlatformJob])):
                router = ConsoleRouter()
        self.assertEqual(router.scope, "platform")
        self.assertEqual(router.gate, (IsAdminUser,))
        self.assertEqual([p for p, _v, _b in router.registry], ["jobs"])
        self.assertFalse(router.include_root_view)

        class Ungated(TurboDRFRouter):
            scope = "platform"

        with self.assertRaisesMessage(ImproperlyConfigured, "requires a gate"):
            Ungated()

    def test_root_view(self):
        tenant = _router((PlatformJob, {**JOB, "enabled": False}))
        self.assertTrue(tenant.include_root_view)
        platform = _platform((PlatformJob, PlatformJob.turbodrf()))
        self.assertFalse(platform.include_root_view)
        self.assertNotIn(
            "platform-api-root", [getattr(u, "name", None) for u in platform.urls]
        )
        indexed = _platform(
            (PlatformJob, PlatformJob.turbodrf()), include_root_view=True
        )
        root = next(u for u in indexed.urls if u.name == "platform-api-root")
        self.assertEqual(root.callback.cls.permission_classes, [IsAdminUser])

    def test_tenant_router_takes_an_optional_gate(self):
        router = _router((PlatformJob, {**JOB}), gate=[IsAuthenticated])
        viewset = router.registry[0][1]
        self.assertEqual(viewset.turbodrf_scope, TENANT)
        self.assertEqual(viewset.turbodrf_gate, (IsAuthenticated,))
        plain = _router((PlatformJob, {**JOB}))
        self.assertEqual(plain.registry[0][1].turbodrf_gate, ())

    def test_platform_viewsets(self):
        router = _platform((PlatformJob, PlatformJob.turbodrf()))
        prefix, viewset, basename = router.registry[0]
        self.assertEqual((prefix, basename), ("jobs", "platform-platformjob"))
        self.assertEqual(viewset.__name__, "PlatformJobPlatformViewSet")
        self.assertEqual(viewset.turbodrf_scope, PLATFORM)
        self.assertEqual(viewset.turbodrf_gate, (IsAdminUser,))
        self.assertIsNone(viewset._tenant_field)
        self.assertEqual(viewset._predicates, [])
        self.assertTrue(viewset._read_only)
        self.assertTrue(callable(viewset.retry_job))
        names = {u.name for u in router.urls}
        self.assertIn("platform-platformjob-list", names)
        self.assertIn("platform-platformjob-retry-job", names)


# ---------------------------------------------------------------------------
# Which models each router registers
# ---------------------------------------------------------------------------
class TestRegistration(SimpleTestCase):
    def test_only_models_with_a_platform_block(self):
        router = _platform(
            (PlatformJob, PlatformJob.turbodrf()),
            (Deal, Deal.turbodrf()),
            (PlatformAccount, PlatformAccount.turbodrf()),
        )
        self.assertEqual(
            sorted(v.model.__name__ for _p, v, _b in router.registry),
            ["PlatformAccount", "PlatformJob"],
        )

    def test_top_level_enabled_false_still_serves_the_platform_block(self):
        config = {**JOB, "enabled": False, "platform": {"read_only": True}}
        self.assertEqual(_router((PlatformJob, config)).registry, [])
        router = _platform((PlatformJob, config))
        self.assertEqual([p for p, _v, _b in router.registry], ["platformjobs"])

    def test_platform_enabled_false_gives_no_route(self):
        config = {**JOB, "platform": {"enabled": False, "read_only": True}}
        self.assertEqual(_platform((PlatformJob, config)).registry, [])
        self.assertEqual(len(_router((PlatformJob, config)).registry), 1)

    def test_platform_enabled_false_keeps_the_model_reachable(self):
        account = {
            "tenant_field": "brokerage",
            "fields": ["id", "name"],
            "platform": {"enabled": False},
        }
        job = {
            **JOB,
            "platform": {
                "read_only": True,
                "fields": ["id", "account", "account__name"],
            },
        }
        router = _platform((PlatformAccount, account), (PlatformJob, job))
        self.assertEqual([p for p, _v, _b in router.registry], ["platformjobs"])
        no_block = {"tenant_field": "brokerage", "fields": ["id", "name"]}
        with self.assertRaisesMessage(ImproperlyConfigured, "can't reach"):
            _platform((PlatformAccount, no_block), (PlatformJob, job))

    def test_the_tenant_router_ignores_platform_blocks(self):
        router = _router((PlatformAccount, PlatformAccount.turbodrf()))
        prefix, viewset, basename = router.registry[0]
        self.assertEqual((prefix, basename), ("platform-accounts", "platformaccount"))
        self.assertEqual(viewset.turbodrf_scope, TENANT)
        self.assertEqual(viewset._tenant_field, "brokerage")

    def test_endpoint_collision_within_the_platform_router(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "both route to 'jobs/'"):
            _platform(
                (PlatformJob, PlatformJob.turbodrf()),
                _ticket({"endpoint": "jobs", "read_only": True}),
            )


# ---------------------------------------------------------------------------
# Startup validation of the platform block
# ---------------------------------------------------------------------------
@override_settings(**TENANT_MODEL)
class TestPlatformBlockValidation(SimpleTestCase):
    def assertRefused(self, platform, message, **top):
        for build in (_router, _platform):
            with self.subTest(router=build.__name__):
                with self.assertRaisesMessage(ImproperlyConfigured, message):
                    build(_ticket(platform, **top))

    def test_the_platform_key_is_known(self):
        _router(_ticket({"read_only": True}))

    def test_block_must_be_a_dict(self):
        self.assertRefused(True, "'platform' must be a dict")

    def test_tenancy_keys_are_refused(self):
        for key, value in (
            ("tenant_field", "brokerage"),
            ("visibility", []),
            ("owner_field", "assignee"),
            ("bypass_owner_roles", ["admin"]),
            ("tenancy", "shared"),
            ("public_access", True),
        ):
            with self.subTest(key=key):
                self.assertRefused(
                    {"read_only": True, key: value}, "tenancy keys belong at the top"
                )

    def test_unknown_keys_are_refused(self):
        self.assertRefused(
            {"read_only": True, "feilds": ["id"]}, "unknown keys ['feilds']"
        )
        self.assertRefused(
            {"read_only": True, "platform": {}}, "unknown keys ['platform']"
        )
        self.assertRefused(
            {"read_only": True, "sensitive_fields": ["x"]}, "class attribute"
        )

    def test_fields_are_checked(self):
        self.assertRefused(
            {"read_only": True, "fields": ["id", "subjct"]},
            "turbodrf()['platform'] 'fields' lists ['subjct']",
        )
        self.assertRefused({"read_only": True, "fields": "id"}, "must be a list")

    def test_lookup_field_is_checked(self):
        self.assertRefused(
            {"read_only": True, "lookup_field": "nope"}, "'lookup_field' 'nope'"
        )
        self.assertRefused(
            {"read_only": True, "lookup_field": "subject"}, "isn't unique"
        )

    def test_property_config_is_checked(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "not an attribute"):
            _platform(_ticket({"read_only": True, "unscoped_properties": ["nope"]}))

    def test_relations_must_reach_platform_models(self):
        self.assertRefused(
            {"read_only": True, "fields": ["id", "deal__title"]},
            "'deal__title', whose relation 'deal' leads to test_app.Deal",
        )

    def test_a_bare_fk_to_an_unreachable_model_is_allowed(self):
        _platform(_ticket({"read_only": True, "fields": ["id", "deal"]}))

    def test_to_many_relations_must_reach_platform_models(self):
        folder = {
            "tenant_field": "brokerage",
            "compiled": False,
            "fields": ["id", "name"],
            "platform": {"read_only": True, "fields": ["id", "items"]},
        }
        with self.assertRaisesMessage(ImproperlyConfigured, "'items'"):
            _platform((ScopeFolder, folder), (ScopeItem, ScopeItem.turbodrf()))
        reachable = {**ScopeItem.turbodrf(), "platform": {"enabled": False}}
        _platform((ScopeFolder, folder), (ScopeItem, reachable))

    def test_tenant_model_and_plain_models_are_reachable(self):
        _platform(
            _ticket(
                {
                    "read_only": True,
                    "fields": ["id", "brokerage__name", "assignee__username"],
                }
            )
        )

    def test_searchable_fields_must_be_platform_fields(self):
        self.assertRefused(
            {"read_only": True, "fields": ["id"], "searchable_fields": ["subject"]},
            "'searchable_fields' lists 'subject', which isn't in its 'fields'",
        )
        self.assertRefused(
            {"read_only": True, "searchable_fields": "subject"}, "must be a list"
        )
        _platform(_ticket({"read_only": True, "searchable_fields": ["^subject"]}))

    def test_class_attribute_search_fields_dont_apply(self):
        router = _platform(_ticket({"read_only": True, "fields": ["id", "subject"]}))
        viewset = router.registry[0][1]()
        viewset.request = None
        with (
            use_scope(PLATFORM),
            mock.patch.object(
                PlatformTicket, "searchable_fields", ["subject"], create=True
            ),
            mock.patch.object(
                PlatformTicket,
                "turbodrf",
                classmethod(lambda cls: {**TICKET, "platform": {"read_only": True}}),
            ),
        ):
            self.assertEqual(viewset.search_fields, [])

    def test_a_create_must_be_able_to_name_the_tenant(self):
        self.assertRefused(
            {"fields": ["id", "subject"]}, "leave out 'brokerage', the first hop"
        )
        self.assertRefused(
            {"fields": ["id", "subject"], "http_methods": ["get", "POST"]},
            "allows creates (POST)",
        )
        for ok in (
            {"fields": ["id", "subject"], "read_only": True},
            {"fields": ["id", "subject"], "http_methods": ["get", "patch"]},
            {"fields": ["id", "subject", "brokerage__name"]},
            {"fields": "__all__"},
        ):
            with self.subTest(block=ok):
                _platform(_ticket(ok))

    def test_tenant_router_checks_blocks_of_disabled_models(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "unknown keys"):
            _router(_ticket({"read_only": True, "bogus": 1}, enabled=False))


# ---------------------------------------------------------------------------
# Platform role settings
# ---------------------------------------------------------------------------
class TestPlatformRoleSettings(SimpleTestCase):
    def test_platform_roles_are_required(self):
        for value in (None, {}, ["admin"]):
            with self.subTest(value=value):
                with override_settings(TURBODRF_PLATFORM_ROLES=value):
                    with self.assertRaisesMessage(
                        ImproperlyConfigured, "requires TURBODRF_PLATFORM_ROLES"
                    ):
                        _platform((PlatformJob, PlatformJob.turbodrf()))

    @override_settings(
        TURBODRF_PLATFORM_ROLES=None, TURBODRF_PERMISSION_MODE="database"
    )
    def test_database_mode_says_platform_roles_are_static(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "always static"):
            _platform((PlatformJob, PlatformJob.turbodrf()))

    @override_settings(
        TURBODRF_PLATFORM_ROLES={"support": ["test_app.platformjob.raed"]}
    )
    def test_permission_strings_are_validated(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured, "TURBODRF_PLATFORM_ROLES contains 1 invalid"
        ):
            _platform((PlatformJob, PlatformJob.turbodrf()))

    @override_settings(TURBODRF_PLATFORM_ROLE_GETTER="tests.nowhere.roles")
    def test_role_getter_must_import(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "can't be imported"):
            _platform((PlatformJob, PlatformJob.turbodrf()))

    @override_settings(TURBODRF_PLATFORM_ROLE_GETTER=42)
    def test_role_getter_type(self):
        with self.assertRaisesMessage(
            ImproperlyConfigured, "dotted path or a callable"
        ):
            _platform((PlatformJob, PlatformJob.turbodrf()))


# ---------------------------------------------------------------------------
# Roles, snapshots and caches by scope
# ---------------------------------------------------------------------------
class TestRolesAndSnapshotsByScope(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("scoped", password="x")
        self.user._test_roles = ["account_admin"]
        self.user._test_platform_roles = ["support"]

    def test_roles_by_scope(self):
        self.assertEqual(get_user_roles(self.user), ["account_admin"])
        with use_scope(PLATFORM):
            self.assertEqual(get_user_roles(self.user), ["support"])
        with _permission_memo():
            self.assertEqual(get_user_roles(self.user), ["account_admin"])
            with use_scope(PLATFORM):
                self.assertEqual(get_user_roles(self.user), ["support"])
            self.assertEqual(get_user_roles(self.user), ["account_admin"])

    @override_settings(TURBODRF_PLATFORM_ROLES={"guest": ["test_app.platformjob.read"]})
    def test_no_anonymous_platform_role(self):
        from django.contrib.auth.models import AnonymousUser

        with use_scope(PLATFORM):
            self.assertEqual(get_user_roles(AnonymousUser()), [])
            snapshot = build_permission_snapshot(AnonymousUser(), PlatformJob)
        self.assertEqual(snapshot.allowed_actions, set())

    def test_platform_scope_never_bypasses_permissions(self):
        from turbodrf.permissions import permissions_bypassed

        with override_settings(TURBODRF_DISABLE_PERMISSIONS=True):
            self.assertTrue(permissions_bypassed())
            with use_scope(PLATFORM):
                self.assertFalse(permissions_bypassed())

    def test_snapshots_by_scope(self):
        tenant = build_permission_snapshot(self.user, PlatformAccount)
        with use_scope(PLATFORM):
            platform = build_permission_snapshot(self.user, PlatformAccount)
        self.assertEqual(tenant.allowed_actions, {"read", "create", "update", "delete"})
        self.assertEqual(platform.allowed_actions, {"read"})
        with use_scope(PLATFORM):
            job = build_permission_snapshot(self.user, PlatformJob)
        self.assertNotIn("payload", job.readable_fields)  # auditor/admin rule
        self.assertEqual(
            build_permission_snapshot(self.user, PlatformJob).readable_fields,
            set(),  # no tenant role grants jobs
        )

    def test_cache_keys_by_scope(self):
        tenant_key = get_cache_key(self.user, PlatformAccount)
        with use_scope(PLATFORM):
            platform_key = get_cache_key(self.user, PlatformAccount)
        self.assertNotEqual(tenant_key, platform_key)
        self.assertTrue(tenant_key.startswith(f"turbodrf_perm:{self.user.pk}:"))
        self.assertTrue(
            platform_key.startswith(f"turbodrf_perm:platform:{self.user.pk}:")
        )

    def test_a_cached_platform_snapshot_never_serves_the_tenant_scope(self):
        self.user._test_roles = []
        with use_scope(PLATFORM):
            self.assertIn(
                "read",
                build_permission_snapshot(self.user, PlatformAccount).allowed_actions,
            )
        self.assertEqual(
            build_permission_snapshot(self.user, PlatformAccount).allowed_actions, set()
        )
        with _permission_memo():
            with use_scope(PLATFORM):
                self.assertTrue(
                    build_permission_snapshot(
                        self.user, PlatformAccount
                    ).allowed_actions
                )
            self.assertFalse(
                build_permission_snapshot(self.user, PlatformAccount).allowed_actions
            )

    def test_request_snapshots_by_scope(self):
        request = SimpleNamespace(user=self.user)
        tenant = attach_snapshot_to_request(request, PlatformAccount)
        with use_scope(PLATFORM):
            platform = attach_snapshot_to_request(request, PlatformAccount)
        self.assertIsNot(tenant, platform)
        self.assertEqual(
            set(request._turbodrf_snapshots),
            {"test_app.platformaccount", "platform:test_app.platformaccount"},
        )

    def test_role_changes_are_picked_up(self):
        with use_scope(PLATFORM):
            before = build_permission_snapshot(self.user, PlatformAccount)
            self.user._test_platform_roles = ["auditor"]
            after = build_permission_snapshot(self.user, PlatformAccount)
        self.assertEqual(before.allowed_actions, {"read"})
        self.assertEqual(after.allowed_actions, set())


# ---------------------------------------------------------------------------
# Registries and config by scope
# ---------------------------------------------------------------------------
@override_settings(**TENANT_MODEL)
class TestRegistriesByScope(TestCase):
    def test_row_scoping_by_scope(self):
        self.assertEqual(get_tenant_field(PlatformTicket), "brokerage")
        self.assertEqual(len(get_predicates(PlatformTicket)), 1)  # Owner
        with use_scope(PLATFORM):
            self.assertIsNone(get_tenant_field(PlatformTicket))
            self.assertEqual(get_predicates(PlatformTicket), [])
            self.assertEqual(registered_tenant_field(PlatformTicket), "brokerage")
            # A TurboDRF model without a platform block matches no row.
            (deny,) = get_predicates(Deal)
            self.assertEqual(deny.q(None, set()).children, [("pk__in", [])])
            # The tenant model and plain models have no rule.
            self.assertEqual(get_predicates(Brokerage), [])
            self.assertEqual(get_predicates(User), [])

    def test_no_tenant_from_the_staff_member(self):
        brokerage = Brokerage.objects.create(name="b")
        user = User.objects.create_user("tenant_user")
        self.addCleanup(_test_user_brokerages.pop, user.pk, None)
        set_test_brokerage(user, brokerage)
        self.assertEqual(get_user_tenant(user), brokerage)
        with use_scope(PLATFORM):
            self.assertIsNone(get_user_tenant(user))

    def test_configured_fields_by_scope(self):
        self.assertEqual(
            _resolve_configured_fields(PlatformAccount, "list"), ["id", "name", "plan"]
        )
        with use_scope(PLATFORM):
            self.assertEqual(
                _resolve_configured_fields(PlatformAccount, "list"),
                ["id", "name", "plan", "staff_notes", "brokerage", "brokerage__name"],
            )
            self.assertEqual(PlatformAccount.get_api_fields("list")[3], "staff_notes")
            self.assertEqual(_resolve_configured_fields(Deal, "list"), [])

    def test_scope_resets(self):
        self.assertEqual(current_scope(), TENANT)
        with self.assertRaises(RuntimeError):
            with use_scope(PLATFORM):
                raise RuntimeError
        self.assertEqual(current_scope(), TENANT)
        with self.assertRaises(ValueError):
            with use_scope("staff"):
                pass


# ---------------------------------------------------------------------------
# Viewsets built outside the router, and mixins
# ---------------------------------------------------------------------------
class TestHandWrittenViewsets(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user("hw_staff", is_staff=True)
        self.staff._test_platform_roles = ["admin"]

    def _call(self, viewset_class, user):
        from rest_framework.test import APIRequestFactory, force_authenticate

        request = APIRequestFactory().get("/")
        force_authenticate(request, user=user)
        return viewset_class.as_view({"get": "list"})(request)

    def test_platform_viewset_without_a_gate_refuses(self):
        from turbodrf.views import TurboDRFViewSet

        class NoGate(TurboDRFViewSet):
            model = PlatformJob
            turbodrf_scope = PLATFORM
            _tenant_field = None
            _predicates = []

        with self.assertRaisesMessage(ImproperlyConfigured, "without a gate"):
            self._call(NoGate, self.staff)

    def test_overriding_get_permissions_keeps_the_gate(self):
        from turbodrf.views import TurboDRFViewSet

        class Lax:
            def get_permissions(self):
                return [AllowAny()]

        class Gated(Lax, TurboDRFViewSet):
            model = PlatformJob
            turbodrf_scope = PLATFORM
            turbodrf_gate = (IsAdminUser,)
            _tenant_field = None
            _predicates = []

        outsider = User.objects.create_user("hw_outsider")
        self.assertEqual(self._call(Gated, outsider).status_code, 403)
        self.assertEqual(self._call(Gated, self.staff).status_code, 200)

    def test_scope_is_visible_to_the_viewset(self):
        from turbodrf.views import TurboDRFViewSet

        seen = []

        class Probe(TurboDRFViewSet):
            model = PlatformJob
            turbodrf_scope = PLATFORM
            turbodrf_gate = (AllowAny,)
            _tenant_field = None
            _predicates = []

            def list(self, request, *args, **kwargs):
                seen.append(
                    (self.turbodrf_scope, current_scope(), get_user_roles(request.user))
                )
                return super().list(request, *args, **kwargs)

        self.assertEqual(self._call(Probe, self.staff).status_code, 200)
        self.assertEqual(seen, [(PLATFORM, PLATFORM, ["admin"])])
        self.assertEqual(current_scope(), TENANT)


# ---------------------------------------------------------------------------
# Management commands
# ---------------------------------------------------------------------------
class TestCommands(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command(*args, stdout=out, stderr=out)
        return out.getvalue()

    def test_check_platform_scope(self):
        out = self.run_command("turbodrf_check", "--scope", "platform")
        self.assertIn("test_app.PlatformJob (platform: jobs/) [compiled]", out)
        self.assertIn("test_app.PlatformAccount (platform: accounts/)", out)
        self.assertIn("Tenant path: brokerage", out)
        self.assertIn("Rows: every tenant", out)
        self.assertNotIn("test_app.Deal", out)
        self.assertNotIn("Public access", out)

    def test_check_tenant_scope_is_unchanged(self):
        out = self.run_command("turbodrf_check", "--model", "PlatformAccount")
        self.assertIn("test_app.PlatformAccount [compiled] OK", out)
        self.assertIn("Fields: 3 DB", out)
        self.assertNotIn("PlatformJob", self.run_command("turbodrf_check"))

    def test_check_platform_model_filter(self):
        out = self.run_command(
            "turbodrf_check", "--scope", "platform", "--model", "Deal"
        )
        self.assertIn("No TurboDRF model with a platform block named 'Deal'", out)

    def test_explain_platform_scope(self):
        out = self.run_command(
            "turbodrf_explain",
            "PlatformJob",
            "--scope",
            "platform",
            "--role",
            "auditor",
        )
        self.assertIn("PlatformJob (platform scope)", out)
        self.assertIn("brokerage_name ← brokerage__name", out)
        self.assertIn("Permission filtering (role: auditor)", out)
        # auditor can't read brokerages or accounts: their nested values (and
        # the FK only they bring in) are pruned.
        self.assertIn("Rendered: id, kind, status, attempts, brokerage", out)
        self.assertIn("Pruned: account, brokerage_name, account_name", out)
        out = self.run_command(
            "turbodrf_explain",
            "PlatformJob",
            "--scope",
            "platform",
            "--role",
            "support",
        )
        self.assertIn("All fields permitted", out)

    def test_explain_platform_role_must_exist(self):
        out = self.run_command(
            "turbodrf_explain",
            "PlatformJob",
            "--scope",
            "platform",
            "--role",
            "account_admin_typo",
        )
        self.assertIn("not found in TURBODRF_PLATFORM_ROLES", out)

    def test_explain_platform_field_selection(self):
        out = self.run_command(
            "turbodrf_explain",
            "PlatformAccount",
            "--scope",
            "platform",
            "--fields",
            "staff_notes",
        )
        self.assertIn("Rendered: staff_notes", out)

    def test_explain_model_without_a_platform_block(self):
        from django.core.management.base import CommandError

        with self.assertRaisesMessage(CommandError, "no platform block"):
            self.run_command("turbodrf_explain", "Deal", "--scope", "platform")

    def test_explain_opted_out_platform_model(self):
        out = self.run_command(
            "turbodrf_explain", "PlatformTicket", "--scope", "platform"
        )
        self.assertIn("Compiled: False (opted out", out)
        self.assertEqual(PlatformTicket.turbodrf()["platform"]["compiled"], False)


# ---------------------------------------------------------------------------
# Tenant paths in the platform write checks
# ---------------------------------------------------------------------------
@override_settings(**TENANT_MODEL)
class TestPlatformTenantPaths(TestCase):
    def setUp(self):
        from tests.test_app.models import BankAccount

        self.BankAccount = BankAccount
        self.acme = Brokerage.objects.create(name="Acme")
        self.globex = Brokerage.objects.create(name="Globex")
        self.acme_deal = Deal.objects.create(title="a", brokerage=self.acme)
        self.globex_deal = Deal.objects.create(title="g", brokerage=self.globex)

    def test_chained_tenant_hop_is_required_on_create(self):
        from turbodrf.serializers import _platform_tenant_errors

        self.assertEqual(registered_tenant_field(self.BankAccount), "deal__brokerage")
        errors = _platform_tenant_errors(self.BankAccount, {"name": "x"}, None)
        self.assertEqual(list(errors), ["deal"])
        self.assertEqual(
            _platform_tenant_errors(self.BankAccount, {"deal": self.acme_deal}, None),
            {},
        )

    def test_row_and_value_tenants_through_a_chain(self):
        from turbodrf.serializers import _platform_row_tenant, _platform_value_tenant

        account = self.BankAccount.objects.create(deal=self.acme_deal)
        self.assertEqual(
            _platform_row_tenant(self.BankAccount, "deal__brokerage", {}, account),
            (self.acme.pk, False),
        )
        self.assertEqual(
            _platform_row_tenant(
                self.BankAccount,
                "deal__brokerage",
                {"deal": self.globex_deal},
                account,
            ),
            (self.globex.pk, True),
        )
        self.assertEqual(
            _platform_value_tenant(self.BankAccount, account, Brokerage, "brokerage"),
            self.acme.pk,
        )
        self.assertEqual(
            _platform_value_tenant(Brokerage, self.globex, Brokerage, "brokerage"),
            self.globex.pk,
        )

    def test_value_tenant_of_a_user(self):
        from turbodrf.serializers import _platform_value_tenant

        user = User.objects.create_user("tenant_path_user")
        self.addCleanup(_test_user_brokerages.pop, user.pk, None)
        set_test_brokerage(user, self.globex)
        self.assertEqual(
            _platform_value_tenant(User, user, Brokerage, "brokerage"), self.globex.pk
        )
        set_test_brokerage(user, None)
        self.assertIsNone(_platform_value_tenant(User, user, Brokerage, "brokerage"))


# ---------------------------------------------------------------------------
# Schema views
# ---------------------------------------------------------------------------
class TestSchemaViewArguments(SimpleTestCase):
    def test_platform_schema_view_requires_a_gate(self):
        from turbodrf.documentation import get_turbodrf_schema_view

        with self.assertRaisesMessage(ImproperlyConfigured, "requires a gate"):
            get_turbodrf_schema_view(scope="platform")
        with self.assertRaisesMessage(ImproperlyConfigured, "scope must be one of"):
            get_turbodrf_schema_view(scope="staff")

    def test_router_schema_views(self):
        from turbodrf.swagger import PlatformSchemaGenerator, RoleBasedSchemaGenerator

        platform = _platform((PlatformJob, PlatformJob.turbodrf()))
        view = platform.get_schema_view(title="Console")
        self.assertEqual(view.permission_classes, [IsAdminUser])
        self.assertEqual(view.turbodrf_scope, PLATFORM)
        self.assertTrue(issubclass(view.generator_class, PlatformSchemaGenerator))
        enumerator = view.generator_class.endpoint_enumerator_class
        self.assertEqual(enumerator.view_classes(), {platform.registry[0][1]})
        tenant = _router((PlatformJob, {**JOB}))
        view = tenant.get_schema_view()
        self.assertTrue(issubclass(view.generator_class, RoleBasedSchemaGenerator))
        self.assertFalse(issubclass(view.generator_class, PlatformSchemaGenerator))

    @override_settings(TURBODRF_ENABLE_DOCS=False)
    def test_docs_disabled(self):
        self.assertIsNone(
            _platform((PlatformJob, PlatformJob.turbodrf())).get_schema_view()
        )


class TestTenantGate(TestCase):
    def test_a_tenant_viewset_runs_its_gate_first(self):
        from rest_framework.test import APIRequestFactory, force_authenticate

        from turbodrf.views import TurboDRFViewSet

        class Gated(TurboDRFViewSet):
            model = PlatformAccount
            turbodrf_gate = (IsAdminUser,)

        user = User.objects.create_user("tenant_gate_user")
        user._test_roles = ["account_admin"]
        request = APIRequestFactory().get("/")
        force_authenticate(request, user=user)
        self.assertEqual(Gated.as_view({"get": "list"})(request).status_code, 403)
