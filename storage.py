import sqlite3
import os
from typing import List, Dict, Any, Optional
import json
from contextlib import contextmanager

class DriftMonitorStorage:
    """SQLite storage for LLM drift monitoring data."""
    
    def __init__(self, db_path: str = "data/drift_monitor.db"):
        self.db_path = db_path
        # Ensure data directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """Initialize database schema."""
        with self._db() as conn:
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS response_features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    response_length INTEGER,
                    is_valid INTEGER,
                    FOREIGN KEY (run_id) REFERENCES probe_runs (id)
                )
            """)
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
                    bh_significant_tests TEXT,  -- JSON list
                    overall_drift_detected INTEGER,
                    overall_quality_degradation INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
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
            conn.commit()
    
    def insert_probe_run(self, 
                        timestamp: str,
                        prompt_version: str,
                        prompt_text: str,
                        model_id: str,
                        response_text: str,
                        response_length: int,
                        is_valid: bool,
                        run_status: str,
                        adapter_type: str) -> int:
        """Insert a probe run and return its ID."""
        with self._db() as conn:
            cursor = conn.execute("""
                INSERT INTO probe_runs (
                    timestamp, prompt_version, prompt_text, model_id,
                    response_text, response_length, is_valid, run_status, adapter_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp, prompt_version, prompt_text, model_id,
                response_text, response_length, 1 if is_valid else 0, run_status, adapter_type
            ))
            run_id = cursor.lastrowid
            conn.commit()
            return run_id
    
    def insert_response_features(self, run_id: int, response_length: int, is_valid: bool):
        """Insert response features for a run."""
        with self._db() as conn:
            conn.execute("""
                INSERT INTO response_features (run_id, response_length, is_valid)
                VALUES (?, ?, ?)
            """, (run_id, response_length, 1 if is_valid else 0))
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
                    bh_significant_tests, overall_drift_detected, overall_quality_degradation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                results['timestamp'], results['reference_size'], results['current_size'],
                results.get('ks_statistic'), results.get('ks_pvalue'), results.get('ks_raw_pvalue'),
                results.get('ks_adjusted_pvalue'), results.get('ks_significant', 0),
                results.get('proportion_z_stat'), results.get('proportion_z_pvalue'), 
                results.get('proportion_z_raw_pvalue'), results.get('proportion_z_adjusted_pvalue'),
                results.get('proportion_z_significant', 0),
                results.get('mmd_statistic'), results.get('mmd_pvalue'), results.get('mmd_raw_pvalue'),
                results.get('mmd_adjusted_pvalue'), results.get('mmd_significant', 0),
                json.dumps(results.get('bh_significant_tests', [])), 
                results.get('overall_drift_detected', 0), 
                results.get('overall_quality_degradation', 0)
            ))
            conn.commit()
    
    def insert_alert(self, alert: Dict[str, Any]):
        """Insert an alert."""
        with self._db() as conn:
            conn.execute("""
                INSERT INTO alerts (
                    timestamp, alert_type, severity, message,
                    ks_pvalue, proportion_z_pvalue, mmd_pvalue,
                    overall_drift_detected, overall_quality_degradation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert['timestamp'], alert['alert_type'], alert['severity'], alert['message'],
                alert.get('ks_pvalue'), alert.get('proportion_z_pvalue'), alert.get('mmd_pvalue'),
                alert.get('overall_drift_detected', 0), alert.get('overall_quality_degradation', 0)
            ))
            conn.commit()
    
    def get_all_runs(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all probe runs."""
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM probe_runs ORDER BY timestamp DESC"
            if limit:
                query += f" LIMIT {limit}"
            cursor = conn.execute(query)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_all_runs_ordered(self) -> List[Dict[str, Any]]:
        """Get all probe runs in chronological order (oldest first)."""
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM probe_runs ORDER BY timestamp ASC, id ASC")
            return [dict(r) for r in cur.fetchall()]
    
    @contextmanager
    def _db(self):
        """Provide a sqlite connection that commits on success / rolls back on error."""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    
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
            conn.row_factory = sqlite3.Row
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
    
    def get_all_test_results(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all test results."""
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM test_results ORDER BY timestamp DESC"
            if limit:
                query += f" LIMIT {limit}"
            cursor = conn.execute(query)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_all_alerts(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all alerts."""
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            query = "SELECT * FROM alerts ORDER BY timestamp DESC"
            if limit:
                query += f" LIMIT {limit}"
            cursor = conn.execute(query)
            return [dict(row) for row in cursor.fetchall()]
    
    def run_ids_from_runs(self, runs: List[Dict[str, Any]]) -> List[int]:
        """Extract run IDs from run dicts."""
        return [r['id'] for r in runs if 'id' in r]
    
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

    def count_all_runs(self) -> int:
        """Count total probe runs."""
        with self._db() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM probe_runs")
            return cursor.fetchone()[0]

    def count_test_results(self) -> int:
        """Count total test results."""
        with self._db() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM test_results")
            return cursor.fetchone()[0]

    def count_alerts(self) -> int:
        """Count total alerts."""
        with self._db() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM alerts")
            return cursor.fetchone()[0]
