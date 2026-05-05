"""
Predicate-based row-level access control for TurboDRF.

Predicates declare visibility rules per model. They:
- Produce Django Q objects for read filtering (get_queryset / get_object)
- Auto-fill mandatory fields on create (tenant FK)
- Validate that writes don't violate the predicate
- Optionally generate Postgres RLS policies

Stack with AND. Use Either(...) for OR. Multi-role users are OR'd across roles
(more roles = more access).

Sugar form (the common case) compiles to a predicate list internally:

    'tenant_field': 'brokerage',
    'owner_field': 'assigned_to',
    'bypass_owner_roles': ['admin', 'manager'],
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models import Q


def get_user_tenant(user):
    """Resolve the user's tenant value (FK object or PK).

    Reads TURBODRF_TENANT_USER_FIELD from settings. None on missing/unset.
    """
    tenant_field = getattr(settings, "TURBODRF_TENANT_USER_FIELD", None)
    if not tenant_field:
        return None
    tenant = getattr(user, tenant_field, None)
    # Coerce to (model | int | None). Anything else (string, dict, bound
    # method, related manager) would either crash Django's ORM at
    # SQL-compile time or — worse — silently match unrelated rows. Falling
    # back to None fail-closes downstream filters.
    if tenant is None:
        return None
    if hasattr(tenant, "pk"):
        return tenant
    if isinstance(tenant, int):
        return tenant
    return None


def _authed_user(request):
    """Return request.user if authenticated, else None."""
    if not request:
        return None
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return user


def _no_match_q():
    """Q that matches zero rows. Used for fail-closed."""
    return Q(pk__in=[])


class Predicate:
    """Base class for visibility predicates.

    Override:
        q(request, user_roles)             → Q for read filter
        auto_fill(validated_data, request) → dict (mutated) for create
        validate_write(validated_data, instance, request) → list[str] errors
        to_rls_using_clause()              → SQL bool expr for Postgres RLS
    """

    mandatory = False

    def q(self, request, user_roles):
        return Q()

    def auto_fill(self, validated_data, request):
        return validated_data

    def validate_write(self, validated_data, instance, request):
        return []

    def to_rls_using_clause(self):
        raise NotImplementedError(
            f"{type(self).__name__} does not generate RLS clauses."
        )

    def to_rls_policy(self, table_name, policy_name=None):
        clause = self.to_rls_using_clause()
        name = policy_name or f"{table_name}_{type(self).__name__.lower()}"
        return f"CREATE POLICY {name} ON {table_name} USING ({clause});"


class Tenant(Predicate):
    """Mandatory tenant boundary.

    Filters by request.user.<TURBODRF_TENANT_USER_FIELD>. Auto-fills on create
    (direct columns only). Rejects PATCH/POST that would set the tenant FK to
    a different value. `field` accepts `__`-paths for chained tenancy.
    """

    mandatory = True

    def __init__(self, field):
        self.field = field

    def q(self, request, user_roles):
        user = _authed_user(request)
        if user is None:
            return _no_match_q()
        tenant = get_user_tenant(user)
        if tenant is None:
            return _no_match_q()
        return Q(**{self.field: tenant})

    def auto_fill(self, validated_data, request):
        if "__" in self.field:
            return validated_data
        user = _authed_user(request)
        if user is None:
            return validated_data
        tenant = get_user_tenant(user)
        if tenant is None:
            return validated_data
        # Always overwrite — never trust client-provided tenant
        validated_data = dict(validated_data)
        validated_data[self.field] = tenant
        return validated_data

    def validate_write(self, validated_data, instance, request):
        if "__" in self.field or self.field not in validated_data:
            return []
        user = _authed_user(request)
        if user is None:
            return [f"Cannot set {self.field}: no authenticated user."]
        provided_pk = getattr(
            validated_data[self.field], "pk", validated_data[self.field]
        )
        expected_pk = getattr(get_user_tenant(user), "pk", get_user_tenant(user))
        if provided_pk != expected_pk:
            return [f"Cannot set {self.field} to a different tenant."]
        return []

    def to_rls_using_clause(self):
        if "__" in self.field:
            raise NotImplementedError(
                f"Tenant RLS does not support chained paths ({self.field!r}). "
                f"Add a Tenant policy on each table referencing its closest "
                f"tenant FK column."
            )
        col = self.field if self.field.endswith("_id") else f"{self.field}_id"
        return f"{col} = current_setting('app.tenant_id')::int"


class Owner(Predicate):
    """Within-tenant ownership with role bypass.

    `field` may be a single column or list of columns (multi-owner case — any
    matching column grants visibility). Bypass roles see all rows in tenant.
    """

    def __init__(self, field, bypass=None):
        if isinstance(field, str):
            self.fields = [field]
        else:
            self.fields = list(field)
            if not self.fields:
                raise ImproperlyConfigured("Owner requires at least one field.")
        self.bypass = set(bypass or [])

    def q(self, request, user_roles):
        if user_roles and (user_roles & self.bypass):
            return Q()
        user = _authed_user(request)
        if user is None:
            return _no_match_q()
        q = Q()
        for f in self.fields:
            q |= Q(**{f: user})
        return q

    def auto_fill(self, validated_data, request):
        if len(self.fields) != 1 or "__" in self.fields[0]:
            return validated_data
        user = _authed_user(request)
        if user is None:
            return validated_data
        field = self.fields[0]
        if field in validated_data:
            return validated_data
        validated_data = dict(validated_data)
        validated_data[field] = user
        return validated_data

    def validate_write(self, validated_data, instance, request):
        if any("__" in f for f in self.fields):
            return []
        user = _authed_user(request)
        if user is None:
            return []
        from .backends import get_user_roles

        if set(get_user_roles(user)) & self.bypass:
            return []
        errors = []
        for f in self.fields:
            if f in validated_data:
                value_pk = getattr(validated_data[f], "pk", validated_data[f])
                if value_pk != user.pk:
                    errors.append(f"Cannot set {f} to a different user.")
        return errors

    def to_rls_using_clause(self):
        if any("__" in f for f in self.fields):
            raise NotImplementedError("Owner RLS does not support chained paths.")
        col_clauses = []
        for f in self.fields:
            col = f if f.endswith("_id") else f"{f}_id"
            col_clauses.append(f"{col} = current_setting('app.user_id')::int")
        owner_clause = " OR ".join(col_clauses)
        if self.bypass:
            roles = "|".join(sorted(self.bypass))
            return (
                f"({owner_clause}) OR "
                f"current_setting('app.user_roles') ~ E'\\\\m({roles})\\\\M'"
            )
        return owner_clause


class Either(Predicate):
    """OR of child predicates.

    Read filter: OR of children's Q.
    Write validation: passes if ANY child passes.
    Auto-fill: not applied (ambiguous).
    """

    def __init__(self, *predicates):
        if not predicates:
            raise ImproperlyConfigured("Either requires at least one predicate.")
        for p in predicates:
            if not isinstance(p, Predicate):
                raise ImproperlyConfigured(
                    f"Either children must be Predicate instances; got {type(p).__name__}"
                )
        self.predicates = predicates

    def q(self, request, user_roles):
        combined = None
        for pred in self.predicates:
            child_q = pred.q(request, user_roles)
            combined = child_q if combined is None else combined | child_q
        return combined if combined is not None else Q()

    def validate_write(self, validated_data, instance, request):
        all_errors = []
        for pred in self.predicates:
            errors = pred.validate_write(validated_data, instance, request)
            if not errors:
                return []
            all_errors.extend(errors)
        return all_errors

    def to_rls_using_clause(self):
        return " OR ".join(f"({p.to_rls_using_clause()})" for p in self.predicates)


class Custom(Predicate):
    """Escape hatch for arbitrary within-tenant predicates.

    The Tenant boundary is applied separately as a setting and is NOT
    bypassable — so even if `q_func` returns Q() (no within-tenant
    restriction), tenant isolation still holds. The previous
    `unrestricted_ok` flag was removed because it solved a problem (Tenant
    inside the predicate algebra) that no longer exists in this design.

    For visibility into accidental Q() returns, set
    TURBODRF_LOG_UNRESTRICTED_CUSTOM=True — a warning is logged whenever a
    Custom predicate returns an empty Q.
    """

    def __init__(self, q_func, write_validator=None, auto_filler=None):
        if not callable(q_func):
            raise ImproperlyConfigured("Custom requires a callable q_func.")
        self.q_func = q_func
        self.write_validator = write_validator
        self.auto_filler = auto_filler

    def q(self, request, user_roles):
        if not request:
            return _no_match_q()
        result = self.q_func(request, user_roles)
        # Optional runtime warning if the q_func returned an empty Q.
        # Tenant isolation still holds (separate layer), but this is
        # usually a sign of a bug in the q_func.
        if not result.children:
            from django.conf import settings as _s

            if getattr(_s, "TURBODRF_LOG_UNRESTRICTED_CUSTOM", False):
                import logging

                logging.getLogger(__name__).warning(
                    "Custom predicate returned an empty Q (no within-tenant "
                    "restriction). Tenant isolation still applies but this "
                    "may indicate a bug in q_func."
                )
        return result

    def auto_fill(self, validated_data, request):
        if self.auto_filler:
            return self.auto_filler(validated_data, request)
        return validated_data

    def validate_write(self, validated_data, instance, request):
        if self.write_validator:
            return self.write_validator(validated_data, instance, request)
        return []


# ---------------------------------------------------------------------------
# Advanced predicates — kept importable but not surfaced in main docs.
# Use these when the sugar form + Tenant/Owner/Either/Custom doesn't fit.
# ---------------------------------------------------------------------------


class Members(Predicate):
    """Advanced: user must be in the row's M2M-to-User collection."""

    def __init__(self, m2m_field):
        self.m2m_field = m2m_field

    def q(self, request, user_roles):
        user = _authed_user(request)
        if user is None:
            return _no_match_q()
        return Q(**{self.m2m_field: user})


