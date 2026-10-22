"""
Fast JSON renderer for TurboDRF.

Uses msgspec (fastest) > orjson > stdlib json, depending on what's installed.
Both msgspec and orjson are ~5-7x faster than stdlib json.
"""

import decimal

from rest_framework.renderers import BaseRenderer
from rest_framework.utils.encoders import JSONEncoder as _DRFJSONEncoder

_encoder = None
_lib_name = None

_drf_encoder = _DRFJSONEncoder()


def _enc_default(obj):
    """Encode a value the fast library can't handle natively.

    Only the types DRF's own JSONEncoder handles are coerced (lazy strings,
    dates, timedeltas, UUIDs, bytes, ...), with DRF's own logic. Anything
    else raises TypeError, as it does under stock DRF. Never fall back to
    ``str(obj)``: an @property returning a QuerySet or a model instance would
    otherwise render its ``__str__`` (unscoped rows) into the response.

    A raw Decimal (e.g. from an @property) follows ``COERCE_DECIMAL_TO_STRING``
    as DRF's DecimalField does: a string by default, else a number.
    """
    if isinstance(obj, decimal.Decimal):
        return str(obj) if _decimals_as_strings() else float(obj)
    return _drf_encoder.default(obj)


def _decimals_as_strings():
    from rest_framework.settings import api_settings

    return api_settings.COERCE_DECIMAL_TO_STRING


try:
    import msgspec.json

    _encoder = msgspec.json.Encoder(enc_hook=_enc_default)
    # COERCE_DECIMAL_TO_STRING = False: DecimalField hands the renderer a
    # Decimal, which msgspec would otherwise encode as a string.
    try:
        _number_encoder = msgspec.json.Encoder(
            enc_hook=_enc_default, decimal_format="number"
        )
    except TypeError:  # msgspec without decimal_format: DRF's renderer
        _number_encoder = None
    _lib_name = "msgspec"
except ImportError:
    try:
        import orjson

        _lib_name = "orjson"
    except ImportError:
        _lib_name = "stdlib"


def _stdlib_fallback(data, accepted_media_type, renderer_context):
    """Re-encode via DRF's JSONRenderer when the fast library rejects the data
    (e.g. an int wider than 64 bits). A value DRF itself can't encode raises
    here exactly as it would under stock DRF."""
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
            encoder = _encoder if _decimals_as_strings() else _number_encoder
            if encoder is None:
                return _stdlib_fallback(data, accepted_media_type, renderer_context)
            try:
                return encoder.encode(data)
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
    from rest_framework.renderers import JSONRenderer

    TurboDRFRenderer = JSONRenderer  # type: ignore[misc,assignment]


FAST_JSON_AVAILABLE = _lib_name in ("msgspec", "orjson")
FAST_JSON_LIB = _lib_name
