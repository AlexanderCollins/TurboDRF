"""
Tests for TurboDRF renderer and exception handler.
"""

import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.exceptions import (
    NotFound,
    PermissionDenied,
    ValidationError,
)
from rest_framework.test import APIClient, APIRequestFactory

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf.exceptions import NoRoleAssigned, turbodrf_exception_handler
from turbodrf.renderers import FAST_JSON_AVAILABLE, FAST_JSON_LIB, TurboDRFRenderer

User = get_user_model()


# ---------- Renderer tests ----------


class TestTurboDRFRendererBasic(TestCase):
    """Test the TurboDRFRenderer for basic serialisation behaviour."""

    def setUp(self):
        self.renderer = TurboDRFRenderer()

    def test_renders_bytes(self):
        result = self.renderer.render({"key": "value"})
        self.assertIsInstance(result, bytes)

    def test_renders_valid_json(self):
        data = {"id": 1, "name": "test"}
        result = self.renderer.render(data)
        parsed = json.loads(result)
        self.assertEqual(parsed, data)

    def test_none_returns_empty_bytes(self):
        result = self.renderer.render(None)
        self.assertEqual(result, b"")

    def test_dict(self):
        data = {"a": 1, "b": "two", "c": True, "d": None}
        parsed = json.loads(self.renderer.render(data))
        self.assertEqual(parsed, data)

    def test_list(self):
        data = [1, "two", 3.0, None]
        parsed = json.loads(self.renderer.render(data))
        self.assertEqual(parsed, data)

    def test_nested_structure(self):
        data = {
            "pagination": {"next": None, "total_items": 5},
            "data": [{"id": 1, "tags": ["a", "b"]}, {"id": 2, "tags": []}],
        }
        parsed = json.loads(self.renderer.render(data))
        self.assertEqual(parsed, data)

    def test_empty_dict(self):
        parsed = json.loads(self.renderer.render({}))
        self.assertEqual(parsed, {})

    def test_empty_list(self):
        parsed = json.loads(self.renderer.render([]))
        self.assertEqual(parsed, [])


class TestTurboDRFRendererSpecialTypes(TestCase):
    """Test rendering of types commonly found in DRF API responses."""

    def setUp(self):
        self.renderer = TurboDRFRenderer()

    def test_decimal_value(self):
        data = {"price": Decimal("19.99")}
        result = json.loads(self.renderer.render(data))
        # Decimal is serialised as a number (msgspec/orjson) or string
        self.assertIn(float(result["price"]), [19.99])

    def test_datetime_value(self):
        dt = datetime(2025, 6, 15, 12, 30, 0)
        data = {"created_at": dt}
        result = json.loads(self.renderer.render(data))
        self.assertIn("2025-06-15", result["created_at"])

    def test_date_value(self):
        d = date(2025, 6, 15)
        data = {"published": d}
        result = json.loads(self.renderer.render(data))
        self.assertEqual(result["published"], "2025-06-15")

    def test_uuid_value(self):
        uid = UUID("12345678-1234-5678-1234-567812345678")
        data = {"uuid": uid}
        result = json.loads(self.renderer.render(data))
        self.assertEqual(result["uuid"], "12345678-1234-5678-1234-567812345678")


class TestFastJSONFlags(TestCase):
    """Test that FAST_JSON_AVAILABLE and FAST_JSON_LIB are set correctly."""

    def test_fast_json_available_is_bool(self):
        self.assertIsInstance(FAST_JSON_AVAILABLE, bool)

    def test_fast_json_lib_is_string(self):
        self.assertIn(FAST_JSON_LIB, ("msgspec", "orjson", "stdlib"))

    def test_available_matches_lib(self):
        if FAST_JSON_LIB in ("msgspec", "orjson"):
            self.assertTrue(FAST_JSON_AVAILABLE)
        else:
            self.assertFalse(FAST_JSON_AVAILABLE)

    def test_renderer_media_type(self):
        renderer = TurboDRFRenderer()
        self.assertEqual(renderer.media_type, "application/json")

    def test_renderer_format(self):
        renderer = TurboDRFRenderer()
        self.assertEqual(renderer.format, "json")


# ---------- Exception handler tests ----------


def _make_context():
    """Build a minimal `context` dict for turbodrf_exception_handler."""
    factory = APIRequestFactory()
    request = factory.get("/")
    return {"request": request, "view": None}


