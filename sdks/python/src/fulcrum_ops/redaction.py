"""Local redaction — the rules from ``GET /ingest/config``, applied at source.

The control plane can mask content after it arrives, but by then the content has
already crossed the network and been written to a request log. So every
guardrail set to *Mask*, plus any rule an operator wrote into the workspace
settings, is shipped down to the SDK and applied here, before the payload leaves
the customer's process. That is the whole point of the ``redaction`` array in
the bootstrap document.

Two kinds of rule arrive. A ``pattern`` is a regular expression the server
wrote. ``entity_types`` names classes like ``email`` that every SDK is expected
to recognise on its own, so the server does not have to ship a regex for the
things everyone can match.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

__all__ = [
    "RedactionRule",
    "CompiledRule",
    "SUPPORTED_ENTITY_TYPES",
    "compile_rules",
    "redact_text",
    "redact_value",
    "redact_field",
]

DEFAULT_REPLACEMENT = "[redacted by policy]"
DEFAULT_FIELDS = ("input", "output")
VALID_FIELDS = frozenset({"input", "output", "metadata"})

#: The named entity classes this SDK can match without a server-supplied pattern.
#:
#: Deliberately conservative. A pattern that over-matches silently destroys
#: telemetry; a missed match is visible in the console, where an operator can
#: write a workspace rule for it. Each is anchored on structure rather than on a
#: loose character class for that reason.
_ENTITY_PATTERNS: Dict[str, str] = {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "phone": r"(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3,4}[ .-]\d{3,4}(?:[ .-]\d{2,4})?",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "credit_card": r"\b(?:\d[ -]?){13,19}\b",
    "ip": r"\b(?:\d{1,3}\.){3}\d{1,3}\b|\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b",
    "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "ipv6": r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{1,4}\b",
    "url": r"\bhttps?://[^\s\"'<>]+",
    "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
    "api_key": r"\b(?:sk|pk|rk|api|key|token)[-_][A-Za-z0-9_-]{16,}\b",
    "aws_access_key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "jwt": r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
}

SUPPORTED_ENTITY_TYPES: Sequence[str] = tuple(sorted(_ENTITY_PATTERNS))


class RedactionRule(dict):
    """A rule as the config endpoint sends it.

    A ``dict`` subclass rather than a dataclass so a caller can paste the JSON
    from ``/ingest/config`` straight into ``redaction=[...]`` and have it work.
    """

    def __init__(
        self,
        *,
        id: str = "local",  # noqa: A002 - matches the wire field name
        name: str = "local",
        source: str = "sdk",
        entity_types: Optional[Sequence[str]] = None,
        pattern: Optional[str] = None,
        replacement: str = DEFAULT_REPLACEMENT,
        applies_to: Optional[Sequence[str]] = None,
    ) -> None:
        super().__init__(
            id=id,
            name=name,
            source=source,
            entity_types=list(entity_types or []),
            pattern=pattern,
            replacement=replacement,
            applies_to=list(applies_to or DEFAULT_FIELDS),
        )


class CompiledRule:
    """A rule with its expressions compiled once, rather than per payload."""

    __slots__ = ("id", "name", "replacement", "fields", "patterns", "unsupported_entity_types")

    def __init__(
        self,
        rule_id: str,
        name: str,
        replacement: str,
        fields: Set[str],
        patterns: List["re.Pattern[str]"],
        unsupported: List[str],
    ) -> None:
        self.id = rule_id
        self.name = name
        self.replacement = replacement
        self.fields = fields
        self.patterns = patterns
        self.unsupported_entity_types = unsupported

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "CompiledRule(id={0!r}, name={1!r}, fields={2})".format(
            self.id, self.name, sorted(self.fields)
        )


def _normalise_entity(name: Any) -> str:
    return re.sub(r"[\s-]+", "_", str(name).strip().lower())


def compile_rules(rules: Optional[Iterable[Mapping[str, Any]]]) -> List[CompiledRule]:
    """Compile the rules the config endpoint sent.

    A rule whose expression this engine cannot parse is dropped rather than
    raised: the server's regex flavour and Python's do not agree on everything,
    and one operator's typo must not stop the other rules from protecting
    anything.
    """
    if not rules:
        return []

    compiled: List[CompiledRule] = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue

        patterns: List["re.Pattern[str]"] = []
        unsupported: List[str] = []

        raw_pattern = rule.get("pattern")
        if isinstance(raw_pattern, str) and raw_pattern.strip():
            try:
                patterns.append(re.compile(raw_pattern))
            except re.error:
                pass

        for entity in rule.get("entity_types") or []:
            key = _normalise_entity(entity)
            source = _ENTITY_PATTERNS.get(key)
            if source:
                flags = re.IGNORECASE if key == "api_key" else 0
                patterns.append(re.compile(source, flags))
            else:
                unsupported.append(str(entity))

        if not patterns:
            continue

        fields = {
            str(field).strip().lower()
            for field in (rule.get("applies_to") or DEFAULT_FIELDS)
            if str(field).strip().lower() in VALID_FIELDS
        }
        if not fields:
            fields = set(DEFAULT_FIELDS)

        replacement = rule.get("replacement") or DEFAULT_REPLACEMENT
        compiled.append(
            CompiledRule(
                rule_id=str(rule.get("id") or "rule"),
                name=str(rule.get("name") or "rule"),
                replacement=str(replacement),
                fields=fields,
                patterns=patterns,
                unsupported=unsupported,
            )
        )

    return compiled


def redact_text(value: str, rules: Sequence[CompiledRule]) -> str:
    """Apply every rule's expressions to one string."""
    out = value
    for rule in rules:
        for pattern in rule.patterns:
            try:
                out = pattern.sub(rule.replacement, out)
            except Exception:
                # A catastrophic backtrack or a bad replacement template must
                # not cost the caller their span.
                continue
    return out


def redact_value(value: Any, rules: Sequence[CompiledRule]) -> Any:
    """Walk an already-JSON-safe structure, redacting every string in it.

    Keys are redacted as well as values: a dict keyed by customer email address
    leaks exactly as much as one that stores it in the value.
    """
    if not rules:
        return value
    if isinstance(value, str):
        return redact_text(value, rules)
    if isinstance(value, list):
        return [redact_value(item, rules) for item in value]
    if isinstance(value, dict):
        return {redact_text(str(k), rules): redact_value(v, rules) for k, v in value.items()}
    return value


def redact_field(value: Any, field: str, rules: Sequence[CompiledRule]) -> Any:
    """Redact one named field with only the rules that claim to cover it."""
    if not rules or value is None:
        return value
    applicable = [rule for rule in rules if field in rule.fields]
    if not applicable:
        return value
    return redact_value(value, applicable)
