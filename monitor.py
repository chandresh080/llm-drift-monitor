import json
import yaml
import os
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import random
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from storage import DriftMonitorStorage
from embeddings import EmbeddingExtractor
from drift_tests import ks_test, two_proportion_z_test, mmd_test, benjamini_hochberg_correction


def _post_json_with_retry(
    url: str,
    payload: Dict[str, Any],
    timeout: float,
    max_retries: int = 3,
    base_delay: float = 1.0,
    label: str = "OpenAI-compatible endpoint",
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """POST a JSON payload, retrying transient failures (HTTP 429/5xx, network error, timeout).

    Uses exponential backoff with jitter between attempts.
    """
    data = json.dumps(payload).encode("utf-8")
    backoff = float(base_delay)
    last_error: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            request = Request(
                url,
                data=data,
                headers=headers or {"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            status = exc.code
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"{label} returned HTTP {status}: {detail}")
            if attempt < max_retries and (status == 429 or status >= 500):
                time.sleep(backoff * (0.5 + random.random()))
                backoff *= 2.0
                continue
            raise last_error from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = RuntimeError(f"Cannot reach {label} at {url}. Check the endpoint and network.")
            if attempt < max_retries:
                time.sleep(backoff * (0.5 + random.random()))
                backoff *= 2.0
                continue
            raise last_error from exc
    raise last_error


class FakeLLMAdapter:
    """Fake LLM adapter for testing. Completely offline, no paid APIs."""
    
    def __init__(self, model_id: str = "fake-llm-v1", seed: int = 42):
        self.model_id = model_id
        self.seed = seed
        self.rng = random.Random(seed)
    
    def generate(self, prompt: str) -> str:
        """Generate a fake response. Consistent but slightly varied based on prompt."""
        # Create a deterministic but varied response
        base_responses = [
            f"Response to: {prompt[:50]}... This is a generated answer.",
            f"Here's an explanation: {prompt}",
            "Based on the input, the answer involves considering multiple factors.",
            "This is a simulated response for testing purposes.",
        ]
        # Pick response based on deterministic hash (stable across processes)
        import hashlib
        h = hashlib.md5(f"{self.seed}:{prompt}".encode()).hexdigest()
        idx = int(h, 16) % len(base_responses)
        response = base_responses[idx]
        
        # Add slight variation
        if self.rng.random() < 0.1:  # 10% chance of truncation/variation
            response = response[:len(response)//2] + "..."
        
        return response
    
    def generate_batch(self, prompts: List[str]) -> List[str]:
        """Generate responses for multiple prompts."""
        return [self.generate(p) for p in prompts]

class LocalLLMAdapter:
    """Call a local Ollama model over HTTP without a hosted API key."""

    def __init__(self, model_id: str = "llama3.2:1b",
                 base_url: str = "http://localhost:11434",
                 timeout: float = 120.0,
                 temperature: float = 0.0,
                 max_tokens: int = 128,
                 max_retries: int = 3):
        self.model_id = model_id
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.max_retries = int(max_retries)

    def generate(self, prompt: str) -> str:
        """Generate one response using Ollama's non-streaming endpoint."""
        payload = {
            "model": self.model_id,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        result = _post_json_with_retry(
            url=f"{self.base_url}/api/generate",
            payload=payload,
            timeout=self.timeout,
            max_retries=self.max_retries,
            label="Ollama",
            headers={"Content-Type": "application/json"},
        )
        generated = result.get("response")
        if not isinstance(generated, str):
            raise RuntimeError("Ollama response is missing the generated text")
        return generated

    def generate_batch(self, prompts: List[str]) -> List[str]:
        """Generate responses sequentially, preserving prompt order."""
        return [self.generate(prompt) for prompt in prompts]

class OpenAICompatibleAdapter:
    """Call an OpenAI-compatible chat-completions API (e.g. Grok via api.x.ai, Gemini via Google)."""

    def __init__(self, model_id: str = "gpt-3.5-turbo",
                 api_key: Optional[str] = None,
                 base_url: str = "https://api.openai.com/v1",
                 timeout: float = 120.0,
                 temperature: float = 0.0,
                 max_tokens: int = 128,
                 max_retries: int = 3):
        self.model_id = model_id
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or ""
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.max_retries = int(max_retries)
        if not self.api_key:
            raise RuntimeError(
                "OpenAI-compatible API key is required. Set OPENAI_API_KEY (e.g. in .env) "
                "or pass api_key explicitly."
            )

    def generate(self, prompt: str) -> str:
        """Generate one response using an OpenAI-compatible /chat/completions endpoint."""
        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        result = _post_json_with_retry(
            url=f"{self.base_url}/chat/completions",
            payload=payload,
            timeout=self.timeout,
            max_retries=self.max_retries,
            label="OpenAI-compatible endpoint",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        generated = result.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(generated, str):
            raise RuntimeError("OpenAI-compatible response is missing the generated text")
        return generated

    def generate_batch(self, prompts: List[str]) -> List[str]:
        """Generate responses sequentially, preserving prompt order."""
        return [self.generate(prompt) for prompt in prompts]

class LLMDriftMonitor:
    """Main monitoring pipeline."""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        # Load environment variables from a local .env file (see .env.example)
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass  # python-dotenv optional; fall back to process environment only
        self.storage = DriftMonitorStorage(self.config['data']['db_path'])
        
        # Initialize embedding extractor
        emb_cfg = self.config['embeddings']
        self.embedder = EmbeddingExtractor(
            model_name=emb_cfg['model_name'],
            device=emb_cfg['device']
        )
        
        self.random_seed = self.config['monitoring']['random_seed']
        random.seed(self.random_seed)
        
        # Initialize target adapter
        self.adapter = self._create_adapter()
        
        # Load canary prompts
        self.canary_prompts = self._load_canary_prompts()
    
    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from YAML."""
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    
    def _load_canary_prompts(self) -> Dict[str, Any]:
        """Load canary prompts."""
        with open('canary_prompts.json', 'r') as f:
            return json.load(f)
    
    def _create_adapter(self):
        """Create target LLM adapter based on config."""
        target_cfg = self.config['target']
        adapter_type = target_cfg['type']
        
        if adapter_type == 'fake':
            return FakeLLMAdapter(
                model_id=target_cfg.get('model_id', 'fake-llm-v1'),
                seed=self.random_seed
            )
        elif adapter_type in ('local', 'ollama'):
            return LocalLLMAdapter(
                model_id=target_cfg.get('model_id', 'llama3.2:1b'),
                base_url=target_cfg.get('base_url', 'http://localhost:11434'),
                timeout=target_cfg.get('timeout_seconds', 120),
                temperature=target_cfg.get('temperature', 0.0),
                max_tokens=target_cfg.get('max_tokens', 128),
            )
        elif adapter_type == 'openai-compatible':
            if os.environ.get('GEMINI_API_KEY'):
                # Gemini (Google AI) via the OpenAI-compatible endpoint
                return OpenAICompatibleAdapter(
                    model_id=os.environ.get('GEMINI_MODEL') or 'gemini-3.8-flash',
                    api_key=os.environ.get('GEMINI_API_KEY') or os.environ.get('OPENAI_API_KEY') or '',
                    base_url=os.environ.get('GEMINI_BASE_URL') or 'https://generativelanguage.googleapis.com/v1beta/openai/',
                )
            if os.environ.get('GROQ_API_KEY'):
                # Groq via the OpenAI-compatible endpoint
                return OpenAICompatibleAdapter(
                    model_id=os.environ.get('GROQ_MODEL') or 'llama-3.3-70b-versatile',
                    api_key=os.environ.get('GROQ_API_KEY') or os.environ.get('OPENAI_API_KEY') or '',
                    base_url=os.environ.get('GROQ_BASE_URL') or 'https://api.groq.com/openai/v1',
                )
            # Grok (xAI) via the OpenAI-compatible endpoint
            return OpenAICompatibleAdapter(
                model_id=os.environ.get('OPENAI_MODEL') or os.environ.get('XAI_MODEL') or 'grok-4.7',
                api_key=os.environ.get('OPENAI_API_KEY') or os.environ.get('XAI_API_KEY') or '',
                base_url=os.environ.get('OPENAI_BASE_URL') or os.environ.get('XAI_BASE_URL') or 'https://api.x.ai/v1',
            )
        else:
            raise ValueError(f"Unknown adapter type: {adapter_type}")
    
    def run_canary_probes(self, num_prompts: Optional[int] = None) -> List[int]:
        """
        Run canary probes through target adapter.
        Returns list of run IDs created.
        """
        prompts = self.canary_prompts['prompts']
        if num_prompts is not None:
            prompts = prompts[:num_prompts]
        
        prompt_version = self.canary_prompts['version']
        model_id = self.adapter.model_id
        timestamp = datetime.now().isoformat()
        
        run_ids = []
        adapter_type = self.config['target']['type']
        
        # Generate responses
        responses = self.adapter.generate_batch(prompts)
        
        for i, (prompt, response) in enumerate(zip(prompts, responses)):
            # Extract features
            response_length, is_valid = self.embedder.extract_response_features(response)
            
            # Determine run status
            run_status = 'success' if is_valid else 'failed'
            
            # Insert probe run
            # Use proper timestamp with microseconds for uniqueness
            probe_ts = timestamp if i == 0 else (datetime.fromisoformat(timestamp) + timedelta(milliseconds=i)).isoformat()
            run_id = self.storage.insert_probe_run(
                timestamp=probe_ts,
                prompt_version=prompt_version,
                prompt_text=prompt,
                model_id=model_id,
                response_text=response,
                response_length=response_length,
                is_valid=is_valid,
                run_status=run_status,
                adapter_type=adapter_type
            )
            run_ids.append(run_id)
            
            # Insert response features
            self.storage.insert_response_features(run_id, response_length, is_valid)
            
            # Insert embedding
            embedding = self.embedder.extract_embedding(response)
            self.storage.insert_embedding(run_id, embedding)
        
        return run_ids
    
    def create_reference_window(self, min_runs: Optional[int] = None) -> bool:
        """
        Create reference window from existing successful runs.
        Returns True if reference is created successfully.
        """
        if min_runs is None:
            min_runs = self.config['reference']['min_runs']
        
        successful_runs = self.storage.count_successful_runs()
        
        if successful_runs < min_runs:
            return False
        
        # Reference is implicit: the first N successful runs form the baseline
        return True
    
    def has_sufficient_reference(self) -> bool:
        """Check if we have sufficient data for reference."""
        min_runs = self.config['reference']['min_runs']
        successful_runs = self.storage.count_successful_runs()
        return successful_runs >= min_runs

    def _log_mlflow_run(self, results: Dict[str, Any], ref_size: int, cur_size: int,
                        ref_embeddings: List[Any], cur_embeddings: List[Any],
                        cur_lengths: List[float]) -> None:
        """Best-effort MLflow logging of one monitoring cycle (Phase 2). Never raises."""
        try:
            from tracking import drift_run, log_drift_run, get_git_hash
            from drift_tests import compute_median_bandwidth

            bandwidth = 1.0
            if len(ref_embeddings) >= 2 and len(cur_embeddings) >= 2:
                import numpy as np
                all_emb = np.vstack([ref_embeddings, cur_embeddings])
                bandwidth = round(compute_median_bandwidth(all_emb), 4)

            flagged_count = int(
                results.get('ks_significant', 0) +
                results.get('proportion_z_significant', 0) +
                results.get('mmd_significant', 0)
            )

            total_tokens = sum(cur_lengths) / 4.0 if cur_lengths else 0.0

            with drift_run(
                run_name=f"drift_run_{results.get('timestamp', 'unknown')}",
                experiment_name="llm-drift-daily",
                tags={"run_type": "daily_probe", "model": str(self.adapter.model_id)}
            ) as run:
                log_drift_run(
                    run,
                    params={
                        'target_provider': type(self.adapter).__name__,
                        'model': self.adapter.model_id,
                        'prompt_set_version': self.canary_prompts.get('version'),
                        'embedding_model': self.config['embeddings']['model_name'],
                        'alpha': self.config['alerts']['significance_threshold'],
                        'ref_window_size': ref_size,
                        'cur_window_size': cur_size,
                        'kernel_bandwidth': bandwidth,
                        'git_hash': get_git_hash(),
                        'seed': self.random_seed,
                    },
                    metrics={
                        'ks_stat': results.get('ks_statistic'),
                        'z_stat': results.get('proportion_z_stat'),
                        'mmd_stat': results.get('mmd_statistic'),
                        'ks_pvalue': results.get('ks_pvalue'),
                        'z_pvalue': results.get('proportion_z_pvalue'),
                        'mmd_pvalue': results.get('mmd_pvalue'),
                        'bh_ks_pvalue': results.get('ks_adjusted_pvalue'),
                        'bh_z_pvalue': results.get('proportion_z_adjusted_pvalue'),
                        'bh_mmd_pvalue': results.get('mmd_adjusted_pvalue'),
                        'flagged_probes': flagged_count,
                        'mean_latency': 0.0,
                        'token_counts': total_tokens,
                    },
                    artifacts={'drift_report': results},
                )
        except Exception:
            # MLflow logging failure should never crash the monitoring pipeline
            pass

    def run_monitoring_cycle(self, num_probes: Optional[int] = None) -> Dict[str, Any]:
        """
        Run a complete monitoring cycle: probes + tests + alerts.
        """
        # Run probes
        run_ids = self.run_canary_probes(num_probes)
        
        # Check if we have reference
        if not self.has_sufficient_reference():
            # Create reference by collecting more data
            # For now, just note - tests need reference + current
            return {
                'run_ids': run_ids,
                'tests_run': False,
                'reason': 'Insufficient reference data'
            }
        
        # Get reference and current windows
        # Windows are disjoint by construction: ref = oldest ref_size runs, cur = newest cur_size runs
        ref_size = self.config['reference']['window_size']
        cur_size = self.config['monitoring']['batch_size']
        min_ref = self.config['reference'].get('min_runs', 10)

        # Get all runs in order (including failed)
        all_runs = self.storage.get_all_runs_ordered()

        # Require complete windows (ref = oldest ref_size runs, cur = newest cur_size runs)
        if len(all_runs) < ref_size + cur_size:
            return {
                'run_ids': run_ids,
                'tests_run': False,
                'reason': 'Insufficient disjoint reference data'
            }

        ref_runs = all_runs[:ref_size]
        cur_runs = all_runs[-cur_size:]  # Most recent window
        ref_run_ids = self.storage.run_ids_from_runs(ref_runs)
        cur_run_ids = self.storage.run_ids_from_runs(cur_runs)
        
        # If still insufficient reference, don't proceed with tests
        if len(ref_runs) < min_ref:
            # Don't run tests - insufficient disjoint reference
            timestamp = datetime.now().isoformat()
            return {
                'run_ids': run_ids,
                'tests_run': False,
                'reason': 'Insufficient disjoint reference data'
            }
        
        # Extract features - track effective sample sizes
        ref_lengths = self.storage.get_response_lengths_for_runs(ref_run_ids)
        cur_lengths = self.storage.get_response_lengths_for_runs(cur_run_ids)
        
        ref_validity = self.storage.get_validity_for_runs(ref_run_ids, include_failed=True)
        cur_validity = self.storage.get_validity_for_runs(cur_run_ids, include_failed=True)
        
        # Extract embeddings (only for runs that have them)
        ref_embeddings = self.storage.get_embeddings_for_runs(ref_run_ids)
        cur_embeddings = self.storage.get_embeddings_for_runs(cur_run_ids)
        
        timestamp = datetime.now().isoformat()
        
        # Run statistical tests
        results = {
            'timestamp': timestamp,
            'reference_size': len(ref_runs),
            'current_size': len(cur_runs),
            'ref_length_count': len(ref_lengths),
            'cur_length_count': len(cur_lengths),
            'ref_emb_count': len(ref_embeddings),
            'cur_emb_count': len(cur_embeddings),
            'ref_valid_count': len(ref_validity),
            'cur_valid_count': len(cur_validity),
        }
        
        # KS test on response lengths
        # Require sufficient usable samples
        min_samples = self.config['alerts'].get('min_sample_size', 10)
        if len(ref_lengths) < min_samples or len(cur_lengths) < min_samples:
            ks_stat, ks_pval = float('nan'), 1.0
        else:
            ks_stat, ks_pval = ks_test(ref_lengths, cur_lengths)
        results['ks_statistic'] = ks_stat
        results['ks_pvalue'] = ks_pval
        results['ks_raw_pvalue'] = ks_pval
        
        # Two-proportion z-test on validity
        if len(ref_validity) < 5 or len(cur_validity) < 5:
            prop_z_stat, prop_z_pval = float('nan'), 1.0
        else:
            prop_z_stat, prop_z_pval = two_proportion_z_test(ref_validity, cur_validity)
        results['proportion_z_stat'] = prop_z_stat
        results['proportion_z_pvalue'] = prop_z_pval
        results['proportion_z_raw_pvalue'] = prop_z_pval
        
        # MMD test on embeddings
        if len(ref_embeddings) < 2 or len(cur_embeddings) < 2:
            mmd_stat, mmd_pval = 0.0, 1.0
        else:
            mmd_stat, mmd_pval = mmd_test(
                ref_embeddings,
                cur_embeddings,
                n_permutations=500,  # Smaller for speed
                random_seed=self.random_seed
            )
        results['mmd_statistic'] = mmd_stat
        results['mmd_pvalue'] = mmd_pval
        results['mmd_raw_pvalue'] = mmd_pval
        
        # Benjamini-Hochberg correction on the three p-values
        pvalues = [ks_pval, prop_z_pval, mmd_pval]
        alpha = self.config.get('alerts', {}).get('significance_threshold', 0.05)
        adjusted_pvals, significant_flags = benjamini_hochberg_correction(pvalues, alpha=alpha)
        
        results['ks_adjusted_pvalue'] = adjusted_pvals[0] if len(adjusted_pvals) > 0 else 1.0
        results['ks_significant'] = 1 if len(significant_flags) > 0 and significant_flags[0] else 0
        results['proportion_z_adjusted_pvalue'] = adjusted_pvals[1] if len(adjusted_pvals) > 1 else 1.0
        results['proportion_z_significant'] = 1 if len(significant_flags) > 1 and significant_flags[1] else 0
        results['mmd_adjusted_pvalue'] = adjusted_pvals[2] if len(adjusted_pvals) > 2 else 1.0
        results['mmd_significant'] = 1 if len(significant_flags) > 2 and significant_flags[2] else 0
        
        # Determine overall results
        bh_significant = []
        test_names = ['ks', 'proportion_z', 'mmd']
        for i, name in enumerate(test_names):
            if i < len(significant_flags) and significant_flags[i]:
                bh_significant.append(name)
        results['bh_significant_tests'] = bh_significant
        results['overall_drift_detected'] = 1 if len(bh_significant) > 0 else 0
        
        # Quality degradation detection
        quality_cfg = self.config['alerts']
        min_sample = quality_cfg.get('min_sample_size', 10)
        quality_thresh = quality_cfg.get('quality_degradation_threshold', 0.3)
        
        # Check for demonstrated quality regression based on validity
        overall_quality_degradation = 0
        if len(cur_validity) >= min_sample:
            cur_invalid_prop = 1.0 - np_mean(cur_validity) if cur_validity else 0
            if cur_invalid_prop >= quality_thresh:
                overall_quality_degradation = 1
        
        results['overall_quality_degradation'] = overall_quality_degradation
        
        # Store results
        self.storage.insert_test_results(results)
        
        # Generate alerts
        alerts = self._generate_alerts(results)

        # Log drift run to MLflow (results/alerts persist in SQLite regardless).
        # Memory-constrained hosts (e.g., Render free tier) can set DRIFT_MONITOR_TRACKING=0
        # to skip loading mlflow during runs.
        tracking_enabled = os.environ.get(
            "DRIFT_MONITOR_TRACKING", "1").strip().lower() not in ("0", "false", "no")
        if tracking_enabled:
            self._log_mlflow_run(results, ref_size, cur_size, ref_embeddings, cur_embeddings, cur_lengths)

        return {
            'run_ids': run_ids,
            'tests_run': True,
            'results': results,
            'alerts': alerts
        }
    
    def _generate_alerts(self, results: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate alerts based on test results."""
        alerts = []
        timestamp = results['timestamp']
        
        # Alert for drift
        if results.get('overall_drift_detected', 0):
            alert = {
                'timestamp': timestamp,
                'alert_type': 'drift',
                'severity': 'medium',
                'message': f"Behavioral drift detected. Significant tests: {results.get('bh_significant_tests', [])}",
                'ks_pvalue': results.get('ks_adjusted_pvalue'),
                'proportion_z_pvalue': results.get('proportion_z_adjusted_pvalue'),
                'mmd_pvalue': results.get('mmd_adjusted_pvalue'),
                'overall_drift_detected': 1,
                'overall_quality_degradation': results.get('overall_quality_degradation', 0)
            }
            alerts.append(alert)
            self.storage.insert_alert(alert)
        
        # Alert for quality degradation
        if results.get('overall_quality_degradation', 0):
            alert = {
                'timestamp': timestamp,
                'alert_type': 'quality_degradation',
                'severity': 'high',
                'message': 'High empty/invalid-response rate heuristic; investigate possible quality impact.',
                'ks_pvalue': results.get('ks_adjusted_pvalue'),
                'proportion_z_pvalue': results.get('proportion_z_adjusted_pvalue'),
                'mmd_pvalue': results.get('mmd_adjusted_pvalue'),
                'overall_drift_detected': results.get('overall_drift_detected', 0),
                'overall_quality_degradation': 1
            }
            alerts.append(alert)
            self.storage.insert_alert(alert)
        
        return alerts

def np_mean(arr):
    """Helper for mean."""
    import numpy as np
    return np.mean(arr) if arr else 0
