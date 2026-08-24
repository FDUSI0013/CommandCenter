"""OpenTelemetry OTLP/HTTP receiver, mounted at the application root.

An agent that is already instrumented with OpenTelemetry should not have to
adopt our SDK to be governed. This module accepts ``POST /v1/traces`` in both
encodings the OTLP/HTTP specification defines, translates the OTel data model
into the same :class:`~fulcrum_ops_api.schemas.ingest.TraceIn` items the SDK
endpoints accept, and hands them to the *same* ``services.ingest`` pipeline. An
OTLP span therefore passes exactly the same agent resolution, policy evaluation,
guardrail check, entitlement gate and quota accounting as one reported natively;
this file contains no governance logic of its own.

**Protobuf without a protobuf dependency.** OTLP's binary encoding is decoded
here by hand. That is a deliberate, bounded choice: the only messages we accept
are ``ExportTraceServiceRequest`` and the handful nested inside it, the wire
format needed is the four standard types (varint, fixed64, fixed32,
length-delimited), and every read is bounds-checked against the enclosing
message. Deprecated group encodings and unknown wire types are refused rather
than skipped, recursion is depth-limited, and unrecognised field numbers are
ignored exactly as the specification requires. Adding a code-generation
toolchain and a runtime library to the control plane to parse roughly two
hundred bytes of schema would be the larger risk.

**Semantic conventions.** ``gen_ai.*`` attributes are mapped onto the telemetry
model: the request/response model and system become the span's model and
provider, ``gen_ai.usage.*`` becomes its token counters, ``gen_ai.usage.cost``
its cost, prompts and completions its input and output, and
``gen_ai.conversation.id`` the thread that groups a conversation. Anything not
recognised is preserved verbatim in the span's metadata rather than dropped, so
no instrumentation is lost in translation.

The response is the OTLP partial-success envelope, encoded in whichever format
the request used.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import struct
import uuid
from collections.abc import Iterator
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import ValidationError

from ..core.errors import AppError, ValidationFailed
from ..schemas.ingest import (
    MAX_SPANS_PER_TRACE,
    MAX_TRACES_PER_BATCH,
    BatchEnvelope,
    ErrorInfoIn,
    ItemOutcome,
    ParsedBatch,
    SpanIn,
    SpanType,
    TraceIn,
)
from ..services import ingest as service
from .deps import Db
from .v1.ingest import IngestPrincipal, read_body

router = APIRouter(tags=["OpenTelemetry"])

#: The OTLP body is read raw and size-capped by exactly the same dependency the
#: SDK endpoints use, so one setting governs every ingest front door.
OtlpBody = Annotated[bytes, Depends(read_body)]

JSON_CONTENT_TYPE: Final[str] = "application/json"
PROTOBUF_CONTENT_TYPES: Final[frozenset[str]] = frozenset(
    {"application/x-protobuf", "application/protobuf"}
)

#: How deep a nested protobuf message may go before we stop following it.
MAX_PROTOBUF_DEPTH: Final[int] = 12

#: Longest error message returned in the partial-success envelope.
MAX_ERROR_MESSAGE: Final[int] = 1_000

#: OTLP status codes. 0 unset, 1 ok, 2 error.
STATUS_ERROR: Final[int] = 2

#: Span kinds, for the metadata we keep verbatim.
_SPAN_KINDS: Final[tuple[str, ...]] = (
    "UNSPECIFIED",
    "INTERNAL",
    "SERVER",
    "CLIENT",
    "PRODUCER",
    "CONSUMER",
)


class UnsupportedMediaType(AppError):
    """The body arrived in an encoding this receiver does not decode."""

    status_code = 415
    code = "unsupported_media_type"
    message = "The request body encoding is not supported."


# ---------------------------------------------------------------------------
# Protobuf wire format
#
# Only what ExportTraceServiceRequest needs. Every function below is total: it
# either returns a value or raises _ProtobufError, and never reads past the end
# of the buffer it was given.
# ---------------------------------------------------------------------------

_WIRE_VARINT: Final[int] = 0
_WIRE_FIXED64: Final[int] = 1
_WIRE_BYTES: Final[int] = 2
_WIRE_FIXED32: Final[int] = 5

_TWO_POW_64: Final[int] = 1 << 64
_TWO_POW_63: Final[int] = 1 << 63


class _ProtobufError(ValueError):
    """The bytes are not a well-formed protobuf message."""


def _read_varint(buf: bytes, pos: int, end: int) -> tuple[int, int]:
    """Read one base-128 varint, returning its value and the new position."""
    result = 0
    shift = 0
    while True:
        if pos >= end:
            raise _ProtobufError("truncated varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise _ProtobufError("varint wider than 64 bits")


def _signed(value: int) -> int:
    """Interpret a varint as a two's-complement int64, as OTLP's int64 fields do."""
    return value - _TWO_POW_64 if value >= _TWO_POW_63 else value


def _fields(buf: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[int, int, Any]]:
    """Yield ``(field_number, wire_type, value)`` for one message.

    Length-delimited values are returned as ``bytes``, fixed-width values as
    their raw little-endian bytes, and varints as ``int``. Unknown *fields* are
    the caller's business to ignore; unknown *wire types* are fatal, because
    skipping one means we no longer know where the next field starts.
    """
    limit = len(buf) if end is None else end
    pos = start
    while pos < limit:
        key, pos = _read_varint(buf, pos, limit)
        field, wire = key >> 3, key & 0x07
        if field == 0:
            raise _ProtobufError("field number 0 is not valid")
        if wire == _WIRE_VARINT:
            value, pos = _read_varint(buf, pos, limit)
            yield field, wire, value
        elif wire == _WIRE_FIXED64:
            if pos + 8 > limit:
                raise _ProtobufError("truncated fixed64")
            yield field, wire, buf[pos : pos + 8]
            pos += 8
        elif wire == _WIRE_FIXED32:
            if pos + 4 > limit:
                raise _ProtobufError("truncated fixed32")
            yield field, wire, buf[pos : pos + 4]
            pos += 4
        elif wire == _WIRE_BYTES:
            length, pos = _read_varint(buf, pos, limit)
            if pos + length > limit:
                raise _ProtobufError("length-delimited field runs past the end of the message")
            yield field, wire, buf[pos : pos + length]
            pos += length
        else:
            raise _ProtobufError(f"unsupported wire type {wire}")


def _u64(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


def _text(raw: Any) -> str:
    if not isinstance(raw, bytes):
        raise _ProtobufError("expected a length-delimited string")
    return raw.decode("utf-8", "replace")


def _sub(raw: Any) -> bytes:
    if not isinstance(raw, bytes):
        raise _ProtobufError("expected a nested message")
    return raw


def _decode_any_value(buf: bytes, depth: int) -> Any:
    """``opentelemetry.proto.common.v1.AnyValue``."""
    if depth > MAX_PROTOBUF_DEPTH:
        raise _ProtobufError("attribute nesting is too deep")
    for field, wire, value in _fields(buf):
        if field == 1 and wire == _WIRE_BYTES:
            return _text(value)
        if field == 2 and wire == _WIRE_VARINT:
            return bool(value)
        if field == 3 and wire == _WIRE_VARINT:
            return _signed(value)
        if field == 4 and wire == _WIRE_FIXED64:
            return struct.unpack("<d", value)[0]
        if field == 5 and wire == _WIRE_BYTES:
            return _decode_array_value(_sub(value), depth + 1)
        if field == 6 and wire == _WIRE_BYTES:
            return _decode_key_values(_sub(value), depth + 1)
        if field == 7 and wire == _WIRE_BYTES:
            return base64.b64encode(_sub(value)).decode("ascii")
    return None


def _decode_array_value(buf: bytes, depth: int) -> list[Any]:
    """``ArrayValue`` — repeated AnyValue in field 1."""
    values: list[Any] = []
    for field, wire, value in _fields(buf):
        if field == 1 and wire == _WIRE_BYTES:
            values.append(_decode_any_value(_sub(value), depth + 1))
    return values


def _decode_key_values(buf: bytes, depth: int) -> dict[str, Any]:
    """``KeyValueList`` — repeated KeyValue in field 1."""
    result: dict[str, Any] = {}
    for field, wire, value in _fields(buf):
        if field == 1 and wire == _WIRE_BYTES:
            key, item = _decode_key_value(_sub(value), depth + 1)
            if key:
                result[key] = item
    return result


def _decode_key_value(buf: bytes, depth: int) -> tuple[str, Any]:
    """``KeyValue`` — key in field 1, AnyValue in field 2."""
    key = ""
    item: Any = None
    for field, wire, value in _fields(buf):
        if field == 1 and wire == _WIRE_BYTES:
            key = _text(value)
        elif field == 2 and wire == _WIRE_BYTES:
            item = _decode_any_value(_sub(value), depth + 1)
    return key, item


def _decode_attributes(buf: bytes, field_number: int, depth: int) -> dict[str, Any]:
    """Collect every repeated ``KeyValue`` under one field of a message."""
    attributes: dict[str, Any] = {}
    for field, wire, value in _fields(buf):
        if field == field_number and wire == _WIRE_BYTES:
            key, item = _decode_key_value(_sub(value), depth + 1)
            if key:
                attributes[key] = item
    return attributes


def _decode_status(buf: bytes) -> tuple[int, str | None]:
    """``Status`` — message in field 2, code in field 3."""
    code = 0
    message: str | None = None
    for field, wire, value in _fields(buf):
        if field == 2 and wire == _WIRE_BYTES:
            message = _text(value)
        elif field == 3 and wire == _WIRE_VARINT:
            code = int(value)
    return code, message


class _Span:
    """One decoded OTLP span, in the shape the translation step wants."""

    __slots__ = (
        "trace_id",
        "span_id",
        "parent_span_id",
        "name",
        "kind",
        "start_ns",
        "end_ns",
        "attributes",
        "status_code",
        "status_message",
    )

    def __init__(self) -> None:
        self.trace_id = ""
        self.span_id = ""
        self.parent_span_id = ""
        self.name = ""
        self.kind = 0
        self.start_ns = 0
        self.end_ns = 0
        self.attributes: dict[str, Any] = {}
        self.status_code = 0
        self.status_message: str | None = None


def _decode_span(buf: bytes, depth: int) -> _Span:
    """``opentelemetry.proto.trace.v1.Span``."""
    span = _Span()
    for field, wire, value in _fields(buf):
        if field == 1 and wire == _WIRE_BYTES:
            span.trace_id = _sub(value).hex()
        elif field == 2 and wire == _WIRE_BYTES:
            span.span_id = _sub(value).hex()
        elif field == 4 and wire == _WIRE_BYTES:
            span.parent_span_id = _sub(value).hex()
        elif field == 5 and wire == _WIRE_BYTES:
            span.name = _text(value)
        elif field == 6 and wire == _WIRE_VARINT:
            span.kind = int(value)
        elif field == 7 and wire == _WIRE_FIXED64:
            span.start_ns = _u64(value)
        elif field == 8 and wire == _WIRE_FIXED64:
            span.end_ns = _u64(value)
        elif field == 9 and wire == _WIRE_BYTES:
            key, item = _decode_key_value(_sub(value), depth + 1)
            if key:
                span.attributes[key] = item
        elif field == 15 and wire == _WIRE_BYTES:
            span.status_code, span.status_message = _decode_status(_sub(value))
    return span


class _ScopeSpans:
    """``ScopeSpans`` — an instrumentation scope and the spans it produced."""

    __slots__ = ("name", "version", "spans")

    def __init__(self) -> None:
        self.name = ""
        self.version = ""
        self.spans: list[_Span] = []


class _ResourceSpans:
    """``ResourceSpans`` — one resource and every scope that reported under it."""

    __slots__ = ("attributes", "scopes")

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.scopes: list[_ScopeSpans] = []


def _decode_scope(buf: bytes) -> tuple[str, str]:
    """``InstrumentationScope`` — name in field 1, version in field 2."""
    name = version = ""
    for field, wire, value in _fields(buf):
        if field == 1 and wire == _WIRE_BYTES:
            name = _text(value)
        elif field == 2 and wire == _WIRE_BYTES:
            version = _text(value)
    return name, version


def _decode_scope_spans(buf: bytes, depth: int) -> _ScopeSpans:
    scope = _ScopeSpans()
    for field, wire, value in _fields(buf):
        if field == 1 and wire == _WIRE_BYTES:
            scope.name, scope.version = _decode_scope(_sub(value))
        elif field == 2 and wire == _WIRE_BYTES:
            scope.spans.append(_decode_span(_sub(value), depth + 1))
    return scope


def _decode_resource_spans(buf: bytes, depth: int) -> _ResourceSpans:
    resource = _ResourceSpans()
    for field, wire, value in _fields(buf):
        if field == 1 and wire == _WIRE_BYTES:
            resource.attributes = _decode_attributes(_sub(value), 1, depth + 1)
        elif field == 2 and wire == _WIRE_BYTES:
            resource.scopes.append(_decode_scope_spans(_sub(value), depth + 1))
    return resource


def decode_export_trace_service_request(body: bytes) -> list[_ResourceSpans]:
    """Decode ``ExportTraceServiceRequest`` — repeated ResourceSpans in field 1."""
    resources: list[_ResourceSpans] = []
    for field, wire, value in _fields(body):
        if field == 1 and wire == _WIRE_BYTES:
            resources.append(_decode_resource_spans(_sub(value), 1))
    return resources


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _encode_tag(field: int, wire: int) -> bytes:
    return _encode_varint((field << 3) | wire)


def encode_export_trace_service_response(rejected_spans: int, error_message: str) -> bytes:
    """Encode ``ExportTraceServiceResponse``.

    An empty message means everything was accepted, which is exactly what the
    specification asks for; the ``partial_success`` sub-message is emitted only
    when something was refused.
    """
    if rejected_spans <= 0 and not error_message:
        return b""
    partial = bytearray()
    if rejected_spans > 0:
        partial += _encode_tag(1, _WIRE_VARINT) + _encode_varint(rejected_spans)
    if error_message:
        encoded = error_message.encode("utf-8")[:MAX_ERROR_MESSAGE]
        partial += _encode_tag(2, _WIRE_BYTES) + _encode_varint(len(encoded)) + encoded
    return _encode_tag(1, _WIRE_BYTES) + _encode_varint(len(partial)) + bytes(partial)


# ---------------------------------------------------------------------------
# JSON encoding of the same messages
# ---------------------------------------------------------------------------


def _pick(payload: dict[str, Any], *names: str) -> Any:
    """OTLP/JSON is camelCase, but exporters in the wild send snake_case too."""
    for name in names:
        if name in payload:
            return payload[name]
    return None


def _json_any_value(value: Any, depth: int = 0) -> Any:
    if not isinstance(value, dict) or depth > MAX_PROTOBUF_DEPTH:
        return value
    for key in ("stringValue", "string_value"):
        if key in value:
            return value[key]
    for key in ("boolValue", "bool_value"):
        if key in value:
            return bool(value[key])
    for key in ("intValue", "int_value"):
        if key in value:
            try:
                return int(value[key])
            except (TypeError, ValueError):
                return None
    for key in ("doubleValue", "double_value"):
        if key in value:
            try:
                return float(value[key])
            except (TypeError, ValueError):
                return None
    for key in ("bytesValue", "bytes_value"):
        if key in value:
            return value[key]
    array = _pick(value, "arrayValue", "array_value")
    if isinstance(array, dict):
        return [_json_any_value(item, depth + 1) for item in array.get("values", []) or []]
    kvlist = _pick(value, "kvlistValue", "kvlist_value")
    if isinstance(kvlist, dict):
        return _json_attributes(kvlist.get("values", []) or [], depth + 1)
    return None


def _json_attributes(raw: Any, depth: int = 0) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    if not isinstance(raw, list):
        return attributes
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not key:
            continue
        attributes[str(key)] = _json_any_value(entry.get("value"), depth + 1)
    return attributes


def _json_nanos(value: Any) -> int:
    """OTLP/JSON renders uint64 nanoseconds as a decimal string."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _json_hex(value: Any) -> str:
    """Trace and span ids travel as lowercase hex in OTLP/JSON."""
    if not isinstance(value, str) or not value:
        return ""
    candidate = value.strip().lower()
    try:
        bytes.fromhex(candidate)
    except ValueError:
        # Some exporters send base64 for ids; accept that rather than lose the span.
        try:
            return base64.b64decode(value, validate=True).hex()
        except (binascii.Error, ValueError):
            return ""
    return candidate


