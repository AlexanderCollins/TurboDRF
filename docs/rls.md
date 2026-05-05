# Postgres Row Level Security (RLS) — Defense in Depth

TurboDRF's predicate system enforces row-level access at the **app layer** —
every queryset, every detail lookup, every write. This is the primary mechanism
and works on any Django backend (Postgres, MySQL, SQLite, etc.).

For Postgres deployments you can additionally enable **Row Level Security** as
a defense-in-depth layer that catches paths bypassing the framework: raw SQL,
admin shells, ad-hoc scripts, ORM bugs. RLS enforces the same rules at the
database layer — every connection is filtered, no exceptions.

This page covers:

- When to bother with RLS (and when not to)
- Setup: middleware, predicate-to-policy mapping, migration workflow
- The clean half (Tenant, Owner) and the messy half (Members, Group, Conditional)
- Caveats (pgbouncer, M2M perf)

---

## When to use RLS

**Worth the work if:**
- You're on Postgres and serious about multi-tenant isolation
- Operators run ad-hoc SQL against production
- You have admin tools or background scripts that touch the DB outside of
  TurboDRF
- Compliance requires defense-in-depth at the DB layer

**Skip it if:**
- Your app is single-tenant
- You're not on Postgres
- You'd rather have one source of truth (the predicate config) and trust the
  app layer

---

## How it works

```
┌──────────────────────────┐       ┌──────────────────────────┐
│  TurboDRFTenancyMiddleware │   →   │  Postgres session vars   │
│  (sets per request)      │       │  app.user_id, .tenant_id │
└──────────────────────────┘       └──────────────────────────┘
                                                 │
                                                 ↓
┌──────────────────────────────────────────────────────────────┐
│  RLS policy (per table)                                      │
│    USING (workspace_id = current_setting('app.tenant_id'))  │
│  Postgres applies on every query, including raw SQL          │
└──────────────────────────────────────────────────────────────┘
```

1. Middleware reads `request.user` and sets three Postgres session-local vars:
   - `app.user_id` — `request.user.pk`
   - `app.tenant_id` — `request.user.<TURBODRF_TENANT_USER_FIELD>.pk`
   - `app.user_roles` — comma-separated list of the user's roles
2. RLS policies on each table reference these vars in a `USING(...)` clause.
3. Postgres AND's the policy onto every query against that table — ORM, raw
   SQL, admin tools, anything. The DB itself enforces.

---

## Setup

### 1. Install the middleware

```python
# settings.py
MIDDLEWARE = [
    # ... your existing middleware ...
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'turbodrf.rls.TurboDRFTenancyMiddleware',  # AFTER auth
]
```

### 2. Generate draft policies

```bash
python manage.py turbodrf_emit_rls > rls.sql
python manage.py turbodrf_emit_rls --model Project       # single model
```

Sample output:

```sql
-- test_app.Project
ALTER TABLE your_app_project ENABLE ROW LEVEL SECURITY;
CREATE POLICY test_app_deal_tenant_0 ON your_app_project
    USING (workspace_id = current_setting('app.tenant_id')::int);
CREATE POLICY test_app_deal_owner_1 ON your_app_project
    USING (
        (owner_id = current_setting('app.user_id')::int)
        OR current_setting('app.user_roles') ~ E'\m(admin|manager)\M'
    );
```

### 3. Review and migrate

TurboDRF doesn't manage RLS lifecycle. Treat the output as a starting point:

1. Review each policy
2. Apply via Django RunSQL migration:

```python
from django.db import migrations

class Migration(migrations.Migration):
    operations = [
        migrations.RunSQL(
            sql=open('rls.sql').read(),
            reverse_sql='-- write reverse here',
        ),
    ]
```

---

## Predicate → policy mapping

| Predicate | RLS support | Notes |
|---|---|---|
| `Tenant('field')` | ✅ Clean | Simple `WHERE` clause on a FK column |
| `Tenant('a__b__c')` (chained) | ❌ Skipped | RLS doesn't traverse JOINs from a USING clause. Add a Tenant policy on each table along the chain referencing the closest tenant FK column. |
| `Owner('field')` | ✅ Clean | `field_id = current_setting('app.user_id')::int` |
| `Owner('field', bypass=[...])` | ✅ Clean | Adds an OR with `current_setting('app.user_roles') ~ E'\\m(admin\|...)\\M'` |
| `Owner(['a', 'b'])` (multi-owner) | ✅ Clean | OR over each field |
| `Either(...)` | ✅ Clean | OR of children's clauses |
| `Members('m2m')` | ❌ Skipped | Requires EXISTS subquery on the through table; emit doesn't generate it |
| `Group('field')` | ❌ Skipped | Same as Members — write manually |
| `Conditional(...)` | ❌ Skipped | The `when` Q can be arbitrary; write manually |
| `Custom(...)` | ❌ Skipped | Dev's responsibility |

