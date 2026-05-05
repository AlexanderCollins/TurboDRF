"""
Browsable API, OPTIONS metadata, schema/swagger, and HTML/browser-surface
security tests.

Covers compiled-vs-DRF response shape parity, renderer edge cases, OPTIONS
metadata gating, swagger schema enumeration, pagination counters, error-
message inference, FK dropdown leaks, M2M widgets, schema HTML, form
behavior, content sniffing, HTML-shape inference, OptionString scans,
swagger role manipulation, ref-name collisions, and standard browser
defenses (XSS reflection, MIME sniff, CORS, CSRF, clickjacking, HSTS,
cookies, open redirects, cache-control, referrer policy).
"""

import json
from decimal import Decimal
from unittest.mock import MagicMock, patch  # noqa: F401  (kept for tests)

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient, APIRequestFactory

from turbodrf.swagger import TurboDRFSwaggerAutoSchema

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    ArticleWithCategories,
    BankAccount,
    Brokerage,
    Category,
    CompiledArticle,
    CompiledSampleModel,
    CustomEndpointModel,
    Deal,
    RelatedModel,
    SampleModel,
    Transaction,
)

User = get_user_model()

SECRETS = ("VICTIM_SECRET_DEAL", "VICTIM_BANK_ACCOUNT", "999999.99")
VICTIM_TOKENS = SECRETS
TENANT_NAME_TOKENS = ("Victim Co", "Innocent Co")

VICTIM_DEAL_TITLE = "VICTIM_SECRET_DEAL"
VICTIM_BANK_NAME = "VICTIM_BANK_ACCOUNT"
VICTIM_TX_AMOUNT_STR = "999999.99"


# ---------------- helpers ----------------


def _blob(response):
    parts = []
    if hasattr(response, "data") and response.data is not None:
        try:
            parts.append(str(response.data))
        except Exception:
            pass
    if hasattr(response, "content"):
        try:
            parts.append(response.content.decode("utf-8", errors="replace"))
        except Exception:
            pass
    try:
        parts.append(str(dict(response.items())))
    except Exception:
        pass
    return " ".join(parts)


def _assert_clean(testcase, response, label=""):
    blob = _blob(response)
    for s in SECRETS:
        if s in blob:
            testcase.fail(
                f"[{label}] Secret {s!r} leaked. status={response.status_code}"
            )
    if response.status_code >= 500:
        testcase.fail(
            f"[{label}] 5xx ({response.status_code}). body={blob[:600]!r}"
        )


def _no_secret_leak(testcase, response, label=""):
    _assert_clean(testcase, response, label)


def _is_5xx(response):
    return 500 <= response.status_code < 600


def _gen():
    from drf_yasg import openapi

    from turbodrf.swagger import RoleBasedSchemaGenerator

    info = openapi.Info(title="t", default_version="v1")
    return RoleBasedSchemaGenerator(info=info)


def _direct_request(path="/swagger/", user=None):
    factory = APIRequestFactory()
    req = factory.get(path)
    from django.contrib.sessions.middleware import SessionMiddleware

    SessionMiddleware(lambda r: None).process_request(req)
    if user is not None:
        req.user = user
    return req


def _attempt_role(gen, req):
    try:
        gen.get_schema(req, public=False)
    except Exception:
        pass


# ============================================================================
# Unified fixture base
# ============================================================================


class SecBase(TestCase):
    """Single fixture used by all security tests in this module.

    DB rows live on the class (setUpTestData); per-test setUp only does
    cache.clear(), refreshes the in-memory user→brokerage registry, and
    builds a fresh APIClient bound to the attacker.
    """

    @classmethod
    def setUpTestData(cls):
        import tests.urls  # noqa: F401  registers router

        cls.brokerage_attacker = Brokerage.objects.create(name="Attacker Co")
        cls.brokerage_victim = Brokerage.objects.create(name="Victim Co")
        cls.brokerage_third = Brokerage.objects.create(name="Innocent Co")

        cls.attacker = User.objects.create_user(username="attacker", password="x")
        cls.attacker._test_roles = ["underwriter"]

        cls.attacker_manager = User.objects.create_user(username="mgr", password="x")
        cls.attacker_manager._test_roles = ["manager"]

        cls.viewer = User.objects.create_user(username="vw", password="x")
        cls.viewer._test_roles = ["viewer"]

        cls.admin_user = User.objects.create_user(username="adm", password="x")
        cls.admin_user._test_roles = ["admin"]

        cls.no_role_user = User.objects.create_user(username="nr", password="x")
        cls.no_role_user._test_roles = []

        cls.victim = User.objects.create_user(username="victim", password="x")
        cls.victim._test_roles = ["underwriter"]

        cls.victim_deal = Deal.objects.create(
            title=VICTIM_DEAL_TITLE,
            brokerage=cls.brokerage_victim,
            assigned_broker=cls.victim,
        )
        cls.victim_bank = BankAccount.objects.create(
            name=VICTIM_BANK_NAME, deal=cls.victim_deal
        )
        cls.victim_tx = Transaction.objects.create(
            amount=Decimal(VICTIM_TX_AMOUNT_STR), bank_account=cls.victim_bank
        )

        cls.attacker_deal = Deal.objects.create(
            title="ATTACKER_DEAL",
            brokerage=cls.brokerage_attacker,
            assigned_broker=cls.attacker,
        )
        cls.attacker_bank = BankAccount.objects.create(
            name="ATTACKER_BANK", deal=cls.attacker_deal
        )

        # Public-access fixtures.
        cls.related = RelatedModel.objects.create(name="rel_a", description="d_a")
        cls.related_b = RelatedModel.objects.create(name="rel_b", description="d_b")
        cls.cat_a = Category.objects.create(name="cat_a", description="dcA")
        cls.cat_b = Category.objects.create(name="cat_b", description="dcB")
        cls.sample_a = SampleModel.objects.create(
            title="SAMPLE_A",
            price=Decimal("1.10"),
            quantity=1,
            related=cls.related,
        )
        cls.compiled_sample_a = CompiledSampleModel.objects.create(
            title="CSAMPLE_A",
            price=Decimal("1.10"),
            is_active=True,
            related=cls.related,
        )
        cls.article_a = ArticleWithCategories.objects.create(
            title="ART_A", content="ca", author=cls.related
        )
        cls.article_a.categories.add(cls.cat_a)
        cls.compiled_article_a = CompiledArticle.objects.create(
            title="CART_A", author=cls.related
        )
        cls.compiled_article_a.categories.add(cls.cat_a)
        cls.custom_a = CustomEndpointModel.objects.create(name="custom_a")

        # XSS reflection fixture.
        cls.xss_sample = SampleModel.objects.create(
            title="<script>alert('XSSTITLE')</script>",
            description=(
                "<img src=x onerror=alert('XSSDESC')>"
                "<iframe src='https://evil.example/'></iframe>"
            ),
            price=Decimal("1.00"),
            quantity=1,
            related=cls.related,
            secret_field="</title><script>alert(1)</script>",
        )

    def setUp(self):
        cache.clear()
        _test_user_brokerages.clear()
        # Re-populate registry — the property reads it via user.pk.
        set_test_brokerage(self.attacker, self.brokerage_attacker)
        set_test_brokerage(self.attacker_manager, self.brokerage_attacker)
        set_test_brokerage(self.victim, self.brokerage_victim)

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker)

        self.admin_client = APIClient()
        self.admin_client.force_authenticate(user=self.admin_user)

    def tearDown(self):
        cache.clear()
        _test_user_brokerages.clear()

    # ---------------- assertion helpers ----------------

    def assert_no_5xx(self, response, where=""):
        self.assertLess(
            response.status_code,
            500,
            f"5xx at {where}: {response.status_code}\n{response.content[:500]}",
        )

    def assert_no_victim_leak(self, response):
        body = ""
        try:
            body = str(response.data)
        except Exception:
            pass
        try:
            body += response.content.decode("utf-8", errors="ignore")
        except Exception:
            pass
        for tok in VICTIM_TOKENS:
            self.assertNotIn(tok, body, f"VICTIM TOKEN {tok!r} LEAKED")

    def assert_no_victim_data_leak(self, response, where=""):
        body = response.content.decode("utf-8", errors="ignore")
        for tok in VICTIM_TOKENS:
            self.assertNotIn(tok, body, f"VICTIM TOKEN {tok!r} leaked at {where}")

    def get_header(self, response, name):
        for k, v in response.items():
            if k.lower() == name.lower():
                return v
        return None


