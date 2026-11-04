# LLM Drift Monitor: Colab (Free Tier)

Run the drift monitor against a free-tier OpenAI-compatible LLM from Google Colab.

## Files in this folder

- `drift_monitor_colab.ipynb`: the notebook to upload/run in Colab
- `config.yaml`: runtime config for the Colab environment

## What to upload to Colab

In a new Colab notebook, upload the following files into `/content` (Colab file browser or `files.upload()`):

- `monitor.py`, `storage.py`, `embeddings.py`, `drift_tests.py` (from the repo root)
- `canary_prompts.json` (from the repo root)
- `config.yaml` (from this `colab/` folder)

## Steps

1. Open [Google Colab](https://colab.research.google.com/) and create a new notebook.
2. Upload the files listed above into `/content`.
3. Run all cells top-to-bottom.
4. In the setup cell, paste your free API key and adjust `OPENAI_BASE_URL` / `OPENAI_MODEL` if needed.
5. Watch the monitoring cycle run; results are stored in `/content/drift_monitor.db`.

## Free-tier notes

- Works with Gemini (e.g. `gemini-2.0-flash` via `https://generativelanguage.googleapis.com/v1beta/openai/`) or Grok (e.g. `grok-2` via `https://api.x.ai/v1`).
- Free-tier API rate limits apply, keep probe batch sizes modest.
- The first embedding run downloads `sentence-transformers/all-MiniLM-L6-v2` automatically.
- The monitoring cycle may report insufficient reference data on a fresh database; run multiple cycles to build the reference window.
