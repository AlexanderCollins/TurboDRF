"""Security review (0.6.0): field-level RBAC and the write path.

Each test is a finding a review reproduced on 0.5.2, asserted fixed.
"""

from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from tests.test_app.apps import _test_user_brokerages, set_test_brokerage
from tests.test_app.models import (
    Brokerage,
    Deal,
    RbacBoard,
    RbacListOnly,
    RbacNote,
    RbacProject,
    RbacProjectItem,
    RbacTask,
    RbacUuidDoc,
    RelatedModel,
    SampleModel,
)

User = get_user_model()

_CRUD = ("read", "create", "update", "delete")
_RBAC_MODELS = (
    "rbacnote",
    "rbaccomment",
    "rbacboard",
    "rbactask",
    "rbacprojectitem",
    "rbaclistonly",
)


def _crud(model):
    return [f"test_app.{model}.{a}" for a in _CRUD]


def _rbac_roles(**extra):
    roles = {k: list(v) for k, v in settings.TURBODRF_ROLES.items()}
    for role in ("underwriter", "manager"):
        for m in _RBAC_MODELS:
            roles[role] += _crud(m)
    for role, perms in extra.items():
        roles.setdefault(role, [])
        roles[role] += perms
    return roles


RBAC_ROLES = _rbac_roles()


class _Base(TestCase):
    def setUp(self):
        import tests.urls  # noqa: F401  (router registers tenancy/predicates)

        cache.clear()
        _test_user_brokerages.clear()
        self.client = APIClient()
        self.brokerage_a = Brokerage.objects.create(name="A")
        self.brokerage_b = Brokerage.objects.create(name="B")
        self.under_a = self._user("ua", ["underwriter"], self.brokerage_a)
        self.under_a2 = self._user("ua2", ["underwriter"], self.brokerage_a)
        self.manager_a = self._user("ma", ["manager"], self.brokerage_a)
        self.under_b = self._user("ub", ["underwriter"], self.brokerage_b)
        self.deal_a = Deal.objects.create(
            title="A deal", brokerage=self.brokerage_a, assigned_broker=self.under_a
        )
        self.deal_a2 = Deal.objects.create(
            title="A2 private deal",
            brokerage=self.brokerage_a,
            assigned_broker=self.under_a2,
        )
        self.deal_b = Deal.objects.create(
            title="B secret deal", brokerage=self.brokerage_b, assigned_broker=None
        )

    def tearDown(self):
        _test_user_brokerages.clear()

    def _user(self, username, roles, brokerage):
        user = User.objects.create_user(username=username, password="x")
        user._test_roles = roles
        set_test_brokerage(user, brokerage)
        return user


