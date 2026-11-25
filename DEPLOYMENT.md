# Deployment and durable history

The container filesystem on a light hosting plan is recycled on restart and redeploy. Local SQLite is therefore suitable for local development only. Monitoring history on a hosted service is durable **only** with an external managed PostgreSQL database configured by the repository owner.

This document records the manual setup and verification. Nothing here provisions external resources, invents credentials, or commits connection strings.

## Manual Render configuration

1. **Provision the database.** Create a managed PostgreSQL database on a **non-expiring** plan. Render Free Postgres expires after 30 days and is acceptable only for a short-lived demo; use a paid Render Postgres plan or another managed provider (for example Neon) for history you need to keep.
2. **Set `DATABASE_URL` in the Render service.** Add it as a service environment variable/secret (`sync: false` in `render.yaml`). Do not put the connection string in the repository. Use the provider's external connection string with the SSL mode it requires; many hosted providers need `?sslmode=require` appended to the URL. A wrong SSL setting or an expired free database will make initialization fail.
3. **Redeploy the service.** The new environment variable is applied on redeploy. On startup the service imports the committed baseline once, keyed by cycle id; existing rows are never cleared.
4. **Check the reported storage state.** Request `GET /config` and confirm:
   ```
   storage.backend = postgres
   storage.durable = true
   storage.accepts_writes = true
   storage.new_checks_enabled = true
   ```
   `GET /health` exposes the same nested `storage` object, so operations and the dashboard see one state. If the backend is reported as `sqlite-fallback` while `DATABASE_URL` is configured, PostgreSQL did not initialize (`storage.fallback = true`, `storage.accepts_writes = false`, `storage.new_checks_enabled = false`): fix the URL/SSL settings and redeploy. In that state the dashboard remains read-only for new cycles and `POST /runs` returns HTTP 503 rather than writing history to an ephemeral database.
5. **Confirm the baseline import once.** After startup, the baseline manifest and its reference cycles are present exactly once. A restart or redeploy must not duplicate the baseline cycles or their detector results.
6. **Run a monitoring cycle.** Trigger one cycle and confirm it completes and appears in `/runs` and `/drift/latest`.
7. **Restart or redeploy the service.** This is the durability check that matters: the container filesystem is discarded.
8. **Verify the previous cycle survived.** After the restart, confirm the earlier cycle, its **40 probe records**, and its detector result are still available through `/runs` and `/drift/latest`.
9. **Append another cycle.** Run a second cycle and verify it is appended as a new cycle with its own probe rows and detector result, without a second copy of the baseline.

Until steps 7–9 have been performed against the real deployment, hosted history must not be described as durable. The code path alone is not proof of persistence.

## Serverless connection recovery (Neon Free + Render Free)

Both services can go idle: Render recycles the container and Neon suspends compute. A warm PostgreSQL connection can therefore look open in the pool while its SSL session has already been closed by the provider. The deployed requirements use `psycopg_pool>=3.2,<4` (verified locally with psycopg 3.3.9 / psycopg_pool 3.3.3) so the pool can use the checkout `check` callback introduced in 3.2.

The pool still keeps one warm connection and grows to two at most. On every checkout the pool runs its documented `check_connection()` callback; a stale socket is discarded and a fresh connection is attempted with the pool's existing backoff, not a tight reconnect loop. Warm connections are recycled after 15 minutes (`max_lifetime`) and spare connections after two minutes idle (`max_idle`), with the Neon URL's SSL parameters unchanged. No SQL write is replayed: if a connection dies after a statement reaches PostgreSQL, the outcome stays ambiguous rather than creating duplicate rows.

Storage reports four separate facts: backend/configuration, durability, current reachability (`storage.reachable`) and whether new checks are safe (`storage.new_checks_enabled`). `GET /health` performs a real `SELECT 1` through the pool and reports `database: available` or `unavailable`; `GET /health/live` remains process liveness only. `POST /runs` performs the same cheap read before queueing and returns HTTP 503 while the database is unavailable. Dashboard reads use the short-lived cache and do not force reconnects. A failed configured database never switches new writes to SQLite.

### Manual verification A–D

Run these against the real Render/Neon deployment after redeploying this change. Do not run destructive database tests against production Neon.

**A. Normal readiness**

```bash
curl -s https://llm-drift-monitor-api.onrender.com/health | python3 -m json.tool
```

Expect `database: "available"`, `db_error: null`, `storage_backend: "postgres"`, `storage_durable: true`, `storage.reachable: true`, and `storage.new_checks_enabled: true`. `/health/live` must still return `{"status":"ok"}` with HTTP 200.

**B. Recovery after inactivity**

Leave the service and database idle long enough for Neon compute to suspend (at least the provider's scale-to-zero window). Request `/health` once; a cold-start delay is acceptable. Expect `database: "available"` and no `db_error` after the request completes, not a stuck `SSL connection has been closed unexpectedly`. Repeat `/health` immediately and confirm the second request also reports available without an error.

**C. Recovery after Render restart**

Restart the Render service, then request `/health` and `/config`; both must show the PostgreSQL backend, durable true, reachable true, and new checks enabled. Check the startup log's `baseline_import` line: because the four baseline cycles are already in Neon, it must report `imported_cycles: 0`, `imported_runs: 0`, `imported_test_results: 0`, `skipped_cycles: 4` (the implementation's idempotent-skip semantics). No baseline rows may be recreated or overwritten.

**D. Persistence**

Before triggering a new cycle, `/config` (or `get_diagnostics`) must still show `probe_runs: 160`, `cycles: 4`, `reference_baseline_cycles: 3`, and `test_results: 1`. Only then start one monitoring cycle. After it completes, confirm the new cycle has 40 probe records and one detector result. Restart Render once more and confirm the new cycle, its 40 probes and its detector result remain available, with no duplicate baseline. Mocked tests prove only the local failure paths; they do not prove live Neon recovery.

## Local development

With `DATABASE_URL` unset, the service uses local SQLite as usual:

```bash
python -m scripts.generate_seed_db
DRIFT_MONITOR_CONFIG=config/config.cloud.yaml uvicorn api:app --reload --port 8000
```

`GET /config` reports the SQLite backend as non-durable. This is expected for local work and demo data; it is not the hosted persistence path.

## Demo dashboard artifact

The Demo tab reads the committed, read-only `web/data/demo-scenarios.json` through `/static/data/demo-scenarios.json`. It makes no `/runs` call and never changes the live monitor, provider configuration or stored history. The artifact contains two deterministic scenarios whose synthetic responses were processed by the real `run_monitoring_cycle()` pipeline in a temporary SQLite database.

Regenerate it deliberately after changing the detector pipeline or the synthetic scenario design:

```bash
python -m scripts.generate_demo_artifacts
```

The generator neutralises `DATABASE_URL`, provider keys and network model resolution, refuses any storage path outside its own temporary directory, and writes the output atomically. Commit the regenerated artifact only after reviewing the scenario summaries it prints.

## Tests

Run `ruff check .`, `python -m pytest -q`, and `node web/smoke.test.js`. PostgreSQL integration tests are reported as skipped unless `TEST_DATABASE_URL` is set to a dedicated disposable database; run them separately with `python -m pytest -q -k postgres -rs`. Never point `TEST_DATABASE_URL` at a production database: those tests clear rows and recreate a legacy schema, then clean up their own test data. They do not establish that the real hosted deployment is durable; only the restart/redeploy verification above does.
