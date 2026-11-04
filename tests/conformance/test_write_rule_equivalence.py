"""Differential fuzz of the predicate WRITE rules against an independent
executable specification (tests/conformance/write_spec.py, written from the
docs only).

Each example draws a random predicate tree (up to three top-level predicates,
ANDed; Owner on one column, two columns, a ``__`` path, or a column and a
path, with and without a bypass role, also as the ``owner_field`` sugar;
Members; Group; Conditional and Custom, some over the M2M or a nullable FK
chain, negated too; up to four predicates, Either nested to depth 2), a caller with random roles, a
starting row (the first of three random rows the caller can see, if any) and
a random write: a create with owner columns omitted / self / another user /
null, a PATCH or a full PUT that changes or keeps owner columns, the path's
first hop, the team, the members and other columns, or a DELETE. It serves
WriteRuleRow with that tree through ``turbodrf.register()`` and a fresh
router, sends the write through the real API, and asserts the outcome
(allowed / 404 / refused) and the table afterwards match the spec's.

Every user, row, parent and team is in one tenant: the tenant boundary isn't
under test, nor is RBAC (every caller holds create / update / delete, and no
field has a rule).

CI runs ``TURBODRF_WRITE_RULE_EXAMPLES`` (50) examples. To fuzz harder:

    TURBODRF_WRITE_RULE_EXAMPLES=3000 pytest -n 0 -p no:cacheprovider \\
        tests/conformance/test_write_rule_equivalence.py

With ``TURBODRF_WRITE_RULE_COLLECT=<file>`` a divergence is appended to the
file as a JSON line instead of failing the example, so one campaign lists them
all.

Triage so far: the only divergences were doc gaps where the code is the
stricter one (write_spec READINGS 2, 3 and 5: a writer without a bypass role
can't write null into an owner column, nor name a column that holds another
user even with its current value). The spec now takes the code's reading
there; TestHandPickedCases pins it. One framework bug (class a), in the read
scope the saved-row check shares: TestTriagedDivergences, expected failures.
"""

import json
import os
from unittest import mock

import pytest

pytest.importorskip("hypothesis", reason="hypothesis not installed (dev/test extra)")

from django.contrib.auth import get_user_model  # noqa: E402
from django.core.cache import cache  # noqa: E402
from django.db.models import Q  # noqa: E402
from django.test import TestCase as DjangoTestCase  # noqa: E402
from django.test import override_settings  # noqa: E402
from django.urls import include, path  # noqa: E402
from hypothesis import HealthCheck, event, given, note, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.errors import InvalidArgument  # noqa: E402
from hypothesis.extra.django import TestCase  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from tests.fixtures import router_apps  # noqa: E402
from tests.test_app.apps import _test_user_brokerages, set_test_brokerage  # noqa: E402
from tests.test_app.models import (  # noqa: E402
    Brokerage,
    WriteRuleParent,
    WriteRuleRow,
    WriteRuleTeam,
)

from . import write_spec as spec  # noqa: E402
from .write_spec import (  # noqa: E402
    Caller,
    SConditional,
    SCustom,
    SEither,
    SGroup,
    SMembers,
    SOwner,
    World,
    Write,
)

User = get_user_model()

urlpatterns = []  # a router serving WriteRuleRow with this example's tree

URL = "/api/writerows/"
PERMS = [f"test_app.writerulerow.{a}" for a in ("read", "create", "update", "delete")]
ROLES = {role: list(PERMS) for role in ("writer", "boss", "staff", "auditor")}
FIELDS = [
    "id",
    "title",
    "status",
    "brokerage",
    "owner",
    "editor",
    "parent",
    "team",
    "members",
]

settings.register_profile(
    "write-rule-equivalence",
    max_examples=int(os.environ.get("TURBODRF_WRITE_RULE_EXAMPLES", "50")),
    deadline=None,
    database=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.filter_too_much,
        HealthCheck.data_too_large,
    ],
)
FUZZ = settings.get_profile("write-rule-equivalence")
COLLECT = os.environ.get("TURBODRF_WRITE_RULE_COLLECT")

