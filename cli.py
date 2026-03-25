#!/usr/bin/env python3
"""
devex — Repo-aware interactive CLI powered by the Claude Agent SDK.

Commands:
  devex scan       build / refresh knowledge base for the repo
  devex stats      show context-window usage history
  devex            interactive mode  (uses KB if present)
  devex -p "..."   single query      (uses KB if present)
  devex --no-kb    skip KB, read files live
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import anyio

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    AssistantMessage,
    TextBlock,
    SystemMessage,
    CLINotFoundError,
    CLIConnectionError,
    list_sessions,
)

# ── ANSI helpers ──────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"

def color(text: str, *codes: str) -> str:
    if not sys.stdout.isatty():
        return text
    return "".join(codes) + text + RESET

# ── Knowledge-base constants ──────────────────────────────────────────────────

KB_FILE         = ".devex-kb.md"
STATS_FILE      = ".devex-stats.json"
GUARDRAILS_FILE = ".devex-guardrails.md"
TASKS_DIR       = ".devex-tasks"

# Context window limit — override with CONTEXT_WINDOW env var
CONTEXT_WINDOW = int(os.environ.get("CONTEXT_WINDOW", "128000"))
WARN_AT        = 0.75   # warn when avg usage exceeds 75 %
CRITICAL_AT    = 0.90   # critical at 90 %
STATS_KEEP     = 200    # max entries to retain

SCAN_PROMPT = """\
Analyze this entire codebase and produce a comprehensive, structured knowledge \
base in markdown that can later be injected as context to answer questions \
about the repo without re-reading files.

Cover:
1. **Project overview** — what it does, tech stack, high-level architecture
2. **Directory map** — every significant directory and its purpose
3. **Core modules** — for each non-trivial file:
   - purpose / responsibility
   - public API: key classes, functions, CLI commands, HTTP routes
   - notable patterns, algorithms, or gotchas
4. **Data flow** — how data enters, transforms, and exits the system
5. **External dependencies** — key libraries and what they're used for
6. **Configuration & entry points** — env vars, config files, main entry points

Be thorough. Do not truncate or summarise away important detail.
"""

KB_SYSTEM = """\
You are an expert on this codebase. A knowledge base was pre-built by fully \
scanning the repository — use it to answer questions accurately without \
re-reading files unless the user asks for live file content or the KB lacks \
the specific detail needed.
"""

# ── Token estimation ──────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """Rough estimate: ~4 characters per token."""
    return max(1, len(text) // 4)

def usage_pct(tokens: int) -> float:
    return tokens / CONTEXT_WINDOW * 100

def usage_bar(pct: float, width: int = 20) -> str:
    filled = int(width * pct / 100)
    bar    = "█" * filled + "░" * (width - filled)
    code   = GREEN if pct < 60 else YELLOW if pct < 85 else RED
    return color(f"[{bar}]", code) + color(f" {pct:.1f}%", code)

# ── Stats store ───────────────────────────────────────────────────────────────

def _stats_path(cwd: str) -> Path:
    return Path(cwd) / STATS_FILE

def record_stats(
    cwd: str,
    prompt: str,
    kb_content: str | None,
    response_text: str,
) -> dict:
    """Build a stats record, append to the JSON store, return the record."""
    kb_tokens       = estimate_tokens(kb_content) if kb_content else 0
    prompt_tokens   = estimate_tokens(prompt)
    response_tokens = estimate_tokens(response_text)
    total_tokens    = kb_tokens + prompt_tokens + response_tokens

    record = {
        "timestamp":       datetime.now().isoformat(),
        "kb_tokens":       kb_tokens,
        "prompt_tokens":   prompt_tokens,
        "response_tokens": response_tokens,
        "total_tokens":    total_tokens,
        "usage_pct":       round(usage_pct(total_tokens), 1),
        "kb_loaded":       kb_content is not None,
    }

    path = _stats_path(cwd)
    data: list[dict] = []
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = []
    data.append(record)
    data = data[-STATS_KEEP:]
    path.write_text(json.dumps(data, indent=2))
    return record

def load_stats(cwd: str) -> list[dict]:
    path = _stats_path(cwd)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []

def context_warning(cwd: str) -> str | None:
    """Return a warning string if recent average context usage is high."""
    data = load_stats(cwd)
    if not data:
        return None
    recent  = data[-5:]
    avg_pct = sum(q["usage_pct"] for q in recent) / len(recent)
    if avg_pct >= CRITICAL_AT * 100:
        return (
            color(f"⚠  Context {avg_pct:.0f}% full", RED + BOLD)
            + color(" (avg last 5 queries) — consider switching to KB-agent mode", RED)
        )
    if avg_pct >= WARN_AT * 100:
        return (
            color(f"⚠  Context {avg_pct:.0f}% full", YELLOW + BOLD)
            + color(" (avg last 5 queries)", YELLOW)
        )
    return None

# ── KB helpers ────────────────────────────────────────────────────────────────

def kb_path(cwd: str) -> Path:
    return Path(cwd) / KB_FILE

def load_kb(cwd: str) -> str | None:
    p = kb_path(cwd)
    return p.read_text() if p.exists() else None

def kb_age_str(cwd: str) -> str:
    p = kb_path(cwd)
    if not p.exists():
        return "not found"
    delta = datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)
    if delta.days:
        return f"{delta.days}d old"
    if delta.seconds >= 3600:
        return f"{delta.seconds // 3600}h old"
    return f"{delta.seconds // 60}m old"

def guardrails_path(cwd: str) -> Path:
    return Path(cwd) / GUARDRAILS_FILE

def load_guardrails(cwd: str) -> str | None:
    p = guardrails_path(cwd)
    return p.read_text().strip() if p.exists() else None

def guardrails_block(guardrails: str | None) -> str:
    """Returns XML guardrails block to append to system prompts / docs."""
    if not guardrails:
        return ""
    return f"\n\n<guardrails>\n{guardrails}\n</guardrails>"


_GITIGNORE_ENTRIES = [
    ".devex-tasks/",
    ".devex-reviews/",
    ".devex-kb.md",
    ".devex-stats.json",
    ".devex-guardrails.md",
    ".devex-requirements.md",
    ".devex-devplan.md",
]

_GITIGNORE_MARKER = "# devex — auto-generated files"

def _ensure_gitignore(cwd: str) -> None:
    """Append devex entries to the repo's .gitignore if they're not already there."""
    gi = Path(cwd) / ".gitignore"
    existing = gi.read_text() if gi.exists() else ""

    missing = [e for e in _GITIGNORE_ENTRIES if e not in existing]
    if not missing:
        return

    block = f"\n{_GITIGNORE_MARKER}\n" + "\n".join(missing) + "\n"
    with gi.open("a") as f:
        f.write(block)


# ── Task queue helpers ────────────────────────────────────────────────────────

def _tasks_root(cwd: str) -> Path:
    return Path(cwd) / TASKS_DIR

def _task_dir(cwd: str, task_id: str) -> Path:
    return _tasks_root(cwd) / f"task-{task_id}"

def _next_task_id(cwd: str) -> str:
    """Return next zero-padded 3-digit task ID."""
    root = _tasks_root(cwd)
    if not root.exists():
        return "001"
    ids = sorted(
        int(p.name.split("-")[1])
        for p in root.iterdir()
        if p.is_dir() and p.name.startswith("task-") and p.name.split("-")[1].isdigit()
    )
    return f"{(ids[-1] + 1 if ids else 1):03d}"

def _task_status_path(cwd: str, task_id: str) -> Path:
    return _task_dir(cwd, task_id) / "status.json"

def _load_task(cwd: str, task_id: str) -> dict:
    p = _task_status_path(cwd, task_id)
    return json.loads(p.read_text()) if p.exists() else {}

def _save_task(cwd: str, task_id: str, data: dict) -> None:
    data = {**data, "updated_at": datetime.now().isoformat()}
    _task_status_path(cwd, task_id).write_text(json.dumps(data, indent=2))

def _list_tasks(cwd: str) -> list[dict]:
    root = _tasks_root(cwd)
    if not root.exists():
        return []
    tasks = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and p.name.startswith("task-"):
            sp = p / "status.json"
            if sp.exists():
                try:
                    tasks.append(json.loads(sp.read_text()))
                except Exception:
                    pass
    return tasks

def _extract_doc_title(doc_text: str) -> str:
    """Extract first H1 title from a markdown doc."""
    for line in doc_text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = line.lstrip("# ").strip()
            for prefix in ("Dev Plan:", "Requirements:", "dev plan:", "requirements:"):
                if title.lower().startswith(prefix.lower()):
                    title = title[len(prefix):].strip()
            return title
    return "Untitled Task"


def build_system_prompt(kb_content: str | None, custom: str | None, guardrails: str | None = None) -> str | None:
    if custom:
        return custom
    base = ""
    if kb_content:
        base = KB_SYSTEM + "\n\n<knowledge_base>\n" + kb_content + "\n</knowledge_base>"
    if guardrails:
        base += guardrails_block(guardrails)
    return base or None

# ── Options builder ───────────────────────────────────────────────────────────

def make_opts(
    cwd: str,
    tools: list[str],
    permission_mode: str,
    system_prompt: str | None = None,
    max_turns: int | None = None,
    resume: str | None = None,
) -> dict:
    opts: dict = {
        "cwd": cwd,
        "allowed_tools": tools,
        "permission_mode": permission_mode,
    }
    if system_prompt:
        opts["system_prompt"] = system_prompt
    if max_turns:
        opts["max_turns"] = max_turns
    if resume:
        opts["resume"] = resume
    model = os.environ.get("MODEL")
    if model:
        opts["model"] = model
    return opts

# ── Core query runner ─────────────────────────────────────────────────────────

async def run_query(
    prompt: str,
    opts: dict,
    cwd: str,
    kb_content: str | None = None,
) -> tuple[str | None, ResultMessage | None]:
    """Stream a query, record stats, return (session_id, ResultMessage)."""
    session_id    = None
    response_text = ""
    final_result: ResultMessage | None = None

    try:
        async for message in query(prompt=prompt, options=ClaudeAgentOptions(**opts)):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = message.data.get("session_id")
                print(color(f"[session: {session_id}]", DIM), flush=True)
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
                        response_text += block.text
            elif isinstance(message, ResultMessage):
                final_result = message
            # intentionally no early return — let the generator finish naturally

    except CLINotFoundError:
        print(color("\nError: Claude Code CLI not found.", RED))
        print(color("Install: pip install claude-agent-sdk", DIM))
        sys.exit(1)
    except CLIConnectionError as e:
        print(color(f"\nConnection error: {e}", RED))
        sys.exit(1)
    except KeyboardInterrupt:
        print(color("\n[interrupted]", YELLOW))
        return session_id, None

    print()

    if final_result:
        if final_result.stop_reason and final_result.stop_reason != "end_turn":
            print(color(f"[stop: {final_result.stop_reason}]", YELLOW))

        rec = record_stats(cwd, prompt, kb_content, response_text)
        bar = usage_bar(rec["usage_pct"])
        print(
            color(
                f"  tokens: {rec['total_tokens']:,}"
                f"  (kb {rec['kb_tokens']:,}"
                f" + prompt {rec['prompt_tokens']:,}"
                f" + response {rec['response_tokens']:,})"
                f"  context ",
                DIM,
            )
            + bar,
            flush=True,
        )

    return session_id, final_result

# ── Stats command ─────────────────────────────────────────────────────────────

def cmd_stats(cwd: str) -> None:
    data = load_stats(cwd)
    if not data:
        print(color("No stats recorded yet. Run a query first.", DIM))
        return

    kb_tokens = estimate_tokens(load_kb(cwd) or "")
    print(color(f"Context window: {CONTEXT_WINDOW:,} tokens", BOLD))
    print(color(f"KB size:        {kb_tokens:,} tokens  ({kb_tokens / CONTEXT_WINDOW * 100:.1f}% of window)", DIM))
    print()

    # Summary
    recent  = data[-10:]
    avg_pct = sum(q["usage_pct"] for q in recent) / len(recent)
    max_pct = max(q["usage_pct"] for q in recent)
    print(color(f"Last {len(recent)} queries — avg {avg_pct:.1f}%  peak {max_pct:.1f}%", BOLD))
    print()

    # Table
    header = f"  {'Timestamp':<22}  {'Total':>7}  {'KB':>7}  {'Prompt':>7}  {'Response':>9}  Usage"
    print(color(header, BOLD))
    print(color("  " + "─" * 75, DIM))

    for q in data[-20:]:
        ts   = q["timestamp"][:19].replace("T", " ")
        line = (
            f"  {ts:<22}"
            f"  {q['total_tokens']:>7,}"
            f"  {q['kb_tokens']:>7,}"
            f"  {q['prompt_tokens']:>7,}"
            f"  {q['response_tokens']:>9,}"
            f"  "
        )
        print(line + usage_bar(q["usage_pct"], width=15))

    # Warning
    warn = context_warning(cwd)
    if warn:
        print()
        print(warn)
        print(color("  → Rebuild KB with `devex scan` to trim it, or switch to KB-agent mode.", DIM))

# ── Scan ──────────────────────────────────────────────────────────────────────

async def cmd_scan(cwd: str) -> None:
    print(color(f"Scanning {cwd}", BOLD + CYAN))
    print(color("This may take a few minutes for large repos…", DIM))
    print()

    opts = make_opts(
        cwd=cwd,
        tools=["Read", "Glob", "Grep"],
        permission_mode="default",
        system_prompt="You are a code analyst. Produce exhaustive, structured documentation.",
        max_turns=100,
    )

    result_text = ""
    try:
        async for message in query(prompt=SCAN_PROMPT, options=ClaudeAgentOptions(**opts)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
            elif isinstance(message, ResultMessage):
                print()
                result_text = message.result or ""
    except KeyboardInterrupt:
        print(color("\n[scan interrupted]", YELLOW))
        return

    if not result_text.strip():
        print(color("Scan produced no output — KB not updated.", RED))
        return

    dest = kb_path(cwd)
    dest.write_text(f"<!-- Generated: {datetime.now().isoformat()} -->\n\n" + result_text)
    size_kb = dest.stat().st_size / 1024
    kb_tokens = estimate_tokens(result_text)
    print(color(f"\nKnowledge base saved → {dest}", GREEN + BOLD))
    print(color(f"  {size_kb:.1f} KB  ·  ~{kb_tokens:,} tokens  ·  {kb_tokens / CONTEXT_WINDOW * 100:.1f}% of context window", DIM))

# ── Requirements flow ─────────────────────────────────────────────────────────

REQ_FILE = ".devex-requirements.md"

TECH_LEAD_SYSTEM = """\
You are a senior tech lead whose job is to fully clarify a development task \
before any coding begins. You have deep knowledge of the codebase via the \
knowledge base provided.

