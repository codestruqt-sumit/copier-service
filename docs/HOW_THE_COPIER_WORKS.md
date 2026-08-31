# How the Copier Works — Web & API Execution Modes

> **Status:** the Copier at commit `0a7235c` runs one execution mode today — **Web
> (Selenium)** — which is live and ATS-validated. This document restructures the design
> around **two selectable modes, Web and API**, so the API mode can be built *behind the
> same seam* without touching the web path. Web-mode sections describe shipping code;
> API-mode sections marked **(to be implemented)** are the build spec.
>
> **Living document rule:** when code changes, update the matching section in the same
> commit and add a changelog row (§14). If doc and code disagree, the code wins — fix the
> doc.
>
> **Scope:** the *Copier* (`D:\Codes\copier-service`). The *Sender* (`D:\Codes\prop-dashboard`,
> the master portal) is a separate repo, described here only as the contract we consume.

---

## 0. The one idea

A copier does three things, in order, and **mode changes only the last two**:

```
   MATCHING            PROMPTING              EXECUTION
   (shared)            (mode-specific)        (mode-specific)
   ────────            ───────────────        ──────────────
   signal  ->  turn    establish a trading    place / verify / report
   into per-account    session with the       the order on the broker
   actions             broker
```

- **Matching** — receive a signal, apply the rules, enqueue one action per target
  account. **Identical in both modes.**
- **Prompting** — establish an authenticated trading session. *Web:* a human logs into the
  browser terminal. *API:* the process authenticates to Tradovate (direct credentials, or
  **OAuth** for normal end users).
- **Execution** — actually place the order and confirm the outcome. *Web:* drive the
  browser DOM and read the screen. *API:* call the REST endpoint and read the WebSocket
  event stream.

Everything above the **gateway seam** (§8) is matching + the safety chain and is
mode-agnostic. The mode swaps out exactly one object behind that seam. **Only one mode is
active per process** (§2).

---

## 1. What it is, in one paragraph

A master trader places orders on the **Sender**. The Sender publishes each as a **signal**.
This **Copier** pulls those signals over HTTPS, matches each into per-account work, and
**executes** it against Tradovate — in **Web mode** by driving a logged-in browser terminal
with Selenium, or (planned) in **API mode** by calling the Tradovate REST/WebSocket API —
then reports back truthfully what actually happened, verified against the broker's own
state. One copier process serves one broker identity (one `COPIER_KEY`).

Guiding principle, both modes: **never claim an outcome we did not verify, and never send
an order we cannot prove is safe to send.**

---

## 2. Execution modes and the segregation guarantee

The mode is picked **at boot**, from two places (dashboard choice wins):

```
COPIER_MODE = web|api        # .env default (web if unset)
UI: /oauth/tradovate         # "Switch mode" buttons -> stored in the local DB
                             #  (KV copier_mode_override) -> APPLIED ON RESTART
```

A mode change is deliberately restart-applied: the gateway is constructed once at boot, so
the transport can never swap mid-run (and never mid-order). The status page shows both the
ACTIVE mode (this run) and the STORED mode (next boot) with a restart banner when they
differ.

**How the switch works (clean, one place):** a small factory (`app/gateways/__init__.py:
build_gateway(settings)`) reads `COPIER_MODE` and returns exactly one gateway object that
satisfies the seam (§8). `app/main.py` calls the factory; nothing else in the codebase
knows which mode is running.

**The segregation guarantee — enforced structurally, not by discipline:**