# The symbolic world write_spec reasons about. P_* are parents (their owner),
# T_* teams (their members).
WORLD = World(
    parents={"P_me": "me", "P_other": "other", "P_none": None},
    teams={"T_me": frozenset({"me"}), "T_other": frozenset({"other"})},
)


# ---------------------------------------------------------------------------
# The real predicates for a spec tree
# ---------------------------------------------------------------------------


def _roles_of(request):
    from turbodrf.backends import get_user_roles

    return set(get_user_roles(request.user))


def _v_no_rule(validated_data, instance, request):
    return []


def _v_block_auditors(validated_data, instance, request):
    return ["auditors cannot write."] if "auditor" in _roles_of(request) else []


def _v_block_x_titles(validated_data, instance, request):
    title = (validated_data or {}).get("title")
    return ["x titles are locked."] if title and title.startswith("x") else []


def _v_block_frozen_rows(validated_data, instance, request):
    if instance is not None and instance.status == "frozen":
        return ["frozen rows are read-only."]
    return []


# name: (q_func, write_validator); each mirrors write_spec.CUSTOM_RULES[name].
REAL_CUSTOMS = {
    "published": (lambda req, roles: Q(title__startswith="pub"), _v_no_rule),
    "open_status": (lambda req, roles: Q(status="open"), _v_block_auditors),
    "auditor_sees_all": (
        lambda req, roles: Q() if "auditor" in set(roles) else Q(pk__in=[]),
        _v_block_x_titles,
    ),
    "frozen_guard": (lambda req, roles: Q(), _v_block_frozen_rows),
    "fills_editor": (lambda req, roles: Q(), _v_no_rule),
    "not_shared_with_other": (
        lambda req, roles: ~Q(members__username="wr_other"),
        _v_no_rule,
    ),
    "my_team_or_no_parent": (
        lambda req, roles: Q(team__members=req.user) | Q(parent__isnull=True),
        _v_no_rule,
    ),
}
assert set(REAL_CUSTOMS) == set(spec.CUSTOM_RULES)

# Each mirrors write_spec.WHENS[name].
REAL_WHENS = {
    "status=staff": Q(status="staff"),
    "status=frozen": Q(status="frozen"),
    "no members": Q(members__isnull=True),
    "other is a member": Q(members__username="wr_other"),
    "parent has no owner": Q(parent__owner__isnull=True),
    "other is in the team": Q(team__members__username="wr_other"),
}
assert set(REAL_WHENS) == set(spec.WHENS)


def _fill_editor(validated_data, request):
    if "editor" in validated_data:
        return validated_data
    return {**validated_data, "editor": request.user}


REAL_FILLERS = {"fills_editor": _fill_editor}
assert set(REAL_FILLERS) == set(spec.CUSTOM_FILLERS)


def build(pred):
    from turbodrf.predicates import Conditional, Custom, Either, Group, Members, Owner

    if isinstance(pred, SOwner):
        column = pred.fields[0] if len(pred.fields) == 1 else list(pred.fields)
        return Owner(column, bypass=list(pred.bypass) or None)
    if isinstance(pred, SMembers):
        return Members(pred.field)
    if isinstance(pred, SGroup):
        return Group(pred.field)
    if isinstance(pred, SConditional):
        return Conditional(
            when=REAL_WHENS[pred.when], require_roles=list(pred.require_roles)
        )
    if isinstance(pred, SCustom):
        q_func, validator = REAL_CUSTOMS[pred.name]
        return Custom(
            q_func, write_validator=validator, auto_filler=REAL_FILLERS.get(pred.name)
        )
    if isinstance(pred, SEither):
        return Either(*[build(c) for c in pred.children])
    raise TypeError(pred)


def config_for(tree, sugar=False):
    config = {
        "endpoint": "writerows",
        "tenant_field": "brokerage",
        "fields": list(FIELDS),
    }
    if sugar and len(tree) == 1 and isinstance(tree[0], SOwner):
        # tenancy.md, "Sugar form vs predicate list": owner_field compiles to
        # one Owner (a list to one several-column Owner).
        (owner,) = tree
        config["owner_field"] = (
            owner.fields[0] if len(owner.fields) == 1 else list(owner.fields)
        )
        if owner.bypass:
            config["bypass_owner_roles"] = list(owner.bypass)
        return config
    return {**config, "visibility": [build(p) for p in tree]}


