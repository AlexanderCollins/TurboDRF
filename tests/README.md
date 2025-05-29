# TurboDRF Test Suite

This directory contains the comprehensive test suite for TurboDRF.

## Test Structure

```
tests/
├── __init__.py              # Test suite initialization
├── settings.py              # Django settings for tests
├── urls.py                  # URL configuration for tests
├── fixtures.py              # Common test fixtures and utilities
├── test_runner.py           # Custom test runner
├── test_package.py          # Package structure tests
│
├── test_app/                # Django app for testing
│   ├── __init__.py
│   ├── models.py            # Test models with various configurations
│   └── apps.py              # App configuration
│
├── unit/                    # Unit tests
│   ├── __init__.py
│   ├── test_mixins.py       # Tests for TurboDRFMixin
│   ├── test_permissions.py  # Tests for permission classes
│   ├── test_serializers.py  # Tests for serializers
│   ├── test_views.py        # Tests for viewsets
│   └── test_router.py       # Tests for router
│
└── integration/             # Integration tests
    ├── __init__.py
    ├── test_api_endpoints.py    # API endpoint tests
    └── test_role_permissions.py # Role-based permission tests
```

## Running Tests

### Run All Tests

```bash
# From the project root
python -m pytest tests/

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

## Test Categories

### Unit Tests

Unit tests focus on individual components in isolation:

- **test_mixins.py**: Tests the TurboDRFMixin functionality
- **test_permissions.py**: Tests role-based permission logic
- **test_serializers.py**: Tests dynamic serializer generation
- **test_views.py**: Tests ViewSet behavior
- **test_router.py**: Tests automatic model discovery and URL registration

### Integration Tests

Integration tests verify the complete API functionality:

- **test_api_endpoints.py**: Tests CRUD operations, search, filtering, ordering, and pagination
- **test_role_permissions.py**: Tests role-based access control across the API

## Test Models

The test suite uses several models defined in `test_app/models.py`:

- **TestModel**: Main model with various field types and relationships
- **RelatedModel**: For testing foreign key relationships
- **NoTurboDRFModel**: Model without TurboDRF mixin (for negative testing)
- **CustomEndpointModel**: Model with custom endpoint configuration
- **DisabledModel**: Model with TurboDRF disabled

## Test Users and Roles

The test suite uses three default user roles:

1. **Admin**: Full access to all operations and fields
2. **Editor**: Read and update access, no delete, limited field access
3. **Viewer**: Read-only access, minimal field visibility

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

Tests are automatically run on:
- Every push to the repository
- Every pull request
- Multiple Python versions (3.8, 3.9, 3.10, 3.11)
- Multiple Django versions (3.2, 4.0, 4.1, 4.2)

See `.github/workflows/tests.yml` for CI configuration.