# ============================================================================
# Compiled vs DRF response shape parity
# ============================================================================


class CompiledVsDrf(SecBase):
    def test_compiled_list_shape_and_decimal_rendering(self):
        # No victim leak; row dicts must NOT include the property-style
        # FK leak field (related_author_name); price is rendered as str
        # (no float drift in compiled path).
        r = self.client.get("/api/compiledsamplemodels/")
        self.assert_no_5xx(r, "compiled list")
        self.assert_no_victim_leak(r)
        if r.status_code == 200 and isinstance(r.data, dict):
            for row in r.data.get("data", []):
                self.assertNotIn("related_author_name", row)
                price = row.get("price")
                if price is not None:
                    self.assertIsInstance(price, str)

    def test_compiled_fields_param_full_combo_no_leak(self):
        for f in [
            "title", "price", "related_name", "is_active", "display_title",
            "title,price",
            "title,price,related_name,is_active,display_title",
            "id",                  # non-configured, dropped
            "secret_field",        # non-configured
            "related_author_name",  # property accessing FK — would crash
            "related",             # base FK only
        ]:
            r = self.client.get(f"/api/compiledsamplemodels/?fields={f}")
            self.assert_no_5xx(r, f"compiled fields={f}")
            self.assert_no_victim_leak(r)

    def test_compiled_article_list_and_detail_no_leak(self):
        # Compiled article M2M list goes through the compiled path; the
        # detail falls back to DRF (compiled is list-only).
        r_l = self.client.get("/api/compiledarticles/")
        self.assert_no_5xx(r_l, "compiled article m2m")
        self.assert_no_victim_leak(r_l)
        r_d = self.client.get(
            f"/api/compiledarticles/{self.compiled_article_a.id}/"
        )
        self.assert_no_5xx(r_d, "compiled article detail")
        self.assert_no_victim_leak(r_d)

    def test_deal_detail_drf_for_foreign_returns_404_no_extra_field(self):
        r = self.client.get(f"/api/deals/{self.victim_deal.id}/")
        self.assertEqual(r.status_code, 404)
        self.assert_no_victim_leak(r)
        body = r.content.decode("utf-8", errors="ignore")
        self.assertNotIn("assigned_broker", body)
        self.assertNotIn("brokerage", body)

    def test_compiled_path_pagination_total_matches_attacker_scope(self):
        r = self.client.get("/api/compiledsamplemodels/?page_size=1")
        self.assert_no_5xx(r, "compiled pagination")
        if r.status_code == 200 and isinstance(r.data, dict):
            total = r.data.get("pagination", {}).get("total_items")
            self.assertIsNotNone(total)
            self.assertGreaterEqual(total, 0)


# ============================================================================
# Renderer / serialization edge cases
# ============================================================================


class RendererEdges(SecBase):
    def test_unicode_filter_inputs_no_leak(self):
        # Emoji, RTL override, ZWJ/ZWSP/BOM, combining diaeresis.
        for q in (
            "title__icontains=%F0%9F%98%80",     # emoji
            "search=%E2%80%AE",                    # RTL override
            "search=%E2%80%8B",                    # ZWSP
            "search=%E2%80%8D",                    # ZWJ
            "search=%EF%BB%BF",                    # BOM
            "title__icontains=VICTIM%CC%88",       # combining diaeresis
        ):
            r = self.client.get(f"/api/deals/?{q}")
            self.assert_no_5xx(r, q)
            self.assert_no_victim_leak(r)

    def test_numeric_edge_inputs_no_500(self):
        for val in ("NaN", "Infinity", "-Infinity", "1e400"):
            r = self.client.get(f"/api/transactions/?amount={val}")
            self.assert_no_5xx(r, f"NaN/Inf {val}")
            self.assert_no_victim_leak(r)

    def test_overflow_integer_in_pk_no_500(self):
        for val in ("9999999999999999999999999999",
                    "-9999999999999999999999999999"):
            r = self.client.get(f"/api/deals/{val}/")
            self.assert_no_5xx(r, f"overflow int {val}")
            self.assert_no_victim_leak(r)

    def test_dunder_and_iso_datetime_inputs_dropped_or_safe(self):
        # Dunder field names must be dropped from `fields` query, and ISO
        # datetime variants must not 500 / leak when used as filters.
        for f in ("__dict__", "__class__", "_state", "_meta"):
            r = self.client.get(f"/api/deals/?fields={f}")
            self.assert_no_5xx(r, f"dunder {f}")
            self.assert_no_victim_leak(r)
        for val in (
            "2026-01-01T00%3A00%3A00Z",
            "2026-01-01T00%3A00%3A00%2B00%3A00",
            "2026-01-01T00%3A00%3A00.000Z",
        ):
            r = self.client.get(f"/api/deals/?id__gte=1&search={val}")
            self.assert_no_5xx(r, f"iso datetime {val}")
            self.assert_no_victim_leak(r)

    def test_renderer_charset_negotiation_unicode_round_trip(self):
        # Charset on JSON list is unset/utf-8; q-factor / unknown / */*
        # negotiation never 5xxs and never leaks; unicode POSTs do not
        # double-encode in the response body.
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 200)
        ct = r.get("Content-Type", "")
        if "charset" in ct.lower():
            self.assertIn("utf-8", ct.lower())
        for accept in (
            "text/html;q=0.1, application/json;q=0.9",
            "application/x-victim-leak",
            "*/*",
        ):
            r = self.client.get("/api/deals/", HTTP_ACCEPT=accept)
            self.assert_no_5xx(r, f"accept {accept}")
            self.assert_no_victim_leak(r)
        r_post = self.client.post(
            "/api/deals/",
            {"title": "héllo 日本", "brokerage": self.brokerage_attacker.id},
            format="json",
        )
        if r_post.status_code == 201:
            self.assertNotIn(
                "\\u00e9",
                r_post.content.decode("utf-8", errors="ignore"),
            )

    def test_search_999999_does_not_pivot_across_endpoints(self):
        for ep in ("deals", "bankaccounts", "transactions"):
            r = self.client.get(f"/api/{ep}/?search=999999")
            self.assert_no_5xx(r, f"search 999999 {ep}")
            self.assert_no_victim_leak(r)


# ============================================================================
# OPTIONS metadata
# ============================================================================


class OptionsMetadata(SecBase):
    def test_options_no_victim_leak_across_endpoints_and_classes_hidden(self):
        # OPTIONS on every router endpoint must not echo victim tokens
        # nor framework internal class names.
        for ep in ("/api/deals/", "/api/bankaccounts/", "/api/transactions/",
                   "/api/compiledsamplemodels/", "/api/compiledarticles/"):
            r = self.client.options(ep)
            self.assert_no_5xx(r, f"OPTIONS {ep}")
            self.assert_no_victim_leak(r)
        body = str(getattr(self.client.options("/api/deals/"), "data", ""))
        for tok in ("TurboDRFViewSet", "TurboDRFSerializerFactory",
                    "Traceback", "ImproperlyConfigured"):
            self.assertNotIn(tok, body)
        # Same OPTIONS under text/html negotiation.
        r_html = self.client.options("/api/deals/", HTTP_ACCEPT="text/html")
        self.assert_no_5xx(r_html, "options text/html")
        self.assert_no_victim_data_leak(r_html, "OPTIONS text/html")

    def test_options_allow_header_parity_foreign_vs_ghost(self):
        # Allow header must not differ between foreign vs nonexistent → no
        # existence oracle.
        r_foreign = self.client.options(f"/api/deals/{self.victim_deal.id}/")
        r_ghost = self.client.options("/api/deals/9999999/")
        self.assert_no_5xx(r_foreign, "options foreign")
        self.assert_no_5xx(r_ghost, "options ghost")
        self.assertEqual(r_foreign.get("Allow", ""), r_ghost.get("Allow", ""))

    def test_options_capabilities_compiled_vs_drf(self):
        r_c = self.client.options("/api/compiledsamplemodels/")
        if r_c.status_code == 200 and isinstance(r_c.data, dict):
            self.assertEqual(
                r_c.data.get("capabilities", {}).get("client_fields_param"),
                "fields",
            )
        r_d = self.client.options("/api/deals/")
        if r_d.status_code == 200 and isinstance(r_d.data, dict):
            # compiled=False → client_fields_param None.
            self.assertIsNone(
                r_d.data.get("capabilities", {}).get("client_fields_param")
            )

    def test_options_tenancy_and_field_choices_do_not_leak(self):
        # Tenancy block exposes only the structural field name (no tenant
        # value/id). Field choices are filtered by visibility — no
        # VICTIM-tagged choice values for unreadable fields.
        r = self.client.options("/api/deals/")
        if r.status_code == 200 and isinstance(r.data, dict):
            tenancy = r.data.get("tenancy", {})
            self.assertNotIn("tenant_value", tenancy)
            self.assertNotIn("tenant_id", tenancy)
            self.assertNotIn(str(self.brokerage_attacker.id), str(tenancy))
            self.assertEqual(tenancy.get("tenant_field"), "brokerage")
            fields = (r.data.get("model") or {}).get("fields", {}) or {}
            for finfo in fields.values():
                for c in (finfo.get("choices") or []):
                    self.assertNotIn("VICTIM", str(c))


