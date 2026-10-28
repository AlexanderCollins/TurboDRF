# Performance

## Queries per request

A list request makes the same number of queries whatever the page size:

- a `COUNT` for the pagination envelope, and the page's `SELECT`. The `SELECT` joins the single-valued relations that nested fields read (`author__name`) and works out, in the same query, whether the caller can see each joined row;
- one query per to-many field on the page (`tags`, `tags__name`, a reverse relation), loading only the related rows the caller can see;
- for `@property` fields, one query for the page's rows plus one per relation hop declared in `property_relations` (see [the property sandbox](configuration.md#the-property-sandbox)).

Permission snapshots come from the cache: one read per model the request touches. In database permission mode the request also reads the caller's roles once.

## Indexes for tenant-scoped lists

A list on a tenant-scoped model filters by the tenant FK and sorts by the model's `Meta.ordering` (or the primary key when it has none). The index Django creates on the FK doesn't cover the sort, so on a large tenant the database walks the whole sort index to find one page. Add a composite index that leads with the tenant FK:

```python
class Project(models.Model, TurboDRFMixin):
    workspace = models.ForeignKey('accounts.Workspace', on_delete=models.CASCADE)
    ...

    class Meta:
        indexes = [models.Index(fields=['workspace', 'id'])]
```

The database then reads one page of the tenant's rows from the index, already in order. If the model has a `Meta.ordering`, or clients often sort with `?ordering=`, index `['workspace', <that field>]` as well.

## Fast JSON rendering

TurboDRF auto-detects the fastest available JSON library:

| Library | Speed vs stdlib | Install |
|---|---|---|
| msgspec | ~7x faster | `pip install "turbodrf[fast]"` |
| orjson | ~5x faster | `pip install orjson` |
| stdlib json | baseline | built-in |

No configuration needed -- install and it's used automatically.