class Serving:
    """Serve WriteRuleRow with ``tree``'s config: ``turbodrf.register()`` and
    a router that sees only it, the registries restored on exit."""

    def __init__(self, tree, sugar=False):
        self.tree = tree
        self.sugar = sugar

    def __enter__(self):
        import turbodrf.router as router_mod
        from turbodrf import predicates, register
        from turbodrf.router import TurboDRFRouter

        self.saved = (
            dict(predicates._model_predicates),
            dict(predicates._model_tenant_fields),
            router_mod._bypass_roles_validated,
        )
        register(WriteRuleRow, config_for(self.tree, self.sugar))
        with mock.patch("turbodrf.router.apps", router_apps([WriteRuleRow])):
            router = TurboDRFRouter()
        urlpatterns[:] = [path("api/", include(router.urls))]
        return self

    def __exit__(self, *exc):
        import turbodrf.router as router_mod
        from turbodrf import predicates
        from turbodrf.mixins import _registered

        urlpatterns.clear()
        _registered.pop(WriteRuleRow, None)
        predicates._model_predicates.clear()
        predicates._model_predicates.update(self.saved[0])
        predicates._model_tenant_fields.clear()
        predicates._model_tenant_fields.update(self.saved[1])
        router_mod._bypass_roles_validated = self.saved[2]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

USER = st.sampled_from(["me", "other", None])
PARENT = st.sampled_from(["P_me", "P_other", "P_none", None])
TEAM = st.sampled_from(["T_me", "T_other", None])
STATUS = st.sampled_from(["open", "staff", "frozen"])
TITLE = st.sampled_from(["pub-a", "x-a", "plain"])
MEMBERS = st.sampled_from([(), ("me",), ("other",), ("me", "other")])
BYPASS = st.sampled_from([(), ("boss",)])

owners = st.one_of(
    st.builds(SOwner, st.sampled_from([("owner",), ("editor",)]), BYPASS),
    st.builds(SOwner, st.just(("owner", "editor")), BYPASS),
    st.builds(SOwner, st.just(("parent__owner",)), BYPASS),
    # One Owner over a column and a path: docs neither show nor refuse it.
    st.builds(SOwner, st.just(("owner", "parent__owner")), BYPASS),
)
leaves = st.one_of(
    owners,
    st.just(SMembers()),
    st.just(SGroup()),
    st.builds(
        SConditional,
        st.sampled_from(sorted(spec.WHENS)),
        st.sampled_from([("staff",), ("boss",)]),
    ),
    st.sampled_from([SCustom(name) for name in sorted(spec.CUSTOM_RULES)]),
)
inner_either = st.lists(leaves, min_size=2, max_size=4).map(
    lambda cs: SEither(tuple(cs))
)
top_either = st.lists(st.one_of(leaves, inner_either), min_size=2, max_size=4).map(
    lambda cs: SEither(tuple(cs))
)
trees = st.one_of(
    st.just(()),
    st.tuples(owners),  # served as sugar (owner_field) half the time
    st.lists(leaves, min_size=1, max_size=4),
    st.tuples(top_either),
    st.lists(st.one_of(top_either, leaves), min_size=2, max_size=4),
).map(tuple)

callers = st.frozensets(st.sampled_from(["boss", "staff", "auditor"])).map(
    lambda extra: Caller("me", frozenset({"writer"}) | extra)
)

rows = st.fixed_dictionaries(
    {
        "title": TITLE,
        "status": STATUS,
        "owner": USER,
        "editor": USER,
        "parent": PARENT,
        "team": TEAM,
        "members": MEMBERS.map(frozenset),
    }
)

