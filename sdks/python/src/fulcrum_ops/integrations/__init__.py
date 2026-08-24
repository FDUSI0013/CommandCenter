"""Provider wrappers — tracing that does not change your call sites.

Every integration in here is imported lazily. The base package must install and
import cleanly on a machine with neither OpenAI, Anthropic nor LangChain
present, because most agents use exactly one of them and none of them should be
a dependency of the SDK.

::

    from fulcrum_ops.integrations import track_openai

    openai_client = track_openai(OpenAI())
    # every .chat.completions.create call from here on is a span

The module-level ``__getattr__`` below is what makes ``from
fulcrum_ops.integrations import track_openai`` work without importing the
Anthropic or LangChain modules alongside it.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "track_openai",
    "track_anthropic",
    "FulcrumOpsCallbackHandler",
    "LangChainCallbackHandler",
    "create_langchain_handler",
]

_LAZY = {
    "track_openai": ("fulcrum_ops.integrations.openai", "track_openai"),
    "track_anthropic": ("fulcrum_ops.integrations.anthropic", "track_anthropic"),
    "FulcrumOpsCallbackHandler": (
        "fulcrum_ops.integrations.langchain",
        "FulcrumOpsCallbackHandler",
    ),
    "LangChainCallbackHandler": (
        "fulcrum_ops.integrations.langchain",
        "LangChainCallbackHandler",
    ),
    "create_langchain_handler": (
        "fulcrum_ops.integrations.langchain",
        "create_langchain_handler",
    ),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError("module {0!r} has no attribute {1!r}".format(__name__, name))
    module_name, attribute = target
    import importlib

    return getattr(importlib.import_module(module_name), attribute)


def __dir__() -> Any:  # pragma: no cover - REPL convenience
    return sorted(__all__)
