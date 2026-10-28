"""
Predicate-based row-level access control for TurboDRF.

Predicates declare visibility rules per model. They:
- Produce Django Q objects for read filtering (get_queryset / get_object)
- Auto-fill mandatory fields on create (tenant FK)
- Validate that writes don't violate the predicate

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

from .scope import is_platform, platform_reachable


def get_user_tenant(user):
    """Resolve the user's tenant value (FK object or PK).

    Reads TURBODRF_TENANT_USER_FIELD from settings. None on missing/unset, and
    always None on the platform scope: a staff member's own tenant never
    scopes or fills in anything there.
    """
    if is_platform():
        return None
    return user_tenant_value(user)


def user_tenant_value(user):
    """``user``'s TURBODRF_TENANT_USER_FIELD value as a model instance or an
    integer pk, else None, whatever the scope."""
    tenant_field = getattr(settings, "TURBODRF_TENANT_USER_FIELD", None)
    if not tenant_field:
        return None
    tenant = getattr(user, tenant_field, None)
    # Coerce to (model | int | None). Anything else (string, dict, bound
    # method, related manager) would either crash Django's ORM at
    # SQL-compile time or — worse — silently match unrelated rows. Falling
    # back to None fail-closes downstream filters.
    # An unsaved instance (pk None) can't scope a query (Django 5.2 raises), and
    # a bool is an int to Python but never a tenant key.
    if tenant is None or isinstance(tenant, bool):
        return None
    if hasattr(tenant, "pk"):
        return tenant if tenant.pk is not None else None
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


def _role_set(roles, what):
    """``roles`` as a set of role names. A bare string is one role, not the set
    of its characters (``bypass='admin'`` would have bypassed for roles 'a',
    'd', ...)."""
    if roles is None:
        return set()
    if isinstance(roles, str):
        return {roles}
    try:
        return set(roles)
    except TypeError:
        raise ImproperlyConfigured(
            f"{what} must be a role name or a list of role names; got "
            f"{type(roles).__name__}."
        )


def _current_pk(instance, field_name):
    """The pk an owner FK of ``instance`` holds now (None if unset), read from
    the column so the related user isn't loaded."""
    return getattr(instance, instance._meta.get_field(field_name).attname, None)


class Predicate:
    """Base class for visibility predicates.

    Override:
        q(request, user_roles)             → Q for read filter
        auto_fill(validated_data, request) → dict (mutated) for create
        validate_write(validated_data, instance, request) → list[str] errors
    """

    mandatory = False

    def q(self, request, user_roles):
        return Q()

    def auto_fill(self, validated_data, request):
        return validated_data

    def validate_write(self, validated_data, instance, request):
        return []


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


class Owner(Predicate):
    """Within-tenant ownership with role bypass.

    `field` may be a single column or list of columns (multi-owner case — any
    matching column grants visibility). Bypass roles see all rows in tenant.

    Writes (non-bypass callers): an owner column can be set only to the caller,
    can't be cleared, and can't be changed while it holds another user — so a
    co-owner can't take the row over. A ``__`` path can't be checked on the
    submitted values, so it's checked on the saved row: the caller must still
    see it, or the write is rolled back.
    """

    def __init__(self, field, bypass=None):
        if isinstance(field, str):
            self.fields = [field]
        else:
            self.fields = list(field)
            if not self.fields:
                raise ImproperlyConfigured("Owner requires at least one field.")
        self.bypass = _role_set(bypass, "Owner bypass")
        self.verify_after_write = any("__" in f for f in self.fields)

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
        provided = [f for f in self.fields if "__" not in f and f in validated_data]
        if not provided:
            return []
        user = _authed_user(request)
        if user is None:
            return [f"Cannot set {f}: no authenticated user." for f in provided]
        from .backends import get_user_roles

        if set(get_user_roles(user)) & self.bypass:
            return []
        errors = []
        for f in provided:
            value_pk = getattr(validated_data[f], "pk", validated_data[f])
            current_pk = _current_pk(instance, f) if instance is not None else None
            if current_pk is not None and current_pk != user.pk:
                errors.append(
                    f"Cannot change {f}: the row is assigned to another user."
                )
            elif value_pk is None:
                errors.append(f"Cannot clear {f}.")
            elif value_pk != user.pk:
                errors.append(f"Cannot set {f} to a different user.")
        return errors


