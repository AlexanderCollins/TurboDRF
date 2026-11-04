"""An executable specification of TurboDRF's predicate WRITE rules, written
from the documentation only:

- AGENTS.md, "Predicates" and "How `Either` treats writes";
- docs/tenancy.md, each predicate's section ("Owner", "Either", "Custom",
  "Advanced predicates");
- docs/security.md, "Row-level access control" and "Custom predicate write
  safety";
- docs/principles.md, "4. The predicate algebra: reads are OR, writes are AND".

It doesn't import TurboDRF and was written without reading the write path
(``serializers._apply_predicate_writes`` and friends, the predicates'
``validate_write``). tests/conformance/test_write_rule_equivalence.py checks
the real API against it.

Everything here is symbolic: users, parents and teams are names ("me",
"other", "P_me", "T_me"), a row is a dict, and visibility (the read rule) is a
plain Python evaluation of the predicates on the row's values, not a queryset.

The tenant boundary isn't modelled: every user, row, parent and team shares
one tenant. RBAC isn't either: every caller holds create / update / delete on
the model and every field is readable and writable.

Readings chosen where the docs are silent or ambiguous are marked
``READING (n)``; the list is in READINGS at the bottom. Two of them (2 and 3)
were changed after the fuzzer found the code taking a stricter reading of a
doc gap; each says so. Nothing else was changed to match the code.
"""

from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# The predicate tree
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SOwner:
    """``Owner(field)`` or ``Owner([f1, f2])``. A ``__`` column is a path
    (``parent__owner``)."""

    fields: Tuple[str, ...]
    bypass: Tuple[str, ...] = ()

    @property
    def is_path(self):
        return any("__" in f for f in self.fields)

    @property
    def is_multi(self):
        return len(self.fields) > 1

    @property
    def is_single_column(self):
        # tenancy.md, Owner: "a single owner column (not a `__` path)".
        return not self.is_multi and not self.is_path


@dataclass(frozen=True)
class SMembers:
    field: str = "members"


@dataclass(frozen=True)
class SGroup:
    """``Group(field, user_via='members')``: the caller is in
    ``row.<field>.<user_via>``."""

    field: str = "team"


@dataclass(frozen=True)
class SConditional:
    """``Conditional(when=Q(...), require_roles=[...])``, ``when`` by name
    from WHENS."""

    when: str
    require_roles: Tuple[str, ...]


@dataclass(frozen=True)
class SCustom:
    """``Custom(q_func, write_validator)``, by name from CUSTOM_RULES."""

    name: str


@dataclass(frozen=True)
class SEither:
    children: Tuple[object, ...]


# ---------------------------------------------------------------------------
# The world, the caller, the write
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    """Rows the predicates reach through a relation. ``parents`` maps a
    parent to its owner (or None); ``teams`` maps a team to its members."""

    parents: Mapping[str, Optional[str]]
    teams: Mapping[str, frozenset]


@dataclass(frozen=True)
class Caller:
    user: str
    roles: frozenset


ROW_COLUMNS = ("title", "status", "owner", "editor", "parent", "team", "members")

# Column defaults on create (the model's): status "open", the rest empty.
CREATE_DEFAULTS = {
    "status": "open",
    "owner": None,
    "editor": None,
    "parent": None,
    "team": None,
    "members": frozenset(),
}


@dataclass(frozen=True)
class Write:
    kind: str  # "create" | "update" | "delete"
    payload: Mapping[str, object] = field(default_factory=dict)
    # An update is a PATCH or a PUT. The rules don't tell them apart: a PUT's
    # payload names every column (with their current values unless changed).
    method: str = "patch"


@dataclass
class Decision:
    outcome: str  # "allowed" | "not_found" (404) | "refused" (400)
    reasons: list
    row: Optional[dict]  # the stored row when allowed (None after a delete)

    @property
    def allowed(self):
        return self.outcome == "allowed"


# ---------------------------------------------------------------------------
# Custom predicates: each one's read rule and write rule in plain Python.
# The fuzzer pairs every name with the Django Q / validator it registers.
# ---------------------------------------------------------------------------


def _no_rule(data, instance, caller):
    return []


def _block_auditors(data, instance, caller):
    return ["auditors cannot write."] if "auditor" in caller.roles else []


def _block_x_titles(data, instance, caller):
    title = (data or {}).get("title")
    return ["x titles are locked."] if title and title.startswith("x") else []


def _block_frozen_rows(data, instance, caller):
    if instance is not None and instance["status"] == "frozen":
        return ["frozen rows are read-only."]
    return []


def _team_has(row, world, user):
    return row["team"] is not None and user in world.teams[row["team"]]


