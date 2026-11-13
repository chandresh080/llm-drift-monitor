import json
import uuid
import re
import logging

import yaml
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
import random
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from storage import DriftMonitorStorage
from embeddings import EmbeddingExtractor
from drift_tests import (ks_test, proportion_test, mmd_test,
                         benjamini_hochberg_correction, centroid_distance,
                         count_unique_embeddings, holm_correction,
                         paired_cluster_mmd_test, category_mmd_tests)
from behavior_features import extract_behavior_features, refusal_rate
from sequential_testing import (spent_alpha, consecutive_flagged_history,
                                consecutive_streak)

logger = logging.getLogger("llm_drift_monitor.monitor")


def _memory_headroom_mb(base: str = "/sys/fs/cgroup") -> Optional[float]:
    """Memory available to the container in MB, or None when unknown.

    Reads the cgroup v2 memory files; cgroup v1 paths and hosts without
    cgroup accounting return None so callers treat memory as unconstrained.
    """
    try:
        with open(os.path.join(base, "memory.max"), encoding="utf-8") as handle:
            limit = handle.read().strip()
        with open(os.path.join(base, "memory.current"), encoding="utf-8") as handle:
            current = handle.read().strip()
        if not limit or limit == "max":
            return None
        return (int(limit) - int(current)) / (1024 * 1024)
    except (OSError, ValueError):
        return None


def _mlflow_headroom_ok(headroom_mb: Optional[float],
                        required_mb: float = 450.0) -> bool:
    """Whether the container can afford a mlflow import in a subprocess.

    The mlflow import costs roughly 400 MB inside the child process, and that
    memory counts against the instance limit even though it is not resident
    in the serving process: the earlier design assumed a subprocess keeps a
    memory capped deployment safe, but the cgroup ceiling applies to the
    whole instance, and the child was the OOM killer's trigger at cycle end.
    """
    return headroom_mb is None or headroom_mb >= required_mb

# Provider keys may arrive as Render-style secret files (a file named after the
# variable, mounted at /etc/secrets/<name>) instead of environment variables.
# The adapters read keys from os.environ, so these files are folded into the
# process environment before the adapter is built.
_PROVIDER_KEY_NAMES = ("GEMINI_API_KEY", "GROQ_API_KEY",
                       "OPENAI_API_KEY", "XAI_API_KEY")


def load_provider_key_secret_files(
        search_dirs: Tuple[str, ...] = ("/etc/secrets", ".")) -> None:
    """Fold provider key secret files into the process environment.

    For each known key name absent from the environment, the first search
    directory holding a non-empty file of that name wins, and the stripped
    file content becomes the variable's value. A variable already present is
    never overwritten, so an explicitly set key keeps precedence and a missing
    key keeps its explicit unconfigured state.
    """
    for name in _PROVIDER_KEY_NAMES:
        if os.environ.get(name):
            continue
        for directory in search_dirs:
            path = os.path.join(directory, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    value = handle.read().strip()
            except OSError:
                continue
            if value:
                os.environ[name] = value
                logger.info("Provider key %s loaded from secret file", name)
                break



def _generate_batch_concurrent(generate, prompts: List[str],
                               max_workers: int = 8) -> List[str]:
    """Run prompt generation concurrently, preserving order.

    HTTP adapters spend their time waiting on the target model, so overlapping
    the calls shortens a cycle from the sum of response times to roughly the
    longest one. Order is preserved, and each call keeps its own retry logic.
    """
    if len(prompts) <= 1:
        return [generate(p) for p in prompts]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(prompts))) as pool:
        return list(pool.map(generate, prompts))


class RateGate:
    """Minimum wall clock spacing between provider calls, thread safe.

    Free tier quotas cap calls per minute, a wall clock allowance, so
    concurrency does not buy throughput: call starts must be spaced apart
    no matter how many workers exist. Each caller reserves the next open
    slot up front under the lock, then sleeps the residual, so slots are
    handed out in order even if some callers lag on sleep scheduling.
    """

    def __init__(self, min_interval: float):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.min_interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)


def complete_cycles_only(all_runs: List[Dict[str, Any]],
                         prompt_ids: List[str]) -> List[Dict[str, Any]]:
    """Keep only runs that belong to a complete monitoring cycle.

    A cycle is complete when it contains exactly one stored run per panel
    prompt id. Partial cycles (failed or interrupted batches) and legacy rows
    without a cycle id are excluded, so a window can never mix categories or
    prompt coverage unevenly: every window is a whole number of whole cycles.
    """
    required = set(prompt_ids)
    by_cycle: Dict[str, List[Dict[str, Any]]] = {}
    for run in all_runs:
        cycle_id = run.get('cycle_id')
        if not cycle_id:
            continue
        by_cycle.setdefault(cycle_id, []).append(run)
    complete = {
        cycle_id for cycle_id, runs in by_cycle.items()
        if len(runs) == len(required)
        and {r.get('prompt_id') for r in runs} == required
    }
    return [run for run in all_runs if run.get('cycle_id') in complete]


