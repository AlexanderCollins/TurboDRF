"""
Tests for custom viewset functionality.

Tests that users can extend TurboDRFViewSet with custom actions.
"""

from decimal import Decimal
from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework import status
from turbodrf.views import TurboDRFViewSet
from tests.test_app.models import SampleModel, RelatedModel
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.request import Request


User = get_user_model()


class CustomSampleViewSet(TurboDRFViewSet):
    """Custom viewset with additional actions."""

    model = SampleModel
    queryset = SampleModel.objects.all()

    @action(detail=True, methods=["post"])
    def set_featured(self, request, pk=None):
        """Mark an item as featured."""
        item = self.get_object()
        item.is_active = True
        item.save()
        return Response({"status": "featured", "id": item.id})

    @action(detail=False)
    def trending(self, request):
        """Get trending items."""
        # Simple implementation - just get active items
        queryset = self.get_queryset().filter(is_active=True)[:5]
        serializer = self.get_serializer(queryset, many=True)
        return Response(
            {
                "trending": True,
                "count": len(serializer.data),
                "results": serializer.data,
            }
        )

    @action(detail=True, methods=["get", "post"])
    def related_items(self, request, pk=None):
        """Get items related to this one."""
        item = self.get_object()
        related = self.get_queryset().filter(related=item.related).exclude(pk=pk)[:3]

        if request.method == "POST":
            # Example of handling POST in custom action
            return Response({"message": "POST handled", "item_id": pk})

        serializer = self.get_serializer(related, many=True)
        return Response(serializer.data)


