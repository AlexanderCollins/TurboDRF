import os
import sys
import django
from pathlib import Path

# Add current directory to Python path
root_dir = Path(__file__).parent
sys.path.insert(0, str(root_dir))

# Configure Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

# Setup Django
import django
from django.conf import settings

if not settings.configured:
    django.setup()
