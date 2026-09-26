#!/usr/bin/env bash
# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
#
# One-command launcher for a local ForgeMesh dev environment (macOS/Linux).
#
# Mode is auto-detected from the env file:
#   OFFLINE  LLM_BASE_URL contains localhost / 127.0.0.1  →  Ollama + embedding cache
#   ONLINE   LLM_BASE_URL absent or cloud URL             →  skip Ollama entirely
#
# In both modes Docker provides MySQL + Fuseki; the webapp runs natively.
#
# Usage:
#   ./start-forgemesh.sh --project synthea        # quick start (config from .env)
#   ./start-forgemesh.sh                          # last-selected project, else DEFAULT_PROJECT (synthea)
#   ./start-forgemesh.sh --env .env.local         # alternate env file (e.g. offline / air-gapped)
#   ./start-forgemesh.sh --stop                   # stop webapp + infra
#   ./start-forgemesh.sh --skip-model-pull        # offline, skip ollama pull
#   ./start-forgemesh.sh --port 5174
#
set -euo pipefail
cd "$(dirname "$0")"

# ── args ─────────────────────────────────────────────────────────────────────
STOP=0; SKIP_PULL=0; ENV_FILE=".env"; PORT=5173; PROJECT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stop)            STOP=1; shift ;;
    --skip-model-pull) SKIP_PULL=1; shift ;;
    --env)             ENV_FILE="$2"; shift 2 ;;
    --project)         PROJECT="$2"; shift 2 ;;
    --port)            PORT="$2"; shift 2 ;;
    -h|--help)         grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ── output helpers ───────────────────────────────────────────────────────────
c_cyan=$'\033[36m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_off=$'\033[0m'
info() { printf "%s[ forge ]%s %s\n" "$c_cyan"   "$c_off" "$*"; }
good() { printf "%s[  ok   ]%s %s\n" "$c_green"  "$c_off" "$*"; }
warn() { printf "%s[ warn  ]%s %s\n" "$c_yellow" "$c_off" "$*"; }
die()  { printf "%s[ fail  ]%s %s\n" "$c_red"    "$c_off" "$*"; exit 1; }

WEB_PID_FILE="runtime/webapp.pid"

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then docker-compose "$@"
  else die "Docker not found. Install Docker (needed for MySQL + Fuseki infra services)."; fi
}

# Read one KEY from an env file, stripping inline comments and quotes.
env_get() {
  local key="$1" file="$2" line val
  [[ -f "$file" ]] || return 0
  line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n1 || true)"
  [[ -n "$line" ]] || return 0
  val="${line#*=}"
  val="${val%%#*}"
  val="$(echo "$val" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
  val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
  echo "$val"
}

# ── STOP path ────────────────────────────────────────────────────────────────
if [[ "$STOP" -eq 1 ]]; then
  info "Stopping ForgeMesh..."
  if [[ -f "$WEB_PID_FILE" ]]; then
    wpid="$(cat "$WEB_PID_FILE" 2>/dev/null || true)"
    if [[ -n "${wpid:-}" ]] && kill -0 "$wpid" 2>/dev/null; then
      kill "$wpid" 2>/dev/null || true; good "Webapp stopped (pid $wpid)."
    fi
    rm -f "$WEB_PID_FILE"
  else
    warn "No webapp PID file; if a webapp is running in another terminal, Ctrl+C it."
  fi
  compose stop db fuseki
  good "Infra containers stopped. (Ollama left running — it's a shared service.)"
  exit 0
fi

