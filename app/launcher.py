"""Launch and manage the Tradovate terminal browser from the dashboard.

The copier NEVER handles credentials: this only starts/points a debug browser (persistent
profile) at Tradovate; the human logs in by hand in that window, and the executor then
attaches to the tab over CDP. Reuses the validated browser binary discovery + launch flags
from browser.session. The launch/dedupe paths spawn the browser PROCESS only (no Selenium
driver) so they never fight the executor's driver; the self-test uses a short-lived,
read-only driver and the dashboard only calls it when the executor isn't already attached.

Native Windows only: a browser must run on a real desktop. Inside Docker the container
cannot launch a host GUI browser, so this reports that clearly (there you launch the
browser on the host and the container attaches via host.docker.internal).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from browser.session import (
    BROWSERS,
    CREATE_BREAKAWAY_FROM_JOB,
    CREATE_NEW_PROCESS_GROUP,
    DETACHED_PROCESS,
)
from config.settings import TRADOVATE_URL
from config.settings import Settings as BotSettings

NEEDLE = "trader.tradovate.com"


def _port_open(host: str, port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.5)
    try:
        return sock.connect_ex((host, port)) == 0
    finally:
        sock.close()


def _list_pages(host: str, port: int) -> list[dict]:
    """Every open page tab, read passively from CDP /json (no tab switching)."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/json", timeout=4) as resp:
            targets = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [t for t in targets if t.get("type") == "page"]


def _tradovate(pages: list[dict]) -> list[dict]:
    return [t for t in pages if NEEDLE in (t.get("url") or "").lower()]


def has_tradovate_tab(host: str, port: int) -> bool:
    return bool(_tradovate(_list_pages(host, port)))


def force_kill_browser() -> str:
    """Last resort: kill the debug-browser process holding the debug port (when a
    graceful CDP close didn't drop it). Windows-only; targets ONLY the PID listening on
    our debug port (not every Edge window)."""
    import subprocess
    if os.name != "nt":
        return "not a Windows desktop"
    s = BotSettings.load()
    port = s.browser.port
    try:
        out = subprocess.check_output(["netstat", "-ano", "-p", "tcp"], text=True,
                                      stderr=subprocess.DEVNULL, timeout=10)
    except Exception as exc:  # noqa: BLE001
        return f"netstat failed: {exc}"
    pids = set()
    for line in out.splitlines():
        up = line.upper()
        if f":{port} " in line and "LISTENING" in up:
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
    killed = []
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            killed.append(pid)
        except Exception:  # noqa: BLE001
            pass
    return f"killed pid(s) {killed}" if killed else "no process found on the debug port"


def terminal_status() -> dict:
    """What the dashboard shows: is the debug browser up, is Tradovate open, and are
    there anomalies (a zombie window with no tabs, or duplicate Tradovate tabs)?"""
    s = BotSettings.load()
    host, port = s.browser.host, s.browser.port
    up = _port_open(host, port)
    pages = _list_pages(host, port) if up else []
    tv = _tradovate(pages)
    return {
        "browser_up": up,
        "tradovate_tab": bool(tv),
        "tradovate_tabs": len(tv),
        "tab_count": len(pages),
        "zombie": up and len(pages) == 0,   # port open but no tabs = window closed, process alive
        "host": host,
        "port": port,
        "can_launch": os.name == "nt",
    }


def _binary(spec: dict) -> str | None:
    return next((p for p in spec["paths"] if Path(p).is_file()), None)


def _spawn(binary: str, args: list[str]) -> None:
    cmd = [binary, *args]
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB
    try:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        subprocess.Popen(cmd, creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                         close_fds=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _close_tab(host: str, port: int, target_id: str) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{host}:{port}/json/close/{target_id}", timeout=4
        ) as resp:
            resp.read()
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_single_tradovate_tab(host: str, port: int, binary: str, profile: Path) -> dict:
    """Guarantee EXACTLY ONE Tradovate tab.

    - none present (fresh window, navigated away, or a zombie window with zero tabs):
      open one. A second invocation with the same profile forwards a new tab/window to
      the already-running instance and exits (it doesn't start a second browser), which
      also revives a zombie window.
    - more than one (Edge restored old tabs): close the extras. All Tradovate tabs share
      the same profile login session, so which one we keep doesn't matter.

    Returns {"opened", "closed", "tabs"}.
    """
    tv = _tradovate(_list_pages(host, port))
    opened = closed = 0
    if not tv:
        _spawn(binary, [f"--user-data-dir={profile}", TRADOVATE_URL])
        for _ in range(30):
            time.sleep(0.5)
            tv = _tradovate(_list_pages(host, port))
            if tv:
                opened = 1
                break
    elif len(tv) > 1:
        for extra in tv[1:]:
            if _close_tab(host, port, extra.get("id", "")):
                closed += 1
        tv = _tradovate(_list_pages(host, port))
    return {"opened": opened, "closed": closed, "tabs": len(tv)}


