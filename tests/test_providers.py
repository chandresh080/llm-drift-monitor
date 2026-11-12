"""Provider adapter tests. All network calls are mocked; no API quota is used."""
import pytest

import monitor
from monitor import (FakeLLMAdapter, GeminiAdapter, LLMDriftMonitor,
                     LocalLLMAdapter, OpenAICompatibleAdapter)


class TestGroqAdapter:
    """Groq is served by the OpenAI-compatible adapter pointed at api.groq.com."""

    def make_adapter(self):
        return OpenAICompatibleAdapter(
            model_id="openai/gpt-oss-20b",
            api_key="test-key",
            base_url="https://api.groq.com/openai/v1")

    def test_normalization_extracts_message_content(self, monkeypatch):
        captured = {}

        def fake_post(url, payload, timeout, max_retries, label, headers):
            captured['url'] = url
            captured['payload'] = payload
            captured['headers'] = headers
            return {
                'choices': [{'message': {'content': 'GROQ WORKING'}}],
                'usage': {'prompt_tokens': 7, 'completion_tokens': 3,
                          'total_tokens': 10},
            }

        monkeypatch.setattr(monitor, '_post_json_with_retry', fake_post)
        adapter = self.make_adapter()
        text = adapter.generate("Say GROQ WORKING")
        assert text == 'GROQ WORKING'
        assert captured['url'].endswith('/chat/completions')
        assert captured['payload']['model'] == 'openai/gpt-oss-20b'
        assert captured['payload']['temperature'] == 0.0
        assert captured['payload']['max_tokens'] == 128
        assert captured['headers']['Authorization'] == 'Bearer test-key'
        assert adapter.last_usage['total_tokens'] == 10

    def test_empty_content_raises_with_finish_reason(self, monkeypatch):
        # Reasoning models can exhaust the token budget before any content.
        monkeypatch.setattr(
            monitor, '_post_json_with_retry',
            lambda *a, **k: {'choices': [{'message': {'content': '',
                                                       'reasoning': '...'},
                                          'finish_reason': 'length'}]})
        with pytest.raises(RuntimeError, match="finish reason: length"):
            self.make_adapter().generate("hello")

    def test_reasoning_effort_included_when_set(self, monkeypatch):
        captured = {}

        def fake_post(url, payload, timeout, max_retries, label, headers):
            captured['payload'] = payload
            return {'choices': [{'message': {'content': 'ok'},
                                 'finish_reason': 'stop'}]}

        monkeypatch.setattr(monitor, '_post_json_with_retry', fake_post)
        adapter = OpenAICompatibleAdapter(
            model_id="openai/gpt-oss-20b", api_key="test-key",
            base_url="https://api.groq.com/openai/v1", reasoning_effort="low")
        assert adapter.generate("hello") == 'ok'
        assert captured['payload']['reasoning_effort'] == 'low'

    def test_missing_content_raises(self, monkeypatch):
        monkeypatch.setattr(
            monitor, '_post_json_with_retry',
            lambda *a, **k: {'choices': [{'message': {}}]})
        with pytest.raises(RuntimeError, match="missing the generated text"):
            self.make_adapter().generate("hello")

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="API key is required"):
            OpenAICompatibleAdapter(model_id="openai/gpt-oss-20b",
                                    base_url="https://api.groq.com/openai/v1")

    def test_http_error_propagates_and_trips_breaker(self, monkeypatch):
        def failing_post(*args, **kwargs):
            raise RuntimeError("OpenAI-compatible endpoint returned HTTP 401")

        monkeypatch.setattr(monitor, '_post_json_with_retry', failing_post)
        adapter = self.make_adapter()
        with pytest.raises(RuntimeError, match="HTTP 401"):
            adapter.generate("hello")
        assert adapter.breaker._consecutive_failures == 1

    def test_timeout_propagates_and_trips_breaker(self, monkeypatch):
        def timing_out_post(*args, **kwargs):
            raise RuntimeError("Cannot reach OpenAI-compatible endpoint")

        monkeypatch.setattr(monitor, '_post_json_with_retry', timing_out_post)
        adapter = self.make_adapter()
        with pytest.raises(RuntimeError, match="Cannot reach"):
            adapter.generate("hello")
        assert adapter.breaker._consecutive_failures == 1


