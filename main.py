#!/usr/bin/env python3
"""
LLM Drift Monitor - Main CLI entry point
"""

import argparse
import sys
from datetime import datetime
from monitor import LLMDriftMonitor

def main():
    parser = argparse.ArgumentParser(description="LLM Drift Monitor")
    parser.add_argument("--run", action="store_true", help="Run monitoring cycle")
    parser.add_argument("--dashboard", action="store_true", help="Launch Streamlit dashboard")
    parser.add_argument("--num-probes", type=int, default=None, help="Number of probes to run")
    parser.add_argument("--create-ref", action="store_true", help="Create reference window")
    args = parser.parse_args()
    
    if not args.run and not args.dashboard and not args.create_ref:
        parser.print_help()
        return
    
    if args.run:
        print(f"[{datetime.now()}] Starting monitoring cycle...")
        monitor = LLMDriftMonitor()
        result = monitor.run_monitoring_cycle(num_probes=args.num_probes)
        if result['tests_run']:
            print("✓ Monitoring cycle completed")
            print(f"  Runs: {len(result['run_ids'])}")
            print(f"  Drift detected: {result['results'].get('overall_drift_detected', 0)}")
            print(f"  Quality degradation: {result['results'].get('overall_quality_degradation', 0)}")
            print(f"  Significant tests: {result['results'].get('bh_significant_tests', [])}")
        else:
            print(f"⚠ Monitoring cycle completed with note: {result.get('reason')}")
            print(f"  Runs: {len(result['run_ids'])}")
            print("  Need more data for reference window")
    
    if args.create_ref:
        print("Creating reference window...")
        monitor = LLMDriftMonitor()
        success = monitor.create_reference_window()
        if success:
            print("✓ Reference window ready")
        else:
            print("✗ Insufficient data for reference window")
    
    if args.dashboard:
        print("Launching Streamlit dashboard...")
        import subprocess
        subprocess.run([sys.executable, "-m", "streamlit", "run", "dashboard.py"])

if __name__ == "__main__":
    main()
