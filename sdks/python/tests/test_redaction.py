"""Local redaction, applied before content leaves the process."""

from __future__ import annotations

from fulcrum_ops.redaction import (
    SUPPORTED_ENTITY_TYPES,
    compile_rules,
    redact_field,
    redact_text,
    redact_value,
)


def rule(**overrides) -> dict:
    body = {
        "id": "r1",
        "name": "rule",
        "source": "workspace",
        "replacement": "[redacted]",
        "applies_to": ["input", "output"],
    }
    body.update(overrides)
    return body


def test_a_server_pattern_is_compiled_and_applied() -> None:
    rules = compile_rules([rule(pattern=r"EMP-\d{6}")])
    assert redact_text("ticket for EMP-123456 today", rules) == "ticket for [redacted] today"


def test_named_entity_types_are_matched_without_a_server_pattern() -> None:
    rules = compile_rules([rule(entity_types=["email", "ssn"])])
    redacted = redact_text("ada@example.com and 123-45-6789", rules)
    assert redacted == "[redacted] and [redacted]"


def test_every_advertised_entity_type_actually_compiles() -> None:
    """The list in the README is the list the code can honour."""
    rules = compile_rules([rule(entity_types=list(SUPPORTED_ENTITY_TYPES))])
    assert len(rules) == 1
    assert len(rules[0].patterns) == len(SUPPORTED_ENTITY_TYPES)
    assert rules[0].unsupported_entity_types == []


def test_the_common_secrets_are_matched() -> None:
    rules = compile_rules([rule(entity_types=["api_key", "aws_access_key", "jwt", "credit_card"])])
    samples = [
        "sk-abcdefghijklmnopqrstuvwx",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "4111 1111 1111 1111",
    ]
    for sample in samples:
        assert redact_text(sample, rules) == "[redacted]", sample


def test_an_unparseable_pattern_is_dropped_and_the_rest_still_protect() -> None:
    """One operator's typo must not stop every other rule working."""
    rules = compile_rules(
        [
            rule(id="bad", pattern="([unclosed"),
            rule(id="good", pattern=r"SECRET-\d+"),
        ]
    )
    assert [compiled.id for compiled in rules] == ["good"]
    assert redact_text("SECRET-9", rules) == "[redacted]"


def test_an_unknown_entity_type_is_reported_not_guessed() -> None:
    rules = compile_rules([rule(entity_types=["email", "medical_record_number"])])
    assert rules[0].unsupported_entity_types == ["medical_record_number"]


def test_a_rule_with_nothing_to_match_is_skipped() -> None:
    assert compile_rules([rule()]) == []
    assert compile_rules([rule(entity_types=["not_a_thing"])]) == []
    assert compile_rules(None) == []
    assert compile_rules(["not a mapping"]) == []


def test_entity_names_are_normalised() -> None:
    rules = compile_rules([rule(entity_types=[" Credit-Card ", "AWS access key"])])
    assert rules[0].unsupported_entity_types == []


def test_redaction_walks_nested_structures_and_keys() -> None:
    """A dict keyed by email address leaks as much as one storing it in the value."""
    rules = compile_rules([rule(entity_types=["email"])])
    payload = {
        "ada@example.com": {"note": "reply to bob@example.com"},
        "list": ["carol@example.com", 42, None, True],
    }

    assert redact_value(payload, rules) == {
        "[redacted]": {"note": "reply to [redacted]"},
        "list": ["[redacted]", 42, None, True],
    }


def test_a_rule_only_touches_the_fields_it_claims() -> None:
    rules = compile_rules([rule(entity_types=["email"], applies_to=["output"])])

    assert redact_field({"a": "ada@example.com"}, "input", rules) == {"a": "ada@example.com"}
    assert redact_field({"a": "ada@example.com"}, "output", rules) == {"a": "[redacted]"}


def test_an_empty_applies_to_falls_back_to_input_and_output() -> None:
    rules = compile_rules([rule(entity_types=["email"], applies_to=[])])
    assert rules[0].fields == {"input", "output"}


def test_an_invalid_field_name_is_ignored() -> None:
    rules = compile_rules([rule(entity_types=["email"], applies_to=["input", "elsewhere"])])
    assert rules[0].fields == {"input"}


def test_the_default_replacement_is_used_when_none_is_given() -> None:
    rules = compile_rules([{"id": "r", "name": "r", "source": "s", "entity_types": ["email"]}])
    assert redact_text("ada@example.com", rules) == "[redacted by policy]"


def test_no_rules_is_a_cheap_no_op() -> None:
    payload = {"a": "ada@example.com"}
    assert redact_value(payload, []) is payload
    assert redact_field(payload, "input", []) is payload
    assert redact_field(None, "input", compile_rules([rule(entity_types=["email"])])) is None
