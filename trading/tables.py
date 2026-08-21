"""Read Tradovate's React fixedDataTable widgets (Positions, Orders, Accounts).

Two hard problems, both handled here:

1. LOCKED-COLUMN DUPLICATION - fixedDataTable renders left-locked columns
   twice and wraps every cell. We read every cell's text + geometry in one JS
   call, group by row (y) and order by column (x), deduping cells that overlap
   at the same x.

2. VIRTUALIZATION - the grid only renders ~10 rows regardless of how many
   exist; the rest are revealed by scrolling. Wheel events do NOT scroll it
   (learned 2026-08-18); the reliable way is to DRAG the vertical scrollbar
   face via CDP. read_table() drags the face top->bottom, reads at each stop,
   and accumulates unique rows - so it returns EVERY order/position, not just
   the ~10 on screen. This is what makes verification and monitoring reliable.

Returns plain data - callers wrap it in an ActionResult.
"""

from __future__ import annotations

import time
from typing import Optional

# Read every rendered cell's text + geometry for the stack whose ACTIVE tab
# title == arguments[0].
_JS_READ_CELLS = r"""
function clean(s){return (s||'')
  .replace(/[​‌‍﻿ ]/g,' ').replace(/\s+/g,' ').trim();}
var title = arguments[0];
var stack = null;
document.querySelectorAll('.lm_stack').forEach(function(st){
  var t = st.querySelector('.lm_tab.lm_active .lm_title');
  if (t && clean(t.textContent) === title) stack = st;
});
if (!stack) return {error: 'active stack not titled ' + JSON.stringify(title)};
var content = stack.querySelector('.lm_items') || stack;

var cells = [].slice.call(
  content.querySelectorAll('.public_fixedDataTableCell_cellContent'));
if (!cells.length) cells = [].slice.call(
  content.querySelectorAll('[class*=cellContent],[role=gridcell]'));

var items = cells.map(function(c){
  var r = c.getBoundingClientRect();
  return {t: clean(c.textContent), x: Math.round(r.x), y: Math.round(r.y),
          w: Math.round(r.width)};
}).filter(function(c){ return c.w > 0 && c.y >= 0; });

var rowsMap = {};
items.forEach(function(c){
  var key = Math.round(c.y / 5) * 5;
  (rowsMap[key] = rowsMap[key] || []).push(c);
});
var ys = Object.keys(rowsMap).map(Number).sort(function(a,b){return a-b;});
var rows = ys.map(function(y){
  var cs = rowsMap[y].sort(function(a,b){return a.x - b.x;});
  var out = []; var lastX = -9999;
  cs.forEach(function(c){
    if (Math.abs(c.x - lastX) > 10) { out.push(c.t); lastX = c.x; }
  });
  return out;
});
return {rows: rows, raw_cell_count: items.length};
"""

# Locate the stack's vertical scrollbar face + track so we can drag it.
_JS_SCROLLBAR = r"""
function clean(s){return (s||'').replace(/\s+/g,' ').trim();}
var title = arguments[0];
var stack = null;
document.querySelectorAll('.lm_stack').forEach(function(st){
  var t = st.querySelector('.lm_tab.lm_active .lm_title');
  if (t && clean(t.textContent) === title) stack = st;
});
if (!stack) return null;
var face = stack.querySelector('.ScrollbarLayout_faceVertical');
if (!face) return {scrollable: false};
var track = stack.querySelector('.ScrollbarLayout_mainVertical') || face.parentElement;
var fr = face.getBoundingClientRect(), tr = track.getBoundingClientRect();
return {
  scrollable: fr.height > 0 && fr.height < tr.height - 2,
  fx: fr.x + fr.width / 2,
  faceCenterY: fr.y + fr.height / 2,
  faceH: fr.height,
  trackTop: tr.y + 2,
  trackBottom: tr.y + tr.height - 2
};
"""


def _rows_to_records(rows: list) -> tuple:
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return [], []
    headers = rows[0]
    records = []
    for row in rows[1:]:
        record = {}
        for i, value in enumerate(row):
            key = headers[i] if i < len(headers) else f"col{i}"
            record[key] = value
        records.append(record)
    return headers, records


def _record_key(record: dict) -> str:
    """Stable dedupe key across scroll positions: prefer a long numeric id,
    else the whole row."""
    import re
    joined = " ".join(str(v) for v in record.values())
    m = re.search(r"\d{8,}", joined)
    return m.group(0) if m else joined