Each round you will receive the original task description and a running Q&A \
log. Your job:
  1. Identify what is still ambiguous or under-specified.
  2. If clarification is needed — respond ONLY with:
       QUESTIONS:
       1. <first question>
       2. <second question>
       ...
  3. If you have enough information to hand off to a developer — respond ONLY with:
       READY

Do not mix prose with QUESTIONS or READY. Keep questions focused and \
non-redundant with answers already given.
"""

REQUIREMENTS_WRITER_SYSTEM = """\
You are a senior requirements analyst and technical writer. You receive a task \
description, a full Q&A log from a tech-lead review session, and a knowledge \
base of the codebase. Produce a thorough requirements document in markdown.

Structure:
# <concise task title>

## Overview
What needs to be built and why (2–4 sentences).

## Acceptance Criteria
Bullet list of specific, testable outcomes.

## Sub-tasks
Numbered list. Each sub-task must have:
  - a short title
  - 1–3 sentences describing exactly what to implement
  - which files / modules are likely affected (use KB knowledge)

## Technical Considerations
Patterns to follow, existing abstractions to reuse, edge cases, migrations needed.

## Out of Scope
What is explicitly NOT part of this task.

## Guardrails
If a <guardrails> block is present in the prompt, copy those rules verbatim \
here under this heading. If no guardrails were provided, omit this section.
"""


async def _silent_query(
    prompt: str,
    system_prompt: str,
    cwd: str,
    resume_session_id: str | None = None,
) -> tuple[str, str | None]:
    """
    Run a query silently (no streaming output).
    Returns (response_text, session_id).
    Pass resume_session_id to continue an existing session.
    """
    opts = make_opts(
        cwd=cwd,
        tools=["Read", "Glob", "Grep"],
        permission_mode="default",
        system_prompt=system_prompt,
        max_turns=10,
        resume=resume_session_id,
    )
    response_text = ""
    session_id: str | None = None
    async for message in query(prompt=prompt, options=ClaudeAgentOptions(**opts)):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id")
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    response_text += block.text
        # let loop finish naturally — no early return
    return response_text.strip(), session_id


def _parse_tech_lead(response: str) -> list[str] | None:
    """
    Returns list of questions if tech lead needs more info,
    None if tech lead signals READY.

    Accepts both strict format (QUESTIONS: / READY) and loose formats where
    the model just writes a numbered/bulleted list or plain prose questions.
    """
    import re

    text = response.strip()
    if not text:
        return None

    # Any line that is exactly "READY" (or starts with it) → satisfied
    for line in text.splitlines():
        if line.strip().upper().startswith("READY"):
            return None

    questions: list[str] = []

    # ── Pass 1: look for explicit QUESTIONS: block ────────────────────────
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("QUESTIONS:"):
            in_block = True
            # handle inline "QUESTIONS: <text>"
            inline = stripped[stripped.upper().index("QUESTIONS:") + len("QUESTIONS:"):].strip()
            if inline:
                questions.append(inline)
            continue
        if in_block:
            if not stripped:
                continue
            # stop at next section header (all-caps word followed by colon)
            if re.match(r"^[A-Z][A-Z\s]+:$", stripped):
                break
            q = re.sub(r"^[\d]+[.)]\s*|^[-*•]\s*", "", stripped).strip()
            if q:
                questions.append(q)

    if questions:
        return questions

    # ── Pass 2: no QUESTIONS: block — extract numbered/bulleted lines ─────
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # numbered list: "1. ..." or "1) ..."
        m = re.match(r"^\d+[.)]\s+(.+)", stripped)
        if m:
            questions.append(m.group(1).strip())
            continue
        # bulleted: "- ..." or "* ..." or "• ..."
        m = re.match(r"^[-*•]\s+(.+)", stripped)
        if m:
            questions.append(m.group(1).strip())

    if questions:
        return questions

    # ── Pass 3: model wrote pure prose — treat the whole response as one question ──
    # Only do this if the response looks like it's asking something
    # (contains a "?" character somewhere)
    if "?" in text:
        # Extract sentences that end with "?"
        sentences = re.findall(r"[^.!?\n]+\?", text)
        clean = [s.strip() for s in sentences if len(s.strip()) > 10]
        if clean:
            return clean

    # Nothing found and no READY → treat as READY to avoid infinite loops
    return None


def _build_tech_lead_prompt(task: str, qa_history: list[dict]) -> str:
    parts = [f"Task: {task}"]
    if qa_history:
        parts.append("\nQ&A so far:")
        for item in qa_history:
            parts.append(f"  Q: {item['question']}")
            parts.append(f"  A: {item['answer']}")
    parts.append("\nReview the above and either list your QUESTIONS or respond READY.")
    return "\n".join(parts)


async def cmd_requirements(cwd: str, kb_content: str | None, save_dir: Path | None = None) -> None:
    """Interactive requirements capture: user ↔ tech-lead loop → requirements doc."""

    kb_ctx = f"\n\n<knowledge_base>\n{kb_content}\n</knowledge_base>" if kb_content else ""

    # ── Header ────────────────────────────────────────────────────────────────
    print(color("━" * 54, CYAN))
    print(color("  devex — Requirements Capture", BOLD + CYAN))
    print(color("━" * 54, CYAN))
    if kb_content:
        print(color(f"  KB loaded  ({kb_age_str(cwd)},  ~{estimate_tokens(kb_content):,} tokens)", DIM))
    else:
        print(color("  No KB found — run `devex scan` first for best results.", YELLOW))
    print()

    # ── Step 1: initial task description ─────────────────────────────────────
    print(color("Describe the task you want to work on:", BOLD))
    print(color("(be as vague or detailed as you like — the tech lead will ask follow-ups)", DIM))
    try:
        task = await anyio.to_thread.run_sync(lambda: input(color("\n> ", BOLD + GREEN)))
    except (EOFError, KeyboardInterrupt):
        print(color("\nCancelled.", DIM))
        return

    task = task.strip()
    if not task:
        print(color("No task provided.", DIM))
        return

    # ── Step 2: tech-lead Q&A loop ────────────────────────────────────────────
    qa_history: list[dict] = []
    MAX_ROUNDS = 5
    tl_session_id: str | None = None  # persisted across rounds for this task

    for round_num in range(1, MAX_ROUNDS + 1):
        print(color(f"\n  [tech lead — round {round_num}/{MAX_ROUNDS}…]", DIM))

        tl_prompt = _build_tech_lead_prompt(task, qa_history)
        tl_response, tl_session_id = await _silent_query(
            tl_prompt, TECH_LEAD_SYSTEM + kb_ctx, cwd,
            resume_session_id=tl_session_id,
        )

        questions = _parse_tech_lead(tl_response)

        if questions is None:
            print(color("  ✓ Tech lead is satisfied.", GREEN))
            break

        print(color(f"\n  Tech lead has {len(questions)} question(s):", BOLD))
        print()

        for i, q in enumerate(questions, 1):
            print(color(f"  {i}. {q}", CYAN))
            try:
                answer = await anyio.to_thread.run_sync(
                    lambda: input(color("     → ", GREEN))
                )
            except (EOFError, KeyboardInterrupt):
                print(color("\nCancelled.", DIM))
                return
            qa_history.append({"question": q, "answer": answer.strip()})

    else:
        print(color(f"\n  Max rounds ({MAX_ROUNDS}) reached — proceeding with what we have.", YELLOW))

    # ── Step 2.5: capture guardrails ──────────────────────────────────────────
    print()
    print(color("  Any guardrails / dos-and-don'ts for this task?", BOLD))
    print(color("  e.g. 'do not touch the auth module', 'always use async functions'", DIM))
    print(color("  Enter one rule per line. Press Enter on a blank line when done.", DIM))
    print(color("  (just press Enter to skip)", DIM))
    print()

    guardrails_lines: list[str] = []
    while True:
        try:
            line = await anyio.to_thread.run_sync(
                lambda: input(color("  guardrail> ", YELLOW))
            )
        except (EOFError, KeyboardInterrupt):
            break
        if not line.strip():
            break
        guardrails_lines.append(line.strip())

    guardrails_text = "\n".join(f"- {l}" for l in guardrails_lines) if guardrails_lines else ""

    if guardrails_text:
        gr_dest = (save_dir / "guardrails.md") if save_dir else guardrails_path(cwd)
        gr_dest.write_text(
            f"<!-- Generated: {datetime.now().isoformat()} -->\n\n"
            f"# Project Guardrails\n\n"
            f"{guardrails_text}\n"
        )
        print(color(f"\n  ✓ Guardrails saved → {gr_dest}", GREEN))
    else:
        print(color("  No guardrails added.", DIM))

    # ── Step 3: generate requirements doc ─────────────────────────────────────
    print(color("\n  [generating requirements document…]", DIM))

    qa_block = "\n".join(
        f"Q: {item['question']}\nA: {item['answer']}" for item in qa_history
    )
    guardrails_ctx = guardrails_block(guardrails_text)
    doc_prompt = (
        f"Task: {task}\n\n"
        + (f"Q&A:\n{qa_block}\n\n" if qa_block else "")
        + (f"<guardrails>\n{guardrails_text}\n</guardrails>\n\n" if guardrails_text else "")
        + "Generate the full requirements document now."
    )

    doc, _ = await _silent_query(doc_prompt, REQUIREMENTS_WRITER_SYSTEM + kb_ctx + guardrails_ctx, cwd)

    if not doc.strip():
        print(color("  Failed to generate document.", RED))
        return

    # ── Step 4: save ──────────────────────────────────────────────────────────
    dest = (save_dir / "requirements.md") if save_dir else Path(cwd) / REQ_FILE
    dest.write_text(
        f"<!-- Generated: {datetime.now().isoformat()} -->\n\n"
        + f"<!-- Task: {task} -->\n\n"
        + doc
    )

    print(color(f"\n  ✓ Requirements saved → {dest}", GREEN + BOLD))
    print(color("━" * 54, CYAN))
    print()
    print(doc)


# ── Dev Plan flow ─────────────────────────────────────────────────────────────

DEVPLAN_FILE = ".devex-devplan.md"

TECH_LEAD_PLANNER_SYSTEM = """\
You are a senior tech lead responsible for producing a precise, actionable \
development plan that a developer can follow step-by-step with zero ambiguity.

