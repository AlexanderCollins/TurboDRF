API Reference
=============

.. module:: turbodrf

Mixins
------

.. autoclass:: turbodrf.TurboDRFMixin
   :members:
   :undoc-members:
   :show-inheritance:

ViewSets
--------

.. autoclass:: turbodrf.TurboDRFViewSet
   :members:
   :undoc-members:
   :show-inheritance:

Serializers
-----------

.. autoclass:: turbodrf.TurboDRFSerializer
   :members:
   :undoc-members:
   :show-inheritance:

.. autoclass:: turbodrf.TurboDRFSerializerFactory
   :members:
   :undoc-members:
   :show-inheritance:

Permissions
-----------

.. autoclass:: turbodrf.TurboDRFPermission
   :members:
   :undoc-members:
   :show-inheritance:

Router
------

.. autoclass:: turbodrf.TurboDRFRouter
   :members:
   :undoc-members:
   :show-inheritance:

Settings
--------

Every ``TURBODRF_*`` setting, with its default and purpose, lives in one
place: :doc:`settings_reference <settings_reference>`. A few commonly used
ones:

.. code-block:: python

   # Permissions
   TURBODRF_ROLES = {}                       # role -> permission strings
   TURBODRF_USE_DEFAULT_PERMISSIONS = False  # use Django model perms instead

   # Documentation
   TURBODRF_ENABLE_DOCS = True               # Swagger/ReDoc at /swagger/, /redoc/

Pagination is standard DRF pagination and is configured through
``REST_FRAMEWORK`` (``DEFAULT_PAGINATION_CLASS`` / ``PAGE_SIZE``), not
through TurboDRF-specific settings.