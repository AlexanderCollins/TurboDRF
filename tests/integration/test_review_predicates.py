"""Security review (0.6.x): predicate writes, tenancy registration and the
router's startup gates, asserted over HTTP.

Fixture (brokerage = tenant): tenant A has members m1, m2 (role ``member``),
manager ``mgr`` (``member`` + ``manager``, the Owner bypass role) and
``collab`` (role ``collaborator``); tenant B has b1.
"""

from contextlib import contextmanager
from unittest import mock

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q
from django.test import TestCase, override_settings
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    Brokerage,
    Deal,
    RbacBoard,
    ScopeBoard,
    ScopeFolder,
    ScopeItem,
    ScopeLoose,
    ScopeMultiOwner,
)
from turbodrf.decorators import turbodrf_action
from turbodrf.predicates import Custom, Either, Members, Owner

User = get_user_model()

_MODELS = ("scopeloose", "scopeboard", "scopemultiowner", "scopeitem", "scopefolder")
_WRITE = [f"test_app.{m}.{a}" for m in _MODELS for a in ("read", "create", "update")]

ROLES = {
    **settings.TURBODRF_ROLES,
    "member": list(_WRITE),
    "manager": list(settings.TURBODRF_ROLES["manager"]) + _WRITE,
    "collaborator": list(_WRITE),
}


@contextmanager
def _preserve_registries():
    """Router builds and registry edits mutate module-level state shared by
    the whole worker. Snapshot and restore it."""
    import turbodrf.router as router_mod
    from turbodrf import compiler, predicates

    preds = dict(predicates._model_predicates)
    tenants = dict(predicates._model_tenant_fields)
    plans = dict(compiler._compiled_plans)
    flag = router_mod._bypass_roles_validated
    try:
        yield
    finally:
        predicates._model_predicates.clear()
        predicates._model_predicates.update(preds)
        predicates._model_tenant_fields.clear()
        predicates._model_tenant_fields.update(tenants)
        compiler._compiled_plans.clear()
        compiler._compiled_plans.update(plans)
        router_mod._bypass_roles_validated = flag


def collaborator_q(request, roles):
    if "collaborator" not in roles:
        return Q(pk__in=[])
    return Q(title__startswith="shared")


def block_collaborator_writes(data, instance, request):
    from turbodrf.backends import get_user_roles

    if "collaborator" in set(get_user_roles(request.user)):
        return ["collaborators cannot write."]
    return []


@override_settings(TURBODRF_ROLES=ROLES)
class _Fixture(TestCase):
    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        self.A = Brokerage.objects.create(name="A")
        self.B = Brokerage.objects.create(name="B")
        self.m1 = self._user("m1", ["member"], self.A)
        self.m2 = self._user("m2", ["member"], self.A)
        self.mgr = self._user("mgr", ["member", "manager"], self.A)
        self.collab = self._user("collab", ["collaborator"], self.A)
        self.b1 = self._user("b1", ["member"], self.B)

    def tearDown(self):
        _test_user_brokerages.clear()

    def _user(self, name, roles, brokerage):
        user = User.objects.create_user(username=name, password="x")
        user._test_roles = roles
        set_test_brokerage(user, brokerage)
        return user

    @contextmanager
    def _viewset(self, model, config):
        """A fresh router's viewset for ``model`` with ``config``; the router
        registers the config's tenancy, which the serializer reads."""
        from turbodrf.router import TurboDRFRouter

        cfg = classmethod(lambda cls: dict(config))
        with (
            _preserve_registries(),
            mock.patch.object(model, "turbodrf", cfg),
            mock.patch("turbodrf.router.apps.get_models", return_value=[model]),
        ):
            router = TurboDRFRouter()
            yield router.registry[0][1]

    def _call(self, viewset, user, method, data=None, pk=None, action=None):
        factory = APIRequestFactory()
        request = getattr(factory, method)("/x/", data, format="json")
        force_authenticate(request, user=user)
        cache.clear()
        default = {
            "post": "create",
            "patch": "partial_update",
            "put": "update",
            "get": "retrieve" if pk else "list",
        }[method]
        view = viewset.as_view({method: action or default})
        return view(request, pk=pk) if pk else view(request)