You are given:
  1. A requirements document describing what needs to be built.
  2. A knowledge base summarising the codebase (architecture, modules, patterns).
  3. Direct access to the codebase via Read / Glob / Grep — use them \
     extensively. You must READ actual code, not guess.

════════════════════════════════════════════════════════
MANDATORY PROCESS — do this before writing a single line of the plan:
════════════════════════════════════════════════════════

For EVERY file that will be changed or created:

  A. READ THE SURROUNDING CODE
     Use the Read tool to open the file and read the 30–50 lines immediately
     around where the change will go (the insertion point or the function to
     modify). Paste this snippet verbatim into the plan as "Nearby Code
     Reference". Do not paraphrase — paste the real code.

  B. CHECK FOR EXISTING SIMILAR FUNCTIONS
     Before specifying "create function X", search the codebase:
       grep -r "def similar_name" / Grep for the concept
     Decision rules:
       • If a function already does the same thing → specify REUSE it (give
         the exact file:line). Do NOT create a duplicate.
       • If a function does something close but not identical → specify EXTEND
         it or CREATE a new one alongside it. Document your reasoning.
       • Only if nothing similar exists → specify CREATE a new function.
     Document this search and its result in the plan.

  C. DERIVE CHANGE CONVENTION FROM THE NEARBY CODE
     From the snippet you read, extract the exact micro-conventions in use:
     variable naming, indentation, error handling, return types, logging,
     imports style, docstring format, etc. Write these as a short checklist
     the developer must match line-for-line.

════════════════════════════════════════════════════════
DEV PLAN DOCUMENT STRUCTURE:
════════════════════════════════════════════════════════

# Dev Plan: <title>

## Strategy
2–4 sentences on the overall implementation approach.

## Global Codebase Conventions
Patterns observed across the repo that apply everywhere:
- Naming conventions (variables, functions, classes, files)
- Error handling idiom
- Import ordering
- Logging/print style
- Test file naming and structure

## Implementation Steps

For each sub-task from the requirements, produce a section like this:

---
### Step N — <sub-task title>

**Files to change / create:**
- `path/to/file.py` — brief description of what changes

