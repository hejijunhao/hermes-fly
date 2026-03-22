# Fly.io Onboarding — Implementation Completion

## What Was Built

A complete Fly.io deployment scaffold (`fly-deploy/`) with an integrated first-run onboarding flow that collects the user's OpenRouter API key via a browser form before launching the Hermes CLI in a web terminal.

---

## Files Created

### `fly-deploy/app.py` — FastAPI web server (new, 270 lines)

The central server. Ported from hermes-alpha's `gateway/app.py` with the following additions:

**Onboarding state detection** (`_is_onboarded()`):
- Checks for `~/.hermes/.onboarded` marker file (persists on Fly volume)
- Falls back to checking environment variables and `~/.hermes/.env` for any LLM provider key (`OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `HERMES_API_KEY`)
- Ignores placeholder values (e.g., `sk-or-...` from `.env.example`)

**Onboarding API endpoint** (`POST /onboarding`):
- Accepts JSON `{ "api_key": "sk-or-v1-..." }`
- Validates the key against OpenRouter's `/api/v1/auth/key` endpoint via `httpx`
- Fail-open on network errors (timeout/unreachable don't block onboarding)
- Writes validated key to `~/.hermes/.env` via `_write_env_key()` (handles create, update, permissions)
- Creates `~/.hermes/.onboarded` marker
- Injects key into `os.environ` so the next PTY spawn inherits it

**Routing changes** (`GET /`):
```
/ → not authed?    → /login
  → not onboarded? → /onboarding
  → else           → /terminal
```

**PTY environment** (`ws_terminal` WebSocket handler):
- Re-reads `~/.hermes/.env` at PTY spawn time via `_read_env_file()` and merges into the subprocess environment
- This ensures keys written during onboarding (after the FastAPI process started) are picked up by the `hermes chat` child process

**Auth bypass**: If `TTYD_PASS` is not set, login is skipped entirely (useful for development or single-user deployments without password protection).

---

### `fly-deploy/static/onboarding.html` — Onboarding page (new, 230 lines)

Single-page form matching the existing cyberpunk aesthetic (IBM Plex Mono, dark theme, scanlines, corner accents). Uses blue accent corners (vs. red for login) to visually distinguish the setup step.

**UX features:**
- Password-type input with SHOW/HIDE toggle for the API key
- Client-side validation: rejects empty input and keys not starting with `sk-or-`
- Enter key submits the form
- Loading spinner during server-side validation
- Inline error messages (no alerts/modals)
- "Skip" button goes directly to `/terminal` (for users who set keys via `fly secrets` or want to configure later)
- Link to `openrouter.ai/keys` for users who don't have a key yet

**No JavaScript frameworks** — vanilla JS, single `<script>` block, `fetch()` for the POST.

---

### `fly-deploy/entrypoint.sh` — Container entrypoint (ported + modified, 90 lines)

Ported from hermes-alpha with one key addition:

**Auto-mark onboarding on secret injection** (lines 24-30):
```bash
case "$key" in
    OPENROUTER_API_KEY|ANTHROPIC_API_KEY|OPENAI_API_KEY|HERMES_API_KEY)
        touch "$HERMES_DIR/.onboarded"
        ;;
esac
```

When any LLM provider key is injected via Fly secrets at boot time, the `.onboarded` marker is created. The web onboarding form is never shown — the user goes straight from login to terminal.

---

### `fly-deploy/Dockerfile` — Container build (ported + adapted, 30 lines)

Adapted from hermes-alpha:
- Uses `COPY . /opt/hermes-agent/` instead of `COPY hermes-agent/` (this repo IS the agent, not a parent repo that vendors it)
- Dockerfile path is `fly-deploy/Dockerfile` (referenced in `fly.toml`)
- Adds `httpx` to pip install (needed for API key validation)
- Copies from `fly-deploy/` instead of `gateway/`

---

### `fly-deploy/fly.toml` — Fly.io configuration (ported + adapted, 20 lines)

- App name changed to `hermes-fly` (from `hermes-alpha`)
- Dockerfile path updated to `fly-deploy/Dockerfile`
- Same VM config: `shared-cpu-1x`, 1GB RAM, Singapore region, auto-stop/start
- Persistent volume `hermes_data` mounted at `/root/.hermes`

---

### `fly-deploy/static/login.html` — Login page (ported, 115 lines)

Ported as-is from hermes-alpha. Password form with `TTYD_PASS` authentication, session cookie.

---

### `fly-deploy/static/terminal.html` — Web terminal (ported + adapted, 210 lines)

Ported from hermes-alpha with minor changes:
- Header tag changed from "ALPHA" to "FLY"
- Provider dropdown defaults to OpenRouter only (removed "NOUS DIRECT" option since this is a general-purpose deployment)

Uses xterm.js 5.5.0 with fit and web-links addons, WebSocket PTY bridge, auto-reconnect with exponential backoff, wake-from-sleep detection.

---

### `fly-deploy/docker-compose.yml` — Local dev (new, 12 lines)

For local testing: `docker compose -f fly-deploy/docker-compose.yml up --build`

---

### `.dockerignore` — Build context filter (new, 12 lines)

Excludes `.git`, `docs/`, `venv`, `.claude`, etc. from the Docker build context.

---

## Files NOT Modified

No existing files in the Hermes agent codebase were changed. The entire deployment and onboarding layer is self-contained in `fly-deploy/` and `.dockerignore`.

---

## User Flow

### First visit (no API key configured):

```
Browser → /
  → /login (enter TTYD_PASS password)
    → /onboarding (enter OpenRouter API key)
      → POST /onboarding (validate key, write to .env, create .onboarded)
        → /terminal (xterm.js connects via WebSocket, PTY spawns hermes chat)
```

### Subsequent visits:

```
Browser → /
  → /login (if not already authenticated via cookie)
    → /terminal (onboarding skipped — .onboarded marker exists)
```

### Key set via Fly secrets (`fly secrets set OPENROUTER_API_KEY=...`):

```
Container boot → entrypoint.sh writes key to .env + creates .onboarded
Browser → / → /login → /terminal (onboarding never shown)
```

### Skip button:

```
/onboarding → [Skip] → /terminal
  → hermes chat launches; its own _has_any_provider_configured() check
    may prompt in the CLI or error
```

---

## Deployment Commands

```bash
# First-time setup
fly apps create hermes-fly
fly volumes create hermes_data --region sin --size 1

# Set access password
fly secrets set TTYD_PASS=your-password

# Deploy
fly deploy --config fly-deploy/fly.toml

# Optional: pre-set API key (skips web onboarding)
fly secrets set OPENROUTER_API_KEY=sk-or-v1-...
```

---

## Edge Cases Handled

| Scenario | Behavior |
|----------|----------|
| Key set via `fly secrets` before first visit | entrypoint writes key + `.onboarded` marker; onboarding skipped |
| User clicks Skip without entering key | Goes to terminal; Hermes CLI handles missing provider |
| Invalid key entered | Server validates against OpenRouter `/auth/key`; shows inline error |
| OpenRouter unreachable during validation | Fail-open — key is accepted (don't block on network) |
| Key written during onboarding, before PTY spawns | PTY handler re-reads `.env` at spawn time; key is inherited |
| Volume wiped (fresh deploy) | `.onboarded` lost; onboarding shown again (correct) |
| No `TTYD_PASS` set | Login page is skipped entirely; goes straight to onboarding/terminal |
| `.env.example` placeholder (`sk-or-...`) in .env | `_is_onboarded()` regex ignores this placeholder |
