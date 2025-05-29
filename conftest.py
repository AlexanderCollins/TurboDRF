import os
import sys
from pathlib import Path

import django
from django.conf import settings

# Add current directory to Python path
root_dir = Path(__file__).parent
sys.path.insert(0, str(root_dir))

# Configure Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

# Setup Django
if not settings.configured:
    django.setup()
