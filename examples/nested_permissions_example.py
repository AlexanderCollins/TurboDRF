"""
Example: Database-Backed Nested Field Permissions

This example demonstrates how nested field permissions work with
database-backed permission mode, showing how permissions are checked
at EVERY level of the relationship chain.

Setup:
    Book -> Author -> Publisher (3-level nesting)

Scenarios:
    1. User with full access (can see everything)
    2. User with limited access (blocked at publisher level)
    3. User with field-level restrictions (can't see salary)
"""

import django
import os

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'examples.settings')
django.setup()

from django.contrib.auth import get_user_model
from turbodrf.models import TurboDRFRole, RolePermission, UserRole
from turbodrf.validation import check_nested_field_permissions
from turbodrf.backends import build_permission_snapshot

# Assuming models from test suite
from tests.test_nesting_validation import Book, Author, Publisher

User = get_user_model()

def setup_database_permissions():
    """Create roles and permissions in the database."""
    print("\n=== Setting up Database Permissions ===\n")

    # Clean up existing data
    TurboDRFRole.objects.all().delete()

    # Role 1: Full Access
    print("Creating 'full_access' role...")
    full_access = TurboDRFRole.objects.create(name='full_access')

    # Model-level permissions grant access to all fields
    RolePermission.objects.create(
        role=full_access,
        app_label='tests',
        model_name='book',
        action='read'
    )
    RolePermission.objects.create(
        role=full_access,
        app_label='tests',
        model_name='author',
        action='read'
    )
    RolePermission.objects.create(
        role=full_access,
        app_label='tests',
        model_name='publisher',
        action='read'
    )

    # Role 2: Limited Access (no publisher access)
    print("Creating 'limited_access' role...")
    limited_access = TurboDRFRole.objects.create(name='limited_access')

    RolePermission.objects.create(
        role=limited_access,
        app_label='tests',
        model_name='book',
        action='read'
    )
    RolePermission.objects.create(
        role=limited_access,
        app_label='tests',
        model_name='author',
        action='read'
    )
    # NO publisher permission!

    # Role 3: Field-Level Restrictions (can't see salary)
    print("Creating 'field_restricted' role...")
    field_restricted = TurboDRFRole.objects.create(name='field_restricted')

    RolePermission.objects.create(
        role=field_restricted,
        app_label='tests',
        model_name='book',
        action='read'
    )
    # Explicit field permissions for Author (no model-level read)
    RolePermission.objects.create(
        role=field_restricted,
        app_label='tests',
        model_name='author',
        field_name='name',
        permission_type='read'
    )
    RolePermission.objects.create(
        role=field_restricted,
        app_label='tests',
        model_name='author',
        field_name='email',
        permission_type='read'
    )
    # NO permission on author.salary!

    print("\nRoles created successfully!\n")


def test_full_access():
    """Test user with full access can see all nested fields."""
    print("=== Testing Full Access ===\n")

    # Create user and assign role
    user = User.objects.create_user(username='full_user')
    full_role = TurboDRFRole.objects.get(name='full_access')
    UserRole.objects.create(user=user, role=full_role)

    # Test various nested fields
    test_fields = [
        'title',
        'author__name',
        'author__salary',
        'author__publisher__name',
        'author__publisher__revenue',
    ]

    for field in test_fields:
        has_permission = check_nested_field_permissions(Book, field, user)
        status = "✓ ALLOWED" if has_permission else "✗ DENIED"
        print(f"  {status}: {field}")

    print()


def test_limited_access():
    """Test user without publisher access is blocked at that level."""
    print("=== Testing Limited Access (No Publisher) ===\n")

    # Create user and assign role
    user = User.objects.create_user(username='limited_user')
    limited_role = TurboDRFRole.objects.get(name='limited_access')
    UserRole.objects.create(user=user, role=limited_role)

    test_fields = [
        ('title', True),
        ('author__name', True),
        ('author__salary', True),
        ('author__publisher__name', False),  # Blocked at publisher level
        ('author__publisher__revenue', False),  # Blocked at publisher level
    ]

    for field, expected in test_fields:
        has_permission = check_nested_field_permissions(Book, field, user)
        status = "✓ ALLOWED" if has_permission else "✗ DENIED"
        match = "✓" if has_permission == expected else "✗ UNEXPECTED"
        print(f"  {status}: {field} {match}")

    print()