OPTIONAL = {
    "status": STATUS,
    "owner": USER,
    "editor": USER,
    "parent": PARENT,
    "team": TEAM,
    "members": MEMBERS,
}
CHANGES = st.fixed_dictionaries({}, optional={"title": TITLE, **OPTIONAL})
writes = st.one_of(
    st.fixed_dictionaries({"title": TITLE}, optional=OPTIONAL).map(
        lambda p: Write("create", p)
    ),
    CHANGES.map(lambda p: Write("update", p)),
    CHANGES.map(lambda p: Write("update", p)),
    # A PUT: every column, the drawn ones changed (see full_put()).
    CHANGES.map(lambda p: Write("update", p, method="put")),
    st.just(Write("delete")),
)


def full_put(write, before):
    """A PUT names every column: the row's values, then the changes."""
    if write.method != "put":
        return write
    current = {k: before[k] for k in spec.ROW_COLUMNS}
    current["members"] = tuple(sorted(current["members"]))
    return Write("update", {**current, **write.payload}, method="put")


# ---------------------------------------------------------------------------
# Running one example against the real API
# ---------------------------------------------------------------------------


class _World:
    """The database rows for the symbolic WORLD."""

    def __init__(self):
        self.tenant = Brokerage.objects.create(name="A")
        self.users = {
            name: User.objects.create(username=f"wr_{name}") for name in ("me", "other")
        }
        self.parents = {
            name: WriteRuleParent.objects.create(
                name=name,
                brokerage=self.tenant,
                owner=self.users[owner] if owner else None,
            )
            for name, owner in WORLD.parents.items()
        }
        self.teams = {}
        for name, members in WORLD.teams.items():
            team = WriteRuleTeam.objects.create(name=name, brokerage=self.tenant)
            team.members.set([self.users[m] for m in members])
            self.teams[name] = team
        self._sym = {}
        for table in (self.users, self.parents, self.teams):
            for name, obj in table.items():
                self._sym[(type(obj), obj.pk)] = name

    def pk(self, column, name):
        if name is None:
            return None
        table = {"owner": self.users, "editor": self.users, "parent": self.parents}
        return {**table, "team": self.teams}[column][name].pk

    def sym(self, model, pk):
        return None if pk is None else self._sym[(model, pk)]

    def body(self, payload):
        out = {}
        for key, value in payload.items():
            if key == "members":
                out[key] = [self.users[m].pk for m in value]
            elif key in ("owner", "editor", "parent", "team"):
                out[key] = self.pk(key, value)
            else:
                out[key] = value
        return out

    def make_row(self, row):
        obj = WriteRuleRow.objects.create(
            title=row["title"],
            status=row["status"],
            brokerage=self.tenant,
            owner_id=self.pk("owner", row["owner"]),
            editor_id=self.pk("editor", row["editor"]),
            parent_id=self.pk("parent", row["parent"]),
            team_id=self.pk("team", row["team"]),
        )
        obj.members.set([self.users[m] for m in row["members"]])
        return obj

    def snapshot(self, obj):
        return {
            "title": obj.title,
            "status": obj.status,
            "owner": self.sym(User, obj.owner_id),
            "editor": self.sym(User, obj.editor_id),
            "parent": self.sym(WriteRuleParent, obj.parent_id),
            "team": self.sym(WriteRuleTeam, obj.team_id),
            "members": frozenset(
                self.sym(User, pk) for pk in obj.members.values_list("pk", flat=True)
            ),
        }

    def rows(self):
        return [self.snapshot(o) for o in WriteRuleRow.objects.order_by("pk")]


def actual_outcome(kind, status):
    if status == {"create": 201, "update": 200, "delete": 204}[kind]:
        return "allowed"
    if status == 404:
        return "not_found"
    if 400 <= status < 500:
        return "refused"
    return f"error {status}"


