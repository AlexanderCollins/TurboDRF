from django.apps import AppConfig


class BooksConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "books"
    
    def ready(self):
        # Extend the User model with roles
        from django.contrib.auth import get_user_model
        
        User = get_user_model()
        
        def get_user_roles(self):
            # For now, return admin role for superusers, viewer for others
            if self.is_superuser:
                return ['admin']
            elif self.is_staff:
                return ['editor']
            else:
                return ['viewer']
        
        # Add the roles property to the User model
        if not hasattr(User, 'roles'):
            User.add_to_class('roles', property(get_user_roles))
