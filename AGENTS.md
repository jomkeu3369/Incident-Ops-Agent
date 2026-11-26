# AGENTS.md

Incident-Ops-Agent — Korean-language multi-agent incident response workspace.

## Layout & Entrypoints

- `backend/` — Java 21, Spring Boot domain REST/MCP, MySQL with Flyway migrations (`db/migration/`) and MyBatis (`mapper/`).
- `agent/` — Python 3.12+, three FastAPI apps sharing `agent/src/common/`:
  - `supervisor.main:app` — AG-UI SSE, LangGraph orchestration, Redis memory/logs/plans.
  - `log_agent.main:app` — A2A worker for DR-DCI corpus retrieval via Spring MCP.
  - `remediation_agent.main:app` — A2A worker for external tools and evidence judgment.
- `frontend/` — Node.js 20.9+, Next.js UI and server-side BFF.
- `infra/Caddyfile` — single-origin gateway. `docs/` — architecture, API specs (`02-*`, `09-*`), verification (`10-verification.md`).

## Commands

Run each command from the listed directory. These examples work in Windows PowerShell 5.1.

- **`backend/`** — Install JDK 21 and Maven before running Spring or tests locally (no `mvnw`). Unit tests run without a DB:
  - All: `mvn test`
  - Single: `mvn '-Dtest=IncidentWorkflowTest#approvalCopiesTheActionFromTheStoredPlanStep' test`
- **`agent/`** — First run `python -m pip install -e ".[dev,eval]"`. Tests run offline:
  - All: `python -m pytest`
  - Single: `python -m pytest tests/test_p0_1_evidence_judge_and_eval.py -k 'test_rule_based_judge_fail_closed_behavior'`
  - Offline eval: `python -m eval --judges rule_based`
- **`frontend/`** — After `npm ci`, run `npm run typecheck`, `npm run build`, then `npm test`. To run one test file, run `npx tsc -p tsconfig.test.json`, then `node --test .test-dist/tests/ag-ui-phase2.test.js`.

