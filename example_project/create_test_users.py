#!/usr/bin/env python
import os
import sys
import django

# Add the project directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'source.settings')
django.setup()

from django.contrib.auth.models import User

# Create users with different roles
users = [
    {'username': 'viewer', 'password': 'viewer123', 'role': 'viewer'},
    {'username': 'editor', 'password': 'editor123', 'role': 'editor'},
    {'username': 'admin', 'password': 'admin123', 'role': 'admin'},
]

for user_data in users:
    user, created = User.objects.get_or_create(username=user_data['username'])
    if created:
        user.set_password(user_data['password'])
        user.save()
        # Set the turbodrf_role attribute
        user.turbodrf_role = user_data['role']
        user.save()
        print(f"Created user: {user.username} with role: {user_data['role']}")
    else:
        # Update existing user
        user.set_password(user_data['password'])
        user.turbodrf_role = user_data['role']
        user.save()
        print(f"Updated user: {user.username} with role: {user_data['role']}")

print("\nAll test users created/updated successfully!")
print("\nCredentials:")
for user_data in users:
    print(f"Username: {user_data['username']}, Password: {user_data['password']}, Role: {user_data['role']}")