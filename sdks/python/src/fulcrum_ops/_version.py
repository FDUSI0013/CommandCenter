"""Identity this SDK reports on every batch, so the console can tell clients apart."""

#: The ``sdk`` field on every ingest body.
SDK_NAME = "python"

#: Kept in step with ``pyproject.toml`` by hand; there is no build-time inlining.
SDK_VERSION = "1.0.0"

#: Sent as ``User-Agent`` on every request.
USER_AGENT = "fulcrum-ops-sdk-{0}/{1}".format(SDK_NAME, SDK_VERSION)

__all__ = ["SDK_NAME", "SDK_VERSION", "USER_AGENT"]
