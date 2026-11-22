"""Storage for the drift monitor.

SQLite remains the default for local development and tests. A managed
external PostgreSQL database can be selected with ``DATABASE_URL``; it is
the only durable option on a container whose disk is recycled. The
PostgreSQL path deliberately keeps a very small connection pool (one warm
connection, two at most) so it fits the hosted 512 MiB instance.

Both backends expose the same public methods. The SQLite implementation is
the reference; the PostgreSQL subclass only overrides schema creation,
placeholder handling and the one insert that needs ``RETURNING id``.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("llm_drift_monitor.storage")

# Explicit probe outcomes. Empty output and a failed provider request are
# different observations: an empty response still describes the model's
# behaviour (the availability detector counts the miss), whereas a failed
# request says nothing about the model at all.
OUTCOME_SUCCESS = "success"
OUTCOME_EMPTY = "empty"
OUTCOME_FAILED = "failed"

# Marker for the one-time, non-destructive relabel of legacy rows. Old code
# wrote run_status='failed' for every invalid (empty/whitespace) response,
# because a real provider failure aborted the batch before any row existed.
_EMPTY_STATUS_MIGRATION = "legacy_empty_run_status_v1"


def is_legacy_empty_response(status: Optional[str], text: Optional[str],
                             is_valid: Any) -> bool:
    """True when a stored 'failed' row is really an empty model response.

    Used by the one-time migration and when importing the committed baseline
    into a fresh backend, so both paths apply the same, evidence-based rule.
    """
    return (status == OUTCOME_FAILED
            and not (text or "").strip()
            and not bool(is_valid))


class _Row:
    """Small row wrapper for PostgreSQL that supports both index and name
    access, matching the sqlite3.Row surface the rest of this module uses."""

    __slots__ = ("_keys", "_values", "_map")

    def __init__(self, description, values):
        self._keys = [d[0] for d in description]
        self._values = tuple(values)
        self._map = dict(zip(self._keys, self._values))

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return self._map[key]

    def __iter__(self):
        return iter(self._values)

    def keys(self):
        return list(self._keys)


class _PgCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return _Row(self._cursor.description, row)

    def fetchall(self):
        return [_Row(self._cursor.description, row)
                for row in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class _PgConnection:
    """Translate the module's SQLite-style ``?`` placeholders to psycopg's
    ``%s`` and return row objects with the same access surface."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=()):
        cursor = self._connection.cursor()
        cursor.execute(sql.replace("?", "%s"), params)
        return _PgCursor(cursor)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def commit(self):
        self._connection.commit()

    def close(self):
        # The pool owns the connection lifetime.
        pass