def run_write(world, tree, caller, before, write, sugar=False):
    """Send ``write`` as ``caller`` to WriteRuleRow served with ``tree``, the
    row ``before`` created first for an update or delete. Returns (outcome,
    status, body)."""
    user = world.users[caller.user]
    user._test_roles = sorted(caller.roles)
    set_test_brokerage(user, world.tenant)
    for other in world.users.values():
        set_test_brokerage(other, world.tenant)
    cache.clear()
    client = APIClient()
    client.force_authenticate(user)
    obj = world.make_row(before) if write.kind != "create" else None
    with Serving(tree, sugar):
        if write.kind == "create":
            resp = client.post(URL, world.body(write.payload), format="json")
        elif write.method == "put":
            body = {**world.body(write.payload), "brokerage": world.tenant.pk}
            resp = client.put(f"{URL}{obj.pk}/", body, format="json")
        elif write.kind == "update":
            resp = client.patch(
                f"{URL}{obj.pk}/", world.body(write.payload), format="json"
            )
        else:
            resp = client.delete(f"{URL}{obj.pk}/")
    body = resp.content[:600].decode(errors="replace")
    return actual_outcome(write.kind, resp.status_code), resp.status_code, body


def _describe(tree, caller, before, write):
    return {
        "tree": repr(tree),
        "roles": sorted(caller.roles),
        "before": (
            None if before is None else {**before, "members": sorted(before["members"])}
        ),
        "write": {
            "kind": write.kind,
            "method": write.method,
            "payload": dict(write.payload),
        },
    }


def _event(text):
    try:
        event(text)
    except InvalidArgument:  # outside a Hypothesis test
        pass


