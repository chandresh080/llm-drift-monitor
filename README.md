# LLM Drift Monitor

A minimal, fully free, end-to-end monitoring system that detects silent quality degradation in deployed LLMs without labelled ground truth. The system uses canary probe testing, local sentence embeddings, statistical drift tests, and a Streamlit dashboard.

> **Note on historical claims:** The original implementation referenced "97% live accuracy against Llama-3.3-70B" - this claim has not been reproduced, verified, or quantified in this reconstruction. The definition of "accuracy", dataset used, or evaluation methodology cannot be reconstructed from the available information.

## Key Features

- **Canary probe pipeline**: Runs a fixed, versioned set of prompts daily against a target LLM
- **Local embedding extraction**: Uses `sentence-transformers` to compute embeddings entirely offline (after initial model download)
- **Statistical drift detection**: Combines Kolmogorov-Smirnov (KS), two-proportion z-test, and Maximum Mean Discrepancy (MMD) with Benjamini–Hochberg correction
- **No ground truth required**: Detects behavioral drift that latency and error-rate monitoring miss
- **Reference window tracking**: Uses a fixed reference dataset for comparison
- **Quality regression alerts**: Distinguishes statistical drift from demonstrated quality regressions
- **Local persistence**: SQLite database for all runs, features, and alerts
- **Interactive dashboard**: Streamlit UI for visualizing drift patterns

## Limitations

- **No ground truth**: Without labeled data, we can detect behavioral drift but cannot definitively measure quality degradation. A shift in responses doesn't necessarily mean worse quality - it could mean different valid responses.
- **Feature dependence**: Drift detection relies on response length and embedding distributions. Subtle semantic changes may not be captured, or length changes may reflect harmless verbosity differences.
- **Validity signal**: The binary validity signal (empty/invalid responses) is a coarse proxy. It may miss nuanced quality issues.
- **Reference stability**: The reference window must be stable and representative. Poor reference data can lead to false positives.

## Installation (WSL Ubuntu on Windows)

1. Clone/create this repository in WSL:
```bash
cd /home/cheenu/sandbox/llm-drift-monitor
```

2. Create a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Download the embedding model (first run will download automatically, but you can pre-cache):
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

The system uses a **fake LLM adapter** for all tests and demos. This makes it completely testable offline without any paid APIs. A real LLM integration is optional and not required.

## Configuration Notes

- Set `target.type: "fake"` for testing/offline use (default)
- Reference window size controls how many runs constitute the "normal" baseline
- MMD uses permutation tests with a fixed seed for reproducibility
- Benjamini–Hochberg correction is applied to the three predeclared daily p-values
