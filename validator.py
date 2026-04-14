# =============================================================================
# CORE VALIDATION LOGIC — extracted from CodeReviewer.Notebook
# =============================================================================

import re
import json
import os
import requests
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Finding:
    severity: str        # "critical", "warning", "suggestion"
    area: str
    issue: str
    fix: str
    cell: Optional[int] = None

    def to_dict(self):
        return asdict(self)


# ── NOTEBOOK LOADING ──────────────────────────────────────────────────────────

def load_notebook_from_ipynb(path: str) -> str:
    """Read code cells from a standard .ipynb file."""
    with open(path, "r", encoding="utf-8") as f:
        nb = json.load(f)
    snippets = []
    for i, cell in enumerate(nb.get("cells", [])):
        if cell.get("cell_type") == "code":
            src = "".join(cell.get("source", [])).strip()
            if src:
                snippets.append(f"### Cell {i+1}\n{src}")
    return "\n\n".join(snippets)


def load_notebook_from_py(path: str) -> str:
    """Read code from a Fabric notebook-content.py file."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Split on Fabric cell markers
    cell_pattern = re.compile(
        r'# (?:CELL|PARAMETERS CELL|MARKDOWN CELL) \*{10,}',
        re.IGNORECASE
    )
    parts = cell_pattern.split(content)
    snippets = []
    cell_num = 0
    for part in parts:
        # Skip metadata blocks
        stripped = part.strip()
        if not stripped or stripped.startswith("# Fabric notebook source"):
            continue
        # Remove metadata sections
        meta_pattern = re.compile(
            r'# METADATA \*{10,}.*?(?=\n(?!# META)|\Z)', re.DOTALL
        )
        cleaned = meta_pattern.sub("", stripped).strip()
        if cleaned and not cleaned.startswith("# META"):
            cell_num += 1
            snippets.append(f"### Cell {cell_num}\n{cleaned}")

    return "\n\n".join(snippets)


def load_notebook(path: str) -> str:
    """Load notebook from either .ipynb or notebook-content.py."""
    if path.endswith(".ipynb"):
        return load_notebook_from_ipynb(path)
    elif path.endswith(".py"):
        return load_notebook_from_py(path)
    else:
        raise ValueError(f"Unsupported file type: {path}")


# ── PYTHON PRE-SCAN (regex-based deterministic checks) ───────────────────────

def python_pre_scan(code_text: str) -> list[Finding]:
    """Deterministic regex-based scan — catches printSchema, display(), hardcoded lists, etc."""
    findings = []
    current_cell = None

    for line in code_text.split("\n"):
        # Track cell number
        if line.startswith("### Cell"):
            try:
                current_cell = int(line.replace("### Cell", "").strip())
            except Exception:
                pass
            continue

        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # printSchema()
        if re.search(r'\.printSchema\s*\(\)', stripped) or re.search(r'^printSchema\s*\(\)', stripped):
            findings.append(Finding(
                severity="warning", area="Schema Printing", cell=current_cell,
                issue=f"`{stripped[:150]}` — printSchema() is a debug call, not suitable for pipeline.",
                fix="Remove printSchema() before pipeline deployment. Use logging or schema validation instead."
            ))

        # display()
        if re.search(r'\bdisplay\s*\(.+\)', stripped):
            findings.append(Finding(
                severity="warning", area="DataFrame Display", cell=current_cell,
                issue=f"`{stripped[:150]}` — display() exposes data in run logs and slows execution.",
                fix="Remove display() calls before pipeline deployment."
            ))

        # df.show()
        if re.search(r'\w+\s*\.\s*show\s*\(', stripped):
            findings.append(Finding(
                severity="warning", area="DataFrame Display", cell=current_cell,
                issue=f"`{stripped[:150]}` — .show() transfers data to driver node.",
                fix="Remove .show() calls before pipeline deployment."
            ))

        # df.head()
        if re.search(r'\w+\s*\.\s*head\s*\(', stripped):
            findings.append(Finding(
                severity="warning", area="DataFrame Display", cell=current_cell,
                issue=f"`{stripped[:150]}` — .head() collects data to driver.",
                fix="Remove .head() calls before pipeline deployment."
            ))

        # Hardcoded lists of strings
        if re.search(r'''(\w+)\s*=\s*\[\s*['"][^'"]+['"]\s*''', stripped):
            findings.append(Finding(
                severity="critical", area="Hardcoded Lists", cell=current_cell,
                issue=f"`{stripped[:150]}` — list variable contains hardcoded string values.",
                fix="Replace with a pipeline parameter or dynamic query. "
                    "E.g. listSilverTables = getArgument('silverTables').split(',') "
                    "or read from a config table in the lakehouse."
            ))

        # listSilverTables specifically
        if re.search(r'\blistSilverTables\s*=\s*\[', stripped):
            findings.append(Finding(
                severity="critical", area="Hardcoded Lists", cell=current_cell,
                issue=f"`{stripped[:150]}` — listSilverTables is hardcoded. Must be parameterised.",
                fix="Pass as a pipeline parameter: listSilverTables = getArgument('silverTables').split(',') "
                    "or query the lakehouse dynamically: "
                    "listSilverTables = [r.tableName for r in spark.catalog.listTables()]"
            ))

        # Hardcoded credentials / secrets
        secret_patterns = [
            (r'(?:password|passwd|pwd)\s*=\s*["\']', "password"),
            (r'(?:api_key|apikey|api_secret)\s*=\s*["\']', "API key"),
            (r'(?:token|access_token|bearer)\s*=\s*["\']', "token"),
            (r'(?:connection_string|conn_str)\s*=\s*["\']', "connection string"),
            (r'(?:sas_token|shared_access)\s*=\s*["\']', "SAS token"),
            (r'(?:GROQ_API_KEY|OPENAI_API_KEY)\s*=\s*["\']', "API key"),
        ]
        for pattern, secret_type in secret_patterns:
            if re.search(pattern, stripped, re.IGNORECASE):
                findings.append(Finding(
                    severity="critical", area="Security",
                    cell=current_cell,
                    issue=f"`{stripped[:100]}` — Hardcoded {secret_type} detected.",
                    fix=f"Use environment variables or a secret manager instead of hardcoding {secret_type}s."
                ))

    # Deduplicate
    seen, deduped = set(), []
    for f in findings:
        key = (f.area, f.cell, f.issue[:60])
        if key not in seen:
            seen.add(key)
            deduped.append(f)

    return deduped


# ── METADATA CHECKS (for Fabric notebook-content.py) ─────────────────────────

def meta_checks_from_py(content: str) -> list[Finding]:
    """Check notebook metadata from a Fabric notebook-content.py file."""
    findings = []

    # Check for parameter cell
    if "# PARAMETERS CELL" not in content:
        findings.append(Finding(
            severity="warning", area="Pipeline-Readiness",
            issue="No parameter cell found in the notebook.",
            fix="Add a parameter cell (# PARAMETERS CELL) at the top with pipeline parameters."
        ))

    return findings


def meta_checks_from_ipynb(nb_dict: dict) -> list[Finding]:
    """Check notebook metadata from a parsed .ipynb dict."""
    findings = []
    cells = nb_dict.get("cells", [])

    # Check for parameter cell (tagged with 'parameters')
    has_param_cell = False
    for cell in cells:
        tags = cell.get("metadata", {}).get("tags", [])
        if "parameters" in tags:
            has_param_cell = True
            break

    if not has_param_cell:
        findings.append(Finding(
            severity="warning", area="Pipeline-Readiness",
            issue="No parameter cell (tagged with 'parameters') found.",
            fix="Add a cell with tag 'parameters' for pipeline parameterisation."
        ))

    return findings


# ── AI REVIEW ─────────────────────────────────────────────────────────────────

AI_REVIEW_PROMPT = """You are a senior Microsoft Fabric / PySpark code reviewer.
Carefully read every line of every code cell and review across these areas:

