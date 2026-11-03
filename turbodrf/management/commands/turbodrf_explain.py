"""
Explain what a user may do on a TurboDRF model, and why:

    python manage.py turbodrf_explain --user alice --model shop.Book [--pk 42]

The answers come from the code a request runs, for a synthetic request
carrying the user: the permission snapshot (built fresh, not from the cache),
the serializer's field check, and the tenant boundary and predicates applied
through views.Authorization. It only reads.
"""

from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db.models import AutoField
from django.test import RequestFactory
from rest_framework.request import Request

from turbodrf import backends, predicates, validation
from turbodrf.mixins import TurboDRFMixin, _model_config, _resolve_configured_fields
from turbodrf.permissions import permissions_bypassed
from turbodrf.scope import SCOPES, TENANT, is_platform, use_scope
from turbodrf.views import Authorization

from .turbodrf_check import describe_predicate

# Each action's Django model permission (DefaultDjangoPermission) and HTTP
# methods (TurboDRFPermission).
ACTIONS = {
    "read": ("view", {"get"}),
    "create": ("add", {"post"}),
    "update": ("change", {"put", "patch"}),
    "delete": ("delete", {"delete"}),
}
DISABLED = "TURBODRF_DISABLE_PERMISSIONS"


def turbodrf_models():
    """Every TurboDRF model: the discovery turbodrf_check uses."""
    return [m for m in apps.get_models() if issubclass(m, TurboDRFMixin)]


def _first(manager, **lookup):
    """The first row matching ``lookup``; None for a value of the wrong type."""
    try:
        return manager.filter(**lookup).first()
    except (ValueError, TypeError, ValidationError):
        return None