class Either(Predicate):
    """OR of child predicates.

    Read filter: OR of children's Q. A child that places no restriction (an
    empty ``Q()``, e.g. an Owner bypass role) makes the whole Either
    unrestricted — ``Q() | Q(x)`` would otherwise collapse to ``Q(x)``.
    Write validation: EVERY child's check runs and any objection blocks the
    write. A child with no write rule (Members, Group) raises none, so an
    Owner child's rule holds inside the Either. (Passing when one child
    passed let a Members branch override Owner.)
    Auto-fill: every child's, so an Owner child fills its column on create.
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
            if not child_q:
                return Q()
            combined = child_q if combined is None else combined | child_q
        return combined if combined is not None else Q()

    def auto_fill(self, validated_data, request):
        for pred in self.predicates:
            validated_data = pred.auto_fill(validated_data, request)
        return validated_data

    def validate_write(self, validated_data, instance, request):
        errors = []
        for pred in self.predicates:
            errors.extend(pred.validate_write(validated_data, instance, request))
        return list(dict.fromkeys(errors))


class Custom(Predicate):
    """Escape hatch for arbitrary within-tenant predicates.

    The Tenant boundary is applied separately as a setting and is NOT
    bypassable — so even if `q_func` returns Q() (no within-tenant
    restriction), tenant isolation still holds. The previous
    `unrestricted_ok` flag was removed because it solved a problem (Tenant
    inside the predicate algebra) that no longer exists in this design.

    A warning is logged whenever a Custom predicate returns an empty Q, to
    surface accidental Q() returns; set TURBODRF_LOG_UNRESTRICTED_CUSTOM=False
    to silence it.
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
# Advanced predicates (docs/tenancy.md, "Advanced predicates").
# Use these when the sugar form + Owner/Either/Custom doesn't fit.
# ---------------------------------------------------------------------------


class Members(Predicate):
    """Advanced: user must be in the row's M2M-to-User collection.

    Scopes reads, and so updates and deletes (only visible rows can be
    fetched). A create is not membership-checked: the new row has no members
    until they're set. Use ``Owner`` (or ``Custom`` with explicit write hooks)
    when creates must be restricted.
    """

    def __init__(self, m2m_field):
        self.m2m_field = m2m_field

    def q(self, request, user_roles):
        user = _authed_user(request)
        if user is None:
            return _no_match_q()
        return Q(**{self.m2m_field: user})


