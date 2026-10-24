from django.contrib.auth.models import AbstractUser
from django.db import models

from turbodrf.mixins import TurboDRFMixin


class Store(models.Model):
    """The tenant: each user works for one store and sees only its books."""

    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name


class User(AbstractUser):
    store = models.ForeignKey(Store, null=True, blank=True, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, blank=True)  # a TURBODRF_ROLES key

    @property
    def roles(self):
        # TurboDRF reads user.roles: a list of role names.
        return [self.role] if self.role else []


class Author(TurboDRFMixin, models.Model):
    name = models.CharField(max_length=100)

    def __str__(self):
        return self.name

    @classmethod
    def turbodrf(cls):
        # Shared reference data: every store sees every author.
        return {"fields": ["id", "name"], "tenancy": "shared"}


class Book(TurboDRFMixin, models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.PROTECT)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return self.title

    @classmethod
    def turbodrf(cls):
        return {
            "fields": ["id", "title", "author", "author__name", "price"],
            "searchable_fields": ["title"],
            # Rows belong to the caller's store; filled in on create.
            "tenant_field": "store",
        }


class Review(TurboDRFMixin, models.Model):
    book = models.ForeignKey(Book, on_delete=models.CASCADE)
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE)
    rating = models.PositiveSmallIntegerField()
    comment = models.TextField(blank=True)

    @classmethod
    def turbodrf(cls):
        return {
            "fields": ["id", "book", "book__title", "reviewer", "rating", "comment"],
            # The store is reached through the book.
            "tenant_field": "book__store",
            # Users see their own reviews; admins see all of their store's.
            "owner_field": "reviewer",
            "bypass_owner_roles": ["admin"],
        }
