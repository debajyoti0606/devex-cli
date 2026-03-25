# devex

An AI-powered developer workflow CLI. It scans your codebase into a knowledge base, then orchestrates a chain of Claude agents — requirements writer → tech lead → developer → reviewer — to implement features end-to-end with full codebase context.

---

## Setup

Clone the repo and run the one-time setup script:

```bash
git clone https://github.com/debajyoti0606/devex-cli
cd devex-cli
bash setup.sh
```

The script will:
1. Check Python ≥ 3.10
2. Install [pipx](https://pipx.pypa.io) if not present
3. Install `devex` globally via pipx (available in every terminal, no activation needed)
4. Create `.env` from `.env.example` and prompt for your API key

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | Your API key |
| `ANTHROPIC_BASE_URL` | No | Override API endpoint (e.g. a proxy) |
| `MODEL` | No | Override the model (default: `claude-opus-4-6`) |

### Updating after pulling new changes

```bash
pipx reinstall claude-cli
```

---

## Workflows

### Option A — single feature (interactive)

```
cd /your/project

devex scan          # 1. scan codebase → knowledge base + build config
devex req           # 2. write requirements doc
devex plan          # 3. tech lead produces a dev plan
devex dev           # 4. developer implements + auto-reviews
```

Each step builds on the previous one. The KB, requirements doc, and dev plan are saved as files in the project directory so you can inspect or edit them between steps.

### Option B — task queue (batch)

Queue up multiple features first, then let devex implement them one by one automatically:

```
cd /your/project

devex scan          # 1. scan codebase → knowledge base + build config
devex task          # 2. describe a feature → added to queue
devex task          # 2. describe another feature → added to queue
devex task          # 2. ... repeat for as many tasks as you want
devex tasks         # 3. review the queue
devex execute       # 4. implement all queued tasks: plan → dev → review, one by one
```

Use this when you have a batch of features or bug fixes to hand off. Each task goes through the full pipeline (requirements → dev plan → implementation → review) unattended.

---

## Commands

### `devex scan`
Scans the entire codebase and writes a structured knowledge base document (`DEVEX_KB.md`). Also detects build and test commands, confirms them with you, and saves them to `.devex-build.json`.

Run this first, and again whenever the codebase changes significantly.

```bash
devex scan
```

---

### `devex req` / `devex requirements`
Interactive requirements capture. A requirements-writer agent interviews you about what needs to be built and produces a structured requirements document.

```bash
devex req
```

---

### `devex plan` / `devex devplan`
Tech lead agent. Reads the requirements doc and the knowledge base, explores the codebase, and produces a detailed step-by-step dev plan with code references, convention checklists, and type placement rules.

```bash
devex plan
```

---

### `devex dev`
Developer agent. Reads the dev plan, creates a git branch, implements each step, runs build/tests, and hands off to the reviewer. Loops until the reviewer approves or the iteration limit is hit.

```bash
devex dev                          # prompts for branch name
devex dev --branch feat/my-thing   # specify branch up front
devex dev --auto                   # skip permission prompts
devex dev --no-review              # skip reviewer after implementation
devex dev --max-iter 5             # max 5 review-fix cycles (default: 10)
```

---

### `devex review`
Standalone reviewer agent. Runs against an existing branch without re-implementing anything. Checks the diff against requirements, conventions, and type placement rules.

```bash
devex review                       # reviews current git branch
devex review --branch feat/my-thing
```

---

### `devex task`
Capture requirements for a new task and add it to the task queue (`.devex-tasks.json`).

```bash
devex task
```

---

### `devex tasks`
List all queued tasks and their status.

```bash
devex tasks
```

---

### `devex execute`
Execute all pending tasks in the queue automatically — runs `plan → dev --auto` for each task.

```bash
devex execute
```

---

### `devex stats`
Show context-window usage history from past scans.

```bash
devex stats
```

---

### `devex guardrails`
Display the current guardrails file for the repo (things the developer agent must never do).

```bash
devex guardrails
```

---

### `devex` (interactive / query mode)
Chat directly with a KB-aware agent.

```bash
devex                              # interactive REPL
devex -p "how does auth work?"    # single query
devex --no-kb -p "read auth.py"  # skip KB, read files live
devex --resume <session-id>       # resume a previous session
devex --list-sessions             # list past sessions
```

---

## Flags

| Flag | Description |
|---|---|
| `--branch NAME` | Branch name for `--dev` / `--review` |
| `--auto` | Skip permission prompts (bypassPermissions) |
| `--no-review` | Skip reviewer after `--dev` |
| `--max-iter N` | Max review-fix iterations (default: 10) |
| `--no-kb` | Ignore knowledge base; let agent read files live |
| `-p / --prompt TEXT` | Single prompt (non-interactive) |
| `--cwd PATH` | Working directory (default: `$PWD`) |
| `--resume SESSION_ID` | Resume a previous agent session |
| `--list-sessions` | List past sessions |
| `--permission-mode` | `default` / `acceptEdits` / `bypassPermissions` / `plan` |
| `--tools TOOL ...` | Allowed tools (default: Read Glob Grep Bash) |
| `--max-turns N` | Max agent turns |
| `--system-prompt TEXT` | Override the system prompt |

---

## Project files

devex stores its runtime files in your project directory. All of them are automatically added to `.gitignore`.

| File | Description |
|---|---|
| `DEVEX_KB.md` | Knowledge base produced by `devex scan` |
| `DEVEX_REQUIREMENTS.md` | Requirements doc produced by `devex req` |
| `DEVEX_DEVPLAN.md` | Dev plan produced by `devex plan` |
| `DEVEX_GUARDRAILS.md` | Guardrails for this repo (edit manually) |
| `.devex-build.json` | Build and test commands (set during `devex scan`) |
| `.devex-tasks.json` | Task queue for `devex task` / `devex execute` |
| `.devex-stats.json` | Context-window usage history |

---

## How the agent pipeline works

```
devex scan
  └─ read-only analyst agent → DEVEX_KB.md
  └─ detect build/test commands → .devex-build.json

devex req
  └─ requirements-writer agent → DEVEX_REQUIREMENTS.md

devex plan
  └─ tech lead agent (reads KB + requirements, explores codebase)
       → DEVEX_DEVPLAN.md
         ├─ Nearby Code Reference per step (real code snippets)
         ├─ Change Convention checklist per step
         ├─ Existing Function Check (reuse / extend / create)
         └─ Type Check (types must go to designated type files)

devex dev
  └─ developer agent (reads KB + requirements + dev plan)
       → creates branch
       → implements each step, one commit per step
       → runs build + tests (must pass before handoff)
  └─ reviewer agent
       → checks diff vs requirements, conventions, type placement
       → APPROVED or ISSUES list
  └─ if ISSUES → developer fix agent → reviewer again (up to --max-iter)
```

The reviewer will never flag style opinions unless the surrounding codebase does it differently — it judges by what is actually in the code, not textbook rules.