class Group(Predicate):
    """Advanced: user must belong to the group/team that owns the row.

    ``field`` is the FK on the row to the group/team.
    ``user_via`` is the lookup from the group/team to its users, as a query
    would spell it: an M2M field name (``members``) or a reverse relation's
    query name (``user`` for ``User.groups``, not the ``user_set``
    accessor). The router checks ``<field>__<user_via>`` reaches the user
    model at startup.

    Scopes reads, and so updates and deletes. A create (or an update that
    moves the row to another group) is not membership-checked; use ``Custom``
    with a ``write_validator`` when it must be.
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
    """Advanced: rows matching ``when`` are visible only to users with a
    required role.

    Writes are checked after the save, inside the same transaction: a caller
    without a required role can't create or update a row so that it matches
    ``when`` (the write is rolled back with a 400).
    """

    verify_after_write = True

    def __init__(self, when, require_roles):
        if not isinstance(when, Q):
            raise ImproperlyConfigured("Conditional `when` must be a Q object.")
        self.when = when
        self.require_roles = _role_set(require_roles, "Conditional require_roles")

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
      - 'owner_field': str|list     → one Owner predicate (a list → over
        every column)
      - 'bypass_owner_roles': list  → applied to Owner predicates
      - 'visibility': [Predicate..] → power form. Tenant() inside is
        EXTRACTED to the tenant_field setting (with deprecation warning).
        Tenant() inside an Either raises — would let OR-composition
        escape the tenant boundary.
    """
    import warnings

    if not isinstance(config, dict):
        raise ImproperlyConfigured(
            f"turbodrf() must return a dict; got {type(config).__name__}"
        )

    tenancy = config.get("tenancy")
    if tenancy is not None:
        if tenancy != "shared":
            raise ImproperlyConfigured(
                f"'tenancy' must be 'shared'; got {tenancy!r}. Scope tenant data "
                f"with 'tenant_field' (or 'visibility')."
            )
        scoped = [
            k for k in ("tenant_field", "owner_field", "visibility") if config.get(k)
        ]
        if scoped:
            raise ImproperlyConfigured(
                f"'tenancy': 'shared' can't be combined with {scoped}: a shared "
                f"model has no row scoping. Drop 'tenancy' to scope the model."
            )
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
        if not visibility and config.get("tenant_field") is None:
            raise ImproperlyConfigured(
                "'visibility' is empty, which scopes nothing. Declare at least "
                "one predicate, a 'tenant_field', or 'tenancy': 'shared'."
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
            # One Owner over every column: reads match ANY column, and a write
            # must not set ANY of them to another user. Either(Owner, Owner)
            # passed a write whenever one column was simply left out.
            if owner_field:
                predicates.append(Owner(list(owner_field), bypass=bypass))
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
    """True if config declares how its rows are scoped across tenants: a
    ``tenant_field``, ``tenancy: 'shared'``, or a ``visibility`` list that
    restricts every caller to their own rows.

    ``owner_field`` alone doesn't count: it scopes rows within a tenant, and
    its bypass roles would see every tenant's rows. Nor does a ``visibility``
    list (without a ``tenant_field``) whose predicates can all lift the
    restriction — an ``Owner`` with bypass roles, a ``Conditional``, or an
    ``Either`` with such a child — for the same reason. Any other ``tenancy``
    value is a typo, not a declaration.
    """
    if not isinstance(config, dict):
        return False
    if config.get("tenancy") == "shared" or config.get("tenant_field") is not None:
        return True
    visibility = config.get("visibility")
    if not isinstance(visibility, (list, tuple)):
        return False
    # The list is AND'd: one predicate that always restricts is enough.
    return any(_always_restricts(p) for p in visibility)


def _always_restricts(predicate):
    """True if ``predicate`` restricts every caller to rows tied to them,
    whatever their roles. ``Custom`` (and unknown subclasses) count: their
    rule is the developer's. A top-level ``Tenant`` becomes the tenant
    boundary."""
    if isinstance(predicate, Owner):
        return not predicate.bypass
    if isinstance(predicate, Conditional):
        return False
    if isinstance(predicate, Either):
        return all(_always_restricts(p) for p in predicate.predicates)
    return isinstance(predicate, Predicate)


# ---------------------------------------------------------------------------
# Module-level registries (populated by router at startup)
# ---------------------------------------------------------------------------

_model_predicates = {}
_model_tenant_fields = {}


class _PlatformUnreachable(Predicate):
    """The row rule of a model the platform scope can't reach (a TurboDRF
    model without a platform block, see scope.platform_reachable): no row."""

    def q(self, request, user_roles):
        return _no_match_q()


def register_predicates(model, predicates):
    _model_predicates[model] = list(predicates)


def get_predicates(model):
    """The within-tenant predicates of ``model`` on the current scope. On the
    platform scope a reachable model has none, and any other model a rule
    that matches no row."""
    if is_platform():
        return [] if platform_reachable(model) else [_PlatformUnreachable()]
    return _model_predicates.get(model, [])


def register_tenant_field(model, tenant_field):
    """Register the tenant_field setting for a model. None means 'no tenant'."""
    if tenant_field is None:
        _model_tenant_fields.pop(model, None)
    else:
        _model_tenant_fields[model] = tenant_field


def get_tenant_field(model):
    """Return the tenant_field setting for a model on the current scope, or
    None. The platform scope has no tenant boundary: always None there (see
    ``registered_tenant_field``)."""
    if is_platform():
        return None
    return _model_tenant_fields.get(model)


def registered_tenant_field(model):
    """The model's registered tenant_field, whatever the scope: the platform
    scope's write checks use it to keep a row and the rows it points at in
    one tenant."""
    return _model_tenant_fields.get(model)


def clear_predicates():
    """Used in tests to reset between runs."""
    _model_predicates.clear()
    _model_tenant_fields.clear()


def validate_predicate_write_safety(model):
    """Refuse to boot if a Custom predicate is registered without an
    explicit ``write_validator``.

    A ``Custom``'s ``q_func`` filters reads only. Without a
    ``write_validator`` a write isn't checked against it: a caller with the
    model-level write permission can create rows, or move visible rows,
    outside the rule (as with ``Members`` / ``Group``). Inside
    ``Either(Owner, Custom)`` this no longer weakens ``Owner`` — ``Either``
    runs every child's check — but the Custom's own rule still isn't applied
    to writes, and the developer should say so.

    Detection is fully static: walk every model's predicate stack
    (recursing into Either), find any ``Custom`` whose
    ``write_validator`` is ``None``, and refuse to start.

    Resolution (raised in the error message):

      * Pass ``write_validator=my_check`` to enforce writes for real.
      * Pass ``write_validator=lambda d, i, r: []`` to declare that this
        rule doesn't restrict writes (other predicates still apply).
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
        f"A Custom's q_func filters reads only. Without a write_validator, "
        f"writes aren't checked against it: any caller with the model-level "
        f"write permission can create rows, or move rows, outside the "
        f"rule.\n\n"
        f"Fix one of:\n"
        f"  • Pass write_validator=my_check to enforce the rule on writes — "
        f"return a list of error strings to block, [] to allow. Inside "
        f"Either(...) every child's check runs, and any error blocks the "
        f"write.\n"
        f"  • Pass write_validator=lambda d, i, r: [] if this rule shouldn't "
        f"restrict writes (other predicates, such as Owner, still apply).\n"
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


def validate_permission_strings(roles=None, setting="TURBODRF_ROLES"):
    """Refuse to boot if ``TURBODRF_ROLES`` contains a permission string
    that doesn't resolve to a real model + field + action. ``roles`` and
    ``setting`` check another role map the same way
    (``TURBODRF_PLATFORM_ROLES``).

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
    if roles is None:
        roles = getattr(dj_settings, setting, None)
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
        # get_model() ignores case, but permission checks compare exactly: a
        # string the gate accepts must be one the snapshot can match.
        if (app_label, model_name) != (model._meta.app_label, model._meta.model_name):
            errors.append(
                f"role={role!r} perm={perm!r}: write the model as "
                f"{model._meta.app_label}.{model._meta.model_name} (permission "
                f"strings are matched exactly)"
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
        # Field rules apply to concrete and M2M fields, by name: get_field()
        # also accepts an FK's attname ("related_id"), which no rule matches.
        rule_fields = {f.name for f in model._meta.fields} | {
            f.name for f in model._meta.many_to_many
        }
        if field_name not in rule_fields:
            # Field-level perms can target related-field paths via __,
            # but TURBODRF_ROLES uses dotted paths. We accept "field" as
            # a single name — anything else is a typo. List close
            # matches via difflib for the error message.
            import difflib

            field_names = sorted(rule_fields)
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
        f"{setting} contains {len(errors)} invalid permission string(s).\n"
        f"Each permission must be 'app.model.action' or 'app.model.field.action' "
        f"and resolve to a real registered model.\n\n"
        + "\n".join(f"  • {e}" for e in errors)
        + "\n\nFix the typos in settings.py, or set "
        + "TURBODRF_ALLOW_UNKNOWN_PERMISSIONS=True to skip checks for "
        + "models that aren't yet registered (plugin systems, lazy apps)."
    )
    raise ImproperlyConfigured(message)
