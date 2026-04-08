# Integrations

## django-allauth

Session-based authentication for SPAs:

```bash
pip install turbodrf[allauth]
```

```python
INSTALLED_APPS = [
    'allauth',
    'allauth.account',
    'allauth.headless',
    'turbodrf',
]

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'turbodrf.integrations.allauth.AllAuthRoleMiddleware',
]

TURBODRF_ALLAUTH_INTEGRATION = True
TURBODRF_ALLAUTH_ROLE_MAPPING = {
    'Administrators': 'admin',
    'Editors': 'editor',
}
```

## Keycloak / OpenID Connect

```bash
pip install social-auth-app-django
```

```python
MIDDLEWARE = [
    'turbodrf.integrations.keycloak.KeycloakRoleMiddleware',
]

TURBODRF_KEYCLOAK_INTEGRATION = True
TURBODRF_KEYCLOAK_ROLE_CLAIM = 'realm_access.roles'
TURBODRF_KEYCLOAK_ROLE_MAPPING = {
    'realm-admin': 'admin',
    'content-editor': 'editor',
}
```

## drf-api-tracking

```bash
pip install drf-api-tracking
```

```python
INSTALLED_APPS = [
    'rest_framework_tracking',
    'turbodrf',
]
```

Automatically logs all requests.