class Group(Predicate):
    """Advanced: user must belong to the group/team that owns the row.

    `field` is the FK on the row to the group/team.
    `user_via` is the reverse-M2M name from group/team to user.
    """

    def __init__(self, field, user_via="members"):
        self.field = field
        self.user_via = user_via

    def q(self, request, user_roles):
        user = _authed_user(request)
        if user is None:
            return _no_match_q()
        return Q(**{f"{self.field}__{self.user_via}": user})


class Conditional(Predicate):
    """Advanced: rows matching `when` are visible only to users with a required role."""

    def __init__(self, when, require_roles):
        if not isinstance(when, Q):
            raise ImproperlyConfigured("Conditional `when` must be a Q object.")
        self.when = when
        self.require_roles = set(require_roles or [])

    def q(self, request, user_roles):
        if user_roles and (user_roles & self.require_roles):
            return Q()
        return ~self.when


# ---------------------------------------------------------------------------
# Sugar parser
# ---------------------------------------------------------------------------
# turbodrf() config dict → (tenant_field, list[Predicate])
#
# Tenant is a SETTING, not a predicate — applied as a separate AND outside
# the predicate algebra. Keeping it outside prevents cross-tenant escape
# via Either OR-composition. Owner/Members/Either/Custom operate ONLY
# within-tenant.
# ---------------------------------------------------------------------------


