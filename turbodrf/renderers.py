"""
Fast JSON renderer for TurboDRF.

Uses msgspec (fastest) > orjson > stdlib json, depending on what's installed.
Both msgspec and orjson are ~5-7x faster than stdlib json.
"""

from rest_framework.renderers import BaseRenderer

_encoder = None
_lib_name = None

try:
    import msgspec.json

    _encoder = msgspec.json.Encoder()
    _lib_name = "msgspec"
except ImportError:
    try:
        import orjson

        _lib_name = "orjson"
    except ImportError:
        _lib_name = "stdlib"


if _lib_name == "msgspec":

    class TurboDRFRenderer(BaseRenderer):
        """JSON renderer using msgspec (~7x faster than stdlib json)."""

        media_type = "application/json"
        format = "json"
        charset = None

        def render(self, data, accepted_media_type=None, renderer_context=None):
            if data is None:
                return b""
            return _encoder.encode(data)

elif _lib_name == "orjson":

    class TurboDRFRenderer(BaseRenderer):  # type: ignore[no-redef]
        """JSON renderer using orjson (~5x faster than stdlib json)."""

        media_type = "application/json"
        format = "json"
        charset = None

        def render(self, data, accepted_media_type=None, renderer_context=None):
            if data is None:
                return b""
            return orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS)

else:
    from rest_framework.renderers import (  # type: ignore[no-redef] # noqa: F811, F401
        JSONRenderer as TurboDRFRenderer,
    )


FAST_JSON_AVAILABLE = _lib_name in ("msgspec", "orjson")
FAST_JSON_LIB = _lib_name
