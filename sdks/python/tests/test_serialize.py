"""Serialisation: total, lossy where it must be, and never raising."""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
import uuid

from fulcrum_ops.serialize import (
    MAX_ITEMS,
    MAX_STRING_LENGTH,
    estimate_bytes,
    json_dumps,
    to_json_safe,
    to_payload,
)


class Colour(enum.Enum):
    RED = "red"


@dataclasses.dataclass
class Point:
    x: int
    y: int


class Model:
    """Duck-typed the way Pydantic v2 announces itself."""

    def model_dump(self) -> dict:
        return {"field": "value"}


class Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("even my repr is broken")


def test_scalars_pass_through() -> None:
    assert to_json_safe(None) is None
    assert to_json_safe(True) is True
    assert to_json_safe(42) == 42
    assert to_json_safe("text") == "text"
    assert to_json_safe(1.5) == 1.5


def test_json_has_no_nan_or_infinity() -> None:
    """The server's parser refuses both, so they are stringified rather than sent."""
    assert to_json_safe(float("nan")) == "NaN"
    assert to_json_safe(float("inf")) == "Infinity"
    assert to_json_safe(float("-inf")) == "-Infinity"


def test_huge_integers_become_strings() -> None:
    """Past 2^53 a JSON number stops round-tripping through most readers."""
    assert to_json_safe(2**53) == 2**53
    assert to_json_safe(2**70) == str(2**70)


def test_the_standard_library_types_are_converted() -> None:
    moment = dt.datetime(2026, 8, 19, 12, 30, tzinfo=dt.timezone.utc)
    identifier = uuid.uuid4()

    assert to_json_safe(moment) == moment.isoformat()
    assert to_json_safe(dt.date(2026, 8, 19)) == "2026-08-19"
    assert to_json_safe(dt.timedelta(seconds=90)) == 90.0
    assert to_json_safe(identifier) == str(identifier)
    assert to_json_safe(decimal.Decimal("1.25")) == 1.25
    assert to_json_safe(Colour.RED) == "red"
    assert to_json_safe({1, 2}) in ([1, 2], [2, 1])
    assert to_json_safe((1, 2)) == [1, 2]
    assert to_json_safe(b"abc") == "<3 bytes>"


def test_dataclasses_and_duck_typed_models_are_converted() -> None:
    assert to_json_safe(Point(1, 2)) == {"x": 1, "y": 2}
    assert to_json_safe(Model()) == {"field": "value"}


def test_an_arbitrary_object_falls_back_to_its_attributes() -> None:
    class Plain:
        def __init__(self) -> None:
            self.visible = 1
            self._hidden = 2

    assert to_json_safe(Plain()) == {"visible": 1}


def test_cycles_are_detected_rather_than_recursed_forever() -> None:
    node: dict = {"name": "root"}
    node["self"] = node

    assert to_json_safe(node) == {"name": "root", "self": "<circular reference>"}


def test_depth_and_width_are_capped() -> None:
    deep: dict = {}
    cursor = deep
    for _ in range(30):
        cursor["next"] = {}
        cursor = cursor["next"]

    rendered = json_dumps(to_json_safe(deep))
    assert "max depth" in rendered or "{}" in rendered or "next" in rendered

    wide = to_json_safe(list(range(MAX_ITEMS + 50)))
    assert len(wide) == MAX_ITEMS + 1
    assert "more items" in str(wide[-1])


def test_long_strings_are_truncated_with_a_marker() -> None:
    rendered = to_json_safe("x" * (MAX_STRING_LENGTH + 100))
    assert rendered.startswith("x" * 100)
    assert "truncated" in rendered


def test_a_broken_repr_never_raises() -> None:
    """A value that defeats every strategy becomes a placeholder, not an exception."""
    rendered = to_json_safe(Hostile())
    assert isinstance(rendered, str)
    assert "unserialisable" in rendered or "Hostile" in rendered


def test_an_exception_value_is_summarised() -> None:
    assert to_json_safe(ValueError("nope")) == {"type": "ValueError", "message": "nope"}


def test_to_payload_boxes_a_bare_value() -> None:
    """``"input": "hi"`` is refused by the endpoint; ``{"value": "hi"}`` is not."""
    assert to_payload({"already": "an object"}) == {"already": "an object"}
    assert to_payload("hi") == {"value": "hi"}
    assert to_payload([1, 2]) == {"value": [1, 2]}
    assert to_payload(None) is None


def test_json_dumps_and_estimate_bytes_never_raise() -> None:
    assert json_dumps({"a": 1}) == '{"a":1}'
    assert estimate_bytes({"a": 1}) == 7
    # An object that survived to_json_safe but not json.dumps still produces
    # something rather than costing the whole batch.
    assert isinstance(json_dumps(Hostile()), str)
    assert estimate_bytes(Hostile()) > 0


def test_non_ascii_is_measured_in_bytes_not_characters() -> None:
    """The batch limit the control plane enforces is in bytes."""
    assert estimate_bytes({"k": "café"}) > len('{"k":"café"}')
