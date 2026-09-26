# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
#requires -Version 5.1
<#
.SYNOPSIS
  One-command launcher for a local / air-gapped ForgeMesh dev environment.

.DESCRIPTION
  Brings up the two planes ForgeMesh needs and leaves you at a running webapp:

    Infra plane (Docker)  : MySQL + Fuseki         -- data/triplestore services
    App plane (native)    : Ollama + the webapp    -- needs the host GPU + model cache

  The script is idempotent: re-run it any time. It only does work that is missing
  (starts stopped containers, pulls absent models, etc.) and then launches the
  webapp natively with --env .env.local so every LLM call stays on this machine.

  Model names are read FROM .env.local (LLM_MODEL / QUERY_MODEL / ONTOLOGY_*),
  so changing models there is all you need -- no edits here.

.PARAMETER Stop
  Stop the webapp and the infra containers, then exit. Leaves Ollama running
  (it's a shared system service; stop it yourself with: ollama stop / Stop-Service).

.PARAMETER SkipModelPull
  Do not auto-pull missing Ollama models; only check and warn. Use offline.

.PARAMETER EnvFile
  Env file to drive the app. Default: .env.local

.PARAMETER Port
  Webapp port. Default: 5173

.EXAMPLE
  .\start-forgemesh.ps1
.EXAMPLE
  .\start-forgemesh.ps1 -Stop
.EXAMPLE
  .\start-forgemesh.ps1 -SkipModelPull
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$SkipModelPull,
    [string]$EnvFile = ".env.local",
    [int]$Port = 5173
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# ── tiny output helpers ──────────────────────────────────────────────────────
function Info($m) { Write-Host "[ forge ] $m" -ForegroundColor Cyan }
function Good($m) { Write-Host "[  ok   ] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[ warn  ] $m" -ForegroundColor Yellow }
function Die ($m) { Write-Host "[ fail  ] $m" -ForegroundColor Red; exit 1 }

$OllamaExe = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
if (-not (Test-Path $OllamaExe)) {
    $cmd = Get-Command ollama -ErrorAction SilentlyContinue
    if ($cmd) { $OllamaExe = $cmd.Source }
}
$WebPidFile = Join-Path $PSScriptRoot "runtime\webapp.pid"

function Get-Compose {
    # Prefer `docker compose` (v2); fall back to `docker-compose` (v1).
    if (Get-Command docker -ErrorAction SilentlyContinue) {
        & docker compose version *> $null
        if ($LASTEXITCODE -eq 0) { return @("docker", "compose") }
    }
    if (Get-Command docker-compose -ErrorAction SilentlyContinue) { return @("docker-compose") }
    Die "Docker not found. Install Docker Desktop (needed for the MySQL + Fuseki infra services)."
}

# ── .env.local parsing (KEY=VALUE, strips inline # comments) ──────────────────
function Read-EnvFile($path) {
    $map = @{}
    if (-not (Test-Path $path)) { return $map }
    foreach ($line in Get-Content $path) {
        $t = $line.Trim()
        if ($t -eq "" -or $t.StartsWith("#")) { continue }
        $eq = $t.IndexOf("=")
        if ($eq -lt 1) { continue }
        $k = $t.Substring(0, $eq).Trim()
        $v = $t.Substring($eq + 1)
        $hash = $v.IndexOf(" #")           # strip trailing inline comment
        if ($hash -ge 0) { $v = $v.Substring(0, $hash) }
        $map[$k] = $v.Trim().Trim('"').Trim("'")
    }
    return $map
}

# ── STOP path ────────────────────────────────────────────────────────────────
# Kill a process and its whole child tree (the recorded PID may be this script,
# whose child is the uvicorn python; or, from an older launch, uvicorn directly).
function Stop-Tree($rootPid) {
    if (-not $rootPid) { return $false }
    $killed = $false
    $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$rootPid" -ErrorAction SilentlyContinue
    foreach ($k in $kids) { if (Stop-Tree $k.ProcessId) { $killed = $true } }
    $p = Get-Process -Id $rootPid -ErrorAction SilentlyContinue
    if ($p) { Stop-Process -Id $rootPid -Force -ErrorAction SilentlyContinue; $killed = $true }
    return $killed
}

if ($Stop) {
    Info "Stopping ForgeMesh..."
    $stoppedAny = $false
    if (Test-Path $WebPidFile) {
        $wpid = (Get-Content $WebPidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($wpid -and (Stop-Tree ([int]$wpid))) { $stoppedAny = $true; Good "Webapp tree stopped (pid $wpid)." }
        Remove-Item $WebPidFile -ErrorAction SilentlyContinue
    }
    # Fallback: kill whatever is actually listening on the webapp port. Catches
    # orphans from an earlier (detached) launch even with no/again stale PID file.
    try {
        $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            if (Stop-Tree $c.OwningProcess) { $stoppedAny = $true; Good "Stopped process on port $Port (pid $($c.OwningProcess))." }
        }
    } catch { }
    if (-not $stoppedAny) { Warn "No running webapp found (nothing on port $Port)." }
    $compose = Get-Compose
    & $compose[0] ($compose[1..($compose.Count-1)] + @("stop", "db", "fuseki"))
    Good "Infra containers stopped. (Ollama left running -- it's a shared service.)"
    exit 0
}

# ── 0. config ────────────────────────────────────────────────────────────────
if (-not (Test-Path $EnvFile)) { Die "$EnvFile not found. Copy .env.example and configure a local backend, or pass -EnvFile." }
$cfg = Read-EnvFile $EnvFile
Info "Using config: $EnvFile"

# Export RUNPOD_POD_ID (if present) so the webapp child process inherits it.
# The mimic project composes its remote LLM URL from ${RUNPOD_POD_ID}; pydantic's
# --env loading populates the Settings object, not os.environ, so the resolver
# would not otherwise see it. Only this one key is exported -- the rest of the
# env file reaches the app via --env.
if ($cfg["RUNPOD_POD_ID"]) {
    $env:RUNPOD_POD_ID = $cfg["RUNPOD_POD_ID"]
    Info "RunPod pod id set: $($cfg['RUNPOD_POD_ID']) (remote LLM available to cloud projects)."
}

$baseUrl = $cfg["LLM_BASE_URL"]; if (-not $baseUrl) { $baseUrl = $cfg["OPENAI_BASE_URL"] }
if (-not $baseUrl) { Warn "No LLM_BASE_URL in $EnvFile -- this profile is NOT air-gapped (would use the cloud)." }

# Distinct, non-empty model names referenced by the profile.
$models = @($cfg["LLM_MODEL"], $cfg["QUERY_MODEL"], $cfg["ONTOLOGY_GEN_MODEL"], $cfg["ONTOLOGY_JUDGE_MODEL"]) |
          Where-Object { $_ } | Select-Object -Unique

New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "runtime") | Out-Null

