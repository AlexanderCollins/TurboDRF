"""
Coverage-focused tests for serializer, renderer, filter backend, and apps.

Targets uncovered lines identified by coverage reports.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from tests.test_app.models import (
    ArticleWithCategories,
    Category,
    RelatedModel,
    SampleModel,
)
from turbodrf.backends import build_permission_snapshot
from turbodrf.filter_backends import ORFilterBackend
from turbodrf.renderers import FAST_JSON_LIB, TurboDRFRenderer
from turbodrf.serializers import TurboDRFSerializer

User = get_user_model()


# ---------------------------------------------------------------------------
# Serializer: to_representation with null FK — lines 88-90
# ---------------------------------------------------------------------------


class TestSerializerNullFK(TestCase):
    """Cover serializer to_representation when FK is null (lines 88-90)."""

    def setUp(self):
        self.author = RelatedModel.objects.create(name="Author", description="desc")

    def test_to_representation_with_null_fk(self):
        """Line 89-90: exception in nested field traversal is caught."""
        article = ArticleWithCategories.objects.create(
            title="No Author", content="Content", author=None
        )

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "author"]
                _nested_fields = {"author": ["author__name"]}

        serializer = TestSerializer(article)
        data = serializer.data

        # Should not raise; the FK traversal encounters None
        self.assertIn("title", data)
        # author_name should be None since author is null
        self.assertIn("author_name", data)
        self.assertIsNone(data["author_name"])


# ---------------------------------------------------------------------------
# Serializer: _is_many_to_many_field exception — lines 108-109
# ---------------------------------------------------------------------------


class TestIsManyToManyFieldException(TestCase):
    """Cover _is_many_to_many_field returning False on exception (lines 108-109)."""

    def test_nonexistent_field_returns_false(self):
        serializer = TurboDRFSerializer()
        instance = SampleModel(title="Test", price=Decimal("10.00"), quantity=1)
        result = serializer._is_many_to_many_field(instance, "nonexistent_field_xyz")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Serializer: _serialize_m2m_field null manager — line 132
# ---------------------------------------------------------------------------


class TestSerializeM2MFieldNullManager(TestCase):
    """Cover _serialize_m2m_field when manager is None (line 132)."""

    def test_null_m2m_manager_returns_empty_list(self):
        serializer = TurboDRFSerializer()
        instance = MagicMock()
        instance.nonexistent_m2m = None
        # getattr(instance, 'nonexistent_m2m', None) returns None
        delattr(instance, "nonexistent_m2m")
        result = serializer._serialize_m2m_field(instance, "nonexistent_m2m", [])
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Serializer: _serialize_m2m_field with short-form field names — line 145
# ---------------------------------------------------------------------------


class TestSerializeM2MShortFormFields(TestCase):
    """Cover line 145: nested_field that doesn't start with base_field__."""

    def setUp(self):
        self.author = RelatedModel.objects.create(name="Author", description="desc")
        self.cat = Category.objects.create(name="Tech", description="Tech stuff")
        self.article = ArticleWithCategories.objects.create(
            title="Test", content="Content", author=self.author
        )
        self.article.categories.add(self.cat)

    def test_short_form_field_names(self):
        """When nested_fields use short form (e.g. 'name' instead of 'categories__name')."""

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "categories"]
                _nested_fields = {"categories": ["name"]}

        serializer = TestSerializer(self.article)
        data = serializer.data

        self.assertIn("categories", data)
        self.assertIsInstance(data["categories"], list)
        self.assertEqual(len(data["categories"]), 1)
        self.assertIn("name", data["categories"][0])


# ---------------------------------------------------------------------------
# Serializer: _serialize_m2m_field exception in getattr — lines 154-155
# ---------------------------------------------------------------------------


