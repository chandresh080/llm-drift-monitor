import os
from types import SimpleNamespace

import httpx
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

st.set_page_config(
    page_title="LLM Drift Monitor",
    page_icon="📊",
    layout="wide"
)

@st.cache_resource
def load_api():
    """Return a cached data source backed by the LLM drift monitor API."""
    client = httpx.Client(
        base_url=os.environ.get("DRIFT_MONITOR_API_URL", "http://localhost:8000"),
        timeout=10.0,
    )

    def get_all_runs(limit=None):
        params = {"limit": limit} if limit else None
        resp = client.get("/runs", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_all_alerts(limit=None):
        params = {"limit": limit} if limit else None
        resp = client.get("/alerts", params=params)
        resp.raise_for_status()
        return resp.json()

    def get_all_test_results(limit=None):
        params = {"limit": limit} if limit else None
        resp = client.get("/drift/results", params=params)
        resp.raise_for_status()
        return resp.json()

    def count_successful_runs():
        runs = get_all_runs(limit=1000)
        return sum(1 for r in runs if str(r.get("run_status")) == "success")

    return SimpleNamespace(
        get_all_runs=get_all_runs,
        get_all_alerts=get_all_alerts,
        get_all_test_results=get_all_test_results,
        count_successful_runs=count_successful_runs,
    )

def main():
    st.title("📊 LLM Drift Monitor")
    st.markdown("Canary-based behavioral drift signals; quality impact requires separate validation.")
    
    data = load_api()
    
    # Sidebar
    st.sidebar.header("Navigation")
    page = st.sidebar.selectbox(
        "Select page",
        ["Overview", "Run History", "Drift Analysis", "Alerts"]
    )
    
    if page == "Overview":
        show_overview(data)
    elif page == "Run History":
        show_run_history(data)
    elif page == "Drift Analysis":
        show_drift_analysis(data)
    elif page == "Alerts":
        show_alerts(data)

def show_overview(data):
    st.header("Overview")
    
    col1, col2, col3, col4 = st.columns(4)
    
    total_runs = data.count_successful_runs()
    col1.metric("Successful Runs", total_runs)
    
    test_results = data.get_all_test_results(limit=10)
    if test_results:
        recent_drift = sum(1 for tr in test_results if tr['overall_drift_detected'])
        col2.metric("Recent Drift Detections", recent_drift)
        
        recent_quality = sum(1 for tr in test_results if tr['overall_quality_degradation'])
        col3.metric("Recent Invalid-Response Flags", recent_quality)
    else:
        col2.metric("Recent Drift Detections", 0)
        col3.metric("Recent Invalid-Response Flags", 0)
    
    alerts = data.get_all_alerts(limit=10)
    col4.metric("Recent Alerts", len(alerts))
    
    st.markdown("---")
    
    # Recent test results
    if test_results:
        st.subheader("Recent Test Results")
        df = pd.DataFrame(test_results)
        display_cols = ['timestamp', 'reference_size', 'current_size', 
                       'overall_drift_detected', 'overall_quality_degradation',
                       'ks_significant', 'proportion_z_significant', 'mmd_significant',
                       'bh_significant_tests']
        st.dataframe(df[display_cols], use_container_width=True)
    
    # Notes on limitations
    st.markdown("---")
    st.warning("""
    **Limitations:** Without labeled ground truth, this system detects behavioral drift, not guaranteed quality degradation. 
    Changes in response length, validity signals, or embeddings may reflect harmless variations.
    """)

def show_run_history(data):
    st.header("Run History")
    
    runs = data.get_all_runs(limit=100)
    if not runs:
        st.info("No runs yet. Run monitoring cycle first.")
        return
    
    df = pd.DataFrame(runs)
    display_cols = ['id', 'timestamp', 'prompt_version', 'model_id', 
                   'response_length', 'is_valid', 'run_status', 'adapter_type']
    st.dataframe(df[display_cols], use_container_width=True)
    
    # Simple visualization
    if len(runs) > 1:
        st.subheader("Response Length Over Time")
        df['timestamp_dt'] = pd.to_datetime(df['timestamp'])
        fig = px.line(df, x='timestamp_dt', y='response_length', 
                     title='Response Length Trend')
        st.plotly_chart(fig, use_container_width=True)

def show_drift_analysis(data):
    st.header("Drift Analysis")
    
    test_results = data.get_all_test_results(limit=20)
    if not test_results:
        st.info("No test results yet. Run monitoring cycle first.")
        return
    
    df = pd.DataFrame(test_results)
    display_cols = ['timestamp', 'ks_statistic', 'ks_pvalue', 'ks_adjusted_pvalue', 'ks_significant',
                   'proportion_z_stat', 'proportion_z_pvalue', 'proportion_z_adjusted_pvalue', 'proportion_z_significant',
                   'mmd_statistic', 'mmd_pvalue', 'mmd_adjusted_pvalue', 'mmd_significant',
                   'bh_significant_tests']
    st.dataframe(df[display_cols], use_container_width=True)
    
    # Visualize p-values over time
    if len(df) > 1:
        st.subheader("Adjusted P-values Over Time")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['ks_adjusted_pvalue'], 
                               name='KS (Adjusted)', mode='lines+markers'))
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['proportion_z_adjusted_pvalue'], 
                               name='Proportion Z (Adjusted)', mode='lines+markers'))
        fig.add_trace(go.Scatter(x=df['timestamp'], y=df['mmd_adjusted_pvalue'], 
                               name='MMD (Adjusted)', mode='lines+markers'))
        fig.add_hline(y=0.05, line_dash="dash", line_color="red", annotation_text="α=0.05")
        fig.update_layout(yaxis_title="Adjusted p-value", xaxis_title="Timestamp")
        st.plotly_chart(fig, use_container_width=True)

def show_alerts(data):
    st.header("Alerts")
    
    alerts = data.get_all_alerts(limit=100)
    if not alerts:
        st.info("No alerts yet.")
        return
    
    df = pd.DataFrame(alerts)
    display_cols = ['timestamp', 'alert_type', 'severity', 'message',
                   'overall_drift_detected', 'overall_quality_degradation']
    st.dataframe(df[display_cols], use_container_width=True)

if __name__ == "__main__":
    main()