See [README.md §4](README.md#4-실행-방법) for startup details. Run Compose from the repository root; start infra first because it creates `incident-ops-net`. To seed `db/demo/data.sql`, set `SPRING_PROFILES_ACTIVE=docker,demo` in the root `.env` or run `$env:SPRING_PROFILES_ACTIVE = 'docker,demo'` in PowerShell before starting the app stack.

## Environment & Authentication

- **Compose:** Copy `.env.example` to the repository root `.env` and fill in a non-empty `INTERNAL_SERVICE_TOKEN`; the app stack cannot start with the template's blank value. The template's MySQL, Redis, backend and A2A hostnames are Docker DNS names.
- **Host processes:** Agent auto-loads the root `.env` (`agent/src/common/config.py`, `parents[3]`). Override `REDIS_URL`, `SPRING_MCP_SSE_URL`, the A2A URLs, and `BACKEND_BASE_URL` if set, with `localhost` and reachable ports. `docker-compose-infra.yaml` does not publish Redis; a host supervisor needs a reachable Redis instance. Spring and Next do not auto-load the root `.env`; inject settings into their process environments. For host Spring, point `SPRING_DATASOURCE_URL` at the published MySQL port rather than relying on the Spring default. For standalone live Next, set `SUPERVISOR_URL` (enables `/api/ag-ui*` rewrites), `BACKEND_INTERNAL_URL`, and `INTERNAL_SERVICE_TOKEN`.
- **Internal token boundary:** Fail-closed `X-Internal-Token` validation (`401` for missing/invalid tokens or an unset shared token) applies only to protected Spring routes (`/api/*`, `/sse`, `/mcp/message`; `/actuator/health*`, `/swagger-ui*`, `/v3/api-docs*` are exempt) and FastAPI A2A worker endpoints (RPC and `/internal/*`). Optional `INTERNAL_INVESTIGATOR_TOKEN` (must differ from the shared token) is a read-only identity: Spring forces the `investigator` role for it regardless of headers, the supervisor uses it to call `log_agent`, and Compose gives `log_agent` only that token. Holders of the shared token remain trusted writers (`X-Agent-Role` is self-declared; unknown values get `403`). Credential isolation applies to Compose; host processes that share the root `.env` also read the shared token, and Spring needs the investigator token injected or `log_agent` gets `401`. For read-only MCP requests `InternalTokenFilter` is the enforcement boundary (tool callbacks run on other threads), so it accepts only a single `application/json` Content-Type with UTF-8 and strictly parsed JSON. Browser-facing supervisor routes (`/api/ag-ui` and its log/plan management routes) do not check the internal token. Caddy is a router, not an authentication boundary. There is no user authentication, so use this setup only locally or in a trusted environment. Keep internal tokens server-side; never put secrets in browser code/requests, responses, or execution logs.

## Frontend Integration Tests

- `TEST_AG_UI_BASE_URL` is the base URL of a running Next.js or Caddy instance. Without it, four integration tests skip. Three (AG-UI SSE, plan CRUD, session logs) use Next's `/bff/mock-ag-ui` routes and do not require a real supervisor.
- The SSR/BFF test (`npm run test:ssr-bff`) also needs a real Spring backend behind the Next BFF. Set `TEST_SECRET_TOKEN` to the secret value that must not appear in response bodies; the test does not send it for authentication. By default it matches `db/demo/data.sql` (`INC-2026-001` / `결제 API 502 Bad Gateway 급증 및 응답 지연` with seeded plans and approvals); set `TEST_EXPECTED_POSTMORTEM_ROOT_CAUSE` (and optionally `TEST_EXPECTED_TITLE`) to assert a completed postmortem fixture.
- When adding a test file, update only `scripts.test` in `frontend/package.json`: `tsconfig.test.json` already includes `tests/**/*.ts` and compiles app modules imported by tests.

## Non-Obvious Architecture & Invariants

- **Caddy routing:** `/api/ag-ui*` goes to the supervisor; remaining `/api/*` goes to Spring. Keep Next.js Route Handlers under `frontend/app/bff/`, not `app/api/`. `frontend/app/backend-api.ts` attaches the internal token server-side.
- **DR-DCI:** `log_agent` searches Spring MCP (`searchCorpusIndex`) and pulls selected chunks one at a time (`pullCorpusChunk`, up to `DR_DCI_MAX_PULL_STEPS`). Only summaries, not raw chunks, enter the reasoning context (`DR_DCI_MAX_CHUNK_CHARS` caps summary length after retrieval; runbook summaries preserve line breaks). `DR_DCI_MODE=agentic` enables the read-only adaptive/LLM selector loop (`agent/src/log_agent/agentic_dr_dci.py`) with server-enforced allowlist, deduplication, and step/char/timeout budgets.
- **Planner vs. evidence judge:** Rollback selection and scheduling are deterministic (`agent/src/common/planner_math.py`). Evidence judgment (`agent/src/common/decisions.py`) defaults to `decisions_api` and fails closed with `UNDECIDABLE` on API failure. `EVIDENCE_JUDGE_MODE=auto` retries with Structured Outputs only when the primary Decisions API returns `UNDECIDABLE`.
- **Simulated execution & approval:** Real cluster commands are never run. `IncidentRecoveryService` requires an `APPROVED` approval linked to a stored plan for a new execution record. A retry can return an existing legacy result without recording a new execution when incident, command and execution status match. Conflicts return HTTP `409` via REST; `agent/src/common/backend_client.py` normalizes MCP tool errors to `409`. `X-Agent-Role: investigator` (`log_agent`) is read-only and blocked with `403` / MCP tool rejection on write or completion endpoints; Spring MCP also exposes the read-only resource `incident://evidence-summary/v1`.
- **AG-UI checkpointing & A2A streaming:** `supervisor` compiles LangGraph with `RedisCheckpointSaver` (`agent/src/supervisor/checkpointer.py`) across `execute_log` → `execute_remediation` so `resume=True` skips already-completed steps. The Spring approval ledger is authoritative for business state (`supervisor/ledger.py`): every transition commits a snapshot with a new `eventSeq` before emitting it, `explicitCancel=True` preempts the active run (`supervisor/active_runs.py`, single process only) without interrupting ledger writes, then derives the status from the ledger; `log_agent` supports A2A `SendStreamingMessage` (`capabilities.streaming=True`); the supervisor uses it for investigations (AG-UI `CUSTOM a2a_progress`, server task IDs, `CancelTask` on explicit cancel only). Deduplication is scoped to idempotency key + incident + request fingerprint, bounded by TTL/size, and skips partial or failed results. A custom `a2a_caller` (tests) bypasses streaming.
- **FastAPI errors and headers:** `agent/src/common/app_factory.py` maps request validation errors to `400` (not `422`) and hides internal exception messages on `500`. Middleware defaults are `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`; SSE retains `Cache-Control: no-cache`, and unhandled `500` responses may lack both default headers.
- **Audit & LangSmith tracing:** Redis execution logs (`EventBus` → `LoggingSubAgent`, no LLM calls) are append/read/delete-only; anomaly analysis runs rules first and calls no LLM when `NORMAL`. LangSmith distributed tracing (`agent/src/common/observability.py`) is strictly opt-in (`LANGSMITH_TRACING=true` + non-placeholder `LANGSMITH_API_KEY`), propagates `langsmith-trace`/`baggage` headers across A2A worker calls, redacts secrets/tokens before export, and fails safely without breaking agent execution.
