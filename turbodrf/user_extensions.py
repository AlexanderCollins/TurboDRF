"""
Example of how to add roles to your User model for TurboDRF
"""

from django.contrib.auth.models import AbstractUser
from django.db import models


class TurboDRFUser(AbstractUser):
    """Extended User model with roles support for TurboDRF."""

    # You can implement roles in different ways:

    # Option 1: Many-to-Many relationship
    roles = models.ManyToManyField("Role", related_name="users")

    # Option 2: JSON field
    # roles = models.JSONField(default=list)

    # Option 3: Comma-separated values
    # roles_csv = models.CharField(max_length=255, default='')

    # @property
    # def roles(self):
    #     return self.roles_csv.split(',') if self.roles_csv else []


class Role(models.Model):
    """Role model for many-to-many approach."""

    name = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)

    def __str__(self):
        return self.name


# Alternative: Monkey-patch existing User model
"""
from django.contrib.auth import get_user_model

User = get_user_model()

# Add roles property
def get_user_roles(self):
    # Implement your logic here
    # For example, get from groups:
    return [group.name for group in self.groups.all()]

User.add_to_class('roles', property(get_user_roles))
"""
