"""Backstops: checks that hold even when the check meant to catch a write
first has a gap. Each test disables that first check, as a bug in it would,
and asserts the backstop still refuses the write."""

from contextlib import contextmanager
from unittest import mock

from django.test import override_settings

from tests.integration.test_audit_084 import _Base as _Base084
from tests.integration.test_review_field_rbac import RBAC_ROLES, _Base
from tests.test_app.models import Deal, RbacComment


@contextmanager
def _no_fk_checks():
    """Both checks before a write let any FK / M2M value through: the related
    fields' scoped choices, and the FK injection check."""
    from turbodrf.serializers import TurboDRFSerializer

    with (
        mock.patch(
            "turbodrf.serializers._check_fk_injection_writes", lambda *a, **k: {}
        ),
        mock.patch.object(
            TurboDRFSerializer, "_referenceable", lambda self, qs, *a, **k: qs.all()
        ),
    ):
        yield


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestReferencesStayInTheTenant(_Base):
    """Before a write commits, every row it names through an FK or M2M must be
    in the caller's tenant (or in none), judged from the rows' tenant
    columns rather than the scoped querysets the first check uses."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.manager_a)

    def test_m2m_to_another_tenants_row(self):
        comment = RbacComment.objects.create(body="c", deal=self.deal_a)
        with _no_fk_checks(), self.assertLogs("turbodrf.serializers", "ERROR"):
            resp = self.client.patch(
                f"/api/rbaccomments/{comment.pk}/",
                {"linked_deals": [self.deal_b.pk]},
                format="json",
            )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(comment.linked_deals.exists())

    def test_fk_on_create(self):
        with _no_fk_checks(), self.assertLogs("turbodrf.serializers", "ERROR"):
            resp = self.client.post(
                "/api/rbaccomments/",
                {"body": "x", "deal": self.deal_b.pk},
                format="json",
            )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertFalse(RbacComment.objects.filter(body="x").exists())

    def test_fk_to_another_tenants_user(self):
        with _no_fk_checks(), self.assertLogs("turbodrf.serializers", "ERROR"):
            resp = self.client.patch(
                f"/api/deals/{self.deal_a.pk}/",
                {"assigned_broker": self.under_b.pk},
                format="json",
            )
        self.assertEqual(resp.status_code, 400, resp.content)
        self.deal_a.refresh_from_db()
        self.assertEqual(self.deal_a.assigned_broker, self.under_a)

    def test_same_tenant_writes_pass(self):
        with _no_fk_checks():
            resp = self.client.post(
                "/api/rbaccomments/",
                {"body": "ok", "deal": self.deal_a.pk},
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(Deal.objects.filter(pk=self.deal_a.pk).exists())


class TestSandboxRowsHoldOnlyReadableColumns(_Base084):
    """A row's construction (``__init__``, a ``post_init`` handler, a change
    tracker) can load a hidden column and keep it under another name; the
    sandbox row keeps only the readable columns."""

    def test_a_post_init_copy_of_a_hidden_column(self):
        from django.db.models.signals import post_init

        from tests.integration.test_audit_085 import _patch, _patch_config
        from tests.test_app.models import RelatedModel, SampleModel

        def track(sender, instance, **kwargs):
            instance._original_secret = instance.secret_field  # loads it

        post_init.connect(track, sender=SampleModel)
        self.addCleanup(post_init.disconnect, track, sender=SampleModel)
        _patch(
            self,
            SampleModel,
            "shadow",
            property(lambda s: getattr(s, "_original_secret", None)),
            create=True,
        )
        _patch_config(self, SampleModel, fields=["id", "title", "shadow"])
        SampleModel.objects.create(
            title="t",
            price=1,
            related=RelatedModel.objects.create(name="r"),
            secret_field="TOPSECRET",
        )
        self._as(self._user("viewer", ["viewer"], self.A))
        (row,) = self._rows(self.client.get("/api/samplemodels/"))
        self.assertNotIn("secret_field", row)
        self.assertNotEqual(row.get("shadow"), "TOPSECRET")