class TestGeminiAdapter:
    """Gemini uses the native generateContent endpoint."""

    def make_adapter(self):
        return GeminiAdapter(model_id="gemini-3.1-flash-lite", api_key="test-key")

    def test_normalization_joins_parts(self, monkeypatch):
        captured = {}

        def fake_post(url, payload, timeout, max_retries, label, headers):
            captured['url'] = url
            captured['payload'] = payload
            captured['headers'] = headers
            return {
                'candidates': [{'content': {'parts': [
                    {'text': 'GEMINI'}, {'text': ' WORKING'}]},
                    'finishReason': 'STOP'}],
                'usageMetadata': {'promptTokenCount': 7,
                                  'candidatesTokenCount': 3,
                                  'totalTokenCount': 10},
            }

        monkeypatch.setattr(monitor, '_post_json_with_retry', fake_post)
        adapter = self.make_adapter()
        text = adapter.generate("Say GEMINI WORKING")
        assert text == 'GEMINI WORKING'
        assert '/models/gemini-3.1-flash-lite:generateContent' in captured['url']
        assert captured['payload']['contents'][0]['parts'][0]['text'] == \
            "Say GEMINI WORKING"
        assert captured['payload']['generationConfig']['maxOutputTokens'] == 128
        assert captured['headers']['x-goog-api-key'] == 'test-key'
        assert adapter.last_usage['totalTokenCount'] == 10

    def test_no_candidates_raises(self, monkeypatch):
        monkeypatch.setattr(
            monitor, '_post_json_with_retry', lambda *a, **k: {'candidates': []})
        with pytest.raises(RuntimeError, match="no text"):
            self.make_adapter().generate("hello")

    def test_empty_parts_raises_with_finish_reason(self, monkeypatch):
        monkeypatch.setattr(
            monitor, '_post_json_with_retry',
            lambda *a, **k: {'candidates': [
                {'content': {'parts': []}, 'finishReason': 'SAFETY'}]})
        with pytest.raises(RuntimeError, match="SAFETY"):
            self.make_adapter().generate("hello")

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="API key is required"):
            GeminiAdapter()

    def test_secret_file_folds_into_environment(self, monkeypatch, tmp_path):
        """A Render-style secret file (key name as filename) supplies the key
        when the environment variable is absent."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        secret = tmp_path / "GEMINI_API_KEY"
        secret.write_text("  AIza-test-key-from-secret-file  \n",
                          encoding="utf-8")
        empty = tmp_path / "sub"
        empty.mkdir()
        monitor.load_provider_key_secret_files(
            search_dirs=(str(empty), str(tmp_path)))
        import os
        assert os.environ["GEMINI_API_KEY"] == "AIza-test-key-from-secret-file"

    def test_secret_file_never_overrides_explicit_env(self, monkeypatch, tmp_path):
        """An explicitly set environment variable keeps precedence."""
        monkeypatch.setenv("GEMINI_API_KEY", "explicit-key")
        secret = tmp_path / "GEMINI_API_KEY"
        secret.write_text("file-key", encoding="utf-8")
        monitor.load_provider_key_secret_files(search_dirs=(str(tmp_path),))
        import os
        assert os.environ["GEMINI_API_KEY"] == "explicit-key"

    def test_secret_file_missing_and_empty_are_ignored(self, monkeypatch, tmp_path):
        """A missing file and an empty file both leave the variable unset."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        (tmp_path / "GEMINI_API_KEY").write_text("   \n", encoding="utf-8")
        monitor.load_provider_key_secret_files(search_dirs=(str(tmp_path),))
        import os
        assert not os.environ.get("GEMINI_API_KEY")

    def test_gemini_adapter_uses_secret_file_key(self, monkeypatch, tmp_path):
        """End to end: adapter builds with the key folded in from the file."""
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        secret = tmp_path / "GEMINI_API_KEY"
        secret.write_text("AIza-test-key", encoding="utf-8")
        monitor.load_provider_key_secret_files(search_dirs=(str(tmp_path),))
        adapter = GeminiAdapter()
        assert adapter.api_key == "AIza-test-key"

    def test_api_error_propagates_and_trips_breaker(self, monkeypatch):
        def failing_post(*args, **kwargs):
            raise RuntimeError("Gemini native endpoint returned HTTP 429")

        monkeypatch.setattr(monitor, '_post_json_with_retry', failing_post)
        adapter = self.make_adapter()
        with pytest.raises(RuntimeError, match="HTTP 429"):
            adapter.generate("hello")
        assert adapter.breaker._consecutive_failures == 1


class TestPostHelperUserAgent:
    """The shared POST helper sends a real User-Agent (Cloudflare blocks the
    default Python-urllib agent with HTTP 403 on some hosted endpoints)."""

    def test_user_agent_added_and_not_overridden(self, monkeypatch):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{}'

        def fake_urlopen(request, timeout):
            captured['headers'] = dict(request.headers)
            return FakeResponse()

        monkeypatch.setattr(monitor, 'urlopen', fake_urlopen)
        monitor._post_json_with_retry(
            "https://example.test/v1/x", {}, timeout=5, label="test",
            headers={"Authorization": "Bearer k"})
        assert captured['headers'].get('User-agent') == 'llm-drift-monitor/1.0'
        assert captured['headers'].get('Authorization') == 'Bearer k'


