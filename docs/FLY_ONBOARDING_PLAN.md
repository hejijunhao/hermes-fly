# Fly.io Onboarding Layer — Implementation Plan

## Context

This repo is a fork of Hermes Agent, modified for one-click Fly.io deployment. The [hermes-alpha](https://github.com/kaminocorp/hermes-alpha) repo demonstrated the pattern: a FastAPI web server serves an xterm.js terminal over WebSocket, with `hermes chat` spawned as a PTY process inside the container. Fly.io volumes persist `~/.hermes/` across deploys.

**Goal:** Add a minimal onboarding flow so that when a user deploys this app to Fly.io and opens it for the first time, they are prompted to enter their OpenRouter API key before being dropped into the Hermes CLI. After onboarding, subsequent visits skip straight to the terminal.

---

## Architecture Decision: Web-Based Onboarding (Not CLI-Based)

Two approaches were considered:

| Approach | Pros | Cons |
|----------|------|------|
| **Web form (chosen)** | Clean browser-native UX; no terminal interaction needed; key is persisted before PTY spawns; works even if Hermes CLI has issues | Requires a new HTML page and a small API endpoint |
| **CLI setup wizard** | Reuses existing `hermes setup` code | Awkward in a web terminal; user must type a key into xterm.js (no paste on mobile); harder to style; CLI setup asks many irrelevant questions for cloud use |

**Decision:** A single-page web form served by the FastAPI app. The PTY process is not spawned until a valid API key is stored. This keeps the onboarding browser-native and the terminal experience clean.

---

## Prerequisites: Fly.io Deployment Scaffold

This repo does not yet contain the deployment files from hermes-alpha. Before implementing onboarding, the following files need to be ported (or recreated) from hermes-alpha:

| File | Purpose |
|------|---------|
| `fly-deploy/Dockerfile` | Container build (Python 3.11, hermes install, uvicorn) |
| `fly-deploy/fly.toml` | Fly.io config (region, VM size, volume mount, auto-stop) |
| `fly-deploy/entrypoint.sh` | Volume bootstrap, env injection, process management |
| `fly-deploy/app.py` | FastAPI server: login, WebSocket PTY bridge |
| `fly-deploy/static/terminal.html` | xterm.js web terminal |
| `fly-deploy/static/login.html` | Password login page |

> **Note:** We use `fly-deploy/` instead of `gateway/` to avoid collision with the existing `gateway/` messaging module in this repo.

---

## Implementation Plan

### Phase 1: Onboarding State Detection

**File:** `fly-deploy/app.py`

Add a function that checks whether onboarding is complete:

```python
HERMES_HOME = Path.home() / ".hermes"
ENV_FILE = HERMES_HOME / ".env"
ONBOARDED_MARKER = HERMES_HOME / ".onboarded"

def is_onboarded() -> bool:
    """Check if the user has completed first-run onboarding."""
    if ONBOARDED_MARKER.exists():
        return True
    # Also treat pre-existing key (e.g., set via fly secrets) as onboarded
    if os.getenv("OPENROUTER_API_KEY"):
        return True
    if ENV_FILE.exists():
        content = ENV_FILE.read_text()
        if re.search(r'^OPENROUTER_API_KEY=.+', content, re.MULTILINE):
            return True
    return False
```

**Why a marker file?** The `.onboarded` marker at `~/.hermes/.onboarded` lets us distinguish "user completed onboarding" from "user hasn't visited yet." Since `~/.hermes/` is on a persistent Fly volume, the marker survives container restarts. This avoids re-prompting after every deploy.

---

### Phase 2: Onboarding Page

**File:** `fly-deploy/static/onboarding.html`

A single, self-contained HTML page. Design principles:
- Matches the existing terminal page aesthetic (dark background, monospace font, cyberpunk palette)
- Single input field for the OpenRouter API key
- A "Skip" option (user may have set the key via `fly secrets set` instead)
- No JavaScript framework — vanilla JS, under 200 lines total
- Submits via `POST /onboarding` with JSON body

**Wireframe:**

```
┌──────────────────────────────────────────────┐
│                                              │
│           ⟡  HERMES AGENT  ⟡                │
│                                              │
│   Welcome. Enter your OpenRouter API key     │
│   to get started.                            │
│                                              │
│   ┌────────────────────────────────────┐     │
│   │ sk-or-v1-...                       │     │
│   └────────────────────────────────────┘     │
│                                              │
│   [ Launch Hermes ]         [ Skip → ]       │
│                                              │
│   ─────────────────────────────────────      │
│   Don't have a key?                          │
│   Get one at openrouter.ai/keys              │
│                                              │
└──────────────────────────────────────────────┘
```

**Key UX details:**
- Input field has `type="password"` with a show/hide toggle (API keys are sensitive)
- Client-side format validation: must start with `sk-or-v1-` and be ≥20 chars
- "Skip" navigates directly to `/terminal` (for users who set keys via Fly secrets)
- On success, redirects to `/terminal`
- Subtle error display inline (no alerts/modals)

---

### Phase 3: Onboarding API Endpoint

**File:** `fly-deploy/app.py`

Add a `POST /onboarding` endpoint:

```python
@app.post("/onboarding")
async def submit_onboarding(request: Request):
    body = await request.json()
    api_key = body.get("api_key", "").strip()

    if not api_key:
        return JSONResponse({"error": "API key is required"}, status_code=400)

    # Optional: validate the key actually works
    valid = await _validate_openrouter_key(api_key)
    if not valid:
        return JSONResponse({"error": "Invalid API key — could not authenticate with OpenRouter"}, status_code=400)

    # Persist to ~/.hermes/.env
    _write_env_key("OPENROUTER_API_KEY", api_key)

    # Mark onboarding complete
    ONBOARDED_MARKER.touch()

    return JSONResponse({"ok": True})
```

**Key validation** (lightweight, optional but recommended):

```python
async def _validate_openrouter_key(key: str) -> bool:
    """Hit OpenRouter's /auth/key endpoint to verify the key is valid."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return True  # Network error — don't block onboarding
```

**Env file writer:**

```python
def _write_env_key(key: str, value: str):
    """Write or update a key in ~/.hermes/.env."""
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)

    if ENV_FILE.exists():
        content = ENV_FILE.read_text()
        # Remove existing entry for this key
        content = re.sub(rf'^{re.escape(key)}=.*\n?', '', content, flags=re.MULTILINE)
    else:
        content = ""

    content = content.rstrip('\n') + f"\n{key}={value}\n"
    ENV_FILE.write_text(content)
    ENV_FILE.chmod(0o600)

    # Also inject into current process environment so the PTY inherits it
    os.environ[key] = value
```

---

### Phase 4: Routing Logic

**File:** `fly-deploy/app.py`

Modify the main routes to gate on onboarding state:

```python
@app.get("/")
async def root(request: Request):
    # If not authenticated (login password), show login
    if not _is_authenticated(request):
        return RedirectResponse("/login")
    # If not onboarded, show onboarding
    if not is_onboarded():
        return RedirectResponse("/onboarding")
    # Otherwise, show terminal
    return RedirectResponse("/terminal")

@app.get("/onboarding")
async def onboarding_page(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login")
    return FileResponse("static/onboarding.html")

@app.get("/terminal")
async def terminal_page(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login")
    # Allow terminal even without onboarding (skip button / fly secrets)
    return FileResponse("static/terminal.html")
```

**Flow diagram:**

```
Browser hits /
  │
  ├─ Not authenticated? → /login (password form)
  │                          │
  │                     POST /login → set session cookie → redirect /
  │
  ├─ Not onboarded? → /onboarding (API key form)
  │                       │
  │                  POST /onboarding → write key → set .onboarded → redirect /terminal
  │                       │
  │                  [Skip →] → /terminal (works if key set via fly secrets)
  │
  └─ Onboarded → /terminal (xterm.js WebSocket PTY)
```

---

### Phase 5: Entrypoint Integration

**File:** `fly-deploy/entrypoint.sh`

The existing entrypoint from hermes-alpha writes Fly secrets into `~/.hermes/.env` on boot. Modify the bootstrap section to respect onboarding state:

```bash
# --- Volume bootstrap (first boot only) ---
if [ ! -f "$HERMES_HOME/.bootstrapped" ]; then
    mkdir -p "$HERMES_HOME"/{sessions,logs,memories,skills,hooks,cron}
    cp /opt/hermes-agent/.env.example "$HERMES_HOME/.env"
    cp /opt/hermes-agent/cli-config.yaml.example "$HERMES_HOME/config.yaml"
    touch "$HERMES_HOME/.bootstrapped"
fi

# --- Inject Fly secrets into .env (only if set) ---
inject_if_set() {
    local key="$1" val="${!1}"
    [ -z "$val" ] && return
    sed -i "/^${key}=/d" "$HERMES_HOME/.env"
    echo "${key}=${val}" >> "$HERMES_HOME/.env"
    # If OPENROUTER_API_KEY is being injected via fly secrets,
    # mark onboarding as complete (user configured externally)
    [ "$key" = "OPENROUTER_API_KEY" ] && touch "$HERMES_HOME/.onboarded"
}

inject_if_set OPENROUTER_API_KEY
inject_if_set ANTHROPIC_API_KEY
inject_if_set OPENAI_API_KEY
# ... other keys ...
```

**Key point:** If the user set `OPENROUTER_API_KEY` as a Fly secret (`fly secrets set OPENROUTER_API_KEY=sk-or-v1-...`), the entrypoint writes it and marks onboarding complete. The web form is never shown.

---

### Phase 6: PTY Environment Inheritance

**File:** `fly-deploy/app.py` (WebSocket handler)

When spawning the PTY for `hermes chat`, ensure the process inherits the freshly-written environment:

```python
async def websocket_terminal(ws: WebSocket):
    # Re-read .env before spawning so keys written during onboarding are picked up
    env = os.environ.copy()
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()

    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe("hermes", ["hermes", "chat"], env)
    # ... rest of WebSocket relay ...
```

This ensures that even if the key was written to `.env` after the FastAPI process started (i.e., during onboarding), the PTY subprocess picks it up.

---

## File Summary

| File | Action | Description |
|------|--------|-------------|
| `fly-deploy/static/onboarding.html` | **Create** | Onboarding page — API key input form |
| `fly-deploy/app.py` | **Create/Modify** | Add `GET /onboarding`, `POST /onboarding`, routing logic, key validation, env writer |
| `fly-deploy/entrypoint.sh` | **Create/Modify** | Add `inject_if_set` helper, auto-mark onboarding when Fly secrets provide the key |
| `fly-deploy/Dockerfile` | **Create** | Port from hermes-alpha, no onboarding-specific changes |
| `fly-deploy/fly.toml` | **Create** | Port from hermes-alpha, no onboarding-specific changes |
| `fly-deploy/static/terminal.html` | **Create** | Port from hermes-alpha, no onboarding-specific changes |
| `fly-deploy/static/login.html` | **Create** | Port from hermes-alpha, no onboarding-specific changes |

**No changes to the core Hermes agent code.** The onboarding layer is entirely within the `fly-deploy/` deployment scaffold.

---

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| User sets key via `fly secrets set` before first visit | Entrypoint writes key + `.onboarded` marker → onboarding skipped |
| User clicks "Skip" without entering a key | Goes to terminal; Hermes CLI's own `_has_any_provider_configured()` check will prompt or fail gracefully |
| User enters invalid key | Server-side validation against OpenRouter `/auth/key` endpoint returns error; user can retry |
| OpenRouter API is unreachable during validation | Validation returns `True` (fail-open) — don't block onboarding on network issues |
| User wants to change key later | Use `hermes setup` in the terminal, or update via `fly secrets set`, or manually edit `~/.hermes/.env` |
| Volume is wiped (fresh deploy without volume) | `.onboarded` marker is lost → onboarding shown again, which is correct |
| Multiple concurrent users | Fly.io single-machine model means one user at a time; file writes are safe. If scaled later, each machine has its own volume |

---

## Future Extensions (Out of Scope)

These are not part of this implementation but could be added later:

- **Multi-provider onboarding:** Tabs or dropdown to choose between OpenRouter, Anthropic, OpenAI — each with its own key field
- **Model selection:** Let users pick their default model during onboarding (e.g., Claude, GPT-4, Llama)
- **Settings page:** A `/settings` web UI to change keys, model, toolsets without using the CLI
- **Multi-user support:** Per-user volumes, auth tokens, isolated sessions
