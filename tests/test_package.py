"""
Test that the TurboDRF package is properly structured and installable.
"""

import unittest

import turbodrf


class TestPackageStructure(unittest.TestCase):
    """Test package structure and imports."""

    def test_version_defined(self):
        """Test that package version is defined."""
        self.assertTrue(hasattr(turbodrf, "__version__"))
        self.assertIsInstance(turbodrf.__version__, str)
        # Version should follow semantic versioning format
        version_parts = turbodrf.__version__.split(".")
        self.assertEqual(len(version_parts), 3)
        for part in version_parts:
            self.assertTrue(part.isdigit())

    def test_core_imports(self):
        """Test that core components can be imported."""
        # Test main components
        from turbodrf import (
            TurboDRFMixin,
            TurboDRFPermission,
            TurboDRFRouter,
            TurboDRFSerializer,
            TurboDRFSerializerFactory,
            TurboDRFViewSet,
        )

        # Verify they're classes/functions
        self.assertTrue(callable(TurboDRFMixin))
        self.assertTrue(callable(TurboDRFRouter))
        self.assertTrue(callable(TurboDRFViewSet))
        self.assertTrue(callable(TurboDRFPermission))
        self.assertTrue(callable(TurboDRFSerializer))
        self.assertTrue(callable(TurboDRFSerializerFactory))

    def test_submodule_imports(self):
        """Test that submodules can be imported."""
        # Test individual module imports
        import turbodrf.documentation
        import turbodrf.mixins
        import turbodrf.permissions
        import turbodrf.router
        import turbodrf.serializers
        import turbodrf.swagger
        import turbodrf.swagger_ui
        import turbodrf.views

        # Verify modules are loaded
        self.assertIsNotNone(turbodrf.mixins)
        self.assertIsNotNone(turbodrf.router)
        self.assertIsNotNone(turbodrf.views)

    def test_all_exports(self):
        """Test that __all__ exports are correct."""
        expected_exports = [
            "TurboDRFMixin",
            "TurboDRFRouter",
            "TurboDRFViewSet",
            "TurboDRFPermission",
            "TurboDRFSerializer",
            "TurboDRFSerializerFactory",
        ]

        self.assertTrue(hasattr(turbodrf, "__all__"))
        for export in expected_exports:
            self.assertIn(export, turbodrf.__all__)
            self.assertTrue(hasattr(turbodrf, export))


if __name__ == "__main__":
    unittest.main()