class TestProviderSwitching:
    """The config dispatch selects the right adapter for each target type."""

    def make_monitor_config(self, adapter_type, extra=None):
        return {
            'data': {'db_path': ':memory:'},
            'embeddings': {'model_name': 'sentence-transformers/all-MiniLM-L6-v2',
                           'device': 'cpu'},
            'monitoring': {'random_seed': 42, 'batch_size': 10},
            'reference': {'window_size': 50},
            'alerts': {},
            'target': {'type': adapter_type, **(extra or {})},
        }

    def test_gemini_type_builds_native_adapter(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.delenv("GEMINI_MODEL", raising=False)
        m = LLMDriftMonitor.__new__(LLMDriftMonitor)
        m.config = self.make_monitor_config('gemini')
        m.random_seed = 42
        adapter = m._create_adapter()
        assert isinstance(adapter, GeminiAdapter)
        assert adapter.model_id == 'gemini-3.1-flash-lite'
        assert adapter.base_url == \
            'https://generativelanguage.googleapis.com/v1beta'

    def test_gemini_config_model_wins_over_environment(self, monkeypatch):
        # A stale GEMINI_MODEL in an .env must not silently point the hosted
        # demo at a different model and quota tier than its config (and the
        # collected baseline) name.
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
        m = LLMDriftMonitor.__new__(LLMDriftMonitor)
        m.config = self.make_monitor_config(
            'gemini', extra={'model_id': 'gemini-3.1-flash-lite'})
        m.random_seed = 42
        adapter = m._create_adapter()
        assert adapter.model_id == 'gemini-3.1-flash-lite'

    def test_openai_compatible_with_groq_key(self, monkeypatch):
        monkeypatch.setenv("GROQ_API_KEY", "test-key")
        monkeypatch.delenv("GROQ_MODEL", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        m = LLMDriftMonitor.__new__(LLMDriftMonitor)
        m.config = self.make_monitor_config('openai-compatible')
        m.random_seed = 42
        adapter = m._create_adapter()
        assert isinstance(adapter, OpenAICompatibleAdapter)
        assert adapter.base_url == 'https://api.groq.com/openai/v1'

    def test_fake_type_still_works(self, monkeypatch):
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        m = LLMDriftMonitor.__new__(LLMDriftMonitor)
        m.config = self.make_monitor_config('fake')
        m.random_seed = 42
        adapter = m._create_adapter()
        assert isinstance(adapter, FakeLLMAdapter)

    def test_local_type_still_works(self, monkeypatch):
        m = LLMDriftMonitor.__new__(LLMDriftMonitor)
        m.config = self.make_monitor_config(
            'local', {'model_id': 'llama3.2:1b',
                      'base_url': 'http://localhost:11434'})
        m.random_seed = 42
        adapter = m._create_adapter()
        assert isinstance(adapter, LocalLLMAdapter)
        assert adapter.base_url == 'http://localhost:11434'


class TestExistingAdaptersUnchanged:
    """Fake and Ollama behavior must be untouched by the provider additions."""

    def test_fake_adapter_generate(self):
        adapter = FakeLLMAdapter(seed=42)
        text = adapter.generate("What is drift?")
        assert isinstance(text, str) and len(text) > 0
        # Deterministic per seed and completely order independent: generation
        # derives from hashes of the seed and prompt only, with no shared
        # generator whose state the concurrent probe pool would race over.
        assert FakeLLMAdapter(seed=42).generate("What is drift?") == text
        # The same prompt produces the same answer no matter what other
        # prompts were generated before it, on the same or a fresh adapter,
        # so interleaved calls cannot change any prompt's answer.
        for _ in range(3):
            adapter.generate("Completely different prompt")
        assert adapter.generate("What is drift?") == text
        # Seeded variation: different seeds must differ for some prompts,
        # proving the variation branch is exercised by construction rather
        # than by scheduling luck.
        seeds_disagree = any(
            FakeLLMAdapter(seed=42).generate(f"probe {probe}")
            != FakeLLMAdapter(seed=7).generate(f"probe {probe}")
            for probe in range(200))
        assert seeds_disagree, "seeded variation should differ for some prompts"

    def test_local_adapter_payload_shape(self, monkeypatch):
        captured = {}

        def fake_post(url, payload, timeout, max_retries, label, headers):
            captured['url'] = url
            captured['payload'] = payload
            return {'response': 'ok'}

        monkeypatch.setattr(monitor, '_post_json_with_retry', fake_post)
        adapter = LocalLLMAdapter(model_id='llama3.2:1b',
                                  base_url='http://localhost:11434')
        assert adapter.generate("hello") == 'ok'
        assert captured['url'].endswith('/api/generate')
        assert captured['payload']['options']['num_predict'] == adapter.max_tokens