**Existing Function Check:**
- Searched for: `<search terms / grep pattern used>`
- Result: FOUND `existing_func()` at `path/to/file.py:42` / NOT FOUND
- Decision: [REUSE existing_func | EXTEND existing_func | CREATE new_func]
  Reason: <one sentence why>

**Nearby Code Reference:**
The actual code surrounding the insertion point, read with the Read tool.
Include file path and line numbers.

```
// path/to/file.py  lines N–M
<paste verbatim code snippet here>
```

**Change Convention** (derived from the snippet above):
- [ ] Use the same naming style as `existing_var` on line N
- [ ] Match the error-handling pattern: `try/except X` returning `Y`
- [ ] Follow the same import style already in the file
- [ ] Use the same helper `util_fn()` already called nearby
- (add as many specific bullets as apply)

**Logic:**
Precise description: function signature, parameters, return type, \
algorithm, edge cases, how it connects to surrounding code.

**Git commit message for this step:**
`feat: <concise description>`

---

## Implementation Order
Numbered list with one-line rationale per ordering decision.

## Testing Plan
- What to test per step.
- Existing test file to mirror (paste its first 20 lines as reference).
- Fixtures or mocks needed.

## Risks & Gotchas
Migration concerns, backward-compat issues, things that could break.

## Guardrails
If guardrails were provided, list them verbatim here.

════════════════════════════════════════════════════════
QUALITY BAR:
════════════════════════════════════════════════════════
- Every step MUST have a "Nearby Code Reference" with a real pasted snippet.
- Every step MUST have an "Existing Function Check" with a documented search.
- Every "Change Convention" checklist must be derived from the actual snippet,
  not invented. Generic advice like "follow good practices" is not allowed.
- A developer reading only this plan — without looking at any other file —
  must know exactly what to write, character for character.
"""


async def cmd_devplan(
    cwd: str,
    kb_content: str | None,
    req_file: Path | None = None,
    out_file: Path | None = None,
    guardrails_override: str | None = None,
) -> None:
    """Tech-lead agent: ingest requirements doc → explore codebase → produce dev plan."""

    # ── Load requirements doc ─────────────────────────────────────────────────
    req_path = req_file if req_file else Path(cwd) / REQ_FILE
    if not req_path.exists():
        print(color(f"No requirements doc found at {req_path}", RED))
        print(color("Run `devex req` first to generate one.", DIM))
        return

    req_doc = req_path.read_text()
    print(color("━" * 54, CYAN))
    print(color("  devex — Tech Lead: Dev Plan", BOLD + CYAN))
    print(color("━" * 54, CYAN))
    print(color(f"  Requirements: {req_path.name}  ({len(req_doc.splitlines())} lines)", DIM))
    if kb_content:
        print(color(f"  KB loaded  ({kb_age_str(cwd)},  ~{estimate_tokens(kb_content):,} tokens)", DIM))
    else:
        print(color("  No KB — agent will rely solely on live file reads.", YELLOW))
    print()
    print(color("  Analysing codebase and writing dev plan…", DIM))
    print(color("  (the agent will read files — this may take a few minutes)", DIM))
    print()

    # ── Build system prompt ───────────────────────────────────────────────────
    guardrails    = guardrails_override if guardrails_override is not None else load_guardrails(cwd)
    kb_ctx        = f"\n\n<knowledge_base>\n{kb_content}\n</knowledge_base>" if kb_content else ""
    gr_ctx        = guardrails_block(guardrails)
    system_prompt = TECH_LEAD_PLANNER_SYSTEM + kb_ctx + gr_ctx

    if guardrails:
        print(color(f"  Guardrails loaded", DIM))

    # ── Build task prompt ─────────────────────────────────────────────────────
    task_prompt = f"""\
Here is the requirements document you must plan against:

<requirements>
{req_doc}
</requirements>

{f'<guardrails>{chr(10)}{guardrails}{chr(10)}</guardrails>{chr(10)}{chr(10)}' if guardrails else ''}\
Follow the MANDATORY PROCESS in your instructions:

1. Identify every file that will need to change.
2. For each file — BEFORE writing the plan step:
   a. Read the file around the insertion point (use the Read tool).
   b. Grep for any existing functions similar to what you need to add.
   c. Paste the real code snippet into the plan as "Nearby Code Reference".
   d. Derive the Change Convention checklist from that snippet.
3. Produce the complete dev plan document following the structure in your \
   instructions exactly.
"""

    opts = make_opts(
        cwd=cwd,
        tools=["Read", "Glob", "Grep"],
        permission_mode="default",
        system_prompt=system_prompt,
        max_turns=150,
    )

    # ── Stream the plan ───────────────────────────────────────────────────────
    plan_text = ""
    try:
        async for message in query(prompt=task_prompt, options=ClaudeAgentOptions(**opts)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
                        plan_text += block.text
            elif isinstance(message, ResultMessage):
                pass  # let loop finish naturally
    except KeyboardInterrupt:
        print(color("\n[interrupted]", YELLOW))
        if not plan_text.strip():
            return

    print()

    if not plan_text.strip():
        print(color("Agent produced no output.", RED))
        return

    # ── Save ──────────────────────────────────────────────────────────────────
    dest = out_file if out_file else Path(cwd) / DEVPLAN_FILE
    dest.write_text(
        f"<!-- Generated: {datetime.now().isoformat()} -->\n"
        f"<!-- Requirements: {req_path.name} -->\n\n"
        + plan_text
    )
    print(color(f"\n━━  Dev plan saved → {dest}", GREEN + BOLD))


# ── Developer agent ───────────────────────────────────────────────────────────

DEVELOPER_SYSTEM = """\
You are a senior software developer implementing a feature from a detailed \
dev plan produced by the tech lead.

You are given:
  1. A requirements document — what needs to be built.
  2. A dev plan — contains step-by-step instructions, AND for each step:
       • "Nearby Code Reference" — the actual code snippet from the file
         surrounding your insertion point.
       • "Change Convention" — a checklist derived from that snippet.
       • "Existing Function Check" — whether to reuse, extend, or create.
  3. A knowledge base summarising the codebase.
  4. Full tool access: Read / Glob / Grep / Write / Edit / Bash.

════════════════════════════════════════════════════════
CONVENTION RULE — non-negotiable:
════════════════════════════════════════════════════════
The dev plan's "Nearby Code Reference" and "Change Convention" checklist for
each step are LAW. Every piece of code you write must match them exactly:
  - Same naming style (variables, functions, classes).
  - Same error-handling pattern.
  - Same import style.
  - Same helper functions and utilities already in use nearby.
  - Same indentation, spacing, and formatting style.

IF YOU MUST DEVIATE from what the plan specifies — e.g. the plan references
a function that doesn't exist, the signature is wrong, or there is a genuine
technical blocker — you MUST mark it:

  ##REVIEW## <reason for deviation in one sentence>

Place this comment on the line immediately BEFORE the deviating code block.
One ##REVIEW## per deviation. Do not use it for minor stylistic choices.

════════════════════════════════════════════════════════
WORKFLOW — follow in order:
════════════════════════════════════════════════════════

STEP 0 — Create branch
  git checkout -b <branch-name>
  Use the branch name from the task prompt. If not given, derive from the
  task title in kebab-case (e.g. "devex/add-stripe-subscriptions").

STEP 1 — Study the plan
  Read the dev plan in full before writing any code.
  For each step, re-read the "Nearby Code Reference" snippet so it is fresh
  in mind when you make that change.
  Verify that every file path and function name referenced actually exists
  (use Read / Grep). Note discrepancies — those are ##REVIEW## candidates.

STEP 2 — Implement each step in order
  For each Implementation Step in the plan:
    a. Use the Read tool to open the exact file and lines shown in
       "Nearby Code Reference" — confirm the snippet is still accurate.
    b. Check "Existing Function Check":
         REUSE  → call the existing function, do not rewrite it.
         EXTEND → add a parameter or subclass, do not duplicate logic.
         CREATE → write the new function following the Change Convention.
    c. Write the code matching every item in the "Change Convention" checklist.
    d. If you cannot match a checklist item, add ##REVIEW## before the block.
    e. Commit after each step:
         git add <changed files>
         git commit -m "<commit message from the plan>"

STEP 3 — Tests
  Follow the "Testing Plan" section. Mirror the existing test file structure
  shown in the plan. Commit: git commit -m "test: <description>"

STEP 4 — Final check
  Run the test suite / linter if configured \
  (check package.json / Makefile / pyproject.toml).
  Fix failures. Do not silence them.

════════════════════════════════════════════════════════
HARD RULES:
════════════════════════════════════════════════════════
  - Never skip a step or a checklist item without ##REVIEW##.
  - Never modify files not listed in the plan without ##REVIEW##.
  - Never refactor or "improve" code outside the change scope.
  - One commit per implementation step — small, focused.
  - If a guardrails block is present, treat every item as an absolute
    prohibition. Violating a guardrail requires ##REVIEW## AND a TODO.
