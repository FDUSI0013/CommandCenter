"""Turning arbitrary Python values into something the ingest contract accepts.

The SDK captures whatever a customer's function was called with, and that is
routinely a SQLAlchemy row, a NumPy array, a Pydantic model, a file handle or an
object whose ``__repr__`` raises. None of those are JSON, and none of them are
worth crashing an agent over, so the rules here are:

* Convert what can be converted, by structure rather than by import — the SDK
  must not import Pydantic or NumPy to serialise them.
* Never recurse forever: cycles are detected, depth and width are capped.
* Never raise. A value that defeats every strategy becomes its ``repr``, and a
  ``repr`` that itself raises becomes a placeholder.

The contract types ``input`` and ``output`` as JSON *objects*, not as arbitrary
JSON, so :func:`to_payload` wraps a bare value under ``{"value": ...}`` rather
than sending something the server would refuse.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
import json
import math
import uuid
from typing import Any, Dict, List, Optional

__all__ = ["to_json_safe", "to_payload", "estimate_bytes", "json_dumps"]

#: Strings longer than this are truncated with a marker. Generous enough for a
#: model response, small enough that one runaway prompt cannot fill a batch.
MAX_STRING_LENGTH = 64_000
#: How deep the walker will go before it summarises what is left.
MAX_DEPTH = 12
#: Items kept from a list or set, and keys kept from a mapping.
MAX_ITEMS = 500
#: Attributes read off an arbitrary object.
MAX_ATTRIBUTES = 50

_TRUNCATED = "…[truncated {0} chars]"
_UNSERIALISABLE = "<unserialisable {0}>"


def _truncate(value: str) -> str:
    if len(value) <= MAX_STRING_LENGTH:
        return value
    dropped = len(value) - MAX_STRING_LENGTH
    return value[:MAX_STRING_LENGTH] + _TRUNCATED.format(dropped)


def _safe_repr(value: Any) -> str:
    try:
        return _truncate(repr(value))
    except Exception:
        return _UNSERIALISABLE.format(type(value).__name__)


def _number(value: Any) -> Any:
    """JSON has no NaN or Infinity; the server's parser rejects both."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # Beyond 2^53 a JSON number stops round-tripping through most readers.
        return value if abs(value) <= 2**53 else str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    return value


def _duck_typed(value: Any) -> Optional[Any]:
    """Convert well-known shapes without importing the library that defines them.

    Pydantic v2, Pydantic v1, attrs, NumPy and anything with ``to_dict`` all
    announce themselves through a method name. Checking for the method is both
    cheaper and more robust than an ``isinstance`` against an import that may
    not be installed.
    """
    for method in ("model_dump", "dict", "to_dict", "_asdict"):
        fn = getattr(value, method, None)
        if callable(fn) and not isinstance(value, type):
            try:
                result = fn()
            except Exception:
                continue
            if isinstance(result, dict):
                return result
    # NumPy scalars and arrays.
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return tolist()
        except Exception:
            return None
    return None


def _walk(value: Any, depth: int, seen: set) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return _number(value)
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, bytes):
        return "<{0} bytes>".format(len(value))

    if isinstance(value, enum.Enum):
        return _walk(value.value, depth, seen)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        try:
            return _number(float(value))
        except Exception:
            return str(value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": _truncate(str(value))}

    if depth >= MAX_DEPTH:
        return _safe_repr(value)

    marker = id(value)
    if marker in seen:
        return "<circular reference>"

    if isinstance(value, dict):
        seen.add(marker)
        try:
            out: Dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= MAX_ITEMS:
                    out["…"] = "{0} more keys".format(len(value) - MAX_ITEMS)
                    break
                out[_key(key)] = _walk(item, depth + 1, seen)
            return out
        finally:
            seen.discard(marker)

    if isinstance(value, (list, tuple, set, frozenset)):
        seen.add(marker)
        try:
            items: List[Any] = []
            source = list(value) if not isinstance(value, (list, tuple)) else value
            for index, item in enumerate(source):
                if index >= MAX_ITEMS:
                    items.append("…{0} more items".format(len(source) - MAX_ITEMS))
                    break
                items.append(_walk(item, depth + 1, seen))
            return items
        finally:
            seen.discard(marker)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        seen.add(marker)
        try:
            return {
                field.name: _walk(getattr(value, field.name, None), depth + 1, seen)
                for field in dataclasses.fields(value)
            }
        finally:
            seen.discard(marker)

    converted = _duck_typed(value)
    if converted is not None:
        seen.add(marker)
        try:
            return _walk(converted, depth + 1, seen)
        finally:
            seen.discard(marker)

    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict) and attributes:
        seen.add(marker)
        try:
            out = {}
            for index, (key, item) in enumerate(attributes.items()):
                if str(key).startswith("_"):
                    continue
                if index >= MAX_ATTRIBUTES:
                    break
                out[_key(key)] = _walk(item, depth + 1, seen)
            if out:
                return out
        finally:
            seen.discard(marker)

    return _safe_repr(value)


def _key(key: Any) -> str:
    if isinstance(key, str):
        return _truncate(key)
    try:
        return str(key)
    except Exception:
        return _UNSERIALISABLE.format(type(key).__name__)


def to_json_safe(value: Any) -> Any:
    """Convert any Python value into JSON-encodable data. Never raises."""
    try:
        return _walk(value, 0, set())
    except Exception:
        return _safe_repr(value)


def to_payload(value: Any) -> Optional[Dict[str, Any]]:
    """Coerce a captured value into the JSON *object* the contract requires.

    A dict passes through. Anything else — a string, a list, a model object that
    serialised to a list — is wrapped under ``value``, because ``"input": "hi"``
    is refused by the endpoint while ``"input": {"value": "hi"}`` is not.
    """
    if value is None:
        return None
    converted = to_json_safe(value)
    if isinstance(converted, dict):
        return converted
    return {"value": converted}


def json_dumps(payload: Any) -> str:
    """Serialise a prepared payload, falling back rather than raising.

    ``default=str`` is the safety net: everything reaching here has already been
    through :func:`to_json_safe`, so it should be unnecessary, and on the day it
    is not, a stringified value beats a dropped batch.
    """
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    except Exception:
        return json.dumps(
            {"_fulcrum_serialisation_error": _safe_repr(payload)},
            ensure_ascii=False,
            separators=(",", ":"),
        )


def estimate_bytes(payload: Any) -> int:
    """Roughly how large this item will be on the wire, for the batch byte budget."""
    try:
        return len(json_dumps(payload).encode("utf-8"))
    except Exception:
        return 1_024
