"""
Custom test runner for TurboDRF tests.

Provides additional test configuration and setup.
"""

import os
import sys

import django
from django.conf import settings
from django.test.utils import get_runner


def run_tests():
    """Run the TurboDRF test suite."""
    os.environ["DJANGO_SETTINGS_MODULE"] = "tests.settings"
    django.setup()

    TestRunner = get_runner(settings)
    test_runner = TestRunner(verbosity=2, interactive=True, keepdb=False)

    # Run all tests or specific test labels
    test_labels = sys.argv[1:] if len(sys.argv) > 1 else []
    failures = test_runner.run_tests(test_labels)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    run_tests()
