# Third-party licences

Fulcrum Ops embeds a telemetry engine distributed under the **Apache License,
Version 2.0**. That licence permits commercial redistribution and rebranding; it
grants no trademark rights, and it imposes no attribution requirement on a
product's user interface. It does require that redistributed source or binaries
retain the licence text and any accompanying notices, which is what this file
and `THIRD_PARTY_LICENCE.txt` are for.

This file is repository documentation. It is not bundled into the console, the
SDKs or any customer-facing artefact, and nothing in the product renders it.

## Embedded components

| Component | Role in Fulcrum Ops | Licence |
|---|---|---|
| Telemetry engine (Java/Dropwizard) | Stores and queries traces, spans, threads, feedback scores, prompts, datasets, experiments, guardrail definitions and cost records. Runs on the private compose network as `telemetry-engine`; never publicly addressable. | Apache-2.0 |
| Metric runner (Python) | Executes user-defined evaluation metrics in a sandbox. Runs as `metric-runner`. | Apache-2.0 |
| Safety scanner (Python) | Guardrail inference for PII, topic and content checks. Runs as `safety-scanner`. | Apache-2.0 |

The full upstream licence text is preserved verbatim in
[`THIRD_PARTY_LICENCE.txt`](THIRD_PARTY_LICENCE.txt), written there by
`engine/fetch-vendor.sh` when the source is fetched.

## Datastores

Stock upstream images, unmodified, pulled at deploy time: MySQL (GPL-2.0 with the
FOSS exception), ClickHouse (Apache-2.0), ZooKeeper (Apache-2.0), Redis
(BSD-3-Clause for the 7.2 line), MinIO (AGPL-3.0, used as an unmodified network
service and not linked into our code), PostgreSQL (PostgreSQL Licence), Caddy
(Apache-2.0).

## Our own dependencies

The control plane's Python dependencies and their licences are resolvable from
`apps/control-plane/pyproject.toml`; the SDKs declare theirs in their respective
manifests. All are permissive (MIT, BSD, Apache-2.0, PSF).

## What "white-labelled" means here, concretely

Product surfaces — the console, the API contract, both SDKs, package names,
environment variables, log output, container labels and image tags — carry only
Fulcrum Ops naming. The upstream project's name appears in exactly two places,
both repository-only and neither shipped: this licence documentation, and the
unmodified source under `engine/vendor/`, which is not committed and is fetched
at build time. `scripts/check-branding.sh` enforces that boundary in CI.
