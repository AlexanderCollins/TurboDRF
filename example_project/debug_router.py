#!/usr/bin/env python
import os
import sys

import django

# Add the project to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "source.settings")
django.setup()

from django.apps import apps

from turbodrf.mixins import TurboDRFMixin
from turbodrf.router import TurboDRFRouter

print("Checking for models with TurboDRFMixin...")

# Check all models
for model in apps.get_models():
    print(f"\nModel: {model.__name__}")
    print(f"  Module: {model.__module__}")
    print(f"  Has TurboDRFMixin: {issubclass(model, TurboDRFMixin)}")

    if hasattr(model, "turbodrf"):
        print(f"  Has turbodrf method: True")
        try:
            config = model.turbodrf()
            print(f"  Config: {config}")
        except Exception as e:
            print(f"  Error getting config: {e}")

# Create router and check registered endpoints
print("\n\nCreating TurboDRFRouter...")
router = TurboDRFRouter()

print("\nRegistered URLs:")
for url in router.urls:
    print(f"  {url}")
