Installation
============

Requirements
------------

* Python 3.10+
* Django 4.2+
* Django REST Framework 3.14+

``pyproject.toml`` is the source of truth for supported versions.

Install from PyPI
-----------------

.. code-block:: bash

   pip install turbodrf

Install from Source
-------------------

.. code-block:: bash

   git clone https://github.com/alexandercollins/turbodrf.git
   cd turbodrf
   pip install -e .

Configuration
-------------

Add ``turbodrf`` to your ``INSTALLED_APPS``:

.. code-block:: python

   INSTALLED_APPS = [
       'django.contrib.admin',
       'django.contrib.auth',
       'django.contrib.contenttypes',
       'django.contrib.sessions',
       'django.contrib.messages',
       'django.contrib.staticfiles',
       'rest_framework',
       'turbodrf',  # Add this
       # Your apps...
   ]

Define your roles (required for anything but public models):

.. code-block:: python

   TURBODRF_ROLES = {
       'admin': ['myapp.book.read', 'myapp.book.create',
                 'myapp.book.update', 'myapp.book.delete'],
       'viewer': ['myapp.book.read'],
   }

See :doc:`settings_reference <settings_reference>` for every ``TURBODRF_*``
setting and its default.

Add URLs to your project:

.. code-block:: python

   from django.urls import path, include

   urlpatterns = [
       path('admin/', admin.site.urls),
       path('api/', include('turbodrf.urls')),
   ]