def parse_config(config):
    """Convert turbodrf() config to (tenant_field: str|None, predicates: list).

    Recognized keys:
      - 'tenancy': 'shared'         → (None, [])
      - 'tenant_field': str         → tenant_field setting (NOT a predicate)
      - 'owner_field': str|list     → Owner / Either(Owner,...)  predicate
      - 'bypass_owner_roles': list  → applied to Owner predicates
      - 'visibility': [Predicate..] → power form. Tenant() inside is
        EXTRACTED to the tenant_field setting (with deprecation warning).
        Tenant() inside an Either raises — would let OR-composition
        escape the tenant boundary.
    """
    import logging
    import warnings

    logging.getLogger(__name__)

    if not isinstance(config, dict):
        raise ImproperlyConfigured(
            f"turbodrf() must return a dict; got {type(config).__name__}"
        )

    if config.get("tenancy") == "shared":
        return None, []

    tenant_field = None
    predicates = []

    visibility = config.get("visibility")
    if visibility is not None:
        # Mixing sugar form with power form is ambiguous — refuse to guess
        # which one wins. Silently dropping either side is how cross-tenant
        # leaks happen.
        conflicting = [
            k
            for k in ("tenant_field", "owner_field", "bypass_owner_roles")
            if config.get(k) is not None
        ]
        if conflicting:
            raise ImproperlyConfigured(
                f"Cannot mix 'visibility' with sugar keys {conflicting}. "
                f"Pick one form: either 'visibility=[...]' (power form) OR "
                f"'tenant_field' / 'owner_field' / 'bypass_owner_roles' "
                f"(sugar form)."
            )
        if not isinstance(visibility, (list, tuple)):
            raise ImproperlyConfigured(
                "'visibility' must be a list of Predicate instances."
            )
        for p in visibility:
            if not isinstance(p, Predicate):
                raise ImproperlyConfigured(
                    f"'visibility' items must be Predicate instances; "
                    f"got {type(p).__name__}"
                )
            _reject_tenant_inside_either(p)

        # Extract any top-level Tenant predicates into tenant_field setting
        cleaned = []
        for p in visibility:
            if isinstance(p, Tenant):
                if tenant_field is not None and tenant_field != p.field:
                    raise ImproperlyConfigured(
                        f"Multiple Tenant predicates in 'visibility' with "
                        f"different fields: {tenant_field!r} and {p.field!r}. "
                        f"Use a single 'tenant_field' setting instead."
                    )
                tenant_field = p.field
                warnings.warn(
                    "Tenant() inside 'visibility' is deprecated. Use the "
                    "'tenant_field' setting instead — Tenant is a mandatory "
                    "boundary, not a composable predicate.",
                    DeprecationWarning,
                    stacklevel=3,
                )
            else:
                cleaned.append(p)
        return tenant_field, cleaned

    # Sugar form
    tf = config.get("tenant_field")
    if tf is not None:
        if not isinstance(tf, str):
            raise ImproperlyConfigured("'tenant_field' must be a string.")
        tenant_field = tf

    owner_field = config.get("owner_field")
    bypass = config.get("bypass_owner_roles", [])
    if owner_field is not None:
        if isinstance(owner_field, str):
            predicates.append(Owner(owner_field, bypass=bypass))
        elif isinstance(owner_field, (list, tuple)):
            owner_fields = list(owner_field)
            if len(owner_fields) == 1:
                predicates.append(Owner(owner_fields[0], bypass=bypass))
            elif len(owner_fields) > 1:
                predicates.append(
                    Either(*[Owner(f, bypass=bypass) for f in owner_fields])
                )
        else:
            raise ImproperlyConfigured("'owner_field' must be str or list[str].")

    return tenant_field, predicates


