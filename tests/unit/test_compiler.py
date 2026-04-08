"""Tests for the compiled read path."""

from collections import defaultdict
from decimal import Decimal

import django
from django.core.exceptions import ImproperlyConfigured
from django.db.models import F
from django.test import TestCase

from turbodrf.compiler import (
    CompiledQueryPlan,
    DictProxy,
    compile_model,
    get_compiled_plan,
    is_compiled,
    register_compiled_plan,
)

from tests.test_app.models import (
    ArticleWithCategories,
    Category,
    CompiledArticle,
    CompiledSampleModel,
    CustomEndpointModel,
    RelatedModel,
    SampleModel,
)


class DictProxyTests(TestCase):
    def test_attribute_access(self):
        proxy = DictProxy({"name": "hello", "price": 42})
        self.assertEqual(proxy.name, "hello")
        self.assertEqual(proxy.price, 42)

    def test_missing_attribute_raises(self):
        proxy = DictProxy({"name": "hello"})
        with self.assertRaises(AttributeError):
            _ = proxy.missing

    def test_with_property_fget(self):
        """Property fget should work with DictProxy as self."""

        # Simulate a model property
        def display_title(self):
            return self.title.upper()

        proxy = DictProxy({"title": "hello world"})
        result = display_title(proxy)
        self.assertEqual(result, "HELLO WORLD")

    def test_with_real_model_property(self):
        """Test using an actual model's property fget."""
        fget = CompiledSampleModel.display_title.fget
        proxy = DictProxy({"title": "test book"})
        result = fget(proxy)
        self.assertEqual(result, "TEST BOOK")


class CompileModelTests(TestCase):
    def test_compile_model_not_compiled(self):
        """Models with compiled=False explicitly return None."""

        class OptedOut:
            @classmethod
            def turbodrf(cls):
                return {"compiled": False, "fields": ["name"]}

        result = compile_model(OptedOut)
        self.assertIsNone(result)

    def test_compile_model_simple_fields(self):
        plan = compile_model(CompiledSampleModel)
        self.assertIsNotNone(plan)
        # title, price, is_active should be in simple_fields
        self.assertIn("title", plan.simple_fields)
        self.assertIn("price", plan.simple_fields)
        self.assertIn("is_active", plan.simple_fields)

    def test_compile_model_includes_pk(self):
        plan = compile_model(CompiledSampleModel)
        self.assertIn("id", plan.simple_fields)

    def test_compile_model_fk_annotations(self):
        plan = compile_model(CompiledSampleModel)
        self.assertIn("related_name", plan.fk_annotations)
        self.assertEqual(plan.fk_annotations["related_name"].name, "related__name")
        # Base FK field should be in simple_fields for raw ID
        self.assertIn("related", plan.simple_fields)

    def test_compile_model_property_fields(self):
        plan = compile_model(CompiledSampleModel)
        self.assertIn("display_title", plan.property_fields)
        self.assertEqual(
            plan.property_fields["display_title"],
            CompiledSampleModel.display_title.fget,
        )

    def test_compile_model_m2m_specs(self):
        plan = compile_model(CompiledArticle)
        self.assertIn("categories", plan.m2m_specs)
        spec = plan.m2m_specs["categories"]
        self.assertEqual(spec["related_model"], Category)
        self.assertEqual(spec["sub_fields"], ["name"])
        self.assertIn("name", spec["annotations"])

    def test_compile_model_decimal_coercion(self):
        plan = compile_model(CompiledSampleModel)
        self.assertIn("price", plan.type_coercers)
        # Verify the coercer works
        self.assertEqual(plan.type_coercers["price"](Decimal("99.99")), "99.99")
        self.assertIsNone(plan.type_coercers["price"](None))

    def test_compile_model_invalid_field_raises(self):
        """A non-existent, non-property field should raise."""

        class BadModel(django.db.models.Model):
            title = django.db.models.CharField(max_length=100)

            class Meta:
                app_label = "test_app"

            @classmethod
            def turbodrf(cls):
                return {"compiled": True, "fields": ["title", "nonexistent_field"]}

        with self.assertRaises(ImproperlyConfigured):
            compile_model(BadModel)