def _parent_owner(row, world):
    return world.parents[row["parent"]] if row["parent"] else None


CUSTOM_RULES = {
    # name: (read rule (row, caller, world) -> bool, write_validator)
    "published": (lambda row, c, w: row["title"].startswith("pub"), _no_rule),
    "open_status": (lambda row, c, w: row["status"] == "open", _block_auditors),
    "auditor_sees_all": (lambda row, c, w: "auditor" in c.roles, _block_x_titles),
    "frozen_guard": (lambda row, c, w: True, _block_frozen_rows),
    "fills_editor": (lambda row, c, w: True, _no_rule),
    # Rules across a many-to-many and a nullable FK chain, negated too.
    "not_shared_with_other": (
        lambda row, c, w: "other" not in row["members"],
        _no_rule,
    ),
    "my_team_or_no_parent": (
        lambda row, c, w: _team_has(row, w, c.user) or row["parent"] is None,
        _no_rule,
    ),
}

# Conditional `when`s: name -> (row, world) -> whether the row matches.
WHENS = {
    "status=staff": lambda row, w: row["status"] == "staff",
    "status=frozen": lambda row, w: row["status"] == "frozen",
    "no members": lambda row, w: not row["members"],
    "other is a member": lambda row, w: "other" in row["members"],
    "parent has no owner": lambda row, w: _parent_owner(row, w) is None,
    "other is in the team": lambda row, w: _team_has(row, w, "other"),
}

# tenancy.md, Custom: "`auto_filler(validated_data, request)` optionally
# returns the data to create with." name: (payload, caller) -> the columns it
# adds. READING (14): the filler sees the request's data, and its columns are
# part of the created row the checks judge.
CUSTOM_FILLERS = {
    "fills_editor": lambda payload, caller: (
        {} if "editor" in payload else {"editor": caller.user}
    ),
}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _bypassed(owner, caller):
    # tenancy.md, Owner: "Filters to rows where <field> = request.user,
    # unless caller has a bypass role".
    return bool(set(owner.bypass) & set(caller.roles))


def owner_value(row, column, world):
    """The user an owner column names on ``row``; a ``__`` path follows the
    parent."""
    if column == "parent__owner":
        return world.parents.get(row["parent"]) if row["parent"] else None
    return row[column]


def owner_holds(owner, row, world, caller):
    """The Owner on its own: a bypass role, or ANY column naming the caller
    (tenancy.md: "A multi-column Owner ... reads rows where ANY column
    matches")."""
    if _bypassed(owner, caller):
        return True
    return any(owner_value(row, f, world) == caller.user for f in owner.fields)


def matches(pred, row, world, caller):
    """Whether ``pred`` lets ``caller`` see ``row``."""
    if isinstance(pred, SOwner):
        return owner_holds(pred, row, world, caller)
    if isinstance(pred, SMembers):
        return caller.user in row[pred.field]
    if isinstance(pred, SGroup):
        team = row[pred.field]
        return team is not None and caller.user in world.teams[team]
    if isinstance(pred, SConditional):
        # tenancy.md: "rows matching the `when` Q are visible only to callers
        # holding one of `require_roles`; other rows are unaffected".
        if not WHENS[pred.when](row, world):
            return True
        return bool(set(pred.require_roles) & set(caller.roles))
    if isinstance(pred, SCustom):
        return CUSTOM_RULES[pred.name][0](row, caller, world)
    if isinstance(pred, SEither):
        # principles.md 4: "Either is OR for reads".
        return any(matches(c, row, world, caller) for c in pred.children)
    raise TypeError(pred)


def visible(tree, row, world, caller):
    # principles.md 4: "The visibility list is AND: every predicate must
    # match." An empty list is tenant-only: every row in the tenant.
    return all(matches(p, row, world, caller) for p in tree)


# ---------------------------------------------------------------------------
# Walking the tree
# ---------------------------------------------------------------------------


def walk(tree, inside_either=False):
    """Every predicate in the tree with whether it sits (at any depth) inside
    an Either."""
    for pred in tree:
        yield pred, inside_either
        if isinstance(pred, SEither):
            yield from walk(pred.children, inside_either=True)


def owners(tree):
    return [(p, inside) for p, inside in walk(tree) if isinstance(p, SOwner)]


def customs(tree):
    return [p for p, _ in walk(tree) if isinstance(p, SCustom)]


