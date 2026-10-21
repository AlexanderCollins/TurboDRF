"""
Test models for TurboDRF tests.
"""

from django.db import models
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response

from turbodrf.decorators import turbodrf_action
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
            },
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
            },
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


class Brokerage(models.Model):
    """Tenant model for predicate tests. Plain Django model — not TurboDRF-exposed."""

    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Deal(TurboDRFMixin, models.Model):
    """Tenant-scoped model with owner. Used by predicate / IDOR tests."""

    title = models.CharField(max_length=200)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="deals"
    )
    assigned_broker = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        related_name="assigned_deals",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.title

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "owner_field": "assigned_broker",
            "bypass_owner_roles": ["manager", "admin"],
            "fields": ["id", "title", "brokerage", "assigned_broker"],
            "compiled": False,
        }


class BankAccount(TurboDRFMixin, models.Model):
    """Bank account chained to a Deal. Tests __ traversal in tenant_field."""

    name = models.CharField(max_length=100)
    deal = models.ForeignKey(
        Deal, on_delete=models.CASCADE, related_name="bank_accounts"
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "deal__brokerage",
            "fields": ["id", "name", "deal"],
            "compiled": False,
        }


class Transaction(TurboDRFMixin, models.Model):
    """Transaction chained to a BankAccount.

    Tests two-hop tenant traversal (bank_account__deal__brokerage) and
    filter chaining (?bank_account=X must be filtered by tenant scope).
    """

    amount = models.DecimalField(max_digits=10, decimal_places=2)
    bank_account = models.ForeignKey(
        BankAccount, on_delete=models.CASCADE, related_name="transactions"
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.amount}"

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "bank_account__deal__brokerage",
            "fields": ["id", "amount", "bank_account"],
            "compiled": False,
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


@turbodrf_action(detail=True, methods=["get"], url_path="ping")
def widget_ping(self, request, pk=None):
    """Custom action handler — inherits get_object() scoping from the viewset."""
    obj = self.get_object()
    return Response({"pong": obj.name})


class DenyAll(BasePermission):
    """Permission class that refuses every request."""

    def has_permission(self, request, view):
        return False


@turbodrf_action(
    detail=True, methods=["post"], url_path="bump", permission_classes=[AllowAny]
)
def widget_bump(self, request, pk=None):
    """POST action on a read_only resource with its own permission_classes."""
    obj = self.get_object()
    return Response({"bumped": obj.name})


@turbodrf_action(
    detail=True, methods=["get"], url_path="locked", permission_classes=[DenyAll]
)
def widget_locked(self, request, pk=None):
    """GET action whose declared permission_classes deny everyone."""
    obj = self.get_object()
    return Response({"name": obj.name})


class Widget(TurboDRFMixin, models.Model):
    """Read-only model with a custom action.

    Exercises the turbodrf() config keys ``read_only`` (writes -> 405) and
    ``actions`` (a custom endpoint attached to the generated viewset).
    """

    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {
            "public_access": True,
            "read_only": True,
            "fields": ["id", "name"],
            "actions": [widget_ping, widget_bump, widget_locked],
        }


class Gadget(TurboDRFMixin, models.Model):
    """Writable, non-compiled model exercising full_clean + computed fields.

    ``label`` is a @property (a computed read field); ``clean()`` enforces a
    business rule surfaced as a 400 via the ``full_clean: True`` config.
    """

    name = models.CharField(max_length=100)
    qty = models.IntegerField(default=0)

    @property
    def label(self):
        return f"{self.name} x{self.qty}"

    def clean(self):
        from django.core.exceptions import ValidationError

        # Normalise: full_clean must persist values clean() changes.
        if self.name:
            self.name = self.name.strip().lower()
        if self.name == "forbidden":
            raise ValidationError({"name": "name cannot be 'forbidden'"})

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {
            "public_access": True,
            "compiled": False,
            "full_clean": True,
            "fields": ["id", "name", "qty", "label"],
        }


class Portfolio(TurboDRFMixin, models.Model):
    """Tenant-scoped FK target with no predicates (reached via Holding)."""

    name = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="portfolios"
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "fields": ["id", "name", "brokerage"],
            "compiled": False,
        }


class Holding(TurboDRFMixin, models.Model):
    """Tenant-scoped FK target whose own FK (portfolio) is nullable."""

    name = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="holdings"
    )
    portfolio = models.ForeignKey(
        Portfolio,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="holdings",
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "fields": ["id", "name", "brokerage", "portfolio"],
            "compiled": False,
        }


@turbodrf_action(
    detail=True,
    methods=["post"],
    url_path="reprocess",
    permission_classes=[IsAuthenticated],
)
def holding_event_reprocess(self, request, pk=None):
    """Declared permission_classes replace the role check; get_object() still
    applies the tenant boundary."""
    obj = self.get_object()
    return Response({"reprocessed": obj.pk})


class HoldingEvent(TurboDRFMixin, models.Model):
    """Compiled, tenant-scoped model with a NULLABLE FK to a tenant-scoped
    target, nested fields across it (including one ending on an FK), and
    @property fields — one computable from the row, one that reads the
    related object."""

    status = models.CharField(max_length=32, default="new")
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="holding_events"
    )
    holding = models.ForeignKey(
        Holding,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )

    searchable_fields = ["status", "holding__name"]

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.status

    @property
    def status_label(self):
        return self.status.replace("_", " ").title()

    @property
    def holding_label(self):
        # Reads the related object — not computable from a .values() row.
        return self.holding.name.upper() if self.holding else None

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "compiled": True,
            "fields": [
                "id",
                "status",
                "brokerage",
                "holding",
                "holding__name",
                "holding__portfolio",
                "status_label",
                "holding_label",
            ],
            "actions": [holding_event_reprocess],
        }
