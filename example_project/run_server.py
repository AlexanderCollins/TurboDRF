#!/usr/bin/env python
import os
import sys
from pathlib import Path

# Add the parent directory to sys.path to ensure turbodrf is available
sys.path.insert(0, str(Path(__file__).parent.parent))

# Ensure we're using the example project settings, not test settings
os.environ['DJANGO_SETTINGS_MODULE'] = 'source.settings'

# Remove any test paths from sys.path
sys.path = [p for p in sys.path if 'tests' not in p]

if __name__ == '__main__':
    from django.core.management import execute_from_command_line
    execute_from_command_line(['manage.py', 'runserver'])