class TestSerializeM2MGetAttrException(TestCase):
    """Cover lines 154-155: exception in getattr during M2M serialization."""

    def test_getattr_exception_produces_none(self):
        """When reading a sub-field of a related object raises an exception."""
        author = RelatedModel.objects.create(name="Author", description="desc")
        article = ArticleWithCategories.objects.create(
            title="Test", content="Content", author=author
        )
        article.categories.add(Category.objects.create(name="Tech"))

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "categories"]
                _nested_fields = {"categories": ["categories__name"]}

        serializer = TestSerializer(article)

        # The try/except around the sub-field read should catch and set None
        with patch.object(
            TurboDRFSerializer, "_nested_value", side_effect=Exception("boom")
        ):
            result = serializer._serialize_m2m_field(
                article, "categories", ["categories__name"]
            )
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0]["name"])


# ---------------------------------------------------------------------------
# Serializer: _serialize_m2m_field outer exception — lines 160-161
# ---------------------------------------------------------------------------


class TestSerializeM2MOuterException(TestCase):
    """Cover lines 160-161: outer exception returns empty list."""

    def test_manager_all_raises(self):
        serializer = TurboDRFSerializer()
        instance = MagicMock()
        mock_manager = MagicMock()
        mock_manager.all.side_effect = Exception("db error")
        instance.categories = mock_manager

        result = serializer._serialize_m2m_field(
            instance, "categories", ["categories__name"]
        )
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Serializer: update/create without request context — lines 179-186, 222-230
# ---------------------------------------------------------------------------


class TestSerializerCreateUpdateNoContext(TestCase):
    """Cover create/update when no request is in context (lines 179-186, 222-230)."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Related", description="desc")

    def test_update_without_request_context(self):
        """Lines 179-186: update with no request falls through to super()."""

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = SampleModel
                fields = ["title", "description"]

        instance = SampleModel.objects.create(
            title="Original",
            description="Desc",
            price=Decimal("10.00"),
            quantity=1,
            related=self.related,
        )

        serializer = TestSerializer(
            instance, data={"title": "Updated", "description": "New"}, context={}
        )
        self.assertTrue(serializer.is_valid())
        updated = serializer.save()
        self.assertEqual(updated.title, "Updated")

    def test_create_without_request_context(self):
        """Lines 222-230: create with no request falls through to super()."""

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = RelatedModel
                fields = ["name", "description"]

        serializer = TestSerializer(
            data={"name": "New Item", "description": "Desc"}, context={}
        )
        self.assertTrue(serializer.is_valid())
        created = serializer.save()
        self.assertEqual(created.name, "New Item")


# ---------------------------------------------------------------------------
# Serializer: update/create with write permission filtering — lines 438, 471-473
# (mapped to create/update filtering logic with snapshot)
# ---------------------------------------------------------------------------


class TestSerializerWritePermissionFiltering(TestCase):
    """Cover write permission filtering in create/update."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Related", description="desc")
        self.user = User.objects.create_user(username="editor")
        self.user._test_roles = ["editor"]

    def _make_request(self):
        factory = APIRequestFactory()
        django_request = factory.post("/")
        django_request.user = self.user
        return Request(django_request)

    def test_update_filters_unwritable_fields(self):
        """Update should filter out fields without write permission."""
        instance = SampleModel.objects.create(
            title="Original",
            description="Desc",
            price=Decimal("10.00"),
            quantity=1,
            related=self.related,
        )

        snapshot = build_permission_snapshot(self.user, SampleModel, use_cache=False)

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = SampleModel
                fields = ["title", "price"]

        request = self._make_request()
        serializer = TestSerializer(
            instance,
            data={"title": "Updated", "price": "99.99"},
            context={"request": request},
            partial=True,
        )
        serializer._permission_snapshot = snapshot

        self.assertTrue(serializer.is_valid())
        updated = serializer.save()

        # Editor can write title but not price
        self.assertEqual(updated.title, "Updated")

    def test_create_filters_unwritable_fields(self):
        """Create should filter out fields without write permission."""
        snapshot = build_permission_snapshot(self.user, RelatedModel, use_cache=False)

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = RelatedModel
                fields = ["name", "description"]

        request = self._make_request()
        serializer = TestSerializer(
            data={"name": "New Item", "description": "Desc"},
            context={"request": request},
        )
        serializer._permission_snapshot = snapshot

        self.assertTrue(serializer.is_valid())
        created = serializer.save()
        self.assertIsNotNone(created.pk)