def parse_json_request(payload: Any) -> list[_ResourceSpans]:
    """Translate an OTLP/JSON export request into the decoded structures."""
    if not isinstance(payload, dict):
        raise ValidationFailed("An OTLP export request must be a JSON object.")

    raw_resources = _pick(payload, "resourceSpans", "resource_spans")
    if raw_resources is None:
        raise ValidationFailed("The body is missing its 'resourceSpans' array.")
    if not isinstance(raw_resources, list):
        raise ValidationFailed("'resourceSpans' must be an array.")

    resources: list[_ResourceSpans] = []
    for raw_resource in raw_resources:
        if not isinstance(raw_resource, dict):
            continue
        resource = _ResourceSpans()
        resource_block = raw_resource.get("resource")
        if isinstance(resource_block, dict):
            resource.attributes = _json_attributes(resource_block.get("attributes"))

        for raw_scope in _pick(raw_resource, "scopeSpans", "scope_spans") or []:
            if not isinstance(raw_scope, dict):
                continue
            scope = _ScopeSpans()
            scope_block = raw_scope.get("scope")
            if isinstance(scope_block, dict):
                scope.name = str(scope_block.get("name") or "")
                scope.version = str(scope_block.get("version") or "")
            for raw_span in raw_scope.get("spans") or []:
                if not isinstance(raw_span, dict):
                    continue
                span = _Span()
                span.trace_id = _json_hex(_pick(raw_span, "traceId", "trace_id"))
                span.span_id = _json_hex(_pick(raw_span, "spanId", "span_id"))
                span.parent_span_id = _json_hex(
                    _pick(raw_span, "parentSpanId", "parent_span_id")
                )
                span.name = str(raw_span.get("name") or "")
                kind = raw_span.get("kind")
                span.kind = kind if isinstance(kind, int) else 0
                span.start_ns = _json_nanos(
                    _pick(raw_span, "startTimeUnixNano", "start_time_unix_nano")
                )
                span.end_ns = _json_nanos(_pick(raw_span, "endTimeUnixNano", "end_time_unix_nano"))
                span.attributes = _json_attributes(raw_span.get("attributes"))
                status = raw_span.get("status")
                if isinstance(status, dict):
                    code = status.get("code")
                    span.status_code = code if isinstance(code, int) else 0
                    span.status_message = status.get("message")
                scope.spans.append(span)
            resource.scopes.append(scope)
        resources.append(resource)
    return resources


