Quick Start Guide
=================

.. note::

   This mirrors the quickstart in the project ``README.md`` and
   ``INSTALL.md``. Those Markdown docs are the source of truth if this page
   ever drifts.

Basic Usage
-----------

1. Add ``turbodrf`` to ``INSTALLED_APPS``:

.. code-block:: python

   INSTALLED_APPS = [
       'rest_framework',
       'turbodrf',
   ]

2. Add ``TurboDRFMixin`` to a model and define a ``turbodrf()`` classmethod:

.. code-block:: python

   from django.db import models
   from turbodrf.mixins import TurboDRFMixin

   class Book(models.Model, TurboDRFMixin):
       title = models.CharField(max_length=200)
       author = models.ForeignKey(Author, on_delete=models.CASCADE)
       price = models.DecimalField(max_digits=10, decimal_places=2)

       searchable_fields = ['title']

       @classmethod
       def turbodrf(cls):
           return {
               'fields': {
                   'list': ['title', 'author__name', 'price'],
                   'detail': ['title', 'author__name', 'author__email', 'price'],
               }
           }

3. Register the router:

.. code-block:: python

   from django.urls import path, include
   from turbodrf.router import TurboDRFRouter

   router = TurboDRFRouter()

   urlpatterns = [
       path('api/', include(router.urls)),
   ]

The following endpoints are automatically available:

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

Search across a model's ``searchable_fields``:

.. code-block:: bash

   GET /api/books/?search=python

Filtering
~~~~~~~~~

Filter results by field values (Django lookups supported):

.. code-block:: bash

   GET /api/books/?author__name=John%20Doe
   GET /api/books/?price__lt=20

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

Field Selection
~~~~~~~~~~~~~~~

Request a subset of the configured fields with ``?fields=``:

.. code-block:: bash

   GET /api/books/?fields=title,price

Nested relationships are declared with ``__`` paths in the ``turbodrf()``
config (e.g. ``author__name``), not with a request-time ``expand`` parameter.
See :doc:`configuration <configuration>` for the full ``turbodrf()`` contract.
