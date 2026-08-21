# Copier Service

The local half of the Sender/Copier system. One copier runs on each Windows VM and
controls one trading terminal/tab. It receives trade signals from the Sender
(the prop-dashboard signal provider), converts them into per-account actions, and
queues them durably for terminal execution.

## Architecture — three decoupled parts

```
Signal Provider ──▶ [2] Signal Receiver ─▶ Processor ─▶ Local Action Queue ─▶ [3] Terminal Executor
                          │                                    │                     (Part 3, later)
                          └────────────── [1] Local Dashboard ─┘
```

| Part | What | Status |
|------|------|--------|
| 1 | **Local dashboard** — health, accounts, queue, signals, activity log at `http://<vm>:8100/` | ✅ built |
| 2 | **Reception & queue** — register/heartbeat/poll the Sender, process signals, durable SQLite action queue | ✅ built |
| 3 | **Terminal executor** — consume the queue serially, drive the Tradovate terminal, report status back | ✅ built |

Decoupling is the point: reception must capture signals in milliseconds even while the
terminal (later) spends seconds executing — **no signal is ever lost because the
terminal is busy**. The queue (SQLite, WAL) is the buffer and the audit trail; the
poll cursor is persisted, so a restart resumes exactly where it left off.

### Processing rules
- One **Action** per (signal, revision, account). The executor later performs the
  terminal mechanics (switch account → switch symbol → place) inside that one action.
- Per-account quantity = `max(1, ceil(base_qty × copy_ratio))`.
- Idempotent by `(signal_id, revision)` — re-delivery never duplicates work.
- A new revision **supersedes** the older revision's queued actions.
- A cancelled/expired signal **cancels** its queued actions.
- `exit` on symbol `*` is the Sender's **flatten-all** sentinel.
- The poll `since` cursor is opaque — stored and echoed, never parsed.

## Run locally

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
copy .env.example .env    # fill in SENDER_BASE_URL / COPIER_KEY / COPIER_NAME
.venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8100
```

Open `http://localhost:8100/` — the dashboard self-refreshes every 2 s.

## Run in Docker (the VM deployment shape)

```bash
docker compose up -d --build
```