def select_matched_windows(all_runs: List[Dict[str, Any]], prompt_version: str,
                           prompt_ids: List[str], ref_size: int,
                           cur_size: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pick reference and current windows that compare like with like.

    Only runs from the same prompt set version and the same prompt ids as the
    current cycle are eligible: comparing responses to different prompts would
    measure the prompt mix, not the model. Reference is the oldest eligible
    runs, current the newest, and the windows are disjoint by construction.
    """
    eligible = [
        run for run in all_runs
        if run.get('prompt_version') == prompt_version and run.get('prompt_id') in set(prompt_ids)
    ]
    ref_runs = eligible[:ref_size]
    rest = eligible[ref_size:]
    cur_runs = rest[-cur_size:] if len(rest) >= cur_size else []
    return ref_runs, cur_runs


class CircuitBreaker:
    """Fail fast when the target model is down instead of paying full retries.

    After a run of consecutive failures every call raises immediately for a
    cooldown window; the first call after the window probes the target again.
    Successful calls reset the failure count.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._consecutive_failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    @property
    def open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def before_call(self) -> None:
        with self._lock:
            if time.monotonic() < self._open_until:
                raise RuntimeError(
                    "Target model is unavailable (circuit open); retry after the cooldown")

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._open_until = time.monotonic() + self.cooldown_seconds
                self._consecutive_failures = 0


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
            # A real User-Agent: some hosted endpoints (Groq behind Cloudflare)
            # reject the default Python-urllib agent with HTTP 403.
            headers = {"User-Agent": "llm-drift-monitor/1.0", **(headers or {})}
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
                delay = backoff * (0.5 + random.random())
                if status == 429:
                    # Honor whatever wait the provider recommended: a header
                    # when present (most hosts), otherwise the google.rpc
                    # RetryInfo delay Google embeds in the 429 body. Backing
                    # off less than that just guarantees another 429.
                    told = (exc.headers.get("Retry-After")
                            if exc.headers else None)
                    if not told:
                        match = re.search(
                            r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', detail)
                        told = match.group(1) if match else None
                    try:
                        delay = max(delay, min(float(told), 90.0))
                    except (TypeError, ValueError):
                        pass
                time.sleep(delay)
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
    """Demo LLM adapter for testing. Completely offline, no paid APIs."""

    def __init__(self, model_id: str = "fake-llm-v1", seed: int = 42):
        self.model_id = model_id
        self.seed = seed

    def generate(self, prompt: str) -> str:
        """Generate a demo response for a prompt, with no shared state.

        Every choice, both the base response selection and the occasional
        truncation, is derived from hashes of the seed and prompt alone.
        This keeps generation deterministic under the concurrent probe pool:
        with a shared random generator, which call consumed which draw would
        depend on thread scheduling, and the same prompt could produce a
        different answer run to run.
        """
        import hashlib
        h = hashlib.md5(f"{self.seed}:{prompt}".encode()).hexdigest()
        base_responses = [
            f"Response to: {prompt[:50]}... This is a generated answer.",
            f"Here's an explanation: {prompt}",
            "Based on the input, the answer involves considering multiple factors.",
            "This is a simulated response for testing purposes.",
        ]
        response = base_responses[int(h, 16) % len(base_responses)]
        # About 10% of prompts show the truncated variant. The trigger is a
        # second independent hash slice, deliberately not correlated with the
        # response selection above.
        variation = int(
            hashlib.md5(f"{self.seed}:variation:{prompt}".encode()).hexdigest()[:12],
            16) % 100
        if variation < 10 and len(response) > 8:
            response = response[:len(response) // 2] + "..."
        return response
    
    def generate_batch(self, prompts: List[str]) -> List[str]:
        """Generate responses for multiple prompts."""
        return [self.generate(p) for p in prompts]

    def ping(self) -> bool:
        """Availability probe for the health endpoint; always true offline."""
        return True

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
        self.breaker = CircuitBreaker()

    def generate(self, prompt: str) -> str:
        """Generate one response using Ollama's non-streaming endpoint."""
        self.breaker.before_call()
        payload = {
            "model": self.model_id,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }
        try:
            result = _post_json_with_retry(
                url=f"{self.base_url}/api/generate",
                payload=payload,
                timeout=self.timeout,
                max_retries=self.max_retries,
                label="Ollama",
                headers={"Content-Type": "application/json"},
            )
        except Exception:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        generated = result.get("response")
        if not isinstance(generated, str):
            raise RuntimeError("Ollama response is missing the generated text")
        return generated

    def generate_batch(self, prompts: List[str]) -> List[str]:
        """Generate responses for prompts concurrently; each call is HTTP bound."""
        return _generate_batch_concurrent(self.generate, prompts)

    def ping(self) -> bool:
        """Cheap availability probe for the health endpoint."""
        try:
            with urlopen(Request(f"{self.base_url}/api/tags",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "llm-drift-monitor/1.0"}),
                         timeout=3.0) as response:
                return response.status == 200
        except Exception:
            return False

class OpenAICompatibleAdapter:
    """Call an OpenAI-compatible chat-completions API (e.g. Grok via api.x.ai, Gemini via Google)."""

    def __init__(self, model_id: str = "gpt-3.5-turbo",
                 api_key: Optional[str] = None,
                 base_url: str = "https://api.openai.com/v1",
                 timeout: float = 120.0,
                 temperature: float = 0.0,
                 max_tokens: int = 128,
                 max_retries: int = 3,
                 reasoning_effort: Optional[str] = None):
        self.model_id = model_id
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or ""
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.max_retries = int(max_retries)
        # Optional provider knob for reasoning models (e.g. Groq's
        # openai/gpt-oss-20b supports 'low'|'medium'|'high'). Without cap
        # management these models can spend the whole token budget on hidden
        # reasoning and return no answer text at all.
        self.reasoning_effort = reasoning_effort
        self.breaker = CircuitBreaker()
        if not self.api_key:
            raise RuntimeError(
                "OpenAI-compatible API key is required. Set OPENAI_API_KEY (e.g. in .env) "
                "or pass api_key explicitly."
            )

    def generate(self, prompt: str) -> str:
        """Generate one response using an OpenAI-compatible /chat/completions endpoint."""
        self.breaker.before_call()
        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        try:
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
        except Exception:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        # Token usage, when the provider returns it, is kept for experiment
        # reporting; the monitoring pipeline itself only needs the text.
        self.last_usage = result.get('usage')
        choice = result.get("choices", [{}])[0]
        generated = choice.get("message", {}).get("content")
        if not isinstance(generated, str) or not generated:
            # Reasoning models can spend the whole token budget before any
            # content appears; an empty answer is a failed call, not data.
            raise RuntimeError(
                "OpenAI-compatible response is missing the generated text "
                f"(finish reason: {choice.get('finish_reason', 'unknown')})")
        return generated

    def ping(self) -> bool:
        """Cheap availability probe for the health endpoint."""
        try:
            with urlopen(Request(f"{self.base_url}/models",
                                 headers={"Authorization": f"Bearer {self.api_key}",
                                          "User-Agent": "llm-drift-monitor/1.0"}),
                         timeout=3.0) as response:
                return response.status == 200
        except Exception:
            return False

    def generate_batch(self, prompts: List[str]) -> List[str]:
        """Generate responses for prompts concurrently; each call is HTTP bound."""
        return _generate_batch_concurrent(self.generate, prompts)


class GeminiAdapter:
    """Call Google Gemini through the native generateContent endpoint.

    The OpenAI-compatible path above also nominally covers Gemini, but the
    hosted compatibility layer has been observed to 404 for some models, so
    this adapter speaks the native generativelanguage API instead and keeps
    the difference encapsulated here. Responses are normalized to plain text
    so the monitoring pipeline needs no provider-specific handling.
    """

    def __init__(self, model_id: str = "gemini-3.1-flash-lite",
                 api_key: Optional[str] = None,
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta",
                 timeout: float = 60.0,
                 temperature: float = 0.0,
                 max_tokens: int = 128,
                 max_retries: int = 6):
        self.model_id = model_id
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or ""
        self.base_url = base_url.rstrip("/")
        self.timeout = float(timeout)
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.max_retries = int(max_retries)
        self.breaker = CircuitBreaker()
        self.last_usage = None
        if not self.api_key:
            raise RuntimeError(
                "Gemini API key is required. Set GEMINI_API_KEY (e.g. in .env) "
                "or pass api_key explicitly.")

    def generate(self, prompt: str) -> str:
        """Generate one response using the native generateContent endpoint."""
        self.breaker.before_call()
        url = f"{self.base_url}/models/{self.model_id}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": self.temperature,
                "maxOutputTokens": self.max_tokens,
            },
        }
        try:
            result = _post_json_with_retry(
                url=url,
                payload=payload,
                timeout=self.timeout,
                max_retries=self.max_retries,
                label="Gemini native endpoint",
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": self.api_key,
                },
            )
        except Exception:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        # Token usage, when the provider returns it, is kept for experiment
        # reporting; the monitoring pipeline itself only needs the text.
        self.last_usage = result.get('usageMetadata')
        candidates = result.get("candidates") or []
        parts = (candidates[0].get("content", {}).get("parts", [])
                 if candidates else [])
        texts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        generated = "".join(texts)
        if not generated:
            finish = candidates[0].get("finishReason", "UNKNOWN") if candidates else "NONE"
            raise RuntimeError(
                f"Gemini response contained no text (finishReason: {finish})")
        return generated

    def generate_batch(self, prompts: List[str]) -> List[str]:
        """Generate responses for prompts concurrently; each call is HTTP bound."""
        return _generate_batch_concurrent(self.generate, prompts)

    def ping(self) -> bool:
        """Cheap availability probe for the health endpoint."""
        try:
            with urlopen(Request(f"{self.base_url}/models",
                                 headers={"x-goog-api-key": self.api_key,
                                          "User-Agent": "llm-drift-monitor/1.0"}),
                         timeout=3.0) as response:
                return response.status == 200
        except Exception:
            return False


