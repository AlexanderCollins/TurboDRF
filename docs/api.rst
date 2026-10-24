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

Scope
-----

The scope a request runs in, ``"tenant"`` or ``"platform"`` (see
:doc:`platform <platform>`).

.. autofunction:: turbodrf.scope.current_scope

.. autofunction:: turbodrf.scope.use_scope

Settings
--------

Every ``TURBODRF_*`` setting, with its default and purpose, lives in one
place: :doc:`settings_reference <settings_reference>`. A few commonly used
ones:

.. code-block:: python

   # Permissions
   TURBODRF_ROLES = {                        # role -> permission strings
       'viewer': ['books.book.read'],
   }
   TURBODRF_USE_DEFAULT_PERMISSIONS = False  # use Django model perms instead

   # Documentation
   TURBODRF_ENABLE_DOCS = True               # Swagger/ReDoc at /swagger/, /redoc/

Pagination isn't configurable through ``REST_FRAMEWORK``: every TurboDRF
viewset uses ``TurboDRFPagination`` (page-number pagination, 20 items per
page, at most 100, with the ``page`` and ``page_size`` query parameters),
whatever ``DEFAULT_PAGINATION_CLASS`` / ``PAGE_SIZE`` say. See
:doc:`filtering <filtering>` for the response format.