class DriftMonitorStorage:
    """SQLite storage for LLM drift monitoring data."""

    backend = "sqlite"
    is_durable = False

    def __init__(self, db_path: str = "data/drift_monitor.db"):
        self.db_path = db_path
        self.storage_error: Optional[str] = None
        # Ensure data directory exists. In memory databases have no path to
        # create, and an empty dirname must not become a makedirs call.
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if self.db_path != ":memory:":
            self.resolved_path = os.path.abspath(self.db_path)
        else:
            self.resolved_path = ":memory:"
        self.location = self.resolved_path
        self._init_db()

    # ---------- schema ----------

    def _migrate_columns(self, conn: sqlite3.Connection) -> None:
        """Add columns introduced after the first schema, if missing."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(probe_runs)")}
        for column in ("prompt_id TEXT", "category TEXT", "cycle_id TEXT"):
            name = column.split()[0]
            if name not in existing:
                conn.execute(f"ALTER TABLE probe_runs ADD COLUMN {column}")

    def _migrate_test_columns(self, conn: sqlite3.Connection) -> None:
        """Add effect size, distinct count and cycle link columns, if missing."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(test_results)")}
        for column in ("cycle_id TEXT",
                       "length_shift_pct REAL", "centroid_distance REAL",
                       "refusal_rate_reference REAL", "refusal_rate_current REAL",
                       "unique_reference INTEGER", "unique_current INTEGER",
                       "paired_cluster_mmd_statistic REAL",
                       "paired_cluster_mmd_pvalue REAL",
                       "paired_cluster_prompt_pairs INTEGER",
                       "provider_latency_mean_seconds REAL",
                       "provider_latency_p50_seconds REAL",
                       "provider_latency_p95_seconds REAL",
                       "probe_tokens_total INTEGER",
                       "paired_cluster_method TEXT",
                       "mmd_permutation_unit TEXT"):
            name = column.split()[0]
            if name not in existing:
                conn.execute(f"ALTER TABLE test_results ADD COLUMN {column}")

    def _migrate_alert_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
        if "cycle_id" not in existing:
            conn.execute("ALTER TABLE alerts ADD COLUMN cycle_id TEXT")

    def _migrate_feature_columns(self, conn: sqlite3.Connection) -> None:
        """Add behavioral feature columns introduced after the first schema."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(response_features)")}
        for column in ("is_refusal INTEGER", "repetition_ratio REAL", "token_count INTEGER",
                       "has_code_block INTEGER"):
            name = column.split()[0]
            if name not in existing:
                conn.execute(f"ALTER TABLE response_features ADD COLUMN {column}")

    def _init_db(self):
        """Initialize database schema."""
        with self._db() as conn:
            # WAL lets dashboard reads proceed while a run cycle is writing.
            # A persistent database property: set once here, not per
            # connection, where it would take a write lock on every query.
            conn.execute("PRAGMA journal_mode=WAL")
            # Baseline manifest: which provider, model, panel version and
            # decoding settings produced the reference window, plus when.
            # Reference windows are only comparable to runs made with the
            # same manifest, so it is stored with the data it describes.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS baseline_manifest (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS probe_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    prompt_text TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    response_text TEXT NOT NULL,
                    response_length INTEGER,
                    is_valid INTEGER,
                    run_status TEXT NOT NULL,
                    adapter_type TEXT NOT NULL
                )
            """)
            self._migrate_columns(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS response_features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    response_length INTEGER,
                    is_valid INTEGER,
                    FOREIGN KEY (run_id) REFERENCES probe_runs (id)
                )
            """)
            self._migrate_feature_columns(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS embeddings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    embedding_blob TEXT NOT NULL,  -- JSON array of floats
                    FOREIGN KEY (run_id) REFERENCES probe_runs (id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS test_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    cycle_id TEXT,
                    reference_size INTEGER,
                    current_size INTEGER,
                    ks_statistic REAL,
                    ks_pvalue REAL,
                    ks_raw_pvalue REAL,
                    ks_adjusted_pvalue REAL,
                    ks_significant INTEGER,
                    proportion_z_stat REAL,
                    proportion_z_pvalue REAL,
                    proportion_z_raw_pvalue REAL,
                    proportion_z_adjusted_pvalue REAL,
                    proportion_z_significant INTEGER,
                    mmd_statistic REAL,
                    mmd_pvalue REAL,
                    mmd_raw_pvalue REAL,
                    mmd_adjusted_pvalue REAL,
                    mmd_significant INTEGER,
                    length_shift_pct REAL,
                    centroid_distance REAL,
                    refusal_rate_reference REAL,
                    refusal_rate_current REAL,
                    unique_reference INTEGER,
                    unique_current INTEGER,
                    paired_cluster_mmd_statistic REAL,
                    paired_cluster_mmd_pvalue REAL,
                    paired_cluster_prompt_pairs INTEGER,
                    provider_latency_mean_seconds REAL,
                    provider_latency_p50_seconds REAL,
                    provider_latency_p95_seconds REAL,
                    probe_tokens_total INTEGER,
                    paired_cluster_method TEXT,
                    mmd_permutation_unit TEXT,
                    bh_significant_tests TEXT,  -- JSON list
                    overall_drift_detected INTEGER,
                    overall_quality_degradation INTEGER
                )
            """)
            self._migrate_test_columns(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    cycle_id TEXT,
                    alert_type TEXT NOT NULL,  -- 'drift' or 'quality_degradation'
                    severity TEXT NOT NULL,  -- 'low', 'medium', 'high'
                    message TEXT NOT NULL,
                    ks_pvalue REAL,
                    proportion_z_pvalue REAL,
                    mmd_pvalue REAL,
                    overall_drift_detected INTEGER,
                    overall_quality_degradation INTEGER
                )
            """)
            self._migrate_alert_columns(conn)
            # Legacy databases (including the committed seed) predate the
            # exact cycle link. Backfill it once from insert order/timestamps;
            # new rows always carry the cycle id directly.
            self._backfill_cycle_links(conn)
            # Distinguish empty model output from a provider failure. Runs
            # once; later rows already carry the correct explicit outcome.
            self._migrate_run_status(conn)
            # Every read path filters or orders by these columns; without the
            # indexes each dashboard poll scans the tables.
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_probe_runs_timestamp ON probe_runs (timestamp);
                CREATE INDEX IF NOT EXISTS idx_probe_runs_status ON probe_runs (run_status);
                CREATE INDEX IF NOT EXISTS idx_probe_runs_cycle ON probe_runs (cycle_id);
                CREATE INDEX IF NOT EXISTS idx_response_features_run ON response_features (run_id);
                CREATE INDEX IF NOT EXISTS idx_embeddings_run ON embeddings (run_id);
                CREATE INDEX IF NOT EXISTS idx_test_results_timestamp ON test_results (timestamp);
                CREATE INDEX IF NOT EXISTS idx_test_results_cycle ON test_results (cycle_id);
                CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts (timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_cycle ON alerts (cycle_id);
            """)
            conn.commit()

    def _migrate_run_status(self, conn) -> None:
        """Relabel legacy empty responses once, without touching other data.

        A historical row with status 'failed', empty text and validity 0 was
        produced by the old `run_status = 'success' if is_valid else 'failed'`
        rule: it is an empty model output, not a transport failure. The
        migration is marker-guarded so a later, genuine 'failed' row is never
        rewritten on a subsequent start.
        """
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
        """)
        if conn.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (_EMPTY_STATUS_MIGRATION,)).fetchone():
            return
        conn.execute("""
            UPDATE probe_runs SET run_status = ?
            WHERE run_status = ?
              AND (response_text IS NULL OR TRIM(response_text) = '')
              AND (is_valid IS NULL OR is_valid = 0)
        """, (OUTCOME_EMPTY, OUTCOME_FAILED))
        conn.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (_EMPTY_STATUS_MIGRATION, datetime.now(timezone.utc).isoformat()))

    def _backfill_cycle_links(self, conn) -> None:
        """Attach legacy test results and alerts to the cycle they followed.

        Exact ids are stored for every new row. The committed seed and other
        pre-migration databases have no id on their result rows, so the only
        honest reconstruction is the latest complete cycle that finished
        before the result was written. Each cycle is used at most once.
        """
        try:
            cycles = [dict(row) for row in conn.execute(
                "SELECT cycle_id, MIN(id) AS first_id, MAX(timestamp) AS ended "
                "FROM probe_runs WHERE cycle_id IS NOT NULL AND cycle_id <> '' "
                "GROUP BY cycle_id ORDER BY MIN(id) ASC").fetchall()]
        except Exception:
            return
        if not cycles:
            return
        try:
            used = {row[0] for row in conn.execute(
                "SELECT DISTINCT cycle_id FROM test_results "
                "WHERE cycle_id IS NOT NULL AND cycle_id <> ''").fetchall()}
        except Exception:
            used = set()
        try:
            pending = [dict(row) for row in conn.execute(
                "SELECT id, timestamp FROM test_results "
                "WHERE cycle_id IS NULL OR cycle_id = '' ORDER BY id ASC").fetchall()]
        except Exception:
            pending = []
        for row in pending:
            timestamp = row.get("timestamp") or ""
            candidate = None
            for cycle in cycles:
                if cycle["cycle_id"] in used:
                    continue
                if (cycle.get("ended") or "") <= timestamp:
                    candidate = cycle["cycle_id"]
                else:
                    break
            if candidate:
                conn.execute(
                    "UPDATE test_results SET cycle_id = ? WHERE id = ?",
                    (candidate, row["id"]))
                used.add(candidate)

        # Alerts written before the link existed follow the same rule.
        try:
            used_alerts = {row[0] for row in conn.execute(
                "SELECT DISTINCT cycle_id FROM alerts "
                "WHERE cycle_id IS NOT NULL AND cycle_id <> ''").fetchall()}
            pending_alerts = [dict(row) for row in conn.execute(
                "SELECT id, timestamp FROM alerts "
                "WHERE cycle_id IS NULL OR cycle_id = '' ORDER BY id ASC").fetchall()]
        except Exception:
            pending_alerts = []
        for row in pending_alerts:
            timestamp = row.get("timestamp") or ""
            candidate = None
            for cycle in cycles:
                if cycle["cycle_id"] in used_alerts:
                    continue
                if (cycle.get("ended") or "") <= timestamp:
                    candidate = cycle["cycle_id"]
                else:
                    break
            if candidate:
                conn.execute(
                    "UPDATE alerts SET cycle_id = ? WHERE id = ?",
                    (candidate, row["id"]))
                used_alerts.add(candidate)

    @contextmanager
    def _db(self):
        """Provide a connection that commits on success / rolls back on error."""
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            # WAL lets dashboard reads proceed while a run cycle is writing.
            # journal_mode is a persistent database property, so it is set
            # once in _init_db; setting it per connection would take a write
            # lock on every query for no effect.
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- writes ----------

    def insert_probe_run(self,
                         timestamp: str,
                         prompt_version: str,
                         prompt_text: str,
                         model_id: str,
                         response_text: str,
                         response_length: int,
                         is_valid: bool,
                         run_status: str,
                         adapter_type: str,
                         prompt_id: Optional[str] = None,
                         category: Optional[str] = None,
                         cycle_id: Optional[str] = None) -> int:
        """Insert a probe run and return its ID."""
        with self._db() as conn:
            cursor = conn.execute("""
                INSERT INTO probe_runs (
                    timestamp, prompt_version, prompt_text, model_id,
                    response_text, response_length, is_valid, run_status, adapter_type,
                    prompt_id, category, cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp, prompt_version, prompt_text, model_id,
                response_text, response_length, 1 if is_valid else 0, run_status, adapter_type,
                prompt_id, category, cycle_id
            ))
            run_id = cursor.lastrowid
            conn.commit()
            return run_id

    def insert_response_features(self, run_id: int, response_length: int, is_valid: bool,
                                 is_refusal: Optional[int] = None,
                                 repetition_ratio: Optional[float] = None,
                                 token_count: Optional[int] = None,
                                 has_code_block: Optional[int] = None):
        """Insert response features for a run."""
        with self._db() as conn:
            conn.execute("""
                INSERT INTO response_features (run_id, response_length, is_valid,
                    is_refusal, repetition_ratio, token_count, has_code_block)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (run_id, response_length, 1 if is_valid else 0,
                  is_refusal, repetition_ratio, token_count, has_code_block))
            conn.commit()

    def insert_embedding(self, run_id: int, embedding: List[float]):
        """Insert embedding for a run."""
        # Convert numpy floats to Python floats for JSON serialization
        embedding = [float(x) for x in embedding]
        with self._db() as conn:
            conn.execute("""
                INSERT INTO embeddings (run_id, embedding_blob)
                VALUES (?, ?)
            """, (run_id, json.dumps(embedding)))
            conn.commit()

    def insert_manifest(self, items: Dict[str, str]) -> None:
        """Write or update the baseline manifest for this database."""
        with self._db() as conn:
            for key, value in items.items():
                conn.execute(
                    "INSERT INTO baseline_manifest (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, str(value)))
            conn.commit()

    def get_manifest(self) -> Dict[str, str]:
        """The manifest describing this database's reference window."""
        with self._db() as conn:
            cursor = conn.execute(
                "SELECT key, value FROM baseline_manifest ORDER BY key ASC")
            return {r[0]: r[1] for r in cursor.fetchall()}

    def insert_test_results(self, results: Dict[str, Any]):
        """Insert test results."""
        with self._db() as conn:
            conn.execute("""
                INSERT INTO test_results (
                    timestamp, reference_size, current_size,
                    ks_statistic, ks_pvalue, ks_raw_pvalue, ks_adjusted_pvalue, ks_significant,
                    proportion_z_stat, proportion_z_pvalue, proportion_z_raw_pvalue,
                    proportion_z_adjusted_pvalue, proportion_z_significant,
                    mmd_statistic, mmd_pvalue, mmd_raw_pvalue, mmd_adjusted_pvalue, mmd_significant,
                    length_shift_pct, centroid_distance,
                    refusal_rate_reference, refusal_rate_current,
                    unique_reference, unique_current,
                    paired_cluster_mmd_statistic, paired_cluster_mmd_pvalue,
                    paired_cluster_prompt_pairs,
                    provider_latency_mean_seconds, provider_latency_p50_seconds,
                    provider_latency_p95_seconds, probe_tokens_total,
                    paired_cluster_method, mmd_permutation_unit,
                    bh_significant_tests, overall_drift_detected, overall_quality_degradation,
                    cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                results['timestamp'], results['reference_size'], results['current_size'],
                results.get('ks_statistic'), results.get('ks_pvalue'), results.get('ks_raw_pvalue'),
                results.get('ks_adjusted_pvalue'), results.get('ks_significant', 0),
                results.get('proportion_z_stat'), results.get('proportion_z_pvalue'),
                results.get('proportion_z_raw_pvalue'), results.get('proportion_z_adjusted_pvalue'),
                results.get('proportion_z_significant', 0),
                results.get('mmd_statistic'), results.get('mmd_pvalue'), results.get('mmd_raw_pvalue'),
                results.get('mmd_adjusted_pvalue'), results.get('mmd_significant', 0),
                results.get('length_shift_pct'), results.get('centroid_distance'),
                results.get('ref_refusal_rate'), results.get('cur_refusal_rate'),
                results.get('reference_unique_responses'), results.get('current_unique_responses'),
                results.get('paired_cluster_mmd_statistic'), results.get('paired_cluster_mmd_pvalue'),
                results.get('paired_cluster_prompt_pairs'),
                results.get('provider_latency_mean_seconds'),
                results.get('provider_latency_p50_seconds'),
                results.get('provider_latency_p95_seconds'),
                results.get('probe_tokens_total'),
                results.get('paired_cluster_method'),
                results.get('mmd_permutation_unit'),
                json.dumps(results.get('bh_significant_tests', [])),
                results.get('overall_drift_detected', 0),
                results.get('overall_quality_degradation', 0),
                results.get('cycle_id')
            ))
            conn.commit()

    def insert_alert(self, alert: Dict[str, Any]):
        """Insert an alert."""
        with self._db() as conn:
            conn.execute("""
                INSERT INTO alerts (
                    timestamp, alert_type, severity, message,
                    ks_pvalue, proportion_z_pvalue, mmd_pvalue,
                    overall_drift_detected, overall_quality_degradation, cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert['timestamp'], alert['alert_type'], alert['severity'], alert['message'],
                alert.get('ks_pvalue'), alert.get('proportion_z_pvalue'), alert.get('mmd_pvalue'),
                alert.get('overall_drift_detected', 0), alert.get('overall_quality_degradation', 0),
                alert.get('cycle_id')
            ))
            conn.commit()

    # ---------- reads ----------

    def get_all_runs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all probe runs."""
        with self._db() as conn:
            query = "SELECT * FROM probe_runs ORDER BY id DESC"
            if limit:
                query += f" LIMIT {int(limit)}"
            cursor = conn.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def get_all_runs_ordered(self) -> List[Dict[str, Any]]:
        """Get all probe runs in chronological order (oldest first).

        Ordered by insertion id rather than timestamp: wall clock steps (NTP
        corrections) can move recorded timestamps backwards, and window
        selection must follow true insert order.
        """
        with self._db() as conn:
            cur = conn.execute("SELECT * FROM probe_runs ORDER BY id ASC")
            return [dict(r) for r in cur.fetchall()]

    def get_response_lengths_for_runs(self, run_ids: List[int]) -> List[float]:
        """Get response lengths for given run IDs."""
        if not run_ids:
            return []
        with self._db() as conn:
            placeholders = ','.join('?' for _ in run_ids)
            cursor = conn.execute(f"""
                SELECT response_length FROM probe_runs
                WHERE id IN ({placeholders}) AND run_status = 'success'
            """, run_ids)
            return [row[0] for row in cursor.fetchall() if row[0] is not None]

    def get_validity_for_runs(self, run_ids: List[int], include_failed: bool = True) -> List[int]:
        """Get validity flags (1=valid, 0=invalid) for given run IDs."""
        if not run_ids:
            return []
        with self._db() as conn:
            placeholders = ','.join('?' for _ in run_ids)
            if include_failed:
                cursor = conn.execute(f"""
                    SELECT is_valid FROM probe_runs
                    WHERE id IN ({placeholders})
                """, run_ids)
            else:
                cursor = conn.execute(f"""
                    SELECT is_valid FROM probe_runs
                    WHERE id IN ({placeholders}) AND run_status = 'success'
                """, run_ids)
            return [row[0] for row in cursor.fetchall()]

    def get_embeddings_for_runs(self, run_ids: List[int]) -> List[List[float]]:
        """Get embeddings for given run IDs."""
        if not run_ids:
            return []
        with self._db() as conn:
            placeholders = ','.join('?' for _ in run_ids)
            cursor = conn.execute(f"""
                SELECT e.embedding_blob
                FROM embeddings e
                JOIN probe_runs pr ON e.run_id = pr.id
                WHERE pr.id IN ({placeholders}) AND pr.run_status = 'success'
            """, run_ids)
            embeddings = []
            for row in cursor.fetchall():
                try:
                    emb = json.loads(row['embedding_blob'])
                    embeddings.append(emb)
                except Exception:
                    continue
            return embeddings

    def get_embeddings_with_run_ids_for_runs(
            self, run_ids: List[int]) -> List[Tuple[int, List[float]]]:
        """Embeddings paired with their run ids, in run id order.

        The run level pairing exists for prompt level analysis: cluster labels
        such as prompt ids must attach to the exact runs a window selected,
        not to whatever order the database happens to return rows in. Rows
        whose embedding cannot be decoded are skipped, mirroring
        get_embeddings_for_runs, but the run id travels with the vector so
        the caller can align labels with certainty.
        """
        if not run_ids:
            return []
        with self._db() as conn:
            placeholders = ','.join('?' for _ in run_ids)
            cursor = conn.execute(f"""
                SELECT e.run_id, e.embedding_blob
                FROM embeddings e
                JOIN probe_runs pr ON e.run_id = pr.id
                WHERE pr.id IN ({placeholders}) AND pr.run_status = 'success'
                ORDER BY pr.id ASC
            """, run_ids)
            paired = []
            for row in cursor.fetchall():
                try:
                    emb = json.loads(row['embedding_blob'])
                    paired.append((int(row['run_id']), emb))
                except Exception:
                    continue
            return paired

    def get_all_test_results(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all test results."""
        with self._db() as conn:
            query = "SELECT * FROM test_results ORDER BY id DESC"
            if limit:
                query += f" LIMIT {int(limit)}"
            cursor = conn.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def get_recent_drift_flags(self, limit: int) -> List[int]:
        """Oldest-first drift flags for the most recent stored results.

        Callers include the just-inserted current result in `limit`, so the
        list ends with the current cycle and feeds the alert persistence rule.
        """
        with self._db() as conn:
            rows = conn.execute(
                "SELECT overall_drift_detected FROM test_results "
                "ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [int(r[0]) for r in reversed(rows)]

    def get_category_embeddings(self, run_ids: List[int]) -> Dict[str, List[List[float]]]:
        """Embeddings grouped by prompt category, for per category MMD tests."""
        if not run_ids:
            return {}
        with self._db() as conn:
            placeholders = ','.join('?' for _ in run_ids)
            rows = conn.execute(f"""
                SELECT pr.category, e.embedding_blob
                FROM embeddings e
                JOIN probe_runs pr ON e.run_id = pr.id
                WHERE pr.id IN ({placeholders}) AND pr.run_status = 'success'
                  AND pr.category IS NOT NULL
            """, run_ids).fetchall()
        grouped: Dict[str, List[List[float]]] = {}
        for category, blob in rows:
            try:
                grouped.setdefault(category, []).append(json.loads(blob))
            except Exception:
                continue
        return grouped

    def get_features_for_runs(self, run_ids: List[int]) -> List[Dict[str, Any]]:
        """Behavioral feature rows for the given runs."""
        if not run_ids:
            return []
        with self._db() as conn:
            placeholders = ','.join('?' for _ in run_ids)
            cursor = conn.execute(f"""
                SELECT is_refusal, repetition_ratio, token_count, has_code_block
                FROM response_features
                WHERE run_id IN ({placeholders})
            """, run_ids)
            return [dict(r) for r in cursor.fetchall()]

    def get_refusal_with_run_ids_for_runs(
            self, run_ids: List[int]) -> List[Tuple[int, int]]:
        """Refusal flags paired with their run ids, in run id order.

        Window display needs the refusal flag attached to the exact run the
        window selected, not to whatever order the database returns rows in.
        Runs without a stored feature row are simply absent from the result.
        """
        if not run_ids:
            return []
        with self._db() as conn:
            placeholders = ','.join('?' for _ in run_ids)
            cursor = conn.execute(f"""
                SELECT rf.run_id, rf.is_refusal
                FROM response_features rf
                WHERE rf.run_id IN ({placeholders})
                ORDER BY rf.run_id ASC
            """, run_ids)
            return [(int(r[0]), int(r[1] or 0))
                    for r in cursor.fetchall()]

    def get_all_alerts(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all alerts."""
        with self._db() as conn:
            query = "SELECT * FROM alerts ORDER BY id DESC"
            if limit:
                query += f" LIMIT {int(limit)}"
            cursor = conn.execute(query)
            return [dict(row) for row in cursor.fetchall()]

    def run_ids_from_runs(self, runs: List[Dict[str, Any]]) -> List[int]:
        """Extract run IDs from run dicts."""
        return [r['id'] for r in runs if 'id' in r]

    # ---------- cycle aggregation and diagnostics ----------

    def _reference_cycle_ids(self, conn, reference_window: Optional[int]) -> set:
        """Cycles whose rows make up the configured reference window.

        Derived from the exact stored cycles, oldest first: walk the cycles
        until their cumulative probe count reaches the configured reference
        window. Nothing is keyed to a cycle number or a timestamp guess, so a
        different batch size or window size still identifies the right set.
        """
        if not reference_window:
            return set()
        ids: set = set()
        cumulative = 0
        rows = conn.execute("""
            SELECT cycle_id, COUNT(*) AS probe_count
            FROM probe_runs
            WHERE cycle_id IS NOT NULL AND cycle_id <> ''
            GROUP BY cycle_id ORDER BY MIN(id) ASC
        """).fetchall()
        for row in rows:
            if cumulative >= int(reference_window):
                break
            ids.add(row[0])
            cumulative += int(row[1] or 0)
        return ids

    def get_cycle_summaries(self, limit: int = 12,
                            expected_probes: Optional[int] = None,
                            reference_window: Optional[int] = None) -> List[Dict[str, Any]]:
        """Group probe records by cycle and attach the exact detector result.

        The join is on ``cycle_id``, never on timestamps. A cycle with no
        result is still a real monitoring cycle and stays in the list. Cycles
        that supplied the reference window are flagged from stored records,
        so they can be shown as baseline collection rather than as pending
        evaluations.
        """
        with self._db() as conn:
            rows = conn.execute("""
                SELECT c.*,
                       ROW_NUMBER() OVER (ORDER BY c.first_id ASC) AS cycle_number
                FROM (
                    SELECT cycle_id,
                           MIN(timestamp) AS started_at,
                           MAX(timestamp) AS ended_at,
                           COUNT(*) AS probe_count,
                           SUM(CASE WHEN run_status = 'success' THEN 1 ELSE 0 END)
                               AS success_count,
                           SUM(CASE WHEN run_status = 'empty' THEN 1 ELSE 0 END)
                               AS empty_count,
                           SUM(CASE WHEN run_status = 'failed' THEN 1 ELSE 0 END)
                               AS failed_count,
                           MIN(id) AS first_id,
                           MAX(id) AS last_id
                    FROM probe_runs
                    WHERE cycle_id IS NOT NULL AND cycle_id <> ''
                    GROUP BY cycle_id
                ) c
                ORDER BY c.first_id DESC
                LIMIT ?
            """, (int(limit),)).fetchall()
            summaries = [dict(row) for row in rows]
            baseline_ids = self._reference_cycle_ids(conn, reference_window)
            cycle_ids = [s["cycle_id"] for s in summaries]
            results: Dict[str, Dict[str, Any]] = {}
            if cycle_ids:
                placeholders = ','.join('?' for _ in cycle_ids)
                for row in conn.execute(
                        f"SELECT * FROM test_results WHERE cycle_id IN ({placeholders}) "
                        "ORDER BY id ASC", cycle_ids).fetchall():
                    record = dict(row)
                    results.setdefault(record.get("cycle_id"), record)
            for summary in summaries:
                summary["result"] = results.get(summary["cycle_id"])
                summary["timestamp"] = summary.get("ended_at") or summary.get("started_at")
                summary["reference_baseline"] = summary["cycle_id"] in baseline_ids
                summary["expected_probes"] = expected_probes
            return summaries

    def get_cycle_ids(self) -> set:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT DISTINCT cycle_id FROM probe_runs "
                "WHERE cycle_id IS NOT NULL AND cycle_id <> ''").fetchall()
            return {row[0] for row in rows}

    def get_diagnostics(self, expected_probes: Optional[int] = None,
                        reference_window: Optional[int] = None) -> Dict[str, Any]:
        """Counts that expose missing, orphaned or unevaluated records.

        Awaiting-evaluation counts exclude cycles that supplied the reference
        window: those are baseline collection, not pending detector work.
        """
        with self._db() as conn:
            def scalar(sql, params=()):
                row = conn.execute(sql, params).fetchone()
                return int(row[0]) if row is not None and row[0] is not None else 0

            probe_runs = scalar("SELECT COUNT(*) FROM probe_runs")
            successful = scalar(
                "SELECT COUNT(*) FROM probe_runs WHERE run_status = 'success'")
            empty = scalar(
                "SELECT COUNT(*) FROM probe_runs WHERE run_status = 'empty'")
            failed = scalar(
                "SELECT COUNT(*) FROM probe_runs WHERE run_status = 'failed'")
            without_cycle = scalar(
                "SELECT COUNT(*) FROM probe_runs "
                "WHERE cycle_id IS NULL OR cycle_id = ''")
            cycles = scalar(
                "SELECT COUNT(DISTINCT cycle_id) FROM probe_runs "
                "WHERE cycle_id IS NOT NULL AND cycle_id <> ''")
            test_results = scalar("SELECT COUNT(*) FROM test_results")
            results_without_cycle = scalar(
                "SELECT COUNT(*) FROM test_results "
                "WHERE cycle_id IS NULL OR cycle_id = ''")
            evaluated_ids = {row[0] for row in conn.execute(
                "SELECT DISTINCT cycle_id FROM test_results "
                "WHERE cycle_id IS NOT NULL AND cycle_id <> ''").fetchall()}
            evaluated_cycles = len(evaluated_ids)
            alerts = scalar("SELECT COUNT(*) FROM alerts")
            failed_cycles = scalar("""
                SELECT COUNT(*) FROM (
                    SELECT cycle_id FROM probe_runs
                    WHERE cycle_id IS NOT NULL AND cycle_id <> ''
                    GROUP BY cycle_id
                    HAVING SUM(CASE WHEN run_status = 'failed' THEN 1 ELSE 0 END) > 0
                ) c
            """)
            baseline_ids = self._reference_cycle_ids(conn, reference_window)
            complete_cycles = None
            awaiting_evaluation = None
            if expected_probes:
                complete_rows = conn.execute("""
                    SELECT cycle_id FROM probe_runs
                    WHERE cycle_id IS NOT NULL AND cycle_id <> ''
                    GROUP BY cycle_id HAVING COUNT(*) = ?
                """, (int(expected_probes),)).fetchall()
                complete_cycles = len(complete_rows)
                awaiting_evaluation = sum(
                    1 for row in complete_rows
                    if row[0] not in evaluated_ids and row[0] not in baseline_ids)
            return {
                "probe_runs": probe_runs,
                "successful_probe_runs": successful,
                "empty_probe_runs": empty,
                "failed_probe_runs": failed,
                "probe_runs_without_cycle": without_cycle,
                "cycles": cycles,
                "failed_cycles": failed_cycles,
                "reference_baseline_cycles": len(baseline_ids),
                "complete_cycles": complete_cycles,
                "evaluated_cycles": evaluated_cycles,
                "awaiting_evaluation_cycles": awaiting_evaluation,
                "test_results": test_results,
                "test_results_without_cycle": results_without_cycle,
                "alerts": alerts,
                "expected_probes_per_cycle": expected_probes,
                "reference_window": reference_window,
            }

    def info(self) -> Dict[str, Any]:
        """Non-secret storage identity for startup logs and /config."""
        return {
            "backend": self.backend,
            "location": self.location,
            "durable": self.is_durable,
            "storage_error": self.storage_error,
        }

    def log_startup_status(self, expected_probes: Optional[int] = None,
                           reference_window: Optional[int] = None) -> None:
        """Log the resolved database identity and row counts, never secrets."""
        try:
            payload = {"event": "storage_ready", **self.info(),
                       "counts": self.get_diagnostics(expected_probes, reference_window)}
            logger.info(json.dumps(payload, default=str))
        except Exception as exc:  # a log line must never stop the service
            logger.warning("storage startup status unavailable: %s", exc)

    # ---------- counts ----------

    def count_successful_runs(self) -> int:
        """Count total successful runs."""
        with self._db() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM probe_runs WHERE run_status = 'success'")
            return cursor.fetchone()[0]

    def count_all_runs(self) -> int:
        """Count every probe run (successful or not)."""
        with self._db() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM probe_runs")
            return cursor.fetchone()[0]

    def count_test_results(self) -> int:
        """Count saved drift-test cycles."""
        with self._db() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM test_results")
            return cursor.fetchone()[0]

    def count_alerts(self) -> int:
        """Count saved alerts."""
        with self._db() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM alerts")
            return cursor.fetchone()[0]

    def clear_all(self):
        """Clear all data (for testing)."""
        with self._db() as conn:
            conn.execute("DELETE FROM embeddings")
            conn.execute("DELETE FROM response_features")
            conn.execute("DELETE FROM test_results")
            conn.execute("DELETE FROM alerts")
            conn.execute("DELETE FROM probe_runs")
            conn.commit()


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS baseline_manifest (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_runs (
    id BIGSERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    model_id TEXT NOT NULL,
    response_text TEXT NOT NULL,
    response_length INTEGER,
    is_valid INTEGER,
    run_status TEXT NOT NULL,
    adapter_type TEXT NOT NULL,
    prompt_id TEXT,
    category TEXT,
    cycle_id TEXT
);

CREATE TABLE IF NOT EXISTS response_features (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES probe_runs (id),
    response_length INTEGER,
    is_valid INTEGER,
    is_refusal INTEGER,
    repetition_ratio DOUBLE PRECISION,
    token_count INTEGER,
    has_code_block INTEGER
);

CREATE TABLE IF NOT EXISTS embeddings (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES probe_runs (id),
    embedding_blob TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_results (
    id BIGSERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    cycle_id TEXT,
    reference_size INTEGER,
    current_size INTEGER,
    ks_statistic DOUBLE PRECISION,
    ks_pvalue DOUBLE PRECISION,
    ks_raw_pvalue DOUBLE PRECISION,
    ks_adjusted_pvalue DOUBLE PRECISION,
    ks_significant INTEGER,
    proportion_z_stat DOUBLE PRECISION,
    proportion_z_pvalue DOUBLE PRECISION,
    proportion_z_raw_pvalue DOUBLE PRECISION,
    proportion_z_adjusted_pvalue DOUBLE PRECISION,
    proportion_z_significant INTEGER,
    mmd_statistic DOUBLE PRECISION,
    mmd_pvalue DOUBLE PRECISION,
    mmd_raw_pvalue DOUBLE PRECISION,
    mmd_adjusted_pvalue DOUBLE PRECISION,
    mmd_significant INTEGER,
    length_shift_pct DOUBLE PRECISION,
    centroid_distance DOUBLE PRECISION,
    refusal_rate_reference DOUBLE PRECISION,
    refusal_rate_current DOUBLE PRECISION,
    unique_reference INTEGER,
    unique_current INTEGER,
    paired_cluster_mmd_statistic DOUBLE PRECISION,
    paired_cluster_mmd_pvalue DOUBLE PRECISION,
    paired_cluster_prompt_pairs INTEGER,
    provider_latency_mean_seconds DOUBLE PRECISION,
    provider_latency_p50_seconds DOUBLE PRECISION,
    provider_latency_p95_seconds DOUBLE PRECISION,
    probe_tokens_total INTEGER,
    paired_cluster_method TEXT,
    mmd_permutation_unit TEXT,
    bh_significant_tests TEXT,
    overall_drift_detected INTEGER,
    overall_quality_degradation INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY,
    timestamp TEXT NOT NULL,
    cycle_id TEXT,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    ks_pvalue DOUBLE PRECISION,
    proportion_z_pvalue DOUBLE PRECISION,
    mmd_pvalue DOUBLE PRECISION,
    overall_drift_detected INTEGER,
    overall_quality_degradation INTEGER
);

CREATE INDEX IF NOT EXISTS idx_probe_runs_timestamp ON probe_runs (timestamp);
CREATE INDEX IF NOT EXISTS idx_probe_runs_status ON probe_runs (run_status);
CREATE INDEX IF NOT EXISTS idx_probe_runs_cycle ON probe_runs (cycle_id);
CREATE INDEX IF NOT EXISTS idx_response_features_run ON response_features (run_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_run ON embeddings (run_id);
CREATE INDEX IF NOT EXISTS idx_test_results_timestamp ON test_results (timestamp);
CREATE INDEX IF NOT EXISTS idx_test_results_cycle ON test_results (cycle_id);
CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts (timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_cycle ON alerts (cycle_id);
"""


class PostgresStorage(DriftMonitorStorage):
    """Managed PostgreSQL backend selected by ``DATABASE_URL``.

    The pool is deliberately tiny: one warm connection, two at most. Every
    method reuses the same public surface as the SQLite backend.
    """

    backend = "postgres"
    is_durable = True

    def __init__(self, database_url: str):
        try:
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise RuntimeError(
                "DATABASE_URL is set but psycopg_pool is not installed. "
                "Install psycopg[binary,pool] from requirements.txt."
            ) from exc
        self.db_path = None
        self.resolved_path = None
        self.location = "DATABASE_URL (external PostgreSQL)"
        self.storage_error = None
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=2,
            timeout=10,
            open=True,
            kwargs={"connect_timeout": 8},
        )
        self._init_db()

    @contextmanager
    def _db(self):
        with self._pool.connection() as connection:
            yield _PgConnection(connection)

    def _init_db(self):
        with self._db() as conn:
            conn.executescript(_PG_SCHEMA)
            self._backfill_cycle_links(conn)
            self._migrate_run_status(conn)
            conn.commit()

    def _migrate_columns(self, conn) -> None:
        for column in ("prompt_id TEXT", "category TEXT", "cycle_id TEXT"):
            conn.execute(f"ALTER TABLE probe_runs ADD COLUMN IF NOT EXISTS {column}")

    def _migrate_test_columns(self, conn) -> None:
        for column in ("cycle_id TEXT",
                       "length_shift_pct DOUBLE PRECISION",
                       "centroid_distance DOUBLE PRECISION",
                       "refusal_rate_reference DOUBLE PRECISION",
                       "refusal_rate_current DOUBLE PRECISION",
                       "unique_reference INTEGER", "unique_current INTEGER",
                       "paired_cluster_mmd_statistic DOUBLE PRECISION",
                       "paired_cluster_mmd_pvalue DOUBLE PRECISION",
                       "paired_cluster_prompt_pairs INTEGER",
                       "provider_latency_mean_seconds DOUBLE PRECISION",
                       "provider_latency_p50_seconds DOUBLE PRECISION",
                       "provider_latency_p95_seconds DOUBLE PRECISION",
                       "probe_tokens_total INTEGER",
                       "paired_cluster_method TEXT",
                       "mmd_permutation_unit TEXT"):
            conn.execute(f"ALTER TABLE test_results ADD COLUMN IF NOT EXISTS {column}")

    def _migrate_alert_columns(self, conn) -> None:
        conn.execute("ALTER TABLE alerts ADD COLUMN IF NOT EXISTS cycle_id TEXT")

    def _migrate_feature_columns(self, conn) -> None:
        for column in ("is_refusal INTEGER", "repetition_ratio DOUBLE PRECISION",
                       "token_count INTEGER", "has_code_block INTEGER"):
            conn.execute(
                f"ALTER TABLE response_features ADD COLUMN IF NOT EXISTS {column}")

    def insert_probe_run(self,
                         timestamp: str,
                         prompt_version: str,
                         prompt_text: str,
                         model_id: str,
                         response_text: str,
                         response_length: int,
                         is_valid: bool,
                         run_status: str,
                         adapter_type: str,
                         prompt_id: Optional[str] = None,
                         category: Optional[str] = None,
                         cycle_id: Optional[str] = None) -> int:
        with self._db() as conn:
            cursor = conn.execute("""
                INSERT INTO probe_runs (
                    timestamp, prompt_version, prompt_text, model_id,
                    response_text, response_length, is_valid, run_status, adapter_type,
                    prompt_id, category, cycle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
            """, (
                timestamp, prompt_version, prompt_text, model_id,
                response_text, response_length, 1 if is_valid else 0, run_status,
                adapter_type, prompt_id, category, cycle_id
            ))
            return int(cursor.fetchone()[0])


def create_storage(db_path: str = "data/drift_monitor.db",
                   database_url: Optional[str] = None) -> DriftMonitorStorage:
    """Select durable PostgreSQL when configured, otherwise SQLite.

    A configured-but-unreachable PostgreSQL URL falls back to SQLite and
    records the error, so the dashboard keeps serving stored data while
    clearly reporting that it is ephemeral. It never pretends the fallback
    is durable.
    """
    url = (database_url or "").strip()
    if not url:
        return DriftMonitorStorage(db_path)
    try:
        return PostgresStorage(url)
    except Exception as exc:
        logger.error(
            "DATABASE_URL is set but PostgreSQL is unavailable; falling back "
            "to ephemeral SQLite: %s", exc)
        storage = DriftMonitorStorage(db_path)
        storage.backend = "sqlite-fallback"
        storage.is_durable = False
        storage.storage_error = str(exc)
        return storage


def _source_cycle_links(conn: sqlite3.Connection) -> Dict[int, str]:
    """Reconstruct cycle ids for a legacy source database without mutating it."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(test_results)")}
    has_cycle = "cycle_id" in columns
    select = "SELECT id, timestamp" + (", cycle_id" if has_cycle else "") + \
             " FROM test_results ORDER BY id ASC"
    results = [dict(row) for row in conn.execute(select).fetchall()]
    cycles = [dict(row) for row in conn.execute(
        "SELECT cycle_id, MIN(id) AS first_id, MAX(timestamp) AS ended "
        "FROM probe_runs WHERE cycle_id IS NOT NULL AND cycle_id <> '' "
        "GROUP BY cycle_id ORDER BY MIN(id) ASC").fetchall()]
    used = {r.get("cycle_id") for r in results if r.get("cycle_id")}
    links: Dict[int, str] = {}
    for row in results:
        if row.get("cycle_id"):
            links[row["id"]] = row["cycle_id"]
            continue
        timestamp = row.get("timestamp") or ""
        candidate = None
        for cycle in cycles:
            if cycle["cycle_id"] in used:
                continue
            if (cycle.get("ended") or "") <= timestamp:
                candidate = cycle["cycle_id"]
            else:
                break
        if candidate:
            links[row["id"]] = candidate
            used.add(candidate)
    return links


def import_baseline(target: DriftMonitorStorage, source_path: str) -> Dict[str, Any]:
    """Import a committed SQLite baseline into a target database, idempotently.

    A cycle that already exists in the target is skipped whole, so a redeploy
    can never duplicate baseline probes. The manifest is only written when
    the target has none, and a test result is only inserted when its cycle
    has no result yet. The reference window is never overwritten. Legacy
    empty responses in the source are stored with the explicit 'empty'
    outcome, not mislabelled as provider failures.
    """
    if not os.path.exists(source_path):
        return {"source": source_path, "exists": False, "imported_cycles": 0}
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    try:
        existing_cycles = target.get_cycle_ids()
        cycles = [dict(row) for row in source.execute(
            "SELECT cycle_id, MIN(id) AS first_id, MAX(id) AS last_id "
            "FROM probe_runs WHERE cycle_id IS NOT NULL AND cycle_id <> '' "
            "GROUP BY cycle_id ORDER BY MIN(id) ASC").fetchall()]
        imported_cycles = 0
        imported_runs = 0
        for cycle in cycles:
            if cycle["cycle_id"] in existing_cycles:
                continue
            runs = source.execute(
                "SELECT * FROM probe_runs WHERE cycle_id = ? ORDER BY id ASC",
                (cycle["cycle_id"],)).fetchall()
            for run in runs:
                run = dict(run)
                # The committed seed predates the outcome split. Apply the
                # same evidence-based rule the migration uses, without
                # mutating the source file.
                status = run["run_status"]
                if is_legacy_empty_response(
                        status, run["response_text"], run["is_valid"]):
                    status = OUTCOME_EMPTY
                new_id = target.insert_probe_run(
                    timestamp=run["timestamp"],
                    prompt_version=run["prompt_version"],
                    prompt_text=run["prompt_text"],
                    model_id=run["model_id"],
                    response_text=run["response_text"],
                    response_length=run["response_length"],
                    is_valid=bool(run["is_valid"]),
                    run_status=status,
                    adapter_type=run["adapter_type"],
                    prompt_id=run.get("prompt_id"),
                    category=run.get("category"),
                    cycle_id=run.get("cycle_id"),
                )
                feature = source.execute(
                    "SELECT * FROM response_features WHERE run_id = ? "
                    "ORDER BY id ASC LIMIT 1", (run["id"],)).fetchone()
                if feature:
                    feature = dict(feature)
                    target.insert_response_features(
                        new_id,
                        feature.get("response_length"),
                        bool(feature.get("is_valid")),
                        is_refusal=feature.get("is_refusal"),
                        repetition_ratio=feature.get("repetition_ratio"),
                        token_count=feature.get("token_count"),
                        has_code_block=feature.get("has_code_block"),
                    )
                embedding = source.execute(
                    "SELECT embedding_blob FROM embeddings WHERE run_id = ? "
                    "ORDER BY id ASC LIMIT 1", (run["id"],)).fetchone()
                if embedding:
                    try:
                        target.insert_embedding(
                            new_id, json.loads(embedding["embedding_blob"]))
                    except Exception:
                        pass
                imported_runs += 1
            imported_cycles += 1

        # Test results: exact cycle ids where the source has them, otherwise
        # the one-time legacy reconstruction. Never overwrite an existing row.
        links = _source_cycle_links(source)
        with target._db() as conn:
            existing_result_cycles = {
                row[0] for row in conn.execute(
                    "SELECT DISTINCT cycle_id FROM test_results "
                    "WHERE cycle_id IS NOT NULL AND cycle_id <> ''").fetchall()}
        imported_results = 0
        for row in source.execute("SELECT * FROM test_results ORDER BY id ASC").fetchall():
            record = dict(row)
            cycle_id = record.get("cycle_id") or links.get(record["id"])
            if not cycle_id or cycle_id in existing_result_cycles:
                continue
            record["cycle_id"] = cycle_id
            flagged = record.get("bh_significant_tests")
            if isinstance(flagged, str):
                try:
                    record["bh_significant_tests"] = json.loads(flagged)
                except Exception:
                    record["bh_significant_tests"] = []
            record.setdefault("bh_significant_tests", [])
            target.insert_test_results(record)
            existing_result_cycles.add(cycle_id)
            imported_results += 1

        # The manifest is written only when the target has none: an existing
        # reference window must never be redefined by a redeploy import.
        try:
            manifest = {row[0]: row[1] for row in source.execute(
                "SELECT key, value FROM baseline_manifest").fetchall()}
        except sqlite3.Error:
            manifest = {}
        if manifest and not target.get_manifest():
            target.insert_manifest(manifest)

        return {
            "source": source_path,
            "exists": True,
            "imported_cycles": imported_cycles,
            "imported_runs": imported_runs,
            "imported_test_results": imported_results,
            "skipped_cycles": len(cycles) - imported_cycles,
        }
    finally:
        source.close()
