Installation
============

Requirements
------------

* Python 3.8+
* Django 3.2+
* Django REST Framework 3.12+

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

Add TurboDRF settings (optional):

.. code-block:: python

   # TurboDRF Settings
   TURBODRF_PAGINATION_SIZE = 20
   TURBODRF_MAX_PAGINATION_SIZE = 100
   TURBODRF_DEFAULT_PERMISSION_CLASSES = ['turbodrf.permissions.TurboDRFPermission']
   TURBODRF_ENABLE_SWAGGER = True

Add URLs to your project:

.. code-block:: python

   from django.urls import path, include

   urlpatterns = [
       path('admin/', admin.site.urls),
       path('api/', include('turbodrf.urls')),
   ]