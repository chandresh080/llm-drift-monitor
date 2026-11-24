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

## Local development

With `DATABASE_URL` unset, the service uses local SQLite as usual:

```bash
python -m scripts.generate_seed_db
DRIFT_MONITOR_CONFIG=config/config.cloud.yaml uvicorn api:app --reload --port 8000
```

`GET /config` reports the SQLite backend as non-durable. This is expected for local work and demo data; it is not the hosted persistence path.

## Tests

Run `ruff check .`, `python -m pytest -q`, and `node web/smoke.test.js`. PostgreSQL integration tests are reported as skipped unless `TEST_DATABASE_URL` is set to a dedicated disposable database; run them separately with `python -m pytest -q -k postgres -rs`. Never point `TEST_DATABASE_URL` at a production database: those tests clear rows and recreate a legacy schema, then clean up their own test data. They do not establish that the real hosted deployment is durable; only the restart/redeploy verification above does.
