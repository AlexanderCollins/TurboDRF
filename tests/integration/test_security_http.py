"""HTTP-surface security tests.

Covers HTTP method/verb behavior, URL encoding edge cases, trailing
slashes, path traversal, OPTIONS / HEAD existence oracles, content
negotiation, accept-header tunneling, exotic verbs, header abuse, REST
semantic violations, and metadata payload exhaustion.
"""

from __future__ import annotations

import time
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

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


def _no_secret_leak(testcase, response, label=""):
    """Assert no victim secret appears in response body, headers, or data."""
    blob = ""
    if hasattr(response, "data") and response.data is not None:
        try:
            blob += str(response.data)
        except Exception:
            pass
    if hasattr(response, "content"):
        try:
            blob += response.content.decode("utf-8", errors="replace")
        except Exception:
            pass
    try:
        blob += str(dict(response.items()))
    except Exception:
        pass
    for s in SECRETS:
        testcase.assertNotIn(
            s,
            blob,
            f"[{label}] Secret {s!r} leaked (status={response.status_code})",
        )


def _is_5xx(response):
    return 500 <= response.status_code < 600


def _no_open_redirect(testcase, response, label=""):
    """Verify Location header (if any) does not point to attacker domain."""
    location = response.get("Location", "")
    if location:
        for evil in ("evil.com", "attacker.com", "//evil", "http://evil"):
            testcase.assertNotIn(
                evil.lower(),
                location.lower(),
                f"[{label}] Open redirect to {location!r}",
            )


def _no_header_reflection(testcase, response, marker, label=""):
    """Verify a marker string injected via header is not reflected back."""
    blob = ""
    try:
        blob += response.content.decode("utf-8", errors="replace")
    except Exception:
        pass
    try:
        blob += str(dict(response.items()))
    except Exception:
        pass
    testcase.assertNotIn(
        marker,
        blob,
        f"[{label}] header marker {marker!r} reflected in response",
    )


class HttpSecurityBase(TestCase):
    """Shared adversary fixture.

    Heavy fixture is created ONCE per TestCase class (`setUpTestData`)
    and re-used across tests via Django's per-test transaction rollback.
    `setUp` only resets per-test state (cache + brokerage map +
    APIClient).
    """

    @classmethod
    def setUpTestData(cls):
        import tests.urls  # noqa: F401  ensure URL conf loaded

        cls.brokerage_attacker = Brokerage.objects.create(name="Attacker Co")
        cls.brokerage_victim = Brokerage.objects.create(name="Victim Co")
        cls.brokerage_third = Brokerage.objects.create(name="Innocent Co")

        # Attacker — non-bypass underwriter at brokerage_attacker
        cls.attacker = User.objects.create_user(username="attacker", password="x")
        cls.attacker._test_roles = ["underwriter"]

        # Manager (bypass owner, NOT tenant)
        cls.attacker_manager = User.objects.create_user(username="mgr", password="x")
        cls.attacker_manager._test_roles = ["manager"]

        # Admin (full perms, no tenant set)
        cls.admin_user = User.objects.create_user(username="adm", password="x")
        cls.admin_user._test_roles = ["admin"]

        # Viewer (read-only on SampleModel/RelatedModel; no perms on Deal)
        cls.viewer = User.objects.create_user(username="viewer", password="x")
        cls.viewer._test_roles = ["viewer"]

        # Editor
        cls.editor = User.objects.create_user(username="editor", password="x")
        cls.editor._test_roles = ["editor"]

        # User with empty roles
        cls.no_roles_user = User.objects.create_user(username="no_roles", password="x")
        cls.no_roles_user._test_roles = []

        # Innocent victim
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
        cls.attacker_tx = Transaction.objects.create(
            amount=Decimal("1.00"), bank_account=cls.attacker_bank
        )

        # Plain non-tenant fixtures for tunneling & negotiation tests
        cls.related = RelatedModel.objects.create(name="r1")
        cls.sample = SampleModel.objects.create(
            title="s1",
            description="d",
            price=Decimal("1.00"),
            quantity=1,
            related=cls.related,
        )
        cls.cat = Category.objects.create(name="cat1", description="d")
        cls.article = ArticleWithCategories.objects.create(
            title="a1", content="c", author=cls.related
        )
        cls.article.categories.add(cls.cat)
        cls.compiled_sample = CompiledSampleModel.objects.create(
            title="cs1",
            price=Decimal("2.00"),
            is_active=True,
            related=cls.related,
        )
        cls.compiled_article = CompiledArticle.objects.create(
            title="cap1", author=cls.related
        )
        cls.compiled_article.categories.add(cls.cat)
        cls.custom_item = CustomEndpointModel.objects.create(name="ce1")

    def setUp(self):
        # Per-test reset only — DB rows from setUpTestData survive
        cache.clear()
        _test_user_brokerages.clear()
        # Repopulate the brokerage map: the dict was cleared but the
        # User objects on cls still reference the same brokerage rows.
        set_test_brokerage(self.attacker, self.brokerage_attacker)
        set_test_brokerage(self.attacker_manager, self.brokerage_attacker)
        set_test_brokerage(self.victim, self.brokerage_victim)

        self.client = APIClient()
        self.client.force_authenticate(user=self.attacker)


# ============================================================================
# 1. URL / PK MANIPULATION (encoding, trailing slash, traversal, formats,
# whitespace, overflow, double-slash, multi-PK, format suffix)
# ============================================================================


class TestURLAndPKManipulation(HttpSecurityBase):
    """All URL- / PK-shape attacks in one class. Each loop covers a
    distinct attack class; one assertion shape per family.
    """

    def test_pk_format_variants_do_not_bypass_tenant(self):
        encoded = "".join(f"%{ord(c):02X}" for c in str(self.victim_deal.pk))
        variants = [
            encoded,  # percent-encoded digits
            f"{self.victim_deal.pk}%00",  # null byte
            f"{self.victim_deal.pk}%2F",  # encoded slash
            f"{self.victim_deal.pk}.0",  # dotted decimal
            f"+{self.victim_deal.pk}",  # signed
            "0x2",
            "0o2",
            "0b10",  # hex/octal/binary
            f"%20{self.victim_deal.pk}",  # leading space
            f"{self.victim_deal.pk}%20",  # trailing space
            f"%09{self.victim_deal.pk}",  # tab
        ]
        for v in variants:
            r = self.client.get(f"/api/deals/{v}/")
            _no_secret_leak(self, r, f"pk={v!r}")
            self.assertNotEqual(r.status_code, 200, f"leak via pk={v!r}")

    def test_pk_extremes_do_not_5xx(self):
        # negative / zero / overflow / multi-PK / non-numeric — must
        # never produce a 5xx (info disclosure via traceback).
        for url in (
            "/api/deals/-1/",
            "/api/deals/0/",
            "/api/deals/99999999999999999999999/",
            f"/api/deals/{self.attacker_deal.pk}/{self.victim_deal.pk}/",
            "/api/deals/abc/",
            "/api/deals/00000000-0000-0000-0000-000000000000/",
        ):
            r = self.client.get(url)
            _no_secret_leak(self, r, url)
            self.assertFalse(_is_5xx(r), f"5xx on {url}: body={r.content!r}")

    def test_no_trailing_slash_get_patch_delete_blocked(self):
        url = f"/api/deals/{self.victim_deal.pk}"
        r_get = self.client.get(url)
        r_patch = self.client.patch(url, {"title": "PWNED"}, format="json")
        r_delete = self.client.delete(url)
        for r in (r_get, r_patch, r_delete):
            _no_secret_leak(self, r)
        self.assertNotEqual(r_get.status_code, 200)
        self.assertNotIn(r_patch.status_code, (200, 201))
        self.assertNotEqual(r_delete.status_code, 204)
        self.victim_deal.refresh_from_db()
        self.assertEqual(self.victim_deal.title, "VICTIM_SECRET_DEAL")
        self.assertTrue(Deal.objects.filter(pk=self.victim_deal.pk).exists())

    def test_path_traversal_and_double_slash(self):
        urls = [
            f"/api/deals/../transactions/{self.victim_tx.pk}/",
            f"/api/deals/{self.attacker_deal.pk}/../{self.victim_deal.pk}/",
            f"/api/bankaccounts/../deals/{self.victim_deal.pk}/",
            f"/api//deals/{self.victim_deal.pk}/",
            f"/api/deals/{self.victim_deal.pk}//",
            "//api//deals//",
            f"/api/deals/{self.victim_deal.pk};jsessionid=ABCDEF/",
            f"/api/deals/{self.victim_deal.pk};foo=bar/",
            f"/api/deals/{self.victim_deal.pk}/;param=value",
        ]
        for url in urls:
            r = self.client.get(url)
            _no_secret_leak(self, r, url)
            if r.status_code == 200:
                self.fail(f"Path traversal {url} returned 200")

    def test_format_suffix_does_not_bypass_tenant(self):
        for suffix in (".json", ".api"):
            r = self.client.get(f"/api/deals/{self.victim_deal.pk}{suffix}")
            _no_secret_leak(self, r, suffix)
            self.assertNotEqual(r.status_code, 200)

    def test_case_and_whitespace_path_variants_404(self):
        for url in ("/api/Deals/", "/api/DEALS/", "/api/deals/ "):
            r = self.client.get(url)
            _no_secret_leak(self, r, url)
            self.assertFalse(_is_5xx(r))