def has_saved_row_rule(tree):
    """principles.md 4: "Some rules can only be judged on the saved row: a
    Conditional, an Owner over a `__` path, and an Owner with several
    columns. The row is checked after the save ... and the write is rolled
    back if the writer could no longer see it."

    READING (1): the trigger is the presence of one of these predicates
    anywhere in the tree (inside an Either too), whatever the caller's
    bypass roles.
    """
    for pred, _ in walk(tree):
        if isinstance(pred, SConditional):
            return True
        if isinstance(pred, SOwner) and (pred.is_path or pred.is_multi):
            return True
    return False


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _owner_column_errors(owner, payload, before, caller):
    """tenancy.md, Owner: "Writes by a caller without a bypass role are
    checked: they can't set any owner column to another user, and they can't
    change an owner column whose current value is another user."

    AGENTS.md / tenancy.md, Either: these checks apply to every writer,
    "including users who see the row through another child".

    A ``__`` path column isn't written by the request; its rule is judged on
    the saved row instead (see decide()).
    """
    if _bypassed(owner, caller):
        return []
    errors = []
    for column in owner.fields:
        if "__" in column or column not in payload:
            continue
        new = payload[column]
        current = before[column] if before is not None else None
        # READING (2), settled by triage (a doc gap, class b; the code's reading
        # is the stricter one): naming a column that holds another user is a
        # change even when the value sent is the current one, so a PUT-style
        # body from a Members viewer naming the existing owner is refused.
        # (The first reading let an unchanged value through.)
        if current is not None and current != caller.user:
            errors.append(f"{column}: can't change a column holding another user")
        elif new is None:
            # READING (3), settled by triage (a doc gap, class b; the code's
            # reading is the stricter one): a writer without a bypass role
            # can't write null into an owner column, on create too, even when
            # it's already null. The docs name only "another user"; the first
            # reading allowed clearing.
            errors.append(f"{column}: can't clear an owner column")
        elif new != caller.user:
            errors.append(f"{column}: can't set an owner column to another user")
    return errors


def _apply(before, payload):
    row = dict(before)
    for key, value in payload.items():
        row[key] = frozenset(value) if key == "members" else value
    return row


def decide(tree, caller, before, write, world):
    """The documented decision for ``write`` by ``caller``.

    ``tree`` is the visibility list (top-level predicates, ANDed). ``before``
    is the row before the write (None for a create). Returns a Decision with
    the stored row on success.
    """
    tree = tuple(tree)
    payload = dict(write.payload)

    # -- Update / delete: only visible rows can be fetched -----------------
    # tenancy.md, Behavior: "Detail / PATCH / DELETE return 404 (not 403) when
    # a row is filtered out."
    if write.kind in ("update", "delete"):
        assert before is not None
        if not visible(tree, before, world, caller):
            return Decision("not_found", ["row not visible"], None)

    if write.kind == "delete":
        return _decide_delete(tree, caller, before, world)

    reasons = []

    # -- The stored row ----------------------------------------------------
    if write.kind == "create":
        stored = _apply({**CREATE_DEFAULTS}, payload)
        # tenancy.md, Owner: "On create, a single owner column (not a `__`
        # path) left out of the request is filled with the caller". Either:
        # "Create fills a single-column Owner child's column with the caller
        # when the request leaves it out, as a top-level Owner does."
        # READING (4): the fill applies at any depth of Either nesting, and to
        # a caller who holds the Owner's bypass role too (the docs make no
        # exception).
        # READING (5): an explicit null isn't "left out": it isn't filled (and
        # READING (3) refuses it from a writer without a bypass role).
        for owner, _ in owners(tree):
            if owner.is_single_column:
                column = owner.fields[0]
                if column not in payload:
                    stored[column] = caller.user
        for custom in customs(tree):
            if custom.name in CUSTOM_FILLERS:
                stored.update(CUSTOM_FILLERS[custom.name](payload, caller))
    else:
        # security.md: "The owner is auto-filled on create only; an update
        # leaves it as is."
        stored = _apply(before, payload)

    # -- Owner's column checks, for every Owner in the tree ----------------
    for owner, _ in owners(tree):
        reasons += _owner_column_errors(owner, payload, before, caller)

    # -- Owner rules judged on the saved row --------------------------------
    for owner, inside_either in owners(tree):
        if _bypassed(owner, caller):
            continue
        if write.kind == "create" and owner.is_multi:
            # READING (13): an Owner over a column and a path counts as
            # several columns here, and as a path below.
            # tenancy.md: a multi-column Owner "isn't auto-filled, so the saved
            # row is checked instead: a create by a caller without a bypass
            # role must name them in one of the columns". AGENTS.md: "A
            # several-column Owner is checked on the saved row too: a create
            # must name the caller in one of its columns" (in the paragraph on
            # Either, so inside an Either too).
            if not owner_holds(owner, stored, world, caller):
                reasons.append("create must name the caller in an owner column")
        if owner.is_path and inside_either:
            # tenancy.md, Either: "An Owner over a `__` path holds on its own
            # inside an Either: a create must satisfy it, and so must an update
            # that changes the path's first hop, before and after."
            hops = {f.split("__")[0] for f in owner.fields if "__" in f}
            changed = [
                h for h in hops if h in payload and before and payload[h] != before[h]
            ]
            if write.kind == "create":
                if not owner_holds(owner, stored, world, caller):
                    reasons.append("path Owner must hold on the created row")
            elif changed:
                # READING (6): re-sending the current first hop isn't a change.
                if not owner_holds(owner, before, world, caller):
                    reasons.append("path Owner must hold before the hop change")
                if not owner_holds(owner, stored, world, caller):
                    reasons.append("path Owner must hold after the hop change")

    # -- Custom write validators ---------------------------------------------
    # tenancy.md, Step 6: the validator receives (validated_data, instance,
    # request), instance None on create; non-empty rejects with a 400. Inside
    # an Either too: "a write must pass every child's check".
    # READING (7): ``instance`` is the row as it was before the update, and
    # ``validated_data`` holds the fields the request sent.
    for custom in customs(tree):
        reasons += CUSTOM_RULES[custom.name][1](payload, before, caller)

    # -- The saved-row visibility check --------------------------------------
    # principles.md 4 / tenancy.md (Conditional, path Owner): a write that
    # would leave the row invisible to its writer is rolled back with a 400.
    # READING (8): "invisible" means through the whole visibility list (every
    # top-level predicate, ANDed), not only the predicate that triggered the
    # check; "inside Either, the saved row must be visible through the Either
    # as a whole" is the special case of a list holding one Either.
    # READING (9): the M2M is part of the saved row: a writer removing
    # themselves from `members` is judged on the row without them.
    # READING (10): a single-column, non-path Owner adds no saved-row check:
    # it's not in principles.md's list. (Its column checks already keep its
    # column on the writer.)
    if has_saved_row_rule(tree) and not visible(tree, stored, world, caller):
        reasons.append("the saved row would be invisible to its writer")

    if reasons:
        return Decision("refused", reasons, None)
    return Decision("allowed", [], stored)


