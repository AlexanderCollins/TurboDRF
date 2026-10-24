"""
The scope a TurboDRF request runs in.

``"tenant"`` (the default) is what TurboDRF has always served: rows are scoped
by the tenant boundary and the model's predicates, roles come from
``user.roles`` (or ``UserRole`` rows in database mode) and permissions from
``TURBODRF_ROLES``.

``"platform"`` serves an operator's own staff across tenants. A
``TurboDRFRouter(scope="platform", gate=[...])`` registers the models whose
``turbodrf()`` config has a ``"platform"`` block. On that scope no tenant
boundary and no predicates apply, roles come from
``TURBODRF_PLATFORM_ROLE_GETTER`` and permissions from
``TURBODRF_PLATFORM_ROLES`` (always static). See docs/platform.md.

The scope is a context variable. ``TurboDRFViewSet.dispatch`` sets it for each
request (and renders the response inside it); the schema generator and the
management commands set it explicitly. The code that depends on it reads
``current_scope()``: the model config (``mixins._model_config``), roles and
permission snapshots (``backends``), the row-scoping registry
(``predicates.get_predicates`` / ``get_tenant_field``) and the compiled plans.
"""

import contextvars
from contextlib import contextmanager

TENANT = "tenant"
PLATFORM = "platform"
SCOPES = (TENANT, PLATFORM)

_current = contextvars.ContextVar("turbodrf_scope", default=TENANT)


def current_scope():
    """The scope of the code running now: ``"tenant"`` unless a platform
    request (or a platform schema / command) set it."""
    return _current.get()


def is_platform():
    return _current.get() == PLATFORM


@contextmanager
def use_scope(scope):
    """Run the block in ``scope``."""
    if scope not in SCOPES:
        raise ValueError(f"Unknown TurboDRF scope {scope!r}; use one of {SCOPES}.")
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


# turbodrf() keys that decide row scoping. They have no meaning in a platform
# block: the platform scope applies no tenant boundary and no predicates.
TENANCY_KEYS = frozenset(
    {
        "bypass_owner_roles",
        "owner_field",
        "public_access",
        "tenancy",
        "tenant_field",
        "visibility",
    }
)


def platform_block(model, config=None):
    """The ``"platform"`` block of ``model``'s ``turbodrf()`` config, or
    ``None`` when it has none (or isn't a TurboDRF model)."""
    if config is None:
        if not callable(getattr(model, "turbodrf", None)):
            return None
        try:
            config = model.turbodrf()
        except Exception:
            return None
    block = config.get("platform") if isinstance(config, dict) else None
    return block if isinstance(block, dict) else None


def platform_config(config):
    """The config the platform scope serves for a model whose top-level
    ``turbodrf()`` config is ``config``: its platform block. A model without
    one exposes nothing there."""
    block = platform_block(None, config)
    if block is None:
        return {"enabled": False, "fields": []}
    return dict(block)


def platform_reachable(model):
    """True if the platform scope may reach ``model``'s rows through a
    relation: a nested ``__`` field, a bare M2M or reverse relation, a filter,
    a search path, a ``property_relations`` hop, or a FK / M2M write.

    Reachable: a model with a platform block (``"enabled": False`` in it
    gives no route but keeps it reachable), the tenant model
    (``TURBODRF_TENANT_MODEL``), and a model that isn't a TurboDRF model,
    which has no row rules on either scope. A TurboDRF model without a
    platform block declared rules for its tenants only, and the platform
    scope doesn't lift them: it reads as having no rows."""
    if model is None or not hasattr(model, "_meta"):
        return False
    from .mixins import TurboDRFMixin

    if not issubclass(model, TurboDRFMixin):
        return True
    if platform_block(model) is not None:
        return True
    return _is_tenant_model(model)


def _is_tenant_model(model):
    from django.conf import settings

    from .tenancy import _resolve_tenant_model

    try:
        tenant_model = _resolve_tenant_model(
            getattr(settings, "TURBODRF_TENANT_MODEL", None)
        )
    except Exception:
        return False
    return (
        tenant_model is not None
        and model._meta.concrete_model is tenant_model._meta.concrete_model
    )
