"""Settings for the demo the website's videos and examples are captured from."""

SECRET_KEY = "demo-only"
DEBUG = False
ALLOWED_HOSTS = ["*"]
USE_TZ = True
ROOT_URLCONF = "urls"
AUTH_USER_MODEL = "demo.Member"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "rest_framework",
    "turbodrf",
    "demo",
]

DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
}

TURBODRF_TENANT_MODEL = "demo.Workspace"
TURBODRF_TENANT_USER_FIELD = "workspace"

TURBODRF_ROLES = {
    "admin": [
        "demo.project.read",
        "demo.project.create",
        "demo.project.update",
        "demo.project.delete",
        "demo.project.budget.read",
        "demo.project.budget.write",
        "demo.client.read",
        "demo.member.read",
    ],
    "manager": [
        "demo.project.read",
        "demo.project.update",
        "demo.project.budget.read",
        "demo.client.read",
        "demo.member.read",
    ],
    "viewer": [
        "demo.project.read",
        "demo.client.read",
        "demo.member.read",
    ],
}
