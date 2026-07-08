"""Decorators for declaring custom endpoints on TurboDRF-generated viewsets."""

from rest_framework.decorators import action


def turbodrf_action(
    detail=False, methods=None, url_path=None, url_name=None, **kwargs
):
    """Declare a custom endpoint on a model's auto-generated viewset.

    Decorate a handler with this and list it in the model's ``turbodrf()``
    config under ``"actions"``. The router attaches it to the generated viewset,
    so the handler is a normal DRF ``@action`` and gets the SAME tenant +
    predicate scoping as the CRUD routes via ``self.get_object()`` /
    ``self.get_queryset()`` — no need to re-implement access control by hand.

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
    return action(
        detail=detail,
        methods=methods or ["get"],
        url_path=url_path,
        url_name=url_name,
        **kwargs,
    )
