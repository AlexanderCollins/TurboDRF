"""Shared fixture data for the ParityAllTypes / ParityTarget test models: one
row per common field type, FK and to_field chains, M2M tags (one soft-deleted),
and two role users. Tests patch the model's turbodrf() config with
``parity_config``."""

import datetime
import json
import uuid
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages
from tests.test_app.models import (
    ParityAllTypes,
    ParityCurrency,
    ParityLeaf,
    ParityTag,
    ParityTarget,
)

User = get_user_model()
UTC = datetime.timezone.utc

ALL_URL = "/api/parityalltypess/"
TARGET_URL = "/api/paritytargets/"


@contextmanager
def parity_config(model, overrides):
    """Serve ``model`` with its turbodrf() config updated by ``overrides``."""
    cfg = {**model.turbodrf(), **overrides}
    with patch.object(model, "turbodrf", classmethod(lambda cls: dict(cfg))):
        yield cfg


class ParityFixture(TestCase):
    """The fixture rows and users, plus a JSON ``_get``."""

    def setUp(self):
        import tests.urls  # noqa: F401  (the router registers the models)

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()
        self.client.raise_request_exception = False

        self.usd = ParityCurrency.objects.create(code="USD", name="Dollar")
        self.eur = ParityCurrency.objects.create(code="EUR", name="Euro")
        self.leaf1 = ParityLeaf.objects.create(
            name="leaf-one",
            amount=Decimal("3.50"),
            seen_at=datetime.datetime(2024, 1, 2, 3, 4, 5, 600000, tzinfo=UTC),
        )
        self.leaf2 = ParityLeaf.objects.create(name="leaf-two")  # NULL amount
        self.target1 = ParityTarget.objects.create(
            name="alpha",
            amount=Decimal("12.50"),
            seen_at=datetime.datetime(2024, 3, 10, 15, 30, 45, 123456, tzinfo=UTC),
            span=datetime.timedelta(hours=5, microseconds=7),
            uid=uuid.UUID("12345678-1234-5678-1234-567812345678"),
            doc="docs/alpha.txt",
            leaf=self.leaf1,
            partner=self.leaf2,
            currency=self.usd,
        )
        self.target2 = ParityTarget.objects.create(name="beta")  # NULL leaf
        self.target3 = ParityTarget.objects.create(name="gamma", leaf=self.leaf2)

        now = datetime.datetime(2024, 5, 6, 7, 8, 9, 10, tzinfo=UTC)
        self.tag_apple = ParityTag.objects.create(
            name="apple",
            weight=Decimal("1.25"),
            created=now,
            leaf=self.leaf1,
            currency=self.eur,
        )
        self.tag_banana = ParityTag.objects.create(name="banana")
        self.tag_zebra = ParityTag.objects.create(name="zebra", weight=Decimal("9.00"))
        self.tag_deleted = ParityTag.objects.create(name="ghost", is_deleted=True)

        self.row_full = ParityAllTypes.objects.create(
            char="Hello",
            char_null="maybe",
            text="long text",
            text_null="tn",
            integer=42,
            integer_null=7,
            big=9007199254740993,
            big_null=-5,
            positive=5,
            positive_null=6,
            flt=1.5,
            flt_null=2.25,
            flag=True,
            flag_null=False,
            day=datetime.date(2024, 2, 29),
            moment=datetime.datetime(2024, 3, 10, 15, 30, 45, 123456, tzinfo=UTC),
            clock=datetime.time(13, 14, 15, 123),
            span=datetime.timedelta(days=1, hours=2, minutes=3, seconds=4),
            uid=uuid.UUID("87654321-4321-8765-4321-876543218765"),
            data={"a": {"b": [1, 2, {"c": None}]}, "n": 1.5, "s": "x"},
            data_null={"k": "v"},
            status="live",
            status_null="draft",
            level=2,
            email="a@example.com",
            url="https://example.com/x",
            slug="a-slug",
            ip="2001:db8::1",
            attachment="files/report.pdf",
            picture="img/p.png",
            blob=b"\x00\x01binary\xff",
            amount=Decimal("1234.50"),
            amount_null=Decimal("0.10"),
            target=self.target1,
            twin=self.target2,
            currency=self.usd,
        )
        # Through-table order apple -> zebra; ParityTag.Meta.ordering = -name.
        self.row_full.tags.add(self.tag_apple)
        self.row_full.tags.add(self.tag_zebra)

        self.row_nulls = ParityAllTypes.objects.create(char="nulls")
        self.row_mid = ParityAllTypes.objects.create(
            char="mid",
            integer=3,
            moment=datetime.datetime(2024, 7, 1, 23, 59, 59, tzinfo=UTC),
            clock=datetime.time(0, 0),
            target=self.target2,  # target.leaf is NULL (2nd hop)
            twin=self.target3,
            data=[1, "two", None],
        )
        self.row_mid.tags.add(self.tag_banana)
        self.row_leafnull = ParityAllTypes.objects.create(
            char="leafnull", integer=9, target=self.target3  # leaf.amount NULL
        )

        self.reader = self._user("reader", ["parity_reader"])
        self.limited = self._user("limited", ["parity_limited"])

    def tearDown(self):
        _test_user_brokerages.clear()

    def _user(self, username, roles):
        user = User.objects.create_user(username=username, password="x")
        user._test_roles = roles
        return user

    def _get(self, url, params=None):
        resp = self.client.get(url, params or {})
        exc = getattr(resp, "exc_info", None)
        if exc:
            return resp.status_code, f"{exc[0].__name__}: {exc[1]}"
        try:
            return resp.status_code, json.loads(resp.content)
        except ValueError:
            return resp.status_code, resp.content[:200]

    @staticmethod
    def _rows(body):
        if isinstance(body, dict) and "data" in body:
            return body["data"]
        return body
