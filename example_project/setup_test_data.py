from datetime import date

from books.models import Author, Book
from django.contrib.auth.models import User

# Create users with different roles
users = [
    {"username": "viewer", "password": "viewer123", "role": "viewer"},
    {"username": "editor", "password": "editor123", "role": "editor"},
    {"username": "admin", "password": "admin123", "role": "admin"},
]

for user_data in users:
    user, created = User.objects.get_or_create(username=user_data["username"])
    if created:
        user.set_password(user_data["password"])
        # Set staff/superuser based on role
        if user_data["role"] == "admin":
            user.is_superuser = True
            user.is_staff = True
        elif user_data["role"] == "editor":
            user.is_staff = True
            user.is_superuser = False
        else:
            user.is_staff = False
            user.is_superuser = False
        user.save()
        print(f"Created user: {user.username} with role: {user_data['role']}")
    else:
        # Update existing user
        user.set_password(user_data["password"])
        if user_data["role"] == "admin":
            user.is_superuser = True
            user.is_staff = True
        elif user_data["role"] == "editor":
            user.is_staff = True
            user.is_superuser = False
        else:
            user.is_staff = False
            user.is_superuser = False
        user.save()
        print(f"Updated user: {user.username} with role: {user_data['role']}")

print("\nAll test users created/updated successfully!")
print("\nCredentials:")
for user_data in users:
    print(
        f"Username: {user_data['username']}, Password: {user_data['password']}, Role: {user_data['role']}"
    )

# Create some test data
print("\nCreating test data...")

# Create an author
author, created = Author.objects.get_or_create(
    name="Test Author", defaults={"email": "test@example.com", "bio": "A test author"}
)

# Create a book
book, created = Book.objects.get_or_create(
    isbn="1234567890123",
    defaults={
        "title": "Test Book",
        "author": author,
        "price": 29.99,
        "published_date": date.today(),
        "description": "A test book for API testing",
    },
)

print(f"Created test book: {book.title} (ID: {book.id})")
