# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Hermes Agent?

A self-improving AI agent framework by Nous Research. It uses OpenAI-compatible APIs (OpenRouter, Anthropic, Nous Portal, etc.) to power an interactive CLI agent with 40+ tools, 36+ skills, a messaging gateway (Telegram, Discord, Slack, WhatsApp, Signal, etc.), and RL training environments. Python 3.11+, MIT licensed.

## Build & Development Commands

```bash
# Setup
uv venv venv --python 3.11
source venv/bin/activate
uv pip install -e ".[all,dev]"

# Run tests (unit only, excludes integration — this is what CI runs)
pytest tests/ -q --ignore=tests/integration --tb=short -n auto

# Run a single test file
pytest tests/agent/test_prompt_builder.py -v

# Run integration tests (requires real API keys)
pytest tests/integration/ -v -m integration

# Diagnostics
hermes doctor
```

CI runs on every push/PR to main: Python 3.11, `uv`, `pytest -n auto` with API keys blanked out. 10-minute timeout.

## Architecture

### Core Loop

```
User message → AIAgent.run_conversation() (run_agent.py)
  → Build system prompt (agent/prompt_builder.py)
  → Call LLM (OpenAI-compatible API)
  → If tool_calls: dispatch via tools/registry → loop
  → If text: persist to SQLite + JSON logs → return
  → Context compression if approaching token limit
```

### File Dependency Chain

```
tools/registry.py        ← zero deps, imported by all tool files
       ↑
tools/*.py               ← each calls registry.register() at import time
       ↑
model_tools.py           ← imports all tool modules, triggers discovery
       ↑
run_agent.py / cli.py / gateway/run.py / batch_runner.py
```

### Key Modules

| Module | Purpose |
|--------|---------|
| `run_agent.py` | AIAgent class — conversation loop, tool dispatch, session persistence |
| `cli.py` | HermesCLI — interactive TUI with prompt_toolkit |
| `model_tools.py` | Tool orchestration, `_discover_tools()`, `handle_function_call()` |
| `toolsets.py` | Tool groupings and per-platform presets |
| `hermes_state.py` | SQLite session DB with FTS5 search |
| `agent/prompt_builder.py` | System prompt assembly (identity, skills, memory, context) |
| `agent/context_compressor.py` | Auto-summarization when approaching context limits |
| `hermes_cli/config.py` | `DEFAULT_CONFIG`, `OPTIONAL_ENV_VARS`, config migration |
| `hermes_cli/commands.py` | Central slash command registry (`CommandDef` list) |
| `tools/registry.py` | Central tool registry (schemas, handlers, dispatch) |
| `gateway/run.py` | GatewayRunner — messaging platform lifecycle and routing |

### Design Patterns

- **Self-registering tools**: Each `tools/*.py` calls `registry.register()` at import time. Circular-import safe.
- **Toolset grouping**: Tools organized by capability (web, terminal, file, browser, etc.), enabled/disabled per platform.
- **Ephemeral injection**: System prompts are built at API call time, never persisted to DB or logs.
- **Provider abstraction**: Any OpenAI-compatible endpoint. Provider resolution at init time.
- **All tool handlers return JSON strings.**

## How to Add Things

### New Tool (3 files)
1. Create `tools/your_tool.py` — schema + handler + `registry.register()` call
2. Add import to `model_tools.py` `_modules` list
3. Add to `toolsets.py` (existing toolset or new one)

### New Slash Command
1. Add `CommandDef` to `COMMAND_REGISTRY` in `hermes_cli/commands.py`
2. Add handler in `HermesCLI.process_command()` in `cli.py`
3. Optionally add gateway handler in `gateway/run.py`

### New Config Option
1. Add to `DEFAULT_CONFIG` in `hermes_cli/config.py`
2. Bump `_config_version` to trigger migration

### New Skill
Create `skills/<category>/<name>/SKILL.md` with YAML frontmatter (name, description, version, author, platforms, tags). See `CONTRIBUTING.md` for full SKILL.md format and conditional activation fields.

## Code Style

- PEP 8, no strict line length enforcement
- Comments only for non-obvious intent, trade-offs, or API quirks — no narration
- Catch specific exceptions; use `logger.warning()`/`logger.error()` with `exc_info=True`
- Cross-platform: never assume Unix (supports Windows via WSL2, macOS, Linux)
- Prefer skills over tools for new capabilities (skills = instructions + existing tools; tools = deep integration)

## Config Systems

Three separate config loaders exist — be aware when changing config behavior:

| Loader | Used by | Location |
|--------|---------|----------|
| `load_cli_config()` | CLI mode | `cli.py` |
| `load_config()` | `hermes tools`, `hermes setup` | `hermes_cli/config.py` |
| Direct YAML load | Gateway | `gateway/run.py` |

User config lives in `~/.hermes/`: `config.yaml` (settings), `.env` (API keys), `auth.json` (OAuth), `state.db` (sessions), `skills/`, `memories/`.

## Test Conventions

- Shared fixtures in `tests/conftest.py`: `_isolate_hermes_home` redirects HERMES_HOME to temp dir, `mock_config` provides minimal config, 30s per-test timeout on Unix
- Integration tests use `@pytest.mark.integration` and are excluded from CI
- Tests run in parallel via `pytest-xdist` (`-n auto`)
