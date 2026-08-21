# Automated Test Suite (ATS)

Long-running end-to-end validation of the **Signal Provider → Copier** flow.

The ATS **only** generates signals through the Signal Provider API and **observes** what
happens downstream (copier machine logs + diagnostic status). It never instructs the
copier or the trading terminal to execute anything, and never inspects the terminal.

## What it does

Until the requested duration elapses, each cycle:
1. picks a random symbol and a random order type (`market` buy/sell, `bid`, `ask`),
2. sends it with **qty = 1** via `POST /api/ats/signals`, recording the request + API result,
3. waits a random **60–180 s**, then sends the matching **exit** (`order_kind=exit`),
4. observes the copier(s) — correlating copier actions to the signal by `signal_id`, plus
   the delivery view from `/api/ats/status` and the copier's executor health,
5. records the full chain, then waits a random **300–900 s** before the next cycle.

At the end it writes a JSON report (signals generated, API interactions, delivery,
observed copier actions, and the accumulated copier event audit) for verdict analysis.
The report is rewritten atomically after every cycle, so a crash never loses data.

Order types are limited to the **price-free** set on purpose: `market`/`bid`/`ask` carry
no price, so the ATS needs no market-data source and stays within its no-terminal-access
boundary, and every entry is closed by an exit to keep the balance flat. (`limit`/`stop`/
`stop_limit` need prices; add them later via a custom entries set once a price source exists.)

## Prerequisites

- An **ATS API key**, issued in the Signal Dashboard's **Config** tab (shown once).
- The Signal Provider URL, the **group** to target (must contain an active account owned
  by the copier you're observing), and the copier's local URL (e.g. `http://<vm>:8100`).

## Run

```bash
export ATS_KEY=<key from the Config tab>
python -m ats \
  --duration 2h \
  --sender https://YOUR-SENDER.azurewebsites.net \
  --group Medium \
  --copier http://127.0.0.1:8100
```

Symbols default to whatever `/api/ats/config` reports; override with `--symbols ES,NQ,GC`.
Use `--seed` for a reproducible run. The report lands at `ats-report-<timestamp>.json`
(override with `--report`). Important activity prints to the console as it runs.
