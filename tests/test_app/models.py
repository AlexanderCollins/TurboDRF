"""
Test models for TurboDRF tests.
"""

import uuid

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
        """Property that reads a related object: in the sandbox it needs
        ``related`` declared in ``property_relations``."""
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
            "property_relations": {"holding_label": ["holding__name"]},
            "actions": [holding_event_reprocess],
        }


class LiveFundManager(models.Manager):
    """Soft-delete default manager: hides deleted funds."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted=False)


class Fund(TurboDRFMixin, models.Model):
    """Tenant-scoped FK target with a soft-delete DEFAULT manager and no manager
    named ``objects``."""

    name = models.CharField(max_length=100)
    nav = models.DecimalField(max_digits=24, decimal_places=8, default=0)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="funds"
    )
    deleted = models.BooleanField(default=False)

    live = LiveFundManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "fields": ["id", "name", "nav", "brokerage"],
        }


class Valuation(TurboDRFMixin, models.Model):
    """Compiled model with values DRF formats differently from their raw
    Python value (decimals, datetimes, times, durations) and nested fields
    across an FK to a soft-deletable target."""

    units = models.DecimalField(max_digits=24, decimal_places=8)
    valued_at = models.DateTimeField(null=True, blank=True)
    settles_at = models.TimeField(null=True, blank=True)
    period = models.DurationField(null=True, blank=True)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="valuations"
    )
    fund = models.ForeignKey(Fund, on_delete=models.CASCADE, related_name="valuations")

    searchable_fields = ["fund__name"]

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "compiled": True,
            "fields": [
                "id",
                "units",
                "valued_at",
                "settles_at",
                "period",
                "brokerage",
                "fund",
                "fund__name",
                "fund__nav",
            ],
        }


# ---------------------------------------------------------------------------
# Compiled-vs-serializer parity parity models
# (tests/integration/test_parity_compiled_vs_serializer.py). Every common field type, nullable
# and not, plus FK / O2O / M2M targets for nested paths. Scenarios patch the
# turbodrf() config per test, so the configs below are only defaults.
# ---------------------------------------------------------------------------


class ParityCurrency(models.Model):
    """FK target referenced through ``to_field`` (a non-pk unique column)."""

    code = models.CharField(max_length=3, unique=True)
    name = models.CharField(max_length=50)

    def __str__(self):
        return self.code


class ParityLeaf(models.Model):
    """Third hop of the ``target__leaf__*`` chain."""

    name = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    seen_at = models.DateTimeField(null=True)

    def __str__(self):
        return self.name


class ParityTarget(TurboDRFMixin, models.Model):
    """FK / O2O target with a nullable FK of its own (``leaf``)."""

    name = models.CharField(max_length=50)
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True)
    seen_at = models.DateTimeField(null=True)
    span = models.DurationField(null=True)
    uid = models.UUIDField(null=True)
    doc = models.FileField(blank=True)
    leaf = models.ForeignKey(
        ParityLeaf, null=True, blank=True, on_delete=models.SET_NULL
    )
    partner = models.OneToOneField(
        ParityLeaf,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="partner_of",
    )
    currency = models.ForeignKey(
        ParityCurrency,
        to_field="code",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.name

    @property
    def label(self):
        return self.name.upper()

    @classmethod
    def turbodrf(cls):
        return {"public_access": True, "fields": ["id", "name", "amount"]}


class ParityActiveManager(models.Manager):
    """Default manager that hides soft-deleted rows."""

    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)


class ParityTag(models.Model):
    """M2M target: soft-delete default manager and a non-pk Meta.ordering."""

    name = models.CharField(max_length=50)
    weight = models.DecimalField(max_digits=6, decimal_places=2, null=True)
    created = models.DateTimeField(null=True)
    leaf = models.ForeignKey(
        ParityLeaf, null=True, blank=True, on_delete=models.SET_NULL
    )
    currency = models.ForeignKey(
        ParityCurrency,
        to_field="code",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    is_deleted = models.BooleanField(default=False)

    objects = ParityActiveManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ["-name"]

    def __str__(self):
        return self.name

    @property
    def label(self):
        return self.name.upper()


class ParityAllTypes(TurboDRFMixin, models.Model):
    """One column per common Django field type, nullable and non-null."""

    STATUS_CHOICES = [("draft", "Draft"), ("live", "Live")]
    LEVEL_CHOICES = [(1, "Low"), (2, "High")]

    char = models.CharField(max_length=50)
    char_null = models.CharField(max_length=50, null=True, blank=True)
    text = models.TextField(blank=True)
    text_null = models.TextField(null=True, blank=True)
    integer = models.IntegerField(default=0)
    integer_null = models.IntegerField(null=True, blank=True)
    big = models.BigIntegerField(default=0)
    big_null = models.BigIntegerField(null=True, blank=True)
    positive = models.PositiveIntegerField(default=0)
    positive_null = models.PositiveIntegerField(null=True, blank=True)
    flt = models.FloatField(default=0.0)
    flt_null = models.FloatField(null=True, blank=True)
    flag = models.BooleanField(default=False)
    flag_null = models.BooleanField(null=True, blank=True)
    day = models.DateField(null=True, blank=True)
    moment = models.DateTimeField(null=True, blank=True)
    clock = models.TimeField(null=True, blank=True)
    span = models.DurationField(null=True, blank=True)
    uid = models.UUIDField(null=True, blank=True)
    data = models.JSONField(default=dict, blank=True)
    data_null = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="draft")
    status_null = models.CharField(
        max_length=10, choices=STATUS_CHOICES, null=True, blank=True
    )
    level = models.IntegerField(choices=LEVEL_CHOICES, default=1)
    email = models.EmailField(blank=True)
    url = models.URLField(blank=True)
    slug = models.SlugField(blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    attachment = models.FileField(blank=True)
    picture = models.ImageField(blank=True)
    blob = models.BinaryField(null=True, blank=True, editable=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    amount_null = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    target = models.ForeignKey(
        ParityTarget,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rows",
    )
    twin = models.OneToOneField(
        ParityTarget,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="twin_row",
    )
    currency = models.ForeignKey(
        ParityCurrency,
        to_field="code",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    tags = models.ManyToManyField(ParityTag, blank=True, related_name="rows")

    searchable_fields = ["char", "target__name"]

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.char

    @property
    def char_upper(self):
        """Computable from the .values() row."""
        return self.char.upper()

    @property
    def target_label(self):
        """Reads the related object."""
        return self.target.name if self.target else None

    @property
    def target_label_safe(self):
        """Reads the related object defensively (getattr with a default)."""
        target = getattr(self, "target", None)
        return target.name if target is not None else "no-target"

    @property
    def kind(self):
        return type(self).__name__

    @classmethod
    def turbodrf(cls):
        return {
            "public_access": True,
            "fields": ["id", "char", "integer", "target", "target__name"],
        }


class ParityCodePk(TurboDRFMixin, models.Model):
    """Custom (char) primary key name."""

    code = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=50)
    target = models.ForeignKey(
        ParityTarget, null=True, blank=True, on_delete=models.SET_NULL
    )
    tags = models.ManyToManyField(ParityTag, blank=True, related_name="code_rows")

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code

    @classmethod
    def turbodrf(cls):
        return {
            "public_access": True,
            "fields": ["code", "name", "target__name", "tags__name"],
        }


class ParityUuidPk(TurboDRFMixin, models.Model):
    """UUID primary key; the default config does NOT list the pk."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50)
    rank = models.IntegerField(default=0)
    tags = models.ManyToManyField(ParityTag, blank=True, related_name="uuid_rows")

    class Meta:
        ordering = ["rank"]

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        return {"public_access": True, "fields": ["name", "rank"]}