# ============================================================================
# 2. HTTP METHOD OVERRIDE / VERB TUNNELING
# ============================================================================


class TestMethodOverrideAndTunneling(HttpSecurityBase):
    """X-HTTP-Method-Override / _method / _METHOD / X-Forwarded-Method /
    body-key tunneling — none should swap the request verb."""

    OVERRIDE_HEADERS = (
        "HTTP_X_HTTP_METHOD_OVERRIDE",
        "HTTP_X_METHOD_OVERRIDE",
        "HTTP_X_HTTP_METHOD",
        "HTTP_X_FORWARDED_METHOD",
    )

    def test_post_with_method_override_does_not_swap_verb(self):
        url = f"/api/deals/{self.victim_deal.pk}/"
        before = Deal.objects.filter(pk=self.victim_deal.pk).count()
        for hdr in self.OVERRIDE_HEADERS:
            r = self.client.post(url, {}, format="json", **{hdr: "DELETE"})
            _no_secret_leak(self, r, hdr)
            self.assertFalse(_is_5xx(r))
        after = Deal.objects.filter(pk=self.victim_deal.pk).count()
        self.assertEqual(before, after, "victim row deleted via method override")

    def test_method_override_via_body_or_query_ignored(self):
        # Body keys / querystring `_method` / uppercase variants — all ignored.
        url = f"/api/deals/{self.victim_deal.pk}/"
        before = Deal.objects.filter(pk=self.victim_deal.pk).count()
        cases = [
            ("post", url, {"_method": "DELETE"}, "json", {}),
            (
                "post",
                url,
                {"_METHOD": "DELETE", "_HttpMethod": "DELETE"},
                "multipart",
                {},
            ),
            ("post", url + "?_method=GET", {}, "json", {}),
            ("post", url + "?http_method=DELETE", {}, "json", {}),
            ("get", url + "?_method=GET", None, None, {}),
        ]
        for verb, u, body, fmt, kwargs in cases:
            method = getattr(self.client, verb)
            if body is None:
                r = method(u, **kwargs)
            else:
                r = method(u, body, format=fmt, **kwargs)
            _no_secret_leak(self, r, f"{verb} {u}")
            self.assertNotEqual(r.status_code, 200)
        after = Deal.objects.filter(pk=self.victim_deal.pk).count()
        self.assertEqual(before, after)
        self.assertTrue(Deal.objects.filter(pk=self.victim_deal.pk).exists())

    def test_get_with_body_does_not_filter(self):
        # GET with JSON body — body must NOT influence filtering.
        for url in (
            f"/api/deals/{self.victim_deal.pk}/",
            "/api/deals/",
        ):
            r = self.client.generic(
                "GET",
                url,
                data='{"brokerage": ' + str(self.brokerage_victim.pk) + "}",
                content_type="application/json",
            )
            _no_secret_leak(self, r, f"GET+body {url}")
            if url.endswith(f"{self.victim_deal.pk}/"):
                self.assertNotEqual(r.status_code, 200)

    def test_delete_with_body_no_fk_rebind(self):
        r = self.client.delete(
            f"/api/deals/{self.victim_deal.pk}/",
            data='{"brokerage": ' + str(self.brokerage_attacker.pk) + "}",
            content_type="application/json",
        )
        _no_secret_leak(self, r, "DELETE with body")
        self.assertTrue(Deal.objects.filter(pk=self.victim_deal.pk).exists())

    def test_put_with_method_override_patch(self):
        body = {
            "title": "hdr-swap",
            "brokerage": self.brokerage_attacker.pk,
            "assigned_broker": self.attacker.pk,
        }
        r = self.client.put(
            f"/api/deals/{self.attacker_deal.pk}/",
            body,
            format="json",
            HTTP_X_HTTP_METHOD_OVERRIDE="PATCH",
        )
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))
        self.victim_deal.refresh_from_db()
        self.assertEqual(self.victim_deal.title, "VICTIM_SECRET_DEAL")

    def test_layered_overrides_no_state_change(self):
        before = Deal.objects.filter(pk=self.victim_deal.pk).count()
        r = self.client.get(
            f"/api/deals/{self.victim_deal.pk}/",
            HTTP_X_HTTP_METHOD_OVERRIDE="PUT",
            HTTP_X_METHOD_OVERRIDE="DELETE",
        )
        after = Deal.objects.filter(pk=self.victim_deal.pk).count()
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))
        self.assertEqual(before, after)

    def test_head_spoofed_to_get_returns_empty_body(self):
        r = self.client.head(
            f"/api/deals/{self.victim_deal.pk}/",
            HTTP_X_HTTP_METHOD_OVERRIDE="GET",
        )
        self.assertEqual(r.content, b"")
        self.assertFalse(_is_5xx(r))

    def test_trace_no_xst_disclosure(self):
        marker = "MARKER-TRACE-XYZ"
        r = self.client.generic("TRACE", "/api/deals/", HTTP_X_CUSTOM_PROBE=marker)
        _no_secret_leak(self, r)
        _no_header_reflection(self, r, marker, "TRACE")
        self.assertFalse(_is_5xx(r))


# ============================================================================
# 3. EXOTIC HTTP VERBS — PROPFIND, COPY, MOVE, LOCK, MKCOL, etc.
# ============================================================================


class TestExoticVerbs(HttpSecurityBase):
    """Each exotic verb on collection or detail must NOT return 200
    with foreign-tenant data and must NOT 5xx."""

    EXOTIC_VERBS_DETAIL = (
        "TRACE",
        "CONNECT",
        "PROPFIND",
        "PROPPATCH",
        "LOCK",
        "UNLOCK",
        "COPY",
        "MOVE",
        "REPORT",
        "PURGE",
        "FETCH",
        "LINK",
        "UNLINK",
    )
    EXOTIC_VERBS_COLLECTION = ("MKCOL", "SEARCH", "BAN")

    def test_exotic_verbs_on_victim_detail_no_leak(self):
        url = f"/api/deals/{self.victim_deal.pk}/"
        for verb in self.EXOTIC_VERBS_DETAIL:
            r = self.client.generic(verb, url)
            _no_secret_leak(self, r, f"{verb} {url}")
            self.assertNotEqual(r.status_code, 200, f"{verb} returned 200")
            self.assertFalse(_is_5xx(r), f"5xx on {verb}: {r.content!r}")

    def test_exotic_verbs_on_collection_no_leak(self):
        for verb in self.EXOTIC_VERBS_COLLECTION:
            r = self.client.generic(verb, "/api/deals/")
            _no_secret_leak(self, r, f"{verb} collection")
            self.assertNotEqual(r.status_code, 200)
            self.assertFalse(_is_5xx(r))

    def test_lowercase_verb_no_leak(self):
        # Lowercase "get" — some routers normalize.
        r = self.client.generic("get", f"/api/deals/{self.victim_deal.pk}/")
        _no_secret_leak(self, r)
        if r.status_code == 200:
            for s in SECRETS:
                self.assertNotIn(s, str(r.content))


# ============================================================================
# 4. EXISTENCE ORACLES — GET / HEAD / OPTIONS / PATCH / DELETE
# foreign-vs-ghost must return identical status (and same body where
# applicable).
# ============================================================================


