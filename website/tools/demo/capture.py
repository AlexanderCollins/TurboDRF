"""Capture real TurboDRF responses for the website.

Runs the demo project (demo/models.py, settings.py) against an in-memory
SQLite database, makes each request the site shows as a real API call, and
writes them to src/data/demo.json: the infographics and the code samples
read from there, so nothing on the site is typed by hand.

    ../../../venv/bin/python capture.py      (from website/tools/demo)
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["DJANGO_SETTINGS_MODULE"] = "settings"

import django  # noqa: E402

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.urls import get_resolver  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

import turbodrf  # noqa: E402
from demo.models import Client, Member, Project, Workspace  # noqa: E402

OUT = HERE.parent.parent / "src" / "data" / "demo.json"


def seed():
    call_command("migrate", run_syncdb=True, verbosity=0)
    acme = Workspace.objects.create(name="Acme")
    globex = Workspace.objects.create(name="Globex")
    users = {}
    for name, role, ws in [
        ("alice", "admin", acme),
        ("maya", "manager", acme),
        ("victor", "viewer", acme),
        ("bob", "admin", globex),
    ]:
        users[name] = Member.objects.create(username=name, role=role, workspace=ws)
    northwind = Client.objects.create(name="Northwind", workspace=acme)
    initech = Client.objects.create(name="Initech", workspace=acme)
    umbrella = Client.objects.create(name="Umbrella", workspace=globex)
    projects = {}
    for title, client, owner, budget in [
        ("Website relaunch", northwind, "victor", "48000.00"),
        ("Mobile app", initech, "maya", "125000.00"),
        ("Data warehouse", northwind, "alice", "310000.00"),
    ]:
        projects[title] = Project.objects.create(
            title=title,
            workspace=acme,
            client=client,
            owner=users[owner],
            budget=Decimal(budget),
        )
    projects["Payments API"] = Project.objects.create(
        title="Payments API",
        workspace=globex,
        client=umbrella,
        owner=users["bob"],
        budget=Decimal("90000.00"),
    )
    return users, projects, {"acme": acme, "globex": globex}, umbrella


def call(client, user, method, url, data=None, host="localhost:8000"):
    client.force_authenticate(user=user)
    with CaptureQueriesContext(connection) as queries:
        resp = getattr(client, method.lower())(url, data, format="json", HTTP_HOST=host)
    body = None
    if resp.content:
        try:
            body = json.loads(resp.content)
        except ValueError:
            body = resp.content.decode()
    return {
        "user": user.username,
        "role": user.role,
        "workspace": user.workspace.name,
        "method": method,
        "url": url,
        "body": data,
        "status": resp.status_code,
        "response": body,
        "queries": len(queries),
    }


def call_anon(method, url, data=None):
    resp = getattr(APIClient(), method.lower())(url, data, format="json", HTTP_HOST="localhost:8000")
    return {
        "user": None,
        "role": None,
        "workspace": None,
        "method": method,
        "url": url,
        "body": data,
        "status": resp.status_code,
        "response": json.loads(resp.content) if resp.content else None,
    }


def matrix(api, users, projects, clients):
    """Every caller x every action, against the same rows: the site's permission
    grid. Runs in a transaction that's rolled back, so the other captures see
    the original data."""
    from django.db import transaction

    callers = ["alice", "maya", "victor", "bob", None]
    with transaction.atomic():
        spare = Project.objects.create(
            title="Old prototype",
            workspace=users["alice"].workspace,
            client=clients["northwind"],
            owner=users["alice"],
            budget=Decimal("5000.00"),
        )
        rows = [
            {
                "id": p.pk,
                "title": p.title,
                "workspace": p.workspace.name,
                "owner": p.owner.username,
                "budget": str(p.budget),
            }
            for p in Project.objects.select_related("workspace", "owner").order_by("pk")
        ]
        p1 = projects["Website relaunch"].pk
        p2 = projects["Mobile app"].pk
        actions = [
            ("list", "GET", "/api/projects/", lambda who: None),
            ("read_p1", "GET", f"/api/projects/{p1}/", lambda who: None),
            ("read_p2", "GET", f"/api/projects/{p2}/", lambda who: None),
            ("edit_status", "PATCH", f"/api/projects/{p1}/", lambda who: {"status": "review"}),
            ("edit_budget", "PATCH", f"/api/projects/{p1}/", lambda who: {"budget": "99.00"}),
            (
                "create_foreign",
                "POST",
                "/api/projects/",
                lambda who: {
                    "title": "Sneaky",
                    "client": (clients["northwind"] if who == "bob" else clients["umbrella"]).pk,
                    "budget": "1.00",
                },
            ),
            ("delete", "DELETE", f"/api/projects/{spare.pk}/", lambda who: None),
        ]
        grid = {}
        for key, method, url, body in actions:
            # Writes that succeed go last, so every caller acts on the same row.
            order = ["maya", "victor", "bob", None, "alice"] if method != "GET" else callers
            for who in order:
                data = body(who)
                if who is None:
                    cell = call_anon(method, url, data)
                else:
                    cell = call(api, users[who], method, url, data)
                grid.setdefault(key, {})[who or "anonymous"] = cell
        transaction.set_rollback(True)
    return {
        "callers": [c or "anonymous" for c in callers],
        "rows": rows,
        "actions": grid,
        "spare_pk": spare.pk,
    }


def routes():
    out = []
    for pattern in get_resolver().url_patterns[0].url_patterns:
        route = str(pattern.pattern)
        if "format" in route or not route or route == "^$":
            continue
        callback = pattern.callback
        actions = getattr(callback, "actions", {}) or {}
        methods = [m.upper() for m in actions] if actions else []
        out.append({"route": "/api/" + route.lstrip("^").rstrip("$"), "methods": methods})
    return out


def command(*args):
    from io import StringIO

    out = StringIO()
    call_command(*args, stdout=out, no_color=True)
    return out.getvalue().rstrip()


def startup_error():
    """What the router says about a model with no tenancy decision."""
    from django.core.exceptions import ImproperlyConfigured
    from django.db import models

    from turbodrf.mixins import TurboDRFMixin
    from turbodrf.router import TurboDRFRouter

    class Invoice(models.Model, TurboDRFMixin):
        number = models.CharField(max_length=20)
        workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)

        class Meta:
            app_label = "demo"

        @classmethod
        def turbodrf(cls):
            return {"fields": ["number"]}

    try:
        TurboDRFRouter()
    except ImproperlyConfigured as exc:
        return str(exc)
    finally:
        from django.apps import apps

        del apps.all_models["demo"]["invoice"]
        apps.clear_cache()
    return None


def main():
    users, projects, ws, umbrella = seed()
    api = APIClient()
    clients = {c.name.lower(): c for c in Client.objects.all()}
    grid = matrix(api, users, projects, clients)
    acme_pk = projects["Website relaunch"].pk
    mobile_pk = projects["Mobile app"].pk
    calls = {
        "list_admin": call(api, users["alice"], "GET", "/api/projects/"),
        "list_manager": call(api, users["maya"], "GET", "/api/projects/"),
        "list_viewer": call(api, users["victor"], "GET", "/api/projects/"),
        "list_other_tenant": call(api, users["bob"], "GET", "/api/projects/"),
        "detail_admin": call(api, users["alice"], "GET", f"/api/projects/{acme_pk}/"),
        "detail_manager": call(api, users["maya"], "GET", f"/api/projects/{acme_pk}/"),
        "detail_viewer": call(api, users["victor"], "GET", f"/api/projects/{acme_pk}/"),
        "list_admin_page1": call(api, users["alice"], "GET", "/api/projects/?page_size=1"),
        "detail_other_tenant": call(
            api, users["bob"], "GET", f"/api/projects/{acme_pk}/"
        ),
        "fields_viewer": call(
            api, users["victor"], "GET", "/api/projects/?fields=title,budget"
        ),
        "search_admin": call(api, users["alice"], "GET", "/api/projects/?search=app"),
        "filter_admin": call(
            api, users["alice"], "GET", "/api/projects/?budget__gte=100000&ordering=-budget"
        ),
        "patch_viewer": call(
            api, users["victor"], "PATCH", f"/api/projects/{acme_pk}/", {"title": "Mine now"}
        ),
        "patch_manager_budget": call(
            api, users["maya"], "PATCH", f"/api/projects/{mobile_pk}/", {"budget": "1.00"}
        ),
        "patch_manager_status": call(
            api, users["maya"], "PATCH", f"/api/projects/{mobile_pk}/", {"status": "paused"}
        ),
        "create_admin": call(
            api,
            users["alice"],
            "POST",
            "/api/projects/",
            {
                "title": "Partner portal",
                "client": projects["Website relaunch"].client_id,
                "budget": "64000.00",
            },
        ),
        "create_cross_tenant_fk": call(
            api,
            users["alice"],
            "POST",
            "/api/projects/",
            {"title": "Sneaky", "client": umbrella.pk, "budget": "1.00"},
        ),
        "delete_manager": call(
            api, users["maya"], "DELETE", f"/api/projects/{mobile_pk}/"
        ),
    }
    data = {
        "turbodrf_version": turbodrf.__version__,
        "users": {
            u.username: {"role": u.role, "workspace": u.workspace.name}
            for u in users.values()
        },
        "routes": routes(),
        "matrix": grid,
        "check": command("turbodrf_check"),
        "startup_error": startup_error(),
        "calls": calls,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Wrote {OUT}")
    for name, c in calls.items():
        print(f"{name:24} {c['status']}  q={c['queries']}  {json.dumps(c['response'])[:150]}")
    for key, cells in grid["actions"].items():
        print(f"{key:15}", "  ".join(f"{who}={c['status']}" for who, c in cells.items()))
    print(json.dumps(data["routes"]))
    print(data["check"])
    print(data["startup_error"])


if __name__ == "__main__":
    main()