def _reject_tenant_inside_either(predicate):
    """Walk the predicate tree; raise if Tenant appears inside Either/Custom.

    Tenant inside Either ORs with sibling predicates that may collapse to
    Q() for bypass roles, which would erase the tenant boundary entirely.
    Tenant must always be at the top level — and ideally not in the
    visibility list at all; use the tenant_field setting.
    """
    if isinstance(predicate, Either):
        for child in predicate.predicates:
            if isinstance(child, Tenant):
                raise ImproperlyConfigured(
                    "Tenant() cannot appear inside Either(). The tenant "
                    "boundary is mandatory and must not OR-compose with "
                    "discretionary predicates (would create cross-tenant "
                    "escape for bypass roles). Use the 'tenant_field' "
                    "setting at the top level instead."
                )
            _reject_tenant_inside_either(child)


def has_tenancy_declaration(config):
    """True if config declares tenancy (predicates, sugar, or 'shared')."""
    if not isinstance(config, dict):
        return False
    return any(
        config.get(k) is not None or config.get(k) == "shared"
        for k in ("tenancy", "visibility", "tenant_field", "owner_field")
    )


# ---------------------------------------------------------------------------
# Module-level registries (populated by router at startup)
# ---------------------------------------------------------------------------

_model_predicates = {}
_model_tenant_fields = {}


def register_predicates(model, predicates):
    _model_predicates[model] = list(predicates)


def get_predicates(model):
    return _model_predicates.get(model, [])


def register_tenant_field(model, tenant_field):
    """Register the tenant_field setting for a model. None means 'no tenant'."""
    if tenant_field is None:
        _model_tenant_fields.pop(model, None)
    else:
        _model_tenant_fields[model] = tenant_field


def get_tenant_field(model):
    """Return the tenant_field setting for a model, or None."""
    return _model_tenant_fields.get(model)


def clear_predicates():
    """Used in tests to reset between runs."""
    _model_predicates.clear()
    _model_tenant_fields.clear()
