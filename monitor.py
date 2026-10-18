import json
import yaml
import os
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
import random

from storage import DriftMonitorStorage
from embeddings import EmbeddingExtractor
from drift_tests import ks_test, two_proportion_z_test, mmd_test, benjamini_hochberg_correction

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
            f"Based on the input, the answer involves considering multiple factors.",
            f"This is a simulated response for testing purposes.",
        ]
        # Pick response based on hash of prompt for consistency
        idx = hash(prompt) % len(base_responses)
        response = base_responses[idx]
        
        # Add slight variation
        if self.rng.random() < 0.1:  # 10% chance of truncation/variation
            response = response[:len(response)//2] + "..."
        
        return response
    
    def generate_batch(self, prompts: List[str]) -> List[str]:
        """Generate responses for multiple prompts."""
        return [self.generate(p) for p in prompts]

class LocalLLMAdapter:
    """Placeholder for local LLM adapter. Not implemented to keep things minimal."""
    
    def __init__(self, model_id: str = "local-model"):
        self.model_id = model_id
        raise NotImplementedError("Local LLM adapter not implemented in minimal version")

class OpenAICompatibleAdapter:
    """Placeholder for OpenAI-compatible adapter. Not implemented."""
    
    def __init__(self, model_id: str = "gpt-3.5-turbo"):
        self.model_id = model_id
        raise NotImplementedError("OpenAI-compatible adapter not implemented (no paid APIs allowed)")

class LLMDriftMonitor:
    """Main monitoring pipeline."""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
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
        elif adapter_type == 'local':
            return LocalLLMAdapter(model_id=target_cfg.get('model_id', 'local-model'))
        elif adapter_type == 'openai-compatible':
            return OpenAICompatibleAdapter(model_id=target_cfg.get('model_id', 'gpt-3.5-turbo'))
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
        
        batch_size = self.config['monitoring']['batch_size']
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
            run_id = self.storage.insert_probe_run(
                timestamp=timestamp if i == 0 else f"{timestamp}-{i}",  # Make unique if same ts
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
        
        ref_size = self.config['reference']['window_size']
        successful_runs = self.storage.count_successful_runs()
        
        if successful_runs < min_runs:
            return False
        
        # Reference is stored implicitly by taking first N successful runs
        # The storage.get_reference_runs() handles this
        return True
    
    def has_sufficient_reference(self) -> bool:
        """Check if we have sufficient data for reference."""
        min_runs = self.config['reference']['min_runs']
        successful_runs = self.storage.count_successful_runs()
        return successful_runs >= min_runs
    
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
        # Make windows disjoint to avoid overlap
        ref_runs = self.storage.get_reference_runs(self.config['reference']['window_size'], exclude_run_ids=[])
        ref_run_ids = self.storage.run_ids_from_runs(ref_runs)
        cur_runs = self.storage.get_current_window_runs(
            self.config['monitoring']['batch_size'], 
            exclude_run_ids=ref_run_ids
        )
        cur_run_ids = self.storage.run_ids_from_runs(cur_runs)
        
        # If current window is empty due to overlap, just take recent without exclusion
        if len(cur_runs) == 0:
            cur_runs = self.storage.get_current_window_runs(self.config['monitoring']['batch_size'])
            cur_run_ids = self.storage.run_ids_from_runs(cur_runs)
            # Re-fetch ref without overlapping current
            all_cur_ids = set(cur_run_ids)
            ref_runs = [r for r in ref_runs if r['id'] not in all_cur_ids]
            ref_run_ids = self.storage.run_ids_from_runs(ref_runs)
        
        # Extract features
        ref_lengths = self.storage.get_response_lengths_for_runs(ref_run_ids)
        cur_lengths = self.storage.get_response_lengths_for_runs(cur_run_ids)
        
        ref_validity = self.storage.get_validity_for_runs(ref_run_ids, include_failed=True)
        cur_validity = self.storage.get_validity_for_runs(cur_run_ids, include_failed=True)
        
        # Extract embeddings
        ref_embeddings = self.storage.get_embeddings_for_runs(ref_run_ids)
        cur_embeddings = self.storage.get_embeddings_for_runs(cur_run_ids)
        
        timestamp = datetime.now().isoformat()
        
        # Run statistical tests
        results = {
            'timestamp': timestamp,
            'reference_size': len(ref_runs),
            'current_size': len(cur_runs),
        }
        
        # KS test on response lengths
        ks_stat, ks_pval = ks_test(ref_lengths, cur_lengths)
        results['ks_statistic'] = ks_stat
        results['ks_pvalue'] = ks_pval
        results['ks_raw_pvalue'] = ks_pval
        
        # Two-proportion z-test on validity
        prop_z_stat, prop_z_pval = two_proportion_z_test(ref_validity, cur_validity)
        results['proportion_z_stat'] = prop_z_stat
        results['proportion_z_pvalue'] = prop_z_pval
        results['proportion_z_raw_pvalue'] = prop_z_pval
        
        # MMD test on embeddings
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
        adjusted_pvals, significant_flags = benjamini_hochberg_correction(pvalues, alpha=0.05)
        
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
                'message': 'Demonstrated quality degradation: high proportion of invalid responses',
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
