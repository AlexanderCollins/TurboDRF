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
}

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
}

# drf-yasg settings - silence deprecation warning
SWAGGER_USE_COMPAT_RENDERERS = False
