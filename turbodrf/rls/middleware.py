"""
Middleware that sets Postgres session-local variables for RLS policies.

On every authenticated request, sets:
    app.user_id     — request.user.id
    app.tenant_id   — request.user.<TURBODRF_TENANT_USER_FIELD>.pk (if present)
    app.user_roles  — comma-separated list of the user's roles

RLS policies reference these via current_setting('app.user_id')::int etc.

Caveat — connection pooling: with pgbouncer in transaction-pooling mode the
connection may be returned to the pool between Django ORM transactions; SET
LOCAL only persists for the current transaction. For session-pooled or
unpooled connections, SET LOCAL works as expected within a transaction-wrapped
request (Django's ATOMIC_REQUESTS or the default per-view transaction).

Caveat — non-Postgres backends: the middleware is a no-op on non-Postgres.
"""

import logging

logger = logging.getLogger(__name__)


def _is_postgres():
    from django.db import connection

    return connection.vendor == "postgresql"


class TurboDRFTenancyMiddleware:
    """Sets app.user_id, app.tenant_id, app.user_roles per request.

    Install in MIDDLEWARE *after* AuthenticationMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if _is_postgres() and getattr(request, "user", None) is not None:
            self._set_session_vars(request)
        return self.get_response(request)

    def _set_session_vars(self, request):
        from django.conf import settings
        from django.db import connection

        from turbodrf.backends import get_user_roles
        from turbodrf.predicates import get_user_tenant

        user = request.user
        if not user.is_authenticated:
            return

        user_id = str(user.pk)
        tenant_value = get_user_tenant(user)
        if tenant_value is None:
            tenant_id = ""
        else:
            tenant_id = str(getattr(tenant_value, "pk", tenant_value))
        roles = ",".join(get_user_roles(user))

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('app.user_id', %s, true)", [user_id]
                )
                cursor.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", [tenant_id]
                )
                cursor.execute(
                    "SELECT set_config('app.user_roles', %s, true)", [roles]
                )
        except Exception as e:
            # Don't break the request if session-var setting fails
            # (e.g. user is unauthenticated, no transaction). Log and proceed.
            logger.warning("Failed to set RLS session vars: %s", e)