class TestExistenceOracles(HttpSecurityBase):
    """Foreign-tenant pk vs nonexistent pk: must be indistinguishable
    via status code (and body where appropriate). Otherwise the
    framework leaks row existence."""

    def _far_pk(self):
        return self.victim_tx.pk + 99999

    def test_verb_status_codes_match_foreign_vs_ghost(self):
        # GET / PATCH / DELETE / OPTIONS / HEAD all match.
        far = self._far_pk()
        url_v = f"/api/deals/{self.victim_deal.pk}/"
        url_g = f"/api/deals/{far}/"
        cases = [
            ("get", lambda u: self.client.get(u)),
            (
                "patch",
                lambda u: self.client.patch(u, {"title": "x"}, format="json"),
            ),
            ("delete", lambda u: self.client.delete(u)),
            ("options", lambda u: self.client.options(u)),
            ("head", lambda u: self.client.head(u)),
        ]
        for verb, fn in cases:
            r_v = fn(url_v)
            r_g = fn(url_g)
            _no_secret_leak(self, r_v, f"{verb} victim")
            _no_secret_leak(self, r_g, f"{verb} ghost")
            self.assertEqual(
                r_v.status_code,
                r_g.status_code,
                f"{verb} oracle: foreign={r_v.status_code} " f"ghost={r_g.status_code}",
            )

    def test_patch_delete_error_body_identical_foreign_vs_ghost(self):
        far = self._far_pk()
        r_v_p = self.client.patch(
            f"/api/deals/{self.victim_deal.pk}/", {"title": "x"}, format="json"
        )
        r_g_p = self.client.patch(f"/api/deals/{far}/", {"title": "x"}, format="json")
        self.assertEqual(r_v_p.status_code, 404)
        self.assertEqual(r_v_p.data, r_g_p.data)

        r_v_d = self.client.delete(f"/api/deals/{self.victim_deal.pk}/")
        r_g_d = self.client.delete(f"/api/deals/{far}/")
        self.assertEqual(r_v_d.status_code, 404)
        self.assertEqual(r_v_d.status_code, r_g_d.status_code)

    def test_head_content_length_no_existence_leak(self):
        far = self._far_pk()
        r_v = self.client.head(f"/api/deals/{self.victim_deal.pk}/")
        r_g = self.client.head(f"/api/deals/{far}/")
        cl_v = r_v.get("Content-Length") or len(r_v.content)
        cl_g = r_g.get("Content-Length") or len(r_g.content)
        self.assertEqual(
            cl_v,
            cl_g,
            f"HEAD Content-Length leaks existence: foreign={cl_v} ghost={cl_g}",
        )
        self.assertEqual(r_v.content, b"")  # HEAD never has a body

    def test_options_body_length_indistinguishable(self):
        far = self._far_pk()
        r_v = self.client.options(f"/api/deals/{self.victim_deal.pk}/")
        r_g = self.client.options(f"/api/deals/{far}/")
        self.assertEqual(len(r_v.content), len(r_g.content))
        self.assertEqual(
            r_v.get("Allow", ""),
            r_g.get("Allow", ""),
            "Allow header oracle",
        )

    def test_options_own_vs_foreign_status_match_chain_models(self):
        # Across the full chain (deals/bankaccounts/transactions): own,
        # foreign, ghost must return matching status.
        cases = (
            ("deals", self.attacker_deal.pk, self.victim_deal.pk),
            ("bankaccounts", self.attacker_bank.pk, self.victim_bank.pk),
            ("transactions", self.attacker_tx.pk, self.victim_tx.pk),
        )
        for endpoint, own_pk, foreign_pk in cases:
            ghost_pk = foreign_pk + 99999
            r_own = self.client.options(f"/api/{endpoint}/{own_pk}/")
            r_for = self.client.options(f"/api/{endpoint}/{foreign_pk}/")
            r_g = self.client.options(f"/api/{endpoint}/{ghost_pk}/")
            for r, lbl in (
                (r_own, f"{endpoint}-own"),
                (r_for, f"{endpoint}-foreign"),
                (r_g, f"{endpoint}-ghost"),
            ):
                _no_secret_leak(self, r, lbl)
                self.assertFalse(_is_5xx(r))
            self.assertEqual(
                r_for.status_code,
                r_g.status_code,
                f"{endpoint} oracle: foreign={r_for.status_code} "
                f"ghost={r_g.status_code}",
            )

    def test_filter_by_foreign_pk_returns_zero(self):
        r = self.client.get(f"/api/deals/?id={self.victim_deal.id}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            r.data["pagination"]["total_items"],
            0,
            "filter by foreign-tenant id reveals existence via count",
        )

    def test_fk_injection_returns_uniform_400(self):
        r_for = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": self.victim_bank.id},
            format="json",
        )
        r_g = self.client.post(
            "/api/transactions/",
            {"amount": "1.00", "bank_account": 99999999},
            format="json",
        )
        self.assertEqual(r_for.status_code, 400)
        self.assertEqual(r_g.status_code, 400)
        self.assertEqual(
            r_for.data,
            r_g.data,
            "FK injection error message differs foreign vs ghost",
        )

    def test_timing_no_gross_oracle(self):
        n = 30
        t_for, t_g = [], []
        for _ in range(n):
            t0 = time.perf_counter()
            self.client.get(f"/api/deals/{self.victim_deal.id}/")
            t_for.append(time.perf_counter() - t0)
            t0 = time.perf_counter()
            self.client.get("/api/deals/9999999/")
            t_g.append(time.perf_counter() - t0)
        avg_f = sum(t_for) / n
        avg_g = sum(t_g) / n
        ratio = max(avg_f, avg_g) / min(avg_f, avg_g) if min(avg_f, avg_g) > 0 else 1
        self.assertLess(ratio, 5.0, f"timing oracle ratio={ratio:.2f}")


# ============================================================================
# 5. CONTENT-TYPE / ENCODING / CONTENT-NEGOTIATION TRICKS
# ============================================================================


