"""Startup checks on the shape of turbodrf(): unknown keys, and fields that
end on a reverse one-to-one."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from tests.test_app.models import ParityTarget, ScopeLoose
from tests.unit.test_review_tenancy_config import _router


class TestUnknownConfigKeys(SimpleTestCase):
    BASE = {"tenant_field": "brokerage", "compiled": False, "fields": ["id", "title"]}

    def test_typo_is_refused(self):
        """'owner_feild' used to be ignored, leaving rows unscoped by owner."""
        with self.assertRaisesMessage(ImproperlyConfigured, "owner_feild"):
            _router((ScopeLoose, {**self.BASE, "owner_feild": "owner"}))

    def test_sensitive_fields_key_points_at_the_class_attribute(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "class attribute"):
            _router((ScopeLoose, {**self.BASE, "sensitive_fields": ["title"]}))

    def test_known_keys_boot(self):
        _router((ScopeLoose, {**self.BASE, "owner_field": "owner", "read_only": True}))


class TestReverseOneToOneField(SimpleTestCase):
    BASE = {"tenancy": "shared", "compiled": False}

    def test_bare_reverse_one_to_one_is_refused(self):
        """It rendered the related row's pk without that row's own scope."""
        with self.assertRaisesMessage(ImproperlyConfigured, "reverse one-to-one"):
            _router((ParityTarget, {**self.BASE, "fields": ["id", "twin_row"]}))

    def test_field_across_it_boots(self):
        _router((ParityTarget, {**self.BASE, "fields": ["id", "twin_row__char"]}))