class ParityBase(models.Model):
    """Concrete parent for a multi-table-inheritance child."""

    name = models.CharField(max_length=50)

    def __str__(self):
        return self.name


class ParityChild(TurboDRFMixin, ParityBase):
    """Multi-table-inheritance child: its pk is the ``paritybase_ptr`` O2O."""

    extra = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["paritybase_ptr"]

    @classmethod
    def turbodrf(cls):
        return {"public_access": True, "fields": ["id", "name", "extra"]}


# ---------------------------------------------------------------------------
# Security-review regression models (row scope / query surface), used by
# tests/integration/test_review_row_scope.py.
# ---------------------------------------------------------------------------


class ScopeFolder(TurboDRFMixin, models.Model):
    """Owner-private, tenant-scoped JOIN target. ``date`` is named like a
    lookup; Meta.ordering is a value column."""

    name = models.CharField(max_length=100)
    date = models.DateField(null=True, blank=True)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="scope_folders"
    )
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scope_folders",
    )

    searchable_fields = ["name", "items__label"]

    class Meta:
        ordering = ["name"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "owner_field": "owner",
            "fields": ["id", "name", "date", "brokerage", "owner"],
            "compiled": False,
        }


class ScopeItem(TurboDRFMixin, models.Model):
    """Tenant-only model with a nested field across a nullable FK into the
    owner-private ScopeFolder (compiled: False — the documented gate fix)."""

    label = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="scope_items"
    )
    folder = models.ForeignKey(
        ScopeFolder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="items",
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "fields": ["id", "label", "brokerage", "folder", "folder__name"],
            "compiled": False,
        }


