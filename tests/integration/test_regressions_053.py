"""Regressions fixed in 0.5.3 (reported from a downstream app on 0.5.2).

9. The compiled list rendered DecimalField values with str() ("0E-8" for zero
   at decimal_places=8) where the serializer renders "0.00000000". Datetimes,
   times and durations diverged the same way.
10. Traversal scoping used the JOIN target's default manager, so a row whose
    FK pointed at a soft-deleted target vanished from the compiled list, its
    count, and search over the nested path. It also assumed every model has a
    manager named ``objects``.
"""

import datetime
import json
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import Brokerage, Fund, Valuation

User = get_user_model()

URL = "/api/valuations/"


class _Fixture(TestCase):
    def setUp(self):
        import tests.urls  # noqa: F401  (router registers tenancy)

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()

        self.brokerage_a = Brokerage.objects.create(name="A")
        self.brokerage_b = Brokerage.objects.create(name="B")
        self.fund = Fund.all_objects.create(
            name="Live", nav=Decimal("0"), brokerage=self.brokerage_a
        )
        self.gone = Fund.all_objects.create(
            name="Gone", nav=Decimal("1.5"), brokerage=self.brokerage_a, deleted=True
        )
        self.fund_b = Fund.all_objects.create(name="Other", brokerage=self.brokerage_b)

        self.analyst = User.objects.create_user(username="analyst", password="x")
        self.analyst._test_roles = ["analyst"]
        set_test_brokerage(self.analyst, self.brokerage_a)
        self.client.force_authenticate(self.analyst)

    def tearDown(self):
        _test_user_brokerages.clear()

    def _valuation(self, fund=None, brokerage=None, **values):
        return Valuation.objects.create(
            units=values.pop("units", Decimal("1")),
            brokerage=brokerage or self.brokerage_a,
            fund=fund or self.fund,
            **values,
        )

    def _list(self, **params):
        resp = self.client.get(URL, params)
        self.assertEqual(resp.status_code, 200, resp.content)
        return json.loads(resp.content)

    def _rows(self, **params):
        return {row["id"]: row for row in self._list(**params)["data"]}

    def _detail(self, pk):
        resp = self.client.get(f"{URL}{pk}/")
        self.assertEqual(resp.status_code, 200, resp.content)
        return json.loads(resp.content)


class CompiledValuesRenderLikeTheSerializer(_Fixture):
    """Compare the rendered JSON of list rows (compiled) and detail
    (serializer): the same row must read the same on both."""

    def assertListMatchesDetail(self, obj):
        row = self._rows()[obj.pk]
        detail = self._detail(obj.pk)
        for key, value in row.items():
            self.assertEqual(value, detail[key], f"{key}: list {value!r}")
        return row

    def test_decimals_are_quantized_not_exponent_form(self):
        cases = {
            Decimal("0"): "0.00000000",
            Decimal("1E-8"): "0.00000001",
            Decimal("-5.5"): "-5.50000000",
            # (SQLite stores decimals as REAL: keep within float precision.)
            Decimal("98765432.25"): "98765432.25000000",
        }
        for units, expected in cases.items():
            with self.subTest(units=str(units)):
                obj = self._valuation(units=units)
                row = self.assertListMatchesDetail(obj)
                self.assertEqual(row["units"], expected)

    def test_nested_decimal_formatted_on_both_paths(self):
        row = self.assertListMatchesDetail(self._valuation())
        self.assertEqual(row["fund_nav"], "0.00000000")

    def test_datetime_time_and_duration_match(self):
        obj = self._valuation(
            valued_at=datetime.datetime(
                2026, 10, 22, 1, 2, 3, 456789, tzinfo=datetime.timezone.utc
            ),
            settles_at=datetime.time(9, 30, 0, 123456),
            period=datetime.timedelta(days=1, hours=2),
        )
        row = self.assertListMatchesDetail(obj)
        self.assertEqual(row["valued_at"], "2026-10-22T01:02:03.456789Z")
        self.assertEqual(row["settles_at"], "09:30:00.123456")
        self.assertEqual(row["period"], "1 02:00:00")

    @override_settings(TIME_ZONE="Australia/Sydney")
    def test_datetime_in_current_timezone(self):
        obj = self._valuation(
            valued_at=datetime.datetime(
                2026, 10, 22, 1, 0, tzinfo=datetime.timezone.utc
            )
        )
        row = self.assertListMatchesDetail(obj)
        self.assertEqual(row["valued_at"], "2026-10-22T12:00:00+11:00")

    def test_nulls_stay_null(self):
        row = self.assertListMatchesDetail(self._valuation())
        self.assertIsNone(row["valued_at"])
        self.assertIsNone(row["settles_at"])
        self.assertIsNone(row["period"])

    def test_coerce_decimal_to_string_off_matches(self):
        obj = self._valuation(units=Decimal("2.5"))
        with override_settings(
            REST_FRAMEWORK={
                "DEFAULT_AUTHENTICATION_CLASSES": [
                    "rest_framework.authentication.SessionAuthentication"
                ],
                "EXCEPTION_HANDLER": "turbodrf.exceptions.turbodrf_exception_handler",
                "COERCE_DECIMAL_TO_STRING": False,
            }
        ):
            self.assertListMatchesDetail(obj)


class SoftDeletedJoinTargetKeepsTheRow(_Fixture):
    """A default-manager filter (soft delete) is not a scoping layer: a row
    pointing at a soft-deleted target in the caller's tenant stays listed, as
    it does on detail. A target in another tenant is still out of scope."""

    def setUp(self):
        super().setUp()
        self.live = self._valuation(fund=self.fund)
        self.orphan = self._valuation(fund=self.gone)
        self.cross = self._valuation(fund=self.fund_b)
        self.other_tenant = self._valuation(
            fund=self.fund_b, brokerage=self.brokerage_b
        )

    def test_list_and_count_keep_row_with_soft_deleted_target(self):
        body = self._list()
        self.assertEqual(
            {r["id"] for r in body["data"]},
            {self.live.pk, self.orphan.pk, self.cross.pk},
        )
        self.assertEqual(body["pagination"]["total_items"], 3)

    def test_nested_value_renders_from_soft_deleted_target(self):
        row = self._rows()[self.orphan.pk]
        self.assertEqual(row["fund_name"], "Gone")
        self.assertEqual(row["fund_nav"], self._detail(self.orphan.pk)["fund_nav"])

    def test_search_over_nested_path_finds_it(self):
        self.assertEqual(set(self._rows(search="Gone")), {self.orphan.pk})

    def test_cross_tenant_target_still_hidden(self):
        # Listed (the row is the caller's) with the other tenant's values null;
        # search can't match them.
        row = self._rows()[self.cross.pk]
        self.assertIsNone(row["fund_name"])
        self.assertIsNone(row["fund_nav"])
        self.assertIsNone(self._detail(self.cross.pk)["fund_name"])
        self.assertEqual(self._rows(search="Other"), {})

    def test_model_without_objects_manager_is_served(self):
        resp = self.client.get("/api/funds/")
        self.assertEqual(resp.status_code, 200, resp.content)
        # The target's own endpoint still uses its default manager.
        self.assertEqual([r["id"] for r in resp.data["data"]], [self.fund.pk])