# ---------------------------------------------------------------------------
# F1. Database permission mode ignores field rules defined on OTHER roles.
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_PERMISSION_MODE="database")
class TestDbModeCrossRoleFieldRules(TestCase):
    """Static mode: "If ANY role defines an explicit field rule, that field
    requires explicit permission for ALL roles". Database mode only scans the
    CALLER's roles, so a role with no field rules reads/writes everything."""

    def setUp(self):
        import tests.urls  # noqa: F401
        from turbodrf.models import RolePermission, TurboDRFRole, UserRole

        cache.clear()
        self.client = APIClient()

        def perm(role, action=None, field=None, ptype=None):
            RolePermission.objects.create(
                role=role,
                app_label="test_app",
                model_name="samplemodel",
                action=action,
                field_name=field,
                permission_type=ptype,
            )

        admin = TurboDRFRole.objects.create(name="db_admin")
        for a in ("read", "update"):
            perm(admin, action=a)
        perm(admin, field="secret_field", ptype="read")
        perm(admin, field="price", ptype="read")
        perm(admin, field="price", ptype="write")

        editor = TurboDRFRole.objects.create(name="db_editor")
        for a in ("read", "update"):
            perm(editor, action=a)

        self.editor = User.objects.create_user(username="db_editor_user")
        UserRole.objects.create(user=self.editor, role=editor)

        related = RelatedModel.objects.create(name="r")
        self.item = SampleModel.objects.create(
            title="t",
            price=Decimal("10.00"),
            related=related,
            secret_field="TOP-SECRET",
        )

    def test_role_without_field_rules_cannot_read_restricted_fields(self):
        self.client.force_authenticate(self.editor)
        resp = self.client.get(f"/api/samplemodels/{self.item.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("secret_field", resp.data, resp.data)
        self.assertNotIn("price", resp.data, resp.data)

    def test_role_without_field_write_rule_cannot_write_restricted_field(self):
        self.client.force_authenticate(self.editor)
        resp = self.client.patch(
            f"/api/samplemodels/{self.item.pk}/", {"price": "0.01"}, format="json"
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.price, Decimal("10.00"), resp.data)


# ---------------------------------------------------------------------------
# F2. Anonymous callers on public_access models bypass field-level rules.
# ---------------------------------------------------------------------------


class TestAnonymousBypassesFieldRules(TestCase):
    def setUp(self):
        import tests.urls  # noqa: F401

        cache.clear()
        self.client = APIClient()
        related = RelatedModel.objects.create(name="r")
        self.item = SampleModel.objects.create(
            title="t", price=Decimal("10.00"), related=related, secret_field="SECRET"
        )

    def test_authenticated_viewer_cannot_read_secret_field(self):
        viewer = User.objects.create_user(username="v")
        viewer._test_roles = ["viewer"]
        self.client.force_authenticate(viewer)
        resp = self.client.get(f"/api/samplemodels/{self.item.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("secret_field", resp.data)
        self.assertNotIn("price", resp.data)

    def test_anonymous_cannot_read_field_restricted_from_viewers(self):
        # No 'guest' role configured (the test settings, and the default).
        resp = self.client.get(f"/api/samplemodels/{self.item.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("secret_field", resp.data, resp.data)

    def test_anonymous_with_guest_role_lacking_model_read_gets_nothing(self):
        roles = dict(settings.TURBODRF_ROLES)
        roles["guest"] = ["test_app.category.read"]  # guest exists, not for this model
        with override_settings(TURBODRF_ROLES=roles):
            resp = self.client.get(f"/api/samplemodels/{self.item.pk}/")
        self.assertNotIn("secret_field", getattr(resp, "data", {}) or {}, resp.data)


# ---------------------------------------------------------------------------
# F3. Owner auto_fill runs on UPDATE: a bypass-role edit steals ownership.
# ---------------------------------------------------------------------------


class TestOwnerAutofillOnUpdate(_Base):
    def test_manager_patch_does_not_reassign_owner(self):
        self.client.force_authenticate(self.manager_a)
        resp = self.client.patch(
            f"/api/deals/{self.deal_a.pk}/", {"title": "renamed"}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        self.deal_a.refresh_from_db()
        self.assertEqual(self.deal_a.assigned_broker_id, self.under_a.pk)


# ---------------------------------------------------------------------------
# F4. Chained tenant_field through a non-TurboDRF model: no FK check.
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestChainedTenantFkInjection(_Base):
    def setUp(self):
        super().setUp()
        self.project_a = RbacProject.objects.create(name="pa", firm=self.brokerage_a)
        self.project_b = RbacProject.objects.create(name="pb", firm=self.brokerage_b)

    def test_create_into_other_tenant_project_rejected(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.post(
            "/api/rbacprojectitems/",
            {"name": "planted", "project": self.project_b.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(RbacProjectItem.objects.filter(name="planted").exists())

    def test_patch_moves_row_into_other_tenant_rejected(self):
        item = RbacProjectItem.objects.create(name="mine", project=self.project_a)
        self.client.force_authenticate(self.under_a)
        resp = self.client.patch(
            f"/api/rbacprojectitems/{item.pk}/",
            {"project": self.project_b.pk},
            format="json",
        )
        item.refresh_from_db()
        self.assertEqual(item.project_id, self.project_a.pk, resp.data)


# ---------------------------------------------------------------------------
# F5. Host with no tenant_field and no predicates: FK/M2M checks skipped;
#     nested FK fields then render the injected target.
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestUnscopedHostFkInjection(_Base):
    def test_fk_to_other_tenant_rejected_and_not_rendered(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.post(
            "/api/rbaccomments/",
            {"body": "x", "deal": self.deal_b.pk},
            format="json",
        )
        # Observed today: 201 and resp.data["deal_title"] == "B secret deal".
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_m2m_to_other_tenant_rejected(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.post(
            "/api/rbaccomments/",
            {"body": "x", "deal": self.deal_a.pk, "linked_deals": [self.deal_b.pk]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestPredicateOnlyHostM2MInjection(_Base):
    def test_m2m_to_other_tenant_rejected(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.post(
            "/api/rbacboards/",
            {"title": "b", "owner": self.under_a.pk, "deals": [self.deal_b.pk]},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)

    def test_fk_on_same_host_is_checked(self):
        # Contrast: FKs on a predicate-only host ARE checked (layer 3a);
        # only M2M is skipped.
        self.client.force_authenticate(self.under_a)
        resp = self.client.post(
            "/api/rbacboards/",
            {"title": "b", "owner": self.under_b.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(RbacBoard.objects.filter(title="b").exists())


# ---------------------------------------------------------------------------
# F6. DRF (non-compiled) path: nested FK fields are NOT scoped to the target's
#     predicates (docs recommend compiled: False as the safe fallback).
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestDrfPathNestedFkNotScoped(_Base):
    def setUp(self):
        super().setUp()
        # A manager (bypass role) legitimately attached a note to ua2's deal.
        self.note = RbacNote.objects.create(
            body="n", brokerage=self.brokerage_a, deal=self.deal_a2
        )

    def test_target_hidden_via_own_endpoint(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.get(f"/api/deals/{self.deal_a2.pk}/")
        self.assertEqual(resp.status_code, 404)

    def test_nested_field_does_not_render_hidden_target(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.get(f"/api/rbacnotes/{self.note.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotEqual(resp.data.get("deal_title"), "A2 private deal", resp.data)

    def test_list_does_not_render_hidden_target(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.get("/api/rbacnotes/")
        titles = [r.get("deal_title") for r in resp.data["data"]]
        self.assertNotIn("A2 private deal", titles, resp.data)


# ---------------------------------------------------------------------------
# F7. Custom actions: get_serializer() is the UNFILTERED serializer.
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=_rbac_roles(manager=["test_app.rbacnote.body.read"]))
class TestCustomActionSerializerUnfiltered(_Base):
    def setUp(self):
        super().setUp()
        self.note = RbacNote.objects.create(
            body="MANAGER-ONLY", brokerage=self.brokerage_a, deal=self.deal_a
        )

    def test_crud_detail_hides_body(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.get(f"/api/rbacnotes/{self.note.pk}/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("body", resp.data)

    def test_custom_action_hides_body(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.get(f"/api/rbacnotes/{self.note.pk}/peek/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("body", resp.data, resp.data)


# ---------------------------------------------------------------------------
# F8. Multi-column owner_field: Either() lets non-bypass users assign rows
#     to other users.
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestMultiOwnerWriteCheck(_Base):
    def test_single_owner_model_blocks_assigning_others(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.post(
            "/api/deals/",
            {"title": "x", "assigned_broker": self.under_a2.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_multi_owner_model_blocks_assigning_others(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.post(
            "/api/rbactasks/",
            {"title": "planted", "owner": self.under_a2.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertFalse(RbacTask.objects.filter(title="planted").exists())


# ---------------------------------------------------------------------------
# F9. Case-variant permission strings pass the typo gate but are ignored —
#     a field restriction written that way fails OPEN.
# ---------------------------------------------------------------------------


class TestPermissionStringCase(TestCase):
    ROLES = {
        "boss": ["test_app.samplemodel.read", "test_app.SampleModel.secret_field.read"],
        "pleb": ["test_app.samplemodel.read"],
    }

    def test_case_variant_rejected_or_honoured(self):
        from turbodrf.backends import build_permission_snapshot
        from turbodrf.predicates import validate_permission_strings

        with override_settings(TURBODRF_ROLES=self.ROLES):
            gate_raised = False
            try:
                validate_permission_strings()
            except Exception:
                gate_raised = True
            pleb = User.objects.create_user(username="pleb")
            pleb._test_roles = ["pleb"]
            snap = build_permission_snapshot(pleb, SampleModel, use_cache=False)
        # Either the gate rejects the string, or the restriction applies.
        self.assertTrue(
            gate_raised or "secret_field" not in snap.readable_fields,
            "typo gate accepted 'SampleModel' and the rule was silently ignored",
        )

    def test_fk_attname_variant_rejected_or_honoured(self):
        from turbodrf.backends import build_permission_snapshot
        from turbodrf.predicates import validate_permission_strings

        roles = {
            "boss": [
                "test_app.samplemodel.read",
                "test_app.samplemodel.related_id.read",
            ],
            "pleb": ["test_app.samplemodel.read"],
        }
        with override_settings(TURBODRF_ROLES=roles):
            gate_raised = False
            try:
                validate_permission_strings()
            except Exception:
                gate_raised = True
            pleb = User.objects.create_user(username="pleb2")
            pleb._test_roles = ["pleb"]
            snap = build_permission_snapshot(pleb, SampleModel, use_cache=False)
        self.assertTrue(
            gate_raised or "related" not in snap.readable_fields,
            "typo gate accepted 'related_id' and the rule was silently ignored",
        )


# ---------------------------------------------------------------------------
# F10. `fields` dict with only 'list' → detail/create/update use '__all__'.
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestListOnlyFieldsDict(_Base):
    def setUp(self):
        super().setUp()
        self.obj = RbacListOnly.objects.create(title="t", internal_notes="INTERNAL")

    def test_detail_does_not_expose_unlisted_column(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.get(f"/api/rbaclistonlys/{self.obj.pk}/")
        self.assertNotIn("internal_notes", resp.data, resp.data)

    def test_list_filter_is_not_an_oracle_on_unlisted_column(self):
        RbacListOnly.objects.create(title="u", internal_notes="other")
        self.client.force_authenticate(self.under_a)
        resp = self.client.get(
            "/api/rbaclistonlys/", {"internal_notes__istartswith": "INT"}
        )
        self.assertEqual(resp.data["pagination"]["total_items"], 2, resp.data)

    def test_patch_cannot_write_unlisted_column(self):
        self.client.force_authenticate(self.under_a)
        self.client.patch(
            f"/api/rbaclistonlys/{self.obj.pk}/",
            {"internal_notes": "overwritten"},
            format="json",
        )
        self.obj.refresh_from_db()
        self.assertEqual(self.obj.internal_notes, "INTERNAL")


# ---------------------------------------------------------------------------
# F12. Editable primary key is writable on update: PATCH {"id": new} clones
#      the row (an update-only role creates rows), and an existing pk anywhere
#      in the table answers "already exists" (cross-tenant existence oracle).
# ---------------------------------------------------------------------------


@override_settings(
    TURBODRF_ROLES=_rbac_roles(
        updater=["test_app.rbacuuiddoc.read", "test_app.rbacuuiddoc.update"]
    )
)
class TestEditablePkOverwrite(_Base):
    def setUp(self):
        super().setUp()
        self.updater = self._user("upd", ["updater"], self.brokerage_a)
        self.doc = RbacUuidDoc.objects.create(title="mine", brokerage=self.brokerage_a)
        self.victim = RbacUuidDoc.objects.create(
            title="theirs", brokerage=self.brokerage_b
        )

    def test_patch_pk_does_not_create_rows(self):
        import uuid as _uuid

        self.client.force_authenticate(self.updater)
        new_id = str(_uuid.uuid4())
        resp = self.client.patch(
            f"/api/rbacuuiddocs/{self.doc.pk}/",
            {"id": new_id, "title": "clone"},
            format="json",
        )
        # Observed today: 200, and a second tenant-A row now exists.
        self.assertEqual(
            RbacUuidDoc.objects.filter(brokerage=self.brokerage_a).count(),
            1,
            resp.data,
        )

    def test_patch_pk_is_not_a_cross_tenant_existence_oracle(self):
        self.client.force_authenticate(self.updater)
        resp = self.client.patch(
            f"/api/rbacuuiddocs/{self.doc.pk}/",
            {"id": str(self.victim.pk)},
            format="json",
        )
        self.assertNotIn("already exists", str(resp.data), resp.data)


# ---------------------------------------------------------------------------
# F11 (functional). Permission-off modes still strip writes by TurboDRF roles.
# ---------------------------------------------------------------------------


class TestPermissionOffModesWrite(TestCase):
    def setUp(self):
        import tests.urls  # noqa: F401

        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(username="plain")
        self.user._test_roles = ["viewer"]  # has relatedmodel.read only

    @override_settings(TURBODRF_DISABLE_PERMISSIONS=True)
    def test_disable_permissions_create_keeps_fields(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/relatedmodels/", {"name": "kept"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        obj = RelatedModel.objects.order_by("-pk").first()
        self.assertEqual(obj.name, "kept", resp.data)

    @override_settings(TURBODRF_USE_DEFAULT_PERMISSIONS=True)
    def test_default_permissions_create_keeps_fields(self):
        self.user.user_permissions.add(
            Permission.objects.get(codename="add_relatedmodel"),
            Permission.objects.get(codename="view_relatedmodel"),
        )
        self.user = User.objects.get(pk=self.user.pk)
        self.user._test_roles = ["viewer"]
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/relatedmodels/", {"name": "kept"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        obj = RelatedModel.objects.order_by("-pk").first()
        self.assertEqual(obj.name, "kept")


# ---------------------------------------------------------------------------
# Misc: helpers documented in AGENTS.md.
# ---------------------------------------------------------------------------


class TestDocumentedHelpers(TestCase):
    def test_invalidate_user_permissions_drops_cached_snapshots(self):
        from turbodrf.backends import (
            build_permission_snapshot,
            get_cache_key,
            invalidate_user_permissions,
        )

        user = User.objects.create_user(username="inv", password="x")
        user._test_roles = ["viewer"]
        build_permission_snapshot(user, SampleModel)
        key = get_cache_key(user, SampleModel)
        self.assertIsNotNone(cache.get(key))

        invalidate_user_permissions(user)
        self.assertNotEqual(get_cache_key(user, SampleModel), key)

        build_permission_snapshot(user, SampleModel)
        key = get_cache_key(user, SampleModel)
        invalidate_user_permissions()  # everyone
        self.assertNotEqual(get_cache_key(user, SampleModel), key)


# ---------------------------------------------------------------------------
# Controls that HOLD (no finding) — kept as regression coverage.
# ---------------------------------------------------------------------------


@override_settings(TURBODRF_ROLES=RBAC_ROLES)
class TestControlsHold(_Base):
    def test_tenant_reassign_on_note_rejected(self):
        note = RbacNote.objects.create(
            body="n", brokerage=self.brokerage_a, deal=self.deal_a
        )
        self.client.force_authenticate(self.manager_a)
        resp = self.client.patch(
            f"/api/rbacnotes/{note.pk}/",
            {"brokerage": self.brokerage_b.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        note.refresh_from_db()
        self.assertEqual(note.brokerage_id, self.brokerage_a.pk)

    def test_note_fk_to_predicate_hidden_deal_rejected(self):
        self.client.force_authenticate(self.under_a)
        resp = self.client.post(
            "/api/rbacnotes/",
            {"body": "n", "deal": self.deal_a2.pk},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(RbacNote.objects.exists())

    def test_viewer_cannot_filter_or_order_on_denied_field(self):
        related = RelatedModel.objects.create(name="r")
        SampleModel.objects.create(
            title="a", price=Decimal("1"), related=related, secret_field="aaa"
        )
        SampleModel.objects.create(
            title="b", price=Decimal("2"), related=related, secret_field="zzz"
        )
        viewer = User.objects.create_user(username="v2")
        viewer._test_roles = ["viewer"]
        self.client.force_authenticate(viewer)
        base = self.client.get("/api/samplemodels/")
        for q in (
            {"secret_field__istartswith": "zz"},
            {"secret_field__istartswith_or": "zz"},
            {"price__gte": "2"},
        ):
            resp = self.client.get("/api/samplemodels/", q)
            self.assertEqual(
                resp.data["pagination"]["total_items"],
                base.data["pagination"]["total_items"],
                q,
            )
