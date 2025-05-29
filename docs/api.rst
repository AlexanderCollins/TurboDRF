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

Available settings for TurboDRF:

.. code-block:: python

   # Pagination settings
   TURBODRF_PAGINATION_SIZE = 20
   TURBODRF_MAX_PAGINATION_SIZE = 100

   # Permission settings
   TURBODRF_DEFAULT_PERMISSION_CLASSES = ['turbodrf.permissions.TurboDRFPermission']
   TURBODRF_USE_DEFAULT_PERMISSIONS = False

   # Documentation settings
   TURBODRF_ENABLE_SWAGGER = True
   TURBODRF_SWAGGER_TITLE = "TurboDRF API"
   TURBODRF_SWAGGER_VERSION = "v1"

   # Search and filter settings
   TURBODRF_DEFAULT_SEARCH_FIELDS = ['name', 'title', 'description']
   TURBODRF_DEFAULT_ORDERING_FIELDS = '__all__'
   TURBODRF_DEFAULT_FILTERSET_FIELDS = '__all__'