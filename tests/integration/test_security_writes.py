"""
Write-path security tests covering POST/PUT/PATCH/DELETE corruption and
hijacking attempts.

Surface includes FK injection, tenant escalation, mass assignment, nested
write manipulation, type confusion, PUT/PATCH semantics, bulk variations,
read-only field tampering, co-tenant write checks, race/transaction
ordering, permission corner cases, and serializer/factory internals.
"""

from decimal import Decimal
from unittest.mock import MagicMock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework import serializers as drf_serializers
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    ArticleWithCategories,
    BankAccount,
    Brokerage,
    Category,
    Deal,
    RelatedModel,
    SampleModel,
    Transaction,
)
from turbodrf.backends import (
    PermissionSnapshot,
    build_permission_snapshot_static,
    get_cache_key,
)
from turbodrf.serializers import (
    TurboDRFSerializer,
    TurboDRFSerializerFactory,
    _apply_predicate_writes,
)
from turbodrf.validation import is_field_path_sensitive

User = get_user_model()

SECRETS = ("VICTIM_SECRET_DEAL", "VICTIM_BANK_ACCOUNT", "999999.99")


def assert_no_secrets(testcase, response):
    blob = (
        str(getattr(response, "data", ""))
        + " "
        + str(getattr(response, "content", b""))
    )
    for s in SECRETS:
        testcase.assertNotIn(
            s,
            blob,
            f"Secret {s!r} leaked. status={response.status_code}",
        )


# ---------------------------------------------------------------------------
# Shared fixture base
# ---------------------------------------------------------------------------


class WriteSecurityBase(TestCase):
    """Three brokerages: attacker, victim, third (innocent witness).

    Fixtures created once via setUpTestData; per-test setUp only re-binds
    the in-memory test_user_brokerages map and the API client.
    """

    @classmethod
    def setUpTestData(cls):
        import tests.urls  # noqa: F401

        cls.brokerage_attacker = Brokerage.objects.create(name="Attacker Co")
        cls.brokerage_victim = Brokerage.objects.create(name="Victim Co")
        cls.brokerage_third = Brokerage.objects.create(name="Innocent Co")

        cls.attacker = User.objects.create_user(username="attacker", password="x")
        cls.attacker._test_roles = ["underwriter"]

        cls.attacker_manager = User.objects.create_user(username="mgr", password="x")
        cls.attacker_manager._test_roles = ["manager"]

        cls.attacker_other = User.objects.create_user(
            username="other_attacker", password="x"
        )
        cls.attacker_other._test_roles = ["underwriter"]

        cls.victim = User.objects.create_user(username="victim", password="x")
        cls.victim._test_roles = ["underwriter"]

        cls.victim_deal = Deal.objects.create(
            title="VICTIM_SECRET_DEAL",
            brokerage=cls.brokerage_victim,
            assigned_broker=cls.victim,
        )
        cls.victim_bank = BankAccount.objects.create(
            name="VICTIM_BANK_ACCOUNT", deal=cls.victim_deal
        )
        cls.victim_tx = Transaction.objects.create(
            amount=Decimal("999999.99"), bank_account=cls.victim_bank
        )

        cls.attacker_deal = Deal.objects.create(
            title="ATTACKER_DEAL",
            brokerage=cls.brokerage_attacker,
            assigned_broker=cls.attacker,
        )
        cls.attacker_bank = BankAccount.objects.create(
            name="ATTACKER_BANK", deal=cls.attacker_deal
        )

    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        # Re-populate test_user_brokerages each test (it's a process-global
        # dict cleared above).
        set_test_brokerage(self.attacker, self.brokerage_attacker)
        set_test_brokerage(self.attacker_manager, self.brokerage_attacker)
        set_test_brokerage(self.attacker_other, self.brokerage_attacker)
        set_test_brokerage(self.victim, self.brokerage_victim)

        self.client = APIClient()
        self.client.raise_request_exception = False
        self.client.force_authenticate(user=self.attacker)

    def tearDown(self):
        cache.clear()

    def _victim_unchanged(self):
        self.victim_deal.refresh_from_db()
        self.victim_bank.refresh_from_db()
        self.victim_tx.refresh_from_db()
        self.assertEqual(self.victim_deal.title, "VICTIM_SECRET_DEAL")
        self.assertEqual(self.victim_deal.brokerage_id, self.brokerage_victim.id)
        self.assertEqual(self.victim_deal.assigned_broker_id, self.victim.id)
        self.assertEqual(self.victim_bank.name, "VICTIM_BANK_ACCOUNT")
        self.assertEqual(self.victim_bank.deal_id, self.victim_deal.id)
        self.assertEqual(self.victim_tx.amount, Decimal("999999.99"))
        self.assertEqual(self.victim_tx.bank_account_id, self.victim_bank.id)


# =============================================================================
# Class A — primary write-path attack surface (HTTP layer)
# =============================================================================