# ---------------------------------------------------------------------------
# Finding 1: Either passed a write as soon as one child accepted, so the
# Owner rule never held inside Either.
# ---------------------------------------------------------------------------
class TestEitherWrites(_Fixture):
    def _owner_or_collaborator(self):
        return {
            "tenant_field": "brokerage",
            "visibility": [
                Either(
                    Owner("owner", bypass=["manager"]),
                    Custom(collaborator_q, write_validator=block_collaborator_writes),
                )
            ],
            "fields": ["id", "title", "owner"],
            "compiled": False,
        }

    def test_member_cannot_assign_owner_to_another_user(self):
        """docs/tenancy.md's cross-user grant recipe: a plain member POSTing
        {"owner": victim} got 201."""
        with self._viewset(ScopeLoose, self._owner_or_collaborator()) as vs:
            r = self._call(vs, self.m1, "post", {"title": "t", "owner": self.m2.pk})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(ScopeLoose.objects.exists())

    def test_bypass_role_can_assign_owner(self):
        with self._viewset(ScopeLoose, self._owner_or_collaborator()) as vs:
            r = self._call(vs, self.mgr, "post", {"title": "t", "owner": self.m2.pk})
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(ScopeLoose.objects.get().owner_id, self.m2.pk)

    def test_custom_write_validator_still_blocks(self):
        with self._viewset(ScopeLoose, self._owner_or_collaborator()) as vs:
            r = self._call(vs, self.collab, "post", {"title": "shared x"})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("collaborators cannot write.", str(r.data))

    def test_either_owner_owner_rejects_foreign_owner(self):
        """Either(Owner('owner'), Owner('editor')): leaving 'editor' out let
        the second child pass the write."""
        config = {
            "tenant_field": "brokerage",
            "visibility": [Either(Owner("owner"), Owner("editor"))],
            "fields": ["id", "title", "owner", "editor"],
            "compiled": False,
        }
        with self._viewset(ScopeMultiOwner, config) as vs:
            r = self._call(vs, self.m1, "post", {"title": "t", "owner": self.m2.pk})
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(ScopeMultiOwner.objects.exists())

    def _owner_or_watcher(self):
        return {
            "tenant_field": "brokerage",
            "visibility": [Either(Owner("owner"), Members("watchers"))],
            "fields": ["id", "title", "owner"],
            "compiled": False,
        }

    def test_watcher_cannot_reassign_owner(self):
        """Either(Owner('created_by'), Members('participants')): a participant
        PATCHed the owner to a third user (200)."""
        board = ScopeBoard.objects.create(title="b", brokerage=self.A, owner=self.m1)
        board.watchers.add(self.m2)
        with self._viewset(ScopeBoard, self._owner_or_watcher()) as vs:
            to_third = self._call(
                vs, self.m2, "patch", {"owner": self.mgr.pk}, pk=board.pk
            )
            to_self = self._call(
                vs, self.m2, "patch", {"owner": self.m2.pk}, pk=board.pk
            )
            renamed = self._call(vs, self.m2, "patch", {"title": "x"}, pk=board.pk)
        self.assertEqual(to_third.status_code, 400, to_third.data)
        self.assertEqual(to_self.status_code, 400, to_self.data)
        self.assertEqual(renamed.status_code, 200, renamed.data)
        board.refresh_from_db()
        self.assertEqual(board.owner_id, self.m1.pk)

    def test_creator_owns_and_sees_the_row(self):
        """Either didn't auto-fill: the AGENTS.md recipe created the row with
        owner NULL, and the creator then got a 404 on it."""
        with self._viewset(ScopeBoard, self._owner_or_watcher()) as vs:
            created = self._call(vs, self.m1, "post", {"title": "mine"})
            self.assertEqual(created.status_code, 201, created.data)
            board = ScopeBoard.objects.get()
            self.assertEqual(board.owner_id, self.m1.pk)
            detail = self._call(vs, self.m1, "get", pk=board.pk)
        self.assertEqual(detail.status_code, 200)


