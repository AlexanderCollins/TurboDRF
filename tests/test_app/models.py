"""
Test models for TurboDRF tests.
"""

from django.db import models

from turbodrf.mixins import TurboDRFMixin


class RelatedModel(TurboDRFMixin, models.Model):
    """A related model for testing relationships."""

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {"public_access": True, "fields": ["name", "description"]}


class SampleModel(TurboDRFMixin, models.Model):
    """Main test model with various field types."""

    # Basic fields
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # Numeric fields
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.IntegerField(default=0)

    # Date fields
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_date = models.DateField(null=True, blank=True)

    # Boolean field
    is_active = models.BooleanField(default=True)

    # Relationship fields
    related = models.ForeignKey(
        RelatedModel, on_delete=models.CASCADE, related_name="test_models"
    )

    # Secret field (for testing permissions)
    secret_field = models.CharField(max_length=100, blank=True)

    # Define searchable fields
    searchable_fields = ["title", "description"]

    class Meta:
        ordering = ["id"]
        db_table = "test_app_testmodel"  # Keep the same table name for compatibility

    def __str__(self):
        return self.title

    @classmethod
    def turbodrf(cls):
        return {
            "public_access": True,
            "fields": {
                "list": ["title", "price", "related__name", "is_active"],
                "detail": [
                    "title",
                    "description",
                    "price",
                    "quantity",
                    "related__name",
                    "related__description",
                    "is_active",
                    "secret_field",
                    "created_at",
                    "updated_at",
                    "published_date",
                ],
            }
        }


class NoTurboDRFModel(models.Model):
    """Model without TurboDRF mixin for testing."""

    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class CustomEndpointModel(TurboDRFMixin, models.Model):
    """Model with custom endpoint configuration."""

    name = models.CharField(max_length=100)

    @classmethod
    def turbodrf(cls):
        return {"public_access": True, "endpoint": "custom-items", "fields": ["name"]}


class DisabledModel(TurboDRFMixin, models.Model):
    """Model with TurboDRF disabled."""

    name = models.CharField(max_length=100)

    @classmethod
    def turbodrf(cls):
        return {"enabled": False, "fields": ["name"]}


class Category(TurboDRFMixin, models.Model):
    """Category model for testing ManyToMany relationships."""

    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {"public_access": True, "fields": ["name", "description"]}


class ArticleWithCategories(TurboDRFMixin, models.Model):
    """Test model with ManyToMany relationships."""

    title = models.CharField(max_length=200)
    content = models.TextField(blank=True)
    author = models.ForeignKey(
        RelatedModel, on_delete=models.CASCADE, related_name="articles", null=True
    )
    categories = models.ManyToManyField(Category, related_name="articles", blank=True)

    def __str__(self):
        return self.title

    @classmethod
    def turbodrf(cls):
        return {
            "public_access": True,
            "fields": {
                "list": ["title", "author__name", "categories__name"],
                "detail": [
                    "title",
                    "content",
                    "author__name",
                    "categories__name",
                    "categories__description",
                ],
            }
        }


class CompiledSampleModel(TurboDRFMixin, models.Model):
    """Test model with compiled read path enabled."""

    title = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    is_active = models.BooleanField(default=True)
    related = models.ForeignKey(
        RelatedModel,
        on_delete=models.CASCADE,
        related_name="compiled_samples",
    )

    searchable_fields = ["title"]

    @property
    def display_title(self):
        return self.title.upper()

    @property
    def price_label(self):
        """Property that accesses multiple fields."""
        if self.price and self.is_active:
            return f"${self.price} (active)"
        return f"${self.price} (inactive)"

    @property
    def related_author_name(self):
        """Property that tries to access a related object — this WILL fail
        with DictProxy because related objects aren't in the dict."""
        return self.related.name

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.title

    @classmethod
    def turbodrf(cls):
        return {
            "public_access": True,
            "compiled": True,
            "fields": {
                "list": [
                    "title",
                    "price",
                    "related__name",
                    "is_active",
                    "display_title",
                ],
                "detail": ["title", "price", "related__name", "is_active"],
            },
        }


class CompiledArticle(TurboDRFMixin, models.Model):
    """Test model with compiled read path and M2M relationships."""

    title = models.CharField(max_length=200)
    author = models.ForeignKey(
        RelatedModel,
        on_delete=models.CASCADE,
        related_name="compiled_articles",
        null=True,
    )
    categories = models.ManyToManyField(
        Category, related_name="compiled_articles", blank=True
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.title

    @classmethod
    def turbodrf(cls):
        return {
            "public_access": True,
            "compiled": True,
            "fields": {
                "list": ["title", "author__name", "categories__name"],
                "detail": [
                    "title",
                    "author__name",
                    "categories__name",
                    "categories__description",
                ],
            },
        }
