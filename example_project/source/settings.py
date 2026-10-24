"""Settings for the TurboDRF example project. Development only."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "django-insecure-example-project-only"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_yasg",
    "turbodrf",
    "books",
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

ROOT_URLCONF = "source.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "source.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_USER_MODEL = "books.User"

USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- TurboDRF -------------------------------------------------------------

# Tenancy: every user belongs to a store (request.user.store).
TURBODRF_TENANT_MODEL = "books.Store"
TURBODRF_TENANT_USER_FIELD = "store"

TURBODRF_ROLES = {
    "admin": [
        "books.author.read",
        "books.author.create",
        "books.author.update",
        "books.author.delete",
        "books.book.read",
        "books.book.create",
        "books.book.update",
        "books.book.delete",
        "books.book.price.read",
        "books.book.price.write",
        "books.review.read",
        "books.review.update",
        "books.review.delete",
    ],
    "editor": [
        "books.author.read",
        "books.author.create",
        "books.book.read",
        "books.book.create",
        "books.book.update",
        # price has field rules, so every role that sees or sets it needs
        # them. editor creates books, and price is required: it needs write.
        "books.book.price.read",
        "books.book.price.write",
        "books.review.read",
    ],
    "viewer": [
        # No price rules: viewers don't see prices.
        "books.author.read",
        "books.book.read",
        "books.review.read",
        "books.review.create",
        "books.review.update",
    ],
}
