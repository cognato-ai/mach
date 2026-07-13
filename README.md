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
- **JSONL-only ledger:** Sessions are append-only `steps.jsonl` files plus content-addressed blobs and a per-session Merkle root — no local SQL database required.
- **Structured risk engine:** Local rules flag sensitive paths, secret-like content, and dangerous commands as `risk_flags` on each step. Enterprise backends can add more flags later; local and external annotations merge cleanly.
- **Lightning TUI:** Drop into a premium, interactive terminal dashboard (`mach log`). Press `/` at any time to filter the timeline by tool name, reasoning, or file modifications.
- **Zero-Latency Ingestion:** AI events are fired into an asynchronous inbox. A lightweight background daemon processes them into the ledger, ensuring minimal latency impact on your actual AI workflows.
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

# Open the interactive agent-step diff for a session
mach diff <session_id>

# Push a local session to Mach Web
mach push <session_id>
```

### Clone (remote → Mach) & resume (Mach → agent)

```bash
# Import a remote session into a local Mach ledger
mach clone my-repo ses_123
mach push <new_local_session_id>

# Mach spawns the agent, transfers the full handoff, links session ids
mach resume ses_abc123 --agent claude
# → [1/4] write structured full-chat handoff (no AI summary)
# → [2/4] activate Mach session
# → [3/4] spawn claude -p … (agent reads handoff via tools)
# → [4/4] print only: claude -r <agent_session_id>
# You do NOT paste context yourself.

mach resume --status          # handoff path + linked vendor id + resume cmd
mach resume --clear-pending
```

`mach resume` owns session creation and handoff transfer. The only user action
after a successful resume is opening the already-seeded agent session.

### The TUI Dashboard
Once Mach is tracking your agents, launch the interactive dashboard:
```bash
mach log
```
* **Navigate:** Use `Arrow Keys` or `Tab` to move between your active AI sessions and the event timeline.
* **Inspect:** Press `Enter` on any step to open a detailed modal showing exact file diffs and raw content.
* **Search:** Press `/` to instantly filter the timeline by tool name, AI reasoning, or file modifications.
* **Diff:** Run `mach diff <session_id>` for a split-pane step view. The left pane lists file-changing steps, and the right pane shows the files changed by that step with recorded hunks or a Git diff fallback.

## ⚙️ Configuration

You can fully customize how Mach behaves by viewing and editing its configuration. Configurations are stored locally in `.mach/config`.

**To view all current configurations:**
```bash
mach config show
```

**To change a configuration:**
Use the `mach config set` command with the appropriate flags.
```bash
# Example: Disable the TUI and revert to classic terminal logs
mach config set --use-tui false

# Example: Disable local risk scoring (facts still capture; no risk_flags)
mach config set --risk-enabled false

# Example: Silence noisy rules
mach config set --risk-disable-rules TOOL_SHELL_EXEC,PATH_INFRA
```

### Configurable Keys:
| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Master switch to enable/disable Mach tracking. |
| `auto_session` | `true` | Automatically groups orphan events into active sessions. |
| `auto_tracking` | `true` | Automatically launches the background daemon when needed. |
| `use_tui` | `true` | Uses the interactive Textual TUI for `mach log`. Set to `false` for raw text logs. |
| `hook_agents` | `[...]` | List of AI agents to automatically install intercepts for. |
| `ignore_paths` | `[...]` | Directories to ignore when calculating file diffs (e.g., `node_modules`). |
| `poll_interval_sec`| `2` | How often the background daemon checks the inbox. |
| `store_content` | `["input", "output", "reasoning", "tool"]` | Step types to actively capture and store as blob data. |
| `risk.enabled` | `true` | Run the local structured risk engine on each recorded step. |
| `risk.disabled_rules` | `[]` | Built-in or custom `rule_id` values to skip. |
| `risk.extra_*_patterns` | `[]` | Optional custom path/content/command regex rules (edit via config JSON). |

## 🔐 Repository Trust Boundary

Mach treats the repository as the trust boundary for remote operations. Before cloning a remote session, your local checkout must be associated with the same repository as the remote session.

### Track A Repository

```bash
mach pull --repository <repository_name>
```

This validates your auth token, fetches repository details from Mach Web, checks that the pulled repository matches the current Git checkout by name and remote URL when available, then stores the details locally in `.mach/tracked_repo.json`.

This command is useful when you want to validate repository access separately. Most users can let `mach clone <repository_name> <session_id>` do this step automatically.

### Clone A Remote Session

```bash
mach clone <repository_name> <session_id>
```

Clone pulls and validates the repository metadata, verifies that the remote session belongs to that repository, pulls session details, steps, and blobs, then creates a new local fork with a new session ID. The inherited remote steps are marked as already pulled/pushed, so a later `mach push <new_session_id>` uploads only new local steps.

If the repository is already tracked locally, the shorter form also works:

```bash
mach clone <session_id>
```

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

`mach fix` merges safely mergeable streamed text chunks, normalizes tool-step hashes, recomputes Merkle roots, and repairs meta counters when applied.

## 💻 Command Reference

### Setup & Configuration
- `mach init`: Bootstrap the repository, interactively select hooks and stored content types, and start the daemon.
- `mach pull --repository <repository_name>`: Validate token access, confirm the remote repository matches this Git checkout, and store the tracked repo locally.
- `mach clone …`: Import a remote session into a local Mach fork (history only).
- `mach resume <session_id> --agent <name>`: Write full-chat handoff, activate Mach session, bind next agent session, print start/resume commands.
- `mach push <session_id>`: Push local session steps and blobs to Mach Web.
- `mach fix [session_id] --apply`: Normalize session ledgers and rebuild the local index.
- `mach config show|set`: View or update Mach configuration (e.g. `mach config set --db-enabled false`).
- `mach enable` / `mach disable`: Globally toggle tracking without losing configuration.

### Session & Event Management
- `mach log`: Open the interactive TUI.
- `mach show <session_id>`: Dump raw JSON output of a specific execution timeline.
- `mach verify`: Cryptographically verify the integrity of the JSONL ledger and Merkle roots.
- `mach fsck`: Verify JSONL ledgers, blob presence, and repair session meta counters.

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
