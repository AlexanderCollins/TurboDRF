"""turbodrf.register(): a model served without the mixin gets the same API,
startup checks and row scoping as one with it."""

from contextlib import contextmanager

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings
from django.urls import include, path
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import Brokerage, Deal, ScopeLedger

User = get_user_model()

LEDGER = {
    "tenant_field": "firm",
    "fields": ["id", "name", "firm"],
    "endpoint": "ledgers",
}
ROLES = {"bookkeeper": ["test_app.scopeledger.read", "test_app.scopeledger.create"]}

urlpatterns = []  # filled with a router that sees ScopeLedger (setUp)


@contextmanager
def registered(model, config):
    """``register(model, config)`` for the block; the registries as they were
    after it."""
    import turbodrf.router as router_mod
    from turbodrf import predicates, register
    from turbodrf.mixins import _registered

    saved = (
        dict(predicates._model_predicates),
        dict(predicates._model_tenant_fields),
        router_mod._bypass_roles_validated,
    )
    try:
        register(model, config)
        yield
    finally:
        _registered.pop(model, None)
        predicates._model_predicates.clear()
        predicates._model_predicates.update(saved[0])
        predicates._model_tenant_fields.clear()
        predicates._model_tenant_fields.update(saved[1])
        router_mod._bypass_roles_validated = saved[2]


@override_settings(ROOT_URLCONF=__name__, TURBODRF_ROLES=ROLES)
class TestRegisteredModelApi(TestCase):
    def setUp(self):
        from unittest import mock

        from tests.fixtures import router_apps
        from turbodrf.router import TurboDRFRouter

        cache.clear()
        _test_user_brokerages.clear()
        self.addCleanup(_test_user_brokerages.clear)
        self.a = Brokerage.objects.create(name="A")
        self.b = Brokerage.objects.create(name="B")
        self.mine = ScopeLedger.objects.create(name="mine", firm=self.a)
        self.theirs = ScopeLedger.objects.create(name="theirs", firm=self.b)
        self.user = User.objects.create_user("bk")
        self.user._test_roles = ["bookkeeper"]
        set_test_brokerage(self.user, self.a)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

        cm = registered(ScopeLedger, LEDGER)
        cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        with mock.patch("turbodrf.router.apps", router_apps([ScopeLedger])):
            router = TurboDRFRouter()
        urlpatterns[:] = [path("api/", include(router.urls))]
        self.addCleanup(urlpatterns.clear)

    def test_list_is_tenant_scoped(self):
        resp = self.client.get("/api/ledgers/")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([r["name"] for r in resp.data["data"]], ["mine"])
        self.assertEqual(
            self.client.get(f"/api/ledgers/{self.theirs.pk}/").status_code, 404
        )

    def test_create_fills_the_tenant(self):
        resp = self.client.post("/api/ledgers/", {"name": "new"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(ScopeLedger.objects.get(name="new").firm_id, self.a.pk)

    def test_tenancy_is_registered_for_other_models(self):
        from turbodrf.predicates import get_tenant_field

        self.assertEqual(get_tenant_field(ScopeLedger), "firm")


class TestRegisterRefusals(TestCase):
    def test_a_mixin_model_is_refused(self):
        from turbodrf import register

        with self.assertRaisesMessage(ImproperlyConfigured, "TurboDRFMixin"):
            register(Deal, {"fields": ["id"]})

    def test_a_non_model_or_bad_config_is_refused(self):
        from turbodrf import register

        with self.assertRaises(TypeError):
            register(object, {})
        with self.assertRaises(TypeError):
            register(ScopeLedger, ["id"])

    @override_settings(TURBODRF_TENANT_MODEL="test_app.Brokerage")
    def test_a_config_error_raises_and_rolls_back(self):
        from turbodrf import register
        from turbodrf.mixins import _registered

        with self.assertRaisesMessage(ImproperlyConfigured, "register(ScopeLedger"):
            register(ScopeLedger, {"tenant_field": "nope", "fields": ["id"]})
        self.assertNotIn(ScopeLedger, _registered)

    def test_a_callable_config_is_read_each_time(self):
        from turbodrf.mixins import turbodrf_config

        calls = []

        def config():
            calls.append(1)
            return dict(LEDGER)

        with registered(ScopeLedger, config):
            self.assertEqual(turbodrf_config(ScopeLedger)["endpoint"], "ledgers")
        self.assertTrue(calls)
