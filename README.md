# LLM Drift Monitor

A local prototype for monitoring LLM behavioral drift. It runs canary prompts against a configurable target, computes sentence embeddings locally, applies statistical distribution tests, and stores results in SQLite for a Streamlit dashboard. Drift is a signal for investigation; it does not establish quality degradation without task-specific validation.

> **Note on historical claims:** The original implementation referenced "97% live accuracy against Llama-3.3-70B" - this claim has not been reproduced, verified, or quantified in this reconstruction. The definition of "accuracy", dataset used, or evaluation methodology cannot be reconstructed from the available information.

## Key Features

- **Canary probe pipeline**: Runs a fixed, versioned set of prompts per monitoring cycle. Schedule `python main.py --run` with your OS scheduler for daily execution.
- **Local embedding extraction**: Uses `sentence-transformers` to compute embeddings entirely offline (after initial model download)
- **Statistical drift detection**: Combines Kolmogorov-Smirnov (KS), two-proportion z-test, and Maximum Mean Discrepancy (MMD) with Benjamini–Hochberg correction
- **No ground truth required**: Detects behavioral drift that latency and error-rate monitoring miss
- **Reference window tracking**: Uses a fixed reference dataset for comparison
- **Invalid-response alerts**: Tracks a narrow empty-response heuristic separately from behavioral drift; this is not a general quality score
- **Local persistence**: SQLite database for all runs, features, and alerts
- **Interactive dashboard**: Streamlit UI for visualizing drift patterns

## Limitations

- **No ground truth**: Without labeled data, we can detect behavioral drift but cannot definitively measure quality degradation. A shift in responses doesn't necessarily mean worse quality - it could mean different valid responses.
- **Feature dependence**: Drift detection relies on response length and embedding distributions. Subtle semantic changes may not be captured, or length changes may reflect harmless verbosity differences.
- **Validity signal**: The binary validity signal (empty/invalid responses) is a coarse proxy. It may miss nuanced quality issues.
- **Reference stability**: The reference window must be stable and representative. Poor reference data can lead to false positives.

## Installation (WSL Ubuntu on Windows)

The default target is Meta Llama 3.2 1B through a local Ollama server. This avoids hosted inference charges, but it is not Llama 3.3 70B and does not validate the historical 97% claim.

1. Install Ollama for your operating system, start its local server, and download the model:
```bash
ollama pull llama3.2:1b
```

2. Clone/create this repository in WSL:
```bash
cd /home/cheenu/sandbox/llm-drift-monitor
```

3. Create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

4. Install dependencies:
```bash
pip install -r requirements.txt
```

5. Download the embedding model (first run will download automatically, but you can pre-cache):
```bash
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"
```

## Usage

### Run monitoring cycle

```bash
python main.py --run
```

### Launch dashboard

```bash
python main.py --dashboard
# Or directly: streamlit run dashboard.py
```

### Run tests

```bash
pytest -v
```

## Architecture

```
.
├── canary_prompts.json  # Versioned fixed prompts
├── config.yaml          # Configuration
├── drift_tests.py       # Statistical tests (KS, z-test, MMD, BH correction)
├── embeddings.py        # Sentence embedding extraction and features
├── storage.py           # SQLite persistence
├── monitor.py           # Core monitoring pipeline
├── dashboard.py         # Streamlit dashboard
├── main.py              # CLI entry point
└── tests/
    └── test_drift_tests.py  # Unit tests
```

## Testing Strategy

The deterministic fake adapter is intended for tests. The default runtime target is local Ollama; after the model and sentence-embedding weights are cached, inference can run without a paid API. Test and real-model runs should be kept separate in the database.

## Unit of Sampling and Statistical Independence

A critical consideration in monitoring LLM behavioral drift is defining the **unit of sampling**:

1. **Non-Independence of Repeated Probes**: Responses generated from the exact same prompt share semantic context, stylistic constraints, and prompt-conditioned priors. Treating repeated outputs from a tiny prompt set (e.g. 10 static prompts) as independent and identically distributed (i.i.d.) observations introduces clustering and artificially inflates degrees of freedom, leading to miscalibrated test statistics.
2. **Stratified Canary Prompt Design**: To establish meaningful behavioral coverage, the monitor uses 40 canary prompts versioned in [`canary_prompts.json`](file:///home/cheenu/sandbox/llm-drift-monitor/canary_prompts.json) across five functional categories:
   - **Factual**: Direct knowledge retrieval and explanatory fidelity.
   - **Reasoning**: Multi-step deductive, arithmetic, and logic puzzles.
   - **Code**: Algorithmic implementation, complexity analysis, and query syntax.
   - **Refusal**: Safety boundary evaluation and refusal trigger compliance.
   - **Formatting**: Structured outputs with hard length, markdown, XML, or JSON schemas.
3. **Sampling Strategy**: Responses are sampled per prompt at temperature $T > 0$ to capture generation variance (aleatoric uncertainty), while prompt-level stratification isolates domain-specific shifts. Two-sample statistical tests (KS, z-test, MMD) are evaluated across windows of prompt-response pairs, with per-category localization tests adjusting for multiple comparisons via Benjamini–Hochberg.

## Phase 1 Synthetic Benchmark Suite

The `experiments/` suite benchmarks detector sensitivity, false positive rates, detection delay, and category localization using controlled synthetic distribution shifts:

```bash
# 1. Calibration: False-positive rates under the null (alpha=0.05) & p-value uniformity
python -m experiments.calibration --seed 42 --n-sims 1000

# 2. Power Analysis: Power vs shift size (delta) across sample sizes N
python -m experiments.power --seed 42

# 3. Detection Delay: 90-day sequential horizon comparing Single-window, k-of-n, and CUSUM
python -m experiments.delay --seed 42

# 4. Localization: Shift isolation to specific categories with Benjamini-Hochberg FDR control
python -m experiments.localization --seed 42

# 5. Sensitivity: Model dimension (MiniLM vs MPNet), kernel bandwidths, & permutation counts
python -m experiments.sensitivity --seed 42
```

Benchmark artifacts (CSVs and PNG plots) are written directly to `results/`.

## Configuration Notes

- Set `target.type: "fake"` for deterministic tests, or `target.type: "local"` to use the local Ollama model. `model_id`, `base_url`, timeout, temperature, and max tokens are configurable.
- Reference window size controls how many runs constitute the "normal" baseline
- MMD uses permutation tests with a fixed seed for reproducibility
- Benjamini–Hochberg correction is applied to the three predeclared daily p-values