def _decide_delete(tree, caller, before, world):
    """security.md, "Delete": "a delete passes the write checks a write would:
    each `Custom`'s `write_validator` runs, and an `Owner` inside an `Either`
    must hold for the row. Seeing a row through `Members` doesn't let a
    caller delete a row that `Owner` gives to someone else."

    READING (11): "hold for the row" is the Owner's read rule on the row as it
    is: a bypass role, or any column (or the path) naming the caller. So a
    Members viewer can't delete a row whose owner column is null either.
    READING (12): the validator gets ``validated_data`` empty and ``instance``
    the row.
    No saved-row check: there's no row left to see.
    """
    reasons = []
    for owner, inside_either in owners(tree):
        if inside_either and not owner_holds(owner, before, world, caller):
            reasons.append(f"Owner{owner.fields} must hold for the deleted row")
    for custom in customs(tree):
        reasons += CUSTOM_RULES[custom.name][1]({}, before, caller)
    if reasons:
        return Decision("refused", reasons, None)
    return Decision("allowed", [], None)


READINGS = {
    1: "The saved-row check fires when the tree holds a Conditional, a path "
    "Owner or a several-column Owner anywhere, whatever the caller's bypass "
    "roles.",
    2: "(triaged, b) Naming an owner column that holds another user is a "
    "change even when the value is unchanged.",
    3: "(triaged, b) A writer without a bypass role can't write null into an "
    "owner column (first reading: allowed).",
    4: "Create auto-fill reaches single-column Owners at any Either depth, "
    "and fills for bypass holders too.",
    5: "An explicit null owner on create isn't 'left out': it's not filled "
    "(and, by READING 3, refused for a writer without a bypass role).",
    6: "Re-sending a path's current first hop isn't a change of it.",
    7: "write_validator gets the pre-update row as instance and the sent "
    "fields as validated_data.",
    8: "The saved-row check is visibility through the whole visibility " "list.",
    9: "The saved-row check sees the M2M as written.",
    10: "A single-column non-path Owner adds no saved-row check.",
    11: "On delete, every Owner inside an Either must hold for the row as "
    "its read rule (a null owner doesn't hold).",
    12: "On delete, write_validator gets empty data and the row.",
    13: "Owner(['owner', 'parent__owner']) is a several-column Owner (create "
    "must name the caller) and a path Owner (holds on its own in an Either).",
    14: "A Custom auto_filler's columns are part of the created row the "
    "saved-row checks judge.",
}