class TestExceptionHandlerPermissionDenied(TestCase):
    """Test 403 errors are wrapped in the standard error envelope."""

    def test_403_structure(self):
        exc = PermissionDenied()
        response = turbodrf_exception_handler(exc, _make_context())

        self.assertEqual(response.status_code, 403)
        self.assertIn("error", response.data)
        error = response.data["error"]
        self.assertEqual(error["status"], 403)
        self.assertIn("code", error)
        self.assertIn("message", error)

    def test_403_code_field(self):
        exc = PermissionDenied()
        response = turbodrf_exception_handler(exc, _make_context())
        self.assertEqual(response.data["error"]["code"], "permission_denied")

    def test_403_message_is_string(self):
        exc = PermissionDenied(detail="Nope")
        response = turbodrf_exception_handler(exc, _make_context())
        self.assertEqual(response.data["error"]["message"], "Nope")


class TestExceptionHandlerValidation(TestCase):
    """Test 400 validation errors preserve field-level detail."""

    def test_400_dict_detail(self):
        exc = ValidationError({"title": ["This field is required."]})
        response = turbodrf_exception_handler(exc, _make_context())

        self.assertEqual(response.status_code, 400)
        error = response.data["error"]
        self.assertEqual(error["status"], 400)
        # Field-level detail is preserved as a dict
        self.assertIsInstance(error["message"], dict)
        self.assertIn("title", error["message"])

    def test_400_list_detail(self):
        exc = ValidationError(["Something went wrong."])
        response = turbodrf_exception_handler(exc, _make_context())

        error = response.data["error"]
        self.assertIsInstance(error["message"], list)

    def test_400_string_detail(self):
        exc = ValidationError("Bad input.")
        response = turbodrf_exception_handler(exc, _make_context())

        error = response.data["error"]
        # DRF wraps a bare string in a list, so the handler receives list detail
        self.assertIn("Bad input", str(error["message"]))


class TestExceptionHandlerNotFound(TestCase):
    """Test 404 errors."""

    def test_404_structure(self):
        exc = NotFound()
        response = turbodrf_exception_handler(exc, _make_context())

        self.assertEqual(response.status_code, 404)
        error = response.data["error"]
        self.assertEqual(error["status"], 404)
        self.assertIn("code", error)
        self.assertIn("message", error)

    def test_404_code(self):
        exc = NotFound()
        response = turbodrf_exception_handler(exc, _make_context())
        self.assertEqual(response.data["error"]["code"], "not_found")


class TestExceptionHandlerNonDRF(TestCase):
    """Test that non-DRF exceptions return None (unhandled)."""

    def test_plain_exception_returns_none(self):
        exc = ValueError("boom")
        response = turbodrf_exception_handler(exc, _make_context())
        self.assertIsNone(response)


# ---------- NoRoleAssigned exception ----------


class TestNoRoleAssigned(TestCase):
    """Test the custom NoRoleAssigned exception."""

    def test_status_code(self):
        self.assertEqual(NoRoleAssigned.status_code, 403)

    def test_default_detail(self):
        exc = NoRoleAssigned()
        self.assertIn("No role assigned", str(exc.detail))

    def test_default_code(self):
        self.assertEqual(NoRoleAssigned.default_code, "no_role_assigned")

    def test_custom_detail(self):
        exc = NoRoleAssigned(detail="Custom message")
        self.assertEqual(str(exc.detail), "Custom message")

    def test_handled_by_exception_handler(self):
        exc = NoRoleAssigned()
        response = turbodrf_exception_handler(exc, _make_context())

        self.assertEqual(response.status_code, 403)
        error = response.data["error"]
        self.assertEqual(error["code"], "no_role_assigned")
        self.assertIn("No role assigned", error["message"])


# ---------- Integration: real API request ----------


class TestExceptionFormatViaAPIClient(TestCase):
    """Hit a real endpoint and verify the standardised error envelope."""

    def setUp(self):
        self.client = APIClient()
        self.related = RelatedModel.objects.create(name="Cat", description="Desc")
        SampleModel.objects.create(
            title="Item",
            price=Decimal("10.00"),
            quantity=1,
            related=self.related,
        )

    def test_unauthenticated_post_returns_error_envelope(self):
        """POST to a public-read endpoint without auth should return 403."""
        response = self.client.post(
            "/api/samplemodels/",
            {"title": "New"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["status"], 403)
        self.assertIn("code", data["error"])
        self.assertIn("message", data["error"])

    def test_not_found_returns_error_envelope(self):
        """GET a non-existent pk returns 404 in standard format."""
        response = self.client.get("/api/samplemodels/999999/")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        data = response.json()
        self.assertIn("error", data)
        self.assertEqual(data["error"]["status"], 404)
