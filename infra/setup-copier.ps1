<#
  setup-copier.ps1 - one-shot copier bootstrap for a fresh Windows VM.

  Run from the COPIER REPO ROOT (the folder with requirements.txt + app\), in an
  ELEVATED PowerShell:

      Set-ExecutionPolicy -Scope Process Bypass -Force
      .\infra\setup-copier.ps1

  It installs Python 3.12, creates the venv, installs deps, and drops a TEMPLATE .env
  plus a run-copier.ps1 helper. It contains NO secrets - after it runs you paste your
  COPIER_KEY and Telegram bot token into .env yourself.

  MIGRATE NOTE: this VM reuses your EXISTING copier key, so the account stays assigned.
  Run only ONE copier per key - STOP the old (local) copier before starting this one, or
  you'll get double orders.
#>

#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$pyVersion = '3.12.8'   # bump if the python.org download 404s

Write-Host "== Copier VM bootstrap ==" -ForegroundColor Cyan

# 0) sanity: are we in the repo root?
if (-not (Test-Path '.\requirements.txt') -or -not (Test-Path '.\app')) {
  throw "Run this from the copier repo root (requirements.txt and app\ must be present)."
}

# 1) Python 3.12 -------------------------------------------------------------------
function Get-Py312 {
  foreach ($c in @('py','python')) {
    try {
      $v = & $c -3.12 --version 2>$null
      if ($LASTEXITCODE -eq 0 -and $v -match '3\.12') { return @($c, '-3.12') }
    } catch {}
    try {
      $v = & $c --version 2>&1
      if ($v -match '3\.12') { return @($c) }
    } catch {}
  }
  return $null
}

$pyCmd = Get-Py312
if (-not $pyCmd) {
  Write-Host "Python 3.12 not found - downloading $pyVersion..." -ForegroundColor Yellow
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  $url = "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-amd64.exe"
  $exe = Join-Path $env:TEMP "python-$pyVersion-amd64.exe"
  Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
  Write-Host "Installing Python (silent, all users, PATH)..." -ForegroundColor Yellow
  Start-Process -FilePath $exe -Wait -ArgumentList `
    '/quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_launcher=1'
  # refresh PATH in THIS session so 'python' resolves right away
  $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
              [Environment]::GetEnvironmentVariable('Path','User')
  $pyCmd = Get-Py312
  if (-not $pyCmd) { throw "Python 3.12 still not found after install - open a new shell and re-run." }
}
Write-Host ("Python: " + (& $pyCmd[0] @($pyCmd[1..($pyCmd.Length-1)]) --version 2>&1)) -ForegroundColor Green

# 2) Edge present? (the copier drives Edge on the debug port; it does NOT install it) -
$edge = @(
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($edge) { Write-Host "Edge: $edge" -ForegroundColor Green }
else { Write-Host "WARNING: Microsoft Edge not found - install it before launching the terminal." -ForegroundColor Yellow }

# 3) venv + dependencies -----------------------------------------------------------
if (-not (Test-Path '.\.venv')) {
  Write-Host "Creating .venv..." -ForegroundColor Yellow
  & $pyCmd[0] @($pyCmd[1..($pyCmd.Length-1)]) -m venv .venv
}
$venvPy = '.\.venv\Scripts\python.exe'
Write-Host "Installing dependencies..." -ForegroundColor Yellow
& $venvPy -m pip install --upgrade pip
& $venvPy -m pip install -r requirements.txt
Write-Host "Dependencies installed." -ForegroundColor Green

# 4) template .env (never clobber an existing one) ---------------------------------
if (Test-Path '.\.env') {
  Write-Host ".env already exists - leaving it untouched." -ForegroundColor Yellow
} else {
  $envText = @'
# --- fill SENDER_BASE_URL + COPIER_KEY (+ Telegram below to enable alerts) ---
SENDER_BASE_URL=https://YOUR-SENDER.azurewebsites.net
COPIER_KEY=PASTE_EXISTING_COPIER_KEY_HERE
COPIER_NAME=VM-AZURE-1
EXECUTOR_ENABLED=true
DATA_DIR=./data
POLL_SEC=1

# local dashboard bind. 0.0.0.0 = all interfaces (still only reachable ON the VM unless you
# open port 8100 in the NSG - which you should NOT: the dashboard drives the terminal).
# Set 127.0.0.1 to hard-restrict the dashboard to the VM itself.
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=8100

# maintenance: recycle the driver (keeps login), never full browser restart
BROWSER_RESTART_SEC=0
DRIVER_RECYCLE_SEC=1800
TAB_REFRESH_SEC=600
EXEC_IDLE_SEC=0.25
# a slower VM needs a bigger market fill-verify window (the Positions widget lags the fill)
NET_VERIFY_SEC=20

# Telegram alerts (optional). Leave the token blank to keep Telegram OFF. To enable,
# paste the bot token + chat id and uncomment/fill the topic ids (see TELEGRAM_SETUP.md).
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
# TELEGRAM_TOPIC_COPIER=
# TELEGRAM_TOPIC_ERRORS=
# TELEGRAM_TOPIC_WARNINGS=
# TELEGRAM_TOPIC_ACTIVITY=
LOGIN_ALERT_REPEAT_SEC=300
'@
  Set-Content -Path '.\.env' -Value $envText -Encoding ascii
  Write-Host "Wrote template .env - edit it and paste COPIER_KEY + TELEGRAM_BOT_TOKEN." -ForegroundColor Green
}

# 5) run helper --------------------------------------------------------------------
$runText = @'
# Starts the copier dashboard + reception/executor (honors DASHBOARD_HOST/PORT from .env).
# On the VM, open http://127.0.0.1:8100
.\.venv\Scripts\python.exe -m app.main
'@
Set-Content -Path '.\run-copier.ps1' -Value $runText -Encoding ascii

Write-Host ""
Write-Host "== Done. Next steps ==" -ForegroundColor Cyan
Write-Host "1. Edit .env  -> set SENDER_BASE_URL + COPIER_KEY (+ Telegram token/chat/topics to enable alerts)"
Write-Host "2. Start the old (local) copier's shutdown FIRST - one copier per key!"
Write-Host "3. .\run-copier.ps1        (starts the copier on http://127.0.0.1:8100)"
Write-Host "4. In the dashboard click 'Start Tradovate terminal', log into Tradovate by hand"
Write-Host "5. Confirm executor LIVE + logged in, then DISCONNECT RDP (do NOT log off)"