# ---------------------------------------------------------------------------
# Findings 2 and 4: Owner's write check on update and without a user.
# ---------------------------------------------------------------------------
class TestOwnerWrites(_Fixture):
    CONFIG = {
        "tenant_field": "brokerage",
        "owner_field": ["owner", "editor"],
        "fields": ["id", "title", "owner", "editor"],
        "compiled": False,
    }

    def test_co_owner_cannot_take_the_row(self):
        """owner=m1, editor=m2: m2 PATCHing {"owner": m2} took the row (200),
        and m1 then got a 404."""
        row = ScopeMultiOwner.objects.create(
            title="t", brokerage=self.A, owner=self.m1, editor=self.m2
        )
        with self._viewset(ScopeMultiOwner, self.CONFIG) as vs:
            r = self._call(vs, self.m2, "patch", {"owner": self.m2.pk}, pk=row.pk)
            still = self._call(vs, self.m1, "get", pk=row.pk)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("assigned to another user", str(r.data))
        self.assertEqual(still.status_code, 200)
        row.refresh_from_db()
        self.assertEqual(row.owner_id, self.m1.pk)

    def test_clearing_owner_has_its_own_message(self):
        row = ScopeMultiOwner.objects.create(title="t", brokerage=self.A, owner=self.m1)
        with self._viewset(ScopeMultiOwner, self.CONFIG) as vs:
            r = self._call(vs, self.m1, "patch", {"owner": None}, pk=row.pk)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn("Cannot clear owner", str(r.data))
        self.assertNotIn("different user", str(r.data))

    def test_anonymous_write_cannot_set_owner(self):
        """An AllowAny action saving through the serializer could create a
        row with owner=<anyone>: Owner skipped its check without a user."""
        from rest_framework.exceptions import ValidationError

        from turbodrf.serializers import _apply_predicate_writes

        request = mock.Mock()
        request.user = mock.Mock(is_authenticated=False, pk=None)
        with self.assertRaises(ValidationError):
            _apply_predicate_writes(
                RbacBoard, {"title": "t", "owner": self.m1}, None, request
            )


# ---------------------------------------------------------------------------
# Finding 5: a visibility list whose predicates can lift the restriction
# doesn't count as a tenancy declaration.
# ---------------------------------------------------------------------------
@override_settings(
    TURBODRF_TENANT_MODEL="test_app.Brokerage", TURBODRF_REQUIRE_TENANCY=True
)
class TestVisibilityOnlyTenancyGate(_Fixture):
    def _build(self, visibility):
        config = {
            "visibility": visibility,
            "fields": ["id", "title"],
            "compiled": False,
        }
        with self._viewset(ScopeLoose, config) as vs:
            return vs

    def test_bypass_owner_without_tenant_field_is_refused(self):
        """Owner('owner', bypass=['manager']) alone let a manager list every
        tenant's rows."""
        with self.assertRaises(ImproperlyConfigured) as cm:
            self._build([Owner("owner", bypass=["manager"])])
        message = str(cm.exception)
        self.assertIn("'tenant_field'", message)
        self.assertNotIn("auto-detected", message)  # autodetect is off
        self.assertNotIn("'visibility': [...]", message)

    def test_conditional_or_lifting_either_is_refused(self):
        from turbodrf.predicates import Conditional

        for visibility in (
            [Conditional(when=Q(title="vip"), require_roles=["manager"])],
            [Either(Owner("owner", bypass=["manager"]), Owner("owner"))],
        ):
            with self.subTest(visibility=repr(visibility)):
                with self.assertRaises(ImproperlyConfigured):
                    self._build(visibility)

    def test_owner_without_bypass_counts(self):
        vs = self._build([Owner("owner")])
        self.assertIsNone(vs._tenant_field)

    def test_message_mentions_autodetection_only_when_on(self):
        """RbacBoard has no forward FK path to Brokerage."""
        config = {
            "owner_field": "owner",
            "bypass_owner_roles": ["manager"],
            "fields": ["id", "title"],
            "compiled": False,
        }
        for autodetect in (False, True):
            with (
                self.subTest(autodetect=autodetect),
                override_settings(TURBODRF_AUTODETECT_TENANT=autodetect),
                self.assertRaises(ImproperlyConfigured) as cm,
                self._viewset(RbacBoard, config),
            ):
                pass
            self.assertEqual("auto-detected" in str(cm.exception), autodetect)


