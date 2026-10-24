from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Author, Book, Review, Store, User

admin.site.register(User, UserAdmin)
admin.site.register([Store, Author, Book, Review])