`.env` next to the compose file supplies `SENDER_BASE_URL`, `COPIER_KEY`, `COPIER_NAME`.
State persists in the `copier-data` volume. `host.docker.internal` reaches the VM host
(used later by Part 3's terminal bridge, or a locally-running Sender in development).

## Configuration (env)

| Var | Default | Meaning |
|-----|---------|---------|
| `COPIER_NAME` | `copier-1` | must match a Copier created in the Sender's Config tab |
| `SENDER_BASE_URL` | — | the Sender, e.g. `https://YOUR-SENDER.azurewebsites.net` |
| `COPIER_KEY` | — | the one-time API key the Sender's Config tab issued |
| `DASHBOARD_PORT` | `8100` | local dashboard port |
| `DATA_DIR` | `./data` (`/data` in Docker) | SQLite queue + audit trail |
| `POLL_SEC` | from Sender config | override the command poll cadence |
| `HEARTBEAT_SEC` | timeout/3 | override the heartbeat cadence |

## Tests

```bash
.venv/Scripts/python -m pytest
```

34 tests: quantity scaling, per-account fan-out, every order kind (market/bid/ask/
limit/stop/stop-limit/exit/flatten-all), dedup on re-delivery, revision supersede,
cancel handling, expiry, cursor persistence across restart, burst capture without
loss, the dashboard/health endpoints, and the failure modes: a **poison command is
isolated** (logged loudly, never wedges the queue), a heartbeat or register outage
never blocks reception (cached registration keeps routing alive), a Sender outage
degrades health without crashing, and even a broken storage layer cannot kill the
reception thread. The suite runs against a **fake Sender** that replicates the real
`/api/copier/*` contract, so no network or real Sender is needed.

## End-to-end validation against the real Sender

With the Sender repo checked out locally (its dev DB running):

```bash
# 1. seed an E2E copier + 2 accounts (+ group) in the Sender's dev DB  [Sender venv]
cd D:\Codes\prop-dashboard
.venv\Scripts\python.exe D:\Codes\copier-service\scripts\e2e_seed_sender.py

# 2. run the Sender (0.0.0.0 so Docker can reach it) and the copier
.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8010
# (copier: locally or via docker run with SENDER_BASE_URL=http://host.docker.internal:8010)

# 3. publish a mixed signal batch (market, two coexisting limits, stop-limit,
#    exit, flatten-all, then cancel one limit)                        [Sender venv]
.venv\Scripts\python.exe D:\Codes\copier-service\scripts\e2e_send_signals.py <master_id> <group_id>

# 4. assert everything arrived, scaled, queued, superseded, cancelled  [copier venv]
.venv\Scripts\python.exe scripts\e2e_check.py http://127.0.0.1:8100
```

Note: a copier that starts **after** a signal was cancelled receives only the signal's
latest state and correctly never queues the cancelled order (late-joiner semantics).

## Part 3 — terminal execution

The `TerminalWorker` (app/executor.py) is a second daemon thread and the ONLY thing
that touches the terminal, so all interaction is serial by construction. It attaches
to the human-opened, logged-in Tradovate window over CDP (never launches or logs in)
through the `TerminalGateway` (app/terminal.py) — the one boundary behind which the
validated Selenium modules (copied as-is into `trading/ browser/ config/ core/`) live.

**Mandatory chain before every action:**
`connected → logged in → not stale → kill switch allows → correct account → execute`.
The symbol check is embedded in each execution path (the OrderTicket sets+verifies the
symbol; the chart panel verifies `expect_symbol`).

**Execution routing (chosen for reliability):**
- market / limit / stop → the **OrderTicket** (the hardened module) — stays reliable
  even with the Order Ticket widget open, which the chart panel does not.
- exit / flatten → an **opposite MARKET** via the OrderTicket, with a **unit-lot
  fallback** (qty=1 is the most reliable preset) so getting flat is bulletproof.
- market/exit verify the net position moved as expected before reporting success.
- bid / ask → the chart panel (only path with a native join-the-book control).

**Feedback (mandatory):** every action's lifecycle — `queued → executing →
done/failed/skipped` — is written to the local Action row + the activity log (visible
on the dashboard) AND posted to the Sender via `/api/copier/reports`. Terminal
snapshots (positions + working orders of the active account) post via
`/api/copier/state` on a relaxed cadence.

**Kill switch:** before executing, the worker checks the Sender's per-account
`trading_allowed` (cached, re-fetched every few seconds). Killed → the action is
**skipped and reported, never executed**. If the switch state can't be fetched and the
cache is stale, execution **holds** (fail-closed).

**Hold vs skip vs fail:** transient doubt (not logged in, kill-switch unknown) → the
action stays queued and retries; a staleness guard skips actions older than
`MAX_ACTION_AGE_SEC` so a restart never fires a burst of old trades; explicit stop
(kill switch) → skipped; execution error → failed with the terminal's own reason. The
worker thread itself can never die.

**Monitoring + keep-alive:** when the queue is idle the worker snapshots the terminal
every `STATE_POLL_SEC` and sends a harmless keep-alive nudge (mouse-move + zero scroll,
self-throttled) so the tab never idles out. No trading action is ever used to keep alive.

**Arming:** the executor is OFF unless `EXECUTOR_ENABLED=true`. Reception/queueing
(Parts 1–2) always run; Part 3 is opt-in per VM.

### Known limitations (tracked)
- **Stop-Limit** placement is not yet wired (needs the ticket's two-price path); such
  actions fail cleanly with an explicit reason rather than misfiring.
- **Per-order cancel** is not automated: `exit` flattens the position and flags any
  working orders for that symbol to cancel manually (Tradovate's Orders panel has a
  Cancel-All). Signal-level cancel still cancels *queued* actions before execution.
- The qty **preset dropdown** can occasionally refuse a multi-lot value; entries then
  fail loudly (re-send), and flatten falls back to unit lots.

### Part 3 config (env)
| Var | Default | Meaning |
|-----|---------|---------|
| `EXECUTOR_ENABLED` | `false` | arm the terminal executor |
| `MAX_ACTION_AGE_SEC` | `300` | skip (never fire) actions older than this |
| `STATE_POLL_SEC` | `15` | terminal monitor / snapshot cadence |
| `KILLSWITCH_TTL_SEC` | `5` | re-fetch kill-switch state after this |
| `KILLSWITCH_STALE_BLOCK_SEC` | `60` | hold execution if state is older than this and unfetchable |

### Full-flow E2E
`scripts/e2e_execution.py` drives signal → reception → execution across every order
type (market, limit, stop, exit, flatten), plus the kill-switch skip and the
stop-limit negative case, asserting the terminal outcome AND the Sender-side execution
reports for each. Run it with the Sender venv against a running executor-armed copier.