# ---------------------------------------------------------------------------
# Finding 12: tenancy is registered for every model, enabled or not, at
# AppConfig.ready().
# ---------------------------------------------------------------------------
class TestTenancyRegisteredForEveryModel(_Fixture):
    def setUp(self):
        super().setUp()
        self.folder = ScopeFolder.objects.create(
            name="A1-private-plan", brokerage=self.A, owner=self.m1
        )

    def test_disabled_target_still_scopes_fk_writes(self):
        """With ScopeFolder disabled, m2 POSTed a ScopeItem pointing at m1's
        private folder (201, folder_name 'A1-private-plan')."""
        from turbodrf.predicates import clear_predicates, get_tenant_field
        from turbodrf.router import TurboDRFRouter

        disabled = {**ScopeFolder.turbodrf(), "enabled": False}
        with (
            _preserve_registries(),
            mock.patch.object(
                ScopeFolder, "turbodrf", classmethod(lambda cls: dict(disabled))
            ),
            mock.patch(
                "turbodrf.router.apps.get_models",
                return_value=[ScopeFolder, ScopeItem],
            ),
        ):
            clear_predicates()
            TurboDRFRouter()
            self.assertEqual(get_tenant_field(ScopeFolder), "brokerage")
            client = APIClient()
            client.force_authenticate(self.m2)
            r = client.post(
                "/api/scopeitems/",
                {"label": "x", "folder": self.folder.pk},
                format="json",
            )
        self.assertEqual(r.status_code, 400, r.content)
        self.assertNotIn("A1-private-plan", r.content.decode())
        self.assertFalse(ScopeItem.objects.exists())

    def test_app_ready_registers_without_a_router(self):
        """A process that never imports the URLconf (a Celery worker) had an
        empty registry: every model read as unscoped."""
        from turbodrf.predicates import (
            clear_predicates,
            get_predicates,
            get_tenant_field,
        )

        with _preserve_registries():
            clear_predicates()
            apps.get_app_config("turbodrf").ready()
            self.assertEqual(get_tenant_field(Deal), "brokerage")
            self.assertEqual(get_tenant_field(ScopeFolder), "brokerage")
            self.assertEqual(len(get_predicates(ScopeFolder)), 1)


# ---------------------------------------------------------------------------
# Finding 13: the generated viewset's `queryset` was every tenant's rows.
# ---------------------------------------------------------------------------
@turbodrf_action(detail=False, methods=["get"], url_path="every-id")
def every_id(self, request):
    return Response(sorted(self.queryset.values_list("pk", flat=True)))


class TestViewsetQuerysetIsScoped(_Fixture):
    def setUp(self):
        super().setUp()
        self.mine = Deal.objects.create(
            title="m", brokerage=self.A, assigned_broker=self.m1
        )
        Deal.objects.create(title="other", brokerage=self.A, assigned_broker=self.m2)
        Deal.objects.create(title="b", brokerage=self.B, assigned_broker=self.b1)

    @override_settings(
        TURBODRF_ROLES={**ROLES, "member": ROLES["member"] + ["test_app.deal.read"]}
    )
    def test_custom_action_reading_self_queryset_gets_no_rows(self):
        """An action using `self.queryset` returned every tenant's ids. There
        is no unscoped queryset to read now; get_queryset() is the way in."""
        config = {**Deal.turbodrf(), "actions": [every_id]}
        with self._viewset(Deal, config) as vs:
            self.assertIsNone(vs.queryset)
            with self.assertRaises(AttributeError):
                self._call(vs, self.m1, "get", action="every_id")

    def test_get_queryset_still_scopes_and_routes_resolve(self):
        import tests.urls

        viewset = dict((p, v) for p, v, _ in tests.urls.router.registry)["deals"]
        self.assertIsNone(viewset.queryset)
        client = APIClient()
        client.force_authenticate(self.m1)
        with override_settings(
            TURBODRF_ROLES={**ROLES, "member": ROLES["member"] + ["test_app.deal.read"]}
        ):
            r = client.get("/api/deals/")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual([row["id"] for row in r.data["data"]], [self.mine.pk])
