"""
Postgres Row Level Security (RLS) support for TurboDRF.

This is an optional defense-in-depth layer for Postgres deployments. It
complements the app-layer predicate enforcement: RLS catches paths that bypass
the framework (raw SQL, admin scripts, ORM bugs) by enforcing the same rules
at the database layer.

Components:
    - middleware.TurboDRFTenancyMiddleware:
        Sets app.user_id, app.tenant_id, app.user_roles as session-local
        Postgres variables on every request.
    - predicates' to_rls_policy() / to_rls_using_clause():
        Generate CREATE POLICY SQL based on declared predicates.
    - turbodrf_emit_rls management command:
        Walks all TurboDRF models and emits draft RLS SQL for review.

Important: TurboDRF does not manage RLS policy lifecycle (migrations, drops,
alters) — that's the developer's responsibility. The emit_rls command produces
a starting point; review it and migrate it in deliberately.
"""

from .middleware import TurboDRFTenancyMiddleware  # noqa: F401