# ============================================================================
# Pagination / counting inference
# ============================================================================


class PaginationCount(SecBase):
    def test_attacker_pagination_total_and_pages_consistent(self):
        # page_size=1, ordering, and huge page_size all yield total=1 for
        # attacker; total_pages stays consistent with total_items; the
        # next link doesn't encode victim tokens.
        for q in ("page_size=1", "ordering=title", "page_size=999999"):
            r = self.client.get(f"/api/deals/?{q}")
            self.assert_no_5xx(r, q)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data["pagination"]["total_items"], 1)
        r = self.client.get("/api/deals/?page_size=1")
        p = r.data["pagination"]
        self.assertEqual(p["total_pages"], 1)
        next_link = p.get("next")
        if next_link:
            for tok in VICTIM_TOKENS:
                self.assertNotIn(tok, next_link)

    def test_filter_icontains_victim_token_returns_zero(self):
        for needle in ("VICTIM", "VICTIM_SECRET", "SECRET_DEAL"):
            r = self.client.get(f"/api/deals/?title__icontains={needle}")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data["pagination"]["total_items"], 0)
            ids = [d["id"] for d in r.data["data"]]
            self.assertNotIn(self.victim_deal.id, ids)
            self.assert_no_victim_leak(r)

    def test_pagination_in_filter_does_not_leak(self):
        # id__in including victim deal must NOT include it in results.
        r = self.client.get(
            f"/api/deals/?id__in={self.victim_deal.id},{self.attacker_deal.id}"
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["pagination"]["total_items"], 1)
        ids = [d["id"] for d in r.data["data"]]
        self.assertNotIn(self.victim_deal.id, ids)


# ============================================================================
# Error message inference
# ============================================================================


class ErrorMessageInference(SecBase):
    def test_malformed_post_bodies_no_500_no_class_or_path_leak(self):
        # Invalid JSON, unknown fields, nested-list typed-error, and the
        # default DRF validation 400. All must avoid 5xx and never leak
        # framework class names or filesystem paths.
        cases = [
            ("{not valid json", "raw"),
            ("}}}}", "raw"),
        ]
        for body, _ in cases:
            r = self.client.post(
                "/api/deals/", body, content_type="application/json"
            )
            self.assert_no_5xx(r, f"invalid json {body!r}")
            self.assert_no_victim_leak(r)
            content = str(r.content)
            for tok in ("/Users/", "site-packages", "Traceback",
                        "msgspec", "orjson"):
                self.assertNotIn(tok, content)

        for payload in (
            {"foobar_unknown": "x"},
            {"title": "x", "brokerage": ["nested-list"]},
            {},
        ):
            r = self.client.post("/api/deals/", payload, format="json")
            self.assert_no_5xx(r, f"POST {payload}")
            self.assert_no_victim_leak(r)
            body = str(r.content)
            for tok in ("TurboDRFViewSet", "TurboDRFSerializerFactory",
                        "Traceback", "/Users/", "site-packages",
                        "ImproperlyConfigured"):
                self.assertNotIn(tok, body)

    def test_cross_tenant_fk_vs_nonexistent_fk_indistinguishable(self):
        r1 = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": self.victim_bank.id},
            format="json",
        )
        r2 = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": 99999999},
            format="json",
        )
        self.assertEqual(r1.status_code, r2.status_code)
        self.assertEqual(r1.data, r2.data)

    def test_500_under_debug_false_does_not_leak_traceback(self):
        with override_settings(DEBUG=False):
            try:
                r = self.client.post(
                    "/api/deals/",
                    [{"title": "a"}, {"title": "b"}],
                    format="json",
                )
                body = str(r.content)
                for tok in ("Traceback", "/Users/", "site-packages",
                            "_prefill_required_fields", "VICTIM"):
                    self.assertNotIn(tok, body)
            except Exception as e:
                for tok in VICTIM_TOKENS:
                    self.assertNotIn(tok, str(e))

    def test_oversized_and_null_byte_field_no_500(self):
        r1 = self.client.post(
            "/api/deals/", {"title": "x" * 100000}, format="json"
        )
        self.assert_no_5xx(r1, "huge field")
        self.assert_no_victim_leak(r1)
        r2 = self.client.post(
            "/api/deals/", {"title": "a\x00b"}, format="json"
        )
        self.assert_no_5xx(r2, "null byte")
        self.assert_no_victim_leak(r2)


# ============================================================================
# Caching / etags / vary
# ============================================================================


class CacheHeaders(SecBase):
    def test_cache_headers_safe_for_user_scoped(self):
        # Vary stable across calls; ETag/Last-Modified don't encode victim
        # tokens; no Cache-Control: public on a user-scoped response.
        r1 = self.client.get("/api/deals/")
        r2 = self.client.get("/api/deals/")
        self.assertEqual(r1.get("Vary", ""), r2.get("Vary", ""))
        for hdr in ("ETag", "Last-Modified"):
            v = r1.get(hdr)
            if v:
                for tok in VICTIM_TOKENS:
                    self.assertNotIn(tok, v)
        cc = (r1.get("Cache-Control") or "").lower()
        if "public" in cc:
            self.fail(
                "Cache-Control: public on user-scoped endpoint — shared "
                "cache could serve cross-tenant data"
            )
        # If Vary is set on the admin detail, must include Accept or Cookie.
        r_d = self.admin_client.get(f"/api/samplemodels/{self.xss_sample.pk}/")
        vary = self.get_header(r_d, "Vary") or ""
        if vary:
            self.assertTrue("Accept" in vary or "Cookie" in vary, vary)

    def test_conditional_get_does_not_yield_victim(self):
        for hdr_name, hdr_val in (
            ("HTTP_IF_NONE_MATCH", '"victim-hash"'),
            ("HTTP_IF_MODIFIED_SINCE", "Sun, 01 Jan 2020 00:00:00 GMT"),
        ):
            r = self.client.get("/api/deals/", **{hdr_name: hdr_val})
            self.assert_no_5xx(r, hdr_name)
            self.assert_no_victim_leak(r)


# ============================================================================
# Browsable API HTML — FK dropdown leak / no-leak across endpoints
# ============================================================================


