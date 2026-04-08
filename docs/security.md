# Security

## Secure by default

Endpoints require authentication unless `public_access: True` is explicitly set.

## Sensitive fields

Fields like `password`, `token`, and `secret_key` are never exposed in API responses or available for filtering, regardless of permissions.

Default deny-list:

```python
TURBODRF_SENSITIVE_FIELDS = [
    'password', 'password_hash', 'secret_key', 'api_key',
    'token', 'access_token', 'refresh_token', 'session_key',
]
```

Override in your settings to customise.

## No-roles denial

Authenticated users with no assigned roles get 403 on all endpoints. This prevents accidental access when a user account exists but hasn't been configured.

## Filter security

Users can only filter on fields they have read permission for. This prevents binary search attacks where an attacker infers hidden values by filtering (e.g. `?salary__gte=100000`).

## Error responses

All errors follow a consistent format. Enable with:

```python
REST_FRAMEWORK = {
    'EXCEPTION_HANDLER': 'turbodrf.exceptions.turbodrf_exception_handler',
}
```

```json
{
    "error": {
        "status": 403,
        "code": "permission_denied",
        "message": "You do not have permission to perform this action."
    }
}
```

## Fail-closed design

If a permission check fails due to an error (database issue, malformed data), access is denied. TurboDRF never grants access on exception.
