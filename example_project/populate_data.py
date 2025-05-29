#!/usr/bin/env python
import os
import sys
from datetime import date

import django

# Add the project to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure Django settings
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "source.settings")
django.setup()

# Import models
from books.models import Author, Book, Review

# Create some authors
author1 = Author.objects.create(
    name="J.K. Rowling",
    email="jk@example.com",
    bio="British author, best known for the Harry Potter series.",
)

author2 = Author.objects.create(
    name="George R.R. Martin",
    email="grrm@example.com",
    bio="American novelist and short story writer, best known for A Song of Ice and Fire.",
)

# Create some books
book1 = Book.objects.create(
    title="Harry Potter and the Philosopher's Stone",
    author=author1,
    isbn="9780747532699",
    price="19.99",
    published_date=date(1997, 6, 26),
    description="The first novel in the Harry Potter series.",
)

book2 = Book.objects.create(
    title="A Game of Thrones",
    author=author2,
    isbn="9780553103540",
    price="24.99",
    published_date=date(1996, 8, 1),
    description="The first novel in A Song of Ice and Fire series.",
)

# Create some reviews
Review.objects.create(
    book=book1,
    reviewer_name="John Doe",
    rating=5,
    comment="Amazing book! A classic that everyone should read.",
)

Review.objects.create(
    book=book2,
    reviewer_name="Jane Smith",
    rating=4,
    comment="Great world-building and complex characters.",
)

print("Sample data created successfully!")