def test_field_restrictions():
    """Test user with field-level restrictions can't see salary."""
    print("=== Testing Field-Level Restrictions (No Salary) ===\n")

    # Create user and assign role
    user = User.objects.create_user(username='field_user')
    field_role = TurboDRFRole.objects.get(name='field_restricted')
    UserRole.objects.create(user=user, role=field_role)

    test_fields = [
        ('title', True),
        ('author__name', True),  # Explicit permission
        ('author__email', True),  # Explicit permission
        ('author__salary', False),  # NO explicit permission
        ('author__ssn', False),  # NO explicit permission
    ]

    for field, expected in test_fields:
        has_permission = check_nested_field_permissions(Book, field, user)
        status = "✓ ALLOWED" if has_permission else "✗ DENIED"
        match = "✓" if has_permission == expected else "✗ UNEXPECTED"
        print(f"  {status}: {field} {match}")

    print()


def test_snapshot_performance():
    """Demonstrate snapshot caching for performance."""
    print("=== Testing Snapshot Caching ===\n")

    user = User.objects.get(username='full_user')

    import time

    # First call - builds snapshot
    start = time.time()
    snapshot1 = build_permission_snapshot(user, Book, use_cache=True)
    time1 = (time.time() - start) * 1000

    # Second call - uses cache
    start = time.time()
    snapshot2 = build_permission_snapshot(user, Book, use_cache=True)
    time2 = (time.time() - start) * 1000

    print(f"  First call (build):  {time1:.2f}ms")
    print(f"  Second call (cache): {time2:.2f}ms")
    print(f"  Speedup: {time1/time2:.1f}x faster\n")

    print(f"  Snapshot contains:")
    print(f"    - {len(snapshot1.allowed_actions)} model-level actions")
    print(f"    - {len(snapshot1.readable_fields)} readable fields")
    print(f"    - {len(snapshot1.writable_fields)} writable fields")
    print()


def test_filter_permissions():
    """Test that filters respect nested field permissions."""
    print("=== Testing Filter Permissions ===\n")

    user = User.objects.get(username='field_user')

    test_filters = [
        ('author__name__icontains', True),  # Has permission
        ('author__salary__gte', False),  # NO permission
        ('author__publisher__name', False),  # NO publisher access
    ]

    from turbodrf.validation import validate_filter_field

    for filter_param, expected in test_filters:
        try:
            field_path, lookup = validate_filter_field(Book, filter_param)
            has_permission = check_nested_field_permissions(Book, field_path, user)

            status = "✓ ALLOWED" if has_permission else "✗ DENIED"
            match = "✓" if has_permission == expected else "✗ UNEXPECTED"
            print(f"  {status}: ?{filter_param}=... {match}")
        except Exception as e:
            print(f"  ✗ INVALID: ?{filter_param}=... (depth limit exceeded)")

    print()


def main():
    """Run all examples."""
    print("\n" + "="*60)
    print("Database-Backed Nested Field Permissions Example")
    print("="*60)

    # Enable database permission mode
    from django.conf import settings
    settings.TURBODRF_PERMISSION_MODE = 'database'

    # Setup
    setup_database_permissions()

    # Run tests
    test_full_access()
    test_limited_access()
    test_field_restrictions()
    test_snapshot_performance()
    test_filter_permissions()

    print("="*60)
    print("Example completed successfully!")
    print("="*60 + "\n")

    print("Key Takeaways:")
    print("  • Nested permissions are checked at EVERY level")
    print("  • Model-level 'read' grants access to all fields")
    print("  • Field-level permissions override model-level")
    print("  • Snapshots are cached for performance")
    print("  • Filters respect the same permission rules")
    print()


if __name__ == '__main__':
    # Note: This example requires the test models to be set up
    # In a real application, you would use your actual models
    print("\nNOTE: This example uses test models.")
    print("In production, use your actual Django models.\n")

    try:
        main()
    except Exception as e:
        print(f"\nError: {e}")
        print("\nMake sure you're running this from the turbodrf project root")
        print("and that test models are available.\n")