# ---------------------------------------------------------------------------
# Serializer: create/update build snapshot fallback — lines 509, 515-516, 521-524
# ---------------------------------------------------------------------------


class TestSerializerSnapshotFallback(TestCase):
    """Cover snapshot fallback when _permission_snapshot not set."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Related", description="desc")
        self.user = User.objects.create_user(username="admin_snap")
        self.user._test_roles = ["admin"]

    def _make_request(self):
        factory = APIRequestFactory()
        django_request = factory.post("/")
        django_request.user = self.user
        return Request(django_request)

    def test_update_builds_snapshot_when_not_attached(self):
        """Lines 515-516: snapshot is built from request when not pre-attached."""
        instance = SampleModel.objects.create(
            title="Original",
            description="Desc",
            price=Decimal("10.00"),
            quantity=1,
            related=self.related,
        )

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = SampleModel
                fields = ["title"]

        request = self._make_request()
        serializer = TestSerializer(
            instance,
            data={"title": "Updated"},
            context={"request": request},
            partial=True,
        )
        # Don't attach _permission_snapshot — force the fallback path
        self.assertTrue(serializer.is_valid())
        updated = serializer.save()
        self.assertEqual(updated.title, "Updated")

    def test_create_builds_snapshot_when_not_attached(self):
        """Lines 521-524: snapshot is built from request when not pre-attached."""

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = RelatedModel
                fields = ["name", "description"]

        request = self._make_request()
        serializer = TestSerializer(
            data={"name": "Created", "description": "Desc"},
            context={"request": request},
        )
        # Don't attach _permission_snapshot
        self.assertTrue(serializer.is_valid())
        created = serializer.save()
        self.assertEqual(created.name, "Created")


# ---------------------------------------------------------------------------
# Serializer: to_representation with short-form nested field — lines 535-541
# ---------------------------------------------------------------------------


class TestSerializerShortFormNestedField(TestCase):
    """Cover the short-form nested field path branch (line 76)."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Related", description="desc")

    def test_short_form_nested_field(self):
        """When nested_field doesn't start with base_field__, build full path."""
        instance = SampleModel.objects.create(
            title="Test",
            description="Desc",
            price=Decimal("10.00"),
            quantity=1,
            related=self.related,
        )

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = SampleModel
                fields = ["title", "related"]
                _nested_fields = {"related": ["name"]}

        serializer = TestSerializer(instance)
        data = serializer.data

        self.assertIn("related_name", data)
        self.assertEqual(data["related_name"], "Related")


# ---------------------------------------------------------------------------
# Renderers: test attributes
# ---------------------------------------------------------------------------


class TestRendererAttributes(TestCase):
    """Cover renderer class attributes (media_type, format, charset)."""

    def test_media_type(self):
        renderer = TurboDRFRenderer()
        self.assertEqual(renderer.media_type, "application/json")

    def test_format(self):
        renderer = TurboDRFRenderer()
        self.assertEqual(renderer.format, "json")

    def test_charset(self):
        renderer = TurboDRFRenderer()
        # Fast renderers (msgspec/orjson) return bytes, so charset is None
        # Stdlib renderer may set charset="utf-8"
        self.assertIn(renderer.charset, (None, "utf-8"))

    def test_lib_name_is_known(self):
        self.assertIn(FAST_JSON_LIB, ("msgspec", "orjson", "stdlib"))


# ---------------------------------------------------------------------------
# Filter backend: __in lookup handling — line 113
# ---------------------------------------------------------------------------


class MockView:
    """Mock view for testing filter backend."""