class ScopeTag(TurboDRFMixin, models.Model):
    """Tenant-only M2M target whose own FK points at the owner-private
    ScopeFolder."""

    name = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="scope_tags"
    )
    folder = models.ForeignKey(
        ScopeFolder,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tags",
    )

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "fields": ["id", "name", "brokerage", "folder"],
            "compiled": False,
        }


class ScopeTagged(TurboDRFMixin, models.Model):
    """Compiled model nesting a two-hop M2M sub-field (tags__folder__name)."""

    title = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="scope_tagged"
    )
    tags = models.ManyToManyField(ScopeTag, related_name="tagged", blank=True)

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        # Not compiled: the boot gate refuses a compiled M2M sub-field that
        # reaches the owner-scoped ScopeFolder. The test compiles it
        # explicitly to check the runtime scoping behind that gate.
        return {
            "tenant_field": "brokerage",
            "compiled": False,
            "fields": {
                "list": ["id", "title", "tags__name", "tags__folder__name"],
                "detail": ["id", "title", "tags__name"],
            },
        }


def _scope_board_visibility():
    from turbodrf.predicates import Either, Members, Owner

    return [Either(Owner("owner", bypass=["manager"]), Members("watchers"))]


class ScopeBoard(TurboDRFMixin, models.Model):
    """Either(Owner(bypass), Members) — the composition AGENTS.md documents."""

    title = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="scope_boards"
    )
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scope_boards",
    )
    watchers = models.ManyToManyField(
        "auth.User", related_name="watched_scope_boards", blank=True
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "visibility": _scope_board_visibility(),
            "read_only": True,
            "fields": ["id", "title", "owner"],
            "compiled": False,
        }


class ScopeLoose(TurboDRFMixin, models.Model):
    """Target of the tenancy-gate tests (its config is patched in-test)."""

    title = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="scope_loose"
    )
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scope_loose",
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "fields": ["id", "title", "owner"],
            "compiled": False,
        }


class ScopeMultiOwner(TurboDRFMixin, models.Model):
    """Multi-owner sugar: owner_field=['owner', 'editor'] → Either(Owner, Owner)."""

    title = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="scope_multi"
    )
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scope_multi_owned",
    )
    editor = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scope_multi_edited",
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "owner_field": ["owner", "editor"],
            "fields": ["id", "title", "owner", "editor"],
            "compiled": False,
        }


class ScopeLedger(models.Model):
    """Plain (non-TurboDRF) intermediate hop on a chained tenant path. Its
    tenant FK is named differently from TURBODRF_TENANT_USER_FIELD."""

    name = models.CharField(max_length=100)
    firm = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="scope_ledgers"
    )