class Command(BaseCommand):
    help = "Explain what a user may read and write on a TurboDRF model, and why"

    def add_arguments(self, parser):
        parser.add_argument("--user", required=True, help="Username or pk")
        parser.add_argument("--model", required=True, help="app_label.Model or Model")
        parser.add_argument("--pk", help="Explain whether this row is visible")
        parser.add_argument("--scope", choices=SCOPES, default=TENANT)

    def handle(self, *args, **options):
        users, name = get_user_model()._default_manager, options["user"]
        self.user = _first(users, **{users.model.USERNAME_FIELD: name})
        self.user = self.user or _first(users, pk=name)
        if self.user is None:
            raise CommandError(f"No user with username or pk {name!r}.")
        self.model = model = self._find_model(options["model"])
        pk = options["pk"]
        if pk is not None and _first(model._base_manager, pk=pk) is None:
            raise CommandError(f"No {model._meta.label} row with pk {pk!r}.")
        self.label = f"{model._meta.app_label}.{model._meta.model_name}"
        self.request = Request(RequestFactory().get("/"))
        self.request.user = self.user
        self.write = self.stdout.write
        with use_scope(options["scope"]):
            self.snapshot = self._snapshot(self.user)
            self.bypass = permissions_bypassed() and (
                DISABLED
                if getattr(settings, DISABLED, False)
                else "TURBODRF_USE_DEFAULT_PERMISSIONS"
            )
            self._caller()
            self._model()
            self._actions()
            self._fields()
            if pk is not None:
                self._row(pk)

    def _find_model(self, value):
        app_label, _, name = value.rpartition(".")
        found = [
            m
            for m in turbodrf_models()
            if m._meta.model_name == name.lower()
            and app_label in ("", m._meta.app_label)
        ]
        if not found:
            raise CommandError(f"No TurboDRF model {value!r}.")
        if len(found) > 1:
            labels = ", ".join(sorted(m._meta.label for m in found))
            raise CommandError(f"{value!r} is ambiguous ({labels}): add the app.")
        return found[0]

    def _snapshot(self, user):
        return backends.build_permission_snapshot(user, self.model, use_cache=False)

    def _caller(self):
        user, write = self.user, self.write
        roles = ", ".join(backends.get_user_roles(user))
        write(f"Caller: {user} (pk {user.pk})")
        if self.bypass:
            write(f"  Permissions are bypassed ({self.bypass}): roles and field rules")
            write("  don't apply; the sensitive deny-list and row scoping still do.")
        kind = "Platform roles" if is_platform() else "Roles"
        write(f"  {kind}: {roles or 'NONE: every TurboDRF request is a 403'}")
        field = getattr(settings, "TURBODRF_TENANT_USER_FIELD", None)
        tenant = predicates.get_user_tenant(user)
        if is_platform():
            write("  Tenant: none applies on the platform scope")
        elif tenant is not None:
            pk = getattr(tenant, "pk", tenant)
            write(f"  Tenant: {tenant} (pk {pk}, from user.{field})")
        elif field:
            value = getattr(user, field, None)
            write(f"  Tenant: NONE (user.{field} is {value!r}): sees no tenant rows")
        else:
            write("  Tenant: NONE (TURBODRF_TENANT_USER_FIELD isn't set)")

    def _model(self):
        model, write = self.model, self.write
        self.config = config = _model_config(model)
        self.routed = config.get("enabled", True)
        endpoint = config.get("endpoint", f"{model._meta.model_name}s")
        unrouted = "no platform block" if is_platform() else "'enabled': False"
        write(f"\nModel: {model._meta.label}")
        write(f"  Route: {f'{endpoint}/' if self.routed else f'none ({unrouted})'}")
        tenant_field = predicates.get_tenant_field(model)
        rows = [f"tenant_field {tenant_field!r}"] if tenant_field else []
        rows += [describe_predicate(p) for p in predicates.get_predicates(model)]
        if is_platform():
            rows = ["every tenant's (the platform scope has no row scoping)"]
        elif config.get("tenancy") == "shared":
            rows = ["shared (no row scoping)"]
        write(f"  Rows: {'; '.join(rows) or 'no row scoping declared'}")
        if not is_platform():
            write(f"  public_access: {bool(config.get('public_access'))}")

    def _actions(self):
        self.write("\nActions")
        roles, meta = backends.get_user_roles(self.user), self.model._meta
        served = {m.lower() for m in self.config.get("http_methods") or ()}
        served = {"get"} if self.config.get("read_only") else served
        self.allowed = {}
        for action, (django_action, methods) in ACTIONS.items():
            if self.bypass == DISABLED:
                ok, why = True, DISABLED
            elif self.bypass:  # DefaultDjangoPermission: Django's model permissions
                why = f"{meta.app_label}.{django_action}_{meta.model_name}"
                ok = self.user.has_perm(why)
            else:
                ok = self.snapshot.can_perform_action(action)
                by = ", ".join(r for r in roles if self._role_grants(r, action))
                why = f"granted by {by}" if by else "none of the caller's roles has it"
                why = f"{self.label}.{action}: {why}"
            if ok and served and not methods & served:  # read_only / http_methods
                ok, why = False, f"{why}, but the route doesn't serve it (405)"
            self.allowed[action] = ok
            self.write(f"  {action:<7} {'allowed' if ok else 'refused'}  ({why})")

    def _role_grants(self, role, action):
        role_only = backends.RoleOnlyIdentity([role])
        return self._snapshot(role_only).can_perform_action(action)

    def _fields(self):
        self.write("\nFields")
        listed = _resolve_configured_fields(self.model, "list")
        detail = _resolve_configured_fields(self.model, "detail")
        for name in dict.fromkeys(listed + detail):
            views = [v for v, f in (("list", listed), ("detail", detail)) if name in f]
            path = validation._normalised_path(self.model, name)
            readable, read_why = self._readable(path)
            writable, write_why = self._writable(path) if readable else (False, None)
            write_why = write_why or "not readable, so not in the serializer"
            self.write(f"  {name} [{', '.join(views)}]")
            self.write(f"    read:  {'yes' if readable else 'no'} ({read_why})")
            self.write(f"    write: {'yes' if writable else 'no'} ({write_why})")

    def _field(self, path):
        try:
            return self.model._meta.get_field(path)
        except FieldDoesNotExist:
            return None  # a property, or a nested path

    def _readable(self, path):
        model, snapshot = self.model, self.snapshot
        if validation.is_field_path_sensitive(path):
            return False, "TURBODRF_SENSITIVE_FIELDS deny-list: never served"
        if self.bypass:
            return True, f"permissions bypassed ({self.bypass})"
        parts = path.split("__")
        for i in range(1, len(parts) + 1):  # the serializer's check, hop by hop
            hop = "__".join(parts[:i])
            ok = validation.check_nested_field_permissions(
                model, hop, self.user, use_cache=False
            )
            if len(parts) > 1 and not ok:
                return False, f"nested path: {hop!r} isn't readable"
        if len(parts) > 1:
            return True, "nested path: every hop readable"
        granted = "granted" if snapshot.can_read_field(path) else "not granted"
        rule = f"{self.label}.{path}.read, {granted}"
        if path in backends._model_restricted_fields(model):
            return ok, f"in the model's sensitive_fields: needs {rule}"
        if snapshot.has_read_rule(path):
            return ok, f"field rule {rule}"
        if not snapshot.can_perform_action("read"):
            return ok, f"no model-level read: {self.label}.read"
        if not ok:
            return ok, "reverse relation: the related model's field isn't readable"
        if self._field(path) is None:  # run in the caller's property sandbox
            return ok, "model-level read; left out if it reads a hidden column"
        return ok, "model-level read"

    def _writable(self, path):
        snapshot, field = self.snapshot, self._field(path)
        if "__" in path:
            return False, "nested path: read-only"
        if field is None:
            return False, "property: read-only"
        if not (field.concrete or field.many_to_many):
            return False, "reverse relation: read-only"
        if isinstance(field, AutoField) or not field.editable:
            return False, "read-only column"
        if self.bypass:
            return True, f"permissions bypassed ({self.bypass})"
        if snapshot.has_write_rule(path):
            ok = snapshot.can_write_field(path)
            granted = "granted" if ok else "not granted"
            return ok, f"field rule {self.label}.{path}.write, {granted}"
        by = [a for a in ("create", "update") if snapshot.can_perform_action(a)]
        if not by:
            return False, "no model-level create or update"
        return True, f"model-level {' and '.join(by)}"

    def _row(self, pk):
        model, request, write = self.model, self.request, self.write
        rows = model._default_manager.all()

        def passes(tenant_q, predicate_q):
            scoped = Authorization(tenant_q, predicate_q).scope(rows)
            return scoped.filter(pk=pk).exists()

        manager = f"{model.__name__}.{model._default_manager.name}"
        in_manager = rows.filter(pk=pk).exists()
        # Each layer a request passes, in order: (passes, why it hides the row).
        layers = [
            (self.routed, "the model has no route on this scope"),
            (self.allowed["read"], "the caller may not read the model (403)"),
            (in_manager, f"the default manager ({manager}) excludes it"),
        ]
        write(f"\nRow {pk}: exists (base manager)")
        write(
            f"  Default manager ({manager}): {'includes' if in_manager else 'EXCLUDES'} it"
        )
        tenant_field = predicates.get_tenant_field(model)
        tenant_q = predicates.tenant_q(tenant_field, request)
        if tenant_field:
            ok = passes(tenant_q, None)
            row = model._base_manager.filter(pk=pk).values_list(tenant_field, flat=True)
            tenant = predicates.get_user_tenant(self.user)
            write(
                f"  Tenant boundary ({tenant_field}): {'passes' if ok else 'EXCLUDES it'}"
                f" (row's tenant {row.first()}, caller's {getattr(tenant, 'pk', tenant)})"
            )
            layers.append((ok, f"the tenant boundary ({tenant_field}) excludes it"))
        elif is_platform():
            write("  Tenant boundary: none on the platform scope")
        rules = predicates.get_predicates(model)
        for rule in rules:
            ok = self._predicate(rule, passes)
            layers.append((ok, f"{describe_predicate(rule)} excludes it"))
        failed = [why for ok, why in layers if not ok]
        if not failed and not passes(tenant_q, predicates.predicates_q(rules, request)):
            failed.append("the predicates together exclude it")
        if failed:
            write(f"  Verdict: NOT visible through the API: {failed[0]}")
        else:
            write("  Verdict: visible through the API")

    def _predicate(self, rule, passes, indent="  "):
        q = predicates.predicates_q([rule], self.request)
        ok = passes(None, q)
        either = isinstance(rule, predicates.Either)
        name = "Either (any child)" if either else describe_predicate(rule)
        note = "" if q else ", no restriction for the caller's roles"
        self.write(f"{indent}{name}: {'passes' if ok else 'EXCLUDES it'}{note}")
        if either:
            for child in rule.predicates:
                self._predicate(child, passes, indent + "  ")
        return ok