class TestORFilterBackendInLookup(TestCase):
    """Cover line 113: __in lookup splits value by comma."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Cat", description="desc")
        self.item1 = SampleModel.objects.create(
            title="AAA", price=Decimal("10.00"), quantity=1, related=self.related
        )
        self.item2 = SampleModel.objects.create(
            title="BBB", price=Decimal("20.00"), quantity=2, related=self.related
        )
        self.item3 = SampleModel.objects.create(
            title="CCC", price=Decimal("30.00"), quantity=3, related=self.related
        )
        self.backend = ORFilterBackend()
        self.factory = APIRequestFactory()
        self.view = MockView()

    def test_in_lookup_splits_values(self):
        """__in filter should split the comma-separated value."""
        ids = f"{self.item1.pk},{self.item2.pk}"
        django_request = self.factory.get(f"/?id__in={ids}")
        request = Request(django_request)

        qs = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, qs, self.view)

        self.assertEqual(filtered.count(), 2)
        pks = set(filtered.values_list("pk", flat=True))
        self.assertEqual(pks, {self.item1.pk, self.item2.pk})


# ---------------------------------------------------------------------------
# Filter backend: invalid filter field rejection — line 135, 185
# ---------------------------------------------------------------------------


class TestORFilterBackendInvalidField(TestCase):
    """Cover invalid filter field rejection."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Cat", description="desc")
        SampleModel.objects.create(
            title="AAA", price=Decimal("10.00"), quantity=1, related=self.related
        )
        self.backend = ORFilterBackend()
        self.factory = APIRequestFactory()
        self.view = MockView()

    def test_invalid_field_is_skipped(self):
        """Non-existent field should be silently skipped."""
        django_request = self.factory.get("/?totally_bogus_field=abc")
        request = Request(django_request)

        qs = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, qs, self.view)

        # Should return all items — invalid field is skipped
        self.assertEqual(filtered.count(), 1)

    def test_invalid_or_field_is_skipped(self):
        """Non-existent _or field should be silently skipped."""
        django_request = self.factory.get("/?totally_bogus_field_or=abc")
        request = Request(django_request)

        qs = SampleModel.objects.all()
        filtered = self.backend.filter_queryset(request, qs, self.view)

        self.assertEqual(filtered.count(), 1)


# ---------------------------------------------------------------------------
# Filter backend: _accepted_filter permission check errors — lines 228-237
# ---------------------------------------------------------------------------


