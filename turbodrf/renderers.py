"""
Fast JSON renderer for TurboDRF.

Uses msgspec (fastest) > orjson > stdlib json, depending on what's installed.
Both msgspec and orjson are ~5-7x faster than stdlib json.
"""

from rest_framework.renderers import BaseRenderer

_encoder = None
_lib_name = None


def _enc_default(obj):
    """Coerce non-native types (Decimal, UUID, ...) to str so a stray value in a
    nested-field or @property render can't 500 the fast renderer. Mirrors DRF's
    default JSON encoder behaviour (Decimal -> str)."""
    return str(obj)


try:
    import msgspec.json

    _encoder = msgspec.json.Encoder(enc_hook=_enc_default)
    _lib_name = "msgspec"
except ImportError:
    try:
        import orjson

        _lib_name = "orjson"
    except ImportError:
        _lib_name = "stdlib"


def _stdlib_fallback(data, accepted_media_type, renderer_context):
    """Last-resort encode via DRF's JSONRenderer so an un-encodable value can
    never turn into an uncaught 500 (which would also leak a traceback under
    DEBUG)."""
    from rest_framework.renderers import JSONRenderer

    return JSONRenderer().render(data, accepted_media_type, renderer_context)


if _lib_name == "msgspec":

    class TurboDRFRenderer(BaseRenderer):
        """JSON renderer using msgspec (~7x faster than stdlib json)."""

        media_type = "application/json"
        format = "json"
        charset = None

        def render(self, data, accepted_media_type=None, renderer_context=None):
            if data is None:
                return b""
            try:
                return _encoder.encode(data)
            except (TypeError, ValueError):
                return _stdlib_fallback(data, accepted_media_type, renderer_context)

elif _lib_name == "orjson":

    class TurboDRFRenderer(BaseRenderer):  # type: ignore[no-redef]
        """JSON renderer using orjson (~5x faster than stdlib json)."""

        media_type = "application/json"
        format = "json"
        charset = None

        def render(self, data, accepted_media_type=None, renderer_context=None):
            if data is None:
                return b""
            try:
                return orjson.dumps(
                    data, default=_enc_default, option=orjson.OPT_NON_STR_KEYS
                )
            except (TypeError, ValueError):
                return _stdlib_fallback(data, accepted_media_type, renderer_context)

else:
    from rest_framework.renderers import (  # type: ignore[no-redef] # noqa: F811, F401
        JSONRenderer as TurboDRFRenderer,
    )


FAST_JSON_AVAILABLE = _lib_name in ("msgspec", "orjson")
FAST_JSON_LIB = _lib_name
