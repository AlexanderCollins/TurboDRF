"""Sanity check: a 201 CREATE response strips fields the user can't read.

Without this, a user with create+restricted-read perms could see hidden
fields by creating a row and reading the response body — leaking the
exact value they just submitted (or any auto-filled / server-computed
value) past the field-permission gate.
"""
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.models import Category

User = get_user_model()

# Role can write both fields, but only read `name`. `description` is
# write-only for this role.
#
# We also define an `admin` role with `description.read` so the field
# is GATED (per TurboDRF semantics: a field becomes permission-gated as
# soon as ANY role defines a `<model>.<field>.read` rule for it; if no
# role defines any rule for a field, it falls back to model-level read).
WRITE_BOTH_READ_NAME_ONLY = {
    "admin": [
        "test_app.category.read",
        "test_app.category.name.read",
        "test_app.category.description.read",
    ],
    "limited": [
        "test_app.category.create",
        "test_app.category.read",
        "test_app.category.name.write",
        "test_app.category.description.write",
        "test_app.category.name.read",
        # NO test_app.category.description.read — gated, must be stripped
    ],
}


@override_settings(TURBODRF_ROLES=WRITE_BOTH_READ_NAME_ONLY)
class Test201ResponseFieldFiltering(TestCase):

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="creator", password="x")
        self.user._test_roles = ["limited"]
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_201_response_strips_unreadable_fields(self):
        r = self.client.post(
            "/api/categorys/",
            {"name": "Cat1", "description": "SECRET_DESC_DONT_LEAK"},
            format="json",
        )
        self.assertEqual(r.status_code, 201, r.data)
        body = r.data

        # Readable field present
        self.assertIn("name", body)
        self.assertEqual(body["name"], "Cat1")

        # Unreadable field MUST be stripped from the response
        self.assertNotIn("description", body)
        # Belt and braces — scan the entire body string for the value
        self.assertNotIn("SECRET_DESC_DONT_LEAK", str(body))

    def test_get_response_strips_same_fields_as_201(self):
        """The 201 response and the equivalent GET response should have
        the same field set — confirming the same filter is applied."""
        cat = Category.objects.create(
            name="Cat2", description="SECRET_DESC_2"
        )
        r_get = self.client.get(f"/api/categorys/{cat.id}/")
        self.assertEqual(r_get.status_code, 200)
        get_body = r_get.data

        r_post = self.client.post(
            "/api/categorys/",
            {"name": "Cat3", "description": "SECRET_DESC_3"},
            format="json",
        )
        self.assertEqual(r_post.status_code, 201)
        post_body = r_post.data

        # Same readable field set — proves single source of truth
        self.assertEqual(
            sorted(get_body.keys()), sorted(post_body.keys()),
            f"GET keys {sorted(get_body.keys())} != "
            f"POST keys {sorted(post_body.keys())} — different filters!"
        )
        self.assertNotIn("description", get_body)
        self.assertNotIn("description", post_body)