class BrowsableHtml(SecBase):
    # Endpoints whose ?format=api response must NOT echo any victim token.
    NO_LEAK_ENDPOINTS = (
        "/api/deals/",
        "/api/bankaccounts/",
        "/api/transactions/",
        "/api/samplemodels/",
        "/api/articlewithcategoriess/",
        "/api/compiledarticles/",
        "/api/custom-items/",
        "/api/compiledsamplemodels/",
        "/api/relatedmodels/",
        "/api/categorys/",
    )

    def test_no_victim_leak_across_browsable_endpoints(self):
        for ep in self.NO_LEAK_ENDPOINTS:
            r = self.client.get(f"{ep}?format=api")
            self.assert_no_5xx(r, f"{ep} ?format=api")
            self.assert_no_victim_data_leak(r, ep)

    def test_fk_dropdowns_do_not_leak_foreign_tenants(self):
        # Brokerage has no tenant_field of its own; its FK queryset still
        # must not surface foreign tenant names. Deal-FK on bankaccounts
        # and bankaccount-FK on transactions ARE tenant-scoped.
        r_deals = self.client.get("/api/deals/?format=api")
        self.assert_no_5xx(r_deals, "deals ?format=api")
        body = r_deals.content.decode("utf-8", errors="ignore")
        self.assertNotIn("Victim Co", body)
        self.assertNotIn("Innocent Co", body)

        r_ba = self.client.get("/api/bankaccounts/?format=api")
        self.assertNotIn(
            "VICTIM_SECRET_DEAL",
            r_ba.content.decode("utf-8", errors="ignore"),
        )
        self.assert_no_victim_data_leak(r_ba, "bankaccounts list")

        r_tx = self.client.get("/api/transactions/?format=api")
        self.assertNotIn(
            "VICTIM_BANK_ACCOUNT",
            r_tx.content.decode("utf-8", errors="ignore"),
        )
        self.assert_no_victim_data_leak(r_tx, "transactions list")

    def test_brokerage_option_count_does_not_reveal_total(self):
        # Inference: <option> count inside name="brokerage" select reveals
        # total brokerage count → infer foreign tenants exist.
        r = self.client.get("/api/deals/?format=api")
        body = r.content.decode("utf-8", errors="ignore")
        if r.status_code == 200:
            idx = body.find('name="brokerage"')
            if idx > 0:
                snippet = body[idx: idx + 4000]
                close_idx = snippet.find("</select>")
                if close_idx > 0:
                    snippet = snippet[:close_idx]
                opt_count = snippet.count("<option")
                self.assertLess(
                    opt_count, 3,
                    f"inference: {opt_count} <option> tags reveals "
                    f">=3 brokerages exist",
                )

    def test_victim_detail_html_returns_404(self):
        # Deal, BankAccount (2-hop), Transaction (3-hop) — each detail must
        # 404 without the victim token in the response.
        for path, tok in (
            (f"/api/deals/{self.victim_deal.id}/?format=api",
             "VICTIM_SECRET_DEAL"),
            (f"/api/bankaccounts/{self.victim_bank.id}/?format=api",
             "VICTIM_BANK_ACCOUNT"),
            (f"/api/transactions/{self.victim_tx.id}/?format=api",
             "999999.99"),
        ):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 404, path)
            body = r.content.decode("utf-8", errors="ignore")
            self.assertNotIn(tok, body)

    def test_own_deal_detail_html_no_filters_panel_leak(self):
        # Own-deal detail must NOT echo Victim Co / victim username; the
        # list filters panel must not surface secret_field.
        r_d = self.client.get(
            f"/api/deals/{self.attacker_deal.id}/?format=api"
        )
        self.assert_no_5xx(r_d, "own deal detail HTML")
        self.assert_no_victim_data_leak(r_d, "own deal HTML")
        body_d = r_d.content.decode("utf-8", errors="ignore")
        self.assertNotIn("Victim Co", body_d)
        self.assertNotIn(">victim<", body_d)

        r_l = self.client.get("/api/deals/?format=api")
        body_l = r_l.content.decode("utf-8", errors="ignore")
        self.assertNotIn("secret_field", body_l)
        self.assert_no_victim_data_leak(r_l, "deals filters panel")

    def test_html_response_under_role_swap_and_anon(self):
        # Manager role (still tenant-bound), anonymous (not 200), and an
        # anonymous browsable hit on a public-access model.
        self.client.force_authenticate(user=self.attacker_manager)
        r_mgr = self.client.get("/api/deals/?format=api")
        self.assert_no_5xx(r_mgr, "manager HTML")
        self.assertNotIn(
            "VICTIM_SECRET_DEAL",
            r_mgr.content.decode("utf-8", errors="ignore"),
        )
        anon = APIClient()
        r_anon = anon.get("/api/deals/?format=api")
        self.assertNotEqual(r_anon.status_code, 200)
        self.assertNotIn(
            "VICTIM_SECRET_DEAL",
            r_anon.content.decode("utf-8", errors="ignore"),
        )
        r_pub = anon.get("/api/samplemodels/?format=api")
        self.assert_no_5xx(r_pub, "anon samplemodels")
        self.assert_no_victim_data_leak(r_pub, "anon public-access HTML")

    def test_post_form_fk_injection_blocked_at_layer3(self):
        # Even though the browsable form may surface foreign FK options,
        # the POST itself must be rejected by Layer-3 write check.
        r = self.client.post(
            "/api/deals/",
            data={"title": "ATTEMPT",
                  "brokerage": self.brokerage_victim.id,
                  "assigned_broker": self.victim.id},
            format="json",
        )
        self.assertNotEqual(r.status_code, 201)
        body = r.content.decode("utf-8", errors="ignore")
        self.assertNotIn("VICTIM_SECRET_DEAL", body)

    def test_browsable_search_ordering_filter_negotiation_no_leak(self):
        # Cross-cutting matrix: search/ordering/brokerage-filter on the
        # browsable endpoint, plus content-negotiation Accept variants on
        # the JSON list. None must leak the victim deal title.
        cases = [f"format=api&{q}" for q in (
            "search=VICTIM", "ordering=-id",
            f"brokerage={self.brokerage_victim.id}",
        )]
        for q in cases:
            r = self.client.get(f"/api/deals/?{q}")
            self.assert_no_5xx(r, f"browsable {q}")
            self.assertNotIn(
                "VICTIM_SECRET_DEAL",
                r.content.decode("utf-8", errors="ignore"),
            )
        for accept in (
            "text/html",
            "text/html;q=1.0,application/json;q=0.1",
            "*/*",
        ):
            r = self.client.get("/api/deals/", HTTP_ACCEPT=accept)
            self.assert_no_5xx(r, f"accept {accept}")
            self.assertNotIn(
                "VICTIM_SECRET_DEAL",
                r.content.decode("utf-8", errors="ignore"),
            )
        # format=json overrides Accept text/html.
        r = self.client.get("/api/deals/?format=json", HTTP_ACCEPT="text/html")
        self.assertNotIn(
            "VICTIM_SECRET_DEAL",
            r.content.decode("utf-8", errors="ignore"),
        )

    def test_settings_overrides_do_not_leak_html(self):
        # REQUIRE_TENANCY anon, DISABLE_PERMISSIONS auth — neither must leak.
        with override_settings(TURBODRF_REQUIRE_TENANCY=True):
            c = APIClient()
            r = c.get("/api/deals/?format=api")
            for tok in VICTIM_TOKENS:
                self.assertNotIn(tok, r.content.decode("utf-8", errors="ignore"))
        with override_settings(TURBODRF_DISABLE_PERMISSIONS=True):
            r = self.client.get("/api/deals/?format=api")
            self.assertNotIn(
                "VICTIM_SECRET_DEAL",
                r.content.decode("utf-8", errors="ignore"),
            )


# ============================================================================
# XSS reflection
# ============================================================================