def check(test, world, tree, caller, before, write, strict=False, sugar=False):
    """Fail ``test`` unless the API's outcome and the table afterwards match
    the spec's (with COLLECT set and not ``strict``, log it instead)."""
    if write.kind == "create":
        before = None
    expected = spec.decide(tree, caller, before, write, WORLD)
    outcome, status, body = run_write(world, tree, caller, before, write, sugar)
    after = world.rows()

    # What the spec says the table holds now.
    if expected.allowed:
        want_rows = [] if write.kind == "delete" else [expected.row]
    else:
        want_rows = [] if before is None else [before]

    divergence = None
    if outcome != expected.outcome:
        divergence = "outcome"
    elif after != want_rows:
        divergence = "stored row"
    kind = write.method if write.kind == "update" else write.kind
    _event(f"{kind}: {outcome} ({status})")
    for reason in expected.reasons:
        _event(f"spec refuses: {reason.split(':')[-1].strip()}")
    for shape in sorted({_kind(p, inside) for p, inside in spec.walk(tree)}):
        _event(f"tree has {shape}")
    if divergence is None:
        return

    record = {
        **_describe(tree, caller, before, write),
        "divergence": divergence,
        "spec": expected.outcome,
        "spec_reasons": expected.reasons,
        "spec_rows": [{**r, "members": sorted(r["members"])} for r in want_rows],
        "actual": outcome,
        "status": status,
        "body": body,
        "actual_rows": [{**r, "members": sorted(r["members"])} for r in after],
    }
    if COLLECT and not strict:
        with open(COLLECT, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        return
    try:
        note(json.dumps(record, indent=1, default=str))
    except InvalidArgument:  # outside a Hypothesis test: put it in the message
        test.fail(json.dumps(record, indent=1, default=str))
    test.fail(
        f"{divergence} diverges: spec {expected.outcome}, API {outcome} ({status})"
    )


def _kind(pred, inside_either):
    where = "in Either" if inside_either else "top-level"
    if isinstance(pred, SOwner):
        shape = "path" if pred.is_path else ("multi" if pred.is_multi else "single")
        bypass = "+bypass" if pred.bypass else ""
        return f"Owner {shape}{bypass} {where}"
    if isinstance(pred, SEither):
        return f"Either {where}"
    return f"{type(pred).__name__[1:]} {where}"


@override_settings(
    ROOT_URLCONF=__name__,
    TURBODRF_ROLES=ROLES,
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
    TURBODRF_REQUIRE_TENANCY=True,
)
class TestWriteRuleEquivalence(TestCase):
    def setUp(self):
        _test_user_brokerages.clear()
        self.addCleanup(_test_user_brokerages.clear)
        self.world = _World()

    @FUZZ
    @given(
        tree=trees,
        caller=callers,
        candidates=st.lists(rows, min_size=3, max_size=3),
        write=writes,
        sugar=st.booleans(),
    )
    def test_writes_match_the_spec(self, tree, caller, candidates, write, sugar):
        # An update or delete of a row the caller can't see is a 404 whatever
        # the rules: start from the first candidate row they can see, if any.
        before = next(
            (r for r in candidates if spec.visible(tree, r, WORLD, caller)),
            candidates[0],
        )
        write = full_put(write, before)
        check(self, self.world, tree, caller, before, write, sugar=sugar)


# ---------------------------------------------------------------------------
# Hand-picked cases: one or more per reading the spec chose (write_spec
# READINGS), plus documented rules the fuzzer reaches only rarely. Each pins
# the spec's answer, then checks the API against it.
# ---------------------------------------------------------------------------


def _row(**values):
    row = {
        "title": "plain",
        "status": "open",
        "owner": None,
        "editor": None,
        "parent": None,
        "team": None,
        "members": frozenset(),
    }
    row.update(values)
    row["members"] = frozenset(row["members"])
    return row


WRITER = Caller("me", frozenset({"writer"}))
BOSS = Caller("me", frozenset({"writer", "boss"}))
AUDITOR = Caller("me", frozenset({"writer", "auditor"}))
OWNER = SOwner(("owner",))
EDITOR = SOwner(("editor",))
CO_OWNERS = SOwner(("owner", "editor"))
PATH = SOwner(("parent__owner",))
STAFF_ONLY = SConditional("status=staff", ("staff",))
PUB = SCustom("published")

# (name, tree, caller, row before (None: create), write, the spec's outcome)
CASES = [
    (
        "R1 the saved-row check fires for a bypass holder",
        (SOwner(("owner", "editor"), ("boss",)), PUB),
        BOSS,
        _row(title="pub-a", owner="other"),
        Write("update", {"title": "plain"}),
        "refused",
    ),
    (
        "R2 a Members viewer re-sending another user's owner column",
        (SEither((OWNER, SMembers())),),
        WRITER,
        _row(owner="other", members={"me"}),
        Write("update", {"owner": "other", "title": "pub-a"}),
        "refused",
    ),
    (
        "R2 a Members viewer's PATCH leaving owner out",
        (SEither((OWNER, SMembers())),),
        WRITER,
        _row(owner="other", members={"me"}),
        Write("update", {"title": "pub-a"}),
        "allowed",
    ),
    (
        "R2 a PUT from a Members viewer names the owner",
        (SEither((OWNER, SMembers())),),
        WRITER,
        _row(owner="other", members={"me"}),
        Write("update", {"title": "pub-a"}, method="put"),
        "refused",
    ),
    (
        "R3 an owner can't clear their own column",
        (OWNER,),
        WRITER,
        _row(owner="me"),
        Write("update", {"owner": None}),
        "refused",
    ),
    (
        "R3 a bypass holder can",
        (SOwner(("owner",), ("boss",)),),
        BOSS,
        _row(owner="me"),
        Write("update", {"owner": None}),
        "allowed",
    ),
    (
        "R4 create fills an Owner in a nested Either",
        (SEither((SMembers(), SEither((EDITOR, PUB)))),),
        WRITER,
        None,
        Write("create", {"title": "pub-a"}),
        "allowed",
    ),
    (
        "R4 create fills for a bypass holder",
        (SOwner(("owner",), ("boss",)),),
        BOSS,
        None,
        Write("create", {"title": "plain"}),
        "allowed",
    ),
    (
        "R5 an explicit null owner on create",
        (OWNER,),
        WRITER,
        None,
        Write("create", {"title": "plain", "owner": None}),
        "refused",
    ),
    (
        "R6 re-sending the path's first hop",
        (SEither((PATH, SMembers())),),
        WRITER,
        _row(parent="P_other", members={"me"}),
        Write("update", {"parent": "P_other"}),
        "allowed",
    ),
    (
        "path Owner in an Either: moving a colleague's row onto my parent",
        (SEither((PATH, SMembers())),),
        WRITER,
        _row(parent="P_other", members={"me"}),
        Write("update", {"parent": "P_me"}),
        "refused",
    ),
    (
        "path Owner in an Either: moving my row onto a colleague's parent",
        (SEither((PATH, SMembers())),),
        WRITER,
        _row(parent="P_me", members={"me"}),
        Write("update", {"parent": "P_other"}),
        "refused",
    ),
    (
        "R7 write_validator sees the row before the update",
        (SCustom("frozen_guard"),),
        WRITER,
        _row(status="frozen"),
        Write("update", {"status": "open"}),
        "refused",
    ),
    (
        "R8 the saved-row check is the whole visibility list",
        (STAFF_ONLY, PUB),
        WRITER,
        _row(title="pub-a"),
        Write("update", {"title": "plain"}),
        "refused",
    ),
    (
        "R9 the saved-row check sees the M2M as written",
        (SMembers(), STAFF_ONLY),
        WRITER,
        _row(members={"me"}),
        Write("update", {"members": []}),
        "refused",
    ),
    (
        "R9 Members alone adds no saved-row check",
        (SMembers(),),
        WRITER,
        _row(members={"me"}),
        Write("update", {"members": []}),
        "allowed",
    ),
    (
        "R10 a single-column Owner adds no saved-row check",
        (OWNER, PUB),
        WRITER,
        _row(owner="me", title="pub-a"),
        Write("update", {"title": "plain"}),
        "allowed",
    ),
    (
        "R11 delete: an Owner in an Either over a null owner",
        (SEither((OWNER, SMembers())),),
        WRITER,
        _row(members={"me"}),
        Write("delete"),
        "refused",
    ),
    (
        "R11 delete: an Owner in a nested Either",
        (SEither((SMembers(), SEither((OWNER, PUB)))),),
        WRITER,
        _row(owner="other", members={"me"}),
        Write("delete"),
        "refused",
    ),
    (
        "R12 delete: write_validator gets no data",
        (SCustom("auditor_sees_all"),),
        AUDITOR,
        _row(title="x-a"),
        Write("delete"),
        "allowed",
    ),
    (
        "R13 Owner over a column and a path: the path names the creator",
        (SOwner(("owner", "parent__owner")),),
        WRITER,
        None,
        Write("create", {"title": "plain", "parent": "P_me"}),
        "allowed",
    ),
    (
        "R13 ... and nothing names them",
        (SOwner(("owner", "parent__owner")),),
        WRITER,
        None,
        Write("create", {"title": "plain"}),
        "refused",
    ),
    (
        "R14 an auto_filler names the creator for a several-column Owner",
        (CO_OWNERS, SCustom("fills_editor")),
        WRITER,
        None,
        Write("create", {"title": "plain"}),
        "allowed",
    ),
    (
        "Members doesn't check creates",
        (SMembers(),),
        WRITER,
        None,
        Write("create", {"title": "plain", "members": ["other"]}),
        "allowed",
    ),
    (
        "Group doesn't check a move to another team",
        (SGroup(),),
        WRITER,
        _row(team="T_me"),
        Write("update", {"team": "T_other"}),
        "allowed",
    ),
    (
        "a several-column Owner in a nested Either: create must name me",
        (SEither((PUB, SEither((SMembers(), CO_OWNERS)))),),
        WRITER,
        None,
        Write("create", {"title": "pub-a", "members": ["me"]}),
        "refused",
    ),
    (
        "a path Owner in a nested Either: create must satisfy it",
        (SEither((SMembers(), SEither((SGroup(), PATH)))),),
        WRITER,
        None,
        Write("create", {"title": "plain", "members": ["me"], "parent": "P_other"}),
        "refused",
    ),
    (
        "Conditional in an Either: the saved row is checked through the Either",
        (SEither((SMembers(), STAFF_ONLY)),),
        WRITER,
        None,
        Write("create", {"title": "plain", "status": "staff"}),
        "refused",
    ),
    (
        "... and passes when another child shows it",
        (SEither((SMembers(), STAFF_ONLY)),),
        WRITER,
        None,
        Write("create", {"title": "plain", "status": "staff", "members": ["me"]}),
        "allowed",
    ),
]


@override_settings(
    ROOT_URLCONF=__name__,
    TURBODRF_ROLES=ROLES,
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
    TURBODRF_REQUIRE_TENANCY=True,
)
class TestHandPickedCases(DjangoTestCase):
    def setUp(self):
        _test_user_brokerages.clear()
        self.addCleanup(_test_user_brokerages.clear)
        self.world = _World()

    def test_cases(self):
        for name, tree, caller, before, write, outcome in CASES:
            with self.subTest(name):
                if before is not None:
                    write = full_put(write, before)
                decision = spec.decide(tree, caller, before, write, WORLD)
                self.assertEqual(decision.outcome, outcome, decision.reasons)
                check(self, self.world, tree, caller, before, write, strict=True)
            WriteRuleRow.objects.all().delete()


# ---------------------------------------------------------------------------
# Triaged divergences: class (a), the code contradicting a clear doc
# statement. Each is the minimal input, kept as an expected failure.
# ---------------------------------------------------------------------------

NOT_SHARED_WITH_OTHER = SCustom("not_shared_with_other")


@override_settings(
    ROOT_URLCONF=__name__,
    TURBODRF_ROLES=ROLES,
    TURBODRF_TENANT_MODEL="test_app.Brokerage",
    TURBODRF_REQUIRE_TENANCY=True,
)
class TestTriagedDivergences(DjangoTestCase):
    """(a) Two predicates over the same many-to-many, one positive and one
    negated, are judged as one Django filter. principles.md 4: "The visibility
    list is AND: every predicate must match." ``predicates_q``
    (turbodrf/predicates.py:97) ANDs their Qs into one, and
    ``without_to_many_joins`` leaves both ``members`` lookups to Django
    (turbodrf/views.py:267, "tied to a positive lookup's JOIN"), which
    correlates the negated lookup's subquery with the positive lookup's JOIN
    (``split_exclude`` reusing the alias). So ``~Q(members__username="wr_other")``
    is read as "the member who is me isn't wr_other", always true: a row
    ``Custom`` (or a ``Conditional`` whose ``when`` crosses the M2M) excludes
    is shown to every member. The saved-row check (``_row_matches``) uses the
    same scope, so it passes a write that leaves the row outside a predicate.
    """

    def setUp(self):
        _test_user_brokerages.clear()
        self.addCleanup(_test_user_brokerages.clear)
        self.world = _World()

    def _listed(self, tree, caller, row):
        """The rows ``caller`` lists under ``tree`` with only ``row`` in the
        table."""
        user = self.world.users[caller.user]
        user._test_roles = sorted(caller.roles)
        set_test_brokerage(user, self.world.tenant)
        cache.clear()
        client = APIClient()
        client.force_authenticate(user)
        self.world.make_row(row)
        with Serving(tree):
            resp = client.get(URL)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.data["data"]

    def test_a_custom_excluding_a_member_is_ignored_beside_members(self):
        tree = (SMembers(), NOT_SHARED_WITH_OTHER)
        row = _row(members={"me", "other"})
        self.assertFalse(spec.visible(tree, row, WORLD, WRITER))
        self.assertEqual(self._listed(tree, WRITER, row), [])

    def test_a_conditional_over_the_members_is_ignored_beside_members(self):
        tree = (SMembers(), SConditional("other is a member", ("staff",)))
        row = _row(members={"me", "other"})
        self.assertFalse(spec.visible(tree, row, WORLD, WRITER))
        self.assertEqual(self._listed(tree, WRITER, row), [])

    def test_the_saved_row_check_passes_a_row_a_predicate_excludes(self):
        # The fuzzer's find (TURBODRF_WRITE_RULE_EXAMPLES=10000), minimised:
        # the Conditional only makes the write take the saved-row check.
        tree = (STAFF_ONLY, SMembers(), NOT_SHARED_WITH_OTHER)
        before = _row(members={"me"})
        write = Write("update", {"members": ["me", "other"]})
        decision = spec.decide(tree, WRITER, before, write, WORLD)
        self.assertEqual(decision.outcome, "refused")
        check(self, self.world, tree, WRITER, before, write, strict=True)