class ScopeEntry(TurboDRFMixin, models.Model):
    """Chained tenancy through a plain model: tenant_field='ledger__firm'."""

    memo = models.CharField(max_length=100)
    ledger = models.ForeignKey(
        ScopeLedger, on_delete=models.CASCADE, related_name="entries"
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "ledger__firm",
            "fields": ["id", "memo", "ledger"],
            "compiled": False,
        }


# ---------------------------------------------------------------------------
# Security-review regression models (field-level RBAC / write path), used by
# tests/integration/test_review_field_rbac.py.
# ---------------------------------------------------------------------------


class RbacProject(models.Model):
    """Plain (non-TurboDRF) intermediate model on a chained tenant path."""

    name = models.CharField(max_length=100)
    firm = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="rbac_projects"
    )


class RbacProjectItem(TurboDRFMixin, models.Model):
    """Chained tenant ('project__firm') through a non-TurboDRF model."""

    name = models.CharField(max_length=100)
    project = models.ForeignKey(
        RbacProject, on_delete=models.CASCADE, related_name="items"
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "project__firm",
            "fields": ["id", "name", "project"],
            "compiled": False,
        }


@turbodrf_action(detail=True, methods=["get"], url_path="peek")
def rbac_note_peek(self, request, pk=None):
    """Idiomatic DRF custom action: scoped get_object() + get_serializer()."""
    obj = self.get_object()
    return Response(self.get_serializer(obj).data)


class RbacNote(TurboDRFMixin, models.Model):
    """Tenant-scoped, DRF (non-compiled) path, nested field across an FK
    into the owner-scoped Deal."""

    body = models.CharField(max_length=200)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="rbac_notes"
    )
    deal = models.ForeignKey(
        Deal, on_delete=models.CASCADE, null=True, related_name="rbac_notes"
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "compiled": False,
            "fields": ["id", "body", "brokerage", "deal", "deal__title"],
            "actions": [rbac_note_peek],
        }


class RbacComment(TurboDRFMixin, models.Model):
    """No tenancy and no predicates declared; FK + M2M into tenant-scoped Deal."""

    body = models.CharField(max_length=200)
    deal = models.ForeignKey(
        Deal, on_delete=models.CASCADE, related_name="rbac_comments"
    )
    linked_deals = models.ManyToManyField(
        Deal, blank=True, related_name="rbac_comment_links"
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "compiled": False,
            "fields": ["id", "body", "deal", "deal__title", "linked_deals"],
        }


class RbacBoard(TurboDRFMixin, models.Model):
    """Predicate-only host (Owner, no tenant_field) with an M2M into Deal."""

    title = models.CharField(max_length=100)
    owner = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, related_name="rbac_boards"
    )
    deals = models.ManyToManyField(Deal, blank=True, related_name="rbac_boards")

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        from turbodrf.predicates import Owner

        return {
            "visibility": [Owner("owner")],
            "compiled": False,
            "fields": ["id", "title", "owner", "deals"],
        }


class RbacTask(TurboDRFMixin, models.Model):
    """Tenant-scoped, multi-column owner_field (Either(Owner, Owner))."""

    title = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="rbac_tasks"
    )
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rbac_owned_tasks",
    )
    reviewer = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rbac_review_tasks",
    )

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "owner_field": ["owner", "reviewer"],
            "bypass_owner_roles": ["manager"],
            "compiled": False,
            "fields": ["id", "title", "brokerage", "owner", "reviewer"],
        }


class RbacUuidDoc(TurboDRFMixin, models.Model):
    """Tenant-scoped model with an editable UUID primary key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4)
    title = models.CharField(max_length=100)
    brokerage = models.ForeignKey(
        Brokerage, on_delete=models.CASCADE, related_name="rbac_uuid_docs"
    )

    @classmethod
    def turbodrf(cls):
        return {
            "tenant_field": "brokerage",
            "compiled": False,
            "fields": ["id", "title", "brokerage"],
        }


class RbacListOnly(TurboDRFMixin, models.Model):
    """`fields` dict that only declares 'list'."""

    title = models.CharField(max_length=100)
    internal_notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["id"]

    @classmethod
    def turbodrf(cls):
        return {"compiled": False, "fields": {"list": ["id", "title"]}}