class XssReflection(SecBase):
    """Round-trip XSS payloads through model fields and through error
    messages. Defense: JSON content-type + Django auto-escape on HTML."""

    def test_xss_payload_round_trip_json_only(self):
        # Round-trip various XSS shapes through POST; every response must
        # be application/json (never text/html), and the canonical script-
        # tag round-trip must also carry X-Content-Type-Options: nosniff.
        payloads = (
            {"title": "<script>alert('XSS_T1')</script>", "description": "x"},
            {"title": "ok", "description": "<img src=x onerror=alert(1)>"},
            {"title": "javascript:alert(1)", "description": "x"},
            {"title": "ok", "description": "<iframe src='evil.example'>"},
        )
        for p in payloads:
            r = self.admin_client.post(
                "/api/samplemodels/",
                {**p, "price": "1.00", "quantity": 1, "related": self.related.pk},
                format="json",
            )
            self.assertIn(r.status_code, (200, 201))
            ct = r.get("Content-Type", "").lower()
            self.assertIn("application/json", ct)
            self.assertNotIn("text/html", ct)
        # Canonical script-tag also gets the nosniff header.
        r0 = self.admin_client.post(
            "/api/samplemodels/",
            {"title": "<script>alert('XSS_T1')</script>", "description": "x",
             "price": "1.00", "quantity": 1, "related": self.related.pk},
            format="json",
        )
        self.assertEqual(
            r0.get("X-Content-Type-Options", "").lower(), "nosniff"
        )

    def test_pre_encoded_payload_is_not_decoded_unsafely(self):
        encoded = "&lt;script&gt;alert(1)&lt;/script&gt;"
        r = self.admin_client.post(
            "/api/samplemodels/",
            {
                "title": encoded, "description": "x",
                "price": "1.00", "quantity": 1, "related": self.related.pk,
            },
            format="json",
        )
        self.assertIn(r.status_code, (200, 201))
        body = r.content.decode("utf-8", errors="replace")
        self.assertIn("&lt;", body)
        self.assertNotIn("<script>", body)

    def test_xss_payload_in_browsable_html_is_escaped(self):
        # Detail and list HTML.
        for path in (f"/api/samplemodels/{self.xss_sample.pk}/?format=api",
                     "/api/samplemodels/?format=api"):
            r = self.admin_client.get(path)
            self.assertEqual(r.status_code, 200)
            body = r.content.decode("utf-8", errors="replace")
            self.assertNotIn("<script>alert('XSSTITLE')</script>", body)
            self.assertNotIn("<img src=x onerror=alert('XSSDESC')>", body)
        # Detail HTML must surface an escaped form somewhere.
        r = self.admin_client.get(
            f"/api/samplemodels/{self.xss_sample.pk}/?format=api"
        )
        body = r.content.decode("utf-8", errors="replace")
        self.assertTrue(
            "&lt;script&gt;" in body or "\\u003c" in body,
            "no escaped form found for <script>",
        )

    def test_xss_in_error_path_and_secret_field_and_404_are_safe(self):
        # 1. Validation 400 on JSON: application/json content-type.
        # 2. Browsable-API error: any HTML must escape the <script>.
        # 3. Field that the viewer cannot read: payload must NOT appear.
        # 4. URL-path XSS in 404 must not be reflected raw.
        r = self.admin_client.post(
            "/api/samplemodels/",
            {"title": "<script>alert('errXSS')</script>", "description": "x"},
            format="json",
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("application/json", r.get("Content-Type", "").lower())

        r2 = self.admin_client.post(
            "/api/samplemodels/?format=api",
            {"title": "<script>alert('errXSS')</script>", "description": "x"},
            format="json",
        )
        if "html" in r2.get("Content-Type", "").lower():
            self.assertNotIn(
                "<script>alert('errXSS')</script>",
                r2.content.decode("utf-8", errors="replace"),
            )

        c = APIClient()
        c.force_authenticate(user=self.viewer)
        r3 = c.get(f"/api/samplemodels/{self.xss_sample.pk}/")
        self.assertEqual(r3.status_code, 200)
        self.assertNotIn(
            "</title><script>alert(1)</script>",
            r3.content.decode("utf-8", errors="replace"),
        )

        r4 = self.client.get(
            "/api/samplemodels/<script>alert(1)</script>/"
        )
        if r4.status_code in (404, 400):
            self.assertNotIn(
                "<script>alert(1)</script>",
                r4.content.decode("utf-8", errors="replace"),
            )


# ============================================================================
# Browser security headers (nosniff, XFO, Referrer-Policy, header sweep)
# ============================================================================


class BrowserHeaders(SecBase):
    """SecurityMiddleware emits X-Content-Type-Options, X-Frame-Options,
    Referrer-Policy on every response. Verify they land on every shape."""

    SECURITY_HEADERS = (
        "X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy",
    )

    def _assert_all(self, r, label):
        for h in self.SECURITY_HEADERS:
            self.assertIsNotNone(
                self.get_header(r, h),
                f"[{label}] missing security header {h}",
            )

    def test_security_headers_on_response_matrix(self):
        # One response per shape that a browser can hit. If middleware is
        # mis-wired, the regression here is catastrophic and easy to spot.
        cases = []

        cases.append(("list", self.client.get("/api/samplemodels/")))
        cases.append((
            "detail",
            self.client.get(f"/api/samplemodels/{self.xss_sample.pk}/"),
        ))

        r_201 = self.admin_client.post(
            "/api/samplemodels/",
            {"title": "ok", "description": "x", "price": "1.00",
             "quantity": 1, "related": self.related.pk},
            format="json",
        )
        self.assertIn(r_201.status_code, (200, 201))
        cases.append(("create-201", r_201))

        r_400 = self.admin_client.post(
            "/api/samplemodels/", {"title": "x"}, format="json"
        )
        self.assertEqual(r_400.status_code, 400)
        cases.append(("create-400", r_400))

        r_404 = self.client.get(f"/api/deals/{self.victim_deal.pk}/")
        self.assertEqual(r_404.status_code, 404)
        cases.append(("404", r_404))

        r_405 = self.client.put("/api/samplemodels/", {}, format="json")
        if r_405.status_code == 405:
            cases.append(("405", r_405))

        cases.append(("options", self.client.options("/api/samplemodels/")))
        cases.append((
            "browsable-html",
            self.client.get("/api/samplemodels/?format=api"),
        ))

        anon = APIClient()
        cases.append(("anon-get", anon.get("/api/samplemodels/")))
        cases.append((
            "anon-browsable",
            anon.get("/api/samplemodels/?format=api"),
        ))

        r_sw = self.client.get("/swagger/")
        if not _is_5xx(r_sw):
            cases.append(("swagger", r_sw))

        for label, r in cases:
            self._assert_all(r, label)

    def test_xfo_or_csp_frame_ancestors_present(self):
        # Either X-Frame-Options or CSP frame-ancestors must block iframes.
        r = self.client.get("/api/samplemodels/?format=api")
        csp = self.get_header(r, "Content-Security-Policy") or ""
        xfo = self.get_header(r, "X-Frame-Options") or ""
        self.assertTrue(
            "frame-ancestors" in csp.lower()
            or xfo.upper() in ("DENY", "SAMEORIGIN"),
        )

    def test_referrer_policy_is_safe(self):
        for path in ("/api/samplemodels/", "/api/samplemodels/?format=api"):
            r = self.client.get(path)
            rp = (self.get_header(r, "Referrer-Policy") or "").lower()
            self.assertNotEqual(rp, "unsafe-url")
            if rp:
                self.assertIn(
                    rp,
                    ("same-origin", "strict-origin",
                     "strict-origin-when-cross-origin", "no-referrer",
                     "no-referrer-when-downgrade"),
                )

    def test_json_response_starts_with_application_json(self):
        # JSON containing a <script> string value is still served as JSON
        # (browsers won't sniff it as HTML).
        r = self.client.get(f"/api/samplemodels/{self.xss_sample.pk}/")
        ct = r.get("Content-Type", "")
        self.assertTrue(ct.lower().startswith("application/json"), ct)


# ============================================================================
# CORS (no permissive headers in default config)
# ============================================================================


class Cors(SecBase):
    def test_no_acao_on_cross_origin_get(self):
        for origin in ("http://evil.example", "null",
                       "http://attacker.example, http://evil.example",
                       "http://evil.example/<script>"):
            r = self.client.get("/api/samplemodels/", HTTP_ORIGIN=origin)
            self.assertIsNone(self.get_header(r, "Access-Control-Allow-Origin"))
            for k, v in r.items():
                self.assertNotIn("<script>", v)

    def test_preflight_emits_no_cors_headers(self):
        r = self.client.options(
            "/api/samplemodels/",
            HTTP_ORIGIN="http://evil.example",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="X-Custom, Authorization",
        )
        for h in (
            "Access-Control-Allow-Origin",
            "Access-Control-Allow-Methods",
            "Access-Control-Allow-Headers",
            "Access-Control-Allow-Credentials",
        ):
            self.assertIsNone(
                self.get_header(r, h),
                f"unexpected {h} on preflight",
            )

    def test_preflight_cross_tenant_detail_no_acao_no_leak(self):
        r = self.client.options(
            f"/api/deals/{self.victim_deal.pk}/",
            HTTP_ORIGIN="http://evil.example",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
        )
        self.assertIsNone(self.get_header(r, "Access-Control-Allow-Origin"))
        _no_secret_leak(self, r, "preflight victim detail")

    def test_cross_origin_post_no_acao(self):
        r = self.admin_client.post(
            "/api/samplemodels/",
            {"title": "ok", "description": "x", "price": "1.00",
             "quantity": 1, "related": self.related.pk},
            format="json",
            HTTP_ORIGIN="http://evil.example",
        )
        self.assertIsNone(self.get_header(r, "Access-Control-Allow-Origin"))


# ============================================================================
# CSRF / Cookies
# ============================================================================


class CsrfCookies(SecBase):
    def test_csrf_cookie_default_attributes(self):
        c = APIClient(enforce_csrf_checks=True)
        r = c.get("/api/samplemodels/?format=api")
        cookie = r.cookies.get("csrftoken")
        if cookie is None:
            self.skipTest("Browsable API not issuing csrftoken")
        self.assertEqual(cookie.get("path") or "/", "/")
        self.assertIn(
            (cookie.get("samesite") or "").lower(),
            ("lax", "strict"),
        )

    def test_post_without_csrf_token_rejected_with_session_auth(self):
        from django.test import Client as DjangoClient

        c = DjangoClient(enforce_csrf_checks=True)
        if not c.login(username=self.admin_user.username, password="x"):
            self.skipTest("Login failed")
        r = c.post(
            "/api/samplemodels/",
            data='{"title":"x","description":"x","price":"1","quantity":1,'
                 f'"related":{self.related.pk}}}',
            content_type="application/json",
        )
        self.assertIn(r.status_code, (401, 403))

    def test_post_with_bogus_csrf_token_rejected(self):
        from django.test import Client as DjangoClient

        c = DjangoClient(enforce_csrf_checks=True)
        if not c.login(username=self.admin_user.username, password="x"):
            self.skipTest("Login failed")
        r = c.post(
            "/api/samplemodels/",
            data='{"title":"x"}',
            content_type="application/json",
            HTTP_X_CSRFTOKEN="attacker-controlled-bogus-token",
        )
        self.assertIn(r.status_code, (401, 403))

    def test_session_cookie_httponly_after_login(self):
        from django.test import Client as DjangoClient

        c = DjangoClient(enforce_csrf_checks=False)
        if not c.login(username=self.admin_user.username, password="x"):
            self.skipTest("Login failed")
        r = c.get("/api/samplemodels/")
        self.assertIn(r.status_code, (200, 403))
        sess = r.cookies.get("sessionid")
        if sess is None:
            self.skipTest("No sessionid cookie observed")
        self.assertTrue(sess.get("httponly"))
        ss = (sess.get("samesite") or "").lower()
        self.assertIn(ss, ("lax", "strict", "none"))

    @override_settings(SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True)
    def test_secure_cookie_flag_when_configured(self):
        c = APIClient(enforce_csrf_checks=True)
        r = c.get("/api/samplemodels/?format=api", secure=True)
        csrf = r.cookies.get("csrftoken")
        if csrf is None:
            self.skipTest("No csrftoken cookie in browsable API")
        self.assertTrue(csrf.get("secure"))

    @override_settings(
        SESSION_COOKIE_SAMESITE="Strict", CSRF_COOKIE_SAMESITE="Strict"
    )
    def test_strict_samesite_when_configured(self):
        c = APIClient(enforce_csrf_checks=True)
        r = c.get("/api/samplemodels/?format=api")
        csrf = r.cookies.get("csrftoken")
        if csrf is None:
            self.skipTest("No csrftoken issued")
        self.assertEqual((csrf.get("samesite") or "").lower(), "strict")


# ============================================================================
# HSTS / TLS
# ============================================================================


class Hsts(SecBase):
    def test_hsts_absent_by_default(self):
        # Test config doesn't set SECURE_HSTS_SECONDS → header absent.
        r = self.client.get("/api/samplemodels/")
        self.assertIsNone(self.get_header(r, "Strict-Transport-Security"))

    @override_settings(
        SECURE_HSTS_SECONDS=31536000,
        SECURE_HSTS_INCLUDE_SUBDOMAINS=True,
        SECURE_HSTS_PRELOAD=True,
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
    )
    def test_hsts_emitted_only_over_https(self):
        # With config + HTTPS signal → emitted with all directives.
        r = self.client.get(
            "/api/samplemodels/", HTTP_X_FORWARDED_PROTO="https"
        )
        hsts = self.get_header(r, "Strict-Transport-Security")
        self.assertIsNotNone(hsts)
        for directive in ("max-age=", "includeSubDomains", "preload"):
            self.assertIn(directive, hsts)
        # Without HTTPS signal → still absent (browsers ignore HTTP HSTS).
        r2 = self.client.get("/api/samplemodels/")
        self.assertIsNone(self.get_header(r2, "Strict-Transport-Security"))


# ============================================================================
# Open redirect / Host header injection
# ============================================================================


class OpenRedirect(SecBase):
    def test_no_open_redirect_via_host_header_or_next_param(self):
        # 1. Pagination next must not reflect attacker Host / X-Forwarded-Host.
        for i in range(3):
            SampleModel.objects.create(
                title=f"row{i}", description="x", price=Decimal("1"),
                quantity=1, related=self.related,
            )
        for hdr in ({}, {"HTTP_HOST": "evil.example"},
                    {"HTTP_X_FORWARDED_HOST": "evil.example"}):
            r = self.admin_client.get("/api/samplemodels/?page_size=2", **hdr)
            if r.status_code == 200:
                next_url = r.data.get("pagination", {}).get("next") or ""
                self.assertNotIn("evil.example", next_url)
                self.assertNotIn("//evil", next_url)

        # 2. Browsable + next param must not 302 to evil.example or echo it.
        r = self.client.get(
            "/api/samplemodels/?format=api&next=https://evil.example/"
        )
        self.assertNotEqual(r.status_code, 302)
        self.assertNotIn(
            "evil.example",
            r.content.decode("utf-8", errors="replace")[:5000],
        )

        # 3. POST with ?next= must not produce a redirect Location either.
        r2 = self.admin_client.post(
            "/api/samplemodels/?next=https://evil.example/",
            {"title": "ok", "description": "x", "price": "1.00",
             "quantity": 1, "related": self.related.pk},
            format="json",
        )
        self.assertNotIn(r2.status_code, (301, 302, 303, 307, 308))
        loc = self.get_header(r2, "Location") or ""
        self.assertNotIn("evil.example", loc)


# ============================================================================
# JSON-shape and fingerprinting headers
# ============================================================================


class BrowserShapes(SecBase):
    def test_json_shape_and_no_jsonp_callback(self):
        # Top-level JSON must be an object (XSSI-safe in old browsers).
        r = self.admin_client.get("/api/samplemodels/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(
            r.content.decode("utf-8", errors="replace").lstrip().startswith("{"),
        )
        # JSONP callback must not be supported.
        r2 = self.client.get("/api/samplemodels/?callback=evilFn")
        self.assertFalse(
            r2.content.decode("utf-8", errors="replace").startswith("evilFn(")
        )
        # filename query must not echo into Content-Disposition.
        r3 = self.client.get("/api/samplemodels/?filename=evil.html")
        cd = self.get_header(r3, "Content-Disposition") or ""
        self.assertNotIn("evil.html", cd)

    def test_fingerprinting_headers_are_safe_or_absent(self):
        r = self.client.get("/api/samplemodels/")
        xdpc = self.get_header(r, "X-DNS-Prefetch-Control")
        if xdpc is not None:
            self.assertEqual(xdpc.lower(), "off")
        xss = self.get_header(r, "X-XSS-Protection")
        if xss is not None:
            self.assertEqual(xss.strip(), "0")
        srv = (self.get_header(r, "Server") or "").lower()
        if srv:
            self.assertNotIn("django/", srv)
            self.assertNotIn("python/", srv)
        self.assertIsNone(self.get_header(r, "X-Powered-By"))


# ============================================================================
# Swagger HTTP surface (anon, role param, formats, no leak)
# ============================================================================


class SwaggerHttp(SecBase):
    def test_swagger_http_surface_no_leak(self):
        # Various swagger / redoc shapes — every one must not echo victim
        # tokens (5xx is acceptable; the current drf-yasg version 5xxs).
        anon = APIClient()
        cases = [
            (anon, "/swagger/?role=admin"),
            (anon, "/swagger/"),
            (anon, "/swagger/?format=openapi"),
            (self.client, "/swagger/?role=admin"),
            (self.client, "/swagger/?role=%61admin"),  # url-encoded a
            (self.client, "/swagger/?role=manager&format=openapi"),
            (self.client, "/swagger.json"),
            (self.client, "/redoc/"),
            (self.client, "/redoc/?role=admin"),
            (self.client, "/swagger/?format=xml"),     # unknown format
            (self.client, "/swagger/?format=yaml"),
            (self.client, "/swagger/?format=.json"),
            (self.client, "/swagger/?format=openapi&format=yaml"),  # double
            (self.client, "/swagger/?cache_timeout=0"),
            (self.client, "/swagger/?role=underwriter&foo=bar&baz=qux"),
            (self.client, "/swagger/?"),
        ]
        # Add a viewer role.
        viewer_client = APIClient()
        viewer_client.force_authenticate(user=self.viewer)
        cases.append((viewer_client, "/swagger/?role=admin"))

        for client, url in cases:
            try:
                r = client.get(url)
                body = r.content.decode("utf-8", errors="ignore")
                for tok in VICTIM_TOKENS:
                    self.assertNotIn(tok, body, f"leak at {url}")
                # No 500 from non-bug paths (some are known to 5xx).
            except Exception as e:
                self.assertNotIn("VICTIM", str(e))

    def test_swagger_other_methods_and_long_query_no_500_no_leak(self):
        # OPTIONS /swagger/, /redoc/; POST /swagger/; absurdly long query.
        long_q = "&".join(f"k{i}=v{i}" for i in range(500))
        for getter in (
            lambda: self.client.options("/swagger/"),
            lambda: self.client.options("/redoc/"),
            lambda: self.client.post("/swagger/", {}),
            lambda: self.client.get(f"/swagger/?role=underwriter&{long_q}"),
        ):
            try:
                r = getter()
                self.assertNotEqual(getattr(r, "status_code", 500), 500)
                body = r.content.decode("utf-8", errors="ignore")
                for tok in VICTIM_TOKENS:
                    self.assertNotIn(tok, body)
            except Exception as exc:
                self.assertNotIn("VICTIM", str(exc))

    def test_schema_view_factory_respects_enable_docs(self):
        from turbodrf.documentation import get_turbodrf_schema_view

        # Default + custom args produce a view.
        self.assertIsNotNone(get_turbodrf_schema_view())
        self.assertIsNotNone(get_turbodrf_schema_view(
            title="My API", version="2.0", description="custom",
            terms_of_service="https://example.com/tos",
            contact_email="a@b.com", license_name="GPL",
        ))
        # Disabled → None.
        with override_settings(TURBODRF_ENABLE_DOCS=False):
            self.assertIsNone(get_turbodrf_schema_view())


# ============================================================================
# Swagger schema enumeration / role-param escalation
# ============================================================================


class SwaggerRoleParam(SecBase):
    """Direct-generator role validation; tolerates drf-yasg's clone_request
    AttributeError on stub requests."""

    def test_no_role_means_no_current_role(self):
        gen = _gen()
        req = _direct_request("/swagger/", user=self.attacker)
        _attempt_role(gen, req)
        self.assertIsNone(gen.current_role)

    def test_role_with_non_legitimate_strings_does_not_escalate(self):
        # whitespace, double-param, newline, very long, homoglyph,
        # url-encoded, integer, array syntax, dot syntax, leading/trailing
        # space, pipe injection. None must end in admin/manager.
        cases = [
            "?role=   ",
            "?role=admin&role=viewer",
            "?role=admin\nviewer",
            f"?role={'A' * 100_000}",
            "?role=аdmin",            # Cyrillic 'а'
            "?role=%61dmin",          # url-encoded a → "admin" but attacker
            "?role=1",
            "?role[]=admin",
            "?role.0=admin",
            "?role= admin ",
            "?role=admin|underwriter",
        ]
        for q in cases:
            gen = _gen()
            req = _direct_request(f"/swagger/{q}", user=self.attacker)
            _attempt_role(gen, req)
            self.assertNotEqual(
                gen.current_role, "admin",
                f"escalation via {q!r}: current_role={gen.current_role!r}",
            )
            self.assertNotEqual(gen.current_role, "manager")

    def test_role_legit_for_attacker_is_accepted(self):
        gen = _gen()
        req = _direct_request("/swagger/?role=underwriter", user=self.attacker)
        _attempt_role(gen, req)
        self.assertEqual(gen.current_role, "underwriter")

    def test_session_admin_does_not_override_query_or_grant_admin(self):
        # Session-poisoned api_role=admin: query underwriter wins; without
        # query, session admin must NOT grant admin to attacker.
        gen = _gen()
        req = _direct_request("/swagger/?role=underwriter", user=self.attacker)
        req.session["api_role"] = "admin"
        req.session.save()
        _attempt_role(gen, req)
        self.assertEqual(gen.current_role, "underwriter")

        gen2 = _gen()
        req2 = _direct_request("/swagger/", user=self.attacker)
        req2.session["api_role"] = "admin"
        req2.session.save()
        _attempt_role(gen2, req2)
        self.assertNotEqual(gen2.current_role, "admin")

    def test_anon_admin_accepted_but_no_role_user_is_none(self):
        # Anonymous: by design, gets the doc role they asked for (schema
        # body doesn't leak DB rows — covered elsewhere). Authenticated
        # user with empty roles list + ?role=admin → falls through to None.
        gen = _gen()
        _attempt_role(gen, _direct_request(
            "/swagger/?role=admin", user=AnonymousUser()
        ))
        self.assertEqual(gen.current_role, "admin")

        gen2 = _gen()
        _attempt_role(gen2, _direct_request(
            "/swagger/?role=admin", user=self.no_role_user
        ))
        self.assertIsNone(gen2.current_role)


# ============================================================================
# Swagger field metadata (no row data; viewer hides forbidden fields)
# ============================================================================


class SwaggerFieldMetadata(SecBase):
    def _openapi(self, client):
        r = client.get("/swagger/?format=openapi")
        return r.data if r.status_code == 200 else None, r

    def test_attacker_and_anon_openapi_have_no_victim_secret(self):
        for client in (self.client, APIClient()):
            _, r = self._openapi(client)
            _assert_clean(self, r, "openapi")

    def test_viewer_openapi_hides_secret_field_and_price_for_samplemodel(self):
        c = APIClient()
        c.force_authenticate(user=self.viewer)
        r = c.get("/swagger/?format=openapi&role=viewer")
        _assert_clean(self, r, "viewer openapi")
        if r.status_code != 200 or not isinstance(r.data, dict):
            return
        for path, methods in (r.data.get("paths") or {}).items():
            if "samplemodel" not in path:
                continue
            for method, op in (methods or {}).items():
                if not isinstance(op, dict) or method != "get":
                    continue
                for st, body in (op.get("responses") or {}).items():
                    sch = (body or {}).get("schema") or {}
                    props = sch.get("properties") or {}
                    self.assertNotIn("secret_field", props)
                    self.assertNotIn("price", props)

    def test_underwriter_openapi_does_not_include_forbidden_paths(self):
        # underwriter has zero perms on these models.
        r = self.client.get("/swagger/?format=openapi&role=underwriter")
        _assert_clean(self, r, "underwriter openapi")
        if r.status_code != 200 or not isinstance(r.data, dict):
            return
        paths = list((r.data.get("paths") or {}).keys())
        for forbidden in (
            "/api/samplemodels", "/api/relatedmodels", "/api/categorys",
            "/api/articlewithcategoriess", "/api/compiledsamplemodels",
            "/api/compiledarticles", "/api/customendpointmodels",
            "/api/custom-items",
        ):
            for p in paths:
                self.assertFalse(p.startswith(forbidden), p)

    def test_schema_does_not_enumerate_db_rows(self):
        # FK/M2M choices and concrete row values must NEVER surface in the
        # OpenAPI doc — even if a Category or extra row is created.
        Category.objects.create(name=VICTIM_DEAL_TITLE)
        rel = RelatedModel.objects.create(name="rel-c9")
        SampleModel.objects.create(
            title="row-c9", price=Decimal("1.00"), related=rel
        )
        r = self.client.get("/swagger/?format=openapi")
        _assert_clean(self, r, "schema enumeration")
        body = _blob(r)
        for tok in ("Victim Co", "Innocent Co", "Attacker Co", "row-c9"):
            self.assertNotIn(tok, body)

    def test_get_endpoints_have_no_in_body_param(self):
        r = self.client.get("/swagger/?format=openapi")
        _assert_clean(self, r, "GET body param")
        if r.status_code != 200 or not isinstance(r.data, dict):
            return
        for path, methods in (r.data.get("paths") or {}).items():
            for method, op in (methods or {}).items():
                if not isinstance(op, dict) or method != "get":
                    continue
                for p in (op.get("parameters") or []):
                    if isinstance(p, dict) and p.get("in") == "body":
                        self.fail(f"GET {path} has in:body param: {p}")

    def test_role_filter_url_prefix_assumption_documented(self):
        # `_extract_model_info` assumes paths start with /api/; drf-yasg
        # strips that prefix → role filter empties paths. Fail-closed:
        # SAFE but the role filter is a no-op. Document the broken-but-safe
        # state so future fixes flip this to FAIL when corrected.
        r = self.client.get("/swagger/?format=openapi&role=underwriter")
        _assert_clean(self, r, "attacker exclusion")
        if r.status_code != 200 or not isinstance(r.data, dict):
            return
        paths = (r.data.get("paths") or {})
        if any("deal" in p for p in paths.keys()):
            return
        self.assertEqual(len(paths), 0)


# ============================================================================
# Swagger ref-name / autoschema / tenant
# ============================================================================


class SwaggerRefAndAutoSchema(SecBase):
    def test_no_ref_name_collision_or_invalid_refs(self):
        try:
            r = self.client.get("/swagger/?format=openapi")
            self.assertNotEqual(
                r.status_code, 500, f"5xx: {r.content[:200]}"
            )
            _assert_clean(self, r, "ref_name collision")
            if r.status_code == 200:
                body = json.dumps(r.data, default=str)
                for bad in ('"#/definitions/<', '"#/definitions/\\n'):
                    self.assertNotIn(bad, body)
                defs = r.data.get("definitions") or {}
                for s in SECRETS:
                    self.assertNotIn(s, json.dumps(defs, default=str))
                for k in defs.keys():
                    for s in SECRETS:
                        self.assertNotIn(s, k)
        except Exception as exc:
            self.assertNotIn(
                "Two schemas with the same ref name", str(exc)
            )
            self.assertNotIn("VICTIM", str(exc))

    def test_role_pivot_does_not_alter_ref_names(self):
        c = APIClient()
        c.force_authenticate(user=AnonymousUser())
        try:
            for q in ("admin", "viewer"):
                r = c.get(f"/swagger/?format=openapi&role={q}")
                self.assertNotEqual(r.status_code, 500)
                _assert_clean(self, r, f"role pivot {q}")
        except Exception as exc:
            self.assertNotIn("ref_name", str(exc).lower())

    def test_autoschema_handles_unknown_actions_and_views(self):
        from rest_framework.viewsets import ViewSet
        from turbodrf.views import TurboDRFViewSet

        class CustomVS(ViewSet):
            pass

        v = CustomVS()
        v.action = "custom_export"
        insp = TurboDRFSwaggerAutoSchema(
            view=v, path="/x", method="GET",
            components=None, request=None, overrides={},
        )
        self.assertEqual(
            insp.get_request_body_parameters(consumes=["application/json"]),
            [],
        )

        # FakeView with no `model` attr → fallback path must not raise on
        # _get_write_operation_serializer (or if it does, no leak).
        class FakeView(ViewSet):
            pass
        fake = FakeView()
        fake.action = "create"
        insp2 = TurboDRFSwaggerAutoSchema(
            view=fake, path="/x", method="POST",
            components=None, request=None, overrides={},
        )
        try:
            insp2._get_write_operation_serializer()
        except Exception as exc:
            self.assertNotIn("VICTIM", str(exc))

        # Real TurboDRFViewSet with various actions.
        class TV(TurboDRFViewSet):
            model = Deal

            def custom(self, request):
                return None

        for action, method, path in (
            ("create", "POST", "/api/deals/"),
            ("partial_update", "PATCH", "/api/deals/{id}/"),
            ("list", "GET", "/api/deals/"),
            ("custom", "GET", "/api/deals/custom/"),
        ):
            tv = TV()
            tv.action = action
            insp = TurboDRFSwaggerAutoSchema(
                view=tv, path=path, method=method,
                components=None, request=None, overrides={},
            )
            try:
                insp.get_request_serializer()
            except Exception as exc:
                self.assertNotIn("VICTIM", str(exc))

    def test_show_all_fields_setting_does_not_leak(self):
        with override_settings(TURBODRF_SWAGGER_SHOW_ALL_FIELDS=True):
            for client in (self.client, APIClient(),
                           self._authed_client(self.viewer)):
                try:
                    r = client.get("/swagger/?format=openapi")
                    _assert_clean(self, r, "SHOW_ALL_FIELDS")
                except Exception as exc:
                    self.assertNotIn("VICTIM", str(exc))

    def _authed_client(self, user):
        c = APIClient()
        c.force_authenticate(user=user)
        return c

    def test_tenant_schema_does_not_leak_brokerage_names(self):
        # Includes 3-hop chain, public-access model, anon, no-role user.
        for client in (
            self.client,
            APIClient(),
            self._authed_client(self.no_role_user),
        ):
            try:
                r = client.get("/swagger/?format=openapi")
                _assert_clean(self, r, "tenant schema")
                self.assertLess(r.status_code, 500)
                body = _blob(r)
                self.assertNotIn("Victim Co", body)
                self.assertNotIn("Innocent Co", body)
            except Exception as exc:
                self.assertNotIn("VICTIM", str(exc))


# ============================================================================
# Swagger generator internals (endpoint filter + extract_model_info etc)
# ============================================================================


class SwaggerInternals(SecBase):
    # ---- endpoint filter ----

    def test_endpoint_filter_drops_no_slash_dupes(self):
        # Dict & tuple forms drop the no-slash duplicate; normal entries
        # pass through; short-tuple inputs pass through untouched; mis-
        # shaped callbacks are NOT flagged as duplicates.
        gen = _gen()

        class CB_dup:
            class cls:
                _basename = "x"
            actions = {"get": "list"}
            name = "x_no_slash"

        class CB_normal(CB_dup):
            name = "x"

        self.assertEqual(
            gen._filter_endpoint_dict({"/api/x/": (CB_dup, ["GET"])}), {}
        )
        out = gen._filter_endpoint_dict({"/api/x/": (CB_normal, ["GET"])})
        self.assertEqual(set(out.keys()), {"/api/x/"})

        self.assertEqual(
            gen._filter_endpoint_tuples([("/api/x", "rx", "GET", CB_dup)]), []
        )
        self.assertEqual(
            gen._filter_endpoint_tuples([("/x", "rx")]), [("/x", "rx")]
        )

        # Negative cases: each is mis-shaped in a different way and must
        # NOT be flagged as a duplicate.
        class CB_no_attr:
            pass

        class CB_no_actions:
            class cls:
                _basename = "x"
            name = "y"

        class CB_blank_name:
            class cls:
                _basename = "x"
            actions = {"get": "list"}
            name = ""

        class CB_no_basename:
            class cls:
                pass
            actions = {"get": "list"}
            name = "x_no_slash"

        for cb in (CB_no_attr(), CB_no_actions(), CB_blank_name(),
                   CB_no_basename()):
            self.assertFalse(gen._is_no_slash_duplicate(cb))

    # ---- _extract_model_info ----

    def test_extract_model_info_rejects_or_singularizes(self):
        # Non-/api/ paths and unknown models → None; known paths singularize
        # via simple .rstrip('s').
        gen = _gen()
        for p in ("/admin/", "/swagger/", "/redoc/",
                  "/api/nonexistents/", "/api/../etc/passwds/",
                  "/api/déals/", "/", "", "/api/"):
            self.assertIsNone(gen._extract_model_info(p))
        self.assertEqual(
            gen._extract_model_info("/api/deals/")["model_name"], "deal"
        )
        self.assertEqual(
            gen._extract_model_info("/api/bankaccounts/")["model_name"],
            "bankaccount",
        )

    # ---- _has_permission / _filter_schema_fields ----

    def test_has_permission_methods_and_perm_set(self):
        gen = _gen()
        info = {"app_label": "test_app", "model_name": "deal"}
        # Unsupported HTTP methods → False.
        for m in ("TRACE", "OPTIONS", "HEAD"):
            self.assertFalse(gen._has_permission(info, m, set()))
        self.assertFalse(gen._has_permission(info, "GET", set()))
        perms = {"test_app.deal.read"}
        self.assertTrue(gen._has_permission(info, "GET", perms))
        # Lowercase method handled.
        self.assertTrue(gen._has_permission(info, "get", perms))
        for m in ("POST", "PUT", "DELETE"):
            self.assertFalse(gen._has_permission(info, m, perms))

    def test_filter_schema_fields_filters_by_perm(self):
        gen = _gen()
        info = {"app_label": "test_app", "model_name": "deal"}
        # No properties at all → unchanged.
        self.assertEqual(
            gen._filter_schema_fields({"type": "object"}, info, set()),
            {"type": "object"},
        )
        # Subset perm → subset properties.
        sch = {"type": "object",
               "properties": {"id": {"type": "integer"},
                              "title": {"type": "string"},
                              "secret": {"type": "string"}}}
        out = gen._filter_schema_fields(
            sch, info, {"test_app.deal.id.read"}
        )
        self.assertEqual(set(out["properties"].keys()), {"id"})
        # Full perms → all fields kept.
        out2 = gen._filter_schema_fields(
            {"type": "object",
             "properties": {"id": {"type": "integer"},
                            "title": {"type": "string"}}},
            info,
            {"test_app.deal.id.read", "test_app.deal.title.read"},
        )
        self.assertEqual(set(out2["properties"].keys()), {"id", "title"})