1. Pipeline-Readiness — hardcoded dates, paths, table names, schema names that should be parameters.
   Also check if there is a toggle parameter cell and if the parameter cell is toggled you can skip
   the data hardcode check.
2. Error Handling — missing try/except, no retry logic, silent failures, bare except
3. Performance — full table loads with no filters, large .collect() or .toPandas() calls
4. Code Duplication / Dead Code — repeated logic blocks, unused variables, commented-out code
5. Logging & Monitoring — no row count checks, no duration tracking, no logging statements
6. Security — hardcoded credentials, tokens, passwords, connection strings, SAS tokens

NOTE: printSchema(), display(), df.show(), and hardcoded lists are already caught by pre-scan.
Focus on the above 6 areas only. Do not duplicate those findings.

Return ONLY a JSON array. Each item must have:
- "severity": "critical", "warning", or "suggestion"
- "area": area name from above
- "cell": cell number (integer) from ### Cell N header, or null
- "issue": quote the exact code causing the issue (1-2 sentences)
- "fix": specific actionable fix with example code

Return only valid JSON — no markdown, no text outside the array."""

CHAT_SYSTEM = """You are a Microsoft Fabric notebook code reviewer assistant.
You have performed a full review including metadata checks, Python pre-scan, and AI code analysis.
Answer questions about the findings. Be specific, reference exact cell numbers and code.
Give complete self-contained answers. Never ask the user a question back.
Keep responses concise and focused on the findings."""


def call_groq(system_prompt: str, user_message: str, max_tokens: int = 3000) -> str:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise ValueError("GROQ_API_KEY environment variable not set")

    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        },
        timeout=60,
    )
    if r.status_code != 200:
        raise Exception(f"Groq error {r.status_code}: {r.text[:300]}")
    return r.json()["choices"][0]["message"]["content"]


def ai_code_review(nb_name: str, code_text: str) -> list[Finding]:
    if not code_text.strip():
        return []
    try:
        raw = call_groq(
            AI_REVIEW_PROMPT,
            f"Notebook: {nb_name}\n\nCode:\n{code_text}",
            max_tokens=3000,
        )
        items = json.loads(raw.replace("```json", "").replace("```", "").strip())
        return [Finding(**item) for item in items]
    except Exception as e:
        return [Finding(
            severity="warning", area="System", cell=None,
            issue=f"AI review failed: {e}",
            fix="Check your GROQ_API_KEY and retry."
        )]


def ask_ai(question: str, nb_name: str, all_findings: list[Finding], chat_history: list) -> str:
    context = (
        f"Notebook: {nb_name}\n\n"
        f"Findings:\n{json.dumps([f.to_dict() for f in all_findings], indent=2)}\n\n"
        f"Question: {question}"
    )
    try:
        return call_groq(CHAT_SYSTEM, context, max_tokens=1000)
    except Exception as e:
        return f"[ERROR] {e}"


# ── FULL REVIEW PIPELINE ─────────────────────────────────────────────────────

def run_full_review(file_path: str) -> tuple[str, list[Finding]]:
    """
    Run the complete review pipeline on a notebook file.
    Returns (notebook_name, list_of_findings).
    """
    nb_name = os.path.basename(file_path)

    # 1. Load code
    code_text = load_notebook(file_path)
    if not code_text:
        return nb_name, [Finding(
            severity="critical", area="System",
            issue=f"Could not read any code cells from '{nb_name}'.",
            fix="Ensure the file is a valid .ipynb or Fabric notebook-content.py."
        )]

    # 2. Metadata checks
    meta_findings = []
    if file_path.endswith(".py"):
        with open(file_path, "r", encoding="utf-8") as f:
            meta_findings = meta_checks_from_py(f.read())
    elif file_path.endswith(".ipynb"):
        with open(file_path, "r", encoding="utf-8") as f:
            meta_findings = meta_checks_from_ipynb(json.load(f))

    # 3. Python pre-scan
    pre_findings = python_pre_scan(code_text)

    # 4. AI review
    ai_findings = ai_code_review(nb_name, code_text)

    # 5. Merge, deduplicate, sort
    severity_order = {"critical": 0, "warning": 1, "suggestion": 2}
    seen, merged = set(), []
    for f in meta_findings + pre_findings + ai_findings:
        key = (f.area, f.cell, f.issue[:40])
        if key not in seen:
            seen.add(key)
            merged.append(f)

    merged.sort(key=lambda x: severity_order.get(x.severity, 2))
    return nb_name, merged
