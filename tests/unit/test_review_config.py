"""
Regression tests for model-config helpers and the package's own modules.
Each test failed before its fix:

- ``'searchable_fields': []`` in the config fell back to the class
  attribute, so search couldn't be turned off from the config.
- ``turbodrf.user_extensions`` shipped concrete example models; importing it
  broke a project's model checks.
- ``turbodrf.settings`` held tenancy defaults nothing reads (the real ones
  are inline where they're read), so the two could drift apart.
"""

import ast
import pathlib

from django.test import TestCase

import turbodrf
from turbodrf.mixins import get_searchable_fields


class _EmptyConfigSearch:
    searchable_fields = ["legacy"]

    @classmethod
    def turbodrf(cls):
        return {"fields": ["title"], "searchable_fields": []}


class _NoConfigSearch:
    searchable_fields = ["legacy"]

    @classmethod
    def turbodrf(cls):
        return {"fields": ["title"]}


class TestSearchableFieldsConfig(TestCase):
    def test_empty_config_list_turns_search_off(self):
        self.assertEqual(get_searchable_fields(_EmptyConfigSearch), [])

    def test_absent_config_key_uses_the_class_attribute(self):
        self.assertEqual(get_searchable_fields(_NoConfigSearch), ["legacy"])


class TestPackageModules(TestCase):
    def test_user_extensions_example_module_is_gone(self):
        package = pathlib.Path(turbodrf.__file__).parent
        self.assertFalse((package / "user_extensions.py").exists())

    def test_every_settings_fallback_is_used(self):
        package = pathlib.Path(turbodrf.__file__).parent
        defined = {
            target.id
            for node in ast.parse((package / "settings.py").read_text()).body
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        imported = set()
        for path in package.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and node.module in (
                    "settings",  # from .settings import ...
                    "turbodrf.settings",
                ):
                    imported.update(alias.name for alias in node.names)
        self.assertTrue(defined)
        self.assertEqual(defined - imported, set())
