# Installation

## Requirements

- Python 3.10+
- Django 4.2+
- Django REST Framework 3.14+

`pyproject.toml` is the source of truth for supported versions.

## Install

```bash
pip install turbodrf
```

This also installs Django REST Framework, django-filter and drf-yasg.
Optional extras (quote them, or zsh expands the brackets):

```bash
pip install "turbodrf[fast]"      # msgspec: faster JSON rendering
pip install "turbodrf[allauth]"   # django-allauth group-to-role mapping (experimental)
```

From GitHub:

```bash
pip install git+https://github.com/AlexanderCollins/TurboDRF.git
```

To work on TurboDRF itself, see
[CONTRIBUTING.md](https://github.com/AlexanderCollins/TurboDRF/blob/main/CONTRIBUTING.md).

## Set up a project

The [Quick start](quickstart.md) builds a working project step by step. In
short:

1. Add `'rest_framework'`, `'drf_yasg'` and `'turbodrf'` to
   `INSTALLED_APPS`. `drf_yasg` serves the Swagger / ReDoc pages; without
   them, leave it out and set `TURBODRF_ENABLE_DOCS = False` (with neither,
   Django's system check warns `turbodrf.W001`).
2. Define `TURBODRF_ROLES` and give users roles through `user.roles` (see
   [Assigning roles to users](permissions.md#assigning-roles-to-users)).
3. Add `path('api/', include('turbodrf.urls'))` to your URLconf.
4. Add `TurboDRFMixin` and a `turbodrf()` classmethod to each model the API
   should expose ([Configuration](configuration.md)), and decide how its
   rows are scoped ([Tenancy](tenancy.md)).

Run `python manage.py check` in CI and before each deploy: it imports the
URLconf, which runs TurboDRF's startup checks (see
[Management commands](commands.md)).

## Troubleshooting

**Every request is a 403.** The user has no role, or no role grants the
action. Check `user.roles` and `TURBODRF_ROLES`. Anonymous callers get a 403
unless the model sets `public_access: True`, and then only for reads.

**A nested field (`author__name`) is missing.** The caller needs read on the
related model too (`myapp.author.read`).

**A model has no endpoint.** Check that it inherits `TurboDRFMixin`, defines
`turbodrf()`, and doesn't set `'enabled': False`. The endpoint is the
lowercase model name plus `s` (`/api/books/`) unless `'endpoint'` is set.

**Users see each other's rows.** Roles don't scope rows. Declare
`owner_field`, `tenant_field` or `visibility` (see [Tenancy](tenancy.md)).

## Uninstall

```bash
pip uninstall turbodrf
```

Then remove `'turbodrf'` from `INSTALLED_APPS` and the TurboDRF imports from
your code.