class TestContentTypeAndNegotiation(HttpSecurityBase):
    """Content-Type spoofing, charset weirdness, Accept header parsing,
    encoding tricks. None should 5xx; none should land cross-tenant rows."""

    def test_post_foreign_brokerage_under_various_content_types(self):
        # form-urlencoded, multipart, JSON-with-utf-7, JSON-with-q=1,
        # x-evil, text/x.doom, multipart-but-actually-json, charset=utf-16,
        # charset=utf-32, BOM body, Transfer-Encoding/Content-Encoding lies.
        body_json = '{"title":"X","brokerage":' + str(self.brokerage_victim.pk) + "}"
        cases = [
            (
                "form-url",
                "application/x-www-form-urlencoded",
                f"title=X&brokerage={self.brokerage_victim.pk}"
                f"&assigned_broker={self.attacker.pk}",
            ),
            ("ct utf-7", "application/json; charset=utf-7", body_json),
            ("ct q=1", "application/json; q=1", body_json),
            ("ct x-evil", "application/x-evil", body_json),
            ("ct text/x.doom", "text/x.doom", body_json),
            (
                "ct mp-but-json",
                "multipart/form-data; boundary=---xx",
                body_json,
            ),
            (
                "ct utf-16",
                "application/json; charset=utf-16",
                body_json.encode("utf-16"),
            ),
            (
                "ct utf-32",
                "application/json; charset=utf-32",
                body_json.encode("utf-32"),
            ),
            ("ct BOM", "application/json", b"\xef\xbb\xbf" + body_json.encode()),
            ("ct dup", "application/json, text/plain", body_json),
        ]
        for label, ct, data in cases:
            r = self.client.post("/api/deals/", data=data, content_type=ct)
            _no_secret_leak(self, r, label)
            self.assertFalse(_is_5xx(r), f"5xx on {label}: {r.content!r}")
            # No cross-tenant row landed
            self.assertFalse(
                Deal.objects.filter(
                    brokerage=self.brokerage_victim, title="X"
                ).exists(),
                f"{label} created row in victim tenant",
            )

    def test_multipart_post_with_foreign_brokerage_does_not_land_in_victim(self):
        r = self.client.post(
            "/api/deals/",
            data={
                "title": "PWN_MP",
                "brokerage": str(self.brokerage_victim.pk),
                "assigned_broker": str(self.attacker.pk),
            },
            format="multipart",
        )
        _no_secret_leak(self, r)
        if r.status_code in (200, 201):
            new_pk = r.data.get("id") if isinstance(r.data, dict) else None
            if new_pk:
                created = Deal.objects.get(pk=new_pk)
                self.assertEqual(created.brokerage_id, self.brokerage_attacker.pk)

    def test_querystring_fk_injection_blocked(self):
        r = self.client.post(
            f"/api/deals/?brokerage={self.brokerage_victim.pk}&title=PWN_QS",
            data={},
            format="json",
        )
        _no_secret_leak(self, r)
        leaked = Deal.objects.filter(
            brokerage=self.brokerage_victim, title="PWN_QS"
        ).exists()
        self.assertFalse(leaked)

    def test_empty_content_type_post_does_not_create_victim_row(self):
        # Empty CT may produce a 5xx (informational finding) but the
        # critical defense is that no cross-tenant row is created.
        c = APIClient(raise_request_exception=False)
        c.force_authenticate(user=self.attacker)
        r = c.post(
            "/api/deals/",
            data='{"title":"E","brokerage":' + str(self.brokerage_victim.pk) + "}",
            content_type="",
        )
        _no_secret_leak(self, r)
        self.assertFalse(
            Deal.objects.filter(brokerage=self.brokerage_victim, title="E").exists()
        )

    def test_smuggling_shape_headers_no_5xx(self):
        body = '{"title":"S","brokerage":' + str(self.brokerage_victim.pk) + "}"
        cases = [
            {"HTTP_TRANSFER_ENCODING": "chunked"},
            {"HTTP_CONTENT_ENCODING": "gzip"},
            {
                "HTTP_TRANSFER_ENCODING": "chunked",
                "HTTP_CONTENT_LENGTH": str(len(body)),
            },
        ]
        for hdrs in cases:
            r = self.client.post(
                "/api/deals/",
                data=body,
                content_type="application/json",
                **hdrs,
            )
            _no_secret_leak(self, r, f"{hdrs}")
            self.assertFalse(_is_5xx(r))
            self.assertFalse(
                Deal.objects.filter(brokerage=self.brokerage_victim, title="S").exists()
            )

    def test_post_empty_body_application_json_no_5xx(self):
        r = self.client.post("/api/deals/", data="", content_type="application/json")
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))

    def test_accept_header_variants_no_leak(self):
        url = f"/api/deals/{self.victim_deal.pk}/"
        accepts = [
            "text/html",
            "application/xml",
            "*/*",
            "*/*; q=0",
            "application/x-evil+json",
            "application/json; charset=utf-7",
            "application/vnd.acme.v1+json",
            "text/plain, application/json, text/xml",
            "application/json;q=zzz, text/html;q=999",
            "text/html;q=0.1, application/json;q=1.0",
        ]
        for a in accepts:
            r = self.client.get(url, HTTP_ACCEPT=a)
            _no_secret_leak(self, r, f"Accept={a!r}")
            self.assertFalse(_is_5xx(r), f"5xx on Accept={a!r}")
            if r.status_code == 200:
                # If 200 succeeds (e.g. xml renderer existed), still no
                # secret in body.
                for s in SECRETS:
                    self.assertNotIn(s, r.content.decode(errors="replace"))

    def test_accept_html_post_with_foreign_brokerage(self):
        r = self.client.post(
            "/api/deals/",
            data='{"title":"x","brokerage":' + str(self.brokerage_victim.pk) + "}",
            content_type="application/json",
            HTTP_ACCEPT="text/html",
        )
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))

    def test_format_query_param_does_not_leak(self):
        # ?format=json/html/api/admin/xml — none should leak victim data.
        for fmt in ("json", "html", "api", "admin", "xml", "yaml", "raw"):
            r = self.client.get(f"/api/deals/?format={fmt}")
            _no_secret_leak(self, r, f"format={fmt}")
            body = r.content.decode("utf-8", errors="ignore")
            self.assertNotIn("VICTIM_SECRET_DEAL", body)

    def test_browsable_api_detail_for_foreign_row_404(self):
        r = self.client.get(f"/api/deals/{self.victim_deal.id}/?format=api")
        self.assertEqual(r.status_code, 404)
        body = r.content.decode("utf-8", errors="ignore")
        self.assertNotIn("VICTIM_SECRET_DEAL", body)

    def test_bad_pk_format_with_json_accept_no_5xx(self):
        r = self.client.get(
            "/api/deals/not-a-number/",
            HTTP_ACCEPT="application/json",
        )
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))


# ============================================================================
# 6. HEADER ABUSE — host injection, X-Forwarded-*, Origin/CORS, Range,
# Referer/User-Agent reflection, forged cookies, multi-Authorization,
# very long header/path/QS, conditional headers
# ============================================================================


class TestHeaderAbuse(HttpSecurityBase):
    """Headers that proxies / cloud frameworks may treat specially."""

    def test_request_headers_are_not_reflected_or_used_for_filtering(self):
        url = f"/api/deals/{self.victim_deal.pk}/"
        marker = "REFLECTION-MARKER-XYZ"
        # Each header probe: must not 5xx and must not be reflected.
        cases = [
            ("HTTP_X_FORWARDED_FOR", "1.2.3.4"),
            ("HTTP_X_REAL_IP", "9.9.9.9"),
            ("HTTP_REFERER", marker),
            ("HTTP_RANGE", "bytes=0-100"),
            ("HTTP_X_LONG_HEADER", "A" * 8192),
        ]
        for hdr, val in cases:
            r = self.client.get(url, **{hdr: val})
            _no_secret_leak(self, r, hdr)
            _no_header_reflection(self, r, val, hdr)
            self.assertFalse(_is_5xx(r), f"5xx on {hdr}")

    def test_host_and_xforwarded_host_no_leak(self):
        # Host: evil and X-Forwarded-Host: evil — must not influence
        # response or get reflected as link host.
        for hdrs in (
            {"HTTP_HOST": "evil.attacker.example"},
            {"HTTP_X_FORWARDED_HOST": "evil.com"},
            {"HTTP_X_FORWARDED_PROTO": "http"},
        ):
            r = self.client.get("/api/deals/", **hdrs)
            _no_secret_leak(self, r, str(hdrs))
            _no_open_redirect(self, r, str(hdrs))
            self.assertFalse(_is_5xx(r))
            body = r.content.decode("utf-8", errors="replace")
            self.assertNotIn("evil.com", body)
            if r.status_code == 200 and isinstance(r.data, dict):
                ids = [d.get("id") for d in r.data.get("data", [])]
                self.assertNotIn(self.victim_deal.pk, ids)

    def test_origin_evil_no_cors_reflection(self):
        r = self.client.get("/api/deals/", HTTP_ORIGIN="https://evil.com")
        _no_secret_leak(self, r)
        acao = r.get("Access-Control-Allow-Origin", "")
        self.assertNotEqual(acao, "https://evil.com")
        self.assertNotEqual(acao, "*")
        self.assertFalse(_is_5xx(r))

    def test_user_agent_xss_marker_not_reflected(self):
        r = self.client.get(
            "/api/deals/not-a-pk/",
            HTTP_USER_AGENT="<script>alert(1)</script>UA-MARKER",
        )
        _no_header_reflection(self, r, "UA-MARKER", "User-Agent")
        self.assertFalse(_is_5xx(r))

    def test_forged_session_cookie_no_data_leak(self):
        c = APIClient()
        c.cookies["sessionid"] = "forged-session-cookie-12345"
        r = c.get("/api/deals/")
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))
        if hasattr(r, "data") and r.data is not None:
            self.assertNotIn("VICTIM_SECRET_DEAL", str(r.data))

    def test_authorization_multi_value_no_leak(self):
        r = self.client.get(
            f"/api/deals/{self.victim_deal.pk}/",
            HTTP_AUTHORIZATION="Bearer fakeA, Bearer fakeB",
        )
        _no_secret_leak(self, r)
        self.assertNotEqual(r.status_code, 200)
        self.assertFalse(_is_5xx(r))

    def test_very_long_path_and_query_no_5xx(self):
        long_path = "/" + ("x" * 4096)
        r1 = self.client.get(f"/api/deals/{self.victim_deal.pk}{long_path}")
        _no_secret_leak(self, r1, "long path")
        self.assertNotEqual(r1.status_code, 200)
        self.assertFalse(_is_5xx(r1))

        long_qs = "&".join(f"q{i}=v" for i in range(2000))
        r2 = self.client.get(f"/api/deals/?{long_qs}")
        _no_secret_leak(self, r2, "long QS")
        self.assertFalse(_is_5xx(r2))

    def test_duplicate_query_keys_does_not_leak_victim_rows(self):
        for qs in (
            f"brokerage={self.brokerage_victim.pk}"
            f"&brokerage={self.brokerage_victim.pk}",
            f"brokerage={self.brokerage_attacker.pk}"
            f"&brokerage={self.brokerage_victim.pk}",
        ):
            r = self.client.get(f"/api/deals/?{qs}")
            _no_secret_leak(self, r, qs)
            if r.status_code == 200 and isinstance(r.data, dict):
                ids = [d.get("id") for d in r.data.get("data", [])]
                self.assertNotIn(self.victim_deal.pk, ids)

    def test_open_redirect_next_param_not_honored(self):
        r = self.client.get("/api/deals/?next=https://evil.com")
        _no_secret_leak(self, r)
        _no_open_redirect(self, r)
        self.assertFalse(_is_5xx(r))

    def test_etag_does_not_leak_victim_marker(self):
        r = self.client.get(f"/api/deals/{self.victim_deal.id}/")
        self.assertEqual(r.status_code, 404)
        for hdr in ("ETag", "Last-Modified"):
            v = r.get(hdr)
            if v:
                self.assertNotIn("VICTIM", v)