class TestWritePathSurface(WriteSecurityBase):
    """IDOR / FK injection / tenant rebinding via the HTTP write surface."""

    def test_fk_injection_to_victim_resources_rejected(self):
        """FK fields on POST must not accept cross-tenant target rows.

        Covers transaction.bank_account, bankaccount.deal, deal.brokerage,
        and the combined owner+tenant injection variant.
        """
        cases = [
            (
                "/api/transactions/",
                {"bank_account": self.victim_bank.id, "amount": "1.00"},
            ),
            (
                "/api/bankaccounts/",
                {"deal": self.victim_deal.id, "name": "x"},
            ),
            (
                "/api/deals/",
                {"brokerage": self.brokerage_victim.id, "title": "x"},
            ),
            (
                "/api/deals/",
                {
                    "title": "claim-victim-tenant",
                    "brokerage": self.brokerage_victim.id,
                    "assigned_broker": self.attacker.id,
                },
            ),
        ]
        for url, payload in cases:
            with self.subTest(url=url, payload=payload):
                r = self.client.post(url, payload, format="json")
                self.assertNotIn(r.status_code, (200, 201), r.data)
        self._victim_unchanged()

    def test_idor_on_victim_resources_returns_404(self):
        """PATCH/PUT/DELETE/empty-body on victim rows all 404, victim row
        preserved. Covers victim deal, bank, transaction, including
        null-owner orphan PATCH and TOCTOU back-to-back PATCH."""
        cases = [
            ("patch", f"/api/deals/{self.victim_deal.id}/", {"title": "PWNED"}),
            ("patch", f"/api/deals/{self.victim_deal.id}/", {}),
            (
                "patch",
                f"/api/deals/{self.victim_deal.id}/",
                {"assigned_broker": None},
            ),
            ("patch", f"/api/bankaccounts/{self.victim_bank.id}/", {"name": "x"}),
            (
                "patch",
                f"/api/bankaccounts/{self.victim_bank.id}/",
                {"deal": self.attacker_deal.id, "name": "PWNED"},
            ),
            ("patch", f"/api/transactions/{self.victim_tx.id}/", {"amount": "1.00"}),
            (
                "patch",
                f"/api/transactions/{self.victim_tx.id}/",
                {"bank_account": self.attacker_bank.id, "amount": "1.00"},
            ),
            (
                "put",
                f"/api/deals/{self.victim_deal.id}/",
                {
                    "title": "PWNED",
                    "brokerage": self.brokerage_attacker.id,
                    "assigned_broker": self.attacker.id,
                },
            ),
            ("delete", f"/api/deals/{self.victim_deal.id}/", None),
            ("delete", f"/api/transactions/{self.victim_tx.id}/", None),
        ]
        for method, url, payload in cases:
            with self.subTest(method=method, url=url):
                if method == "delete":
                    r = self.client.delete(url)
                else:
                    r = getattr(self.client, method)(url, payload, format="json")
                self.assertEqual(r.status_code, 404, r.data)
        # Back-to-back PATCH (TOCTOU) — both 404
        r1 = self.client.patch(
            f"/api/deals/{self.victim_deal.id}/", {"title": "r1"}, format="json"
        )
        r2 = self.client.patch(
            f"/api/deals/{self.victim_deal.id}/", {"title": "r2"}, format="json"
        )
        self.assertEqual(r1.status_code, 404)
        self.assertEqual(r2.status_code, 404)
        self.assertTrue(Deal.objects.filter(pk=self.victim_deal.pk).exists())
        self.assertTrue(Transaction.objects.filter(pk=self.victim_tx.pk).exists())
        self._victim_unchanged()

    def test_patch_own_deal_reassign_attempts_rejected(self):
        """PATCH/PUT on attacker's own row attempting to switch the tenant
        or owner to victim must reject and leave row unchanged.

        Includes manager-bypass role (so we know the FK check, not the
        owner predicate, is the gate)."""
        # PATCH brokerage → victim
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {"brokerage": self.brokerage_victim.id},
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201), r.data)

        # PUT brokerage → victim
        r = self.client.put(
            f"/api/deals/{self.attacker_deal.id}/",
            {
                "title": "still mine",
                "brokerage": self.brokerage_victim.id,
                "assigned_broker": self.attacker.id,
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201), r.data)

        # PATCH assigned_broker → victim (as manager bypass)
        self.client.force_authenticate(user=self.attacker_manager)
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {"assigned_broker": self.victim.id},
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201), r.data)

        # PUT assigned_broker → victim (as manager bypass)
        r = self.client.put(
            f"/api/deals/{self.attacker_deal.id}/",
            {
                "title": "still mine",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.victim.id,
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201), r.data)

        self.attacker_deal.refresh_from_db()
        self.assertEqual(self.attacker_deal.brokerage_id, self.brokerage_attacker.id)
        self.assertEqual(self.attacker_deal.assigned_broker_id, self.attacker.id)
        self._victim_unchanged()

    def test_patch_own_bank_reparent_to_victim_deal_rejected(self):
        """Re-parenting attacker's bank under victim's deal would let them
        link cross-tenant infrastructure."""
        r = self.client.patch(
            f"/api/bankaccounts/{self.attacker_bank.id}/",
            {"deal": self.victim_deal.id},
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201), r.data)
        self.attacker_bank.refresh_from_db()
        self.assertEqual(self.attacker_bank.deal_id, self.attacker_deal.id)
        self._victim_unchanged()

    def test_anon_writes_forbidden(self):
        """Unauthenticated POST/DELETE → 403."""
        self.client.force_authenticate(user=None)
        r = self.client.post(
            "/api/deals/",
            {"title": "anon", "brokerage": self.brokerage_victim.id},
            format="json",
        )
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Deal.objects.filter(title="anon").exists())

        r = self.client.delete(f"/api/deals/{self.victim_deal.id}/")
        self.assertEqual(r.status_code, 403)
        self._victim_unchanged()

    def test_method_override_and_id_redirect_no_op(self):
        """X-HTTP-Method-Override header and id-in-body must not redirect
        the request to a different row or method."""
        # Method override DELETE via POST
        r = self.client.post(
            f"/api/deals/{self.victim_deal.id}/",
            {},
            format="json",
            HTTP_X_HTTP_METHOD_OVERRIDE="DELETE",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.assertTrue(Deal.objects.filter(pk=self.victim_deal.pk).exists())

        # PATCH own deal with id pointing to victim id
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {"id": self.victim_deal.id, "title": "redirect_attempt"},
            format="json",
        )
        # Whatever the status, the victim's row must not have been touched
        self._victim_unchanged()

    def test_bulk_array_post_rejected_no_partial_writes(self):
        """JSON array POST body — must reject (or 500 in current code)
        without persisting any rows on either tenant."""
        before_attacker = Deal.objects.filter(
            brokerage=self.brokerage_attacker
        ).count()
        before_victim = Deal.objects.filter(
            brokerage=self.brokerage_victim
        ).count()
        r = self.client.post(
            "/api/deals/",
            [
                {"title": "bulk1", "brokerage": self.brokerage_attacker.id},
                {"title": "bulk2", "brokerage": self.brokerage_victim.id},
            ],
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.assertEqual(
            Deal.objects.filter(brokerage=self.brokerage_attacker).count(),
            before_attacker,
        )
        self.assertEqual(
            Deal.objects.filter(brokerage=self.brokerage_victim).count(),
            before_victim,
        )
        self._victim_unchanged()

    def test_extra_unrecognized_fields_silent(self):
        """Extra unrecognized fields on a POST must be silently ignored —
        no mass assignment of attacker-controlled garbage."""
        r = self.client.post(
            "/api/deals/",
            {
                "title": "extra",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "is_admin": True,
                "secret_field": "ohno",
                "deleted": False,
                "owner_override": self.victim.id,
            },
            format="json",
        )
        self.assertNotEqual(r.status_code, 500)
        if r.status_code == 201:
            d = Deal.objects.get(title="extra")
            self.assertEqual(d.brokerage_id, self.brokerage_attacker.id)
            self.assertEqual(d.assigned_broker_id, self.attacker.id)
        self._victim_unchanged()

    def test_form_and_text_content_types_safe(self):
        """form-urlencoded and text/plain bodies must not bypass tenant
        gating, must not 500."""
        r = self.client.post(
            "/api/deals/",
            data={
                "title": "form-attack",
                "brokerage": self.brokerage_victim.id,
            },
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.assertFalse(
            Deal.objects.filter(
                title="form-attack", brokerage=self.brokerage_victim
            ).exists()
        )

        r = self.client.post(
            "/api/deals/",
            data='{"title": "raw", "brokerage": ' + str(self.brokerage_victim.id) + "}",
            content_type="text/plain",
        )
        self.assertNotEqual(r.status_code, 500)
        self.assertNotIn(r.status_code, (200, 201))
        self._victim_unchanged()

    def test_negative_or_zero_or_existing_ids_rejected(self):
        """Negative/zero/existing-victim ids in FK or pk fields must not
        produce upserts or accept the invalid pk."""
        cases = [
            ("/api/transactions/", {"bank_account": 0, "amount": "1.00"}),
            ("/api/transactions/", {"bank_account": -1, "amount": "1.00"}),
            ("/api/deals/", {"title": "x", "brokerage": -42}),
        ]
        for url, payload in cases:
            with self.subTest(url=url, payload=payload):
                r = self.client.post(url, payload, format="json")
                self.assertNotIn(r.status_code, (200, 201), r.data)

        # POST with id=victim_tx — must not upsert/overwrite
        original_amount = self.victim_tx.amount
        r = self.client.post(
            "/api/transactions/",
            {
                "bank_account": self.victim_bank.id,
                "id": self.victim_tx.id,
                "amount": "-99999.00",
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201), r.data)
        self.victim_tx.refresh_from_db()
        self.assertEqual(self.victim_tx.amount, original_amount)
        self._victim_unchanged()


# =============================================================================
# Class B — payload shape variations / mass assignment / type confusion
# =============================================================================


class TestPayloadShapes(WriteSecurityBase):
    """Mass-assignment, type-confusion, nested-write, envelope and
    PUT/PATCH/DELETE shape variations."""

    def test_mass_assignment_payloads_dont_persist(self):
        """All mass-assignment vectors: read-only fields, dunder/internal
        attrs, auth fields, alternate tenant keys, ORM lookup injection,
        unicode tricks, long titles, str types — none should mutate
        target rows or 500."""
        payloads = [
            # Read-only & timestamp tampering
            {
                "title": "ro-fields",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "id": 999999,
                "pk": 888888,
                "created_at": "2000-01-01T00:00:00Z",
                "updated_at": "2000-01-01T00:00:00Z",
            },
            # pk alias targeting victim
            {
                "title": "pk-alias",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "pk": self.victim_deal.id,
            },
            # Python dunder / class attrs
            {
                "title": "py-attrs",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "_meta": {"db_table": "junk"},
                "__class__": "Brokerage",
                "objects": ["x"],
                "__str__": "PWNED",
                "save": "PWNED",
                "delete": "PWNED",
            },
            # Auth fields on non-User endpoint
            {
                "title": "auth-fields",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
            # Class/turbodrf attrs
            {
                "title": "class-attr",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "searchable_fields": ["__sql_injection"],
                "turbodrf": {"public_access": True},
            },
            # Serializer internals
            {
                "title": "serializer-internal",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "_permission_snapshot": {
                    "allowed_actions": ["delete"],
                    "readable_fields": ["*"],
                },
                "Meta": {"model": "Brokerage"},
            },
            # Alternate / fake tenant keys
            {
                "title": "alt-tenant",
                "brokerage": self.brokerage_attacker.id,
                "brokerage_id": self.brokerage_victim.id,
                "_test_brokerage": self.brokerage_victim.id,
                "tenant": self.brokerage_victim.id,
                "assigned_broker": self.attacker.id,
            },
            # ORM lookup-style keys
            {
                "title": "orm-lookup",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "brokerage__name__icontains": "Victim",
                "assigned_broker__id__in": [self.victim.id],
            },
            # Negative pk
            {
                "title": "neg-pk",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
                "pk": -42,
            },
            # id collision with victim
            {
                "id": self.victim_deal.id,
                "title": "id-collide",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
            },
            # String brokerage id
            {
                "title": "str-brokerage",
                "brokerage": str(self.brokerage_attacker.id),
                "assigned_broker": self.attacker.id,
            },
            # Unicode tricks (RTL / control chars)
            {
                "title": "‮�\b\x7f",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
            },
        ]
        for payload in payloads:
            with self.subTest(payload=list(payload.keys())):
                r = self.client.post("/api/deals/", payload, format="json")
                self.assertNotEqual(r.status_code, 500, getattr(r, "data", None))
                if r.status_code == 201:
                    d = Deal.objects.get(title=payload["title"])
                    self.assertEqual(
                        d.brokerage_id, self.brokerage_attacker.id
                    )
                    # Read-only fields can't be honored
                    self.assertNotEqual(d.id, 999999)
                    self.assertNotEqual(d.id, 888888)
                    self.assertNotEqual(d.id, self.victim_deal.id)
                    self.assertGreater(d.id, 0)

        # Auth fields didn't leak onto the user
        self.attacker.refresh_from_db()
        self.assertFalse(self.attacker.is_staff)
        self.assertFalse(self.attacker.is_superuser)

        # Class attrs unchanged
        self.assertEqual(Deal.turbodrf()["tenant_field"], "brokerage")
        self._victim_unchanged()

    def test_long_title_and_invalid_field_shapes_rejected(self):
        """5MB title, 10000-char title, list-as-title, NoSQL operator
        object-as-title, all-nulls — clean rejection (400), no 500."""
        payloads = [
            {
                "title": "A" * 10000,
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
            },
            {
                "title": ["array-1", "array-2"],
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
            },
            {
                "title": {"$ne": None},
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
            },
            {"title": None, "brokerage": None, "assigned_broker": None},
        ]
        for payload in payloads:
            with self.subTest(title=str(payload.get("title"))[:30]):
                r = self.client.post("/api/deals/", payload, format="json")
                self.assertNotEqual(r.status_code, 500, getattr(r, "data", None))
                self.assertNotIn(r.status_code, (200, 201))
        self._victim_unchanged()

    def test_fk_type_confusion(self):
        """Non-integer FK shapes (float, bool, list, dict, URL string,
        SQLi, scientific overflow/underflow, deeply nested dict) on
        transaction.bank_account / deal.brokerage — must reject without
        crashing or persisting cross-tenant rows."""
        deal_payloads = [
            ("brokerage", f"{self.brokerage_attacker.id} OR 1=1"),  # SQLi-ish
            (
                "brokerage",
                {
                    "id": self.brokerage_attacker.id,
                    "name": "PWNED",
                    "deals": [
                        {"id": self.victim_deal.id, "title": "OWNED"}
                    ],
                },
            ),
        ]
        for field, value in deal_payloads:
            with self.subTest(deal_field=field):
                payload = {
                    "title": "type-conf",
                    field: value,
                    "assigned_broker": self.attacker.id,
                }
                if field != "brokerage":
                    payload["brokerage"] = self.brokerage_attacker.id
                r = self.client.post("/api/deals/", payload, format="json")
                self.assertNotEqual(r.status_code, 500, getattr(r, "data", None))
                self.assertNotIn(r.status_code, (200, 201))

        tx_payloads = [
            {"amount": "1.00", "bank_account": 1.5},
            {"amount": "1.00", "bank_account": True},
            {"amount": "1.00", "bank_account": [1, 2, 3]},
            {"amount": "1.00", "bank_account": {"random": "junk"}},
            {"amount": "1.00", "bank_account": {"id": self.victim_bank.id}},
            {
                "amount": "1.00",
                "bank_account": f"/api/bankaccounts/{self.victim_bank.id}/",
            },
            {
                "amount": "1.00",
                "bank_account": {
                    "id": self.victim_bank.id,
                    "name": "OWNED",
                    "deal": self.victim_deal.id,
                },
            },
            {
                "amount": "3.00",
                "bank_account": [self.attacker_bank.id, self.victim_bank.id],
            },
        ]
        for payload in tx_payloads:
            with self.subTest(tx_payload=str(payload["bank_account"])[:30]):
                r = self.client.post(
                    "/api/transactions/", payload, format="json"
                )
                self.assertNotEqual(r.status_code, 500, getattr(r, "data", None))
                self.assertNotIn(r.status_code, (200, 201))

        # Scientific-notation amounts (over- and underflow) — handled
        for amt in ("1e308", "1e-308"):
            with self.subTest(amount=amt):
                r = self.client.post(
                    "/api/transactions/",
                    {"amount": amt, "bank_account": self.attacker_bank.id},
                    format="json",
                )
                self.assertNotEqual(r.status_code, 500, getattr(r, "data", None))

        self._victim_unchanged()

    def test_inline_dict_does_not_mutate_targets(self):
        """Inline-dict payloads on FK fields must never side-effect-write
        through the dict (no patching name/title/username via nested write)."""
        # Transaction.bank_account inline dict — must not change victim_bank
        r = self.client.post(
            "/api/transactions/",
            {
                "amount": "10.00",
                "bank_account": {
                    "id": self.victim_bank.id,
                    "name": "PWNED",
                },
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.victim_bank.refresh_from_db()
        self.assertEqual(self.victim_bank.name, "VICTIM_BANK_ACCOUNT")

        # Deal.brokerage inline dict
        r = self.client.post(
            "/api/deals/",
            {
                "title": "bk-mutate",
                "brokerage": {
                    "id": self.brokerage_victim.id,
                    "name": "PWNED",
                },
                "assigned_broker": self.attacker.id,
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.brokerage_victim.refresh_from_db()
        self.assertEqual(self.brokerage_victim.name, "Victim Co")

        # Deal.assigned_broker inline dict trying to rename victim user
        r = self.client.post(
            "/api/deals/",
            {
                "title": "user-mutate",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": {
                    "id": self.victim.id,
                    "username": "EVIL",
                },
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.victim.refresh_from_db()
        self.assertEqual(self.victim.username, "victim")

        # PATCH inline dict with privilege escalation fields
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {
                "assigned_broker": {
                    "id": self.victim.id,
                    "is_superuser": True,
                    "is_staff": True,
                },
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.victim.refresh_from_db()
        self.assertFalse(self.victim.is_superuser)
        self.assertFalse(self.victim.is_staff)

        # BankAccount with inline deal dict pointing at victim
        r = self.client.post(
            "/api/bankaccounts/",
            {
                "name": "deal-inline",
                "deal": {
                    "id": self.victim_deal.id,
                    "title": "PWNED",
                },
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self._victim_unchanged()

    def test_patch_modes_dont_500(self):
        """PATCH null body, PATCH array body, empty PATCH on own deal —
        none should 500 and none should open visibility of victim rows."""
        # null body
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            "null",
            content_type="application/json",
        )
        self.assertNotEqual(r.status_code, 500)

        # array body
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            [{"title": "arr"}],
            format="json",
        )
        self.assertNotEqual(r.status_code, 500)
        self.assertNotIn(r.status_code, (200, 201))

        # empty body on own
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/", {}, format="json"
        )
        self.assertNotEqual(r.status_code, 500)
        self._victim_unchanged()

    def test_put_semantics(self):
        """PUT with all valid fields, omitted brokerage, minimal body —
        attacker's tenant must never be nullified or replaced."""
        payloads = [
            {
                "title": "still mine",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
            },
            # omit brokerage — required field
            {
                "title": "no-brokerage-put",
                "assigned_broker": self.attacker.id,
            },
            # minimal body
            {"title": "minimal"},
        ]
        for payload in payloads:
            with self.subTest(keys=list(payload.keys())):
                r = self.client.put(
                    f"/api/deals/{self.attacker_deal.id}/",
                    payload,
                    format="json",
                )
                self.assertNotEqual(r.status_code, 500, getattr(r, "data", None))
                self.attacker_deal.refresh_from_db()
                # Tenant must NOT change (auto-fill or 400, never null)
                self.assertEqual(
                    self.attacker_deal.brokerage_id, self.brokerage_attacker.id
                )
        self._victim_unchanged()

    def test_envelopes_and_collection_methods_safe(self):
        """`{data: [...]}`, `{objects: [...]}`, `?__many=true`, valid array
        body, PATCH on collection, DELETE with body — none persist
        cross-tenant rows or 500."""
        envelope_payloads = [
            {
                "data": [
                    {"title": "envelope", "brokerage": self.brokerage_victim.id}
                ]
            },
            {
                "objects": [
                    {"title": "obj-env", "brokerage": self.brokerage_victim.id}
                ]
            },
        ]
        for payload in envelope_payloads:
            with self.subTest(envelope=list(payload.keys())[0]):
                r = self.client.post("/api/deals/", payload, format="json")
                self.assertNotEqual(r.status_code, 500, getattr(r, "data", None))

        r = self.client.post(
            "/api/deals/?__many=true",
            {"title": "many-flag", "brokerage": self.brokerage_victim.id},
            format="json",
        )
        self.assertNotEqual(r.status_code, 500)

        # Array of all-valid objects on POST — must not bulk-insert
        before = Deal.objects.count()
        r = self.client.post(
            "/api/deals/",
            [
                {
                    "title": "all-valid-1",
                    "brokerage": self.brokerage_attacker.id,
                    "assigned_broker": self.attacker.id,
                },
                {
                    "title": "all-valid-2",
                    "brokerage": self.brokerage_attacker.id,
                    "assigned_broker": self.attacker.id,
                },
            ],
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.assertEqual(Deal.objects.count(), before)

        # PATCH on collection
        r = self.client.patch(
            "/api/deals/",
            [{"id": self.victim_deal.id, "title": "PWNED"}],
            format="json",
        )
        self.assertNotEqual(r.status_code, 500)
        self.assertNotIn(r.status_code, (200, 201))

        # DELETE collection with ids body
        r = self.client.delete(
            "/api/deals/",
            data={"ids": [self.victim_deal.id, self.attacker_deal.id]},
            content_type="application/json",
        )
        self.assertNotEqual(r.status_code, 500)
        self.assertTrue(Deal.objects.filter(pk=self.victim_deal.pk).exists())

        # No leaks anywhere
        for title in ("envelope", "obj-env", "many-flag"):
            self.assertFalse(
                Deal.objects.filter(
                    title=title, brokerage=self.brokerage_victim
                ).exists()
            )
        self._victim_unchanged()

    def test_amount_validation(self):
        """Decimal field bounds: extreme negative, exceeds max_digits,
        scientific overflow — handled with 4xx, never 500."""
        cases = [
            "-99999999.99",
            "999999999999999.99",
        ]
        for amt in cases:
            with self.subTest(amount=amt):
                r = self.client.post(
                    "/api/transactions/",
                    {"amount": amt, "bank_account": self.attacker_bank.id},
                    format="json",
                )
                self.assertNotEqual(r.status_code, 500, getattr(r, "data", None))
        self._victim_unchanged()

    def test_co_tenant_assignment_outcomes(self):
        """Manager bypass-role + various owner/tenant combinations.

        - cotenant same brokerage user assignment may succeed
        - victim user assignment never succeeds
        - underwriter null-owner doesn't create cross-tenant rows
        - role-injection via body never escalates
        - no-role user can't write
        - stale snapshot after role mutation still respects tenant."""
        # 1. manager assigns co-tenant other underwriter → may succeed
        self.client.force_authenticate(user=self.attacker_manager)
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {"assigned_broker": self.attacker_other.id},
            format="json",
        )
        self.assertNotEqual(r.status_code, 500)

        # 2. manager POST with victim user → reject
        r = self.client.post(
            "/api/deals/",
            {
                "title": "mgr-assign-victim",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.victim.id,
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.assertFalse(
            Deal.objects.filter(
                title="mgr-assign-victim", assigned_broker=self.victim
            ).exists()
        )

        # 3. underwriter POST with null owner → no cross-tenant write
        self.client.force_authenticate(user=self.attacker)
        r = self.client.post(
            "/api/deals/",
            {
                "title": "null-owner",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": None,
            },
            format="json",
        )
        self.assertNotEqual(r.status_code, 500)
        self.assertFalse(
            Deal.objects.filter(
                title="null-owner", brokerage=self.brokerage_victim
            ).exists()
        )

        # 4. underwriter claims manager via body — no escalation
        r = self.client.post(
            "/api/deals/",
            {
                "title": "claim-mgr",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.victim.id,
                "roles": ["manager"],
                "_test_roles": ["manager"],
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))

        # 5. user with no roles → forbidden
        no_role = User.objects.create_user(username="norole", password="x")
        no_role._test_roles = []
        set_test_brokerage(no_role, self.brokerage_attacker)
        self.client.force_authenticate(user=no_role)
        r = self.client.post(
            "/api/deals/",
            {"title": "no-role", "brokerage": self.brokerage_attacker.id},
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.assertNotEqual(r.status_code, 500)
        self.assertFalse(Deal.objects.filter(title="no-role").exists())

        # 6. stale snapshot after role mutation — still tenant-bound
        self.client.force_authenticate(user=self.attacker)
        self.client.get("/api/deals/")  # warm cache
        self.attacker._test_roles = ["manager"]
        r = self.client.post(
            "/api/deals/",
            {
                "title": "stale-snap",
                "brokerage": self.brokerage_victim.id,
                "assigned_broker": self.attacker.id,
            },
            format="json",
        )
        self.assertNotIn(r.status_code, (200, 201))
        self.assertFalse(
            Deal.objects.filter(
                title="stale-snap", brokerage=self.brokerage_victim
            ).exists()
        )
        self._victim_unchanged()

    def test_nonexistent_fk_unified_error_message(self):
        """Nonexistent FK target produces the same error message as a
        cross-tenant target — prevents existence enumeration."""
        cases = [
            (
                "/api/deals/",
                {
                    "title": "ghost-user",
                    "brokerage": self.brokerage_attacker.id,
                    "assigned_broker": 999999,
                },
            ),
            (
                "/api/transactions/",
                {"amount": "1.00", "bank_account": 999999},
            ),
        ]
        for url, payload in cases:
            with self.subTest(url=url):
                r = self.client.post(url, payload, format="json")
                self.assertNotIn(r.status_code, (200, 201), r.data)
                body = str(getattr(r, "data", ""))
                self.assertIn("not found or not accessible", body)
        self._victim_unchanged()

    def test_race_and_sequence_safe(self):
        """Two PATCHes back-to-back, POST→PATCH on new row, DELETE→PATCH,
        DELETE→POST same id — none cause cross-tenant leaks or 500."""
        # 1. Two PATCHes back to back on own deal
        r1 = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {"title": "r1"},
            format="json",
        )
        r2 = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {"title": "r2"},
            format="json",
        )
        self.assertNotEqual(r1.status_code, 500)
        self.assertNotEqual(r2.status_code, 500)

        # 2. POST then PATCH new row to victim brokerage
        r = self.client.post(
            "/api/deals/",
            {
                "title": "fresh",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
            },
            format="json",
        )
        if r.status_code == 201:
            new_id = (
                r.data.get("id") if isinstance(r.data, dict) else None
            ) or Deal.objects.get(title="fresh").id
            r2 = self.client.patch(
                f"/api/deals/{new_id}/",
                {"brokerage": self.brokerage_victim.id},
                format="json",
            )
            self.assertNotIn(r2.status_code, (200, 201))
            d = Deal.objects.get(pk=new_id)
            self.assertEqual(d.brokerage_id, self.brokerage_attacker.id)

        # 3. DELETE then PATCH same row → 404
        attacker_tx = Transaction.objects.create(
            amount=Decimal("5.00"), bank_account=self.attacker_bank
        )
        r1 = self.client.delete(f"/api/transactions/{attacker_tx.id}/")
        self.assertNotEqual(r1.status_code, 500)
        r2 = self.client.patch(
            f"/api/transactions/{attacker_tx.id}/",
            {"amount": "1.00"},
            format="json",
        )
        self.assertEqual(r2.status_code, 404)

        # 4. DELETE own then POST with same id (id is read-only)
        old_id = self.attacker_deal.id
        r1 = self.client.delete(f"/api/deals/{old_id}/")
        self.assertNotEqual(r1.status_code, 500)
        r2 = self.client.post(
            "/api/deals/",
            {
                "id": old_id,
                "title": "re-create",
                "brokerage": self.brokerage_attacker.id,
                "assigned_broker": self.attacker.id,
            },
            format="json",
        )
        self.assertNotEqual(r2.status_code, 500)
        self._victim_unchanged()

    def test_delete_own_deal_cascades_cleanly(self):
        """DELETE attacker's own deal cascades children without touching
        victim chain."""
        Transaction.objects.create(
            amount=Decimal("7.00"), bank_account=self.attacker_bank
        )
        before_victim_bank = BankAccount.objects.filter(
            deal__brokerage=self.brokerage_victim
        ).count()
        r = self.client.delete(f"/api/deals/{self.attacker_deal.id}/")
        self.assertNotEqual(r.status_code, 500)
        self.assertEqual(
            BankAccount.objects.filter(
                deal__brokerage=self.brokerage_victim
            ).count(),
            before_victim_bank,
        )
        self._victim_unchanged()

    def test_post_explicit_null_brokerage_autofills(self):
        """Explicit null brokerage must auto-fill to attacker's brokerage,
        never end up assigned to victim's tenant."""
        r = self.client.post(
            "/api/deals/",
            {
                "title": "null-brokerage",
                "brokerage": None,
                "assigned_broker": self.attacker.id,
            },
            format="json",
        )
        self.assertNotEqual(r.status_code, 500)
        if r.status_code == 201:
            d = Deal.objects.get(title="null-brokerage")
            self.assertEqual(d.brokerage_id, self.brokerage_attacker.id)
        self.assertFalse(
            Deal.objects.filter(
                title="null-brokerage", brokerage=self.brokerage_victim
            ).exists()
        )
        self._victim_unchanged()

    def test_patch_unknown_fk_fields_safe(self):
        """PATCH with unknown FK-shaped fields — no crash, no mutation."""
        r = self.client.patch(
            f"/api/deals/{self.attacker_deal.id}/",
            {
                "owner": self.victim.id,
                "tenant_id": self.brokerage_victim.id,
                "broker_id": self.victim.id,
            },
            format="json",
        )
        self.assertNotEqual(r.status_code, 500)
        self.attacker_deal.refresh_from_db()
        self.assertEqual(self.attacker_deal.brokerage_id, self.brokerage_attacker.id)
        self.assertEqual(self.attacker_deal.assigned_broker_id, self.attacker.id)
        self._victim_unchanged()


# =============================================================================
# Class C — serializer & permission internals
# =============================================================================


class TestSerializerInternals(TestCase):
    """Probes for snapshot construction, factory edge cases, sensitive
    field denylist, M2M rendering, predicate writes, etc."""

    @classmethod
    def setUpTestData(cls):
        import tests.urls  # noqa: F401

        cls.brokerage_attacker = Brokerage.objects.create(name="Attacker Co")
        cls.brokerage_victim = Brokerage.objects.create(name="Victim Co")

        cls.attacker = User.objects.create_user(username="attacker", password="x")
        cls.attacker._test_roles = ["underwriter"]

        cls.attacker_manager = User.objects.create_user(username="mgr", password="x")
        cls.attacker_manager._test_roles = ["manager"]

        cls.victim = User.objects.create_user(username="victim", password="x")
        cls.victim._test_roles = ["underwriter"]

        cls.victim_deal = Deal.objects.create(
            title="VICTIM_SECRET_DEAL",
            brokerage=cls.brokerage_victim,
            assigned_broker=cls.victim,
        )
        cls.victim_bank = BankAccount.objects.create(
            name="VICTIM_BANK_ACCOUNT", deal=cls.victim_deal
        )
        cls.victim_tx = Transaction.objects.create(
            amount=Decimal("999999.99"), bank_account=cls.victim_bank
        )

        cls.attacker_deal = Deal.objects.create(
            title="ATTACKER_DEAL",
            brokerage=cls.brokerage_attacker,
            assigned_broker=cls.attacker,
        )

    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        set_test_brokerage(self.attacker, self.brokerage_attacker)
        set_test_brokerage(self.attacker_manager, self.brokerage_attacker)
        set_test_brokerage(self.victim, self.brokerage_victim)

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker)

    def tearDown(self):
        cache.clear()

    # ----- Helpers --------------------------------------------------------

    def _build_serializer(self, model, fields, user, view_type="detail"):
        return TurboDRFSerializerFactory.create_serializer(
            model=model, fields=fields, user=user, view_type=view_type
        )

    def _make_admin_user(self, name):
        u = User.objects.create_user(username=name, password="x")
        u._test_roles = ["admin"]
        return u

    # ----- Snapshot builder ----------------------------------------------

    def test_snapshot_fail_closed_on_broken_models(self):
        """Mock model with no fields / broken _meta → snapshot must
        either return an empty PermissionSnapshot or raise — never grant."""
        # No fields
        m1 = MagicMock()
        m1._meta.app_label = "fakeapp"
        m1._meta.model_name = "fake"
        m1._meta.fields = []
        m1._meta.many_to_many = []
        try:
            snap = build_permission_snapshot_static(self.attacker, m1)
            self.assertEqual(snap.readable_fields, set())
            self.assertEqual(snap.writable_fields, set())
        except Exception:
            pass

        # Broken _meta
        m2 = MagicMock()
        type(m2)._meta = property(
            lambda self: (_ for _ in ()).throw(AttributeError("no _meta"))
        )
        try:
            snap = build_permission_snapshot_static(self.attacker, m2)
            self.assertIsInstance(snap, PermissionSnapshot)
        except (AttributeError, TypeError):
            pass

    def test_snapshot_grants_nothing_for_empty_or_unmapped_role(self):
        """Empty TURBODRF_ROLES, role string with no entry, AnonymousUser,
        and User.roles=None all → zero permissions."""
        from django.contrib.auth.models import AnonymousUser

        # Empty roles config
        with override_settings(TURBODRF_ROLES={}):
            cache.clear()
            snap = build_permission_snapshot_static(self.attacker, Deal)
            self.assertEqual(snap.allowed_actions, set())
            self.assertEqual(snap.readable_fields, set())
            self.assertEqual(snap.writable_fields, set())

        # Unmapped role
        u = User.objects.create_user(username="ghost", password="x")
        u._test_roles = ["nonexistent_role"]
        snap = build_permission_snapshot_static(u, Deal)
        self.assertEqual(snap.allowed_actions, set())
        self.assertEqual(snap.readable_fields, set())

        # User with empty roles
        u2 = User.objects.create_user(username="noroles", password="x")
        u2._test_roles = []
        snap = build_permission_snapshot_static(u2, Deal)
        self.assertEqual(snap.allowed_actions, set())

        # Anonymous
        snap = build_permission_snapshot_static(AnonymousUser(), Deal)
        self.assertEqual(snap.allowed_actions, set())

    def test_snapshot_dual_rule_field_and_m2m_and_property(self):
        """Field with both read and write rules has both flags; M2M
        fields are treated as model fields; properties don't collide
        with real fields."""
        from tests.test_app.models import CompiledSampleModel

        admin = self._make_admin_user("adm_dual")
        snap = build_permission_snapshot_static(admin, SampleModel)
        self.assertTrue(snap.has_read_rule("title"))
        self.assertTrue(snap.has_write_rule("title"))

        snap_art = build_permission_snapshot_static(admin, ArticleWithCategories)
        self.assertIn("categories", snap_art.readable_fields)

        snap_cs = build_permission_snapshot_static(admin, CompiledSampleModel)
        self.assertNotIn("display_title", snap_cs.readable_fields)

    def test_perm_string_parse_edge_cases(self):
        """Malformed permission strings (special chars, wrong number of
        parts, nonexistent fields) must not match real fields."""
        configs = [
            {
                "weird": [
                    "test_app.deal.title-with-dash.read",
                    "test_app.deal.title.with.dots.read",
                    "test_app.deal.title with space.read",
                ]
            },
            {"x": ["test_app.deal.title.read.extra"]},
            {"x": ["test_app.deal.title"]},
            {"x": ["test_app.deal.fake_field.read"]},
        ]
        for i, cfg in enumerate(configs):
            with self.subTest(cfg_index=i):
                with override_settings(TURBODRF_ROLES=cfg):
                    cache.clear()
                    role = list(cfg.keys())[0]
                    u = User.objects.create_user(
                        username=f"perm_{i}", password="x"
                    )
                    u._test_roles = [role]
                    snap = build_permission_snapshot_static(u, Deal)
                    self.assertNotIn("title", snap.readable_fields)
                    self.assertNotIn("fake_field", snap.readable_fields)

    def test_field_rule_global_scan_and_role_union(self):
        """A field rule (in any role) marks the field globally as having a
        read rule. A role w/o the rule still doesn't see the field. Two
        roles with conflicting rules give union of perms."""
        viewer = User.objects.create_user(username="vw", password="x")
        viewer._test_roles = ["viewer"]
        snap = build_permission_snapshot_static(viewer, SampleModel)
        self.assertIn("title", snap.fields_with_read_rules)
        # Viewer doesn't have rule on secret_field → not readable
        self.assertNotIn("secret_field", snap.readable_fields)

        # Two roles → union
        u = User.objects.create_user(username="dual", password="x")
        u._test_roles = ["admin", "viewer"]
        snap = build_permission_snapshot_static(u, SampleModel)
        self.assertIn("title", snap.readable_fields)

    # ----- to_representation ---------------------------------------------

    def test_to_representation_no_leak_paths(self):
        """Extra dict attrs, broken nested paths, None FK traversal,
        property exceptions — none crash or leak SECRETS."""
        from tests.test_app.models import CompiledSampleModel

        # Extra dict attr
        SerCls = self._build_serializer(
            Deal, ["id", "title"], self.attacker_manager
        )
        deal = self.attacker_deal
        deal._victim_payload = "VICTIM_SECRET_DEAL"
        out = SerCls(deal, context={"request": None}).data
        self.assertNotIn("_victim_payload", out)
        for s in SECRETS:
            self.assertNotIn(s, str(out))

        # Bad nested path
        class BadSer(TurboDRFSerializer):
            class Meta:
                model = Deal
                fields = ["id", "title"]
                _nested_fields = {"nonexistent": ["nonexistent__nope"]}
                ref_name = "BadSerC2"

        out = BadSer(self.attacker_deal, context={"request": None}).data
        for s in SECRETS:
            self.assertNotIn(s, str(out))

        # None FK traversal
        d = Deal.objects.create(
            title="ATTACKER_NULL",
            brokerage=self.brokerage_attacker,
            assigned_broker=None,
        )
        SerCls = self._build_serializer(
            Deal,
            ["id", "title", "assigned_broker__username"],
            self.attacker_manager,
        )
        out = SerCls(d, context={"request": None}).data
        for s in SECRETS:
            self.assertNotIn(s, str(out))

        # Property that raises (related is None)
        rel = RelatedModel.objects.create(name="rel1")
        cs = CompiledSampleModel.objects.create(
            title="cs1", price=Decimal("1.00"), is_active=True, related=rel
        )
        cs.related = None
        SerCls = self._build_serializer(
            CompiledSampleModel,
            ["id", "title", "related__name"],
            self._make_admin_user("cs_adm"),
            view_type="detail",
        )
        out = SerCls(cs, context={"request": None}).data
        for s in SECRETS:
            self.assertNotIn(s, str(out))

    def test_m2m_serialization_paths(self):
        """M2M render paths: empty M2M, declared-only field extraction,
        anon user, no-request, no-user, missing field, no predicates."""
        from django.http import HttpRequest

        # Empty M2M
        art_empty = ArticleWithCategories.objects.create(title="empty article")
        SerCls = self._build_serializer(
            ArticleWithCategories,
            ["id", "title", "categories__name"],
            self._make_admin_user("art_admin1"),
            view_type="detail",
        )
        out = SerCls(art_empty, context={"request": None}).data
        self.assertEqual(out["categories"], [])

        # Only declared nested fields extracted
        cat = Category.objects.create(name="Cat1", description="DESC_DETAILS")
        art = ArticleWithCategories.objects.create(title="art with cat")
        art.categories.add(cat)
        SerCls = self._build_serializer(
            ArticleWithCategories,
            ["id", "title", "categories__name"],
            self._make_admin_user("art_admin2"),
            view_type="detail",
        )
        out = SerCls(art, context={"request": None}).data
        cat_data = out["categories"][0]
        self.assertIn("name", cat_data)
        self.assertNotIn("description", cat_data)

        # Anon request
        req = HttpRequest()
        req.user = MagicMock(is_authenticated=False)
        out = SerCls(art, context={"request": req}).data
        for s in SECRETS:
            self.assertNotIn(s, str(out))

        # Empty context (no request) — uses raw .all()
        out = SerCls(art, context={}).data
        self.assertEqual(out["categories"][0]["name"], "Cat1")

        # Request without user attr
        req2 = HttpRequest()
        out = SerCls(art, context={"request": req2}).data
        # Should not crash

        # Missing M2M field on model
        class ToySer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["id", "title"]
                _nested_fields = {"missing_m2m": ["missing_m2m__name"]}
                ref_name = "ToySerH4"

        art2 = ArticleWithCategories.objects.create(title="h4")
        try:
            _ = ToySer(art2, context={"request": None}).data
        except Exception as exc:
            self.fail(f"M2M missing field crashed: {exc!r}")

    # ----- to_internal_value ---------------------------------------------

    def test_to_internal_value_validation(self):
        """unicode RTL, 5MB string, None for required, missing FK,
        nonexistent FK rewrite, non-dict detail fall-through."""
        # Unicode tricks
        resp = self.client.post(
            "/api/deals/",
            {
                "title": "Ünicödé Tëst — ‮RTL",
                "brokerage": self.brokerage_attacker.pk,
            },
            format="json",
        )
        self.assertIn(resp.status_code, (200, 201, 400))
        assert_no_secrets(self, resp)

        # Long string (max_length=200)
        resp = self.client.post(
            "/api/deals/",
            {"title": "A" * 5_000_000, "brokerage": self.brokerage_attacker.pk},
            format="json",
        )
        self.assertNotEqual(resp.status_code, 500)
        assert_no_secrets(self, resp)

        # None for required field
        resp = self.client.post(
            "/api/deals/",
            {"title": "x", "brokerage": None},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

        # Nonexistent FK gets unified rewritten message
        resp = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": 999999},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("not found or not accessible", str(resp.data))

        # Required FK missing → 400, not 500
        resp = self.client.post(
            "/api/transactions/",
            {"amount": "1.00"},
            format="json",
        )
        self.assertNotEqual(resp.status_code, 500)

        # Non-dict detail fall-through
        class ToyDeal(TurboDRFSerializer):
            class Meta:
                model = Deal
                fields = ["title", "brokerage"]
                ref_name = "ToyDealD5"

        s = ToyDeal()
        try:
            s.to_internal_value(["not a dict"])
        except DRFValidationError as exc:
            self.assertIsNotNone(exc.detail)
        except (TypeError, AttributeError):
            self.fail("rewriter crashed on non-dict detail")

    # ----- Factory + dynamic class isolation -----------------------------

    def test_factory_edge_cases_and_password_strip(self):
        """Empty fields, '__all__', no-perm user, User-model password
        strip, '__all__'+no-perms→empty, no-writable→all-readonly,
        password-anywhere strip, depth limits, view_type, duplicates,
        empty-string field."""
        # Empty fields
        SerCls = TurboDRFSerializerFactory.create_serializer(
            model=Deal, fields=[], user=self.attacker
        )
        out = SerCls(self.attacker_deal, context={"request": None}).data
        for s in SECRETS:
            self.assertNotIn(s, str(out))

        # '__all__'
        SerCls = TurboDRFSerializerFactory.create_serializer(
            model=Deal, fields="__all__", user=self.attacker
        )
        out = SerCls(self.attacker_deal, context={"request": None}).data
        self.assertIn("title", out)
        for s in SECRETS:
            self.assertNotIn(s, str(out))

        # No-perm user
        u_noperm = User.objects.create_user(username="nope", password="x")
        u_noperm._test_roles = []
        SerCls = TurboDRFSerializerFactory.create_serializer(
            model=Deal,
            fields=["id", "title", "brokerage", "assigned_broker"],
            user=u_noperm,
        )
        out = SerCls(self.victim_deal, context={"request": None}).data
        for s in SECRETS:
            self.assertNotIn(s, str(out))

        # User model never includes password
        u_admin = User.objects.create_user(username="dual_e4", password="x")
        u_admin._test_roles = ["admin"]
        SerCls = TurboDRFSerializerFactory.create_serializer(
            model=User,
            fields=["id", "username", "password"],
            user=u_admin,
        )
        self.assertNotIn("password", list(SerCls.Meta.fields))

        # __all__ + no perms → empty
        u_e5 = User.objects.create_user(username="noperm_e5", password="x")
        u_e5._test_roles = []
        permitted = TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
            Deal, "__all__", u_e5
        )
        self.assertEqual(permitted, [])

        # No writable snapshot → everything is read-only
        snap = PermissionSnapshot()
        ro = TurboDRFSerializerFactory._get_read_only_fields_with_snapshot(
            Deal, ["title", "brokerage"], snap
        )
        self.assertEqual(set(ro), {"title", "brokerage"})

        # Password-anywhere strip
        u_e7 = User.objects.create_user(username="adm_e7", password="x")
        u_e7._test_roles = ["admin"]
        permitted = TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
            User,
            ["id", "username", "password", "user__password", "user_password"],
            u_e7,
        )
        self.assertNotIn("password", permitted)
        self.assertNotIn("user__password", permitted)

        # token / session_key strip
        permitted = TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
            Deal,
            ["id", "title", "token", "session_key"],
            self._make_admin_user("k4adm"),
        )
        self.assertNotIn("token", permitted)
        self.assertNotIn("session_key", permitted)

        # Depth limits
        with override_settings(TURBODRF_MAX_NESTING_DEPTH=3):
            cache.clear()
            permitted = (
                TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
                    Deal,
                    ["id", "title", "a__b__c__d__e"],
                    self._make_admin_user("k1adm"),
                )
            )
            self.assertNotIn("a__b__c__d__e", permitted)
        with override_settings(TURBODRF_MAX_NESTING_DEPTH=0):
            cache.clear()
            permitted = (
                TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
                    Deal,
                    ["id", "title", "brokerage__name"],
                    self.attacker,
                )
            )
            self.assertNotIn("brokerage__name", permitted)
            self.assertIn("title", permitted)
        with override_settings(TURBODRF_MAX_NESTING_DEPTH=None):
            cache.clear()
            try:
                _ = TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
                    Deal, ["id", "title"], self._make_admin_user("k2adm")
                )
            except Exception as exc:
                self.fail(f"unlimited depth crashed: {exc!r}")

        # Duplicate fields
        permitted = TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
            Deal, ["title", "title", "title"], self._make_admin_user("k5adm")
        )
        self.assertTrue(all(p == "title" for p in permitted))

        # Empty-string field
        permitted = TurboDRFSerializerFactory._get_permitted_fields_with_snapshot(
            Deal, [""], self._make_admin_user("k6adm")
        )
        self.assertIsInstance(permitted, list)

        # view_type arbitrary
        SerCls = TurboDRFSerializerFactory.create_serializer(
            Deal, ["id", "title"], self.attacker, view_type="custom_view"
        )
        self.assertIn("custom_view", SerCls.Meta.ref_name)

    def test_dynamic_serializer_isolation(self):
        """Two users get distinct DynamicSerializer classes; same
        field-set → same ref_name; mutating Meta on one doesn't bleed;
        nested-fields meta has no cross-tenant refs; auto-built snapshot
        when factory called with snapshot=None."""
        SerA = TurboDRFSerializerFactory.create_serializer(
            Deal, ["id", "title"], self.attacker
        )
        SerB = TurboDRFSerializerFactory.create_serializer(
            Deal, ["id", "title"], self.victim
        )
        self.assertIsNot(SerA, SerB)
        self.assertEqual(SerA.Meta.ref_name, SerB.Meta.ref_name)

        # Mutating SerA.Meta doesn't bleed
        SerA.Meta.fields = list(SerA.Meta.fields) + ["assigned_broker"]
        SerC = TurboDRFSerializerFactory.create_serializer(
            Deal, ["id", "title"], self.victim
        )
        self.assertNotIn("assigned_broker", SerC.Meta.fields)

        # Nested fields meta no cross-tenant refs
        SerCls = TurboDRFSerializerFactory.create_serializer(
            Deal,
            ["id", "title", "assigned_broker__username"],
            self.attacker_manager,
        )
        for k, v in SerCls.Meta._nested_fields.items():
            for path in v:
                self.assertNotIn(str(self.victim.pk), path)
                self.assertNotIn("victim", path)

        # snapshot=None → factory auto-builds
        SerCls = TurboDRFSerializerFactory.create_serializer(
            Deal, ["id", "title", "brokerage"], self.attacker, snapshot=None
        )
        ser = SerCls(
            data={"title": "ok", "brokerage": self.brokerage_attacker.pk},
            context={"request": None},
        )
        self.assertTrue(hasattr(ser, "_permission_snapshot"))

    # ----- Sensitive denylist --------------------------------------------

    def test_sensitive_denylist_segment_match(self):
        """Deny-list match is segment-exact and case-sensitive: matches
        'password', 'token', 'secret_key' as full segments at any depth;
        does NOT match 'PASSWORD', 'user_password' (substring),
        'passwordhash' (concat)."""
        # NOT stripped
        for path in ("PASSWORD", "user_password", "passwordhash"):
            self.assertFalse(
                is_field_path_sensitive(path), f"{path} should NOT match"
            )

        # Stripped
        for path in (
            "password",
            "user__profile__password",
            "a__b__c__d__password",
            "a__token__b",
            "config__secret_key",
        ):
            self.assertTrue(
                is_field_path_sensitive(path), f"{path} should match"
            )

    # ----- Read-only / write filtering -----------------------------------

    def test_read_only_bypass_blocked(self):
        """A snapshot with empty writable_fields blocks updates even when
        Meta declares the field. PATCH bypassing read-only on FK doesn't
        redirect tenant. create() drops unwritable fields."""
        snap_ro = PermissionSnapshot(
            allowed_actions={"read", "update"},
            readable_fields={"title", "brokerage"},
            writable_fields=set(),
            fields_with_read_rules=set(),
            fields_with_write_rules={"title"},
        )

        class ToyDeal(TurboDRFSerializer):
            class Meta:
                model = Deal
                fields = ["title", "brokerage"]
                ref_name = "ToyDealI1"

        ser = ToyDeal(
            self.attacker_deal,
            data={
                "title": "ATTACKER_NEW",
                "brokerage": self.brokerage_attacker.pk,
            },
            partial=True,
            context={"request": MagicMock(user=self.attacker)},
        )
        ser._permission_snapshot = snap_ro
        ser.is_valid(raise_exception=True)
        original_title = self.attacker_deal.title
        try:
            ser.update(self.attacker_deal, ser.validated_data)
        except Exception:
            pass
        self.attacker_deal.refresh_from_db()
        self.assertEqual(self.attacker_deal.title, original_title)

        # PATCH FK = victim brokerage on read-only FK — no redirect
        resp = self.client.patch(
            f"/api/deals/{self.attacker_deal.pk}/",
            {"brokerage": self.brokerage_victim.pk},
            format="json",
        )
        self.attacker_deal.refresh_from_db()
        self.assertEqual(self.attacker_deal.brokerage_id, self.brokerage_attacker.pk)
        assert_no_secrets(self, resp)

        # create() drops unwritable fields
        snap_create = PermissionSnapshot(
            allowed_actions={"read", "create"},
            readable_fields={"title", "brokerage"},
            writable_fields={"brokerage"},
            fields_with_read_rules=set(),
            fields_with_write_rules={"title"},
        )

        class ToyDeal2(TurboDRFSerializer):
            class Meta:
                model = Deal
                fields = ["title", "brokerage"]
                ref_name = "ToyDealI5"

        ser = ToyDeal2(
            data={
                "title": "FORBIDDEN_TITLE",
                "brokerage": self.brokerage_attacker.pk,
            },
            context={"request": MagicMock(user=self.attacker)},
        )
        ser._permission_snapshot = snap_create
        ser.is_valid(raise_exception=True)
        try:
            ser.save()
        except Exception:
            pass
        self.assertFalse(Deal.objects.filter(title="FORBIDDEN_TITLE").exists())

    def test_read_only_meta_construction(self):
        """Factory sets read_only_fields based on snapshot writability; never
        includes fields outside Meta.fields."""
        snap = PermissionSnapshot(
            allowed_actions={"read", "create"},
            readable_fields={"title", "brokerage"},
            writable_fields={"brokerage"},
            fields_with_read_rules=set(),
            fields_with_write_rules={"title"},
        )
        SerCls = TurboDRFSerializerFactory.create_serializer(
            Deal,
            ["id", "title", "brokerage"],
            self.attacker,
            view_type="detail",
            snapshot=snap,
        )
        self.assertIn("title", SerCls.Meta.read_only_fields)

        snap_empty = PermissionSnapshot()
        ro = TurboDRFSerializerFactory._get_read_only_fields_with_snapshot(
            Deal, ["title"], snap_empty
        )
        self.assertEqual(set(ro), {"title"})

    # ----- _apply_predicate_writes ---------------------------------------

    def test_apply_predicate_writes_paths(self):
        """Direct-call: cross-tenant FK rejected, tenant redirect
        rejected, anon user rejected, no request → no-op, FK belonging
        to different tenant via API path → 400."""
        req = MagicMock()
        req.user = self.attacker

        # Cross-tenant FK
        with self.assertRaises(drf_serializers.ValidationError):
            _apply_predicate_writes(
                Transaction,
                {"amount": Decimal("1.00"), "bank_account": self.victim_bank},
                None,
                req,
            )

        # Tenant redirect
        with self.assertRaises(drf_serializers.ValidationError):
            _apply_predicate_writes(
                Deal,
                {"title": "x", "brokerage": self.brokerage_victim},
                None,
                req,
            )

        # Anon
        anon = MagicMock()
        anon.is_authenticated = False
        req_anon = MagicMock()
        req_anon.user = anon
        with self.assertRaises(drf_serializers.ValidationError):
            _apply_predicate_writes(
                Deal,
                {"title": "x", "brokerage": self.brokerage_attacker},
                None,
                req_anon,
            )

        # No request — no-op
        out = _apply_predicate_writes(
            Deal, {"title": "x"}, None, None
        )
        self.assertEqual(out, {"title": "x"})

        # End-to-end: assigned_broker = cross-tenant user → 400
        resp = self.client.post(
            "/api/deals/",
            {
                "title": "trying",
                "brokerage": self.brokerage_attacker.pk,
                "assigned_broker": self.victim.pk,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            Deal.objects.filter(
                title="trying",
                brokerage=self.brokerage_attacker,
                assigned_broker=self.victim,
            ).exists()
        )
        assert_no_secrets(self, resp)

    # ----- Cache key + API end-to-end -------------------------------------

    def test_cache_key_includes_user_pk_and_anon(self):
        """Cache key includes user pk; AnonymousUser maps to its own
        distinct key."""
        from django.contrib.auth.models import AnonymousUser

        key_a = get_cache_key(self.attacker, Deal)
        key_b = get_cache_key(self.victim, Deal)
        self.assertNotEqual(key_a, key_b)
        self.assertIn(str(self.attacker.pk), key_a)
        self.assertIn(str(self.victim.pk), key_b)

        anon_key = get_cache_key(AnonymousUser(), Deal)
        self.assertIn("anonymous", anon_key)

    def test_api_endpoints_no_secret_leak(self):
        """LIST, OPTIONS, and victim detail GET — no SECRET in payload."""
        resp = self.client.get("/api/deals/")
        self.assertEqual(resp.status_code, 200)
        assert_no_secrets(self, resp)

        resp = self.client.options("/api/deals/")
        assert_no_secrets(self, resp)

        resp = self.client.get(f"/api/deals/{self.victim_deal.pk}/")
        self.assertEqual(resp.status_code, 404)
        assert_no_secrets(self, resp)
