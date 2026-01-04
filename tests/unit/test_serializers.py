"""
Unit tests for TurboDRF serializers.

Tests the dynamic serializer generation and nested field handling.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from tests.test_app.models import RelatedModel, SampleModel
from turbodrf.serializers import TurboDRFSerializer, TurboDRFSerializerFactory

User = get_user_model()


class TestTurboDRFSerializer(TestCase):
    """Test cases for TurboDRF serializer functionality."""

    def setUp(self):
        """Set up test fixtures."""
        # Create related model instance
        self.related = RelatedModel.objects.create(
            name="Related Item", description="A related item for testing"
        )

        # Create test model instance
        self.test_obj = SampleModel.objects.create(
            title="Test Item",
            description="Test description",
            price=Decimal("99.99"),
            quantity=10,
            related=self.related,
            secret_field="Secret data",
            is_active=True,
        )

    def test_basic_serialization(self):
        """Test basic serialization without nested fields."""
        # Create a simple serializer
        SimpleSerializer = type(
            "SimpleSerializer",
            (TurboDRFSerializer,),
            {
                "Meta": type(
                    "Meta",
                    (),
                    {"model": SampleModel, "fields": ["title", "price", "is_active"]},
                )
            },
        )

        serializer = SimpleSerializer(self.test_obj)
        data = serializer.data

        self.assertEqual(data["title"], "Test Item")
        self.assertEqual(data["price"], "99.99")
        self.assertTrue(data["is_active"])
        self.assertNotIn("description", data)
        self.assertNotIn("secret_field", data)

    def test_nested_field_serialization(self):
        """Test serialization with nested fields."""
        # Create serializer with nested fields
        NestedSerializer = type(
            "NestedSerializer",
            (TurboDRFSerializer,),
            {
                "Meta": type(
                    "Meta",
                    (),
                    {
                        "model": SampleModel,
                        "fields": ["title", "related"],
                        "_nested_fields": {
                            "related": ["related__name", "related__description"]
                        },
                    },
                )
            },
        )

        serializer = NestedSerializer(self.test_obj)
        data = serializer.data

        self.assertEqual(data["title"], "Test Item")
        self.assertIn("related", data)
        self.assertEqual(data["related"], self.related.id)
        self.assertEqual(data["related_name"], "Related Item")
        self.assertEqual(data["related_description"], "A related item for testing")

    def test_to_representation_with_nested_fields(self):
        """Test the to_representation method handles nested fields correctly."""

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = SampleModel
                fields = ["title", "related"]
                _nested_fields = {"related": ["related__name"]}

        serializer = TestSerializer(self.test_obj)
        data = serializer.data

        self.assertIn("related_name", data)
        self.assertEqual(data["related_name"], "Related Item")

    def test_none_handling_in_nested_fields(self):
        """Test that None values are handled properly in nested fields."""

        # Test with a nullable field (published_date can be None)
        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = SampleModel
                fields = ["title", "published_date"]
                _nested_fields = {
                    "published_date": [
                        "year",
                        "month",
                    ]  # Accessing attributes of a None date
                }

        # Create object with None published_date
        test_obj = SampleModel.objects.create(
            title="No Date",
            price=Decimal("50.00"),
            quantity=5,
            related=self.related,
            published_date=None,
        )

        serializer = TestSerializer(test_obj)
        data = serializer.data

        # Should not raise an error and should handle None gracefully
        self.assertIn("title", data)
        self.assertEqual(data["title"], "No Date")
        # These should be None, not cause an error
        self.assertIn("published_date_year", data)
        self.assertIsNone(data["published_date_year"])
        self.assertIn("published_date_month", data)
        self.assertIsNone(data["published_date_month"])


class TestTurboDRFSerializerFactory(TestCase):
    """Test cases for TurboDRF serializer factory."""

    def setUp(self):
        """Set up test fixtures."""
        from django.core.cache import cache

        cache.clear()  # Clear cache to avoid test pollution

        # Create users with different roles
        self.admin_user = User.objects.create_user(username="admin", is_superuser=True)
        self.admin_user._test_roles = ["admin"]
        self.editor_user = User.objects.create_user(username="editor", is_staff=True)
        self.editor_user._test_roles = ["editor"]
        self.viewer_user = User.objects.create_user(username="viewer")
        self.viewer_user._test_roles = ["viewer"]

        # Create test data
        self.related = RelatedModel.objects.create(
            name="Related", description="Description"
        )
        self.test_obj = SampleModel.objects.create(
            title="Test",
            price=Decimal("100.00"),
            quantity=1,
            related=self.related,
            secret_field="Secret",
        )

    def test_create_serializer_with_permissions(self):
        """Test creating serializer with field permissions."""
        fields = ["title", "price", "secret_field"]

        # Admin should see all fields
        AdminSerializer = TurboDRFSerializerFactory.create_serializer(
            SampleModel, fields, self.admin_user
        )
        serializer = AdminSerializer(self.test_obj)
        data = serializer.data

        self.assertIn("title", data)
        self.assertIn("price", data)
        self.assertIn("secret_field", data)

    def test_get_permitted_fields(self):
        """Test field permission filtering."""
        fields = ["title", "price", "secret_field"]

        # Admin should get all fields
        admin_fields = TurboDRFSerializerFactory._get_permitted_fields(
            SampleModel, fields, self.admin_user
        )
        self.assertEqual(set(admin_fields), set(fields))

        # Editor should get title and price (read permission)
        editor_fields = TurboDRFSerializerFactory._get_permitted_fields(
            SampleModel, fields, self.editor_user
        )
        self.assertIn("title", editor_fields)
        self.assertIn("price", editor_fields)
        self.assertNotIn("secret_field", editor_fields)

        # Viewer should only get title
        viewer_fields = TurboDRFSerializerFactory._get_permitted_fields(
            SampleModel, fields, self.viewer_user
        )
        self.assertIn("title", viewer_fields)
        self.assertNotIn("price", viewer_fields)
        self.assertNotIn("secret_field", viewer_fields)

    def test_get_read_only_fields(self):
        """Test read-only field determination."""
        fields = ["title", "price"]

        # Admin can write all fields
        admin_readonly = TurboDRFSerializerFactory._get_read_only_fields(
            SampleModel, fields, self.admin_user
        )
        self.assertNotIn("price", admin_readonly)

        # Editor cannot write price
        editor_readonly = TurboDRFSerializerFactory._get_read_only_fields(
            SampleModel, fields, self.editor_user
        )
        self.assertIn("price", editor_readonly)

        # Viewer cannot write anything
        viewer_readonly = TurboDRFSerializerFactory._get_read_only_fields(
            SampleModel, fields, self.viewer_user
        )
        self.assertIn("title", viewer_readonly)
        self.assertIn("price", viewer_readonly)

    def test_create_nested_serializer(self):
        """Test nested serializer creation."""
        NestedSerializer = TurboDRFSerializerFactory._create_nested_serializer(
            RelatedModel, ["name", "description"], self.admin_user
        )

        serializer = NestedSerializer(self.related)
        data = serializer.data

        self.assertEqual(data["name"], "Related")
        self.assertEqual(data["description"], "Description")

    def test_handle_nested_fields_in_factory(self):
        """Test factory handling of nested field notation."""
        fields = ["title", "related__name", "related__description"]

        # This should create appropriate nested serializers
        SerializerClass = TurboDRFSerializerFactory.create_serializer(
            SampleModel, fields, self.admin_user
        )

        # Check that the serializer works
        serializer = SerializerClass(self.test_obj)
        data = serializer.data

        self.assertIn("title", data)
        # The factory should handle nested fields appropriately
        # (exact behavior depends on implementation)


class TestSerializerRefNameUniqueness(TestCase):
    """Test cases for unique ref_name generation in serializers (Issue #10)."""

    def setUp(self):
        """Set up test fixtures."""
        self.admin_user = User.objects.create_user(username="admin", is_superuser=True)
        self.admin_user._test_roles = ["admin"]

        self.related = RelatedModel.objects.create(
            name="Related", description="Description"
        )
        self.test_obj = SampleModel.objects.create(
            title="Test",
            price=Decimal("100.00"),
            quantity=1,
            related=self.related,
        )

    def test_different_fields_produce_different_ref_names(self):
        """Test that serializers with different fields have unique ref_names."""
        # Create two serializers with different field sets
        Serializer1 = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["title", "price"], self.admin_user, view_type="list"
        )
        Serializer2 = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["title", "description"], self.admin_user, view_type="list"
        )

        # Get ref_names from Meta
        ref_name_1 = Serializer1.Meta.ref_name
        ref_name_2 = Serializer2.Meta.ref_name

        # They should be different
        self.assertNotEqual(ref_name_1, ref_name_2)

        # Both should contain the app_label and model_name
        self.assertIn("test_app", ref_name_1)
        self.assertIn("samplemodel", ref_name_1)
        self.assertIn("test_app", ref_name_2)
        self.assertIn("samplemodel", ref_name_2)

    def test_same_fields_produce_same_ref_name(self):
        """Test that serializers with identical fields have the same ref_name."""
        # Create two serializers with the same field sets
        Serializer1 = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["title", "price"], self.admin_user, view_type="list"
        )
        Serializer2 = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["title", "price"], self.admin_user, view_type="list"
        )

        # Get ref_names from Meta
        ref_name_1 = Serializer1.Meta.ref_name
        ref_name_2 = Serializer2.Meta.ref_name

        # They should be the same (consistent hashing)
        self.assertEqual(ref_name_1, ref_name_2)

    def test_different_view_types_produce_different_ref_names(self):
        """Test that different view types produce different ref_names."""
        # Create serializers with same fields but different view types
        ListSerializer = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["title", "price"], self.admin_user, view_type="list"
        )
        DetailSerializer = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["title", "price"], self.admin_user, view_type="detail"
        )

        # Get ref_names from Meta
        list_ref_name = ListSerializer.Meta.ref_name
        detail_ref_name = DetailSerializer.Meta.ref_name

        # They should be different
        self.assertNotEqual(list_ref_name, detail_ref_name)

        # Should contain the view_type
        self.assertIn("list", list_ref_name)
        self.assertIn("detail", detail_ref_name)

    def test_ref_name_format(self):
        """Test that ref_name follows the expected format."""
        SerializerClass = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["title", "price"], self.admin_user, view_type="list"
        )

        ref_name = SerializerClass.Meta.ref_name

        # Should be in format: app_label_model_name_view_type_hash
        parts = ref_name.split("_")

        self.assertGreaterEqual(len(parts), 4, "ref_name should have at least 4 parts")
        self.assertEqual(parts[0], "test")
        self.assertEqual(parts[1], "app")
        self.assertEqual(parts[2], "samplemodel")
        self.assertEqual(parts[3], "list")
        # Last part should be the hash (8 characters)
        self.assertEqual(len(parts[4]), 8)

    def test_nested_serializer_has_unique_ref_name(self):
        """Test that nested serializers also have unique ref_names."""
        NestedSerializer = TurboDRFSerializerFactory._create_nested_serializer(
            RelatedModel, ["name", "description"], self.admin_user
        )

        # Should have a ref_name
        self.assertTrue(hasattr(NestedSerializer.Meta, "ref_name"))

        ref_name = NestedSerializer.Meta.ref_name

        # Should contain app_label, model_name, and "nested"
        self.assertIn("test_app", ref_name)
        self.assertIn("relatedmodel", ref_name)
        self.assertIn("nested", ref_name)

    def test_different_nested_fields_produce_different_ref_names(self):
        """Test that nested serializers with different fields have unique ref_names."""
        NestedSerializer1 = TurboDRFSerializerFactory._create_nested_serializer(
            RelatedModel, ["name"], self.admin_user
        )
        NestedSerializer2 = TurboDRFSerializerFactory._create_nested_serializer(
            RelatedModel, ["name", "description"], self.admin_user
        )

        ref_name_1 = NestedSerializer1.Meta.ref_name
        ref_name_2 = NestedSerializer2.Meta.ref_name

        # They should be different
        self.assertNotEqual(ref_name_1, ref_name_2)

    def test_field_order_does_not_affect_ref_name(self):
        """Test that field order doesn't affect ref_name (fields are sorted)."""
        # Create serializers with same fields in different order
        Serializer1 = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["title", "price", "description"], self.admin_user
        )
        Serializer2 = TurboDRFSerializerFactory.create_serializer(
            SampleModel, ["price", "description", "title"], self.admin_user
        )

        ref_name_1 = Serializer1.Meta.ref_name
        ref_name_2 = Serializer2.Meta.ref_name

        # They should be the same (fields are sorted before hashing)
        self.assertEqual(ref_name_1, ref_name_2)


