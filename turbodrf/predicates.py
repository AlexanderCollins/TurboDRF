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

            if getattr(_s, "TURBODRF_LOG_UNRESTRICTED_CUSTOM", True):
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
    """Advanced: user must be in the row's M2M-to-User collection.

    Read-only enforcement. ``Members`` does not implement
    ``auto_fill`` or ``validate_write``, so wiring it onto a writable
    endpoint will silently let any tenant member create or update a
    row without the membership check. Use ``Owner`` (or ``Custom`` with
    explicit write hooks) for writable paths and reserve ``Members``
    for read-only models.
    """

    def __init__(self, m2m_field):
        self.m2m_field = m2m_field

    def q(self, request, user_roles):
        user = _authed_user(request)
        if user is None:
            return _no_match_q()
        return Q(**{self.m2m_field: user})

    def auto_fill(self, validated_data, request):
        raise NotImplementedError(
            "Members predicate has no auto_fill — it only enforces "
            "row reads. For writable endpoints use Owner or Custom "
            "with explicit write hooks."
        )

    def validate_write(self, validated_data, instance, request):
        raise NotImplementedError(
            "Members predicate has no validate_write — it only "
            "enforces row reads. For writable endpoints use Owner "
            "or Custom with explicit write hooks."
        )


class Group(Predicate):
    """Advanced: user must belong to the group/team that owns the row.

    ``field`` is the FK on the row to the group/team.
    ``user_via`` is the reverse-M2M name from group/team to user.

    Read-only enforcement. ``Group`` does not implement ``auto_fill``
    or ``validate_write``, so wiring it onto a writable endpoint will
    silently let any tenant member create or update a row without the
    membership check. Use ``Owner`` (or ``Custom`` with explicit write
    hooks) for writable paths.
    """

    def __init__(self, field, user_via="members"):
        self.field = field
        self.user_via = user_via

    def q(self, request, user_roles):
        user = _authed_user(request)
        if user is None:
            return _no_match_q()
        return Q(**{f"{self.field}__{self.user_via}": user})

    def auto_fill(self, validated_data, request):
        raise NotImplementedError(
            "Group predicate has no auto_fill — it only enforces "
            "row reads. For writable endpoints use Owner or Custom "
            "with explicit write hooks."
        )

    def validate_write(self, validated_data, instance, request):
        raise NotImplementedError(
            "Group predicate has no validate_write — it only "
            "enforces row reads. For writable endpoints use Owner "
            "or Custom with explicit write hooks."
        )


