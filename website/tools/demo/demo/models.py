from django.contrib.auth.models import AbstractUser
from django.db import models

from turbodrf.mixins import TurboDRFMixin


class Workspace(models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class Member(AbstractUser):
    """A user who belongs to one workspace and holds one role."""

    workspace = models.ForeignKey(Workspace, null=True, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, default="viewer")

    @property
    def roles(self):
        return [self.role]


class Client(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=100)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)

    @classmethod
    def turbodrf(cls):
        return {"fields": ["name"], "tenant_field": "workspace"}


class Project(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=20, default="active")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    client = models.ForeignKey(Client, on_delete=models.CASCADE)
    owner = models.ForeignKey(Member, null=True, on_delete=models.SET_NULL)
    budget = models.DecimalField(max_digits=12, decimal_places=2)

    searchable_fields = ["title"]
    sensitive_fields = ["budget"]

    @classmethod
    def turbodrf(cls):
        return {
            "fields": [
                "id",
                "title",
                "status",
                "client",
                "client__name",
                "owner__username",
                "budget",
            ],
            "tenant_field": "workspace",
            "owner_field": "owner",
            "bypass_owner_roles": ["admin", "manager"],
        }