"""


def _branch_name_from_doc(doc_text: str) -> str:
    """Extract task title from requirements or dev plan doc and convert to kebab-case."""
    for line in doc_text.splitlines():
        line = line.strip()
        if line.startswith("# "):
            title = line.lstrip("# ").strip()
            # remove "Dev Plan:" / "Requirements:" prefix if present
            for prefix in ("Dev Plan:", "Requirements:", "dev plan:", "requirements:"):
                if title.lower().startswith(prefix.lower()):
                    title = title[len(prefix):].strip()
            # kebab-case
            import re
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            return f"devex/{slug}"[:60]
    return "devex/feature"


async def cmd_dev(
    cwd: str,
    kb_content: str | None,
    branch: str | None,
    auto: bool,
    no_review: bool = False,
    max_iterations: int = 10,
    devplan_file: Path | None = None,
    req_file: Path | None = None,
    reviews_dir: Path | None = None,
    guardrails_override: str | None = None,
) -> str | None:
    """Developer agent: reads dev plan → creates branch → implements code. Returns branch name."""

    # ── Load docs ─────────────────────────────────────────────────────────────
    devplan_path = devplan_file if devplan_file else Path(cwd) / DEVPLAN_FILE
    req_path     = req_file     if req_file     else Path(cwd) / REQ_FILE

    if not devplan_path.exists():
        print(color(f"No dev plan found at {devplan_path}", RED))
        print(color("Run `devex plan` first.", DIM))
        return None

    devplan_doc = devplan_path.read_text()
    req_doc     = req_path.read_text() if req_path.exists() else ""

    # ── Resolve branch name ───────────────────────────────────────────────────
    if not branch:
        branch = _branch_name_from_doc(req_doc or devplan_doc)

    # ── Header ────────────────────────────────────────────────────────────────
    print(color("━" * 54, CYAN))
    print(color("  devex — Developer Agent", BOLD + CYAN))
    print(color("━" * 54, CYAN))
    print(color(f"  Dev plan : {devplan_path.name}  ({len(devplan_doc.splitlines())} lines)", DIM))
    print(color(f"  Branch   : {branch}", DIM))
    print(color(f"  Mode     : {'bypass (auto)' if auto else 'acceptEdits'}", DIM))
    if kb_content:
        print(color(f"  KB       : ~{estimate_tokens(kb_content):,} tokens", DIM))
    print()

    if not auto:
        print(color("  The agent will edit files and run git commands.", YELLOW))
        print(color("  Pass --auto to skip all prompts.", DIM))
        try:
            confirm = await anyio.to_thread.run_sync(
                lambda: input(color("  Proceed? [y/N] ", BOLD))
            )
        except (EOFError, KeyboardInterrupt):
            print(color("\nCancelled.", DIM))
            return None
        if confirm.strip().lower() != "y":
            print(color("Cancelled.", DIM))
            return None

    print()

    # ── Build system prompt ───────────────────────────────────────────────────
    guardrails    = guardrails_override if guardrails_override is not None else load_guardrails(cwd)
    kb_ctx        = f"\n\n<knowledge_base>\n{kb_content}\n</knowledge_base>" if kb_content else ""
    gr_ctx        = guardrails_block(guardrails)
    system_prompt = DEVELOPER_SYSTEM + kb_ctx + gr_ctx

    if guardrails:
        print(color(f"  Guardrails : loaded", DIM))

    # ── Build task prompt ─────────────────────────────────────────────────────
    task_prompt = f"""\
Branch to create: {branch}

{f'<requirements>{chr(10)}{req_doc}{chr(10)}</requirements>{chr(10)}{chr(10)}' if req_doc else ''}\
<dev_plan>
{devplan_doc}
</dev_plan>

{f'<guardrails>{chr(10)}{guardrails}{chr(10)}</guardrails>{chr(10)}{chr(10)}' if guardrails else ''}\
Begin with STEP 0 (create the branch) and work through every step in order.
"""

    opts = make_opts(
        cwd=cwd,
        tools=["Read", "Glob", "Grep", "Write", "Edit", "Bash"],
        permission_mode="bypassPermissions" if auto else "acceptEdits",
        max_turns=150,
        system_prompt=system_prompt,
    )

    # ── Stream implementation ─────────────────────────────────────────────────
    try:
        async for message in query(prompt=task_prompt, options=ClaudeAgentOptions(**opts)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        print(block.text, end="", flush=True)
            elif isinstance(message, ResultMessage):
                pass  # let loop finish
    except KeyboardInterrupt:
        print(color("\n[interrupted — branch and partial commits preserved]", YELLOW))
        return branch

    print()
    print(color("━" * 54, GREEN))
    print(color(f"  ✓ Implementation complete on branch: {branch}", GREEN + BOLD))
    print(color("━" * 54, GREEN))

    if not no_review:
        await review_loop(
            cwd, kb_content, branch, auto, max_iterations,
            req_file=req_file,
            devplan_file=devplan_file,
            reviews_dir=reviews_dir,
            guardrails_override=guardrails_override,
        )

    return branch


# ── Reviewer agent ────────────────────────────────────────────────────────────

REVIEW_DIR = ".devex-reviews"

REVIEWER_SYSTEM = """\
You are a pragmatic senior code reviewer. Your job is to catch real problems, \
not to enforce imaginary best-practices that don't exist in this codebase.

You are given:
  1. The branch name whose changes to review.
  2. The requirements document — what was supposed to be built.
  3. The dev plan — how it was supposed to be built, including "Nearby Code
     Reference" snippets and "Change Convention" checklists per step.
  4. A knowledge base of the codebase.
  5. Full tool access — Bash for git diff and tests, Read/Grep to inspect files.

════════════════════════════════════════════════════════
REVIEW PROCESS
════════════════════════════════════════════════════════

