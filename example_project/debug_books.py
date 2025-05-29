#!/usr/bin/env python
import os
import sys
from pathlib import Path

# Add the parent directory to sys.path to ensure turbodrf is available
sys.path.insert(0, str(Path(__file__).parent.parent))

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'source.settings')

# Override the INSTALLED_APPS to ensure books is included
from django.conf import settings
settings.INSTALLED_APPS = [
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
    "books",
]

import django
django.setup()

from turbodrf.router import TurboDRFRouter
from turbodrf.mixins import TurboDRFMixin
from books.models import Author, Book, Review
from django.apps import apps

print("Checking if models have TurboDRFMixin...")
for model in [Author, Book, Review]:
    is_turbodrf = issubclass(model, TurboDRFMixin)
    print(f"  - {model.__name__}: {'YES' if is_turbodrf else 'NO'}")
    if is_turbodrf:
        config = model.turbodrf()
        print(f"    Config: {config}")

print("\nAll models with TurboDRFMixin:")
for model in apps.get_models():
    if issubclass(model, TurboDRFMixin):
        print(f"  - {model._meta.app_label}.{model.__name__}")
        config = model.turbodrf()
        print(f"    Enabled: {config.get('enabled', True)}")
        print(f"    Endpoint: {config.get('endpoint', f'{model._meta.model_name}s')}")

print("\nCreating router...")
router = TurboDRFRouter()

print("\nRegistered viewsets:")
for prefix, viewset, basename in router.registry:
    print(f"  - {prefix}: {viewset.__name__} (basename: {basename})")

print("\nURL patterns:")
for url in router.urls:
    print(f"  - {url.pattern}      name: {url.name}")