# ── 1. infra plane: db + fuseki via Docker (infra-only; app profile NOT started) ─
Info "Starting infra (MySQL + Fuseki) via Docker..."
$compose = Get-Compose
& $compose[0] ($compose[1..($compose.Count-1)] + @("up", "-d", "db", "fuseki"))
if ($LASTEXITCODE -ne 0) { Die "docker compose failed to start db + fuseki." }
Good "Infra containers up (db:3307, fuseki:3030)."

# ── 2. app plane: Ollama server ──────────────────────────────────────────────
if (-not (Test-Path $OllamaExe)) {
    Die "Ollama not installed. Install it (winget install Ollama.Ollama) or run with a cloud profile."
}
function Test-Ollama {
    try { Invoke-WebRequest "http://localhost:11434/api/tags" -TimeoutSec 3 -UseBasicParsing | Out-Null; return $true }
    catch { return $false }
}
if (Test-Ollama) {
    Good "Ollama server already running."
} else {
    Info "Starting Ollama server..."
    Start-Process $OllamaExe -ArgumentList "serve" -WindowStyle Hidden
    $up = $false
    foreach ($i in 1..15) { Start-Sleep 1; if (Test-Ollama) { $up = $true; break } }
    if ($up) { Good "Ollama server up." } else { Die "Ollama server did not come up." }
}

# ── 3. models present? ───────────────────────────────────────────────────────
$installed = (& $OllamaExe list) 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -split "\s+")[0] }
foreach ($m in $models) {
    if ($installed -contains $m) {
        Good "Model present: $m"
    } elseif ($SkipModelPull) {
        Warn "Model MISSING: $m  (run: ollama pull $m)"
    } else {
        Info "Pulling model: $m  (this can be several GB)..."
        & $OllamaExe pull $m
        if ($LASTEXITCODE -ne 0) { Die "Failed to pull $m" }
        Good "Pulled: $m"
    }
}

# ── 4. embedding cache (the air-gap gotcha) ──────────────────────────────────
Info "Checking local embedding model cache (all-MiniLM-L6-v2)..."
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
# Write the probe to a temp file rather than passing multi-line source via -c:
# PowerShell mangles indented here-strings as a single native arg, and with
# $ErrorActionPreference='Stop' any stderr would halt the script.
$probe = @'
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer("all-MiniLM-L6-v2")
    print("CACHED")
except Exception as e:
    print("MISSING:" + str(e)[:160])
'@
$probeFile = Join-Path $env:TEMP "forgemesh_embed_probe.py"
Set-Content -Path $probeFile -Value $probe -Encoding utf8
$embedResult = ""
try { $embedResult = & $py -X utf8 $probeFile 2>$null } catch { $embedResult = "MISSING:$($_.Exception.Message)" }
Remove-Item $probeFile -ErrorAction SilentlyContinue
if ($embedResult -match "CACHED") {
    Good "Embedding model cached locally."
} else {
    Warn "Embedding model not cached. Pre-cache it ONCE while online:"
    Warn "  $py -c `"from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')`""
    Warn "(ForgeMesh will try to download it on first use -- which breaks a true air gap.)"
}

# ── 5. preflight ─────────────────────────────────────────────────────────────
Info "Running air-gap preflight..."
& $py -X utf8 (Join-Path $PSScriptRoot "scripts\check_local_llm.py") $EnvFile
if ($LASTEXITCODE -ne 0) { Warn "Preflight reported issues (see above). Continuing to launch anyway." }

# ── 6. launch webapp natively (FOREGROUND) ───────────────────────────────────
# Native (not the docker 'webapp' container) so it can reach the host's Ollama
# and the embedding cache. Run in the FOREGROUND with the call operator so the
# webapp is a child of this console and Ctrl+C propagates to it (Start-Process
# detaches the child, leaving it orphaned and immune to Ctrl+C).
Info "Launching webapp on http://127.0.0.1:$Port"
Good "Everything is local. Open http://127.0.0.1:$Port"
Info "Press Ctrl+C here to stop the webapp. (Infra containers keep running; use -Stop to stop those too.)"

# Record our own PID so `-Stop` from another terminal can still find/kill the
# uvicorn child tree if needed.
$PID | Out-File -Encoding ascii $WebPidFile
try {
    & $py -X utf8 web_app.py --env $EnvFile --host 127.0.0.1 --port $Port
} finally {
    Remove-Item $WebPidFile -ErrorAction SilentlyContinue
    Info "Webapp stopped."
}
