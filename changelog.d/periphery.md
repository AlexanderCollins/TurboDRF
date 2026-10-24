### Security

- **The OpenAPI schema documented columns the API doesn't serve.** With a
  `fields` dict that has only one key (e.g. `{'list': [...]}`), the schema
  resolved the missing view type as `"__all__"`, while the API uses the other
  key. The retrieve response and the write bodies then listed every column
  with no read rule (e.g. `secret_field`, `price`, `quantity`) to every
  reader, anonymous callers included. The schema, OPTIONS
  (`get_api_fields()`) and the management commands now share the viewset's
  resolution.
- **drf-api-tracking logged secrets in clear.**
  `TURBODRF_SENSITIVE_FIELDS = ['ssn']` replaced the built-in deny-list
  instead of adding to it, so `api_key`, `refresh_token`, `password_hash`,
  ... were logged unmasked. And a request rejected before DRF parsed it (401,
  403, throttled, malformed JSON) was logged with its raw body: those bodies
  are now masked too (JSON is parsed and masked; any other body that
  mentions a sensitive name is replaced entirely).
- **`AllAuthRoleMiddleware` / `KeycloakRoleMiddleware` gave session requests
  different roles from token requests.** They wrote integration roles into
  `request.user.__dict__`, shadowing the user model's own `roles` for session
  requests only. Both are now deprecated no-ops (see Changed).
- **Keycloak:** an `auth_time` of `nan` / `inf` passed the
  `TURBODRF_KEYCLOAK_MAX_ROLE_AGE` check; it now counts as unusable and the
  roles are dropped. A non-string entry in the role claim (an object, a
  number) raised `TypeError` (a 500 for that user); only string roles are
  kept.

### Fixed

- The schema documented every path twice (`/items/` and `/items`, each with
  every operation), because the router's no-trailing-slash routes were never
  filtered. Only the trailing-slash form is documented.
- `"__all__"` now includes forward many-to-many fields in the schema's write
  bodies and in OPTIONS, as the API does.
- The list response envelope used OpenAPI 3's `nullable` inside a Swagger 2
  document; `pagination.next` / `previous` are now `x-nullable`.
- `@swagger_auto_schema(request_body=...)` is honoured on create / update and
  custom actions, and a custom action's `@action(serializer_class=...)` is
  documented as its request body.
- The schema no longer publishes example.com terms-of-service and contact
  placeholders; `get_turbodrf_schema_view()` leaves them out unless given.
- `turbodrf_check` reported an `owner_field`-only model as OK though the
  router refuses it, listed every column for a detail-only `fields` dict,
  printed `[compiled] OK` for a model the compiler blocks, and counted a
  reverse-FK path as an FK.
- `turbodrf_explain` printed `Compiled: False` for models the router
  compiles, under-counted list queries (it left out the COUNT), reported
  pruned / permitted fields that didn't match the API's response, printed
  nothing for a role with no readable fields, ignored `--fields`, and could
  leave a model's `turbodrf()` patched if compiling raised.
- `turbodrf_benchmark` crashed on any model with a nested list field and
  assumed a manager named `objects`.
- `allauth_roles` helpers (`get_users_with_role`, `sync_groups_to_roles`,
  `assign_roles_to_user`, `get_or_create_role_group`, `create_role_groups`)
  ignored `TURBODRF_ALLAUTH_ROLE_MAPPING` and strict mode. They now go
  through the mapping both ways; a role no group can give raises
  `ValueError` instead of silently assigning nothing.
- `SentryContextMiddleware` resolved the user, roles and tenant on every
  request (2-3 queries even on a health check). It now only registers the
  event processor, which resolves them when an event is captured.
  `capture_security_message` no longer uses the deprecated `push_scope()`.
- `'searchable_fields': []` in the `turbodrf()` config now turns search off
  instead of falling back to the class attribute.
- Tracking is enabled only when `rest_framework_tracking` is in
  `INSTALLED_APPS`; before, an importable but uninstalled package made
  `turbodrf.views` fail to import.
- Importing any `turbodrf.integrations.*` module no longer imports the
  allauth helpers and Django's auth models.

### Changed

- `AllAuthRoleMiddleware` and `KeycloakRoleMiddleware` are deprecated and do
  nothing (they warn once at startup). The roles resolver TurboDRF installs
  on the user model when an integration is enabled covers session, token and
  JWT requests alike.
- `turbodrf_benchmark` now requires `--user <username>` and times real list
  requests as that user through the model's registered viewset (permissions,
  scoping, filters, pagination and rendering included), on the compiled and
  the serializer path. A model the router doesn't compile gets the serializer
  path only.
- `turbodrf_explain --role` is supported in static permission mode only (it
  used the static snapshot in database mode too, which was wrong).
- Removed `turbodrf/user_extensions.py`, an unused example module whose
  concrete models broke a project's model checks when imported.
- Removed `TurboDRFMixin.get_field_type()`, which TurboDRF never used.
- `turbodrf.settings` keeps only the fallbacks TurboDRF imports
  (`TURBODRF_ROLES`, `TURBODRF_SENSITIVE_FIELDS`, `TURBODRF_MAX_NESTING_DEPTH`);
  the tenancy entries, which nothing read, are gone.

### Upgrading

- Remove `turbodrf.integrations.allauth.AllAuthRoleMiddleware` and
  `turbodrf.integrations.keycloak.KeycloakRoleMiddleware` from `MIDDLEWARE`.
  If your user model defines its own `roles`, that is now what every request
  gets: the middleware no longer overrides it with group / Keycloak roles on
  session requests.
- Add `--user <username>` to `turbodrf_benchmark` invocations.
- With a single-key `fields` dict or `"__all__"`, the schema's bodies change
  to match the API (fewer columns, M2M fields added). Clients generated from
  the old schema may need regenerating.
- If you called `assign_roles_to_user()` / `get_or_create_role_group()` with
  a role your mapping doesn't give, they now raise `ValueError`.
- Code importing `TURBODRF_TENANT_MODEL`, `TURBODRF_TENANT_USER_FIELD`,
  `TURBODRF_REQUIRE_TENANCY` or `TURBODRF_AUTODETECT_TENANT` from
  `turbodrf.settings`, `turbodrf.user_extensions`, or calling
  `get_field_type()` must stop doing so.
- `TURBODRF_KEYCLOAK_MAX_ROLE_AGE` is measured from social-auth's
  `auth_time`, which `UserSocialAuth.refresh_token()` resets while keeping the
  old role claims; if your app refreshes tokens that way the limit doesn't
  bound role age.