class RegistryTests(TestCase):
    def test_register_and_get(self):
        plan = compile_model(CompiledSampleModel)
        register_compiled_plan(CompiledSampleModel, plan)
        self.assertIs(get_compiled_plan(CompiledSampleModel), plan)
        self.assertTrue(is_compiled(CompiledSampleModel))

    def test_not_compiled(self):
        """Models not in the registry return None."""
        from tests.test_app.models import NoTurboDRFModel
        self.assertIsNone(get_compiled_plan(NoTurboDRFModel))
        self.assertFalse(is_compiled(NoTurboDRFModel))


class CompiledQueryExecutionTests(TestCase):
    """Test compiled query execution against real database."""

    def setUp(self):
        self.related = RelatedModel.objects.create(
            name="Test Related", description="desc"
        )
        self.sample1 = CompiledSampleModel.objects.create(
            title="Book One",
            price=Decimal("29.99"),
            is_active=True,
            related=self.related,
        )
        self.sample2 = CompiledSampleModel.objects.create(
            title="Book Two",
            price=Decimal("49.50"),
            is_active=False,
            related=self.related,
        )
        self.plan = compile_model(CompiledSampleModel)

    def test_execute_simple_fields(self):
        qs = CompiledSampleModel.objects.all()
        compiled_qs, active_plan = self.plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = self.plan.post_process(rows, active_plan)

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["title"], "Book One")
        self.assertEqual(rows[1]["title"], "Book Two")
        self.assertIn("is_active", rows[0])

    def test_execute_fk_fields(self):
        qs = CompiledSampleModel.objects.all()
        compiled_qs, active_plan = self.plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = self.plan.post_process(rows, active_plan)

        self.assertEqual(rows[0]["related_name"], "Test Related")
        # Raw FK ID should also be present
        self.assertEqual(rows[0]["related"], self.related.pk)

    def test_execute_decimal_coercion(self):
        qs = CompiledSampleModel.objects.all()
        compiled_qs, active_plan = self.plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = self.plan.post_process(rows, active_plan)

        # Price should be coerced to string
        self.assertEqual(rows[0]["price"], "29.99")
        self.assertIsInstance(rows[0]["price"], str)

    def test_execute_property_fields(self):
        qs = CompiledSampleModel.objects.all()
        compiled_qs, active_plan = self.plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = self.plan.post_process(rows, active_plan)

        self.assertEqual(rows[0]["display_title"], "BOOK ONE")
        self.assertEqual(rows[1]["display_title"], "BOOK TWO")

    def test_execute_null_fk(self):
        """Null FK should produce None values."""
        article = CompiledArticle.objects.create(title="No Author", author=None)
        plan = compile_model(CompiledArticle)
        qs = CompiledArticle.objects.filter(pk=article.pk)
        compiled_qs, active_plan = plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = plan.post_process(rows, active_plan)

        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["author_name"])

    def test_execute_permission_filtering(self):
        """readable_fields should filter output fields."""
        qs = CompiledSampleModel.objects.all()
        # Only allow title and is_active
        readable = {"title", "is_active", "id"}
        compiled_qs, active_plan = self.plan.apply_to_queryset(qs, readable)
        rows = list(compiled_qs)
        rows = self.plan.post_process(rows, active_plan)

        self.assertEqual(len(rows), 2)
        self.assertIn("title", rows[0])
        self.assertIn("is_active", rows[0])
        # FK and property fields should be excluded
        self.assertNotIn("related_name", rows[0])
        self.assertNotIn("display_title", rows[0])
        # Price excluded (not in readable_fields)
        self.assertNotIn("price", rows[0])


