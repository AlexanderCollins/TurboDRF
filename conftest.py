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


# Initialize the TurboDRF router once at conftest load (module level, not
# inside pytest_configure) — this runs before pytest's test-collection
# phase, so the first TurboDRFRouter() init happens under the real
# TURBODRF_ROLES from tests/settings.py. The bypass-roles typo guard
# validates here, and then `turbodrf.router._bypass_roles_validated`
# gates out re-validation on subsequent inits (which often happen under
# `@override_settings` partial role overrides during tests).
#
# Each xdist worker imports conftest.py independently, so this primes
# every worker process before any test runs.
import tests.urls  # noqa: E402, F401