Step 1 — Get the diff
  git diff $(git merge-base main HEAD) HEAD
  (fall back to origin/main or the default branch if main doesn't exist)

Step 2 — Read the EXISTING codebase around every changed location
  For each changed file, use the Read tool to read the surrounding unchanged
  code — the 20–40 lines before and after the diff hunk. This is your ground
  truth for what the convention actually is in this codebase.

Step 3 — Run tests
  If a test command is configured (Makefile / pyproject.toml / package.json),
  run it. Report actual failures — do not speculate about possible failures.

Step 4 — Check requirements
  Verify each "Acceptance Criteria" item from the requirements doc is met.

Step 5 — Convention check (READ THE NEARBY CODE FIRST)
  Compare the new code against the plan's "Change Convention" checklist AND
  against the surrounding unchanged code you read in Step 2.
  The nearby existing code is the authoritative standard — not textbook rules.

Step 6 — ##REVIEW## markers
  Find every ##REVIEW## comment in the diff. Check whether the stated reason
  is valid. Flag unjustified deviations as [ERROR].

════════════════════════════════════════════════════════
WHAT TO FLAG AND WHAT NOT TO FLAG
════════════════════════════════════════════════════════

FLAG as [ERROR] — things that are genuinely broken:
  • Functional bugs: wrong logic, off-by-one, missing case the requirements demand.
  • Test failures reported by the test runner.
  • Missing acceptance criteria.
  • Guardrail violations (if guardrails are provided).
  • Unjustified ##REVIEW## deviations from the plan.

FLAG as [WARNING] — only if the surrounding existing code does it differently:
  • Naming that clashes with the pattern used in the surrounding unchanged code.
  • Error handling that is inconsistent with how the same file handles errors
    elsewhere (verified by reading the file, not assumed).
  • Missing test for a case that the existing test file clearly covers for
    similar functions.

DO NOT FLAG:
  • Additional error handling or validation that doesn't exist in the nearby
    code — unless the requirements or guardrails explicitly forbid it. New
    standalone files or new logic modules may have stricter handling than the
    surrounding legacy code; this is acceptable.
  • Theoretical improvements ("you could also…", "it would be safer if…").
  • Style opinions not grounded in the actual surrounding code you read.
  • Abstractions, helper functions, or patterns the existing code doesn't use —
    unless the plan's Change Convention checklist required them.
  • Anything the existing codebase itself does not do in similar places.

The rule is: if the rest of this codebase does not do X, do not require X.
Read the code. Judge by what is there, not what could be there.

════════════════════════════════════════════════════════
RESPONSE FORMAT — exactly one of these two, nothing else:
════════════════════════════════════════════════════════

Format A — no real issues found:
  APPROVED

  That is the entire response. No explanation, no "all requirements are met",
  no summary. Just the single word APPROVED on its own line.

Format B — real issues found:
  ISSUES:
  1. [ERROR] path/to/file.py:line — precise, factual description
  2. [WARNING] path/to/file.py:line — grounded in nearby code evidence
  ...
  SUMMARY:
  One paragraph: overall state and what must be fixed.

CRITICAL: if you have no [ERROR] or [WARNING] items to report, you MUST
respond with Format A (APPROVED). Do not write prose, do not write
"ISSUES: None found", do not explain that everything is fine. Just: APPROVED
"""

DEVELOPER_FIX_SYSTEM = """\
You are a senior software developer fixing issues identified by a code reviewer.

You are given:
  1. The reviewer's issue log listing every problem to fix.
  2. The original dev plan for context on intended behaviour.
  3. A knowledge base of the codebase.
  4. Full tool access to read and modify files and run git commands.

Fix every issue listed — no skipping. For each fix:
  a. Read the affected file before modifying it.
  b. Make the minimal targeted change that resolves the issue.
  c. Do not refactor unrelated code.
  d. After fixing all issues in a logical group, commit:
       git add <files>
       git commit -m "fix: <brief description>"

After all fixes are committed, respond with a short summary of what you changed.
"""


def _review_log_path(cwd: str, iteration: int) -> Path:
    d = Path(cwd) / REVIEW_DIR
    d.mkdir(exist_ok=True)
    return d / f"iterate-{iteration}.md"


_APPROVAL_PHRASES = (
    "none found",
    "no issues",
    "no fixes",
    "no problems",
    "no errors",
    "all requirements are met",
    "code review passed",
    "approved",
    "lgtm",
)

def _is_approval(response: str) -> bool:
    """Return True if the response is effectively an approval, regardless of exact wording."""
    text = response.strip()
    text_up = text.upper()
    first_line = text_up.splitlines()[0].strip() if text_up else ""

    # Exact APPROVED on first line
    if first_line == "APPROVED":
        return True

    # Any [ERROR] or [WARNING] → definitely not approved
    if "[ERROR]" in text_up or "[WARNING]" in text_up:
        return False

    # "ISSUES: None" / "ISSUES: None found" on the first line
    if first_line.startswith("ISSUES:"):
        remainder = first_line[len("ISSUES:"):].strip()
        if remainder in ("NONE", "NONE FOUND", "NONE.", "N/A"):
            return True
        # Empty remainder = section header; fall through to phrase scan

    # Scan full text for approval phrases (only reached when no [ERROR]/[WARNING])
    for phrase in _APPROVAL_PHRASES:
        if phrase in text.lower():
            return True

    return False


def _parse_reviewer(response: str) -> tuple[bool, list[str], str]:
    """
    Returns (approved, issues_list, summary).
    approved=True means APPROVED with no issues.
    """
    if _is_approval(response):
        return True, [], ""

    issues: list[str] = []
    summary_lines: list[str] = []
    in_issues  = False
    in_summary = False

    for line in response.splitlines():
        s = line.strip()
        if s.upper().startswith("ISSUES:"):
            in_issues = True
            # handle inline "ISSUES: <text>" (not a section header)
            inline = s[s.upper().index("ISSUES:") + len("ISSUES:"):].strip()
            if inline and inline.upper() not in ("NONE", "NONE FOUND", "N/A"):
                issue = inline.lstrip("0123456789.-) ").strip()
                if issue:
                    issues.append(issue)
            continue
        if s.upper().startswith("SUMMARY:"):
            in_issues  = False
            in_summary = True
            inline = s[s.upper().index("SUMMARY:") + len("SUMMARY:"):].strip()
            if inline:
                summary_lines.append(inline)
            continue
        if in_issues and s:
            issue = s.lstrip("0123456789.-) *#").strip()
            if issue:
                issues.append(issue)
        elif in_summary and s:
            summary_lines.append(s)

    # If we parsed an ISSUES block but found zero real issues, treat as approved
    if not issues:
        return True, [], " ".join(summary_lines)

    return False, issues, " ".join(summary_lines)


async def _run_reviewer(
    cwd: str,
    kb_content: str | None,
    branch: str,
    iteration: int,
    req_file: Path | None = None,
    devplan_file: Path | None = None,
    guardrails_override: str | None = None,
) -> tuple[bool, list[str], str, str]:
    """
    Run the reviewer agent.
    Returns (approved, issues, summary, full_response).
    """
    devplan_path = devplan_file if devplan_file else Path(cwd) / DEVPLAN_FILE
    req_path     = req_file     if req_file     else Path(cwd) / REQ_FILE

    guardrails  = guardrails_override if guardrails_override is not None else load_guardrails(cwd)
    kb_ctx      = f"\n\n<knowledge_base>\n{kb_content}\n</knowledge_base>" if kb_content else ""
    gr_ctx      = guardrails_block(guardrails)
    devplan_doc = devplan_path.read_text() if devplan_path.exists() else ""
    req_doc     = req_path.read_text()     if req_path.exists()     else ""

    system_prompt = REVIEWER_SYSTEM + kb_ctx + gr_ctx

    task_prompt = (
        f"Branch to review: {branch}\n\n"
        + (f"<requirements>\n{req_doc}\n</requirements>\n\n" if req_doc else "")
        + (f"<dev_plan>\n{devplan_doc}\n</dev_plan>\n\n"     if devplan_doc else "")
        + (f"<guardrails>\n{guardrails}\n</guardrails>\n\n"  if guardrails else "")
        + "Begin your review now. Pay attention to guardrails if provided — flag any violations."
    )

    opts = make_opts(
        cwd=cwd,
        tools=["Read", "Glob", "Grep", "Bash"],
        permission_mode="default",
        system_prompt=system_prompt,
        max_turns=60,
    )

    print(color(f"\n  [reviewer — iteration {iteration}…]", DIM))
    print(color("  Reading diff and files…", DIM))
    print()

    full_response = ""
    async for message in query(prompt=task_prompt, options=ClaudeAgentOptions(**opts)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
                    full_response += block.text
        # no early return — let loop finish

    print()
    approved, issues, summary = _parse_reviewer(full_response)
    return approved, issues, summary, full_response


async def _run_dev_fix(
    cwd: str,
    kb_content: str | None,
    branch: str,
    issues: list[str],
    summary: str,
    iteration: int,
    auto: bool,
    devplan_file: Path | None = None,
    log_path_override: Path | None = None,
    guardrails_override: str | None = None,
) -> None:
    """Run the developer agent to fix reviewer issues."""
    devplan_path = devplan_file if devplan_file else Path(cwd) / DEVPLAN_FILE
    devplan_doc  = devplan_path.read_text() if devplan_path.exists() else ""

    guardrails    = guardrails_override if guardrails_override is not None else load_guardrails(cwd)
    kb_ctx        = f"\n\n<knowledge_base>\n{kb_content}\n</knowledge_base>" if kb_content else ""
    gr_ctx        = guardrails_block(guardrails)
    issues_block  = "\n".join(f"{i+1}. {issue}" for i, issue in enumerate(issues))
    log_path      = log_path_override if log_path_override else _review_log_path(cwd, iteration)

    system_prompt = DEVELOPER_FIX_SYSTEM + kb_ctx + gr_ctx

    task_prompt = f"""\
You are on branch: {branch}

The reviewer found these issues in iteration {iteration}:

ISSUES:
{issues_block}

SUMMARY:
{summary}

Review log saved at: {log_path}

{f'<dev_plan>{chr(10)}{devplan_doc}{chr(10)}</dev_plan>' if devplan_doc else ''}

{f'<guardrails>{chr(10)}{guardrails}{chr(10)}</guardrails>' if guardrails else ''}

Fix every issue above, commit your fixes, then summarise what you changed.
"""

    opts = make_opts(
        cwd=cwd,
        tools=["Read", "Glob", "Grep", "Write", "Edit", "Bash"],
        permission_mode="bypassPermissions" if auto else "acceptEdits",
        max_turns=100,
        system_prompt=system_prompt,
    )

    print(color(f"\n  [developer — fixing iteration {iteration} issues…]", DIM))
    print()

    async for message in query(prompt=task_prompt, options=ClaudeAgentOptions(**opts)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    print(block.text, end="", flush=True)
        # no early return


async def review_loop(
    cwd: str,
    kb_content: str | None,
    branch: str,
    auto: bool,
    max_iterations: int = 10,
    req_file: Path | None = None,
    devplan_file: Path | None = None,
    reviews_dir: Path | None = None,
    guardrails_override: str | None = None,
) -> bool:
    """Reviewer → fix loop until approved or max_iterations reached. Returns True if approved."""

    print()
    print(color("━" * 54, CYAN))
    print(color("  devex — Code Review", BOLD + CYAN))
    print(color("━" * 54, CYAN))
    print(color(f"  Branch: {branch}  ·  max iterations: {max_iterations}", DIM))

    def _iter_log_path(iteration: int) -> Path:
        if reviews_dir:
            reviews_dir.mkdir(parents=True, exist_ok=True)
            return reviews_dir / f"iterate-{iteration}.md"
        return _review_log_path(cwd, iteration)

    for iteration in range(1, max_iterations + 1):
        approved, issues, summary, full_response = await _run_reviewer(
            cwd, kb_content, branch, iteration,
            req_file=req_file,
            devplan_file=devplan_file,
            guardrails_override=guardrails_override,
        )

        # ── Save review log ────────────────────────────────────────────────
        log_path  = _iter_log_path(iteration)
        guardrails_note = guardrails_override if guardrails_override is not None else load_guardrails(cwd)
        log_content = (
            f"# Review — Iteration {iteration}\n"
            f"**Branch:** {branch}\n"
            f"**Date:** {datetime.now().isoformat()}\n"
            f"**Status:** {'APPROVED' if approved else 'ISSUES FOUND'}\n\n"
            + (f"## Guardrails\n{guardrails_note}\n\n" if guardrails_note else "")
        )
        if not approved:
            log_content += "## Issues\n"
            for i, issue in enumerate(issues, 1):
                log_content += f"{i}. {issue}\n"
            log_content += f"\n## Summary\n{summary}\n\n"
            log_content += f"## Full Reviewer Response\n\n{full_response}\n"
        else:
            log_content += "No issues found. Code approved.\n"

        log_path.write_text(log_content)
        print(color(f"\n  Review log → {log_path}", DIM))

        if approved:
            print()
            print(color("━" * 54, GREEN))
            print(color("  ✓ Code approved by reviewer!", GREEN + BOLD))
            print(color(f"  {log_path}", DIM))
            print(color("━" * 54, GREEN))
            return True

        # ── Issues found — report and fix ──────────────────────────────────
        print()
        print(color(f"  ✗ {len(issues)} issue(s) found — iteration {iteration}/{max_iterations}", RED + BOLD))
        for i, issue in enumerate(issues, 1):
            print(color(f"    {i}. {issue}", RED))
        if summary:
            print(color(f"\n  Summary: {summary}", YELLOW))

        if iteration == max_iterations:
            break

        await _run_dev_fix(
            cwd, kb_content, branch, issues, summary, iteration, auto,
            devplan_file=devplan_file,
            log_path_override=_iter_log_path(iteration),
            guardrails_override=guardrails_override,
        )
        print()

    # Max iterations exhausted
    print()
    print(color("━" * 54, YELLOW))
    print(color(f"  ⚠  Max iterations ({max_iterations}) reached without full approval.", YELLOW + BOLD))
    logs_loc = reviews_dir if reviews_dir else Path(cwd) / REVIEW_DIR
    print(color(f"  Review logs in: {logs_loc}", DIM))
    print(color("━" * 54, YELLOW))
    return False


# ── Guardrails command ────────────────────────────────────────────────────────

def cmd_guardrails(cwd: str) -> None:
    """Display the current guardrails for this repo."""
    gr = load_guardrails(cwd)
    p  = guardrails_path(cwd)
    if not gr:
        print(color(f"No guardrails found at {p}", DIM))
        print(color("Run `devex req` to capture guardrails during the requirements flow.", DIM))
        return
    print(color("━" * 54, CYAN))
    print(color("  devex — Guardrails", BOLD + CYAN))
    print(color(f"  {p}", DIM))
    print(color("━" * 54, CYAN))
    print()
    print(gr)
    print()


# ── Task queue commands ───────────────────────────────────────────────────────

async def cmd_task(cwd: str, kb_content: str | None) -> None:
    """Capture requirements for one task and queue it for later execution."""
    task_id = _next_task_id(cwd)
    tdir    = _task_dir(cwd, task_id)
    tdir.mkdir(parents=True, exist_ok=True)

    print(color("━" * 54, CYAN))
    print(color(f"  devex — New Task  (id: {task_id})", BOLD + CYAN))
    print(color("━" * 54, CYAN))
    print()

    await cmd_requirements(cwd, kb_content, save_dir=tdir)

    # Pull title from the generated requirements doc
    req_path = tdir / "requirements.md"
    title    = "Untitled Task"
    if req_path.exists():
        title = _extract_doc_title(req_path.read_text())

    _save_task(cwd, task_id, {
        "id":         task_id,
        "title":      title,
        "branch":     None,
        "status":     "pending",
        "created_at": datetime.now().isoformat(),
    })

    print()
    print(color(f"  ✓ Task {task_id} queued: {title}", GREEN + BOLD))
    print(color(f"  Run `devex execute` to process the queue.", DIM))


def cmd_task_list(cwd: str) -> None:
    """List all queued tasks with their status."""
    tasks = _list_tasks(cwd)
    if not tasks:
        print(color("No tasks found. Run `devex task` to add one.", DIM))
        return

    STATUS_COLORS = {
        "pending":  YELLOW,
        "planning": CYAN,
        "running":  CYAN,
        "done":     GREEN,
        "failed":   RED,
    }

    print(color("━" * 60, CYAN))
    print(color("  devex — Task Queue", BOLD + CYAN))
    print(color("━" * 60, CYAN))
    print(color(f"  {'ID':<6}  {'Status':<10}  {'Branch':<28}  Title", BOLD))
    print(color("  " + "─" * 56, DIM))
    for t in tasks:
        tid    = t.get("id", "?")
        status = t.get("status", "?")
        branch = t.get("branch") or "—"
        title  = t.get("title", "Untitled")[:38]
        sc     = STATUS_COLORS.get(status, DIM)
        print(
            f"  {color(tid, BOLD):<6}  "
            f"{color(status, sc):<10}  "
            f"{color(branch[:28], DIM):<28}  "
            f"{title}"
        )
    print(color("━" * 60, CYAN))


def _git_current_branch(cwd: str) -> str:
    import subprocess
    r = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=cwd, capture_output=True, text=True,
    )
    return r.stdout.strip() or "main"


def _git_checkout(cwd: str, branch: str) -> bool:
    import subprocess
    r = subprocess.run(
        ["git", "checkout", branch],
        cwd=cwd, capture_output=True, text=True,
    )
    return r.returncode == 0


async def cmd_execute(cwd: str, kb_content: str | None) -> None:
    """Pick pending tasks from the queue and run plan → dev --auto for each."""
    tasks = _list_tasks(cwd)
    pending = [t for t in tasks if t.get("status") == "pending"]

    if not pending:
        print(color("No pending tasks. Run `devex task` to add one.", DIM))
        return

    # Record the current branch — every task branches from here
    base_branch = _git_current_branch(cwd)

    print(color("━" * 54, CYAN))
    print(color(f"  devex — Execute Queue  ({len(pending)} pending)", BOLD + CYAN))
    print(color(f"  Base branch: {base_branch}", DIM))
    print(color("━" * 54, CYAN))

    for task_meta in pending:
        task_id = task_meta["id"]
        title   = task_meta.get("title", "Untitled Task")
        tdir    = _task_dir(cwd, task_id)

        print()
        print(color(f"  ▶ Task {task_id}: {title}", BOLD + CYAN))
        print(color(f"    Returning to base branch: {base_branch}", DIM))

        # Always start each task from the base branch
        if not _git_checkout(cwd, base_branch):
            print(color(f"  ✗ Could not checkout {base_branch} — skipping task.", RED))
            _save_task(cwd, task_id, {**task_meta, "status": "failed"})
            continue

        print()

        req_file    = tdir / "requirements.md"
        devplan_file = tdir / "devplan.md"
        reviews_dir  = tdir / "reviews"
        gr_file      = tdir / "guardrails.md"

        # Load guardrails from task dir (fallback to repo-level)
        guardrails_override = (
            gr_file.read_text().strip() if gr_file.exists() else load_guardrails(cwd)
        )

        # ── Phase 1: plan ─────────────────────────────────────────────────
        _save_task(cwd, task_id, {**task_meta, "status": "planning"})
        print(color("  Phase 1/2: Planning…", DIM))

        try:
            await cmd_devplan(
                cwd, kb_content,
                req_file=req_file,
                out_file=devplan_file,
                guardrails_override=guardrails_override,
            )
        except Exception as exc:
            print(color(f"  ✗ Planning failed: {exc}", RED))
            _save_task(cwd, task_id, {**task_meta, "status": "failed"})
            continue

        if not devplan_file.exists():
            print(color("  ✗ Dev plan not generated — skipping.", RED))
            _save_task(cwd, task_id, {**task_meta, "status": "failed"})
            continue

        # ── Phase 2: implement ────────────────────────────────────────────
        _save_task(cwd, task_id, {**task_meta, "status": "running"})
        print(color("  Phase 2/2: Implementing…", DIM))

        try:
            branch = await cmd_dev(
                cwd, kb_content,
                branch=None,
                auto=True,
                no_review=False,
                devplan_file=devplan_file,
                req_file=req_file,
                reviews_dir=reviews_dir,
                guardrails_override=guardrails_override,
            )
        except Exception as exc:
            print(color(f"  ✗ Implementation failed: {exc}", RED))
            _save_task(cwd, task_id, {**task_meta, "status": "failed"})
            continue

        # ── Save result summary ───────────────────────────────────────────
        result_path = tdir / "result.md"
        result_path.write_text(
            f"# Result — Task {task_id}\n\n"
            f"**Title:** {title}\n"
            f"**Base branch:** {base_branch}\n"
            f"**Feature branch:** {branch or 'unknown'}\n"
            f"**Completed:** {datetime.now().isoformat()}\n\n"
            f"Implementation complete. See branch `{branch or 'unknown'}` for changes.\n"
        )

        _save_task(cwd, task_id, {
            **task_meta,
            "status": "done",
            "branch": branch,
            "base_branch": base_branch,
        })

        print()
        print(color(f"  ✓ Task {task_id} done — branch: {branch}", GREEN + BOLD))

    # Return to base branch when all tasks are done
    _git_checkout(cwd, base_branch)

    print()
    print(color("━" * 54, GREEN))
    print(color("  ✓ Queue execution complete.", GREEN + BOLD))
    print(color(f"  Returned to base branch: {base_branch}", DIM))
    print(color("━" * 54, GREEN))


# ── Interactive REPL ──────────────────────────────────────────────────────────

async def cmd_interactive(base_opts: dict, kb_content: str | None, cwd: str) -> None:
    print(color("devex", BOLD + CYAN))
    if kb_content:
        kb_tokens = estimate_tokens(kb_content)
        print(
            color(f"Knowledge base loaded  ({kb_age_str(cwd)})  ", GREEN)
            + color(f"~{kb_tokens:,} tokens  ({kb_tokens / CONTEXT_WINDOW * 100:.1f}% of window)", DIM)
        )
    else:
        print(color("No knowledge base — run `devex scan` to build one.", YELLOW))

    warn = context_warning(cwd)
    if warn:
        print(warn)

    print(color("Commands: exit · quit · new · sessions · stats · req · plan · dev · guardrails · task · tasks · execute", DIM))
    print(color("─" * 52, DIM))

    session_id: str | None = None

    while True:
        try:
            prompt = await anyio.to_thread.run_sync(
                lambda: input(color("\n> ", BOLD + GREEN))
            )
            prompt = prompt.strip()
        except (EOFError, KeyboardInterrupt):
            print(color("\nGoodbye!", DIM))
            break

        if not prompt:
            continue
        if prompt.lower() in ("exit", "quit"):
            print(color("Goodbye!", DIM))
            break
        if prompt.lower() == "new":
            session_id = None
            print(color("[new session]", DIM))
            continue
        if prompt.lower() == "sessions":
            _print_sessions()
            continue
        if prompt.lower() == "stats":
            cmd_stats(cwd)
            continue
        if prompt.lower() in ("req", "requirements"):
            await cmd_requirements(cwd, kb_content)
            continue
        if prompt.lower() in ("plan", "devplan"):
            await cmd_devplan(cwd, kb_content)
            continue
        if prompt.lower() == "guardrails":
            cmd_guardrails(cwd)
            continue
        if prompt.lower() == "task":
            await cmd_task(cwd, kb_content)
            continue
        if prompt.lower() in ("tasks", "queue"):
            cmd_task_list(cwd)
            continue
        if prompt.lower() == "execute":
            await cmd_execute(cwd, kb_content)
            continue
        if prompt.lower().startswith("dev"):
            parts = prompt.split()
            branch = parts[1] if len(parts) > 1 else None
            await cmd_dev(cwd, kb_content, branch=branch, auto=False)
            continue

        turn_opts = {**base_opts}
        if session_id:
            turn_opts["resume"] = session_id

        print()
        new_sid, _ = await run_query(prompt, turn_opts, cwd, kb_content)
        if new_sid:
            session_id = new_sid

# ── Session listing ───────────────────────────────────────────────────────────

def _print_sessions(limit: int = 20) -> None:
    sessions = list_sessions()
    if not sessions:
        print(color("No sessions found.", DIM))
        return
    print(color(f"{'Session ID':<38}  Directory", BOLD))
    print(color("─" * 70, DIM))
    for s in sessions[:limit]:
        print(f"{color(s.session_id[:36], CYAN)}  {s.cwd or ''}")

# ── Argument parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="devex",
        description="Repo-aware interactive CLI powered by the Claude Agent SDK",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  devex scan                         build / refresh knowledge base
  devex stats                        show context-window usage history
  devex                              interactive mode  (uses KB)
  devex -p "how does auth work?"    single query      (uses KB)
  devex --no-kb -p "read auth.py"   skip KB, read files live
  devex --list-sessions             list past sessions
        """,
    )
    p.add_argument("--scan", action="store_true",
        help="Scan repo and build/refresh the knowledge base")
    p.add_argument("--stats", action="store_true",
        help="Show context-window usage history")
    p.add_argument("--req", action="store_true",
        help="Start requirements capture flow")
    p.add_argument("--guardrails", action="store_true",
        help="Show current guardrails for this repo")
    p.add_argument("--plan", action="store_true",
        help="Tech lead: analyse codebase and produce dev plan from requirements doc")
    p.add_argument("--dev", action="store_true",
        help="Developer agent: create branch and implement from dev plan")
    p.add_argument("--review", action="store_true",
        help="Run reviewer on an existing branch (standalone)")
    p.add_argument("--branch", metavar="NAME",
        help="Branch name for --dev / --review")
    p.add_argument("--auto", action="store_true",
        help="Skip all permission prompts in --dev mode (bypassPermissions)")
    p.add_argument("--no-review", action="store_true",
        help="Skip automatic review after --dev")
    p.add_argument("--task", action="store_true",
        help="Capture requirements for a new task and add it to the queue")
    p.add_argument("--tasks", action="store_true",
        help="List all queued tasks")
    p.add_argument("--execute", action="store_true",
        help="Execute pending tasks: plan → dev --auto for each")
    p.add_argument("--max-iter", type=int, default=10, metavar="N",
        help="Max review-fix iterations (default: 10)")
    p.add_argument("-p", "--prompt", metavar="TEXT",
        help="Single prompt (non-interactive mode)")
    p.add_argument("--no-kb", action="store_true",
        help="Ignore knowledge base; let the agent read files directly")
    p.add_argument("--tools", nargs="+", default=["Read", "Glob", "Grep", "Bash"],
        metavar="TOOL", help="Allowed tools (default: Read Glob Grep Bash)")
    p.add_argument("--cwd", default=os.getcwd(),
        help="Working directory (default: $PWD)")
    p.add_argument("--resume", metavar="SESSION_ID",
        help="Resume a previous session")
    p.add_argument("--list-sessions", action="store_true")
    p.add_argument("--permission-mode",
        choices=["default", "acceptEdits", "bypassPermissions", "plan"],
        default="default")
    p.add_argument("--system-prompt", metavar="TEXT",
        help="Custom system prompt (overrides KB prompt)")
    p.add_argument("--max-turns", type=int, metavar="N")
    return p

