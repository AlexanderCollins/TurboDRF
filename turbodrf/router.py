"""
Automatic router for TurboDRF.

This module provides the TurboDRFRouter that automatically discovers
and registers all models with TurboDRFMixin.
"""

from django.apps import apps
from django.urls import re_path
from rest_framework.routers import DefaultRouter

from .mixins import TurboDRFMixin
from .views import TurboDRFViewSet


def _walk_predicates(predicates):
    """Yield every predicate (recursing into Either's children)."""
    from .predicates import Either

    for p in predicates:
        yield p
        if isinstance(p, Either):
            yield from _walk_predicates(p.predicates)


# Process-level flag: once the bypass-role validation passes for the real
# settings on first init, subsequent router inits (typically under
# override_settings during tests) skip the re-check. Production startup
# hits this once with the real TURBODRF_ROLES; tests with partial role
# overrides won't trip the guard against the model's static bypass list.
_bypass_roles_validated = False


class TurboDRFRouter(DefaultRouter):
    """
    Router that auto-discovers and registers TurboDRF models.

    This router extends DRF's DefaultRouter to automatically discover all
    Django models that inherit from TurboDRFMixin and register them as
    API endpoints. No manual registration is required.

    Features:
        - Automatic model discovery on initialization
        - Dynamic ViewSet generation for each model
        - Respects model configuration (enabled/disabled, custom endpoints)
        - Inherits all DefaultRouter functionality (browsable API, format suffixes)
        - Handles both trailing and non-trailing slash URLs

    Example:
        >>> # In your urls.py
        >>> from django.urls import path, include
        >>> from turbodrf.router import TurboDRFRouter
        >>>
        >>> router = TurboDRFRouter()
        >>>
        >>> urlpatterns = [
        ...     path('api/', include(router.urls)),
        ... ]

    This will automatically create endpoints for all TurboDRF-enabled models:
        - /api/books/ and /api/books
        - /api/authors/ and /api/authors
        - /api/categories/ and /api/categories
        etc.
    """

    def __init__(self, *args, **kwargs):
        """
        Initialize the router and discover models.

        Args:
            *args: Positional arguments passed to DefaultRouter.
            **kwargs: Keyword arguments passed to DefaultRouter.
        """
        super().__init__(*args, **kwargs)
        self.discover_models()

    def discover_models(self):
        """
        Discover all models with TurboDRFMixin and register them.

        This method iterates through all registered Django models and
        automatically registers those that inherit from TurboDRFMixin
        and are enabled in their configuration.

        The method:
        1. Finds all models inheriting from TurboDRFMixin
        2. Checks if the model is enabled (via turbodrf() config)
        3. Validates field nesting depth
        4. Resolves tenancy (sugar → predicates, auto-detection, hard-fail)
        5. Creates a dynamic ViewSet for the model
        6. Registers the ViewSet with the appropriate endpoint

        Models can customize their endpoint name via the 'endpoint' key
        in their turbodrf() configuration. If not specified, the endpoint
        defaults to the pluralized model name.
        """
        import logging

        from django.conf import settings as django_settings
        from django.core.exceptions import ImproperlyConfigured

        from .predicates import (
            Owner,
            has_tenancy_declaration,
            register_predicates,
            register_tenant_field,
        )
        from .tenancy import resolve_tenancy_for_model
        from .validation import validate_nesting_depth

        logger = logging.getLogger(__name__)

        tenant_model_setting = getattr(django_settings, "TURBODRF_TENANT_MODEL", None)
        require_tenancy = getattr(django_settings, "TURBODRF_REQUIRE_TENANCY", True)
        autodetect = getattr(django_settings, "TURBODRF_AUTODETECT_TENANT", False)
        known_roles = set(getattr(django_settings, "TURBODRF_ROLES", {}).keys())

        for model in apps.get_models():
            if issubclass(model, TurboDRFMixin):
                config = model.turbodrf()

                if config.get("enabled", True):
                    # Validate nesting depth for configured fields
                    fields = config.get("fields", [])
                    if isinstance(fields, dict):
                        # Check both list and detail fields
                        all_fields = []
                        all_fields.extend(fields.get("list", []))
                        all_fields.extend(fields.get("detail", []))
                        fields = all_fields
                    elif fields == "__all__":
                        fields = []  # Skip validation for __all__

                    # Validate each field
                    for field in fields:
                        try:
                            validate_nesting_depth(field)
                        except Exception as e:
                            logger.warning(
                                f"Model {model.__name__} field '{field}' "
                                f"validation failed: {str(e)}"
                            )

                    # ---------------------------------------------------
                    # Resolve tenancy: returns (tenant_field, predicates,
                    # autodetected). Tenant is a SETTING separate from the
                    # predicate algebra so OR-composition cannot escape it.
                    # ---------------------------------------------------
                    tenant_field, predicates, autodetected = resolve_tenancy_for_model(
                        model,
                        config,
                        tenant_model_setting,
                        autodetect=autodetect,
                    )

                    if (
                        require_tenancy
                        and tenant_model_setting is not None
                        and tenant_field is None
                        and not predicates
                        and not has_tenancy_declaration(config)
                        and not autodetected
                    ):
                        raise ImproperlyConfigured(
                            f"{model.__name__}.turbodrf() declares no tenancy "
                            f"and no tenant FK could be auto-detected to "
                            f"{tenant_model_setting}. Add one of: "
                            f"'tenant_field': '<path>', "
                            f"'visibility': [...], or "
                            f"'tenancy': 'shared' (for reference data). "
                            f"Set TURBODRF_REQUIRE_TENANCY=False to disable "
                            f"this check."
                        )

                    # Validate bypass roles against TURBODRF_ROLES.
                    # Runs once per process at first init. Subsequent
                    # inits (typically under override_settings in tests)
                    # skip this — the real config has already been
                    # checked, and partial role overrides shouldn't
                    # spuriously fail the typo guard.
                    global _bypass_roles_validated
                    if known_roles and not _bypass_roles_validated:
                        for pred in _walk_predicates(predicates):
                            if isinstance(pred, Owner) and pred.bypass:
                                unknown = pred.bypass - known_roles
                                if unknown:
                                    raise ImproperlyConfigured(
                                        f"{model.__name__}.turbodrf() declares "
                                        f"bypass_owner_roles={sorted(pred.bypass)} "
                                        f"but {sorted(unknown)} are not in "
                                        f"TURBODRF_ROLES. Typo or stale config?"
                                    )

                    register_tenant_field(model, tenant_field)
                    register_predicates(model, predicates)
                    if autodetected:
                        logger.info(
                            f"Auto-detected tenant path for {model.__name__}: "
                            f"{tenant_field}"
                        )

                    # Get custom endpoint or use default
                    endpoint = config.get("endpoint", f"{model._meta.model_name}s")

                    # Get lookup field if specified
                    lookup_field = config.get("lookup_field", None)

                    # Build viewset attributes
                    viewset_attrs = {
                        "model": model,
                        "queryset": model.objects.all(),
                        "_predicates": predicates,
                        "_tenant_field": tenant_field,
                        "__module__": model.__module__,
                        "__doc__": (
                            f"Auto-generated ViewSet for {model.__name__} model."
                        ),
                    }

                    # Add lookup_field if specified
                    if lookup_field:
                        viewset_attrs["lookup_field"] = lookup_field

                    # Create a custom viewset for this model
                    viewset_class = type(
                        f"{model.__name__}ViewSet",
                        (TurboDRFViewSet,),
                        viewset_attrs,
                    )

                    # Register the viewset
                    self.register(
                        endpoint, viewset_class, basename=model._meta.model_name
                    )

                    # Compile read path (on by default, opt out with compiled=False)
                    if config.get("compiled", True):
                        from .compiler import compile_model, register_compiled_plan

                        try:
                            plan = compile_model(model)
                            if plan is not None:
                                register_compiled_plan(model, plan)
                                logger.info(f"Compiled read path for {model.__name__}")
                        except Exception as e:
                            # If compilation fails, fall back to DRF path silently
                            logger.warning(
                                f"Could not compile {model.__name__}: {e}. "
                                f"Falling back to DRF serializer path."
                            )

        # Mark bypass-roles validation as complete for this process.
        # Subsequent inits skip the check.
        _bypass_roles_validated = True
        globals()["_bypass_roles_validated"] = True

        # Second pass: validate compiled-path safety. Must run AFTER the
        # main loop so every model's predicates / tenant_field are
        # registered — model A's M2M target may be model B, and B's
        # predicates may not have registered yet during A's compile.
        # See compiler.validate_compiled_path_safety for the full rule.
        from .compiler import _compiled_plans, validate_compiled_path_safety

        for compiled_model in list(_compiled_plans.keys()):
            validate_compiled_path_safety(compiled_model)

        # Third pass: validate searchable_fields safety. Same class of
        # bug as the compiled M2M target bypass — DRF's SearchFilter
        # joins to the target model without applying the target's own
        # predicates. Runs over every TurboDRF-mixin model regardless
        # of compile mode.
        from .validation import validate_searchable_fields_safety

        for model in apps.get_models():
            if not issubclass(model, TurboDRFMixin):
                continue
            if not model.turbodrf().get("enabled", True):
                continue
            validate_searchable_fields_safety(model)

        # Fourth pass: validate Custom predicate write safety. A Custom
        # without a write_validator returns [] from validate_write,
        # silently letting writes through Either(Owner, Custom) and
        # bypassing Owner's enforcement. See
        # predicates.validate_predicate_write_safety for the full rule.
        from .predicates import validate_predicate_write_safety

        for model in apps.get_models():
            if not issubclass(model, TurboDRFMixin):
                continue
            if not model.turbodrf().get("enabled", True):
                continue
            validate_predicate_write_safety(model)

        # Fifth pass: validate every TURBODRF_ROLES permission string
        # resolves to a real model + field + action. Catches typos like
        # `core.project.titel.read` that silently grant nothing.
        from .predicates import validate_permission_strings

        validate_permission_strings()

    def get_urls(self):
        """
        Generate URL patterns that work with or without trailing slashes.

        This override ensures that POST requests work regardless of whether
        the client includes a trailing slash, avoiding the common Django
        redirect issue that loses POST data.
        """
        urls = super().get_urls()

        # Create duplicate patterns without trailing slashes
        additional_urls = []
        for url_pattern in urls:
            if hasattr(url_pattern, "pattern") and hasattr(
                url_pattern.pattern, "_regex"
            ):
                # Get the regex pattern
                regex = url_pattern.pattern._regex

                # If it ends with '/$', create a version without it
                if regex.endswith("/$"):
                    new_regex = regex[:-2] + "$"  # Remove / before $

                    # Create new URL pattern without trailing slash
                    new_pattern = re_path(
                        new_regex,
                        url_pattern.callback,
                        url_pattern.default_args,
                        url_pattern.name + "_no_slash" if url_pattern.name else None,
                    )
                    additional_urls.append(new_pattern)

        return urls + additional_urls
