"""
Print the schema the docs view serves a caller holding exactly the given
roles, or an anonymous one, with no database user:

    python manage.py turbodrf_schema --role editor [--role viewer]
        [--scope platform] [--format yaml] [--output schema.yaml]
    python manage.py turbodrf_schema --anonymous
"""

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.management.base import BaseCommand, CommandError
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from turbodrf.backends import RoleOnlyIdentity, get_permission_mode
from turbodrf.scope import PLATFORM, SCOPES, TENANT


def swagger2_schema(caller, scope, fmt):
    """The Swagger 2 document (drf-yasg) the docs view of ``scope`` serves
    ``caller``, as JSON or YAML text: the one function an OpenAPI 3
    generator would replace."""
    from drf_yasg import openapi
    from drf_yasg.app_settings import swagger_settings
    from drf_yasg.codecs import OpenAPICodecJson, OpenAPICodecYaml

    from turbodrf.swagger import PlatformSchemaGenerator, RoleBasedSchemaGenerator

    base = PlatformSchemaGenerator if scope == PLATFORM else RoleBasedSchemaGenerator

    class Generator(base):
        def create_view(self, callback, method, request=None):
            view = super().create_view(callback, method, request)
            # A router's gate checks who the caller is, which a list of roles
            # doesn't say: it passes, as for anyone the gated docs view serves.
            if isinstance(caller, RoleOnlyIdentity) and getattr(
                view, "turbodrf_gate", ()
            ):
                view.turbodrf_gate = (AllowAny,)
            return view

    # get_turbodrf_schema_view's defaults. No host (there's no request to take
    # it from) unless SWAGGER_SETTINGS['DEFAULT_API_URL'] names one.
    info = openapi.Info(
        title="TurboDRF API",
        default_version="v1",
        description="Auto-generated API with role-based access control powered "
        "by TurboDRF",
    )
    generator = Generator(info, url=swagger_settings.DEFAULT_API_URL or "")
    request = Request(APIRequestFactory().get("/"))
    request.user = caller
    schema = generator.get_schema(request, public=False)
    if fmt == "yaml":
        return OpenAPICodecYaml(validators=[]).encode(schema).decode()
    return OpenAPICodecJson(validators=[], pretty=True).encode(schema).decode()


def _known_roles(scope):
    if scope == PLATFORM:
        return set(getattr(settings, "TURBODRF_PLATFORM_ROLES", None) or ())
    if get_permission_mode() == "database":
        from turbodrf.models import TurboDRFRole

        return set(TurboDRFRole.objects.values_list("name", flat=True))
    return set(getattr(settings, "TURBODRF_ROLES", None) or ())


class Command(BaseCommand):
    help = "Print the API schema a caller with the given roles gets from the docs"

    def add_arguments(self, parser):
        caller = parser.add_mutually_exclusive_group(required=True)
        caller.add_argument(
            "--role", action="append", dest="roles", help="A role (repeatable)"
        )
        caller.add_argument("--anonymous", action="store_true")
        parser.add_argument("--scope", choices=SCOPES, default=TENANT)
        parser.add_argument("--format", choices=("json", "yaml"), default="json")
        parser.add_argument("--output", help="Write to this file, not stdout")

    def handle(self, *args, **options):
        scope, roles = options["scope"], options["roles"]
        unknown = sorted(set(roles or ()) - _known_roles(scope))
        if unknown:
            known = ", ".join(sorted(_known_roles(scope))) or "none"
            raise CommandError(
                f"Unknown {scope} role(s): {', '.join(unknown)}. Known: {known}."
            )
        caller = RoleOnlyIdentity(roles) if roles else AnonymousUser()
        text = swagger2_schema(caller, scope, options["format"])
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as stream:
                stream.write(text)
            self.stderr.write(f"Wrote {options['output']}")
        else:
            self.stdout.write(text)
