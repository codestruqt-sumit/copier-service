"""OAuth routes on the copier's OWN local dashboard (per-VM, localhost).

Additive: a new router mounted alongside the dashboard. Touches nothing in the web
(Selenium) execution path - it only reads/writes the local token store, which only API
mode consumes. In web mode these pages still render (showing the mode), but the web
trading flow is entirely unaffected.

Routes:
  GET  /oauth/tradovate            -> the Connect / status page (the localhost:8100 page)
  GET  /oauth/tradovate/start      -> begin OAuth (redirect the browser to Tradovate)
  GET  /oauth/tradovate/callback   -> catch the code, exchange it, store the token
  GET  /oauth/tradovate/status.json-> machine-readable status (the page polls this)
  POST /oauth/tradovate/disconnect -> clear the stored token
"""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.gateways.api import oauth, token_store
from app.gateways.api.client import RestClient, TradovateAPIError
from app.gateways.api.gateway import TradovateGateway

log = logging.getLogger("copier.oauth")
router = APIRouter(prefix="/oauth/tradovate")


def _settings(request: Request):
    return request.app.state.settings


def _session_factory(request: Request):
    return request.app.state.session_factory


def _secret_val(v) -> str:
    return v.get_secret_value() if hasattr(v, "get_secret_value") else str(v or "")


def _auth_mode(settings) -> str:
    return str(getattr(settings, "tradovate_auth", "oauth") or "oauth").strip().lower()


def _configured(settings) -> bool:
    """Mode-aware: what 'configured' means depends on the auth model."""
    if _auth_mode(settings) == "credentials":
        return bool(getattr(settings, "tradovate_name", "")
                    and _secret_val(getattr(settings, "tradovate_password", ""))
                    and getattr(settings, "tradovate_cid", "")
                    and _secret_val(getattr(settings, "tradovate_sec", "")))
    return bool(settings.tradovate_client_id and _secret_val(settings.tradovate_client_secret))


@router.get("/start")
def start(request: Request):
    settings = _settings(request)
    if _auth_mode(settings) == "credentials":
        return RedirectResponse(url="/oauth/tradovate?err=credentials_mode", status_code=303)
    if not _configured(settings):
        return RedirectResponse(url="/oauth/tradovate?err=not_configured", status_code=303)
    state = secrets.token_urlsafe(24)
    db = _session_factory(request)()
    try:
        token_store.save_state(db, state)
    finally:
        db.close()
    url = oauth.authorize_url(settings, state)
    log.info("OAuth: redirecting to Tradovate authorize (redirect_uri=%s)",
             settings.oauth_redirect_uri)
    return RedirectResponse(url=url, status_code=303)


@router.get("/callback")
def callback(request: Request):
    settings = _settings(request)
    params = request.query_params
    if params.get("error"):
        return RedirectResponse(
            url=f"/oauth/tradovate?err={params.get('error')}", status_code=303)
    code = params.get("code")
    got_state = params.get("state")
    if not code:
        return RedirectResponse(url="/oauth/tradovate?err=no_code", status_code=303)

    db = _session_factory(request)()
    try:
        expected_state = token_store.pop_state(db)
        if not expected_state or got_state != expected_state:
            log.warning("OAuth callback: state mismatch (possible CSRF) - rejected")
            return RedirectResponse(url="/oauth/tradovate?err=state_mismatch", status_code=303)
        try:
            token = oauth.exchange_code(settings, code)
        except oauth.OAuthError as exc:
            log.warning("OAuth exchange failed: %s", exc)
            return RedirectResponse(url="/oauth/tradovate?err=exchange_failed", status_code=303)
        token_store.save_token(db, token)
        log.info("OAuth: token stored - copier connected to Tradovate")
    finally:
        db.close()
    return RedirectResponse(url="/oauth/tradovate?connected=1", status_code=303)


@router.post("/disconnect")
def disconnect(request: Request):
    db = _session_factory(request)()
    try:
        token_store.clear_token(db)
    finally:
        db.close()
    log.info("OAuth: token cleared - copier disconnected")
    return RedirectResponse(url="/oauth/tradovate?disconnected=1", status_code=303)