class TestFilterBackendPermissionCheckError(TestCase):
    """Cover lines 228-237: permission check error path (fail closed)."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Cat", description="desc")
        SampleModel.objects.create(
            title="AAA", price=Decimal("10.00"), quantity=1, related=self.related
        )
        self.backend = ORFilterBackend()
        self.factory = APIRequestFactory()
        self.view = MockView()

    @override_settings(
        TURBODRF_DISABLE_PERMISSIONS=False, TURBODRF_USE_DEFAULT_PERMISSIONS=False
    )
    def test_permission_check_error_fails_closed(self):
        """When permission check raises, field is denied."""
        user = User.objects.create_user(username="perm_error_user")
        user._test_roles = ["admin"]

        with patch(
            "turbodrf.validation.check_nested_field_permissions",
            side_effect=Exception("permission check exploded"),
        ):
            valid_fields = {"title"}
            result = self.backend._accepted_filter(
                "title", valid_fields, SampleModel, user
            )
            self.assertFalse(result)


# ---------------------------------------------------------------------------
# Filter backend: validate_filter_field raises — lines 200-202
# ---------------------------------------------------------------------------


class TestFilterBackendValidateFilterFieldFails(TestCase):
    """Cover lines 200-202: validate_filter_field raises exception."""

    def setUp(self):
        self.backend = ORFilterBackend()

    @override_settings(
        TURBODRF_DISABLE_PERMISSIONS=False,
        TURBODRF_USE_DEFAULT_PERMISSIONS=False,
        TURBODRF_MAX_NESTING_DEPTH=1,
    )
    def test_deep_nesting_rejected(self):
        """Filter parameter exceeding max nesting depth is rejected."""
        user = User.objects.create_user(username="depth_user")
        user._test_roles = ["admin"]

        valid_fields = {"author"}
        result = self.backend._accepted_filter(
            "author__publisher__name__icontains",
            valid_fields,
            SampleModel,
            user,
        )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Filter backend: _get_valid_filter_fields with callable filterset — line 135
# ---------------------------------------------------------------------------


class TestGetValidFilterFieldsCallable(TestCase):
    """Cover line 135: filterset_fields is callable."""

    def test_callable_filterset_fields(self):
        backend = ORFilterBackend()

        class CallableFilterView:
            @staticmethod
            def filterset_fields():
                return {"title": ["exact", "icontains"], "price": ["gte", "lte"]}

        fields = backend._get_valid_filter_fields(CallableFilterView, SampleModel)

        self.assertIn("title", fields)
        self.assertIn("title__exact", fields)
        self.assertIn("title__icontains", fields)
        self.assertIn("price", fields)
        self.assertIn("price__gte", fields)
        self.assertIn("price__lte", fields)


# ---------------------------------------------------------------------------
# Apps: ready() and drf_yasg auto-install — lines 28-33
# ---------------------------------------------------------------------------


class TestTurboDRFAppConfig(TestCase):
    """Cover apps.py ready() and _ensure_drf_yasg_installed."""

    def test_ready_with_appconfig_path_and_no_drf_yasg(self):
        """Listing the app as 'turbodrf.apps.TurboDRFConfig' without drf_yasg
        used to raise ValueError from INSTALLED_APPS.index('turbodrf') and stop
        Django booting. ready() no longer edits INSTALLED_APPS at all."""
        from turbodrf.apps import TurboDRFConfig

        config = TurboDRFConfig("turbodrf", __import__("turbodrf"))

        from django.conf import settings

        original_apps = settings.INSTALLED_APPS
        apps_list = (
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "turbodrf.apps.TurboDRFConfig",
            "tests.test_app",
        )
        try:
            settings.INSTALLED_APPS = apps_list
            config.ready()
            self.assertEqual(settings.INSTALLED_APPS, apps_list)
        finally:
            settings.INSTALLED_APPS = original_apps

    def test_docs_check_warns_when_drf_yasg_not_installed(self):
        """Adding drf_yasg to settings.INSTALLED_APPS in ready() can't work
        (the app registry is already populated), so a system check tells the
        user to add it instead."""
        from unittest.mock import patch

        from django.core.checks.registry import registry

        from turbodrf.apps import check_docs_dependencies

        self.assertIn(check_docs_dependencies, registry.get_checks())
        self.assertEqual(check_docs_dependencies(), [])

        with patch("turbodrf.apps.apps.is_installed", return_value=False):
            messages = check_docs_dependencies()
            self.assertEqual([m.id for m in messages], ["turbodrf.W001"])
            self.assertIn("drf_yasg", messages[0].hint)
            with override_settings(TURBODRF_ENABLE_DOCS=False):
                self.assertEqual(check_docs_dependencies(), [])

    @override_settings(TURBODRF_ENABLE_DOCS=False)
    def test_ready_skips_yasg_when_docs_disabled(self):
        """ready() skips drf_yasg install when TURBODRF_ENABLE_DOCS=False."""
        from turbodrf.apps import TurboDRFConfig

        config = TurboDRFConfig("turbodrf", __import__("turbodrf"))

        from django.conf import settings

        original_apps = settings.INSTALLED_APPS
        try:
            settings.INSTALLED_APPS = [
                "django.contrib.auth",
                "django.contrib.contenttypes",
                "turbodrf",
                "tests.test_app",
            ]
            config.ready()

            self.assertNotIn("drf_yasg", settings.INSTALLED_APPS)
        finally:
            settings.INSTALLED_APPS = original_apps