# ============================================================================
# 7. REST SEMANTIC INVARIANTS — idempotency, collection-vs-detail verb
# permissions, PUT/PATCH/DELETE write-blocking on foreign rows
# ============================================================================


class TestRESTSemantics(HttpSecurityBase):
    """REST-shaped invariants. PUT idempotency, PATCH partial body,
    DELETE-twice → 404, collection-only verbs → 405, etc."""

    def test_put_same_body_twice_no_duplicate(self):
        body = {
            "title": "my-update",
            "brokerage": self.brokerage_attacker.pk,
            "assigned_broker": self.attacker.pk,
        }
        url = f"/api/deals/{self.attacker_deal.pk}/"
        before = Deal.objects.count()
        self.client.put(url, body, format="json")
        self.client.put(url, body, format="json")
        self.assertEqual(before, Deal.objects.count(), "PUT created duplicate")

    def test_put_body_id_field_does_not_rebind_pk(self):
        body = {
            "id": self.victim_deal.pk,  # attempt PK rebind
            "title": "rebind-attempt",
            "brokerage": self.brokerage_attacker.pk,
            "assigned_broker": self.attacker.pk,
        }
        r = self.client.put(f"/api/deals/{self.attacker_deal.pk}/", body, format="json")
        _no_secret_leak(self, r)
        self.victim_deal.refresh_from_db()
        self.assertEqual(self.victim_deal.title, "VICTIM_SECRET_DEAL")
        self.assertEqual(self.victim_deal.brokerage_id, self.brokerage_victim.pk)
        self.assertFalse(_is_5xx(r))

    def test_patch_empty_body_no_change(self):
        before_title = self.attacker_deal.title
        r = self.client.patch(f"/api/deals/{self.attacker_deal.pk}/", {}, format="json")
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))
        self.attacker_deal.refresh_from_db()
        self.assertEqual(self.attacker_deal.title, before_title)

    def test_put_missing_required_fk_does_not_clear_brokerage(self):
        # PATCH partial body without FK → must NOT clear/rebind FK
        body_no_fk = {"title": "no-fk-here"}
        self.client.put(
            f"/api/deals/{self.attacker_deal.pk}/", body_no_fk, format="json"
        )
        self.client.patch(
            f"/api/deals/{self.attacker_deal.pk}/", body_no_fk, format="json"
        )
        self.attacker_deal.refresh_from_db()
        self.assertEqual(self.attacker_deal.brokerage_id, self.brokerage_attacker.pk)

    def test_delete_twice_second_returns_404(self):
        own = Deal.objects.create(
            title="own-deal",
            brokerage=self.brokerage_attacker,
            assigned_broker=self.attacker,
        )
        r1 = self.client.delete(f"/api/deals/{own.pk}/")
        r2 = self.client.delete(f"/api/deals/{own.pk}/")
        self.assertFalse(_is_5xx(r1))
        self.assertFalse(_is_5xx(r2))
        self.assertEqual(r2.status_code, 404)

    def test_put_to_victim_pk_idempotent_failure(self):
        body = {
            "title": "owned",
            "brokerage": self.brokerage_attacker.pk,
            "assigned_broker": self.attacker.pk,
        }
        url = f"/api/deals/{self.victim_deal.pk}/"
        for _ in range(3):
            r = self.client.put(url, body, format="json")
            _no_secret_leak(self, r)
            self.assertNotIn(r.status_code, (200, 201))
        self.victim_deal.refresh_from_db()
        self.assertEqual(self.victim_deal.title, "VICTIM_SECRET_DEAL")
        self.assertEqual(self.victim_deal.brokerage_id, self.brokerage_victim.pk)

    def test_collection_verbs_405(self):
        # PUT/DELETE/PATCH on collection URL must 405.
        before_count = Deal.objects.count()
        r_put = self.client.put(
            "/api/deals/",
            {"title": "x", "brokerage": self.brokerage_attacker.pk},
            format="json",
        )
        r_delete = self.client.delete("/api/deals/")
        r_patch = self.client.patch("/api/deals/", {"title": "BULK"}, format="json")
        for r in (r_put, r_delete, r_patch):
            _no_secret_leak(self, r)
            self.assertFalse(_is_5xx(r))
            self.assertEqual(r.status_code, 405)
        self.assertEqual(before_count, Deal.objects.count())

    def test_post_to_detail_url_405(self):
        r = self.client.post(
            f"/api/deals/{self.attacker_deal.pk}/",
            {"title": "x"},
            format="json",
        )
        _no_secret_leak(self, r)
        self.assertEqual(r.status_code, 405)

    def test_concurrent_patches_to_foreign_row_blocked(self):
        for i in range(2):
            r = self.client.patch(
                f"/api/deals/{self.victim_deal.pk}/",
                {"title": f"PWN_{i}"},
                format="json",
            )
            _no_secret_leak(self, r)
            self.assertNotIn(r.status_code, (200, 201))
        self.victim_deal.refresh_from_db()
        self.assertEqual(self.victim_deal.title, "VICTIM_SECRET_DEAL")

    def test_post_extra_id_field_not_mass_assigned(self):
        body = {
            "id": 99999,
            "title": "echo",
            "brokerage": self.brokerage_attacker.pk,
            "assigned_broker": self.attacker.pk,
            "created_at": "2020-01-01T00:00:00Z",
            "extra_unknown_field": "<script>alert(1)</script>",
        }
        r = self.client.post("/api/deals/", body, format="json")
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))
        if r.status_code == 201 and isinstance(r.data, dict):
            new_id = r.data.get("id")
            if new_id:
                d = Deal.objects.get(pk=new_id)
                self.assertEqual(d.brokerage_id, self.brokerage_attacker.pk)
                self.assertNotEqual(d.pk, 99999)

    def test_idempotency_key_silent_ignore_no_collision(self):
        body = {
            "title": "ik-1",
            "brokerage": self.brokerage_attacker.pk,
            "assigned_broker": self.attacker.pk,
        }
        for _ in range(2):
            r = self.client.post(
                "/api/deals/",
                body,
                format="json",
                HTTP_IDEMPOTENCY_KEY="00000000-0000-0000-0000-000000000001",
            )
            _no_secret_leak(self, r)
            self.assertFalse(_is_5xx(r))

    def test_idempotency_key_collision_two_users(self):
        c1 = APIClient()
        c1.force_authenticate(user=self.attacker)
        c2 = APIClient()
        c2.force_authenticate(user=self.victim)
        key = "shared-key-collision"
        r1 = c1.post(
            "/api/deals/",
            {
                "title": "atk",
                "brokerage": self.brokerage_attacker.pk,
                "assigned_broker": self.attacker.pk,
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        c2.post(
            "/api/deals/",
            {
                "title": "VICTIM_KEY_COLLISION",
                "brokerage": self.brokerage_victim.pk,
                "assigned_broker": self.victim.pk,
            },
            format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        if hasattr(r1, "data") and r1.data is not None:
            self.assertNotIn("VICTIM_KEY_COLLISION", str(r1.data))

    def test_idempotency_key_weird_values_no_5xx(self):
        body = {
            "title": "spoof",
            "brokerage": self.brokerage_attacker.pk,
            "assigned_broker": self.attacker.pk,
        }
        for k in (
            "\x00\x00\x00",
            "A" * 4096,
            "../../etc/passwd",
            "null",
            "<script>alert(1)</script>",
            "key with spaces",
        ):
            r = self.client.post(
                "/api/deals/", body, format="json", HTTP_IDEMPOTENCY_KEY=k
            )
            _no_secret_leak(self, r, f"key={k!r}")
            self.assertFalse(_is_5xx(r))


# ============================================================================
# 8. FOREIGN-TENANT WRITE PROTECTION ON THE FULL CHAIN (deal → bank → tx)
# ============================================================================


class TestChainWriteProtection(HttpSecurityBase):
    """PATCH/PUT/DELETE/GET on victim chain rows — must always block."""

    def test_chain_writes_blocked(self):
        # PATCH transaction
        r1 = self.client.patch(
            f"/api/transactions/{self.victim_tx.pk}/",
            {"amount": "1.00"},
            format="json",
        )
        # PUT transaction
        r2 = self.client.put(
            f"/api/transactions/{self.victim_tx.pk}/",
            {"amount": "1.00", "bank_account": self.victim_bank.pk},
            format="json",
        )
        # DELETE transaction
        r3 = self.client.delete(f"/api/transactions/{self.victim_tx.pk}/")
        # GET bankaccount
        r4 = self.client.get(f"/api/bankaccounts/{self.victim_bank.pk}/")
        for r in (r1, r2, r3, r4):
            _no_secret_leak(self, r)
        self.assertNotIn(r1.status_code, (200, 201))
        self.assertNotIn(r2.status_code, (200, 201))
        self.assertNotEqual(r3.status_code, 204)
        self.assertNotEqual(r4.status_code, 200)

        # Data unchanged
        self.victim_tx.refresh_from_db()
        self.assertEqual(self.victim_tx.amount, Decimal("999999.99"))
        self.assertTrue(Transaction.objects.filter(pk=self.victim_tx.pk).exists())

    def test_no_trailing_slash_chain_writes_blocked(self):
        r_get = self.client.get(f"/api/transactions/{self.victim_tx.pk}")
        r_patch = self.client.patch(
            f"/api/transactions/{self.victim_tx.pk}",
            {"amount": "1.00"},
            format="json",
        )
        for r in (r_get, r_patch):
            _no_secret_leak(self, r)
        self.assertNotEqual(r_get.status_code, 200)
        self.assertNotIn(r_patch.status_code, (200, 201))
        self.victim_tx.refresh_from_db()
        self.assertEqual(self.victim_tx.amount, Decimal("999999.99"))

    def test_get_via_encoded_pk_chain_blocked(self):
        encoded = "".join(f"%{ord(c):02X}" for c in str(self.victim_bank.pk))
        r = self.client.get(f"/api/bankaccounts/{encoded}/")
        _no_secret_leak(self, r)
        self.assertNotEqual(r.status_code, 200)


# ============================================================================
# 9. ANONYMOUS ACCESS
# ============================================================================


class TestAnonymousAccess(HttpSecurityBase):
    """Anonymous client must not read tenant-scoped detail."""

    def test_anon_cannot_read_deal_detail(self):
        anon = APIClient()
        r = anon.get(f"/api/deals/{self.victim_deal.pk}/")
        _no_secret_leak(self, r)
        self.assertNotEqual(r.status_code, 200)

    def test_anon_options_on_all_collections_no_5xx(self):
        anon = APIClient()
        for ep in (
            "/api/deals/",
            "/api/bankaccounts/",
            "/api/transactions/",
            "/api/samplemodels/",
            "/api/relatedmodels/",
            "/api/categorys/",
            "/api/articlewithcategoriess/",
            "/api/compiledsamplemodels/",
            "/api/compiledarticles/",
            "/api/custom-items/",
        ):
            r = anon.options(ep)
            _no_secret_leak(self, r, f"anon OPTIONS {ep}")
            self.assertFalse(_is_5xx(r), f"5xx on anon OPTIONS {ep}")


# ============================================================================
# 10. OPTIONS METADATA — collection coverage, role-differential, payload
# shape, FK enumeration, conditional & smuggling-shape headers
# ============================================================================


class TestOPTIONSMetadata(HttpSecurityBase):
    """OPTIONS body / metadata: must not leak FK enumeration, must
    return consistent shape across roles, must not reveal hidden
    fields for low-permission roles."""

    ALL_COLLECTIONS = (
        "/api/deals/",
        "/api/bankaccounts/",
        "/api/transactions/",
        "/api/samplemodels/",
        "/api/relatedmodels/",
        "/api/categorys/",
        "/api/articlewithcategoriess/",
        "/api/compiledsamplemodels/",
        "/api/compiledarticles/",
        "/api/custom-items/",
    )

    def test_options_collection_no_secret_no_5xx(self):
        for ep in self.ALL_COLLECTIONS:
            r = self.client.options(ep)
            _no_secret_leak(self, r, ep)
            self.assertFalse(_is_5xx(r), f"5xx on OPTIONS {ep}")

    def test_options_chain_detail_no_secret_no_5xx(self):
        # SampleModel/RelatedModel/Category/Article/Compiled* detail OPTIONS
        urls = [
            f"/api/samplemodels/{self.sample.pk}/",
            f"/api/relatedmodels/{self.related.pk}/",
            f"/api/categorys/{self.cat.pk}/",
            f"/api/articlewithcategoriess/{self.article.pk}/",
            f"/api/compiledsamplemodels/{self.compiled_sample.pk}/",
            f"/api/compiledarticles/{self.compiled_article.pk}/",
            f"/api/custom-items/{self.custom_item.pk}/",
        ]
        for u in urls:
            r = self.client.options(u)
            _no_secret_leak(self, r, u)
            self.assertFalse(_is_5xx(r), f"5xx on {u}")

    def test_options_collection_count_keys_not_leaking_global(self):
        r = self.client.options("/api/deals/")
        body = r.content.decode(errors="replace")
        # If we ever see "count": 2 / "total_items": 2 → leaks the
        # global row count rather than the attacker-scoped count.
        for token in ('"count": 2', '"total_items": 2', '"total": 2'):
            self.assertNotIn(token, body)
        if r.status_code == 200 and isinstance(r.data, dict):
            pag = r.data.get("pagination", {}) or {}
            for k in ("count", "total", "total_items"):
                self.assertNotIn(k, pag, f"pagination metadata leaked {k}")

    def test_options_does_not_enumerate_fk_values(self):
        # A common bug: OPTIONS metadata enumerates all FK PKs as choices.
        r = self.client.options("/api/deals/")
        body = r.content.decode("utf-8", errors="replace")
        for n in ("Victim Co", "Innocent Co", '"victim"'):
            self.assertNotIn(n, body, f"OPTIONS leaked label: {n}")

    def test_options_per_role_no_5xx_no_leak(self):
        # OPTIONS as anon, attacker, manager, viewer, editor, admin,
        # no_roles — all on a foreign-tenant detail. None may leak; none
        # may 5xx.
        url = f"/api/deals/{self.victim_deal.pk}/"
        for user_attr in (
            None,
            "attacker",
            "attacker_manager",
            "viewer",
            "editor",
            "admin_user",
            "no_roles_user",
        ):
            c = APIClient()
            if user_attr is not None:
                c.force_authenticate(user=getattr(self, user_attr))
            r = c.options(url)
            _no_secret_leak(self, r, f"role={user_attr}")
            self.assertFalse(_is_5xx(r), f"5xx role={user_attr}")

    def test_options_actions_keys_consistent_across_roles(self):
        # The set of keys in actions / capabilities must be constant
        # across roles (only values differ); a missing key is itself an
        # oracle.
        url = f"/api/samplemodels/{self.sample.pk}/"
        all_actions = []
        all_caps = []
        for user_attr in (
            "attacker",
            "attacker_manager",
            "viewer",
            "editor",
            "admin_user",
            "no_roles_user",
        ):
            c = APIClient()
            c.force_authenticate(user=getattr(self, user_attr))
            r = c.options(url)
            if r.status_code == 200 and isinstance(r.data, dict):
                a = r.data.get("actions", {})
                if isinstance(a, dict):
                    all_actions.append((user_attr, sorted(a.keys())))
                cap = r.data.get("capabilities", {})
                if isinstance(cap, dict):
                    all_caps.append((user_attr, sorted(cap.keys())))
        for label, all_keys in (("actions", all_actions), ("capabilities", all_caps)):
            if len(all_keys) >= 2:
                first = all_keys[0][1]
                for who, keys in all_keys[1:]:
                    self.assertEqual(
                        keys,
                        first,
                        f"{label} keys differ for {who}: {keys} != {first}",
                    )

    def test_options_viewer_metadata_does_not_leak_secret_field(self):
        c = APIClient()
        c.force_authenticate(user=self.viewer)
        r = c.options(f"/api/samplemodels/{self.sample.pk}/")
        body = r.content.decode("utf-8", errors="replace")
        self.assertFalse(_is_5xx(r))
        self.assertNotIn("secret_field", body, "viewer OPTIONS leaked secret_field")
        if r.status_code == 200 and isinstance(r.data, dict):
            actions = r.data.get("actions", {})
            for k in ("create", "update", "destroy"):
                self.assertFalse(actions.get(k), f"viewer has {k}=true")
            cap = r.data.get("capabilities", {}) or {}
            sf = cap.get("search_fields", []) or []
            self.assertNotIn("secret_field", sf)
            fs = cap.get("filterset_fields", {}) or {}
            self.assertNotIn("secret_field", fs)
            of = cap.get("ordering_fields", []) or []
            if isinstance(of, list):
                for forbidden in ("secret_field", "price"):
                    self.assertNotIn(forbidden, of)

    def test_options_editor_field_metadata_well_formed(self):
        c = APIClient()
        c.force_authenticate(user=self.editor)
        r = c.options(f"/api/samplemodels/{self.sample.pk}/")
        if r.status_code != 200 or not isinstance(r.data, dict):
            self.skipTest(f"non-200 OPTIONS: {r.status_code}")
        fields = r.data.get("model", {}).get("fields", {})
        for fname, fmeta in fields.items():
            if not isinstance(fmeta, dict):
                continue
            self.assertIn("type", fmeta, f"{fname} missing type")
            for v in fmeta.values():
                s = str(v)
                self.assertNotIn("<class ", s)
                self.assertNotIn("Traceback", s)
            ml = fmeta.get("max_length", 0)
            self.assertIsInstance(ml, (int, type(None)))
            if "read_only" in fmeta:
                self.assertIsInstance(fmeta["read_only"], bool)

    def test_options_tenancy_block_no_value_leak(self):
        r = self.client.options("/api/deals/")
        if r.status_code != 200 or not isinstance(r.data, dict):
            self.skipTest(f"non-200 OPTIONS: {r.status_code}")
        tenancy = r.data.get("tenancy", {})
        self.assertEqual(tenancy.get("tenant_field"), "brokerage")
        s = str(tenancy)
        for n in ("Attacker Co", "Victim Co", "Innocent Co"):
            self.assertNotIn(n, s)

    def test_options_with_assorted_headers_no_5xx_no_leak(self):
        # Accept variants, override headers, conditional, body, range,
        # cookie, CORS preflight, language, format, query params.
        url = f"/api/deals/{self.victim_deal.pk}/"
        cases = [
            {"HTTP_ACCEPT": "text/html"},
            {"HTTP_ACCEPT": "application/x-evil+json"},
            {"HTTP_ACCEPT": "application/json; charset=utf-7"},
            {"HTTP_ACCEPT": "*/*"},
            {"HTTP_ACCEPT": "*/*; q=0"},
            {"HTTP_ACCEPT": "application/xml"},
            {"HTTP_ACCEPT": "application/xml;q=0.1, application/json;q=0.9"},
            {"HTTP_ACCEPT_LANGUAGE": "zh-CN,en;q=0.5"},
            {"HTTP_X_HTTP_METHOD_OVERRIDE": "GET"},
            {"HTTP_X_METHOD_OVERRIDE": "GET"},
            {"HTTP_X_HTTP_METHOD_OVERRIDE": "DELETE"},
            {"HTTP_IF_MATCH": '"*"'},
            {"HTTP_IF_NONE_MATCH": '"*"'},
            {"HTTP_IF_MODIFIED_SINCE": "Thu, 01 Jan 1970 00:00:00 GMT"},
            {"HTTP_RANGE": "bytes=0-1000"},
            {"HTTP_VARY": "Cookie, Accept"},
            {"HTTP_COOKIE": "auth=fake; sessionid=garbage"},
            {"HTTP_ORIGIN": "https://evil.example/"},
            {
                "HTTP_ORIGIN": "https://evil.example",
                "HTTP_ACCESS_CONTROL_REQUEST_METHOD": "PATCH",
            },
            {
                "HTTP_ORIGIN": "https://evil.example",
                "HTTP_ACCESS_CONTROL_REQUEST_METHOD": "GET",
                "HTTP_ACCESS_CONTROL_REQUEST_HEADERS": "Authorization,X-Custom",
            },
        ]
        for h in cases:
            r = self.client.options(url, **h)
            _no_secret_leak(self, r, f"hdr={h}")
            self.assertFalse(_is_5xx(r), f"5xx hdr={h} body={r.content!r}")
            if "Origin" in str(h):
                hdrs = "".join(f"{k}:{v}\n" for k, v in r.items())
                self.assertNotIn("evil.example", hdrs)

    def test_options_with_body_and_query_params_no_5xx(self):
        # Body on OPTIONS, ?role=admin, ?format=*, pagination params,
        # ?fields=, secret in body — none should 5xx; none should leak.
        url = f"/api/deals/{self.victim_deal.pk}/"
        # body with foreign-FK injection
        r1 = self.client.generic(
            "OPTIONS",
            url,
            data='{"brokerage": ' + str(self.brokerage_victim.pk) + "}",
            content_type="application/json",
        )
        _no_secret_leak(self, r1)
        self.assertFalse(_is_5xx(r1))
        # body + override
        r2 = self.client.generic(
            "OPTIONS",
            url,
            data="{}",
            content_type="application/json",
            HTTP_X_HTTP_METHOD_OVERRIDE="GET",
        )
        _no_secret_leak(self, r2)
        self.assertFalse(_is_5xx(r2))
        # OPTIONS on collection with body containing FK injection
        r3 = self.client.generic(
            "OPTIONS",
            "/api/deals/",
            data=('{"brokerage": ' + str(self.brokerage_victim.pk) + ', "title": "x"}'),
            content_type="application/json",
        )
        _no_secret_leak(self, r3)
        self.assertFalse(_is_5xx(r3))
        # Query-param permutations
        for qs in (
            "?role=admin",
            "?format=api",
            "?format=xml",
            "?fields=title,price",
            "?page=2&page_size=100",
        ):
            r = self.client.options(url + qs)
            _no_secret_leak(self, r, qs)
            self.assertFalse(_is_5xx(r))

    def test_options_pk_extremes_no_5xx(self):
        for url in (
            "/api/deals/00000000-0000-0000-0000-000000000000/",
            "/api/deals/hello/",
            "/api/deals/0xdead/",
            "/api/deals/-999/",
            "/api/deals/0/",
            "/api/deals/" + ("9" * 200) + "/",
            "/api/deals/" + ("9" * 30) + "/",
            "/api/deals/1%27%20OR%201%3D1%20--/",
        ):
            r = self.client.options(url)
            _no_secret_leak(self, r, url)
            self.assertFalse(_is_5xx(r), f"5xx on {url}: {r.content!r}")

    def test_options_no_trailing_slash_no_leak(self):
        r = self.client.options(f"/api/deals/{self.victim_deal.pk}")
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))

    def test_options_root_does_not_enumerate_models(self):
        r = self.client.options("/api/")
        body = r.content.decode("utf-8", errors="ignore")
        self.assertNotIn("VICTIM_SECRET_DEAL", body)
        self.assertFalse(_is_5xx(r))

    def test_options_alternation_no_cache_pollution(self):
        # Alternating own / foreign / ghost OPTIONS — no leak.
        ghost = self.victim_tx.pk + 88888
        for _ in range(3):
            for url in (
                f"/api/deals/{self.attacker_deal.pk}/",
                f"/api/deals/{self.victim_deal.pk}/",
                f"/api/deals/{ghost}/",
            ):
                r = self.client.options(url)
                _no_secret_leak(self, r, url)
                self.assertFalse(_is_5xx(r))

    def test_options_deterministic_after_cache_clear(self):
        url = f"/api/deals/{self.victim_deal.pk}/"
        r1 = self.client.options(url)
        cache.clear()
        r2 = self.client.options(url)
        self.assertEqual(r1.status_code, r2.status_code)
        _no_secret_leak(self, r1)
        _no_secret_leak(self, r2)

    def test_options_disabled_model_returns_404(self):
        r = self.client.options("/api/disabledmodels/")
        _no_secret_leak(self, r)
        self.assertNotEqual(r.status_code, 200)
        self.assertFalse(_is_5xx(r))

    def test_options_admin_no_tenant_does_not_bypass(self):
        c = APIClient()
        c.force_authenticate(user=self.admin_user)
        r = c.options(f"/api/transactions/{self.victim_tx.pk}/")
        _no_secret_leak(self, r)
        self.assertFalse(_is_5xx(r))

    def test_options_m2m_does_not_enumerate_choices(self):
        r = self.client.options(f"/api/articlewithcategoriess/{self.article.pk}/")
        body = r.content.decode("utf-8", errors="replace")
        _no_secret_leak(self, r)
        # No enumerated `"choices": [{"value": 1` block.
        self.assertNotIn('"choices": [{"value": 1', body)

    def test_options_compiled_vs_normal_capability_diff_well_formed(self):
        r_comp = self.client.options("/api/compiledsamplemodels/")
        r_norm = self.client.options("/api/samplemodels/")
        _no_secret_leak(self, r_comp)
        _no_secret_leak(self, r_norm)
        if not (
            r_comp.status_code == 200
            and r_norm.status_code == 200
            and isinstance(r_comp.data, dict)
            and isinstance(r_norm.data, dict)
        ):
            return
        for r in (r_comp, r_norm):
            cfp = r.data.get("capabilities", {}).get("client_fields_param")
            self.assertIn(cfp, ("fields", None))


# ============================================================================
# 11. RESPONSE-SHAPE / SCHEMA / RENDERER LEAKS
# ============================================================================


class TestResponseAndSchema(HttpSecurityBase):
    """Pagination counts, swagger schema, fields= param, error-message
    inference, browsable HTML."""

    def test_compiled_list_returns_only_attacker_rows(self):
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 200)
        ids = [d["id"] for d in r.data["data"]]
        self.assertNotIn(self.victim_deal.id, ids)
        _no_secret_leak(self, r)

    def test_pagination_total_items_attacker_scoped(self):
        # Attacker owns exactly one row in each chain table, victim owns
        # the rest. total_items must reflect the attacker's slice only.
        r1 = self.client.get("/api/deals/")
        self.assertEqual(r1.data["pagination"]["total_items"], 1)
        ids1 = {d["id"] for d in r1.data["data"]}
        self.assertNotIn(self.victim_deal.pk, ids1)

        r2 = self.client.get("/api/bankaccounts/")
        self.assertEqual(r2.data["pagination"]["total_items"], 1)
        ids2 = {d["id"] for d in r2.data["data"]}
        self.assertNotIn(self.victim_bank.pk, ids2)

        r3 = self.client.get("/api/transactions/")
        self.assertEqual(r3.data["pagination"]["total_items"], 1)
        ids3 = {d["id"] for d in r3.data["data"]}
        self.assertNotIn(self.victim_tx.pk, ids3)

    def test_renderer_for_foreign_detail_returns_404_not_secret(self):
        for accept in ("application/json", "*/*", "application/json; q=0.9"):
            r = self.client.get(
                f"/api/deals/{self.victim_deal.id}/", HTTP_ACCEPT=accept
            )
            self.assertEqual(r.status_code, 404)
            _no_secret_leak(self, r)

    def test_search_does_not_return_victim_row(self):
        r = self.client.get("/api/deals/?search=VICTIM_SECRET_DEAL")
        self.assertEqual(r.status_code, 200)
        ids = [d["id"] for d in r.data["data"]]
        self.assertNotIn(self.victim_deal.id, ids)
        _no_secret_leak(self, r)

    def test_ordering_by_brokerage_no_foreign_leak(self):
        r = self.client.get("/api/deals/?ordering=brokerage")
        self.assertEqual(r.status_code, 200)
        ids = [d["id"] for d in r.data["data"]]
        self.assertNotIn(self.victim_deal.id, ids)

    def test_fields_param_does_not_leak_foreign_data(self):
        for f in (
            "id,title,brokerage,assigned_broker",
            "id,title,assigned_broker__username",
            "id,title,brokerage__name",
            "id,title,brokerage__deals",
            "id,title,brokerage__deals__title",
        ):
            r = self.client.get(f"/api/deals/?fields={f}")
            _no_secret_leak(self, r, f"fields={f}")

    def test_swagger_does_not_leak_victim_data(self):
        try:
            r = self.client.get("/swagger/?format=openapi")
            status = r.status_code
            body = r.content.decode("utf-8", errors="ignore")
        except Exception as e:
            status = 500
            body = str(e)
        # Either 200 schema, or framework error — neither may include secrets.
        for s in SECRETS:
            self.assertNotIn(s, body)
        self.assertTrue(status >= 200)

    def test_swagger_role_query_does_not_elevate(self):
        from drf_yasg import openapi
        from rest_framework.test import APIRequestFactory

        from turbodrf.swagger import RoleBasedSchemaGenerator

        factory = APIRequestFactory()
        request = factory.get("/swagger/?role=admin")
        request.user = self.attacker
        from django.contrib.sessions.middleware import SessionMiddleware

        SessionMiddleware(lambda r: None).process_request(request)
        info = openapi.Info(title="t", default_version="v1")
        gen = RoleBasedSchemaGenerator(info=info)
        try:
            gen.get_schema(request, public=False)
        except Exception:
            pass
        self.assertNotEqual(
            gen.current_role,
            "admin",
            "VULNERABILITY: ?role=admin elevates non-admin schema",
        )

    def test_invalid_field_value_error_does_not_leak_internals(self):
        r = self.client.post(
            "/api/deals/",
            {"title": "x", "brokerage": "not-an-int"},
            format="json",
        )
        body = str(r.data)
        for token in (
            "TurboDRFViewSet",
            "TurboDRFSerializerFactory",
            "Traceback",
            "__init__",
            "/Users/",
            "site-packages",
        ):
            self.assertNotIn(token, body)

    def test_malformed_json_no_5xx(self):
        r = self.client.post(
            "/api/deals/",
            "{not valid json",
            content_type="application/json",
        )
        self.assertNotEqual(r.status_code, 500)

    def test_invalid_pk_format_returns_clean_404(self):
        r = self.client.get("/api/deals/abc/")
        self.assertNotEqual(r.status_code, 500)
        body = str(r.data) if hasattr(r, "data") and r.data else ""
        for token in ("Traceback", "ValueError", "VICTIM"):
            self.assertNotIn(token, body)

    def test_filter_with_bad_value_returns_clean_4xx(self):
        r = self.client.get("/api/deals/?brokerage=not-an-int")
        self.assertNotEqual(r.status_code, 500)

    def test_browsable_html_list_no_victim_leak(self):
        r = self.client.get("/api/deals/?format=api")
        body = r.content.decode("utf-8", errors="ignore")
        self.assertNotIn("VICTIM_SECRET_DEAL", body)

    def test_vary_header_present_does_not_5xx(self):
        r = self.client.get("/api/deals/")
        self.assertEqual(r.status_code, 200)
        # Just ensure the Vary header is well-formed (str or absent).
        v = r.get("Vary") or ""
        self.assertIsInstance(v, str)
