# Agentic Procurement Approval Navigator

A local, reviewable portfolio application for coordinating procurement requests across policy checks, budget validation, supplier evidence, and accountable approval. It is an operational demo, not an autonomous purchasing system: it cannot issue POs, contact suppliers, or approve spending. Every final decision requires a named human approver.

## Quick start

Requires Python 3.11+.

```powershell
cd outputs
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. No API key is needed: the default `AI_MODE=demo` uses deterministic classification and planning with real retrieval, validation, policy rules, and approval gates. To enable an optional model-based exception-planning agent, set `AI_MODE=openai` and `OPENAI_API_KEY` in `.env`; deterministic controls remain authoritative and model suggestions are advisory only. `AI_MODE=demo` is the reproducible default.

On first startup, the app creates `data/procurement.db`, loads the included data source, and indexes its documents into the local Chroma vector store at `data/chroma/`. Reset local demo state by stopping the server and removing those two generated paths, then restarting.

## What it demonstrates

- Specialized workflow stages: intake/classification, policy and evidence retrieval, budget validation, supplier risk review, exception planning, approver routing, and decision capture.
- Explicit LangGraph state and conditional routing, typed Pydantic schemas, deterministic Python tools, bounded retry/fallback, and a human-review stop.
- Hybrid retrieval (Chroma semantic search plus SQLite FTS lexical search) with source citations and provenance metadata.
- Persistent requests, evidence, audit events, and decisions in SQLite; request-specific short-term conversation history.
- FastAPI API, simple review UI, structured logs, health/metrics endpoints, seeded evaluation scenarios, and container deployment files.

## Data source

The complete synthetic source pack is in [`data/source/`](data/source/README.md). It contains procurement policy, approval matrix, budget ledger, supplier master, contracts, and purchase request scenarios. Records carry source ID, owner, effective/expiry dates, version, classification, and access scope. All names and business figures are fictional. The app refuses to treat expired evidence as current and records citations with decisions.

## Workflow and controls

`POST /api/requests/analyze` runs the graph and stops at `pending_human_review` or `blocked`. A human approver reviews the evidence and submits `POST /api/requests/{request_id}/decision`; only an authorized approver role can approve/reject. The API requires an idempotency key for decision writes. Missing, stale, conflicting, or insufficient evidence is surfaced as an exception; model text cannot override deterministic policy or budget findings.

Key endpoints: `GET /api/health`, `GET /api/requests`, `GET /api/requests/{id}`, `POST /api/requests/analyze`, `POST /api/requests/{id}/decision`, `GET /api/metrics`, `GET /api/evaluations`.

## Configuration

See `.env.example`. Local demo auth uses `X-User-Id` and `X-User-Role` headers (default: `demo-requester` / `requester`). Replace this boundary with your organization’s identity provider before deployment. The seeded approver is `demo-approver` with role `approver`.

## Evaluation plan

`GET /api/evaluations` reports a seeded, reviewable set of normal and failure scenarios against policy compliance, routing accuracy, calculation correctness, evidence coverage, cycle time, and override rate. These are illustrative scenario expectations, not measured production results. The application records the fields needed to calculate those metrics from real labeled runs. No unsubstantiated performance claims are made.

## Production hardening before deployment

Add enterprise SSO/RBAC, secrets manager, managed Postgres/vector service, tenant isolation, document ingestion approval, retention policy, rate limiting, network egress restrictions, security review, and an organization-specific evaluation corpus. The demo intentionally uses synthetic data and local storage.