class TestManyToManyFieldSerialization(TestCase):
    """Test cases for ManyToMany field serialization."""

    def setUp(self):
        """Set up test fixtures."""
        from django.core.cache import cache

        from tests.test_app.models import ArticleWithCategories, Category

        cache.clear()  # Clear cache to avoid test pollution

        # Create test users
        self.admin_user = User.objects.create_user(username="admin", is_superuser=True)
        self.admin_user._test_roles = ["admin"]

        # Create categories
        self.category1 = Category.objects.create(
            name="Sales Enablement", description="Sales-related content"
        )
        self.category2 = Category.objects.create(
            name="Marketing", description="Marketing-related content"
        )
        self.category3 = Category.objects.create(
            name="Engineering", description="Technical content"
        )

        # Create author
        self.author = RelatedModel.objects.create(
            name="John Doe", description="Content author"
        )

        # Create article with categories
        self.article = ArticleWithCategories.objects.create(
            title="Test Article", content="Article content", author=self.author
        )
        self.article.categories.add(self.category1, self.category2)

    def test_m2m_field_serialization_as_array_of_objects(self):
        """Test that M2M fields are serialized as arrays of objects."""
        from tests.test_app.models import ArticleWithCategories

        # Create serializer with M2M nested fields
        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "categories"]
                _nested_fields = {
                    "categories": ["categories__name", "categories__description"]
                }

        serializer = TestSerializer(self.article)
        data = serializer.data

        # Check that categories is an array
        self.assertIn("categories", data)
        self.assertIsInstance(data["categories"], list)
        self.assertEqual(len(data["categories"]), 2)

        # Check that each category is an object with name and description
        category_names = {cat["name"] for cat in data["categories"]}
        self.assertIn("Sales Enablement", category_names)
        self.assertIn("Marketing", category_names)

        # Check that description is included
        for cat in data["categories"]:
            self.assertIn("name", cat)
            self.assertIn("description", cat)
            if cat["name"] == "Sales Enablement":
                self.assertEqual(cat["description"], "Sales-related content")

    def test_m2m_field_with_single_nested_field(self):
        """Test M2M serialization with only one nested field."""
        from tests.test_app.models import ArticleWithCategories

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "categories"]
                _nested_fields = {"categories": ["categories__name"]}

        serializer = TestSerializer(self.article)
        data = serializer.data

        # Should still be an array of objects
        self.assertIsInstance(data["categories"], list)
        self.assertEqual(len(data["categories"]), 2)

        # Each object should only have 'name' field
        for cat in data["categories"]:
            self.assertIn("name", cat)
            self.assertEqual(len(cat.keys()), 1)  # Only 'name' field

    def test_m2m_vs_fk_field_handling(self):
        """Test that M2M fields are handled differently from FK fields."""
        from tests.test_app.models import ArticleWithCategories

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "author", "categories"]
                _nested_fields = {
                    "author": ["author__name"],  # FK - should be flat
                    "categories": ["categories__name"],  # M2M - should be array
                }

        serializer = TestSerializer(self.article)
        data = serializer.data

        # FK field should create flat field
        self.assertIn("author_name", data)
        self.assertEqual(data["author_name"], "John Doe")

        # M2M field should create array
        self.assertIn("categories", data)
        self.assertIsInstance(data["categories"], list)
        self.assertEqual(len(data["categories"]), 2)

    def test_empty_m2m_field(self):
        """Test serialization when M2M field has no relations."""
        from tests.test_app.models import ArticleWithCategories

        # Create article with no categories
        empty_article = ArticleWithCategories.objects.create(
            title="Empty Article", content="No categories", author=self.author
        )

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "categories"]
                _nested_fields = {"categories": ["categories__name"]}

        serializer = TestSerializer(empty_article)
        data = serializer.data

        # Should return empty array
        self.assertIn("categories", data)
        self.assertIsInstance(data["categories"], list)
        self.assertEqual(len(data["categories"]), 0)

    def test_m2m_field_permission_filtering(self):
        """Test that permission filtering works for M2M nested fields."""
        from tests.test_app.models import ArticleWithCategories

        # Use factory to create serializer with permissions
        fields = ["title", "categories__name", "categories__description"]

        SerializerClass = TurboDRFSerializerFactory.create_serializer(
            ArticleWithCategories, fields, self.admin_user
        )

        serializer = SerializerClass(self.article)
        data = serializer.data

        # Admin should see categories as array
        self.assertIn("categories", data)
        self.assertIsInstance(data["categories"], list)
        # Should have 2 categories
        self.assertEqual(len(data["categories"]), 2)
        # Each category should have name and description
        for cat in data["categories"]:
            self.assertIn("name", cat)
            self.assertIn("description", cat)

    def test_m2m_serialization_preserves_order(self):
        """Test that M2M serialization returns items in consistent order."""
        from tests.test_app.models import ArticleWithCategories

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "categories"]
                _nested_fields = {"categories": ["categories__name"]}

        # Serialize multiple times
        data1 = TestSerializer(self.article).data
        data2 = TestSerializer(self.article).data

        # Order should be consistent
        names1 = [cat["name"] for cat in data1["categories"]]
        names2 = [cat["name"] for cat in data2["categories"]]
        self.assertEqual(names1, names2)

    def test_m2m_with_null_field_values(self):
        """Test M2M serialization when nested fields have null values."""
        from tests.test_app.models import ArticleWithCategories, Category

        # Create category with no description (blank field)
        empty_cat = Category.objects.create(name="Empty Category", description="")

        article = ArticleWithCategories.objects.create(title="Test", author=self.author)
        article.categories.add(empty_cat)

        class TestSerializer(TurboDRFSerializer):
            class Meta:
                model = ArticleWithCategories
                fields = ["title", "categories"]
                _nested_fields = {
                    "categories": ["categories__name", "categories__description"]
                }

        serializer = TestSerializer(article)
        data = serializer.data

        # Should handle empty description gracefully
        self.assertEqual(len(data["categories"]), 1)
        self.assertEqual(data["categories"][0]["name"], "Empty Category")
        self.assertEqual(data["categories"][0]["description"], "")

    def test_factory_creates_proper_m2m_metadata(self):
        """Test that SerializerFactory creates proper _nested_fields for M2M."""
        from tests.test_app.models import ArticleWithCategories

        fields = ["title", "categories__name", "categories__description"]

        SerializerClass = TurboDRFSerializerFactory.create_serializer(
            ArticleWithCategories, fields, self.admin_user
        )

        # Check that _nested_fields metadata is created
        self.assertTrue(hasattr(SerializerClass.Meta, "_nested_fields"))

        # Check that categories is in nested_fields
        nested_fields = SerializerClass.Meta._nested_fields
        self.assertIn("categories", nested_fields)

        # Check that it includes both nested field paths
        self.assertIn("categories__name", nested_fields["categories"])
        self.assertIn("categories__description", nested_fields["categories"])
