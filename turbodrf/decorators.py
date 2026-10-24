"""Decorators for declaring custom endpoints on TurboDRF-generated viewsets."""

from rest_framework.decorators import action

# DRF @action keyword arguments a TurboDRF viewset honours for one action.
# Anything else is rejected: TurboDRF builds the serializer and the queryset
# itself (permission-filtered and row-scoped), so `serializer_class=` /
# `queryset=` would be silently ignored, and the viewset's computed
# attributes (`search_fields`, `filterset_fields`, ...) can't be overridden.
_ACTION_KWARGS = frozenset(
    {
        "permission_classes",
        "authentication_classes",
        "throttle_classes",
        "renderer_classes",
        "parser_classes",
        "content_negotiation_class",
        "metadata_class",
        "versioning_class",
        "pagination_class",
        "filter_backends",
        "lookup_field",
        "lookup_url_kwarg",
        "name",
        "suffix",
        "description",
    }
)


def turbodrf_action(detail=False, methods=None, url_path=None, url_name=None, **kwargs):
    """Declare a custom endpoint on a model's auto-generated viewset.

    Decorate a handler with this and list it in the model's ``turbodrf()``
    config under ``"actions"``. The router attaches it to the generated viewset,
    so the handler is a normal DRF ``@action`` and gets the SAME tenant +
    predicate scoping as the CRUD routes via ``self.get_object()`` /
    ``self.get_queryset()`` — no need to re-implement access control by hand.

    ``detail`` defaults to ``False`` (a collection route), unlike DRF's
    ``@action``, which requires it. Keyword arguments are DRF's per-action
    view attributes (``permission_classes``, ``throttle_classes``, ...);
    ``serializer_class`` and ``queryset`` are rejected, since TurboDRF builds
    both from the model's config.

    Example::

        from rest_framework.response import Response
        from turbodrf import turbodrf_action

        @turbodrf_action(detail=True, methods=["post"], url_path="resend")
        def resend(self, request, pk=None):
            obj = self.get_object()          # already tenant/predicate scoped
            obj.resend()
            return Response({"status": "sent"})

        class Invite(models.Model, TurboDRFMixin):
            @classmethod
            def turbodrf(cls):
                return {"fields": ["id", "email"], "actions": [resend]}
    """
    unknown = sorted(set(kwargs) - _ACTION_KWARGS)
    if unknown:
        raise TypeError(
            f"turbodrf_action() got unsupported argument(s) "
            f"{', '.join(repr(name) for name in unknown)}. TurboDRF builds an "
            f"action's serializer (self.get_serializer()) and queryset "
            f"(self.get_queryset()) from the model's turbodrf() config, "
            f"permission-filtered and row-scoped; supported: "
            f"{', '.join(sorted(_ACTION_KWARGS))}."
        )
    return action(
        detail=detail,
        methods=methods or ["get"],
        url_path=url_path,
        url_name=url_name,
        **kwargs,
    )
