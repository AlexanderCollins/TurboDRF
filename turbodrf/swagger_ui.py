"""
TurboDRF Swagger UI Role Selection Module

This module provides views for role-based API documentation viewing.
It allows users to select a role when viewing API documentation, which
filters the visible endpoints and fields based on that role's permissions.

The module includes:
    - A role selector interface for choosing documentation perspective
    - Session management for persisting role selection
    - Integration with the role-based schema generator
"""

from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from django.conf import settings
from django.http import Http404


@api_view(["GET"])
@permission_classes([AllowAny])
def role_selector_view(request):
    """
    Display the role selector interface for API documentation.

    This view renders a page that allows users to select which role
    perspective they want to use when viewing the API documentation.
    The selected role determines which endpoints and fields are visible
    in the Swagger/ReDoc documentation.

    The view reads available roles from Django settings and maintains
    the current selection in the user's session.

    Args:
        request: The HTTP request object.

    Returns:
        HttpResponse: Rendered HTML page with role selection interface.

    Template Context:
        - roles: List of available role names from TURBODRF_ROLES setting
        - current_role: The currently selected role (from session or default)

    Example Settings:
        TURBODRF_ROLES = {
            'viewer': ['app.model.read'],
            'editor': ['app.model.read', 'app.model.write'],
            'admin': ['app.model.read', 'app.model.write', 'app.model.delete']
        }

    Usage:
        1. User visits /api/docs/role-selector/
        2. Selects 'editor' role
        3. Views Swagger UI which now shows only editor-accessible endpoints
    """
    # Check if documentation is enabled
    if not getattr(settings, "TURBODRF_ENABLE_DOCS", True):
        raise Http404("Documentation is disabled")

    TURBODRF_ROLES = getattr(settings, "TURBODRF_ROLES", {})
    roles = list(TURBODRF_ROLES.keys())
    current_role = request.session.get("api_role", roles[0] if roles else None)

    return render(
        request,
        "turbodrf/role_selector.html",
        {"roles": roles, "current_role": current_role},
    )


@api_view(["POST"])
@permission_classes([AllowAny])
@csrf_exempt
def set_api_role(request):
    """
    Set the selected API documentation role in the user's session.

    This endpoint handles AJAX requests from the role selector interface
    to update the user's chosen role for viewing API documentation.
    The role is stored in the session and used by the schema generator
    to filter the API documentation.

    Args:
        request: The HTTP request object containing role in request.data.

    Request Body:
        {
            "role": "editor"  # The role name to set
        }

    Returns:
        Response: JSON response indicating success or failure.

    Success Response:
        {
            "status": "success",
            "role": "editor"
        }

    Error Response (400):
        {
            "status": "error",
            "message": "Invalid role"
        }

    Session Storage:
        The selected role is stored in request.session['api_role'] and
        persists across requests until changed or the session expires.

    Example:
        POST /api/set-role/
        Content-Type: application/json
        {"role": "admin"}

        Response: {"status": "success", "role": "admin"}
    """
    # Check if documentation is enabled
    if not getattr(settings, "TURBODRF_ENABLE_DOCS", True):
        raise Http404("Documentation is disabled")

    TURBODRF_ROLES = getattr(settings, "TURBODRF_ROLES", {})
    role = request.data.get("role")
    if role in TURBODRF_ROLES:
        request.session["api_role"] = role
        return Response({"status": "success", "role": role})
    return Response({"status": "error", "message": "Invalid role"}, status=400)
