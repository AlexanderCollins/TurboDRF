#!/usr/bin/env python
import os
import sys

import django

# Add the project to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "source.settings")
django.setup()

from django.contrib.auth import get_user_model  # noqa: E402

User = get_user_model()

# Check if superuser already exists
if not User.objects.filter(username="admin").exists():
    User.objects.create_superuser("admin", "admin@example.com", "admin123")
    print("Superuser created successfully!")
else:
    print("Superuser already exists!")
