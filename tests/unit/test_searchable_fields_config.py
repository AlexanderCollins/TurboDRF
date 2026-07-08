"""searchable_fields is honored from the turbodrf() config dict, not only from a
class attribute.

Regression: declaring ``'searchable_fields': [...]`` inside the ``turbodrf()``
config (the natural place, alongside ``fields``/``tenant_field``) used to be a
silent no-op because only the class attribute was read.
"""

from django.test import TestCase

from turbodrf.mixins import get_searchable_fields


class _ConfigModel:
    @classmethod
    def turbodrf(cls):
        return {"fields": ["title"], "searchable_fields": ["title", "body"]}


class _AttrModel:
    searchable_fields = ["name"]

    @classmethod
    def turbodrf(cls):
        return {"fields": ["name"]}


class _NeitherModel:
    @classmethod
    def turbodrf(cls):
        return {"fields": ["x"]}


class _BothModel:
    searchable_fields = ["legacy"]

    @classmethod
    def turbodrf(cls):
        return {"fields": ["title"], "searchable_fields": ["from_config"]}


class TestSearchableFieldsFromConfig(TestCase):
    def test_read_from_config_dict(self):
        self.assertEqual(get_searchable_fields(_ConfigModel), ["title", "body"])

    def test_fallback_to_class_attribute(self):
        self.assertEqual(get_searchable_fields(_AttrModel), ["name"])

    def test_neither_returns_empty(self):
        self.assertEqual(get_searchable_fields(_NeitherModel), [])

    def test_config_dict_preferred_over_class_attribute(self):
        self.assertEqual(get_searchable_fields(_BothModel), ["from_config"])
