import json
from django.test import TestCase, override_settings
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status

from tests.test_app.models import SampleModel, RelatedModel


class TestDisablePermissions(TestCase):
    """Test cases for TURBODRF_DISABLE_PERMISSIONS setting."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.client = APIClient()
        # Create a related model first
        self.related = RelatedModel.objects.create(
            name='Related Item',
            description='Related Description'
        )
        self.test_data = {
            'title': 'Test Item',
            'description': 'Test Description',
            'price': '29.99',
            'quantity': 10,
            'is_active': True,
            'related': self.related.id
        }
        
    def test_permissions_enabled_by_default(self):
        """Test that permissions are enforced by default (no TURBODRF_DISABLE_PERMISSIONS setting)."""
        # Try to create without authentication - should fail
        response = self.client.post('/api/samplemodels/', self.test_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        
    @override_settings(TURBODRF_DISABLE_PERMISSIONS=False)
    def test_permissions_enabled_when_false(self):
        """Test that permissions are enforced when TURBODRF_DISABLE_PERMISSIONS=False."""
        # Try to create without authentication - should fail
        response = self.client.post('/api/samplemodels/', self.test_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        
    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_permissions_disabled_allows_all_operations(self):
        """Test that all CRUD operations work without authentication when permissions are disabled."""
        # CREATE - should work without authentication
        response = self.client.post('/api/samplemodels/', self.test_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['title'], 'Test Item')
        created_id = response.data['id']
        
        # READ (List) - should work without authentication
        response = self.client.get('/api/samplemodels/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data['results']), 0)
        
        # READ (Detail) - should work without authentication
        response = self.client.get(f'/api/samplemodels/{created_id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['title'], 'Test Item')
        
        # UPDATE (PUT) - should work without authentication
        updated_data = self.test_data.copy()
        updated_data['title'] = 'Updated Item'
        response = self.client.put(f'/api/samplemodels/{created_id}/', updated_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['title'], 'Updated Item')
        
        # UPDATE (PATCH) - should work without authentication
        patch_data = {'description': 'Patched Description'}
        response = self.client.patch(f'/api/samplemodels/{created_id}/', patch_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['description'], 'Patched Description')
        
        # DELETE - should work without authentication
        response = self.client.delete(f'/api/samplemodels/{created_id}/')
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        
        # Verify deletion
        response = self.client.get(f'/api/samplemodels/{created_id}/')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        
    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_authenticated_users_still_work_with_disabled_permissions(self):
        """Test that authenticated users can still access endpoints when permissions are disabled."""
        # Create a user and authenticate
        user = User.objects.create_user(username='testuser', password='testpass')
        self.client.force_authenticate(user=user)
        
        # All operations should still work
        response = self.client.post('/api/samplemodels/', self.test_data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        
        created_id = response.data['id']
        response = self.client.get(f'/api/samplemodels/{created_id}/')
        self.assertEqual(response.status_code, status.HTTP_200_OK)