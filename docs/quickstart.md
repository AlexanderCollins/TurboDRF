# Quick start

This builds a small book API in a new Django project: two models, two roles,
and one user per role. It assumes a project created with
`django-admin startproject mysite .` and an app created with
`python manage.py startapp shop`.

## 1. Install

```bash
pip install turbodrf
```

This also installs Django REST Framework, django-filter and drf-yasg. See
[Installation](installation.md) for the optional extras.

## 2. Settings

```python
# mysite/settings.py
INSTALLED_APPS = [
    # ... Django's default apps ...
    'rest_framework',
    'drf_yasg',      # serves the Swagger / ReDoc pages
    'turbodrf',
    'shop',
]

TURBODRF_ROLES = {
    'editor': [
        'shop.author.read', 'shop.author.create', 'shop.author.update', 'shop.author.delete',
        'shop.book.read', 'shop.book.create', 'shop.book.update', 'shop.book.delete',
    ],
    'viewer': [
        'shop.author.read',
        'shop.book.read',
    ],
}
```

Every request needs a role that grants the action. An authenticated user with
no role gets a 403 on every endpoint, and so does an anonymous caller (unless
the model sets `public_access`). A nested field such as `author__name` also
needs read on the related model (`shop.author.read`); without it the field is
left out of the response.

The API docs pages need `drf_yasg` in `INSTALLED_APPS`. If you don't want
them, leave it out and set `TURBODRF_ENABLE_DOCS = False`; with neither,
Django's system check warns (`turbodrf.W001`).

## 3. Models

```python
# shop/models.py
from django.db import models

from turbodrf.mixins import TurboDRFMixin


class Author(models.Model, TurboDRFMixin):
    name = models.CharField(max_length=100)
    email = models.EmailField()

    @classmethod
    def turbodrf(cls):
        return {'fields': ['id', 'name', 'email']}


class Book(models.Model, TurboDRFMixin):
    title = models.CharField(max_length=200)
    author = models.ForeignKey(Author, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=10, decimal_places=2)

    @classmethod
    def turbodrf(cls):
        return {
            'fields': {
                'list': ['id', 'title', 'author', 'author__name', 'price'],
                'detail': ['id', 'title', 'author', 'author__name', 'author__email', 'price'],
            },
            'searchable_fields': ['title'],
        }
```

`fields` is what the API exposes. Always set it: it defaults to `'__all__'`.
The primary key renders only if it's listed, and `author` (the raw FK id) is
what clients send when they create a book.

## 4. Give users roles

TurboDRF reads `user.roles`, a list of role names. Here they're the names of
the user's Django groups:

```python
# shop/apps.py
from django.apps import AppConfig


class ShopConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'shop'

    def ready(self):
        from django.contrib.auth.models import User

        # TurboDRF reads user.roles. Here: the names of the user's groups.
        User.add_to_class(
            'roles', property(lambda user: [g.name for g in user.groups.all()])
        )
```

A custom user model can define `roles` as a property instead, or roles can
live in the database. See
[Assigning roles to users](permissions.md#assigning-roles-to-users).

## 5. URLs

```python
# mysite/urls.py
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('turbodrf.urls')),
]
```

`turbodrf.urls` registers every TurboDRF model, plus `/api/swagger/` and
`/api/redoc/` when the docs are enabled. (To mount only the API, include
`TurboDRFRouter().urls` from `turbodrf.router` instead.)

## 6. Migrate, create users, run

```bash
python manage.py makemigrations shop
python manage.py migrate
python manage.py shell -c "
from django.contrib.auth.models import Group, User
alice = User.objects.create_user('alice', password='alice-pass')
alice.groups.add(Group.objects.get_or_create(name='editor')[0])
bob = User.objects.create_user('bob', password='bob-pass')
bob.groups.add(Group.objects.get_or_create(name='viewer')[0])
"
python manage.py check
python manage.py runserver
```

`check` imports the URLconf, which runs TurboDRF's startup checks: a
mistake such as a misspelled permission string fails here, not on the first
request.

## 7. Make requests

DRF's default authentication classes accept HTTP Basic auth, so `curl -u`
works for trying things out:

```bash
curl -u alice:alice-pass -H 'Content-Type: application/json' \
  -d '{"name": "Ursula K. Le Guin", "email": "ursula@example.com"}' \
  http://127.0.0.1:8000/api/authors/
# {"id":1,"name":"Ursula K. Le Guin","email":"ursula@example.com"}

curl -u alice:alice-pass -H 'Content-Type: application/json' \
  -d '{"title": "The Dispossessed", "author": 1, "price": "12.50"}' \
  http://127.0.0.1:8000/api/books/
# {"id":1,"title":"The Dispossessed","author":1,"price":"12.50","author_name":"Ursula K. Le Guin","author_email":"ursula@example.com"}

curl -u bob:bob-pass http://127.0.0.1:8000/api/books/
# {"pagination":{...,"total_items":1},"data":[{"id":1,"title":"The Dispossessed","author":1,"price":"12.50","author_name":"Ursula K. Le Guin"}]}

curl -u bob:bob-pass -X DELETE http://127.0.0.1:8000/api/books/1/
# 403: the viewer role has no shop.book.delete
```

Each model gets these routes:

```
GET    /api/books/                       list (paginated, 20 per page)
POST   /api/books/                       create
GET    /api/books/1/                     detail
PUT    /api/books/1/                     update
PATCH  /api/books/1/                     partial update
DELETE /api/books/1/                     delete
GET    /api/books/?search=dispossessed   search the searchable_fields
GET    /api/books/?price__lt=20          filter
GET    /api/books/?ordering=-price       order
GET    /api/books/?fields=title,price    choose fields
```

The endpoint is the lowercase model name plus `s`; set `'endpoint'` in
`turbodrf()` to change it.

## Rows aren't scoped yet

So far, every user whose role grants `shop.book.read` sees every book. Roles
decide which actions and fields a user gets, not which rows. TurboDRF scopes
rows once a model declares how: `owner_field` for rows that belong to one
user, `tenant_field` for data that belongs to a customer, team or workspace.
Read [Tenancy & row-level access](tenancy.md) before you store data that one
user mustn't see.

## Next steps

- [Configuration](configuration.md): every `turbodrf()` option, including
  custom endpoints (`actions`).
- [Permissions](permissions.md): field-level rules, such as hiding `price`
  from viewers.
- [Tenancy & row-level access](tenancy.md): owner and tenant scoping.
- [Security](security.md): what TurboDRF protects against and what it
  doesn't.
- [example_project](https://github.com/AlexanderCollins/TurboDRF/tree/main/example_project):
  a store-scoped version of this API with field rules, to run locally.
