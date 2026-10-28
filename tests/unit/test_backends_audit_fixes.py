"""Regressions for the permission-cache, value-rendering, decorator and
exception handler audit findings. Each test names the finding it pins."""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, models
from django.http import Http404
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APIClient, APIRequestFactory

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf import backends
from turbodrf.backends import (
    build_permission_snapshot,
    get_cache_key,
    get_user_roles,
    invalidate_user_permissions,
)
from turbodrf.decorators import turbodrf_action
from turbodrf.exceptions import turbodrf_exception_handler
from turbodrf.models import RolePermission, TurboDRFRole, UserRole
from turbodrf.serializers import field_representation

User = get_user_model()


class TestUserRoles(TestCase):
    """Finding 9: `user.roles` shapes."""

    def _user(self, roles):
        user = User.objects.create_user(username=f"u{User.objects.count()}")
        user._test_roles = roles
        return user

    def test_string_is_one_role(self):
        self.assertEqual(get_user_roles(self._user("viewer")), ["viewer"])

    def test_manager_gives_role_names(self):
        holder = User.objects.create_user(username="holder")
        holder.groups.add(Group.objects.create(name="editor"))

        class Member:
            is_authenticated = True
            pk = holder.pk
            roles = holder.groups  # a related manager, not a list

        self.assertEqual(get_user_roles(Member()), ["editor"])

    def test_manager_without_name_field_fails_closed(self):
        holder = User.objects.create_user(username="holder")

        class Member:
            is_authenticated = True
            pk = holder.pk
            roles = holder.logentry_set

        with self.assertRaises(ImproperlyConfigured):
            get_user_roles(Member())


class TestCacheKey(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="k")
        self.user._test_roles = ["viewer"]

    def test_key_covers_sensitive_fields_and_public_access(self):
        """Finding 7: a deploy changing either can't reuse old snapshots."""
        key = get_cache_key(self.user, SampleModel)
        with mock.patch.object(SampleModel, "sensitive_fields", ["price"], create=True):
            self.assertNotEqual(get_cache_key(self.user, SampleModel), key)
        config = {**SampleModel.turbodrf(), "public_access": False}
        with mock.patch.object(
            SampleModel, "turbodrf", classmethod(lambda cls: dict(config))
        ):
            self.assertNotEqual(get_cache_key(self.user, SampleModel), key)
        self.assertEqual(get_cache_key(self.user, SampleModel), key)

    def test_evicted_generation_does_not_restore_old_keys(self):
        """Finding 8: a key computed before an invalidation must not come
        back when the generation entry is evicted."""
        before = get_cache_key(self.user, SampleModel)
        invalidate_user_permissions(self.user)
        after = get_cache_key(self.user, SampleModel)
        self.assertNotEqual(after, before)
        prefix = "turbodrf_perm"
        cache.delete_many(
            [f"{prefix}:generation:all", f"{prefix}:generation:{self.user.pk}"]
        )
        self.assertNotIn(get_cache_key(self.user, SampleModel), {before, after})

    def test_invalidating_anonymous_users(self):
        """Finding 8: invalidate_user_permissions(anonymous) did nothing."""
        anonymous = AnonymousUser()
        before = get_cache_key(anonymous, SampleModel)
        invalidate_user_permissions(anonymous)
        self.assertNotEqual(get_cache_key(anonymous, SampleModel), before)


@override_settings(TURBODRF_PERMISSION_MODE="database")
class TestDatabaseModeAnonymousSnapshot(TestCase):
    """Finding 6: the anonymous snapshot on a public_access model depends on
    every role's field rules; a new rule must apply on the next request."""

    def setUp(self):
        cache.clear()
        rel = RelatedModel.objects.create(name="r")
        self.obj = SampleModel.objects.create(
            title="t", description="hidden soon", price=Decimal("1"), related=rel
        )

    def test_new_field_rule_hides_field_from_anonymous(self):
        client = APIClient()
        url = f"/api/samplemodels/{self.obj.pk}/"
        self.assertIn("description", client.get(url).data)
        role = TurboDRFRole.objects.create(name="staff")
        RolePermission.objects.create(
            role=role,
            app_label="test_app",
            model_name="samplemodel",
            field_name="description",
            permission_type="read",
        )
        self.assertNotIn("description", client.get(url).data)


@override_settings(TURBODRF_PERMISSION_MODE="database")
class TestRequestMemo(TestCase):
    """Finding 29: within a request, roles and snapshots are looked up once."""

    def test_roles_and_snapshots_memoised_per_request(self):
        role = TurboDRFRole.objects.create(name="r")
        RolePermission.objects.create(
            role=role, app_label="test_app", model_name="samplemodel", action="read"
        )
        user = User.objects.create_user(username="m")
        UserRole.objects.create(user=user, role=role)
        cache.clear()
        with backends._permission_memo():
            build_permission_snapshot(user, SampleModel)  # cold
            with CaptureQueriesContext(connection) as ctx:
                for _ in range(10):
                    self.assertEqual(get_user_roles(user), ["r"])
                    build_permission_snapshot(user, SampleModel)
        self.assertEqual(len(ctx.captured_queries), 0)
        # Outside a request nothing is memoised: a change applies at once.
        role.delete()
        self.assertEqual(get_user_roles(user), [])


class TestFieldRepresentation(TestCase):
    """Finding 23: only field types whose raw value renders like their DRF
    field render raw; everything else goes through the DRF field."""

    def test_choices_and_foreign_field_classes_use_the_drf_field(self):
        class Custom(models.CharField):
            pass

        def bound(field, name):
            field.name, field.model = name, SampleModel
            return field

        status = bound(models.CharField(max_length=5, choices=[("a", "A")]), "s")
        custom = bound(Custom(max_length=5), "custom")
        self.assertIsNotNone(field_representation(status))
        self.assertIsNotNone(field_representation(custom))
        self.assertIsNone(field_representation(bound(models.CharField(), "plain")))


class TestTurboDRFAction(TestCase):
    """Finding 26: arguments TurboDRF would ignore are refused."""

    def test_serializer_class_and_queryset_are_refused(self):
        for kwargs in ({"serializer_class": object}, {"queryset": None}):
            with self.subTest(kwargs=sorted(kwargs)):
                with self.assertRaises(TypeError):
                    turbodrf_action(detail=True, **kwargs)

    def test_supported_arguments(self):
        decorated = turbodrf_action(
            detail=True, methods=["post"], permission_classes=[IsAuthenticated]
        )(lambda self, request, pk=None: None)
        self.assertTrue(decorated.detail)
        self.assertEqual(decorated.kwargs["permission_classes"], [IsAuthenticated])
        # `detail` keeps its default (a collection route).
        self.assertFalse(turbodrf_action()(lambda self, request: None).detail)


class TestExceptionHandler(TestCase):
    """Finding 10: Django's Http404 gets DRF's not_found code and a message
    that doesn't name the model."""

    def test_http404(self):
        context = {"request": APIRequestFactory().get("/"), "view": None}
        response = turbodrf_exception_handler(
            Http404("No Deal matches the given query."), context
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "not_found")
        self.assertNotIn("Deal", response.data["error"]["message"])
