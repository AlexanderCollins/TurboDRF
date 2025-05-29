.. TurboDRF documentation master file

Welcome to TurboDRF's documentation!
====================================

.. toctree::
   :maxdepth: 2
   :caption: Contents:

   installation
   quickstart
   api

TurboDRF
========

TurboDRF is a powerful Django REST Framework extension that automatically generates API endpoints with minimal configuration.

Features
--------

* Automatic API endpoint generation
* Dynamic serializer creation
* Built-in search, filtering, and ordering
* Role-based permissions
* Swagger/OpenAPI documentation
* Nested field expansion
* Pagination support

Installation
------------

.. code-block:: bash

   pip install turbodrf

Quick Start
-----------

1. Add ``turbodrf`` to your ``INSTALLED_APPS``:

   .. code-block:: python

      INSTALLED_APPS = [
          ...
          'rest_framework',
          'turbodrf',
          ...
      ]

2. Add the TurboDRF URLs to your project:

   .. code-block:: python

      from django.urls import path, include

      urlpatterns = [
          ...
          path('api/', include('turbodrf.urls')),
      ]

3. Add ``TurboDRFMixin`` to your models:

   .. code-block:: python

      from django.db import models
      from turbodrf import TurboDRFMixin

      class Product(TurboDRFMixin, models.Model):
          name = models.CharField(max_length=100)
          price = models.DecimalField(max_digits=10, decimal_places=2)

That's it! Your API endpoints are now available at ``/api/products/``.

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`