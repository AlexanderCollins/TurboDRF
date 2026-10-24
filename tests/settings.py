"""
Django settings for TurboDRF tests.
"""

from pathlib import Path

# Build paths inside the project
BASE_DIR = Path(__file__).resolve().parent.parent

# Security settings
SECRET_KEY = "test-secret-key-for-turbodrf-testing-only"
DEBUG = True
ALLOWED_HOSTS = []

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_filters",
    "drf_yasg",
    "turbodrf",
    "tests.test_app",  # Test application
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "tests.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",  # Use in-memory database for tests
    }
}

# Password validation
AUTH_PASSWORD_VALIDATORS = []

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "/static/"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# TurboDRF settings for testing
TURBODRF_ROLES = {
    "admin": [
        # Full access to test models
        "test_app.samplemodel.read",
        "test_app.samplemodel.create",
        "test_app.samplemodel.update",
        "test_app.samplemodel.delete",
        "test_app.relatedmodel.read",
        "test_app.relatedmodel.create",
        "test_app.relatedmodel.update",
        "test_app.relatedmodel.delete",
        "test_app.customendpointmodel.read",
        "test_app.customendpointmodel.create",
        "test_app.customendpointmodel.update",
        "test_app.customendpointmodel.delete",
        # Field permissions for all fields
        "test_app.samplemodel.title.read",
        "test_app.samplemodel.title.write",
        "test_app.samplemodel.description.read",
        "test_app.samplemodel.description.write",
        "test_app.samplemodel.price.read",
        "test_app.samplemodel.price.write",
        "test_app.samplemodel.quantity.read",
        "test_app.samplemodel.quantity.write",
        "test_app.samplemodel.related.read",
        "test_app.samplemodel.related.write",
        "test_app.samplemodel.secret_field.read",
        "test_app.samplemodel.secret_field.write",
        "test_app.samplemodel.is_active.read",
        "test_app.samplemodel.is_active.write",
        "test_app.samplemodel.created_at.read",
        "test_app.samplemodel.updated_at.read",
        "test_app.samplemodel.published_date.read",
        "test_app.samplemodel.published_date.write",
        # RelatedModel fields
        "test_app.relatedmodel.name.read",
        "test_app.relatedmodel.name.write",
        "test_app.relatedmodel.description.read",
        "test_app.relatedmodel.description.write",
        # CustomEndpointModel fields
        "test_app.customendpointmodel.name.read",
        "test_app.customendpointmodel.name.write",
        # Category model
        "test_app.category.read",
        "test_app.category.create",
        "test_app.category.update",
        "test_app.category.delete",
        "test_app.category.name.read",
        "test_app.category.name.write",
        "test_app.category.description.read",
        "test_app.category.description.write",
        # ArticleWithCategories model
        "test_app.articlewithcategories.read",
        "test_app.articlewithcategories.create",
        "test_app.articlewithcategories.update",
        "test_app.articlewithcategories.delete",
        "test_app.articlewithcategories.title.read",
        "test_app.articlewithcategories.title.write",
        "test_app.articlewithcategories.content.read",
        "test_app.articlewithcategories.content.write",
        "test_app.articlewithcategories.author.read",
        "test_app.articlewithcategories.author.write",
        "test_app.articlewithcategories.categories.read",
        "test_app.articlewithcategories.categories.write",
    ],
    "editor": [
        # Read and update permissions
        "test_app.samplemodel.read",
        "test_app.samplemodel.update",
        "test_app.relatedmodel.read",
        "test_app.relatedmodel.update",
        # Field permissions (can see and write most fields)
        "test_app.samplemodel.title.read",
        "test_app.samplemodel.title.write",
        "test_app.samplemodel.description.read",
        "test_app.samplemodel.description.write",
        "test_app.samplemodel.price.read",  # Read-only for editors
        "test_app.samplemodel.quantity.read",
        "test_app.samplemodel.quantity.write",
        "test_app.samplemodel.related.read",
        "test_app.samplemodel.related.write",
        "test_app.samplemodel.is_active.read",
        "test_app.samplemodel.is_active.write",
        "test_app.samplemodel.created_at.read",
        "test_app.samplemodel.updated_at.read",
        "test_app.samplemodel.published_date.read",
        "test_app.samplemodel.published_date.write",
        # RelatedModel fields
        "test_app.relatedmodel.name.read",
        "test_app.relatedmodel.description.read",
    ],
    "viewer": [
        # Read-only access
        "test_app.samplemodel.read",
        "test_app.relatedmodel.read",
        # Limited field access
        "test_app.samplemodel.title.read",
        "test_app.samplemodel.description.read",
        "test_app.samplemodel.quantity.read",
        "test_app.samplemodel.related.read",
        "test_app.samplemodel.is_active.read",
        "test_app.samplemodel.created_at.read",
        "test_app.samplemodel.updated_at.read",
        "test_app.samplemodel.published_date.read",
        # RelatedModel fields
        "test_app.relatedmodel.name.read",
        "test_app.relatedmodel.description.read",
        # Cannot see secret fields or price
    ],
    # Roles for predicate/IDOR/tenancy tests on the brokerage chain
    "underwriter": [
        "test_app.deal.read",
        "test_app.deal.create",
        "test_app.deal.update",
        "test_app.deal.delete",
        "test_app.bankaccount.read",
        "test_app.bankaccount.create",
        "test_app.bankaccount.update",
        "test_app.bankaccount.delete",
        "test_app.transaction.read",
        "test_app.transaction.create",
        "test_app.transaction.update",
        "test_app.transaction.delete",
    ],
    "manager": [
        "test_app.deal.read",
        "test_app.deal.create",
        "test_app.deal.update",
        "test_app.deal.delete",
        "test_app.bankaccount.read",
        "test_app.bankaccount.create",
        "test_app.bankaccount.update",
        "test_app.bankaccount.delete",
        "test_app.transaction.read",
        "test_app.transaction.create",
        "test_app.transaction.update",
        "test_app.transaction.delete",
    ],
    # Roles for the HoldingEvent / Holding / Portfolio regression tests
    # (nullable FKs + nested fields + properties on the compiled path).
    "analyst": [
        "test_app.holdingevent.read",
        "test_app.holdingevent.create",
        "test_app.holdingevent.update",
        # Field rule: only analysts can read HoldingEvent.holding (auditors
        # can't), so the compiled ?fields= tests can check a restricted column.
        "test_app.holdingevent.holding.read",
        "test_app.holdingevent.holding.write",
        "test_app.holding.read",
        "test_app.portfolio.read",
        "test_app.fund.read",
        "test_app.valuation.read",
    ],
    "auditor": [
        "test_app.holdingevent.read",
        "test_app.holding.read",
        "test_app.portfolio.read",
    ],
    # Compiled-vs-serializer parity harness (test_parity_compiled_vs_serializer.py). Both roles
    # read every parity model; only parity_reader holds the explicit field rules,
    # so parity_limited loses amount, twin, target__amount and tags__weight.
    "parity_reader": [
        "test_app.parityalltypes.read",
        "test_app.paritytarget.read",
        "test_app.parityleaf.read",
        "test_app.paritytag.read",
        "test_app.paritycurrency.read",
        "test_app.paritycodepk.read",
        "test_app.parityuuidpk.read",
        "test_app.paritychild.read",
        "test_app.parityalltypes.amount.read",
        "test_app.parityalltypes.twin.read",
        "test_app.paritytarget.amount.read",
        "test_app.paritytag.weight.read",
    ],
    "parity_limited": [
        "test_app.parityalltypes.read",
        "test_app.paritytarget.read",
        "test_app.parityleaf.read",
        "test_app.paritytag.read",
        "test_app.paritycurrency.read",
        "test_app.paritycodepk.read",
        "test_app.parityuuidpk.read",
        "test_app.paritychild.read",
    ],
}