### Manual templates for the messy half

**Members M2M (Slack channel-style):**

```sql
ALTER TABLE channel ENABLE ROW LEVEL SECURITY;
CREATE POLICY channel_membership ON channel
    USING (EXISTS (
        SELECT 1 FROM channel_participants p
        WHERE p.channel_id = channel.id
          AND p.user_id = current_setting('app.user_id')::int
    ));
```

**Group via team membership:**

```sql
ALTER TABLE document ENABLE ROW LEVEL SECURITY;
CREATE POLICY document_team_access ON document
    USING (EXISTS (
        SELECT 1 FROM team_members m
        WHERE m.team_id = document.team_id
          AND m.user_id = current_setting('app.user_id')::int
    ));
```

**Conditional (staff loans visible to special_admin only):**

```sql
ALTER TABLE document ENABLE ROW LEVEL SECURITY;
CREATE POLICY document_member_only ON document
    USING (
        NOT (SELECT is_staff_loan FROM application
             WHERE application.id = document.application_id)
        OR current_setting('app.user_roles') ~ E'\m(special_admin)\M'
    );
```

---

## Caveats

### Connection pooling

`SET LOCAL` only persists for the current comment. If you use **pgbouncer
in comment-pooling mode**, the middleware's `set_config(..., true)` call
is correct (the `true` makes it comment-local). Make sure each request
runs inside a comment:

```python
DATABASES = {
    'default': {
        # ...
        'ATOMIC_REQUESTS': True,  # wrap each view in a comment
    },
}
```

For pgbouncer in **session-pooling mode**, comments and session vars work
normally.

### M2M policy performance

`Members` / `Group` policies use `EXISTS` subqueries against the through table.
For large M2M tables, ensure a composite index on `(user_id, parent_id)`:

```sql
CREATE INDEX channel_participants_user_id_channel_id_idx
    ON channel_participants (user_id, channel_id);
```

Run `EXPLAIN` on representative queries to verify the planner uses the index.

### Migrations

RLS policies aren't tracked by Django's migration framework. Use `RunSQL`
operations explicitly. To re-emit policies after schema changes:

```bash
python manage.py turbodrf_emit_rls > rls_v2.sql
diff rls.sql rls_v2.sql
```

### Postgres superusers bypass RLS

By default, Postgres superusers and table owners bypass RLS entirely, even
with `FORCE ROW LEVEL SECURITY`. Your **production app must connect as a
non-superuser** for RLS to apply:

```sql
CREATE ROLE app_user LOGIN PASSWORD 'secret' NOSUPERUSER;
GRANT CONNECT ON DATABASE myapp TO app_user;
GRANT USAGE ON SCHEMA public TO app_user;
-- Grant ownership of tables OR use FORCE on each table
ALTER TABLE project OWNER TO app_user;
```

Then for owned tables, use `FORCE` if the owner should still be subject to
RLS:

```sql
ALTER TABLE project ENABLE ROW LEVEL SECURITY;
ALTER TABLE project FORCE ROW LEVEL SECURITY;
```

If your migrations run as a superuser/migration role and the application
connects as a different non-superuser, you don't need `FORCE` — the app role
isn't the owner.

### Default-deny when session var is unset

If `app.tenant_id` is not set and the policy is
`USING (workspace_id = current_setting('app.tenant_id')::int)`, the cast
fails. To deny instead of error, use `current_setting('app.tenant_id', true)`
(returns NULL when missing) and let the comparison return NULL → row excluded:

```sql
CREATE POLICY tenant ON project
    USING (workspace_id = current_setting('app.tenant_id', true)::int);
```

The middleware always sets the var, so this path matters only for non-request
contexts (admin scripts, etc.).

### App-layer + RLS together

Both layers apply. The app-layer Q AND's with the RLS policy. They redundantly
enforce the same rules. If an app-layer bug forgets the predicate (e.g. a
custom view using raw SQL), RLS still blocks. If RLS is dropped (a migration
mistake), the app layer still enforces. Belt and braces.

---

## Verifying it works

The TurboDRF test suite includes a Postgres-only integration test that proves
RLS enforces tenant isolation:

```bash
DATABASE_URL=postgres://localhost/test pytest tests/integration/test_rls.py -v
```

The test:
1. Applies a tenant policy on a real Postgres table
2. Sets `app.tenant_id` for tenant A → asserts only A's rows return
3. Switches to tenant B → asserts only B's rows return
4. Bypasses the ORM via raw SQL → confirms RLS still filters
5. Unsets the var → confirms default-deny

If you're running Postgres locally for development, run this test against
your actual database to verify end-to-end.
