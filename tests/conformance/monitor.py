"""
Independent runtime conformance monitor for TurboDRF authorization.

This is an OUTSIDE-IN checker. Given an API response and the authenticated
caller, it recomputes the *authorized view* from raw database facts and the
declared ``TURBODRF_ROLES`` config — WITHOUT invoking the enforcement code path
under test (no ``get_queryset``, no predicate ``.q()``, no ``PermissionSnapshot``
applied to the response). Any divergence is a conformance violation: the real
Python returned something its own declared policy should forbid.

This checks that the *running Python* enforces the declared authorization
policy on whatever requests are exercised — evidence bounded by coverage.

Independence rules (why this isn't checking the code against itself):
  * a row's tenant is resolved by walking its FK chain on a freshly-fetched
    instance via plain ``getattr`` — a raw DB fact, not the scoped queryset.
  * a field's readability is recomputed directly from ``TURBODRF_ROLES`` here,
    not read off the ``PermissionSnapshot`` the response was built from.
"""

from django.conf import settings


class ConformanceViolation(AssertionError):
    """Raised when the real API response violates the declared policy."""


def resolve_tenant_pk(instance, tenant_field):
    """Follow a (possibly ``__``-chained) tenant field to its pk by raw
    attribute access. Independent of the enforcement layer's predicate Q."""
    obj = instance
    for seg in tenant_field.split("__"):
        if obj is None:
            return None
        obj = getattr(obj, seg)
    return getattr(obj, "pk", obj)


class ConformanceMonitor:
    def __init__(self, roles_config=None):
        self.roles = (
            roles_config
            if roles_config is not None
            else getattr(settings, "TURBODRF_ROLES", {})
        )

    # ---- tenant containment ------------------------------------------------

    def check_tenant_containment(self, model, returned_pks, caller_tenant_pk,
                                 tenant_field, context=""):
        """Every returned row must belong to the caller's tenant.

        Looks each pk up via the *unscoped* default manager and resolves its
        real tenant from the DB. A mismatch is a cross-tenant leak.
        """
        for pk in returned_pks:
            inst = model.objects.get(pk=pk)
            row_tenant = resolve_tenant_pk(inst, tenant_field)
            if row_tenant != caller_tenant_pk:
                raise ConformanceViolation(
                    f"[{context}] {model.__name__} pk={pk} is in tenant "
                    f"{row_tenant!r} but caller tenant is {caller_tenant_pk!r} "
                    f"— CROSS-TENANT LEAK"
                )

    # ---- field exposure (property P4 — field half) --------------------------

    def readable_field_bases(self, app_label, model_name, user_roles):
        """Set of base field names readable by these roles, parsed straight
        from ``TURBODRF_ROLES``. Returns (bases, field_rules_present)."""
        prefix = f"{app_label}.{model_name}."
        bases = set()
        any_rule = False
        for role in user_roles:
            for perm in self.roles.get(role, []):
                if perm.startswith(prefix) and perm.endswith(".read"):
                    middle = perm[len(prefix):-len(".read")]
                    if middle and "." not in middle:  # field-level, not action
                        bases.add(middle)
                        any_rule = True
        return bases, any_rule

    @staticmethod
    def _key_allowed(key, bases):
        """Is a response key readable given the readable base field names?

        Handles TurboDRF's nested-field naming: a configured path ``a__b`` is
        emitted as the JSON key ``a_b`` (and sometimes kept as ``a__b``). A key
        is allowed iff it is, or descends from, a readable base — so the gate is
        keyed on the *base relation's* read permission, not the leaf.
        """
        if key in ("id", "pk"):
            return True
        if key.split("__")[0] in bases:          # a__b form
            return True
        for b in bases:                           # a_b / bare-base form
            if key == b or key.startswith(b + "_"):
                return True
        return False

    def check_field_exposure(self, app_label, model_name, row_keys, user_roles,
                             context=""):
        """Every emitted field must be (or descend from) a readable base."""
        bases, any_rule = self.readable_field_bases(app_label, model_name,
                                                    user_roles)
        if not any_rule:
            return  # no field-level rules declared → field gating off by design
        for key in row_keys:
            if not self._key_allowed(key, bases):
                raise ConformanceViolation(
                    f"[{context}] field {key!r} on {model_name} exposed to roles "
                    f"{user_roles} that lack read permission for it"
                )