# Tenant roles on the platform-scope test models (test_platform_*.py).
TURBODRF_ROLES["account_admin"] = [
    "test_app.platformaccount.read",
    "test_app.platformaccount.create",
    "test_app.platformaccount.update",
    "test_app.platformaccount.delete",
    "test_app.platformticket.read",
    "test_app.platformticket.create",
    "test_app.platformticket.update",
    "test_app.platformticket.delete",
]

# Platform roles (the platform router's own map; tenant roles never apply on
# it, nor these on the tenant router). "account_admin" shares a tenant role's
# name and grants something else here.
TURBODRF_PLATFORM_ROLES = {
    "admin": [
        "test_app.brokerage.read",
        "test_app.platformaccount.read",
        "test_app.platformaccount.create",
        "test_app.platformaccount.update",
        "test_app.platformaccount.delete",
        "test_app.platformjob.read",
        "test_app.platformjob.create",
        "test_app.platformjob.payload.read",
        "test_app.platformticket.read",
        "test_app.platformticket.create",
        "test_app.platformticket.update",
        "test_app.platformticket.delete",
        "test_app.platformticket.internal_note.read",
        "test_app.platformticket.internal_note.write",
    ],
    "support": [
        "test_app.brokerage.read",
        "test_app.platformaccount.read",
        "test_app.platformjob.read",
        "test_app.platformticket.read",
        "test_app.platformticket.create",
        "test_app.platformticket.update",
        "test_app.platformticket.internal_note.read",
    ],
    "auditor": [
        "test_app.platformjob.read",
        "test_app.platformjob.payload.read",
    ],
    "account_admin": ["test_app.platformjob.read"],
}

# Predicate / row-level access control settings
# We do NOT set TURBODRF_TENANT_MODEL globally — that would force every
# existing test model to declare tenancy. Predicate-specific tests use
# override_settings for their scope.
TURBODRF_TENANT_USER_FIELD = "brokerage"
TURBODRF_REQUIRE_TENANCY = False

# REST Framework settings
REST_FRAMEWORK = {
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
    "EXCEPTION_HANDLER": "turbodrf.exceptions.turbodrf_exception_handler",
}

# drf-yasg settings - silence deprecation warning
SWAGGER_USE_COMPAT_RENDERERS = False
