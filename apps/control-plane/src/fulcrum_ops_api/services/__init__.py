"""Business logic.

Every service is a plain class constructed with an ``AsyncSession`` and, where
telemetry is involved, the engine adapter. Services never import FastAPI: that
keeps them usable from the CLI, background jobs and tests without a request.
"""