class ComplexPropertyTests(TestCase):
    """Test properties that do various things."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Author", description="desc")
        self.sample = CompiledSampleModel.objects.create(
            title="Test Book",
            price=Decimal("29.99"),
            is_active=True,
            related=self.related,
        )

    def test_property_accessing_multiple_fields(self):
        """Property that reads price and is_active should work."""
        plan = compile_model(CompiledSampleModel)
        # Add price_label to the plan's property fields manually for this test
        plan.property_fields["price_label"] = CompiledSampleModel.price_label.fget

        qs = CompiledSampleModel.objects.all()
        compiled_qs, active_plan = plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = plan.post_process(rows, active_plan)

        # price is coerced to str by the time the property runs,
        # so it will use the string value
        self.assertIn("price_label", rows[0])
        self.assertEqual(rows[0]["price_label"], "$29.99 (active)")

    def test_property_accessing_related_object_fails(self):
        """Property that accesses related.name will fail because
        DictProxy only has flat dict values, not related objects."""
        plan = compile_model(CompiledSampleModel)
        plan.property_fields["related_author_name"] = (
            CompiledSampleModel.related_author_name.fget
        )

        qs = CompiledSampleModel.objects.all()
        compiled_qs, active_plan = plan.apply_to_queryset(qs)
        rows = list(compiled_qs)

        # The proxy has 'related' as an integer (FK ID), not an object.
        # Calling .name on an int will raise AttributeError.
        with self.assertRaises(AttributeError):
            plan.post_process(rows, active_plan)

    def test_dictproxy_has_fk_annotation_values(self):
        """DictProxy should have FK annotation values available."""
        plan = compile_model(CompiledSampleModel)
        qs = CompiledSampleModel.objects.all()
        compiled_qs, active_plan = plan.apply_to_queryset(qs)
        rows = list(compiled_qs)

        proxy = DictProxy(rows[0])
        # FK annotation value is available
        self.assertEqual(proxy.related_name, "Author")
        # Raw FK ID is available
        self.assertEqual(proxy.related, self.related.pk)


class CompiledM2MExecutionTests(TestCase):
    """Test M2M two-query merge."""

    def setUp(self):
        self.related = RelatedModel.objects.create(name="Author", description="desc")
        self.cat1 = Category.objects.create(name="Python", description="Python stuff")
        self.cat2 = Category.objects.create(name="Django", description="Django stuff")

        self.article1 = CompiledArticle.objects.create(
            title="Article One", author=self.related
        )
        self.article1.categories.add(self.cat1, self.cat2)

        self.article2 = CompiledArticle.objects.create(
            title="Article Two", author=self.related
        )
        self.article2.categories.add(self.cat1)

        self.plan = compile_model(CompiledArticle)

    def test_execute_m2m_fields(self):
        qs = CompiledArticle.objects.all()
        compiled_qs, active_plan = self.plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = self.plan.post_process(rows, active_plan)

        self.assertEqual(len(rows), 2)

        # Article 1 has 2 categories
        art1_cats = rows[0]["categories"]
        self.assertEqual(len(art1_cats), 2)
        cat_names = {c["name"] for c in art1_cats}
        self.assertEqual(cat_names, {"Python", "Django"})

        # Article 2 has 1 category
        art2_cats = rows[1]["categories"]
        self.assertEqual(len(art2_cats), 1)
        self.assertEqual(art2_cats[0]["name"], "Python")

    def test_execute_empty_m2m(self):
        """Articles with no categories should get empty list."""
        article3 = CompiledArticle.objects.create(
            title="No Categories", author=self.related
        )
        qs = CompiledArticle.objects.filter(pk=article3.pk)
        compiled_qs, active_plan = self.plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = self.plan.post_process(rows, active_plan)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["categories"], [])

    def test_m2m_fk_and_m2m_together(self):
        """FK annotations and M2M merge should work together."""
        qs = CompiledArticle.objects.all()
        compiled_qs, active_plan = self.plan.apply_to_queryset(qs)
        rows = list(compiled_qs)
        rows = self.plan.post_process(rows, active_plan)

        # FK annotation
        self.assertEqual(rows[0]["author_name"], "Author")
        # M2M merge
        self.assertIsInstance(rows[0]["categories"], list)