@router.get("/status.json")
def status_json(request: Request):
    settings = _settings(request)
    sf = _session_factory(request)
    out = {
        "mode": getattr(request.app.state, "active_mode", None)
                or (getattr(settings, "copier_mode", "web") or "web").strip().lower(),
        "auth": _auth_mode(settings),
        "configured": _configured(settings),
        "redirect_uri": settings.oauth_redirect_uri,
        "base": settings.tradovate_base,
        "connected": False,
        "detail": "",
        "expires_at": None,
        "accounts": [],
        "accounts_error": None,
    }
    db = sf()
    try:
        rec = token_store.load_token(db)
    finally:
        db.close()
    if rec is None:
        out["detail"] = "no token stored"
        return JSONResponse(out)
    out["expires_at"] = rec.get("expires_at")

    # Resolve a live access token (refreshing if needed) via the gateway's own logic.
    gw = TradovateGateway(settings, sf)
    ok, detail = gw.ensure_connected()
    out["connected"] = ok
    out["detail"] = detail
    if ok:
        # Prove the token works and show what it can see (the "status").
        tok, _ = gw._current_access_token()
        try:
            accts = RestClient(settings.tradovate_base, tok,
                               timeout=settings.http_timeout_sec).account_list()
            out["accounts"] = [
                {"id": a.get("id"), "name": a.get("name"),
                 "active": a.get("active"), "archived": a.get("archived")}
                for a in (accts or [])
            ][:50]
        except TradovateAPIError as exc:
            out["accounts_error"] = str(exc)
    return JSONResponse(out)


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def page(request: Request):
    settings = _settings(request)
    name = settings.copier_name
    return HTMLResponse(_PAGE.replace("__NAME__", name))


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Connect Tradovate · __NAME__</title>
<style>
  :root { color-scheme: light dark; --bg:#0f1216; --card:#171b21; --line:#2a2f37;
          --fg:#e6e9ee; --mut:#9aa4b2; --ok:#3fb950; --bad:#f85149; --acc:#58a6ff; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif; background:var(--bg);
         color:var(--fg); }
  .wrap { max-width:760px; margin:0 auto; padding:32px 20px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:var(--mut); margin:0 0 24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:20px; margin:0 0 16px; }
  .row { display:flex; justify-content:space-between; gap:12px; padding:6px 0;
         border-bottom:1px solid var(--line); }
  .row:last-child { border-bottom:0; }
  .k { color:var(--mut); } .v { text-align:right; word-break:break-all; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:13px; }
  .pill.ok { background:rgba(63,185,80,.15); color:var(--ok); }
  .pill.bad { background:rgba(248,81,73,.15); color:var(--bad); }
  button, .btn { font:inherit; border-radius:8px; border:1px solid var(--line);
          padding:10px 16px; cursor:pointer; background:var(--acc); color:#04101f;
          font-weight:600; text-decoration:none; display:inline-block; }
  .btn.ghost { background:transparent; color:var(--fg); }
  .err { background:rgba(248,81,73,.12); border:1px solid var(--bad); color:#ffb4ae;
         padding:10px 14px; border-radius:8px; margin:0 0 16px; font-size:14px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th,td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }
  th { color:var(--mut); font-weight:500; }
  code { background:#0000; color:var(--acc); }
  .muted { color:var(--mut); font-size:13px; }
</style></head>
<body><div class="wrap">
  <h1>Connect Tradovate</h1>
  <p class="sub">Copier &middot; <strong>__NAME__</strong> &middot; local API-mode connection</p>
  <div id="banner"></div>

  <div class="card">
    <div class="row"><span class="k">Execution mode</span><span class="v" id="mode">…</span></div>
    <div class="row"><span class="k">Auth model</span><span class="v" id="auth">…</span></div>

    <div class="row"><span class="k">Connection</span><span class="v" id="conn">…</span></div>
    <div class="row"><span class="k">Token expires</span><span class="v" id="exp">…</span></div>
    <div class="row"><span class="k">Redirect URI (this VM)</span><span class="v"><code id="redir">…</code></span></div>
    <div class="row"><span class="k">API host</span><span class="v"><code id="base">…</code></span></div>
  </div>

  <div style="display:flex; gap:10px; margin:0 0 20px;">
    <a class="btn" id="connectBtn" href="/oauth/tradovate/start">Connect Tradovate</a>
    <form method="post" action="/oauth/tradovate/disconnect" style="margin:0;">
      <button class="btn ghost" type="submit">Disconnect</button>
    </form>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
      <strong>Accounts visible to this connection</strong>
      <span class="muted" id="acctNote"></span>
    </div>
    <table><thead><tr><th>Account</th><th>Name</th><th>Active</th></tr></thead>
    <tbody id="accts"><tr><td colspan="3" class="muted">…</td></tr></tbody></table>
  </div>

  <p class="muted">The login happens on Tradovate's own page; your password never touches
  this copier. The token is stored locally on this VM only.</p>

<script>
const ERR = {
  credentials_mode: "This copier uses TRADOVATE_AUTH=credentials — it connects AUTOMATICALLY at startup with the .env credentials. There is no OAuth login to start; the connection state is shown below.",
  not_configured: "OAuth is not configured. Set TRADOVATE_CLIENT_ID and TRADOVATE_CLIENT_SECRET in this VM's .env, then restart.",
  no_code: "Tradovate did not return an authorization code.",
  state_mismatch: "Security check failed (state mismatch). Try connecting again.",
  exchange_failed: "Could not exchange the code for a token. Check the client id/secret and that the redirect URI is registered with Tradovate.",
};
const qs = new URLSearchParams(location.search);
function banner() {
  const b = document.getElementById('banner');
  if (qs.get('connected')) b.innerHTML = '<div class="err" style="background:rgba(63,185,80,.12);border-color:var(--ok);color:#8ef0a0">Connected to Tradovate.</div>';
  else if (qs.get('disconnected')) b.innerHTML = '<div class="err">Disconnected. Token cleared.</div>';
  else if (qs.get('err')) b.innerHTML = '<div class="err">'+(ERR[qs.get('err')]||('Error: '+qs.get('err')))+'</div>';
}
function fmt(dt){ if(!dt) return '—'; try { return new Date(dt).toLocaleString(); } catch(e){ return dt; } }
async function refresh(){
  try {
    const r = await fetch('/oauth/tradovate/status.json'); const s = await r.json();
    document.getElementById('mode').textContent = s.mode + '  (from COPIER_MODE in .env; restart to change)';
    const c = document.getElementById('conn');
    c.innerHTML = s.connected ? '<span class="pill ok">connected</span>'
                              : '<span class="pill bad">not connected</span> <span class="muted">'+(s.detail||'')+'</span>';
    document.getElementById('auth').textContent = s.auth==='credentials'
      ? 'credentials (.env) — connects automatically'
      : 'oauth — user login via Connect button';
    document.getElementById('exp').textContent = fmt(s.expires_at);
    document.getElementById('redir').textContent = s.redirect_uri;
    document.getElementById('base').textContent = s.base;
    var cb = document.getElementById('connectBtn');
    if (s.auth === 'credentials') { cb.style.display='none'; }
    else { cb.textContent = s.connected ? 'Reconnect' : 'Connect Tradovate'; }
    const tb = document.getElementById('accts'); const note = document.getElementById('acctNote');
    if (s.accounts_error){ note.textContent = s.accounts_error; }
    if (s.accounts && s.accounts.length){
      note.textContent = s.accounts.length + ' account(s)';
      tb.innerHTML = s.accounts.map(a =>
        '<tr><td><code>'+(a.id||'')+'</code></td><td>'+(a.name||'')+'</td><td>'+(a.active?'yes':'no')+'</td></tr>').join('');
    } else if (!s.accounts_error) {
      tb.innerHTML = '<tr><td colspan="3" class="muted">'+(s.connected?'none':'connect to see accounts')+'</td></tr>';
    }
  } catch(e){ /* leave last values */ }
}
banner(); refresh(); setInterval(refresh, 10000);
</script>
</div></body></html>"""