def _cdp(driver, kind, x, y, pressed):
    p = {"type": kind, "x": float(x), "y": float(y)}
    if pressed:
        p.update({"button": "left", "buttons": 1, "clickCount": 1})
    else:
        p["buttons"] = 0
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", p)


def read_table(driver, active_title: str, scroll: bool = True) -> dict:
    """Return {'headers', 'records', 'rows'} for the stack with the given
    active tab title. When scroll=True and the grid is virtualized, drag the
    scrollbar face through the full range so EVERY row is captured.
    """
    payload = driver.execute_script(_JS_READ_CELLS, active_title)
    if not payload or payload.get("error"):
        return {"error": (payload or {}).get("error", "no data"),
                "records": [], "headers": []}

    headers, records = _rows_to_records(payload.get("rows", []))
    if not scroll:
        return {"headers": headers, "records": records,
                "rows": payload.get("rows", [])}

    sb = driver.execute_script(_JS_SCROLLBAR, active_title)
    if not sb or not sb.get("scrollable"):
        return {"headers": headers, "records": records,
                "rows": payload.get("rows", [])}

    # SAFETY: verify the element at the scrollbar coords really IS the scrollbar
    # face before we press-and-drag. If it is anything else (a tab, header, ...)
    # a drag would move/close a golden-layout widget and corrupt the layout
    # (this happened - it closed the Order Ticket). Never drag a non-scrollbar.
    on_face = driver.execute_script(
        "var e=document.elementFromPoint(arguments[0],arguments[1]);"
        "return !!(e && (e.className+'').toString().indexOf('Scrollbar')!==-1);",
        sb["fx"], sb["faceCenterY"])
    if not on_face:
        return {"headers": headers, "records": records,
                "rows": payload.get("rows", []),
                "scroll_skipped": "scrollbar face not under cursor"}

    # accumulate across scroll positions, deduped by key
    accumulated = {}
    for rec in records:
        accumulated[_record_key(rec)] = rec

    fx = sb["fx"]
    top, bottom = sb["trackTop"], sb["trackBottom"]
    steps = 5
    try:
        _cdp(driver, "mouseMoved", fx, sb["faceCenterY"], False)
        _cdp(driver, "mousePressed", fx, sb["faceCenterY"], True)
        for i in range(steps + 1):
            ny = top + (bottom - top) * i / steps
            _cdp(driver, "mouseMoved", fx, ny, True)
            time.sleep(0.3)
            p = driver.execute_script(_JS_READ_CELLS, active_title)
            if p and not p.get("error"):
                h2, recs = _rows_to_records(p.get("rows", []))
                if h2:
                    headers = h2
                for rec in recs:
                    accumulated[_record_key(rec)] = rec
    finally:
        # ALWAYS end the drag cleanly - a stuck mouse-down corrupts the layout.
        try:
            _cdp(driver, "mouseReleased", fx, ny, False)
            # drag the face back to the top so the UI looks untouched
            _cdp(driver, "mouseMoved", fx, top, False)
            _cdp(driver, "mousePressed", fx, top, True)
            _cdp(driver, "mouseReleased", fx, top, False)
        except Exception:  # noqa: BLE001
            pass
        # belt-and-braces: dispatch a document mouseup and remove any stray
        # golden-layout drag proxy so a partial drag can never linger.
        try:
            driver.execute_script(
                "document.dispatchEvent(new MouseEvent('mouseup',{bubbles:true,button:0}));"
                "document.querySelectorAll('.lm_dragProxy,[class*=dragProxy],"
                ".lm_dropTargetIndicator,[class*=dropTarget]')"
                ".forEach(function(e){e.remove();});")
        except Exception:  # noqa: BLE001
            pass

    # header rows sometimes get swept in as records; drop obvious ones
    final = [r for r in accumulated.values()
             if _record_key(r) and not _looks_like_header(r, headers)]
    return {"headers": headers, "records": final, "rows": None,
            "scrolled": True}


def _looks_like_header(record: dict, headers: list) -> bool:
    vals = [str(v).strip().lower() for v in record.values()]
    heads = [str(h).strip().lower() for h in headers]
    return bool(vals) and all(v in heads for v in vals if v)
