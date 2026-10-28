# TurboDRF Test Suite

This directory contains the test suite for TurboDRF.

## Test Structure

```
tests/
├── settings.py              # Django settings for tests (TURBODRF_ROLES, ...)
├── urls.py                  # URL configuration for tests
├── fixtures.py              # Common test fixtures and utilities
├── test_runner.py           # Custom test runner
├── test_package.py          # Package structure tests
├── test_nesting_validation.py
│
├── test_app/                # Django app for testing
│   ├── models.py            # Test models with various configurations
│   ├── apps.py              # Adds user.roles / user.brokerage; set_test_brokerage()
│   └── migrations/
│
├── unit/                    # Unit tests: permissions, serializers, views,
│                            # router, compiler, filters, predicates,
│                            # integrations, swagger, management commands
│
├── integration/             # API-level tests: CRUD endpoints, role
│                            # permissions, tenancy / predicates, FK
│                            # injection, security regressions,
│                            # swagger scoping
│
└── conformance/             # An independent policy monitor run against the
                             # real API (deterministic + Hypothesis)
```

## Running Tests

### Run All Tests

```bash
# From the project root. Runs in parallel (pytest-xdist, `-n auto` in pyproject.toml)
python -m pytest

# Single process, e.g. for pdb
python -m pytest -p no:xdist

# Or using Django's test runner
python manage.py test

# Or using the custom test runner
python tests/test_runner.py
```

### Run Specific Test Categories

```bash
# Unit tests only
python -m pytest tests/unit/

# Integration tests only
python -m pytest tests/integration/

# Specific test file
python -m pytest tests/unit/test_permissions.py

# Specific test class
python -m pytest tests/unit/test_permissions.py::TestTurboDRFPermission

# Specific test method
python -m pytest tests/unit/test_permissions.py::TestTurboDRFPermission::test_admin_has_all_permissions
```

### Run with Coverage

```bash
# Generate coverage report
python -m pytest tests/ --cov=turbodrf --cov-report=html

# View coverage report
open htmlcov/index.html
```

## Test Models

The test suite uses models defined in `test_app/models.py`, among them:

- **SampleModel**: Main model with various field types and relationships
- **RelatedModel**: For testing foreign key relationships
- **NoTurboDRFModel**: Model without TurboDRF mixin (for negative testing)
- **CustomEndpointModel**: Model with custom endpoint configuration
- **DisabledModel**: Model with TurboDRF disabled
- **Brokerage → Deal → BankAccount → Transaction**: tenant chain for
  tenancy, predicate and IDOR tests
- **Parity\*** and **Scope\*** / **Rbac\*** models: one column per field
  type (parity_fixtures.py), nested-value scoping and RBAC regression tests

## Test Users and Roles

Roles are defined in `tests/settings.py` (`TURBODRF_ROLES`). `test_app/apps.py`
gives the User model a `roles` property: `user._test_roles` if set, otherwise
`admin` for superusers, `editor` for staff and `viewer` for everyone else.
`UserFixtures` creates one user of each:

1. **Admin**: Full access to all operations and fields
2. **Editor**: Read and update access, no delete, limited field access
3. **Viewer**: Read-only access, minimal field visibility

For tenancy tests, `set_test_brokerage(user, brokerage)` sets the user's
tenant (`TURBODRF_TENANT_USER_FIELD = "brokerage"`).

Anonymous callers on `public_access` models get field reads only from a
`guest` role. Tests that expect anonymous field access use
`override_settings(TURBODRF_ROLES=roles_with_guest())` from `fixtures.py`.

## Writing New Tests

### Adding Unit Tests

```python
from django.test import TestCase
from turbodrf.some_module import SomeClass

class TestSomeClass(TestCase):
    def setUp(self):
        # Set up test fixtures
        pass
    
    def test_some_functionality(self):
        # Test specific functionality
        pass
```

### Adding Integration Tests

```python
from rest_framework.test import APITestCase
from tests.fixtures import UserFixtures, ModelFixtures

class TestNewFeature(APITestCase):
    def setUp(self):
        self.users = UserFixtures.create_all_users()
        self.data = ModelFixtures.create_full_test_data()
    
    def test_feature_behavior(self):
        self.client.force_authenticate(user=self.users['admin'])
        response = self.client.get('/api/endpoint/')
        self.assertEqual(response.status_code, 200)
```

### Using Test Fixtures

```python
from tests.fixtures import APITestMixin

class MyTest(TestCase, APITestMixin):
    def test_permissions(self):
        # Use helper methods
        self.assert_has_fields(data, ['field1', 'field2'])
        self.assert_pagination_structure(response.data)
        self.assert_can_perform_action(
            self.client, 'post', '/api/endpoint/', data
        )
```

## Continuous Integration

`.github/workflows/ci.yml` runs the suite with coverage on every push and
pull request to `main`, across:

- Python 3.10, 3.11, 3.12, 3.13, 3.14
- Django 4.2 (Python 3.10–3.12), 5.2 (all), 6.0 (Python 3.12–3.14)

`.github/workflows/lint.yml` runs `black --check`, `isort --check-only` and
`flake8` on the same triggers.
