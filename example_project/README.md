# TurboDRF example project

A small bookstore API: two stores (the tenants), three roles, and field rules
on `price`. Everything TurboDRF-specific is in `books/models.py` and the
bottom of `source/settings.py`.

## Run it

From this directory, with TurboDRF installed (`pip install turbodrf`, or
`pip install -e ..` from a checkout):

```bash
python manage.py migrate
python manage.py seed        # stores, users and books; safe to re-run
python manage.py runserver
```

`seed` creates these users, each with the password `<username>-pass`:

| User | Role | Store |
|---|---|---|
| `admin` | admin | Riverside Books |
| `editor` | editor | Riverside Books |
| `viewer` | viewer | Riverside Books |
| `other_editor` | editor | Hillside Books |

## Try it

```bash
# A viewer sees Riverside's books, without prices (price has field rules).
curl -u viewer:viewer-pass http://127.0.0.1:8000/api/books/

# An editor sees prices and can add a book; the store is filled in for them.
curl -u editor:editor-pass -H 'Content-Type: application/json' \
  -d '{"title": "The Left Hand of Darkness", "author": 1, "price": "11.00"}' \
  http://127.0.0.1:8000/api/books/

# Another store's editor can't see Riverside's books: 404.
curl -u other_editor:other_editor-pass http://127.0.0.1:8000/api/books/1/

# A viewer can review a book from their store; the reviewer is filled in.
curl -u viewer:viewer-pass -H 'Content-Type: application/json' \
  -d '{"book": 1, "rating": 5}' http://127.0.0.1:8000/api/reviews/
```

The Swagger UI at <http://127.0.0.1:8000/api/swagger/> lists only what the
caller may do.

## What it shows

- **Tenancy**: `Book` has `tenant_field: 'store'`, `Review` reaches the store
  through its book (`'book__store'`), and `Author` is shared reference data.
  `TURBODRF_TENANT_MODEL` / `TURBODRF_TENANT_USER_FIELD` point at the store
  on the user.
- **Roles**: the custom user model's `roles` property returns its `role`.
- **Field rules**: only roles granted `books.book.price.read` see the price.
  The editor creates books and `price` is required, so it's also granted
  `books.book.price.write`.
- **Ownership**: reviews use `owner_field: 'reviewer'`; admins bypass it.