def balanced_category_subset(items: List[Dict[str, Any]],
                             num: Optional[int]) -> List[Dict[str, Any]]:
    """A fixed, category balanced subset of the canary suite.

    Taking the first N prompts would exercise only the first category in every
    cycle. Round robing across categories in a stable order keeps the
    selection deterministic (same prompts each cycle, so windows stay matched)
    while spreading coverage over the whole suite.
    """
    if num is None or num >= len(items):
        return list(items)
    by_category: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_category.setdefault(item['category'], []).append(item)
    categories = sorted(by_category)
    selected: List[Dict[str, Any]] = []
    index = 0
    while len(selected) < num:
        for category in categories:
            bucket = by_category[category]
            if index < len(bucket) and len(selected) < num:
                selected.append(bucket[index])
        index += 1
    return selected


class LLMDriftMonitor:
    """Main monitoring pipeline."""
    
    def __init__(self, config_path: str = "config/config.yaml"):
        self.config = self._load_config(config_path)
        # Load environment variables from a local .env file (see config/.env.example)
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass  # python-dotenv optional; fall back to process environment only
        # Render stores keys as secret files at /etc/secrets/<name>; fold those
        # into the environment before the adapter decides it is unconfigured.
        load_provider_key_secret_files()
        self.storage = DriftMonitorStorage(self.config['data']['db_path'])
        
        # Initialize embedding extractor
        emb_cfg = self.config['embeddings']
        self.embedder = EmbeddingExtractor(
            model_name=emb_cfg['model_name'],
            device=emb_cfg['device']
        )
        
        self.random_seed = self.config['monitoring']['random_seed']
        random.seed(self.random_seed)
        
        # Initialize target adapter.
        # Missing provider configuration is an explicit startup state, not a
        # silent fallback to the synthetic adapter: the monitor builds without
        # an adapter, every read endpoint keeps serving stored data, and any
        # probe attempt fails with a provider unavailable error naming the
        # problem. A run against a synthetic stand in while Gemini was meant
        # would be the one thing worse than a stopped demo.
        try:
            self.adapter = self._create_adapter()
            self.adapter_error = None
        except RuntimeError as exc:
            if 'API key is required' not in str(exc):
                raise
            self.adapter = None
            self.adapter_error = str(exc)

        # Concurrency for the probe pool: bounded so a free tier rate limit
        # is a backoff, not a storm. Config surfaced under target.max_workers.
        self.max_workers = int(self.config['target'].get('max_workers', 8))
        # Minimum spacing between provider call starts. Free tier per minute
        # quotas are a wall clock allowance, so this gate matters more than
        # the worker count when the quota is small (5/min on some models).
        self.call_spacing_seconds = float(
            self.config['target'].get('call_spacing_seconds', 0.0))

        # Load canary prompts
        self.canary_prompts = self._load_canary_prompts()
    
    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from YAML."""
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    
    def _load_canary_prompts(self) -> Dict[str, Any]:
        """Load canary prompts, normalized to structured entries.

        Each entry is {'id', 'category', 'text'}: the stable id is what makes
        window comparisons matched (same prompts in reference and current),
        and the category enables per category analysis. Plain string prompts
        from older suite files are accepted and given sequential ids.
        """
        with open('config/canary_prompts.json', 'r') as f:
            raw = json.load(f)
        items = []
        for i, entry in enumerate(raw['prompts'], start=1):
            if isinstance(entry, str):
                items.append({'id': f'p{i:03d}', 'category': 'general', 'text': entry})
            else:
                items.append({
                    'id': entry['id'],
                    'category': entry.get('category', 'general'),
                    'text': entry['text'] if 'text' in entry else entry['prompt'],
                    # Display fields: the public label replaces the raw text
                    # wherever the panel is shown, and prompts marked for
                    # redaction never expose their raw wording on the API.
                    'public_label': entry.get('public_label')
                        or (entry['text'] if 'text' in entry else entry['prompt']),
                    'redact_prompt': bool(entry.get('redact_prompt', False)),
                })
        return {'version': raw['version'], 'prompts': items}
    
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
        elif adapter_type == 'gemini':
            # Gemini via the native generateContent endpoint, which is the
            # verified path (the hosted OpenAI-compatible layer has been
            # observed to 404 for some models).
            return GeminiAdapter(
                # Config is the source of truth for the model id; the
                # environment variable is a fallback, not an override, so a
                # stale GEMINI_MODEL in an .env cannot silently point the
                # hosted demo at a different quota tier than the one the
                # baseline was collected with.
                model_id=(target_cfg.get('model_id')
                          or os.environ.get('GEMINI_MODEL')
                          or 'gemini-3.1-flash-lite'),
                api_key=os.environ.get('GEMINI_API_KEY') or '',
                base_url=target_cfg.get(
                    'base_url', 'https://generativelanguage.googleapis.com/v1beta'),
                timeout=target_cfg.get('timeout_seconds', 60),
                temperature=target_cfg.get('temperature', 0.0),
                max_tokens=target_cfg.get('max_tokens', 128),
            )
        elif adapter_type == 'openai-compatible':
            if os.environ.get('GEMINI_API_KEY'):
                # Gemini (Google AI) via the OpenAI-compatible endpoint
                return OpenAICompatibleAdapter(
                    model_id=(target_cfg.get('model_id')
                              or os.environ.get('GEMINI_MODEL')
                              or 'gemini-3.8-flash'),
                    api_key=os.environ.get('GEMINI_API_KEY') or os.environ.get('OPENAI_API_KEY') or '',
                    base_url=os.environ.get('GEMINI_BASE_URL') or 'https://generativelanguage.googleapis.com/v1beta/openai/',
                )
            if os.environ.get('GROQ_API_KEY'):
                # Groq via the OpenAI-compatible endpoint
                return OpenAICompatibleAdapter(
                    model_id=os.environ.get('GROQ_MODEL') or 'llama-3.3-70b-versatile',
                    api_key=os.environ.get('GROQ_API_KEY') or os.environ.get('OPENAI_API_KEY') or '',
                    base_url=os.environ.get('GROQ_BASE_URL') or 'https://api.groq.com/openai/v1',
                    reasoning_effort=target_cfg.get('reasoning_effort'),
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
        if self.adapter is None:
            raise RuntimeError(
                f'Provider unavailable: {self.adapter_error}')
        items = self.canary_prompts['prompts']
        items = balanced_category_subset(items, num_prompts)

        prompt_version = self.canary_prompts['version']
        model_id = self.adapter.model_id
        timestamp = datetime.now().isoformat()
        cycle_id = str(uuid.uuid4())[:8]
        adapter_type = self.config['target']['type']

        # Generate responses, timing each individual model call. The batch runs
        # concurrently, so wall clock divided by probe count would understate
        # latency by the concurrency factor; only per call timings are honest.
        latencies: List[float] = []
        token_totals: List[int] = []

        gate = RateGate(self.call_spacing_seconds)

        def timed_generate(prompt: str):
            gate.wait()
            call_started = time.perf_counter()
            try:
                try:
                    text = self.adapter.generate(prompt)
                    usage = getattr(self.adapter, 'last_usage', None)
                    if isinstance(usage, dict) and usage.get('totalTokenCount') is not None:
                        try:
                            token_totals.append(int(usage['totalTokenCount']))
                        except (TypeError, ValueError):
                            pass
                except RuntimeError as exc:
                    # A provider side no text response (Gemini signals these
                    # with finishReason RECITATION) is an availability miss,
                    # not a network failure. At temperature 0 it reproduces
                    # stably, so a retry would spend quota for the same
                    # result. It is recorded as the empty response it is,
                    # and the Response availability family counts the miss.
                    if 'contained no text' not in str(exc):
                        raise
                    text = ''
                    logger.warning(
                        "Provider returned no text for one probe (recitation "
                        "guard); recorded as an empty response.")
                return text
            finally:
                latencies.append(time.perf_counter() - call_started)

        responses = _generate_batch_concurrent(
            timed_generate, [item['text'] for item in items],
            max_workers=self.max_workers)
        ordered = sorted(latencies)
        count = max(1, len(ordered))
        self.last_cycle_latency_seconds = round(sum(ordered) / count, 3)
        self.last_latency_p50_seconds = round(
            ordered[(count - 1) // 2] if ordered else 0.0, 3)
        self.last_latency_p95_seconds = round(
            ordered[min(count - 1, int(round(0.95 * (count - 1))))] if ordered else 0.0, 3)
        # Provider reported token accounting, when the adapter exposes it
        # (Gemini does). Nothing is inferred about quality from this; it is
        # only cost visibility for the configured free tier quota.
        self.last_tokens_total = sum(token_totals) if token_totals else None

        run_ids: List[int] = []
        responses_by_text = list(responses)
        embeddings = self.embedder.extract_embeddings(responses_by_text)

        for i, (item, response) in enumerate(zip(items, responses)):
            # Extract features
            response_length, is_valid = self.embedder.extract_response_features(response)
            behavior = extract_behavior_features(response)

            # Determine run status
            run_status = 'success' if is_valid else 'failed'

            # Insert probe run
            # Use proper timestamp with microseconds for uniqueness
            probe_ts = timestamp if i == 0 else (datetime.fromisoformat(timestamp) + timedelta(milliseconds=i)).isoformat()
            run_id = self.storage.insert_probe_run(
                timestamp=probe_ts,
                prompt_version=prompt_version,
                prompt_text=item['text'],
                model_id=model_id,
                response_text=response,
                response_length=response_length,
                is_valid=is_valid,
                run_status=run_status,
                adapter_type=adapter_type,
                prompt_id=item['id'],
                category=item['category'],
                cycle_id=cycle_id
            )
            run_ids.append(run_id)

            # Insert response features, including the behavioral signals
            self.storage.insert_response_features(
                run_id, response_length, is_valid,
                is_refusal=behavior['is_refusal'],
                repetition_ratio=behavior['repetition_ratio'],
                token_count=behavior['token_count'],
                has_code_block=behavior['has_code_block'],
            )

            # Insert embedding
            self.storage.insert_embedding(run_id, embeddings[i])

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
                        ref_embeddings: List[Any], cur_embeddings: List[float],
                        cur_lengths: List[float]) -> None:
        """Best-effort MLflow logging of one monitoring cycle. Never raises.

        The logging happens in a short lived subprocess so mlflow (and its
        memory footprint) never enters the serving process; this keeps a
        memory capped deployment safe while every cycle still lands in MLflow.
        """
        try:
            from tracking import log_drift_run_detached, get_git_hash
            from drift_tests import compute_median_bandwidth

            bandwidth = 1.0
            if len(ref_embeddings) >= 2 and len(cur_embeddings) >= 2:
                import numpy as np
                all_emb = np.vstack([ref_embeddings, cur_embeddings])
                bandwidth = round(compute_median_bandwidth(all_emb), 4)

            # The logging subprocess runs while this process may be near its
            # memory ceiling (model held warm), so drop the model before mlflow
            # exists in a second process. The next cycle reloads it on demand.
            self.embedder.release()

            # The child's memory counts against the instance ceiling even
            # though it is not resident here; skip tracking when the container
            # cannot afford the import (the hosted free tier cannot).
            headroom = _memory_headroom_mb()
            if not _mlflow_headroom_ok(headroom):
                logger.info(
                    "Skipping MLflow logging: %.0f MB memory headroom is below "
                    "what the mlflow import needs; tracking resumes on hosts "
                    "with headroom", headroom if headroom is not None else -1)
                return

            flagged_count = int(
                results.get('ks_significant', 0) +
                results.get('proportion_z_significant', 0) +
                results.get('mmd_significant', 0)
            )

            total_tokens = sum(cur_lengths) / 4.0 if cur_lengths else 0.0

            log_drift_run_detached({
                'run_name': f"drift_run_{results.get('timestamp', 'unknown')}",
                'experiment_name': 'llm-drift-daily',
                'tags': {"run_type": "daily_probe", "model": str(self.adapter.model_id)},
                'params': {
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
                'metrics': {
                    'ks_stat': results.get('ks_statistic'),
                    'z_stat': results.get('proportion_z_stat'),
                    'mmd_stat': results.get('mmd_statistic'),
                    'ks_pvalue': results.get('ks_pvalue'),
                    'z_pvalue': results.get('proportion_z_pvalue'),
                    'mmd_pvalue': results.get('mmd_pvalue'),
                    'bh_ks_pvalue': results.get('ks_adjusted_pvalue'),
                    'bh_z_pvalue': results.get('proportion_z_adjusted_pvalue'),
                    'bh_mmd_pvalue': results.get('mmd_adjusted_pvalue'),
                    'centroid_distance': results.get('centroid_distance'),
                    'length_shift_pct': results.get('length_shift_pct'),
                    'ref_refusal_rate': results.get('ref_refusal_rate'),
                    'cur_refusal_rate': results.get('cur_refusal_rate'),
                    'flagged_probes': flagged_count,
                    'mean_latency': getattr(self, 'last_cycle_latency_seconds', None),
                    'latency_p50': getattr(self, 'last_latency_p50_seconds', None),
                    'latency_p95': getattr(self, 'last_latency_p95_seconds', None),
                    'token_counts': total_tokens,
                    **self._category_mlflow_metrics(results),
                },
                'artifacts': {'drift_report': results},
            })
        except Exception:
            # MLflow logging failure should never crash the monitoring pipeline
            pass

    def _category_mlflow_metrics(self, results: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten category test results into cat_<name>_ p-value metrics."""
        metrics: Dict[str, Any] = {}
        for name, test in (results.get('category_tests') or {}).items():
            safe = ''.join(c if c.isalnum() else '_' for c in name)
            if test.get('pvalue') is not None:
                metrics[f'cat_{safe}_pvalue'] = test['pvalue']
            if test.get('adjusted_pvalue') is not None:
                metrics[f'cat_{safe}_adj_pvalue'] = test['adjusted_pvalue']
        return metrics

    def run_monitoring_cycle(self, num_probes: Optional[int] = None) -> Dict[str, Any]:
        """
        Run a complete monitoring cycle: probes + tests + alerts.
        """
        # Run probes; the configured batch size keeps CLI and API runs identical
        if num_probes is None:
            num_probes = self.config.get('monitoring', {}).get('batch_size')
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
        
        # Get reference and current windows, matched to this cycle's prompts:
        # only runs from the same prompt set version and the same prompt ids
        # are eligible, so a changed prompt mix cannot masquerade as drift.
        ref_size = self.config['reference']['window_size']
        cur_size = self.config['monitoring']['batch_size']
        min_ref = self.config['reference'].get('min_runs', 10)

        # Get all runs in order (including failed), keeping only runs that
        # belong to a complete monitoring cycle, so a window is always a
        # whole number of whole cycles with even prompt coverage.
        cycle_items = balanced_category_subset(self.canary_prompts['prompts'], num_probes)
        cur_prompt_ids = [item['id'] for item in cycle_items]
        all_runs = complete_cycles_only(
            self.storage.get_all_runs_ordered(), cur_prompt_ids)
        ref_runs, cur_runs = select_matched_windows(
            all_runs, self.canary_prompts['version'], cur_prompt_ids, ref_size, cur_size)

        ref_run_ids = self.storage.run_ids_from_runs(ref_runs)
        cur_run_ids = self.storage.run_ids_from_runs(cur_runs)

        # Both windows must be complete before anything is compared: a partial
        # window would store a junk result and dilute the alert history.
        if len(ref_runs) < ref_size or len(cur_runs) < cur_size:
            return {
                'run_ids': run_ids,
                'tests_run': False,
                'reason': 'Insufficient matched window data'
            }
        
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
        
        # Two-sample test on response validity, using Fisher's exact test when
        # the expected cell counts are too small for the normal approximation
        if len(ref_validity) < 5 or len(cur_validity) < 5:
            prop_stat, prop_pval, prop_method = float('nan'), 1.0, 'fisher'
        else:
            prop_stat, prop_pval, prop_method = proportion_test(ref_validity, cur_validity)
        results['proportion_z_stat'] = prop_stat
        results['proportion_z_pvalue'] = prop_pval
        results['proportion_z_raw_pvalue'] = prop_pval
        results['proportion_test_method'] = prop_method
        
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
        # The pooled response level permutation below is retained as an
        # exploratory statistic and an effect size reference. Which MMD
        # enters the significance family is decided right after the paired
        # prompt level test runs.
        results['mmd_statistic_pooled'] = mmd_stat
        results['mmd_pvalue_pooled'] = mmd_pval
        # Distinct embedding vectors next to the nominal window sizes: a
        # deterministic adapter repeats the same answers, so identical p
        # values across cycles over identical samples are repeats, not
        # independent confirmations.
        results['reference_unique_responses'] = count_unique_embeddings(
            ref_embeddings)
        results['current_unique_responses'] = count_unique_embeddings(
            cur_embeddings)

        # Paired prompt level MMD: the matched panel probes the same prompt
        # ids on both sides, and answers from one prompt share that prompt as
        # a source of variation a response level permutation ignores. When
        # the panel is matched, this test is the inferential MMD: it is the
        # permutation that respects the repeated prompt structure, so it is
        # what enters the BH significance family and the alert path. The
        # pooled response level test stays in the results as an exploratory
        # statistic and effect size reference (see *_pooled fields).
        ref_paired = self.storage.get_embeddings_with_run_ids_for_runs(
            ref_run_ids)
        cur_paired = self.storage.get_embeddings_with_run_ids_for_runs(
            cur_run_ids)
        ref_prompt_of = {r['id']: r.get('prompt_id') for r in ref_runs}
        cur_prompt_of = {r['id']: r.get('prompt_id') for r in cur_runs}
        pc_stat, pc_pval, pc_pairs = paired_cluster_mmd_test(
            [emb for _, emb in ref_paired], [emb for _, emb in cur_paired],
            [ref_prompt_of.get(run_id, '') for run_id, _ in ref_paired],
            [cur_prompt_of.get(run_id, '') for run_id, _ in cur_paired],
            random_seed=self.random_seed)
        results['paired_cluster_mmd_statistic'] = pc_stat
        results['paired_cluster_mmd_pvalue'] = pc_pval
        results['paired_cluster_prompt_pairs'] = pc_pairs
        paired_available = pc_pairs is not None and pc_pairs > 0
        results['mmd_statistic'] = pc_stat if paired_available else mmd_stat
        mmd_family_pval = pc_pval if paired_available else mmd_pval
        results['mmd_pvalue'] = mmd_family_pval
        results['mmd_raw_pvalue'] = mmd_family_pval
        # The exchangeable unit and the permutation regime stay explicit:
        # whole prompt answer sets versus individual responses, and exhaustive
        # enumeration versus seeded sampling above the pattern cap.
        results['mmd_permutation_unit'] = ('prompt_cluster' if paired_available
                                           else 'response')
        results['paired_cluster_method'] = (
            ('exhaustive' if pc_pairs <= 12 else 'sampled')
            if paired_available else None)

        # Effect sizes, kept separate from significance: with enough samples a
        # trivial change becomes significant, so alerts report magnitude too.
        # Centroid distance is the mean-shift baseline MMD has to beat: a
        # large MMD with a near-zero centroid distance marks a shape change
        # (spread or mixture) that a mean based monitor would miss.
        if ref_lengths and cur_lengths:
            ref_mean_len = sum(ref_lengths) / len(ref_lengths)
            cur_mean_len = sum(cur_lengths) / len(cur_lengths)
            results['ref_mean_length'] = round(ref_mean_len, 1)
            results['cur_mean_length'] = round(cur_mean_len, 1)
            if ref_mean_len > 0:
                results['length_shift_pct'] = round(
                    100.0 * (cur_mean_len - ref_mean_len) / ref_mean_len, 1)
        else:
            results['ref_mean_length'] = None
            results['cur_mean_length'] = None
            results['length_shift_pct'] = None
        results['centroid_distance'] = round(
            centroid_distance(ref_embeddings, cur_embeddings), 6)

        # Behavioral rates from the stored features of the current window
        ref_features = self.storage.get_features_for_runs(ref_run_ids)
        cur_features = self.storage.get_features_for_runs(cur_run_ids)
        ref_refusals = [f.get('is_refusal') or 0 for f in ref_features]
        cur_refusals = [f.get('is_refusal') or 0 for f in cur_features]
        results['ref_refusal_rate'] = round(refusal_rate(ref_refusals), 4)
        results['cur_refusal_rate'] = round(refusal_rate(cur_refusals), 4)
        results['refusal_rate_change'] = round(
            results['cur_refusal_rate'] - results['ref_refusal_rate'], 4)

        # Category-wise MMD, so a detected change can be localized to the
        # part of the prompt suite that moved. Category windows are wider than
        # the headline windows (a 10-run window holds about two samples per
        # category, far too thin to test), staying disjoint from the reference
        # window and matched to the same prompt set. The family of category
        # p-values gets its own Benjamini Hochberg correction, independent of
        # the three test family.
        results['category_tests'] = self._run_category_tests(
            all_runs, cur_prompt_ids, cur_size)
        
        # Benjamini-Hochberg correction on the three p-values. The MMD entry
        # is the permutation that respects the repeated prompt structure
        # (prompt cluster level) whenever the panel is matched, so the
        # family's semantic p does not inherit cross prompt false confidence.
        pvalues = [ks_pval, prop_pval, results['mmd_pvalue']]
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
        # Legacy field: "any BH significant test", kept for stored history
        # compatibility. Never used for the headline or the drift alert, both
        # of which follow the split below.
        results['overall_drift_detected'] = 1 if len(bh_significant) > 0 else 0

        # Split signal groups. The headline can never call a validity rate
        # change a distribution shift: distribution_shift_detected follows
        # the two distributional detectors only, quality_change_detected is
        # the corrected validity comparison, and the absolute invalid share
        # threshold stays its own guardrail (overall_quality_degradation).
        results['distribution_shift_detected'] = 1 if (
            results['ks_significant'] or results['mmd_significant']) else 0
        results['quality_change_detected'] = int(
            results['proportion_z_significant'])
        
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

        # Provider cost and latency visibility for this cycle: per call
        # timings gathered during the probe batch, plus provider reported
        # token counts when the adapter exposes them.
        results['provider_latency_mean_seconds'] = getattr(
            self, 'last_cycle_latency_seconds', None)
        results['provider_latency_p50_seconds'] = getattr(
            self, 'last_latency_p50_seconds', None)
        results['provider_latency_p95_seconds'] = getattr(
            self, 'last_latency_p95_seconds', None)
        results['probe_tokens_total'] = getattr(self, 'last_tokens_total', None)
        
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
    
    def _run_category_tests(self, all_runs: List[Dict[str, Any]],
                            cur_prompt_ids: List[str], cur_size: int) -> Dict[str, Dict[str, Any]]:
        """Per category MMD tests, or an empty dict when disabled.

        Gated by monitoring.run_category_tests. The category current window
        spans monitoring.category_window_cycles cycles so each category gets
        a usable sample count; windows stay disjoint from the reference and
        matched to the same prompt ids and suite version. Categories without
        enough samples on both sides are reported as skipped rather than
        tested, so a thin category cannot produce a junk p value; the tested
        family gets its own Benjamini Hochberg correction, separate from the
        three-test family computed over the pooled windows.
        """
        cfg = self.config.get('monitoring', {})
        if not cfg.get('run_category_tests', False):
            return {}
        min_per_cat = cfg.get('min_samples_per_category', 8)
        cycles = cfg.get('category_window_cycles', 5)

        ref_size = self.config['reference']['window_size']
        prompt_version = self.canary_prompts['version']
        ref_runs_cat, cur_runs_cat = select_matched_windows(
            all_runs, prompt_version, cur_prompt_ids, ref_size, cur_size * cycles)
        if not ref_runs_cat or not cur_runs_cat:
            return {}
        ref_by_cat = self.storage.get_category_embeddings(
            self.storage.run_ids_from_runs(ref_runs_cat))
        cur_by_cat = self.storage.get_category_embeddings(
            self.storage.run_ids_from_runs(cur_runs_cat))
        alpha = self.config.get('alerts', {}).get('significance_threshold', 0.05)
        return category_mmd_tests(
            ref_by_cat, cur_by_cat, min_samples_per_category=min_per_cat,
            alpha=alpha, n_permutations=500, random_seed=self.random_seed)

    def get_window_comparison(self) -> Dict[str, Any]:
        """Reference versus current answers for display, prompt by prompt.

        Uses the exact window selection a monitoring cycle uses (matched
        prompt ids, panel version, complete cycles only), so the panel shows
        what the detectors actually compared. One answer per prompt per
        window is shown: the most recent run in the reference window and the
        most recent in the current window, with validity, refusal flag and
        length so a visitor can see metadata next to the text. Not a
        statistical result, a readout: a semantic drift flag can be harmless,
        and the panel exists so a person can inspect what changed.
        """
        num_probes = self.config['monitoring']['batch_size']
        cycle_items = balanced_category_subset(self.canary_prompts['prompts'],
                                               num_probes)
        cur_prompt_ids = [item['id'] for item in cycle_items]
        prompt_by_id = {p['id']: p for p in self.canary_prompts['prompts']}
        ref_size = self.config['reference']['window_size']
        cur_size = num_probes
        all_runs = complete_cycles_only(self.storage.get_all_runs_ordered(),
                                        cur_prompt_ids)
        ref_runs, cur_runs = select_matched_windows(
            all_runs, self.canary_prompts['version'], cur_prompt_ids,
            ref_size, cur_size)
        ready = len(ref_runs) >= ref_size and len(cur_runs) >= cur_size
        if not ready:
            return {
                'ready': False,
                'reference_stored': len(ref_runs),
                'reference_required': ref_size,
                'current_stored': len(cur_runs),
                'current_required': cur_size,
                'tests_start_at': ref_size + cur_size,
                'rows': [],
            }

        ref_by_prompt: Dict[str, Dict[str, Any]] = {}
        for run in ref_runs:  # ordered oldest first; later runs overwrite
            pid = run.get('prompt_id') or ''
            if pid:
                ref_by_prompt[pid] = run
        cur_by_prompt: Dict[str, Dict[str, Any]] = {}
        for run in cur_runs:
            pid = run.get('prompt_id') or ''
            if pid:
                cur_by_prompt[pid] = run

        ref_ids = [r['id'] for r in ref_by_prompt.values()]
        cur_ids = [r['id'] for r in cur_by_prompt.values()]
        refusal_of = dict(self.storage.get_refusal_with_run_ids_for_runs(
            ref_ids + cur_ids))

        def describe(run: Optional[Dict[str, Any]]) -> Dict[str, Any]:
            if run is None:
                return {'text': None, 'length': None, 'is_valid': None,
                        'is_refusal': None}
            return {
                'text': run.get('response_text'),
                'length': run.get('response_length'),
                'is_valid': run.get('is_valid'),
                'is_refusal': refusal_of.get(run['id']),
            }

        rows = []
        for pid in cur_prompt_ids:
            prompt = prompt_by_id.get(pid, {})
            # Display discipline: the public label replaces the raw prompt
            # text on the dashboard, and prompts marked for redaction (the
            # safety boundary group) never expose their exact wording at
            # all. A redacted response is withheld the same way, because a
            # model's answer can quote the disallowed request back.
            redacted = bool(prompt.get('redact_prompt', False))
            label = prompt.get('public_label') or prompt.get('text', pid)
            def describe_prompt(run: Optional[Dict[str, Any]]) -> Dict[str, Any]:
                if not redacted:
                    return describe(run)
                if run is None:
                    return {'text': None, 'length': None, 'is_valid': None,
                            'is_refusal': None}
                return {'text': '(withheld: safety boundary prompt)',
                        'length': run.get('response_length'),
                        'is_valid': run.get('is_valid'),
                        'is_refusal': refusal_of.get(run['id'])}
            rows.append({
                'prompt_id': pid,
                'category': prompt.get('category', 'unknown'),
                'label': label,
                'prompt_text': ('(withheld: safety boundary prompt)'
                                if redacted else prompt.get('text', pid)),
                'prompt_redacted': redacted,
                'reference': describe_prompt(ref_by_prompt.get(pid)),
                'current': describe_prompt(cur_by_prompt.get(pid)),
            })
        return {
            'ready': True,
            'reference_size': ref_size,
            'current_size': cur_size,
            'rows': rows,
        }

    def get_category_localization(self) -> Dict[str, Any]:
        """Category family state for display, without inventing statistics.

        Reports what the category family genuinely holds: the distinct and
        nominal counts per side, the skip decision when the distinct sample
        gate fails, and the BH adjusted p value only when a category is
        actually tested. This is the panel that shows the monitor declining
        to make a claim it cannot support.
        """
        num_probes = self.config['monitoring']['batch_size']
        cycle_items = balanced_category_subset(self.canary_prompts['prompts'],
                                               num_probes)
        cur_prompt_ids = [item['id'] for item in cycle_items]
        cur_size = num_probes
        all_runs = complete_cycles_only(self.storage.get_all_runs_ordered(),
                                        cur_prompt_ids)
        tests = self._run_category_tests(all_runs, cur_prompt_ids, cur_size)
        rows = []
        for category in sorted(tests):
            entry = tests[category]
            rows.append({
                'category': category,
                'decision': ('skipped' if entry.get('mmd_statistic') is None
                             else 'tested'),
                'reason': entry.get('reason'),
                'adjusted_pvalue': entry.get('adjusted_pvalue'),
                'n_reference': entry.get('n_reference'),
                'n_current': entry.get('n_current'),
                'unique_reference': entry.get('unique_reference'),
                'unique_current': entry.get('unique_current'),
            })
        return {
            'category_window_cycles': self.config.get(
                'monitoring', {}).get('category_window_cycles', 5),
            'min_samples_per_category': self.config.get(
                'monitoring', {}).get('min_samples_per_category', 8),
            'rows': rows,
        }

    def _generate_alerts(self, results: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Generate alerts based on test results.

        The drift alert is gated by the sequential policy: the per cycle BH
        flag must persist for alerts.min_consecutive_cycles consecutive stored
        results (the current one included) before anyone is paged. Isolated
        borderline cycles are recorded in test_results but stay silent. With
        alerts.use_alpha_spending enabled, the current cycle must also clear
        the quadratic spending boundary (with Holm correction within the
        cycle) for its position in the planning horizon, which makes early
        alerts demand strong evidence. The default persistence rule itself is
        an operational filter, not a formal error control procedure.
        """
        alerts = []
        timestamp = results['timestamp']
        alert_cfg = self.config.get('alerts', {})
        min_consecutive = int(alert_cfg.get('min_consecutive_cycles', 1))

        # Alert for distribution shift. The headline verdict and the alert
        # follow the distributional family only: a significant validity test
        # is a quality signal, not evidence that the response distribution
        # moved, and reporting it as a distribution shift would conflate
        # exactly the two behaviours this system separates.
        if results.get('distribution_shift_detected', 0):
            history = self.storage.get_recent_drift_flags(min_consecutive)
            persistence_ok = consecutive_flagged_history(history, min_consecutive)
            if persistence_ok and alert_cfg.get('use_alpha_spending', False):
                # Cycle index: number of stored results after inserting this one.
                cycle_index = len(self.storage.get_recent_drift_flags(1_000_000))
                horizon = int(alert_cfg.get('planning_horizon_cycles', 30))
                alpha = alert_cfg.get('significance_threshold', 0.05)
                # Holm, not BH: a spending boundary spends family-wise error,
                # so the within-cycle correction must control family-wise
                # error too. BH would only control the false discovery rate.
                holm_adj, _ = holm_correction(
                    [results.get('ks_pvalue'), results.get('proportion_z_pvalue'),
                     results.get('mmd_pvalue')], alpha=alpha)
                strongest = min(holm_adj)
                persistence_ok = strongest <= spent_alpha(cycle_index, horizon, alpha)

            if persistence_ok:
                affected = sorted(
                    name for name, t in (results.get('category_tests') or {}).items()
                    if t.get('significant'))
                parts = [
                    f"Distributional tests flagged: "
                    f"{[t for t in results.get('bh_significant_tests', []) if t != 'proportion_z']}",
                    f"streak: {consecutive_streak(history)} cycles",
                    f"MMD (prompt cluster) stat: {results.get('mmd_statistic')}",
                    f"centroid distance: {results.get('centroid_distance')}",
                    f"length shift: {results.get('length_shift_pct')}%",
                ]
                if affected:
                    parts.append(f"affected categories: {affected}")
                alert = {
                    'timestamp': timestamp,
                    'alert_type': 'drift',
                    'severity': 'medium',
                    'message': 'Behavioral drift detected. ' + '; '.join(parts),
                    'ks_pvalue': results.get('ks_adjusted_pvalue'),
                    'proportion_z_pvalue': results.get('proportion_z_adjusted_pvalue'),
                    'mmd_pvalue': results.get('mmd_adjusted_pvalue'),
                    'overall_drift_detected': 1,
                    'overall_quality_degradation': results.get('overall_quality_degradation', 0)
                }
                alerts.append(alert)
                self.storage.insert_alert(alert)
        
        # Alert for quality. Two distinct sources fire this family: a
        # statistically significant validity (non empty response rate)
        # change between the windows, and the absolute invalid response
        # share guardrail on the current window. The message names which
        # one moved, because they mean different things.
        quality_reasons = []
        if results.get('quality_change_detected', 0):
            quality_reasons.append(
                'validity (non empty response) rate shifted between the windows '
                'after correction')
        if results.get('overall_quality_degradation', 0):
            quality_reasons.append(
                'invalid response share reached the configured absolute '
                'threshold in the current window')
        if quality_reasons:
            alert = {
                'timestamp': timestamp,
                'alert_type': 'quality_degradation',
                'severity': 'high',
                'message': 'Quality signal: ' + ' and '.join(quality_reasons) + '. '
                           'Investigate the current window\'s answers.',
                'ks_pvalue': results.get('ks_adjusted_pvalue'),
                'proportion_z_pvalue': results.get('proportion_z_adjusted_pvalue'),
                'mmd_pvalue': results.get('mmd_adjusted_pvalue'),
                'overall_drift_detected': results.get('distribution_shift_detected', 0),
                'overall_quality_degradation': results.get(
                    'overall_quality_degradation', 0)
            }
            alerts.append(alert)
            self.storage.insert_alert(alert)
        
        return alerts

def np_mean(arr):
    """Helper for mean."""
    import numpy as np
    return np.mean(arr) if arr else 0