# ---------------------------------------------------------------------------
# Semantic conventions → telemetry model
# ---------------------------------------------------------------------------

_AGENT_KEYS: Final[tuple[str, ...]] = (
    "fulcrum.agent",
    "gen_ai.agent.name",
    "service.name",
)
_MODEL_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.response.model",
    "gen_ai.request.model",
    "llm.model_name",
)
_PROVIDER_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.system",
    "gen_ai.provider.name",
    "llm.system",
)
_INPUT_TOKEN_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.prompt_tokens",
    "llm.token_count.prompt",
)
_OUTPUT_TOKEN_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
    "llm.token_count.completion",
)
_TOTAL_TOKEN_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.usage.total_tokens",
    "llm.token_count.total",
)
_COST_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.usage.cost",
    "gen_ai.usage.total_cost",
    "llm.cost.total",
)
_INPUT_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.prompt",
    "gen_ai.input.messages",
    "gen_ai.request.messages",
    "llm.input_messages",
    "input.value",
)
_OUTPUT_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.completion",
    "gen_ai.output.messages",
    "gen_ai.response.messages",
    "llm.output_messages",
    "output.value",
)
_THREAD_KEYS: Final[tuple[str, ...]] = (
    "gen_ai.conversation.id",
    "session.id",
    "thread.id",
)
_TOOL_KEYS: Final[tuple[str, ...]] = ("gen_ai.tool.name", "tool.name")