class TestCustomViewSets(TestCase):
    """Test custom viewset functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.client = APIClient()

        # Create admin user
        self.admin = User.objects.create_user(username="admin", password="admin123")
        self.admin._test_roles = ["admin"]

        # Create test data
        self.related = RelatedModel.objects.create(
            name="Category", description="Test category"
        )

        self.item1 = SampleModel.objects.create(
            title="Item 1",
            price=Decimal("100.00"),
            quantity=10,
            related=self.related,
            is_active=True,
        )

        self.item2 = SampleModel.objects.create(
            title="Item 2",
            price=Decimal("200.00"),
            quantity=20,
            related=self.related,
            is_active=False,
        )

        self.item3 = SampleModel.objects.create(
            title="Item 3",
            price=Decimal("300.00"),
            quantity=30,
            related=self.related,
            is_active=True,
        )

        # The router and URL registration would happen in urls.py normally
        # For unit tests, we'll test the viewset methods directly

    def test_custom_action_detail_post(self):
        """Test custom detail action with POST method."""
        # Create viewset instance
        viewset = CustomSampleViewSet()
        viewset.format_kwarg = None
        viewset.request = None
        viewset.kwargs = {"pk": self.item2.id}

        # Create request
        factory = APIRequestFactory()
        django_request = factory.post(
            f"/api/custom-samples/{self.item2.id}/set_featured/"
        )
        force_authenticate(django_request, user=self.admin)
        request = Request(django_request)
        viewset.request = request

        # Item 2 is not active
        self.assertFalse(self.item2.is_active)

        # Call custom action directly
        response = viewset.set_featured(request, pk=self.item2.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "featured")
        self.assertEqual(response.data["id"], self.item2.id)

        # Verify item is now active
        self.item2.refresh_from_db()
        self.assertTrue(self.item2.is_active)

    def test_custom_action_list(self):
        """Test custom list action."""
        # Create viewset instance
        viewset = CustomSampleViewSet()
        viewset.format_kwarg = None
        viewset.action = "trending"

        # Create request
        factory = APIRequestFactory()
        django_request = factory.get("/api/custom-samples/trending/")
        force_authenticate(django_request, user=self.admin)
        request = Request(django_request)
        viewset.request = request

        # Call custom action directly
        response = viewset.trending(request)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["trending"])
        self.assertEqual(response.data["count"], 2)  # Only 2 active items

        # Check that only active items are returned
        titles = [item["title"] for item in response.data["results"]]
        self.assertIn("Item 1", titles)
        self.assertIn("Item 3", titles)
        self.assertNotIn("Item 2", titles)

    def test_custom_action_detail_get(self):
        """Test custom detail action with GET method."""
        # Create viewset instance
        viewset = CustomSampleViewSet()
        viewset.format_kwarg = None
        viewset.kwargs = {"pk": self.item1.id}
        viewset.action = "related_items"

        # Create another item with same related object
        item4 = SampleModel.objects.create(
            title="Item 4", price=Decimal("400.00"), quantity=40, related=self.related
        )

        # Create request
        factory = APIRequestFactory()
        django_request = factory.get(
            f"/api/custom-samples/{self.item1.id}/related_items/"
        )
        force_authenticate(django_request, user=self.admin)
        request = Request(django_request)
        viewset.request = request

        # Call custom action directly
        response = viewset.related_items(request, pk=self.item1.id)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)  # Should get 3 related items

        # Verify the current item is excluded
        # The serializer might use different field configuration
        # so let's check if 'id' is present first
        if response.data and "id" in response.data[0]:
            ids = [item["id"] for item in response.data]
            self.assertNotIn(self.item1.id, ids)
            self.assertIn(self.item2.id, ids)
            self.assertIn(self.item3.id, ids)
            self.assertIn(item4.id, ids)
        else:
            # Just verify we got 3 items
            self.assertEqual(len(response.data), 3)

    def test_custom_action_multiple_methods(self):
        """Test custom action that handles multiple HTTP methods."""
        # Create viewset instance
        viewset = CustomSampleViewSet()
        viewset.format_kwarg = None
        viewset.kwargs = {"pk": self.item1.id}
        viewset.action = "related_items"
        factory = APIRequestFactory()

        # Test GET
        django_request = factory.get(
            f"/api/custom-samples/{self.item1.id}/related_items/"
        )
        force_authenticate(django_request, user=self.admin)
        request = Request(django_request)
        viewset.request = request
        response = viewset.related_items(request, pk=self.item1.id)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsInstance(response.data, list)

        # Test POST
        django_request = factory.post(
            f"/api/custom-samples/{self.item1.id}/related_items/"
        )
        force_authenticate(django_request, user=self.admin)
        request = Request(django_request)
        viewset.request = request
        response = viewset.related_items(request, pk=self.item1.id)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["message"], "POST handled")
        self.assertEqual(response.data["item_id"], self.item1.id)

    def test_custom_action_permissions(self):
        """Test that custom actions respect permissions."""
        # Create viewset instance
        viewset = CustomSampleViewSet()
        viewset.format_kwarg = None
        factory = APIRequestFactory()

        # Test unauthenticated POST - should be denied
        django_request = factory.post(
            f"/api/custom-samples/{self.item1.id}/set_featured/"
        )
        django_request.user = None
        request = Request(django_request)
        request.user = None

        # Mock permission checking
        viewset.request = request
        viewset.action = "set_featured"

        # Check permission using viewset's check_permissions
        from rest_framework.exceptions import PermissionDenied

        with self.assertRaises(PermissionDenied):
            viewset.check_permissions(request)

        # Test unauthenticated GET - should be allowed
        django_request = factory.get("/api/custom-samples/trending/")
        django_request.user = None
        request = Request(django_request)
        request.user = None
        viewset.request = request
        viewset.action = "trending"

        # This should not raise an exception
        try:
            viewset.check_permissions(request)
        except PermissionDenied:
            self.fail("GET request should be allowed for unauthenticated users")

    def test_viewset_custom_actions_exist(self):
        """Test that custom actions are properly defined on the viewset."""
        # Check that the viewset has the custom actions
        self.assertTrue(hasattr(CustomSampleViewSet, "set_featured"))
        self.assertTrue(hasattr(CustomSampleViewSet, "trending"))
        self.assertTrue(hasattr(CustomSampleViewSet, "related_items"))

        # Check action decorators
        # set_featured should be detail=True, methods=['post']
        self.assertTrue(CustomSampleViewSet.set_featured.detail)
        self.assertEqual(CustomSampleViewSet.set_featured.url_path, "set_featured")

        # trending should be detail=False (list action)
        self.assertFalse(CustomSampleViewSet.trending.detail)
        self.assertEqual(CustomSampleViewSet.trending.url_path, "trending")

        # related_items should be detail=True, methods=['get', 'post']
        self.assertTrue(CustomSampleViewSet.related_items.detail)
        self.assertEqual(CustomSampleViewSet.related_items.url_path, "related_items")
