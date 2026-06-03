<div align="center">

# Mach

**Local-first, Git-adjacent execution ledger for AI agents.**

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Restricted_Mach-eb4034)](LICENSE.md)

⭐ Star the repo if you find it useful!
Sign up for early access → [cognatoai.com](https://cognatoai.com)

</div>

## 📖 What is Mach?

Mach is a high-performance execution tracking system for AI agents. It seamlessly intercepts and logs AI reasoning, inputs, tool calls, and outputs. By sitting right beside your Git repository, Mach provides a cryptographically verifiable, searchable, and structured history of *everything* your AI assistants do.

> [!NOTE]
> **CLI Agents Only:** Currently, Mach only supports intercepting terminal-based AI agents (like Claude Code, Aider, or Copilot CLI). GUI-based IDE agents (like the native Cursor or VSCode extensions) are not yet fully supported for automatic hook tracking.

## ✨ Core Architecture

Mach is built for uncompromising speed, durability, and a native developer experience:

- **Git-Style Blob Storage:** Massive AI outputs and prompts are hashed and deduplicated into a native blob store (`.mach/blobs/`). This keeps your core JSONL logs ultra-lightweight and blazingly fast to parse.
- **Lightning TUI & Search:** Drop into a premium, interactive terminal dashboard (`mach log`). Press `/` at any time to execute real-time, instantaneous searches across thousands of AI events and code chunks.
- **Hybrid Indexing (Toggleable):** Mach uses a fast SQLite FTS5 index (`.mach/index.db`) for sub-millisecond queries. Running on a constrained system? Toggle it off via `--db-enabled false` and Mach will gracefully degrade to pure file-system blob traversal, just like Git.
- **Zero-Latency Ingestion:** AI events are fired into an asynchronous inbox. A lightweight background daemon processes them into the ledger, ensuring 0ms latency impact on your actual AI workflows.
- **Seamless Hooks:** Automatically installs intercepts for terminal-based CLI agents (Claude Code, Copilot CLI, Gemini, Codex, etc).

## 🚀 Installation

Mach requires Python 3.9+ and can be installed via professional package managers or a standalone installer.

### Option 1: Standalone Script (Recommended)
This is the fastest way to get started. It securely sets up an isolated Mach environment.

```bash
# Install Mach globally
curl -fsSL https://raw.githubusercontent.com/cognato-ai/mach/master/install.sh | bash
```
To update later, you can just run `mach update`.

To uninstall:
```bash
curl -fsSL https://raw.githubusercontent.com/cognato-ai/mach/master/uninstall.sh | bash
```

### Option 2: via Pipx
If you prefer managing your Python CLIs with [pipx](https://pipx.pypa.io/stable/):

```bash
pipx install git+https://github.com/cognato-ai/mach.git
```
To update via pipx:
```bash
pipx upgrade mach
```

## 🏁 Quick Start

Navigate to any codebase and initialize Mach:

```bash
# Authenticate before using push, pull, or clone
mach login --token <pat-token>

# Bootstrap .mach and launch the interactive setup selectors
mach init

# Or bypass the interactive prompts for CI/CD
mach init --hook-agents claude,codex,gemini --store-content input,output,reasoning,tool
```

Use your AI agent as usual, then inspect and sync the captured session:

```bash
# Open the session dashboard
mach log

# Push a local session to Mach Web
mach push <session_id>
```

To work from a remote session, first trust the repository, then clone the session:

```bash
# Pull repository metadata and make that repo the local trust boundary
mach pull --repository my-repo

# Clone an existing session into a new local fork after repository trust is set
mach clone ses_123

# Push only the new steps created in your local fork
mach push <new_session_id>
```

### The TUI Dashboard
Once Mach is tracking your agents, launch the interactive dashboard:
```bash
mach log
```
* **Navigate:** Use `Arrow Keys` or `Tab` to move between your active AI sessions and the event timeline.
* **Inspect:** Press `Enter` on any step to open a detailed modal showing exact file diffs and raw content.
* **Search:** Press `/` to instantly filter the timeline by tool name, AI reasoning, or file modifications.

## ⚙️ Configuration

You can fully customize how Mach behaves by viewing and editing its configuration. Configurations are stored locally in `.mach/config`.

**To view all current configurations:**
```bash
mach config show
```

**To change a configuration:**
Use the `mach config set` command with the appropriate flags.
```bash
# Example: Disable the SQLite database indexing
mach config set --db-enabled false

# Example: Disable the TUI and revert to classic terminal logs
mach config set --use-tui false
```

### Configurable Keys:
| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Master switch to enable/disable Mach tracking. |
| `auto_session` | `true` | Automatically groups orphan events into active sessions. |
| `auto_tracking` | `true` | Automatically launches the background daemon when needed. |
| `use_tui` | `true` | Uses the interactive Textual TUI for `mach log`. Set to `false` for raw text logs. |
| `db_enabled` | `true` | Enables the SQLite FTS5 database for instant searching. |
| `hook_agents` | `[...]` | List of AI agents to automatically install intercepts for. |
| `ignore_paths` | `[...]` | Directories to ignore when calculating file diffs (e.g., `node_modules`). |
| `poll_interval_sec`| `2` | How often the background daemon checks the inbox. |
| `store_content` | `["input", "output", "reasoning", "tool"]` | Step types to actively capture and store as blob data. |

## 🔐 Repository Trust Boundary

Mach treats the repository as the trust boundary for remote operations. Before cloning a remote session, your local checkout must be associated with the same repository as the remote session.

### Track A Repository

```bash
mach pull --repository <repository_name>
```

This validates your auth token, fetches repository details from Mach Web, checks that the pulled repository matches the current Git checkout by name and remote URL when available, then stores the details locally in `.mach/tracked_repo.json`.

### Clone A Remote Session

```bash
mach clone <session_id>
```

Clone validates that the remote session belongs to the tracked repository, pulls session details, steps, and blobs, then creates a new local fork with a new session ID. The inherited remote steps are marked as already pulled/pushed, so a later `mach push <new_session_id>` uploads only new local steps.

### Push A Session

```bash
mach push <session_id>
```

Push uploads local steps to Mach Web in batches. If you added or changed `origin` after starting a session, `mach push` refreshes the Git remote URL and repository name before syncing. For cloned sessions, push uses the fork cursor so inherited remote steps are not uploaded again.

If you need to re-send a session:

```bash
# Re-push every step in the session
mach push <session_id> --reset

# Re-push only steps after a known step id
mach push <session_id> --reset-to <step_id>
```

### Fix Session Ledgers

```bash
# Dry-run normalization
mach fix

# Rewrite normalized ledgers and rebuild the index
mach fix --apply
```

`mach fix` merges safely mergeable streamed text chunks, normalizes tool-step hashes, recomputes Merkle roots, and rebuilds the SQLite index when applied.

## 💻 Command Reference

### Setup & Configuration
- `mach init`: Bootstrap the repository, interactively select hooks and stored content types, and start the daemon.
- `mach pull --repository <repository_name>`: Validate token access, confirm the remote repository matches this Git checkout, and store the tracked repo locally.
- `mach clone <session_id>`: Validate the session belongs to the tracked repository, pull its step state, fork it locally with a new session ID, and push only new fork steps later.
- `mach push <session_id>`: Push local session steps and blobs to Mach Web.
- `mach fix [session_id] --apply`: Normalize session ledgers and rebuild the local index.
- `mach config show|set`: View or update Mach configuration (e.g. `mach config set --db-enabled false`).
- `mach enable` / `mach disable`: Globally toggle tracking without losing configuration.

### Session & Event Management
- `mach log`: Open the interactive TUI.
- `mach show <session_id>`: Dump raw JSON output of a specific execution timeline.
- `mach verify`: Cryptographically verify the integrity of the JSONL ledger and Blob hashes.
- `mach fsck`: Rebuild the SQLite search index from scratch using the raw Blob store.

### Daemon Controls & Background Tracking
- `mach track start|stop|status`: Manage the background ingestion process.
- `mach hooks status`: Check the health and presence of your AI agent intercepts.

## ⚖️ License

This project is licensed under the [Mach License with Restrictions](LICENSE.md) — you may use this software, but you **may not copy, reproduce, or use it to create a competing hosted or distributed product** that offers substantially similar functionality.

> [!NOTE]
> **The `workspace_observer` pseudo-agent:**
> If you see `workspace_observer` in your `mach log`, this is Mach's background daemon. If an AI edits a file but fails to properly report it via its hooks (or if you manually edit a file during an active AI session), the daemon detects the "orphan" file system changes and securely logs them under `workspace_observer`. This guarantees your execution ledger is 100% accurate, even if the AI's telemetry is incomplete.

---
<div align="center">
<i>Built to make AI execution as verifiable as your code.</i>
</div>