_OPERATION_KEY: Final[str] = "gen_ai.operation.name"
_LLM_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"chat", "text_completion", "embeddings", "generate_content", "completion"}
)

#: Attribute keys consumed by the mapping above; they are not repeated into
#: metadata, because they already have a home on the span.
_CONSUMED: Final[frozenset[str]] = frozenset(
    _AGENT_KEYS
    + _MODEL_KEYS
    + _PROVIDER_KEYS
    + _INPUT_TOKEN_KEYS
    + _OUTPUT_TOKEN_KEYS
    + _TOTAL_TOKEN_KEYS
    + _COST_KEYS
    + _INPUT_KEYS
    + _OUTPUT_KEYS
    + _THREAD_KEYS
)


def _attr(attributes: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = attributes.get(key)
        if value not in (None, ""):
            return value
    return None


def _attr_int(attributes: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    value = _attr(attributes, keys)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _attr_float(attributes: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    value = _attr(attributes, keys)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _instant(nanos: int) -> dt.datetime:
    """Nanoseconds since the epoch as a UTC instant, without float rounding."""
    seconds, remainder = divmod(max(nanos, 0), 1_000_000_000)
    return dt.datetime.fromtimestamp(seconds, dt.UTC) + dt.timedelta(
        microseconds=remainder // 1_000
    )


def _hex_bytes(value: str, size: int) -> bytes:
    """Fixed-width bytes from a hex id, tolerating a short or odd-length one."""
    padded = value.rjust(size * 2, "0")[: size * 2]
    if len(padded) % 2:
        padded = "0" + padded[:-1]
    try:
        return bytes.fromhex(padded)
    except ValueError:
        return b"\x00" * size


def _trace_uuid(trace_id_hex: str) -> str:
    """An OTLP trace id is 16 bytes, which is exactly a UUID."""
    return str(uuid.UUID(bytes=_hex_bytes(trace_id_hex, 16)))


def _span_uuid(span_id_hex: str, trace_id_hex: str) -> str:
    """Widen an 8-byte span id into a UUID, deterministically.

    The store addresses spans by UUID and OTLP span ids are half that width, so
    the trace's first eight bytes are appended. The derivation is pure, so a
    parent reference resolves to the same id its span was stored under, and a
    replayed export produces the same ids rather than duplicate spans.
    """
    return str(uuid.UUID(bytes=_hex_bytes(span_id_hex, 8) + _hex_bytes(trace_id_hex, 16)[:8]))


def _span_type(attributes: dict[str, Any]) -> SpanType:
    operation = str(_attr(attributes, (_OPERATION_KEY,)) or "").strip().lower()
    if _attr(attributes, _TOOL_KEYS) or operation == "execute_tool":
        return SpanType.TOOL
    if operation in _LLM_OPERATIONS or _attr(attributes, _MODEL_KEYS):
        return SpanType.LLM
    if any(key.startswith("gen_ai.guardrail") for key in attributes):
        return SpanType.GUARDRAIL
    return SpanType.GENERAL


def _usage(attributes: dict[str, Any]) -> dict[str, int] | None:
    prompt = _attr_int(attributes, _INPUT_TOKEN_KEYS)
    completion = _attr_int(attributes, _OUTPUT_TOKEN_KEYS)
    total = _attr_int(attributes, _TOTAL_TOKEN_KEYS)
    if prompt is None and completion is None and total is None:
        return None
    counters: dict[str, int] = {}
    if prompt is not None:
        counters["prompt_tokens"] = prompt
    if completion is not None:
        counters["completion_tokens"] = completion
    counters["total_tokens"] = total if total is not None else (prompt or 0) + (completion or 0)
    return counters


def _residual(attributes: dict[str, Any]) -> dict[str, Any]:
    """Attributes with no home on the span, kept verbatim in its metadata."""
    return {key: value for key, value in attributes.items() if key not in _CONSUMED}


def _error_info(span: _Span) -> ErrorInfoIn | None:
    if span.status_code != STATUS_ERROR:
        return None
    return ErrorInfoIn(
        exception_type=str(span.attributes.get("exception.type") or "SpanStatusError"),
        message=span.status_message or str(span.attributes.get("exception.message") or "")[:4_000]
        or None,
        traceback=str(span.attributes.get("exception.stacktrace") or "")[:16_000] or None,
    )


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _agent_name(resource: _ResourceSpans, span: _Span) -> str | None:
    """Which agent this span belongs to, span attributes winning over the resource."""
    return _as_text(
        _attr(span.attributes, _AGENT_KEYS) or _attr(resource.attributes, _AGENT_KEYS)
    )


def translate(resources: list[_ResourceSpans]) -> tuple[list[TraceIn], int, list[str]]:
    """Turn decoded OTLP spans into the trace items the ingest pipeline accepts.

    Spans are grouped by ``(agent, trace id)``. The span with no parent inside
    the group supplies the trace's name, payloads and thread; if a group has no
    such span — a common case when a batch is exported mid-trace — the earliest
    span stands in, so a partial export still lands as a readable trace rather
    than being dropped.

    Returns the traces, how many spans were dropped, and why.
    """
    groups: dict[tuple[str | None, str], list[tuple[_ResourceSpans, _Span]]] = {}
    dropped = 0
    problems: list[str] = []

    for resource in resources:
        for scope in resource.scopes:
            for span in scope.spans:
                if not span.trace_id or not span.span_id:
                    dropped += 1
                    problems.append("a span arrived without a trace id or span id")
                    continue
                groups.setdefault((_agent_name(resource, span), span.trace_id), []).append(
                    (resource, span)
                )

    traces: list[TraceIn] = []
    for (agent, trace_id_hex), members in groups.items():
        if len(traces) >= MAX_TRACES_PER_BATCH:
            dropped += len(members)
            problems.append("the export carried more traces than one batch accepts")
            continue

        members.sort(key=lambda item: item[1].start_ns)
        if len(members) > MAX_SPANS_PER_TRACE:
            dropped += len(members) - MAX_SPANS_PER_TRACE
            problems.append("a trace carried more spans than one batch accepts")
            members = members[:MAX_SPANS_PER_TRACE]

        ids = {span.span_id for _, span in members}
        root = next(
            (
                span
                for _, span in members
                if not span.parent_span_id or span.parent_span_id not in ids
            ),
            members[0][1],
        )
        root_resource = next(res for res, span in members if span is root)

        spans: list[SpanIn] = []
        for _res, span in members:
            attributes = span.attributes
            parent = (
                _span_uuid(span.parent_span_id, trace_id_hex)
                if span.parent_span_id and span.parent_span_id in ids
                else None
            )
            try:
                spans.append(
                    SpanIn(
                        id=_span_uuid(span.span_id, trace_id_hex),
                        parent_span_id=parent,
                        name=span.name or "span",
                        type=_span_type(attributes),
                        start_time=_instant(span.start_ns),
                        end_time=_instant(span.end_ns) if span.end_ns else None,
                        input=_attr(attributes, _INPUT_KEYS),
                        output=_attr(attributes, _OUTPUT_KEYS),
                        usage=_usage(attributes),
                        model=_as_text(_attr(attributes, _MODEL_KEYS)),
                        provider=_as_text(_attr(attributes, _PROVIDER_KEYS)),
                        total_estimated_cost=_attr_float(attributes, _COST_KEYS),
                        error_info=_error_info(span),
                        metadata={
                            **_residual(attributes),
                            "otel.span_kind": _SPAN_KINDS[span.kind]
                            if 0 <= span.kind < len(_SPAN_KINDS)
                            else "UNSPECIFIED",
                            "otel.span_id": span.span_id,
                            "otel.trace_id": trace_id_hex,
                        },
                    )
                )
            except ValidationError as exc:
                dropped += 1
                problems.append(f"a span could not be translated: {exc.error_count()} problem(s)")

        root_attributes = root.attributes
        latest = max((span.end_ns for _, span in members), default=0)
        try:
            traces.append(
                TraceIn(
                    id=_trace_uuid(trace_id_hex),
                    name=root.name or "trace",
                    start_time=_instant(min(span.start_ns for _, span in members)),
                    end_time=_instant(latest) if latest else None,
                    input=_attr(root_attributes, _INPUT_KEYS),
                    output=_attr(root_attributes, _OUTPUT_KEYS),
                    thread_id=_as_text(_attr(root_attributes, _THREAD_KEYS)),
                    error_info=_error_info(root),
                    metadata={
                        **{f"resource.{k}": v for k, v in root_resource.attributes.items()},
                        "otel.trace_id": trace_id_hex,
                    },
                    agent=agent,
                    spans=spans,
                )
            )
        except ValidationError:
            dropped += len(members)
            problems.append("a trace could not be translated from its OTLP spans")
    return traces, dropped, problems


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


def _is_protobuf(content_type: str) -> bool:
    return content_type.split(";", 1)[0].strip().lower() in PROTOBUF_CONTENT_TYPES


def _is_json(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == JSON_CONTENT_TYPE or media.endswith("+json") or media == ""


@router.post(
    "/v1/traces",
    summary="OpenTelemetry OTLP/HTTP trace export",
    responses={
        200: {
            "description": "Export accepted, possibly in part.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "partialSuccess": {
                                "type": "object",
                                "properties": {
                                    "rejectedSpans": {"type": "integer"},
                                    "errorMessage": {"type": "string"},
                                },
                            }
                        },
                    }
                },
                "application/x-protobuf": {"schema": {"type": "string", "format": "binary"}},
            },
        }
    },
)
async def export_traces(
    principal: IngestPrincipal,
    session: Db,
    body: OtlpBody,
    request: Request,
    content_type: Annotated[str | None, Header()] = None,
) -> Response:
    """Accept an OpenTelemetry trace export, in protobuf or JSON.

    Authenticated exactly like the SDK endpoints — an ingest-scoped API key —
    and governed by exactly the same pipeline, so an OTel-instrumented agent is
    policed no differently from one using our SDK. The reply is the OTLP
    partial-success envelope in whichever encoding the request used: spans
    refused by a policy or a guardrail are counted in ``rejectedSpans`` with the
    reason in ``errorMessage``, which is how an OTel exporter learns not to
    retry them.
    """
    media = content_type or ""
    protobuf = _is_protobuf(media)
    if protobuf:
        try:
            resources = decode_export_trace_service_request(body)
        except _ProtobufError as exc:
            raise ValidationFailed(f"The OTLP protobuf body is malformed: {exc}.") from exc
    elif _is_json(media):
        try:
            payload = json.loads(body) if body else {}
        except ValueError as exc:
            raise ValidationFailed(f"The OTLP JSON body is not valid JSON: {exc}.") from exc
        resources = parse_json_request(payload)
    else:
        raise UnsupportedMediaType(
            f"'{media}' is not an OTLP encoding. Send application/json or "
            "application/x-protobuf.",
            details={"supported": [JSON_CONTENT_TYPE, *sorted(PROTOBUF_CONTENT_TYPES)]},
        )

    traces, dropped, problems = translate(resources)
    rejected_spans = dropped
    reasons: list[str] = list(dict.fromkeys(problems))

    if traces:
        parsed: ParsedBatch[TraceIn] = ParsedBatch(
            envelope=BatchEnvelope(sdk="opentelemetry"),
            items=list(traces),
            errors={},
        )
        outcome = await service.ingest_traces(
            session, principal, parsed, request=request, source=service.SOURCE_OTLP
        )
        for row in outcome.results:
            if row.outcome is ItemOutcome.ACCEPTED:
                continue
            # A refused trace takes its spans with it; a trace that carried none
            # still counts as one rejected span so the exporter sees the loss.
            rejected_spans += max(row.spans, 1)
            if row.reason:
                reasons.append(row.reason)

    return _respond(protobuf, rejected_spans, reasons)


def _respond(protobuf: bool, rejected_spans: int, reasons: list[str]) -> Response:
    """Build the OTLP partial-success reply in the request's own encoding."""
    message = "; ".join(dict.fromkeys(reasons))[:MAX_ERROR_MESSAGE]
    if protobuf:
        return Response(
            content=encode_export_trace_service_response(rejected_spans, message),
            media_type="application/x-protobuf",
        )

    body: dict[str, Any] = {}
    if rejected_spans > 0 or message:
        body["partialSuccess"] = {"rejectedSpans": rejected_spans, "errorMessage": message}
    return Response(content=json.dumps(body), media_type=JSON_CONTENT_TYPE)
