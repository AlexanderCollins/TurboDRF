Quick Start Guide
=================

Basic Usage
-----------

1. Create a model with ``TurboDRFMixin``:

.. code-block:: python

   from django.db import models
   from turbodrf import TurboDRFMixin

   class Book(TurboDRFMixin, models.Model):
       title = models.CharField(max_length=200)
       author = models.CharField(max_length=100)
       isbn = models.CharField(max_length=13)
       published_date = models.DateField()
       price = models.DecimalField(max_digits=10, decimal_places=2)

       class Meta:
           turbodrf_config = {
               'search_fields': ['title', 'author'],
               'ordering_fields': ['title', 'published_date', 'price'],
               'filterset_fields': ['author', 'published_date'],
           }

2. The following endpoints are automatically available:

   * ``GET /api/books/`` - List all books
   * ``POST /api/books/`` - Create a new book
   * ``GET /api/books/{id}/`` - Retrieve a book
   * ``PUT /api/books/{id}/`` - Update a book
   * ``PATCH /api/books/{id}/`` - Partially update a book
   * ``DELETE /api/books/{id}/`` - Delete a book

API Features
------------

Search
~~~~~~

Search across multiple fields:

.. code-block:: bash

   GET /api/books/?search=python

Filtering
~~~~~~~~~

Filter results by field values:

.. code-block:: bash

   GET /api/books/?author=John%20Doe

Ordering
~~~~~~~~

Order results by field:

.. code-block:: bash

   GET /api/books/?ordering=-published_date

Pagination
~~~~~~~~~~

Results are automatically paginated:

.. code-block:: bash

   GET /api/books/?page=2&page_size=10

Field Expansion
~~~~~~~~~~~~~~~

Expand related fields:

.. code-block:: python

   class Author(TurboDRFMixin, models.Model):
       name = models.CharField(max_length=100)
       bio = models.TextField()

   class Book(TurboDRFMixin, models.Model):
       title = models.CharField(max_length=200)
       author = models.ForeignKey(Author, on_delete=models.CASCADE)

Request with expanded author:

.. code-block:: bash

   GET /api/books/?expand=author

Response:

.. code-block:: json

   {
       "count": 1,
       "results": [
           {
               "id": 1,
               "title": "Django for Beginners",
               "author": {
                   "id": 1,
                   "name": "John Doe",
                   "bio": "A Django enthusiast..."
               }
           }
       ]
   }