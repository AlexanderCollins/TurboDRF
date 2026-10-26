"""Row-scope edges: a tenant FK with to_field, an FK to a proxy of the tenant
model, an Owner over a ``__`` path on writes, and the browsable API's
dropdown labels."""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import isolate_apps
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    BankAccount,
    Brokerage,
    Deal,
    RelatedModel,
    SampleModel,
    Transaction,
)
from turbodrf.predicates import (
    Owner,
    _model_predicates,
    _model_tenant_fields,
    register_predicates,
)
from turbodrf.serializers import _in_caller_tenant
from turbodrf.tenancy import _validate_tenant_path

User = get_user_model()


@isolate_apps("tests.test_app")
class TestTenantFieldToField(SimpleTestCase):
    def test_last_hop_with_to_field_is_refused(self):
        """An integer tenant is compared with, and written into, the to_field
        column: a tenant whose code is another tenant's pk crossed over."""

        class Firm(models.Model):
            code = models.CharField(max_length=8, unique=True)

            class Meta:
                app_label = "test_app"

        class Row(models.Model):
            firm = models.ForeignKey(Firm, models.CASCADE, to_field="code")

            class Meta:
                app_label = "test_app"

        with self.assertRaises(ImproperlyConfigured) as cm:
            _validate_tenant_path(Row, "firm")
        self.assertIn("primary key", str(cm.exception))

    def test_to_field_before_the_last_hop_is_allowed(self):
        class Firm(models.Model):
            class Meta:
                app_label = "test_app"

        class Desk(models.Model):
            code = models.CharField(max_length=8, unique=True)
            firm = models.ForeignKey(Firm, models.CASCADE)

            class Meta:
                app_label = "test_app"

        class Row(models.Model):
            desk = models.ForeignKey(Desk, models.CASCADE, to_field="code")

            class Meta:
                app_label = "test_app"

        _validate_tenant_path(Row, "desk__firm")


class TestProxyOfTheTenantModel(SimpleTestCase):
    @isolate_apps("tests.test_app")
    def test_fk_to_a_proxy_gets_the_co_tenant_check(self):
        class BrokerageProxy(Brokerage):
            class Meta:
                proxy = True
                app_label = "test_app"

        other = SimpleNamespace(pk=2)
        mine = SimpleNamespace(pk=1)
        for target in (Brokerage, BrokerageProxy):
            with self.subTest(target=target.__name__):
                self.assertFalse(
                    _in_caller_tenant(other, target, Brokerage, "brokerage", 1)
                )
                self.assertTrue(
                    _in_caller_tenant(mine, target, Brokerage, "brokerage", 1)
                )


class _Predicates:
    """Swap one model's registered predicates for the block."""

    def __init__(self, model, predicates):
        self.model, self.predicates = model, predicates

    def __enter__(self):
        self.saved = (dict(_model_predicates), dict(_model_tenant_fields))
        register_predicates(self.model, self.predicates)

    def __exit__(self, *exc):
        for registry, saved in zip(
            (_model_predicates, _model_tenant_fields), self.saved
        ):
            registry.clear()
            registry.update(saved)


BROKER = {
    "broker": [
        "test_app.transaction.read",
        "test_app.transaction.create",
        "test_app.transaction.update",
        "test_app.bankaccount.read",
    ]
}


@override_settings(TURBODRF_ROLES=BROKER)
class TestOwnerOverAPathOnWrites(TestCase):
    """Owner('bank_account__deal__assigned_broker'): a broker sees the
    transactions of their own deals, and can't write one onto a colleague's
    deal (the row would land in the colleague's scope)."""

    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        firm = Brokerage.objects.create(name="A")
        self.me = User.objects.create_user(username="me")
        self.colleague = User.objects.create_user(username="colleague")
        for user in (self.me, self.colleague):
            user._test_roles = ["broker"]
            set_test_brokerage(user, firm)
        mine = Deal.objects.create(
            title="mine", brokerage=firm, assigned_broker=self.me
        )
        theirs = Deal.objects.create(
            title="theirs", brokerage=firm, assigned_broker=self.colleague
        )
        self.my_account = BankAccount.objects.create(name="m", deal=mine)
        self.their_account = BankAccount.objects.create(name="t", deal=theirs)
        self.client = APIClient()
        self.client.force_authenticate(self.me)

    def _owner_path(self):
        return _Predicates(Transaction, [Owner("bank_account__deal__assigned_broker")])

    def test_create_onto_a_colleagues_account_is_refused(self):
        with self._owner_path():
            resp = self.client.post(
                "/api/transactions/",
                {"amount": "1.00", "bank_account": self.their_account.pk},
                format="json",
            )
            self.assertEqual(resp.status_code, 400, resp.content)
            ok = self.client.post(
                "/api/transactions/",
                {"amount": "1.00", "bank_account": self.my_account.pk},
                format="json",
            )
            self.assertEqual(ok.status_code, 201, ok.content)
        self.assertFalse(
            Transaction.objects.filter(bank_account=self.their_account).exists()
        )

    def test_moving_a_row_onto_a_colleagues_account_is_refused(self):
        row = Transaction.objects.create(amount=1, bank_account=self.my_account)
        with self._owner_path():
            resp = self.client.patch(
                f"/api/transactions/{row.pk}/",
                {"bank_account": self.their_account.pk},
                format="json",
            )
            self.assertEqual(resp.status_code, 400, resp.content)
            ok = self.client.patch(
                f"/api/transactions/{row.pk}/", {"amount": "2.00"}, format="json"
            )
            self.assertEqual(ok.status_code, 200, ok.content)
        row.refresh_from_db()
        self.assertEqual(row.bank_account_id, self.my_account.pk)


LABELS = {
    "editor": [
        "test_app.samplemodel.read",
        "test_app.samplemodel.update",
        "test_app.samplemodel.title.read",
        "test_app.samplemodel.title.write",
        "test_app.samplemodel.related.read",
        "test_app.samplemodel.related.write",
        "test_app.relatedmodel.read",
    ],
    "admin": ["test_app.relatedmodel.name.read"],
}


@override_settings(TURBODRF_ROLES=LABELS)
class TestBrowsableDropdownLabels(TestCase):
    def test_option_labels_dont_render_unreadable_columns(self):
        """The option label was str(row): RelatedModel.__str__ renders its
        name, which the editor can't read."""
        cache.clear()
        user = User.objects.create_user(username="editor")
        user._test_roles = ["editor"]
        related = RelatedModel.objects.create(name="SECRET_NAME_XYZ")
        row = SampleModel.objects.create(title="t", price=1, related=related)
        client = APIClient()
        client.force_authenticate(user)
        self.assertNotIn("name", client.get("/api/relatedmodels/").json()["data"][0])
        page = client.get(f"/api/samplemodels/{row.pk}/", HTTP_ACCEPT="text/html")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn(f'<option value="{related.pk}"', html)
        self.assertNotIn("SECRET_NAME_XYZ", html)