| In `web` mode | In `api` mode |
|---|---|
| `TerminalGateway` is constructed (Selenium/CDP attach) | `TradovateGateway` is constructed (REST/WS client) |
| No Tradovate API client is instantiated, no token is minted, no socket opens | **No browser is launched, no CDP attach, no DOM is ever read** |
| The maintenance subsystem (tab refresh / driver recycle / browser restart) runs | The maintenance subsystem is inert (the API gateway's maintenance methods are no-ops) |

Because the factory constructs **only** the selected gateway, the unselected transport
never runs: selecting `api` means the web flow genuinely does not function (no browser),
and selecting `web` means no API calls to Tradovate are made. The two implementations share
**no runtime state** — only the seam's method signatures. Adding a third transport later
(e.g. a different broker) is one new gateway class + one factory branch; nothing above the
seam changes.

Everything in §§3–7 below is **shared** and runs identically regardless of mode.

---

## 3. The big picture (topology)

```
  MASTER TRADER
       |  places an order
       v
  +--------------+   HTTPS (copier pulls)        +-------------------------------+
  |    SENDER     |<------------------------------|          COPIER (this repo)   |
  | prop-         |   GET  /api/copier/commands   |                               |
  | dashboard     |------------------------------>|  Poller thread   \            |
  | (master)      |   POST /api/copier/reports    |    -> Processor    } MATCHING  |
  |               |<------------------------------|    -> SQLite queue/            |
  |               |   POST /api/copier/state      |                               |
  |               |   POST /api/copier/accounts   |  Executor thread  (safety)    |
  |               |   GET  /api/copier/killswitch |    -> build_gateway(mode)     |
  +--------------+                               |          |                    |
                                                 |   +------+------+   the SEAM   |
                                                 |   |             |             |
                                                 |  WEB          API             |
                                                 | TerminalGw   TradovateGw      |
                                                 |   | CDP         | REST+WS      |
                                                 |   v             v             |
                                                 | Edge/Tradovate  Tradovate     |
                                                 | web terminal    API servers   |
                                                 +-------------------------------+
                                                   Dashboard thread -> operator
```

Three threads in one FastAPI process (`app/main.py: create_app`): **Poller**
(`app/poller.py`), **Executor** (`app/executor.py`), **Dashboard/ASGI**
(`app/dashboard.py`). The Executor obtains its gateway from `build_gateway(settings)` and
otherwise does not care which mode it is.

---

## 4. MATCHING — the Sender contract (shared, mode-independent)

All under `/api/copier/*`, authenticated with the `X-Copier-Key` header
(`app/sender_client.py`). Both modes speak this contract byte-for-byte.

| Method + path | Direction | Purpose |
|---|---|---|
| `POST /api/copier/register` | copier -> sender | announce identity + which local accounts/groups this copier serves; returns the **registration** (accounts, groups, copy_ratio per account) |
| `POST /api/copier/heartbeat` | copier -> sender | liveness + health |
| `GET  /api/copier/commands?since=<cursor>` | copier -> sender | pull the next batch of signals + a new opaque cursor |
| `GET  /api/copier/killswitch` | copier -> sender | per-account trading-allowed state (emergency stop) |
| `POST /api/copier/reports` | copier -> sender | execution outcomes (per signal/revision/account) |
| `POST /api/copier/state` | copier -> sender | live positions + working orders for the active account |
| `POST /api/copier/accounts` | copier -> sender | consolidated P/L + Net Liq snapshot |

**Command fields:** `signal_id`, `revision`, `status`
(`published`/`updated`/`cancelled`/`expired`), `symbol`, `order_kind`
(`market`/`bid`/`ask`/`limit`/`stop`/`stop_limit`/`exit`), `side`, `base_qty`,
`limit_price`, `stop_price`, `tif`, `target_groups`, `valid_until`.

**Report fields:** `signal_id`, `revision`, `account_ref`, `status`, `resolved_qty`,
`order_ref`, `error`. The current edition emits `executing` -> `filled` | `failed`, or
`skipped`. (`partial`/`working` are accepted by the Sender and become useful in API mode,
which can *observe* a partial fill — see §10.)

**Cursor semantics:** opaque, stored in local KV. Staleness is measured from the signal's
enqueue time on the Sender, which is why a fresh copier must not replay history (§6).

---

## 5. MATCHING — Poller -> Processor -> queue (shared)

**Poller** (`app/poller.py: Poller.cycle`): ensure registered, heartbeat if due,
`GET /commands?since=cursor`, hand the batch to `process_batch`, persist the new cursor.
Cadence `POLL_SEC` (VM 0.5s).

**Processor** (`app/processor.py: process_batch` -> `_process_one`): turns commands into
`Action` rows with **per-command isolation** (a poison command rolls back + logs, the loop
continues). Rules, each test-covered:

- **Idempotent by `(signal_id, revision)`** — `signals_seen`, unique `uq_signal_revision`.
- **One `Action` per `(signal, revision, account)`** — unique `uq_action_per_account`.
- **Quantity scaling** — `qty = max(1, ceil(base_qty x copy_ratio))` per account.
- **Supersede** — a newer revision marks the older revision's queued actions `superseded`.
- **Cancel** — a `cancelled`/`expired` status cancels queued actions. If already placed on
  the broker, web mode logs a loud "cancel manually" (no per-order cancel); **API mode can
  actually cancel by `order_ref`** — see §10 and §12.
- **Fan-out by group** — `target_accounts` maps `target_groups` to local accounts.
- **`order_kind: exit` + `symbol: '*'`** is the flatten-all sentinel -> `flatten_all`.

`order_kind` -> action `kind`: market->`place_market`, bid->`place_bid`, ask->`place_ask`,
limit->`place_limit`, stop->`place_stop`, stop_limit->`place_stop_limit`,
exit->`exit_symbol` (or `flatten_all`).

---

## 6. The action queue + fresh-start guard (shared)

Durable **SQLite (WAL)**, four tables (`app/models.py`): `signals_seen`, `actions`
(`queued -> executing -> done | failed`, or `cancelled | superseded | skipped`), `events`,
`kv`. `actions.order_ref` holds the broker order id once a resting order is placed — the
handle a future per-order cancel uses.

**Fresh-start guard** (`poller.skip_to_latest`, `replay_backlog_on_start=false`): a new
copier stores the latest cursor *without* processing ("Fresh start: skipped N …"). The
dashboard's **"Clear queue & skip to now"** button does the same on demand.

---

## 7. EXECUTION — the Executor safety chain (shared)

`app/executor.py: TerminalWorker.step` pops the oldest `queued` action and runs
`_run_action` — the **mandatory safety chain**, identical in both modes because it runs
*above* the seam:

1. **Staleness guard** — queued > `max_action_age_sec` (300s) -> `skipped`.
2. **Login/session check** — `gateway.login_check()`; not authenticated -> `held` (requeue).
3. **Kill switch** — per-account, from the Sender. Blocked -> `skipped`; unknown -> `held`.
4. Mark `executing` + report to the Sender.
5. **Account check** — `gateway.ensure_account(ref)` (switch if needed).
6. **Execute** — `gateway.execute(action_dict)` -> `{outcome, order_ref, detail}`.

`filled`/`executing` -> `done`; else `failed`. **Serial by design** in web mode (one shared
UI). API mode *may* relax this later, but keeps it initially. Cooperative abort/timeout,
staggered idle monitoring, and crash reconciliation (an `executing` action left by a kill
is `failed` without retry) are all shared.

> The two visual diagrams (topology; the six-gate safety chain) live alongside this doc.

---

## 8. The gateway seam — the mode boundary

The Executor depends on **exactly this interface**. A mode is a class that implements it.
`build_gateway(settings)` returns one instance of it.

```
ensure_connected()      -> (ok, detail)   # web: attach browser · api: ensure valid token
login_check()           -> (ok, detail)   # web: DOM session markers · api: token valid + not evicted
active_account()        -> str | None
ensure_account(ref)     -> (ok, detail)   # web: click selector · api: no-op (accountId is per-call)
execute(action)         -> {outcome, order_ref, detail}   # THE order path
read_state()            -> {account, positions, working_orders}
read_accounts_summary() -> [ {account, open_pl, total_pl, net_liq}, ... ]
keepalive()             -> bool           # web: anti-idle · api: no-op
refresh_tab()           -> (ok, detail)   # web: reload tab · api: no-op
recycle_driver()        -> (ok, detail)   # web: fresh driver · api: no-op
restart_browser()       -> (ok, detail)   # web: relaunch · api: no-op (or reconnect socket)
+ attribute: gateway.abort_check = callable | None
```

`execute()` dispatches by `kind` to per-kind handlers. The **contract of the return dict is
the mode-independent truth the Executor trusts**; how each mode fills it is §9 / §10.

---

## 9. WEB MODE (current, implemented) — `TerminalGateway` (`app/terminal.py`)

Drives a logged-in Tradovate web terminal via Selenium/CDP (Edge on port 9250). Because the
DOM gives no clean order acknowledgement, every path **verifies against the terminal's own
state** and reports honestly:

- **Prompting** — a human logs into the browser once; the profile persists the login. The
  copier never handles Tradovate credentials. `login_check` reads session DOM markers.
- **Thorough reads** — Positions/Orders rows virtualize out of the DOM below the fold; every
  *decision* read is visible-first then scrolled-fallback (`_net_thorough`,
  `_find_working_thorough`).
- **Market** — one-click Buy/Sell Mkt; verify net moved (`_await_net`); ticket fallback only
  when it can prove nothing landed; never double-sends.
- **Bid/Ask** — classify to working-order / verified-fill / one safe re-fire / honest "sent".
- **Exit / flatten** — Exit-at-Mkt&Cxl then verify net == 0; warn if a resting order
  survived below the fold.
- **Latency truth** — `[live +Xs, verified +Ys]` on every success.

**Measured:** warmed entries ~6.5s live, exits ~5.5s, signal->verified ~7-11s; degrades
correctly under margin lockout. Details of the correctness machinery are in the code and
the git history; this section is the map, not the territory.

---

## 10. API MODE (to be implemented) — `TradovateGateway`

Speaks Tradovate's REST + WebSocket API. No browser, no DOM, no scraping. The API *tells*
you the outcome (order id, fill, rejection reason) instead of making you infer it.

### 10a. Prompting — two authentication models

Tradovate exposes **two** ways to get the Bearer token the trading API needs. The mode
must support both, chosen by `TRADOVATE_AUTH = oauth | credentials`.

**Model A — Direct credentials** (`credentials`). `POST /auth/accesstokenrequest` with
`{name, password, appId, appVersion, cid, sec}` -> `accessToken` (+ `mdAccessToken`),
90-min TTL, renew via `GET /auth/renewaccesstoken`. Good for a **self-hosted bot with its
own dedicated API user** you control. The process holds the password (in a gitignored
`.env`, `SecretStr`, never logged). No partner approval needed. *This is the model our
earlier recon covered in depth.*

**Model B — OAuth authorization code** (`oauth`) — **the model for normal end users**
("user-to-user"): the user authorizes *their own* Tradovate account without ever giving us
their password. Requires a partner `client_id`/`client_secret`/approved `redirect_uri`
(issued only to approved Tradovate / NinjaTrader Ecosystem partners after review).

### 10b. The OAuth flow (verified against Tradovate's official example + partner docs)

```
  1. Copier redirects the user's browser to Tradovate's own login page:
       https://trader.tradovate.com/oauth
         ?response_type=code
         &client_id=<CLIENT_ID>
         &redirect_uri=<REDIRECT_URI>
         &state=<random-anti-CSRF>          # we add this; verify it on return
  2. User logs into Tradovate + approves.  (We never see their password.)
  3. Tradovate redirects to our callback:  <REDIRECT_URI>?code=<CODE>&state=<...>
  4. Copier exchanges the code (server-to-server, client_secret stays server-side):
       POST /auth/oauthtoken
         grant_type=authorization_code
         code=<CODE>  client_id=<...>  client_secret=<...>  redirect_uri=<...>
     ->  { access_token, token_type: "Bearer", expires_in,
           refresh_token, refresh_token_expires_in }
  5. Use access_token as `Authorization: Bearer <token>` on the trading REST/WS API.
  6. Before expiry, refresh WITHOUT re-prompting the user:
       POST /auth/oauthtoken  grant_type=refresh_token  refresh_token=<...>  client_id=... client_secret=...
```

Endpoints (host per environment): demo `https://demo.tradovateapi.com/v1/auth/oauthtoken`,
live `https://live.tradovateapi.com/auth/oauthtoken`. **Open items to verify at build time:**
whether the OAuth `access_token` also yields an `mdAccessToken` (market data may still need
the separate MD flow — but order mirroring needs no market data); and the exact
`refresh_token` rotation/expiry behaviour.

**The callback + token store are PER-VM and LOCAL — not central.** This copier is deployed
one-per-VM (one user / one `COPIER_KEY`). In API mode the user does OAuth *inside their own
VM*: the copier hosts the `/oauth/tradovate/callback` route on its **own local dashboard**,
catches the `code`, exchanges it, and stores the token **locally** (encrypted, in the
copier's data dir). There is no central token store — the "system" the user OAuths in is
their VM, exactly like the Selenium mode's browser login is local to that VM.

**The redirect_uri is the local loopback, derived dynamically** (`settings.oauth_redirect_uri`):
by default `http://localhost:{DASHBOARD_PORT}/oauth/tradovate/callback`. Because every VM
uses the *same loopback string*, **one registered redirect_uri covers all VMs** — the
browser doing the login runs inside the VM, so `localhost` is reachable. Set
`TRADOVATE_REDIRECT_URI` only to override for a VM reached by a real hostname. (Requires
Tradovate to allow a loopback/`http://localhost` redirect for the client_id — the
installed-app pattern; confirm what is registered.)

**Decision to make before building API mode:** are we (a) a self-hosted bot on our *own*
prop accounts -> **Model A** with a dedicated, scoped API user (fastest, no partner gate),
or (b) each user connecting their *own* account per-VM via **Model B** OAuth (the
individual-account model: local callback, local token, needs partner `client_id`/`_secret`
whitelisted for the loopback redirect). The doc supports both; the choice sets which one we
implement first.

### 10c. Build status (what's real now vs next)

**Built (connection layer):** `COPIER_MODE=api` selects `TradovateGateway`; the copier's
local dashboard hosts the OAuth pages — `GET /oauth/tradovate` (the connect + status page),
`/start`, `/callback`, `/status.json`, `/disconnect`. The callback exchanges the code, stores
the token locally (`app/gateways/api/token_store.py`, in the SQLite KV table), and the
gateway's `login_check`/`ensure_connected` pass off that token with background refresh. The
status page proves the token by listing the accounts it can see. Secrets: `client_secret` is
a `SecretStr` (never logged); the token lives only on the VM.

**Built (execution layer):** `TradovateGateway.execute()` mirrors the web automation
kind-for-kind over REST — `place_market` (verify net moved), `place_limit`/`place_stop`/
`place_stop_limit` (classify Working -> executing / Filled -> filled / Rejected -> failed),
`exit_symbol` (cancel working orders + flatten + verify net 0), `flatten_all`, and
`place_bid`/`place_ask` (honour a `limit_price` as a Limit, else **fail loudly** — no
market-data touch primitive). Same discipline as web mode: a rejection (200 + `failureReason`)
is now a **visible** reason; a net that didn't move is reported honestly, never as a fill;
an **ambiguous send** (bytes left the host, no clean response) is **never retried** — it
fails loud ("VERIFY manually"). Verification is REST polling (net position / order status);
a full **WebSocket fill feed** (catching async `RiskRejected`) is the next hardening. Unit
tested with a fake REST (`tests/test_api_execute.py`); not yet live-validated (blocked on a
completed OAuth connection — same "built + unit-tested, pending live ATS" posture web mode
had before its runs).

### 10e. Execution — how `TradovateGateway.execute` will fill the seam's return dict

- **Place** — `POST /order/placeorder {accountSpec, accountId, action, symbol, orderQty,
  orderType, price?, stopPrice?, isAutomated:true, customTag50}`.
- **Acceptance is NOT the synchronous 200.** A rejection is *also* a 200 (check
  `failureReason`), and a risk rejection can arrive **asynchronously on the WebSocket**
  after a clean 200 + orderId. So `execute` reports `filled`/`working` only after the WS
  event confirms it — mirroring web mode's "verify before you claim it".
- **Verify + monitor from the User WebSocket** (`user/syncrequest` snapshot + `props`
  events) — position, fill, order, cashBalance — feeding `read_state` /
  `read_accounts_summary` from an in-memory cache instead of scraping.
- **Per-order cancel becomes real** — `POST /order/cancelorder {orderId}` using the stored
  `order_ref`. This closes web mode's biggest gap (§12).
- **Rejection reasons become visible** — `failureReason`/`failureText` are returned, so a
  margin rejection reads as exactly that instead of "sent, nothing landed".

### 10d. What API mode removes, and the new failure modes it adds

**Removes:** the browser, DOM reads, login persistence, account switching, off-screen-row
bugs, the whole maintenance subsystem, VM-per-account, multi-lot qty flake.

**Adds (must be designed for — see the `copier-api-edition` build notes):** credential/token
compromise; the OAuth partner-approval + callback-endpoint requirement; `p-captcha` auth
lockout (terminal, ~1h); the 2-concurrent-session and 1-connection quotas (eviction reads as
408/429/500, not 401); token expiry mid-action; **silent partial fills** (no
`PartiallyFilled` status — derive it); socket resync with no gap detection (full-snapshot
rebuild only); and the rule that an ambiguous send (408/429/500 or a dropped socket) is
**never retried** — reconcile or escalate.

---

## 11. Clean segregation & scalability (how the code is laid out)

The structure that keeps the two modes from leaking into each other and lets the system
scale.

**Landed now (the scaffolding — web unchanged, api a loud stub):**
```
app/
  gateways/
    __init__.py        # build_gateway(settings) -> the ONE factory (knows both modes)
    base.py            # GatewayProtocol: the 11-method seam as a typed contract
    api_gateway.py     # TradovateGateway STUB: implements the seam; execute() RAISES
  terminal.py          # web mode (Selenium) - UNMOVED, byte-for-byte unchanged
  main.py              # calls build_gateway(settings) instead of constructing directly
  ...                  # poller/processor/executor/sender_client/models - SHARED, untouched
```
Proven: a process that builds `api` mode imports neither `selenium` nor `app.terminal`; a
process that builds `web` mode never imports `api_gateway`. (`tests/test_gateway_factory.py`.)

**Target when API mode is implemented (packageize the two sides):**
```
app/gateways/
  base.py
  web/               # app/terminal.py + trading/ move here
  api/               # to be implemented:
    gateway.py       #   TradovateGateway (implements the seam)
    auth.py          #   TokenBroker: credentials OR oauth; cache + renew/refresh
    oauth.py         #   authorization-code flow + callback handling
    rest.py          #   REST client (place/cancel/modify/read); 429/423/p-ticket rules
    ws.py            #   User WebSocket: framing, heartbeat, syncrequest, props cache
    state.py         #   EntityCache feeding read_state / read_accounts_summary
    symbols.py       #   symbol -> contractId resolution + front-month roll
```

**Segregation rules (enforced, not aspirational):**
1. **Nothing above the seam imports a mode.** Poller, processor, executor, sender_client,
   models never import `gateways.web.*` or `gateways.api.*` — only the seam's return dict.
2. **Modes never import each other.** `web` and `api` share only `gateways/base.py`.
3. **Heavy imports are lazy.** Selenium imports stay inside `web` (already guarded);
   `httpx`/websocket imports stay inside `api`. Importing the factory pulls in neither
   transport's heavy deps until the selected gateway is constructed.
4. **One config surface, mode-scoped.** `COPIER_MODE` picks the mode; `WEB_*` knobs
   (browser window, recycle, refresh) apply only to web; `TRADOVATE_*` knobs (auth mode,
   client id/secret, base host) apply only to api. A missing web knob never affects api and
   vice versa.

**Scalability path (unchanged seam at every step):**
- **Many users, each per-VM** — the deployment unit stays one copier per VM per user (as
  today). Adding users = adding VMs; each is independent — its own local token (API) or
  browser login (web), its own `COPIER_KEY`. Tradovate's session/connection limits are
  *per user*, so users never contend with each other. This is the model the individual
  OAuth flow serves.
- **More accounts on ONE user's login** — within a single VM, API mode can address several
  `accountId`s from that user's one token (web mode needs the account selector). The
  processor's per-account fan-out already produces the actions; only the gateway changes.
- **More brokers** — a new broker is a new `gateways/<broker>/` package + one factory
  branch. Matching, queue, safety chain, and Sender contract are reused verbatim.
- **More throughput** — API mode can later run the executor's action pump concurrently per
  account (the queue and reports already key on account); web mode stays serial. This is a
  gateway-capability flag, not a rewrite.

---

## 12. Known limitations, by mode

**Web (current):** no per-order cancel (loud "cancel manually"); rejection reason invisible;
one copier per `COPIER_KEY`; multi-lot qty preset can flake; needs a real logged-in browser
+ fixed resolution + maintenance subsystem; read-state is active-account-only (VM per
account for full monitoring).

**API (planned):** per-order cancel and visible rejections are *fixed* here; but adds token
lifecycle, rate-limit/`p-captcha` handling, session/connection quotas, silent partial fills,
socket resync, and — for OAuth — a partner-approval gate and a local (per-VM) callback route. The
ambiguous-send bucket (unknown outcome) must be first-class: never retried, reconciled or
escalated.

---

## 13. Operations quick reference

- **Select the mode:** `COPIER_MODE=web` (default) or `COPIER_MODE=api` in `.env`.
- **Run:** `python -m app.main`. Web mode also needs Edge open + logged into Tradovate on
  CDP port 9250. `.env` holds `COPIER_KEY` and mode-specific knobs; **never secrets in code**.
- **Dashboard:** `http://127.0.0.1:8100/` — overview, activity, actions, operator buttons.
- **Logs:** `data\logs\copier.log` (rotating) + `data\logs\activity.jsonl` + `/api/activity`.
- **Shared knobs:** `EXECUTOR_ENABLED`, `POLL_SEC`, `STATE_POLL_SEC`, `ACCOUNTS_PNL_SEC`,
  `ACTION_TIMEOUT_SEC`, `MAX_ACTION_AGE_SEC`, `TELEGRAM_*`.
- **Web knobs:** `MARKET_FAST_PATH`, `NET_VERIFY_SEC`, `BROWSER_WINDOW_SIZE`, recycle/refresh.
- **API knobs (planned):** `TRADOVATE_AUTH` (oauth|credentials), `TRADOVATE_BASE`,
  `TRADOVATE_CLIENT_ID`/`_SECRET`/`_REDIRECT_URI` (oauth) or
  `TRADOVATE_NAME`/`_PASSWORD`/`_CID`/`_SEC` (credentials).
- **Testing:** the **ATS** harness drives the Sender and reads the copier's `/api/activity`;
  it is mode-agnostic (it tests behaviour through the seam), so the same ATS validates both
  modes. Unit tests: `pytest` (`122` green at `0a7235c`), with a `FakeGateway` proving the
  seam without any transport installed.

---

## 14. Changelog

| Date | Commit | What changed in the code | Sections |
|---|---|---|---|
| 2026-08-26 | `0a7235c` | Baseline: documented the current Selenium edition | all (initial) |
| 2026-08-31 | `d74f6c3`+ | UI mode selector: Web/API buttons on /oauth/tradovate store `copier_mode_override` in the local DB; `resolve_mode` (override>env) applied at BOOT (restart-applied, never mid-run); status.json exposes active vs stored + restart_required; POST /oauth/tradovate/mode validates. Tests: test_mode_select.py (4) + live-boot proof. | 2,13,14 |
| 2026-08-26 | (uncommitted) | DIRECT-CREDENTIAL auth BUILT (`TRADOVATE_AUTH=credentials`): headless per-VM `/auth/accesstokenrequest` (fields match the tested tradovate_place_order.sh) + `/auth/renewaccesstoken`; mint-at-boot, renew-before-expiry, re-mint fallback, p-captcha/p-ticket guard. Gateway `_current_access_token` branches on auth mode; oauth mode still needs the flow, credentials mode auto-connects. `credentials.py` + config name/password/cid/sec (SecretStr). Verified: my contract/order calls MATCH the tested script (mine handles Stop via stopPrice). `test_credentials.py` (5). 152 green. This is the path for per-VM individual users (own login + cid/sec, no OAuth). | 10 |
| 2026-08-26 | (uncommitted) | Verified every Tradovate URL vs official docs (partner.tradovate.com mirror): all 8 paths correct. Fixes: cancel_order now sends isAutomated=true; ordStatus 'Completed' treated as fill; status-page method-name bug (.accounts->.account_list). placeorder returns orderId directly (accept test correct); orderType/timeInForce/action enums match. | 10 |
| 2026-08-26 | (uncommitted) | API-mode ORDER EXECUTION BUILT: `TradovateGateway.execute()` mirrors web automation over REST (market/limit/stop/stop-limit/exit/flatten; bid/ask fail-loud without a price); verify-before-claim via net/order polling; visible rejections; ambiguous-send never retried; latency `[live/verified]` note. `client.py` full REST surface + `AmbiguousSend`. `_action_dict` now carries account_ref (additive). Fake-REST tests (`test_api_execute.py`, 10). Web path unaffected; 147 green. | 10 |
| 2026-08-26 | (uncommitted) | API-mode OAuth connection layer BUILT: `app/gateways/api/` (oauth, token_store, client, gateway) + `app/oauth_routes.py` local pages (/oauth/tradovate), dynamic loopback redirect, SecretStr, local token store + refresh; per-VM local model (corrected from Sender-hosted); execute() still raises (next). Web path unaffected; 137 tests green (+15). | 10,11,13,14 |
| 2026-08-26 | (uncommitted) | Restructured around Web/API modes + segregation guarantee + OAuth flow; landed the mode factory scaffolding — `app/gateways/{__init__,base,api_gateway}.py`, `COPIER_MODE`/`TRADOVATE_AUTH` config, `main.py` uses `build_gateway`; web path byte-for-byte unchanged; API is a loud stub (`execute()` raises). 129 tests green (+7 factory). | 0,2,8,10,11,14 |