# ── 0. config ────────────────────────────────────────────────────────────────
[[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found. Run: cp .env.example .env  (then set LLM_API_KEY), or pass --env."
info "Using config: $ENV_FILE"

BASE_URL="$(env_get LLM_BASE_URL "$ENV_FILE")"
[[ -n "$BASE_URL" ]] || BASE_URL="$(env_get OPENAI_BASE_URL "$ENV_FILE")"

# Detect offline mode: LLM_BASE_URL points to localhost / 127.0.0.1
OFFLINE=0
if [[ "$BASE_URL" =~ localhost|127\.0\.0\.1 ]]; then
  OFFLINE=1
fi

if [[ "$OFFLINE" -eq 1 ]]; then
  info "Mode: OFFLINE (Ollama @ $BASE_URL)"
else
  info "Mode: ONLINE (cloud LLM — Ollama not required)"
fi

mkdir -p runtime

# ── 1. infra plane: db + fuseki ───────────────────────────────────────────────
info "Starting infra (MySQL + Fuseki) via Docker..."
compose up -d db fuseki || die "docker compose failed to start db + fuseki."
good "Infra containers up (MySQL $(compose port db 3306 2>/dev/null || echo '?'), Fuseki $(compose port fuseki 3030 2>/dev/null || echo '?'))."

# ── 2. offline only: Ollama server ───────────────────────────────────────────
if [[ "$OFFLINE" -eq 1 ]]; then
  command -v ollama >/dev/null 2>&1 || die "Ollama not installed. Install from https://ollama.com or 'brew install ollama'."
  ollama_up() { curl -sf --max-time 3 http://localhost:11434/api/tags >/dev/null 2>&1; }
  if ollama_up; then
    good "Ollama server already running."
  else
    info "Starting Ollama server..."
    ollama serve >/tmp/forgemesh-ollama.log 2>&1 &
    up=0; for _ in $(seq 1 15); do sleep 1; if ollama_up; then up=1; break; fi; done
    [[ "$up" -eq 1 ]] && good "Ollama server up." || die "Ollama server did not come up (see /tmp/forgemesh-ollama.log)."
  fi

  # ── 3. offline only: model presence check ──────────────────────────────────
  MODELS=()
  for k in LLM_MODEL QUERY_MODEL ONTOLOGY_GEN_MODEL ONTOLOGY_JUDGE_MODEL; do
    v="$(env_get "$k" "$ENV_FILE")"
    if [[ -n "$v" ]]; then
      dup=0; for m in "${MODELS[@]:-}"; do [[ "$m" == "$v" ]] && dup=1; done
      [[ "$dup" -eq 0 ]] && MODELS+=("$v")
    fi
  done

  INSTALLED="$(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}')"
  for m in "${MODELS[@]:-}"; do
    if echo "$INSTALLED" | grep -qx "$m"; then
      good "Model present: $m"
    elif [[ "$SKIP_PULL" -eq 1 ]]; then
      warn "Model MISSING: $m  (run: ollama pull $m)"
    else
      info "Pulling model: $m  (this can be several GB)..."
      ollama pull "$m" || die "Failed to pull $m"
      good "Pulled: $m"
    fi
  done

  # ── 4. offline only: embedding cache ───────────────────────────────────────
  info "Checking local embedding model cache (all-MiniLM-L6-v2)..."
  PY=".venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python || command -v python3)"
  if "$PY" -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" >/dev/null 2>&1; then
    good "Embedding model cached locally."
  else
    warn "Embedding model not cached. Pre-cache it ONCE while online:"
    warn "  $PY -c \"from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')\""
    warn "(ForgeMesh will try to download it on first use — which breaks a true air gap.)"
  fi

  # ── 5. offline only: air-gap preflight ─────────────────────────────────────
  PY=".venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python || command -v python3)"
  info "Running air-gap preflight..."
  "$PY" scripts/check_local_llm.py "$ENV_FILE" || warn "Preflight reported issues (see above). Continuing to launch anyway."
fi

PY=".venv/bin/python"; [[ -x "$PY" ]] || PY="$(command -v python || command -v python3)"

# ── 6. launch webapp natively ────────────────────────────────────────────────
if [[ -f "$WEB_PID_FILE" ]]; then
  oldpid="$(cat "$WEB_PID_FILE" 2>/dev/null || true)"
  if [[ -n "${oldpid:-}" ]] && kill -0 "$oldpid" 2>/dev/null; then
    warn "Webapp already running (pid $oldpid). Stop it first: ./start-forgemesh.sh --stop"
    info "Open http://127.0.0.1:$PORT"; exit 0
  fi
fi
info "Launching webapp on http://127.0.0.1:$PORT  (Ctrl+C to stop)"
echo $$ > "$WEB_PID_FILE"
trap 'rm -f "$WEB_PID_FILE"' EXIT
good "Open http://127.0.0.1:$PORT"
PROJECT_ARGS=(); [[ -n "$PROJECT" ]] && PROJECT_ARGS=(--project "$PROJECT")
exec "$PY" web_app.py --env "$ENV_FILE" "${PROJECT_ARGS[@]+"${PROJECT_ARGS[@]}"}" --host 127.0.0.1 --port "$PORT"
