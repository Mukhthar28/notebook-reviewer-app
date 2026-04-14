import streamlit as st
import os
import tempfile
from pathlib import Path
from validator import run_full_review, ask_ai, Finding

st.set_page_config(page_title="Notebook Code Reviewer", page_icon="🔍", layout="wide")

st.title("🔍 Notebook Code Reviewer")
st.caption("Upload a Fabric notebook (.py) or Jupyter notebook (.ipynb) to get a full code review.")

# ── API key config (hidden — loaded from secrets/env only) ────────────────────
def _load_api_key():
    try:
        return st.secrets["GROQ_API_KEY"]
    except (KeyError, FileNotFoundError):
        return os.environ.get("GROQ_API_KEY", "")

_api_key = _load_api_key()
if _api_key:
    os.environ["GROQ_API_KEY"] = _api_key

with st.sidebar:
    st.header("⚙️ Configuration")
    if _api_key:
        st.success("API key loaded ✓")
    else:
        st.error("API key not configured. Set GROQ_API_KEY in Streamlit secrets or environment.")

    groq_model = st.selectbox(
        "Agent",
        ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "mixtral-8x7b-32768"],
        index=0,
    )
    os.environ["GROQ_MODEL"] = groq_model

    st.divider()
    st.markdown("**Supported files:**")
    st.markdown("- `notebook-content.py` (Fabric)")
    st.markdown("- `.ipynb` (Jupyter)")

# ── File upload ───────────────────────────────────────────────────────────────
uploaded_file = st.file_uploader(
    "Choose a notebook file",
    type=["py", "ipynb"],
    help="Upload a Fabric notebook-content.py or a Jupyter .ipynb file",
)

# ── Or browse workspace notebooks ────────────────────────────────────────────
with st.expander("Or select from workspace"):
    workspace_root = st.text_input(
        "Workspace root path",
        value=os.environ.get("NOTEBOOK_WORKSPACE", ""),
        placeholder=r"e.g. C:\GITLAB\V1BATSHRDEV\BIS",
    )
    notebook_files = []
    if workspace_root and os.path.isdir(workspace_root):
        root = Path(workspace_root)
        notebook_files = sorted(
            [str(p) for p in root.rglob("notebook-content.py")]
            + [str(p) for p in root.rglob("*.ipynb")]
        )
    selected_path = st.selectbox(
        "Available notebooks",
        [""] + notebook_files,
        format_func=lambda x: x if x else "— select —",
    )

# ── Run review ────────────────────────────────────────────────────────────────
file_path = None

if uploaded_file is not None:
    suffix = ".py" if uploaded_file.name.endswith(".py") else ".ipynb"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded_file.getvalue())
    tmp.close()
    file_path = tmp.name
    nb_display_name = uploaded_file.name
elif selected_path:
    file_path = selected_path
    nb_display_name = os.path.basename(os.path.dirname(file_path)) or os.path.basename(file_path)

if file_path and st.button("🚀 Run Review", type="primary"):
    if not os.environ.get("GROQ_API_KEY"):
        st.error("Please enter your Groq API key in the sidebar.")
    else:
        with st.spinner("Reviewing notebook…"):
            nb_name, findings = run_full_review(file_path)

        st.session_state["nb_name"] = nb_name
        st.session_state["findings"] = findings
        st.session_state["chat_history"] = []

# ── Display results ───────────────────────────────────────────────────────────
if "findings" in st.session_state:
    findings: list[Finding] = st.session_state["findings"]
    nb_name = st.session_state["nb_name"]

    st.divider()
    st.subheader(f"Results for: {nb_name}")

    critical = [f for f in findings if f.severity == "critical"]
    warnings = [f for f in findings if f.severity == "warning"]
    suggestions = [f for f in findings if f.severity == "suggestion"]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total", len(findings))
    col2.metric("🔴 Critical", len(critical))
    col3.metric("🟡 Warnings", len(warnings))
    col4.metric("🔵 Suggestions", len(suggestions))

    # Severity filter
    severity_filter = st.multiselect(
        "Filter by severity",
        ["critical", "warning", "suggestion"],
        default=["critical", "warning", "suggestion"],
    )

    filtered = [f for f in findings if f.severity in severity_filter]

    for f in filtered:
        icon = {"critical": "🔴", "warning": "🟡", "suggestion": "🔵"}.get(f.severity, "⚪")
        cell_info = f" (Cell {f.cell})" if f.cell else ""
        with st.expander(f"{icon} [{f.severity.upper()}] {f.area}{cell_info}"):
            st.markdown(f"**Issue:** {f.issue}")
            st.markdown(f"**Fix:** {f.fix}")

    # ── Chat with AI about findings ───────────────────────────────────────────
    st.divider()
    st.subheader("💬 Ask about the findings")

    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    for msg in st.session_state["chat_history"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if question := st.chat_input("Ask a question about the review findings…"):
        st.session_state["chat_history"].append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            with st.spinner("Thinking…"):
                answer = ask_ai(
                    question, nb_name, findings, st.session_state["chat_history"]
                )
            st.markdown(answer)

        st.session_state["chat_history"].append({"role": "assistant", "content": answer})