def launch_terminal() -> tuple[bool, str]:
    """Make the terminal ready: a debug browser is running with exactly one Tradovate
    tab for you to log into. Idempotent, and self-healing against Edge restoring old
    tabs or leaving a zombie window behind.
    """
    s = BotSettings.load()
    s.ensure_dirs()
    host, port = s.browser.host, s.browser.port

    if os.name != "nt" and not _port_open(host, port):
        return False, ("Can't launch a browser from here (not a Windows desktop - likely "
                       "a container). Start the browser on the host; the copier will attach.")

    spec = BROWSERS.get(s.browser.browser)
    if spec is None:
        return False, f"unknown browser {s.browser.browser!r}"
    binary = _binary(spec)
    profile = Path(s.browser.profile_dir)
    profile.mkdir(parents=True, exist_ok=True)

    launched = False
    if not _port_open(host, port):
        if binary is None:
            return False, f"{spec['label']} not found on this machine"
        args = [
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-session-crashed-bubble",
            "--restore-last-session=false",
        ]
        # Optional fixed window size (BROWSER_WINDOW_SIZE="1600,900"): pins the layout so
        # a varying RDP resolution can't change how many table rows render (the copier's
        # reads are visible-rows-only). Off by default.
        try:
            from app.config import settings as app_settings
            size = (app_settings.browser_window_size or "").replace("x", ",").strip()
            if size:
                w, h = (int(p.strip()) for p in size.split(","))
                args.append(f"--window-size={w},{h}")
        except Exception:  # noqa: BLE001 - a bad value must never block the launch
            pass
        _spawn(binary, args + [TRADOVATE_URL])
        opened_port = False
        for _ in range(40):
            if _port_open(host, port):
                opened_port = True
                break
            time.sleep(0.5)
        if not opened_port:
            return False, "browser did not open the debug port within 20s"
        launched = True
        time.sleep(1.0)  # let any restored tabs settle before we dedupe

    # Port is up. Guarantee exactly one Tradovate tab (handles restored duplicates,
    # a navigated-away window, or a zombie window with no tabs).
    tab = {"opened": 0, "closed": 0, "tabs": 1}
    if binary is not None:
        tab = ensure_single_tradovate_tab(host, port, binary, profile)

    bits = ["browser launched" if launched else "browser already running"]
    if tab["opened"]:
        bits.append("opened the Tradovate tab")
    if tab["closed"]:
        bits.append(f"closed {tab['closed']} duplicate Tradovate tab(s)")

    if tab["tabs"] == 0:
        return False, "; ".join(bits) + " - could NOT open a Tradovate tab (check the browser)."
    if launched or tab["opened"]:
        return True, "; ".join(bits) + " - now log in to Tradovate."
    return True, "; ".join(bits) + f" - {tab['tabs']} Tradovate tab ready; log in if you haven't."


# A visible, non-destructive banner the self-test flashes so you can SEE the script
# touch the tab. It auto-removes after a few seconds and touches nothing else.
_SELFTEST_JS = r"""
var id='__copier_selftest__';
var old=document.getElementById(id); if(old && old.remove) old.remove();
var d=document.createElement('div'); d.id=id; d.textContent=arguments[0];
d.style.cssText='position:fixed;z-index:2147483647;top:18px;left:50%;'
 +'transform:translateX(-50%);background:#12331f;color:#3ecf8e;border:2px solid #3ecf8e;'
 +'border-radius:10px;padding:12px 22px;font:600 15px system-ui,Segoe UI,sans-serif;'
 +'box-shadow:0 8px 30px rgba(0,0,0,.55);pointer-events:none';
document.body.appendChild(d);
setTimeout(function(){var e=document.getElementById(id);
 if(e){e.style.transition='opacity .6s';e.style.opacity='0';
 setTimeout(function(){if(e&&e.remove)e.remove();},700);}},4500);
return true;
"""


def selftest() -> dict:
    """Prove the Python script controls the RIGHT tab: attach read-only, read what the
    tab is, and flash a visible banner in it so you can SEE the script touch it.

    Non-destructive - it reads url/title and injects a transient banner that removes
    itself; it never navigates or trades. Creates its own short-lived driver, so the
    dashboard only calls it when the executor is NOT already attached (no two drivers).
    """
    from browser.session import BrowserLaunchError, BrowserSession

    s = BotSettings.load()
    sess = BrowserSession(s.browser)
    try:
        sess.start(attach_only=True)
    except BrowserLaunchError as exc:
        return {"ok": False, "detail": str(exc).splitlines()[0]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"attach failed: {type(exc).__name__}: {exc}"}

    try:
        if not sess.focus_tab(NEEDLE):
            return {"ok": False, "detail": "attached, but found no Tradovate tab to access"}
        driver = sess.driver
        url = driver.current_url
        title = driver.title
        stamp = time.strftime("%H:%M:%S")
        driver.execute_script(_SELFTEST_JS, f"✅ Copier is here - tab verified at {stamp}")
        tv = _tradovate(sess.list_tabs())
        return {
            "ok": True,
            "url": url,
            "title": title,
            "tradovate_tabs": len(tv),
            "detail": f"Flashed a green banner in the Tradovate tab at {stamp} - "
                      "look at the browser to confirm it's the right tab.",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        sess.stop()  # detach only - keep_open leaves your logged-in browser alone