class Conditional(Predicate):
    """Advanced: rows matching ``when`` are visible only to users with a
    required role.

    Read-only enforcement. ``Conditional`` does not implement
    ``auto_fill`` or ``validate_write``; a writable endpoint with a
    ``Conditional`` predicate would let users freely create or update
    rows that match ``when`` regardless of role. Use ``Owner`` /
    ``Custom`` with explicit write hooks for writable paths.
    """

    def __init__(self, when, require_roles):
        if not isinstance(when, Q):
            raise ImproperlyConfigured("Conditional `when` must be a Q object.")
        self.when = when
        self.require_roles = set(require_roles or [])

    def q(self, request, user_roles):
        if user_roles and (user_roles & self.require_roles):
            return Q()
        return ~self.when

    def auto_fill(self, validated_data, request):
        raise NotImplementedError(
            "Conditional predicate has no auto_fill — it only "
            "enforces row reads. For writable endpoints use Owner "
            "or Custom with explicit write hooks."
        )

    def validate_write(self, validated_data, instance, request):
        raise NotImplementedError(
            "Conditional predicate has no validate_write — it only "
            "enforces row reads. For writable endpoints use Owner "
            "or Custom with explicit write hooks."
        )


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
        # `tenant_field` is conceptually orthogonal to `visibility` — the
        # tenant boundary is a setting outside the predicate algebra, while
        # `visibility` is the within-tenant algebra. They compose cleanly
        # and are the recommended pairing whenever you need
        # Either/Custom alongside tenancy. `owner_field` and
        # `bypass_owner_roles` are sugar that produces predicates which
        # would also appear in `visibility`, so those genuinely conflict.
        conflicting = [
            k
            for k in ("owner_field", "bypass_owner_roles")
            if config.get(k) is not None
        ]
        if conflicting:
            raise ImproperlyConfigured(
                f"Cannot mix 'visibility' with sugar keys {conflicting}. "
                f"Either use 'visibility=[...]' (and put Owner(...) inside "
                f"it) or use 'owner_field' / 'bypass_owner_roles' sugar — "
                f"not both. 'tenant_field' is allowed alongside "
                f"'visibility' since it's a setting, not a predicate."
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

        # Honor an explicit tenant_field setting when present.
        tf = config.get("tenant_field")
        if tf is not None:
            if not isinstance(tf, str):
                raise ImproperlyConfigured("'tenant_field' must be a string.")
            tenant_field = tf

        # Extract any top-level Tenant predicates into tenant_field setting.
        # Still emits a deprecation warning so the canonical form is
        # `tenant_field=...` + within-tenant `visibility=[...]`, but no
        # longer dead-ends users when they follow the warning's advice.
        cleaned = []
        for p in visibility:
            if isinstance(p, Tenant):
                if tenant_field is not None and tenant_field != p.field:
                    raise ImproperlyConfigured(
                        f"Multiple tenant fields declared: 'tenant_field' "
                        f"setting is {tenant_field!r} but Tenant() inside "
                        f"'visibility' uses {p.field!r}. Use a single "
                        f"'tenant_field' setting."
                    )
                tenant_field = p.field
                warnings.warn(
                    "Tenant() inside 'visibility' is deprecated. Use the "
                    "'tenant_field' setting instead — Tenant is a mandatory "
                    "boundary, not a composable predicate. The "
                    "'tenant_field' setting can sit alongside 'visibility'.",
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


def validate_predicate_write_safety(model):
    """Refuse to boot if a Custom predicate is registered without an
    explicit ``write_validator``.

    The bug class this protects against:

    ``Custom`` predicates default to ``validate_write → []`` (no errors,
    write allowed). When wrapped in ``Either(Owner, Custom)`` — the
    common pattern for "owner OR external grant" — the Either combinator
    passes if any child returns no errors. So a no-op ``Custom``
    silently overrides ``Owner``'s careful checks: any caller whose role
    has the model-level write permission can bypass owner enforcement
    via the Custom branch, regardless of whether the Custom predicate
    was meant to enforce writes or not.

    This is the "5-line fix" footgun documented in the security audit:
    a Custom predicate intended for read-only role grants (e.g.
    legacy_contact) can become a write hole the moment its role is
    granted any write permission.

    Detection is fully static: walk every model's predicate stack
    (recursing into Either), find any ``Custom`` whose
    ``write_validator`` is ``None``, and refuse to start.

    Resolution (raised in the error message):

      * Pass ``write_validator=lambda d, i, r: []`` if the predicate is
        intentionally read-only (writes pass through this branch).
      * Pass ``write_validator=my_check`` to enforce writes for real.
      * Set ``TURBODRF_ALLOW_UNSAFE_CUSTOM_WRITE = True`` to bypass the
        gate (logs a warning; for migrations only).
    """
    from django.conf import settings
    from django.core.exceptions import ImproperlyConfigured

    allow_unsafe = getattr(settings, "TURBODRF_ALLOW_UNSAFE_CUSTOM_WRITE", False)
    predicates = get_predicates(model)
    if not predicates:
        return

    offending = list(_walk_unsafe_custom(predicates))
    if not offending:
        return

    descriptions = "\n".join(f"  • {pred_repr}" for pred_repr in offending)
    message = (
        f"{model.__name__} has Custom predicate(s) without an explicit "
        f"write_validator:\n{descriptions}\n\n"
        f"By default, Custom.validate_write returns [] (no errors), "
        f"meaning writes pass through. Inside Either(Owner, Custom) "
        f"this silently overrides Owner's enforcement — any caller with "
        f"the model-level write permission can bypass owner checks via "
        f"the Custom branch.\n\n"
        f"Fix one of:\n"
        f"  • Pass write_validator=lambda d, i, r: [] if the predicate "
        f"is intentionally read-only and writes should pass through.\n"
        f"  • Pass write_validator=my_check to enforce writes — return "
        f"a list of error strings to block, [] to allow.\n"
        f"  • Set TURBODRF_ALLOW_UNSAFE_CUSTOM_WRITE=True to bypass "
        f"this gate (NOT recommended; logs a warning)."
    )

    if allow_unsafe:
        import logging

        logging.getLogger(__name__).warning(
            "TURBODRF_ALLOW_UNSAFE_CUSTOM_WRITE=True — bypassing Custom "
            "predicate write-safety gate. %s",
            message,
        )
        return

    raise ImproperlyConfigured(message)


def _walk_unsafe_custom(predicates, path=""):
    """Yield a human description of every Custom-without-write_validator
    in a predicate stack. Recurses into Either."""
    for pred in predicates:
        if isinstance(pred, Either):
            yield from _walk_unsafe_custom(
                pred.predicates, path=f"{path}Either(...) → "
            )
        elif isinstance(pred, Custom) and pred.write_validator is None:
            qf_name = getattr(pred.q_func, "__name__", repr(pred.q_func))
            yield f"{path}Custom(q_func={qf_name})"


_MODEL_ACTIONS = {"read", "create", "update", "delete"}
_FIELD_ACTIONS = {"read", "write"}


def validate_permission_strings():
    """Refuse to boot if ``TURBODRF_ROLES`` contains a permission string
    that doesn't resolve to a real model + field + action.

    The footgun this protects against:

    Permission strings are parsed as ``"app.model.action"`` or
    ``"app.model.field.action"``. A typo at any segment — wrong app
    label, wrong model name, wrong field name, wrong action — silently
    grants nothing. The role appears configured but doesn't actually
    have the permission. Bugs of this shape look like "this user
    suddenly can't see X" with no error to point at.

    Detection is fully static: walk every permission string in
    ``TURBODRF_ROLES``, parse it, and verify each segment against the
    Django app registry and the model's field list.

    Resolution (raised in the error message):

      * Fix the typo in ``settings.py``.
      * If the permission references a model loaded at runtime (plugins,
        dynamic apps), set
        ``TURBODRF_ALLOW_UNKNOWN_PERMISSIONS = True`` to skip the check
        for permissions whose models aren't yet registered.

    Note: only permissions for models defined under the current Django
    ``apps`` registry are validated. Permissions whose ``app.model``
    isn't loaded are skipped silently when
    ``TURBODRF_ALLOW_UNKNOWN_PERMISSIONS`` is set, otherwise they raise.
    """
    from django.apps import apps
    from django.conf import settings as dj_settings
    from django.core.exceptions import ImproperlyConfigured

    allow_unknown = getattr(dj_settings, "TURBODRF_ALLOW_UNKNOWN_PERMISSIONS", False)
    roles = getattr(dj_settings, "TURBODRF_ROLES", None)
    if not roles:
        return

    errors = []

    def _check(perm, role):
        parts = perm.split(".")
        if len(parts) not in (3, 4):
            errors.append(
                f"role={role!r} perm={perm!r}: expected 'app.model.action' or "
                f"'app.model.field.action' (3 or 4 dot-separated segments)"
            )
            return
        app_label, model_name = parts[0], parts[1]
        try:
            model = apps.get_model(app_label, model_name)
        except LookupError:
            if allow_unknown:
                return
            errors.append(
                f"role={role!r} perm={perm!r}: model {app_label}.{model_name} "
                f"is not registered in INSTALLED_APPS"
            )
            return

        if len(parts) == 3:
            action = parts[2]
            if action not in _MODEL_ACTIONS:
                errors.append(
                    f"role={role!r} perm={perm!r}: action {action!r} is not "
                    f"a valid model-level action {sorted(_MODEL_ACTIONS)}"
                )
            return

        # 4 parts: app.model.field.action
        field_name, action = parts[2], parts[3]
        if action not in _FIELD_ACTIONS:
            errors.append(
                f"role={role!r} perm={perm!r}: action {action!r} is not "
                f"a valid field-level action {sorted(_FIELD_ACTIONS)}"
            )
            return
        try:
            model._meta.get_field(field_name)
        except Exception:
            # Field-level perms can target related-field paths via __,
            # but TURBODRF_ROLES uses dotted paths. We accept "field" as
            # a single name — anything else is a typo. List close
            # matches via difflib for the error message.
            import difflib

            field_names = [f.name for f in model._meta.get_fields()]
            close = difflib.get_close_matches(field_name, field_names, n=3, cutoff=0.6)
            hint = f" (close matches: {close})" if close else ""
            errors.append(
                f"role={role!r} perm={perm!r}: field {field_name!r} not "
                f"found on {app_label}.{model_name}{hint}"
            )

    for role, perms in roles.items():
        if not isinstance(perms, (list, tuple, set)):
            errors.append(
                f"role={role!r}: permissions must be a list/tuple/set, "
                f"got {type(perms).__name__}"
            )
            continue
        for perm in perms:
            if not isinstance(perm, str):
                errors.append(
                    f"role={role!r}: each permission must be a string, "
                    f"got {type(perm).__name__} ({perm!r})"
                )
                continue
            _check(perm, role)

    if not errors:
        return

    message = (
        f"TURBODRF_ROLES contains {len(errors)} invalid permission string(s).\n"
        f"Each permission must be 'app.model.action' or 'app.model.field.action' "
        f"and resolve to a real registered model.\n\n"
        + "\n".join(f"  • {e}" for e in errors)
        + "\n\nFix the typos in settings.py, or set "
        + "TURBODRF_ALLOW_UNKNOWN_PERMISSIONS=True to skip checks for "
        + "models that aren't yet registered (plugin systems, lazy apps)."
    )
    raise ImproperlyConfigured(message)
