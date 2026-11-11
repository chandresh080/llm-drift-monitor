"""Build the compact cross-provider comparison artifact for the dashboard.

Reads the recorded real-provider experiment outputs (never re-runs any API
call) and derives every number shown in the evaluation tab's comparison
section from them. The output is a committed data artifact:
web/provider_comparison.json, fetched by the frontend as a static asset.

This comparison is cross-provider and cross-model by design. It is surfaced
as descriptive evidence that the adapters and the pipeline handle two real
hosted providers, explicitly not as a drift result: a shift between Groq's
gpt-oss-20b and Google's gemini-3.1-flash-lite could never be attributed to
provider behavior, and the monitor's drift verdicts live only in the
Gemini-vs-Gemini hosted demo.
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = [
    os.path.join(REPO, "results", "real_provider_results.json"),
    os.path.join(REPO, "results", "real_provider_extended_results.json"),
]
OUT = os.path.join(REPO, "web", "provider_comparison.json")


def usage_tokens(usage: dict):
    """Providers report usage in different schemas; take the total."""
    if not usage:
        return None
    for key in ("total_tokens", "totalTokenCount"):
        if key in usage:
            return usage[key]
    return None


def cycle_row(entry: dict, stage: str) -> dict:
    results = entry.get("results") or {}
    return {
        "cycle": entry["cycle"],
        "stage": stage,
        "ok_runs": entry.get("ok_runs"),
        "failed_runs": entry.get("failed_runs"),
        "latency_p50_s": entry.get("gen_latency_p50_s"),
        "latency_p95_s": entry.get("gen_latency_p95_s"),
        "latency_mean_s": entry.get("gen_latency_mean_s"),
        "tokens_total": usage_tokens(entry.get("usage")),
        "validity_rate": (results.get("cur_valid_count")
                          / results["current_size"]) if results.get("current_size") else None,
        "mean_length": results.get("cur_mean_length"),
    }


def main() -> None:
    single = json.load(open(SOURCES[0]))
    extended = json.load(open(SOURCES[1]))
    setup = single["setup"]

    groq_cycles = [cycle_row(c, "groq_reference")
                   for c in single["reference_cycles"]]
    # The comparison run's Gemini cycle and the five extension cycles are one
    # continuous current stage over the same reference window.
    gemini_cycles = ([cycle_row(c, "gemini_current")
                      for c in single["current_cycles"]]
                     + [cycle_row(c, "gemini_current")
                        for c in extended["current_cycles"]])

    # Detector verdicts: the single cycle comparison ran the full family once
    # with persistence overridden to one cycle; the extension kept the default.
    single_current = single["current_cycles"][0]["results"]
    ext_current = extended["current_cycles"][-1]["results"]
    families = {
        "single_cycle": {
            "ks_pvalue": single_current.get("ks_pvalue"),
            "mmd_pvalue": single_current.get("mmd_pvalue"),
            "proportion_z_pvalue": single_current.get("proportion_z_pvalue"),
            "paired_mmd_pvalue": single_current.get("paired_cluster_mmd_pvalue"),
            "paired_prompt_pairs": single_current.get("paired_cluster_prompt_pairs"),
            "any_significant": any(single_current.get(k) == 1 for k in
                                   ("ks_significant", "mmd_significant",
                                    "proportion_z_significant")),
            "alerts": single["current_cycles"][0].get("alerts", []),
        },
        "extended_final": {
            "ks_pvalue": ext_current.get("ks_pvalue"),
            "mmd_pvalue": ext_current.get("mmd_pvalue"),
            "proportion_z_pvalue": ext_current.get("proportion_z_pvalue"),
            "paired_mmd_pvalue": ext_current.get("paired_cluster_mmd_pvalue"),
            "paired_prompt_pairs": ext_current.get("paired_cluster_prompt_pairs"),
            "any_significant": any(ext_current.get(k) == 1 for k in
                                   ("ks_significant", "mmd_significant",
                                    "proportion_z_significant")),
            "alerts": extended["current_cycles"][-1].get("alerts", []),
        },
    }

    payload = {
        "generated_by": "scripts/export_provider_comparison.py",
        "source_artifacts": [os.path.relpath(s, REPO) for s in SOURCES],
        "collected_on": setup.get("note") and "2026-11-08",
        "reference": {
            "provider": setup["reference_provider"],
            "model": setup["reference_model"],
            "cycles": groq_cycles,
            "reasoning_effort": setup.get("groq_reasoning_effort"),
        },
        "current": {
            "provider": setup["current_provider"],
            "model": setup["current_model"],
            "cycles": gemini_cycles,
        },
        "shared": {
            "prompt_panel_version": setup["prompt_panel_version"],
            "prompts_per_cycle": setup["prompts_per_cycle"],
            "embedding_model": setup["embedding_model"],
            "temperature": setup["generation_settings"]["temperature"],
            "max_tokens": setup["generation_settings"]["max_tokens"],
            "uncontrolled_differences": setup.get("uncontrolled_differences", []),
        },
        "families": families,
        "caveat": ("Cross-provider, cross-model comparison. Different models "
                   "served by different providers; a detected shift could not "
                   "be attributed exclusively to provider behavior, and no "
                   "drift alert fired here. The hosted demo's drift verdicts "
                   "compare Gemini against Gemini only."),
    }
    with open(OUT, "w") as f:
        json.dump(payload, f, indent=1, sort_keys=True)
        f.write("\n")
    print(f"wrote {os.path.relpath(OUT, REPO)} "
          f"({len(groq_cycles)} Groq cycles, {len(gemini_cycles)} Gemini cycles)")
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        assert groq_cycles and gemini_cycles
        assert families["single_cycle"]["any_significant"] is False
        assert families["extended_final"]["any_significant"] is False


if __name__ == "__main__":
    main()