# ── Entry point ───────────────────────────────────────────────────────────────

SUBCOMMANDS = {
    "scan":         "--scan",
    "stats":        "--stats",
    "req":          "--req",
    "requirements": "--req",
    "plan":         "--plan",
    "devplan":      "--plan",
    "dev":          "--dev",
    "review":       "--review",
    "guardrails":   "--guardrails",
    "task":         "--task",
    "tasks":        "--tasks",
    "execute":      "--execute",
}

def main() -> None:
    # Allow bare subcommands: `devex scan` → `devex --scan`
    if len(sys.argv) > 1 and sys.argv[1] in SUBCOMMANDS:
        sys.argv[1] = SUBCOMMANDS[sys.argv[1]]

    args = build_parser().parse_args()

    # Ensure devex runtime files are ignored by git in the target repo
    _ensure_gitignore(args.cwd)

    if args.list_sessions:
        _print_sessions()
        return

    if args.scan:
        anyio.run(cmd_scan, args.cwd)
        return

    if args.stats:
        cmd_stats(args.cwd)
        return

    if args.guardrails:
        cmd_guardrails(args.cwd)
        return

    if args.req:
        kb_content = None if args.no_kb else load_kb(args.cwd)
        anyio.run(cmd_requirements, args.cwd, kb_content)
        return

    if args.plan:
        kb_content = None if args.no_kb else load_kb(args.cwd)
        anyio.run(cmd_devplan, args.cwd, kb_content)
        return

    if args.dev:
        kb_content = None if args.no_kb else load_kb(args.cwd)
        anyio.run(
            cmd_dev, args.cwd, kb_content, args.branch,
            args.auto, args.no_review, args.max_iter,
        )
        return

    if args.review:
        kb_content = None if args.no_kb else load_kb(args.cwd)
        if not args.branch:
            # use current git branch
            import subprocess
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=args.cwd, capture_output=True, text=True,
            )
            branch = result.stdout.strip() or "HEAD"
        else:
            branch = args.branch
        anyio.run(review_loop, args.cwd, kb_content, branch, args.auto, args.max_iter)
        return

    if args.task:
        kb_content = None if args.no_kb else load_kb(args.cwd)
        anyio.run(cmd_task, args.cwd, kb_content)
        return

    if args.tasks:
        cmd_task_list(args.cwd)
        return

    if args.execute:
        kb_content = None if args.no_kb else load_kb(args.cwd)
        anyio.run(cmd_execute, args.cwd, kb_content)
        return

    kb_content    = None if args.no_kb else load_kb(args.cwd)
    guardrails    = load_guardrails(args.cwd)
    system_prompt = build_system_prompt(kb_content, args.system_prompt, guardrails)

    base_opts = make_opts(
        cwd=args.cwd,
        tools=args.tools,
        permission_mode=args.permission_mode,
        system_prompt=system_prompt,
        max_turns=args.max_turns,
        resume=args.resume,
    )

    if args.prompt:
        anyio.run(run_query, args.prompt, base_opts, args.cwd, kb_content)
    else:
        anyio.run(cmd_interactive, base_opts, kb_content, args.cwd)


if __name__ == "__main__":
    main()
