"""Every CSS/XPath selector for trader.tradovate.com lives here.

WHY THIS FILE EXISTS SEPARATELY
-------------------------------
Tradovate's web terminal is a React app with generated class names. When they
ship a UI change, selectors break - and when that happens you edit THIS file
only, never the trading logic.

IMPORTANT: the selectors below are STARTING GUESSES, not verified against the
live DOM. Run `python -m tools.discover_selectors` while logged in to capture
the real ones, then paste them in. Each entry is a LIST - candidates are tried
in order, so you can keep a fallback alongside a fresh selector.

Selector syntax: prefix with "xpath=" for XPath, otherwise treated as CSS.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Session / auth state
# --------------------------------------------------------------------------
LOGGED_IN_MARKERS = [
    # presence of ANY of these means we have an authenticated session
    "xpath=//*[contains(@class,'app-container')]",
    "xpath=//*[contains(text(),'Account')]",
    ".trading-view",
]

LOGGED_OUT_MARKERS = [
    # presence of ANY of these means we are on the login screen
    #
    # [x] CONFIRMED 2026-08-18 against trader.tradovate.com/welcome
    "input[type='password']",                                     # 1 exact match
    'xpath=//button[normalize-space(.)="Sign in with Google"]',    # confirmed
    'xpath=//button[normalize-space(.)="Sign in with Apple"]',     # confirmed
    'xpath=//p[normalize-space(.)="Log in to access Tradovate."]',  # confirmed
    #
    # [!] REMOVED 2026-08-18: xpath=//*[contains(normalize-space(.),'Login')]
    #     It produced 13 matches, including the LANGUAGE SELECTOR
    #     ("English Deutsch Espanol Francais Italiano ..."). Substring text
    #     matching is far too loose for session-state detection - a false
    #     "logged out" would pause trading for no reason. Use exact
    #     normalize-space() equality or a structural selector instead.
]

SESSION_TIMEOUT_DIALOG = [
    "xpath=//*[contains(normalize-space(.),'session has expired')]",
    "xpath=//*[contains(normalize-space(.),'timed out')]",
    "xpath=//*[contains(normalize-space(.),'Still there')]",
]

# clicked periodically to keep the session warm - must be HARMLESS
KEEPALIVE_TARGETS = [
    "xpath=//*[contains(@class,'header')]",
    "body",
]

# --------------------------------------------------------------------------
# Account panel
# [x] CONFIRMED 2026-08-18 against the logged-in terminal (read-only probe).
#     Top bar layout: [clock] [Manual Lockout] [Open Account] [Account
#     selector dropdown] [Equity / Open P/L] [margin indicators]
# --------------------------------------------------------------------------
ACCOUNT_BALANCE = [
    # Equity column of the top bar. Sample read: '50,492.50 usd' -> 50492.50.
    # First .balance-column is Equity, second is Open P/L - keep :first-child.
    ".account-info-inline .balance-row .balance-column:first-child .currency-wrap",
    # fallback if the inline wrapper class changes:
    "xpath=//div[contains(@class,'balance-column')][.//div[normalize-space(.)='Equity']]//div[contains(@class,'currency-wrap')]",
]

ACCOUNT_SELECTOR = [
    # the whole dropdown pane in the top bar
    ".pane.account-selector",
]

# The clickable header of the account dropdown (shows the ACTIVE account id).
# NOTE: the toggle is a DIV.account; menu entries are A.account - do not mix.
ACCOUNT_SELECTOR_TOGGLE = [
    ".pane.account-selector div.account",
    ".pane.account-selector",
]

# Active account id text inside the toggle, e.g. 'TDFYSL00000000000'
ACCOUNT_ACTIVE_NAME = [
    ".pane.account-selector div.account .name",
    ".pane.account-selector div.account",
]

# Menu entries = switchable accounts. CRITICAL: the same menu also contains
# 'Manage Groups', 'Go to Replay' and 'Logout' as a.account.logout - the
# :not(.logout) filter is what keeps a stray click from LOGGING US OUT.
ACCOUNT_MENU_ITEMS = [
    ".pane.account-selector a.account:not(.logout)",
]

# The li wrapping the currently-selected account carries class 'selected'
ACCOUNT_MENU_SELECTED_LI = [
    ".pane.account-selector li.selected",
]

# --------------------------------------------------------------------------
# NEVER-CLICK DENYLIST
# [x] CONFIRMED present 2026-08-18. Controls that sit next to legitimate
# targets and would end the session or lock the account if clicked. Every
# click helper that works inside the account dropdown MUST check its target
# text against these.
# --------------------------------------------------------------------------
FORBIDDEN_CLICK_TEXTS = [
    "Logout",
    "Manage Groups",
    "Go to Replay",
    "Manual Lockout",
    "Open Account",
]

# A guaranteed-inert click target, used to close dropdowns via outside-click.
# [x] CONFIRMED 2026-08-18: the top-bar clock. Clicking a clock does nothing.
# LEARNING: the account dropdown ignores BOTH Escape and a second toggle
# click - outside-click is the only gesture that closes it.
NEUTRAL_CLICK_TARGET = [
    ".notification-ticker-wrapper",
]

# --------------------------------------------------------------------------
# Symbol switching
# --------------------------------------------------------------------------
SYMBOL_SEARCH_INPUT = [
    "input[placeholder*='Symbol' i]",
    "input[placeholder*='Search' i]",
    "xpath=//input[contains(@class,'symbol')]",
]

SYMBOL_SEARCH_RESULT = [
    # formatted with the symbol at runtime via .format(symbol=...)
    "xpath=//*[contains(@class,'search-result')][contains(normalize-space(.),'{symbol}')]",
    "xpath=//li[contains(normalize-space(.),'{symbol}')]",
]

ACTIVE_SYMBOL_LABEL = [
    "xpath=//*[contains(@class,'symbol-title')]",
    "xpath=//*[contains(@class,'contract-name')]",
]

# --------------------------------------------------------------------------
# Order entry
# --------------------------------------------------------------------------
QTY_INPUT = [
    "input[name='orderQty']",
    "xpath=//input[contains(@class,'qty')]",
    "xpath=//*[contains(normalize-space(.),'Qty')]/following::input[1]",
]

BUY_BUTTON = [
    "xpath=//button[contains(normalize-space(.),'Buy')]",
    "xpath=//*[contains(@class,'buy-button')]",
]

SELL_BUTTON = [
    "xpath=//button[contains(normalize-space(.),'Sell')]",
    "xpath=//*[contains(@class,'sell-button')]",
]

ORDER_CONFIRM_BUTTON = [
    "xpath=//button[normalize-space(.)='Confirm']",
    "xpath=//button[normalize-space(.)='Place Order']",
    "xpath=//button[normalize-space(.)='OK']",
]

ORDER_REJECT_TOAST = [
    "xpath=//*[contains(@class,'toast')][contains(normalize-space(.),'eject')]",
    "xpath=//*[contains(@class,'error')][contains(normalize-space(.),'rder')]",
]

# --------------------------------------------------------------------------
# Positions
# --------------------------------------------------------------------------
POSITIONS_TAB = [
    "xpath=//*[normalize-space(.)='Positions']",
]

POSITION_ROW = [
    # formatted with the symbol at runtime
    "xpath=//tr[contains(normalize-space(.),'{symbol}')]",
    "xpath=//*[contains(@class,'position-row')][contains(normalize-space(.),'{symbol}')]",
]

CLOSE_POSITION_BUTTON = [
    # searched WITHIN a position row element
    "xpath=.//button[contains(normalize-space(.),'Close')]",
    "xpath=.//*[contains(@class,'close-position')]",
]

CLOSE_CONFIRM_BUTTON = [
    "xpath=//button[normalize-space(.)='Confirm']",
    "xpath=//button[normalize-space(.)='Yes']",
    "xpath=//button[normalize-space(.)='Close Position']",
]


# ==========================================================================
# CHART TRADE PANEL  (the buttons above the chart)
# [x] CONFIRMED 2026-08-18. Buttons are DIV.btn (not <button>), text-labelled.
# CRITICAL: 'Buy Mkt'/'Sell Mkt' text ALSO appears in the DOM-ladder stack, so
# these MUST be resolved inside the chart-panel scope, never document-wide.
# The chart trade panel is the lm_stack that uniquely contains a 'Buy Bid'
# button (the DOM ladder has no Buy Bid / Sell Ask).
# ==========================================================================
TRADE_PANEL_SCOPE = [
    "xpath=//div[contains(@class,'lm_stack')]"
    "[.//div[contains(@class,'btn')][normalize-space()='Buy Bid']]",
]
# all relative (.//) so they resolve INSIDE the scope element
TRADE_BUY_MKT = ["xpath=.//div[contains(@class,'btn')][normalize-space()='Buy Mkt']"]
TRADE_SELL_MKT = ["xpath=.//div[contains(@class,'btn')][normalize-space()='Sell Mkt']"]
TRADE_BUY_BID = ["xpath=.//div[contains(@class,'btn')][normalize-space()='Buy Bid']"]
TRADE_SELL_ASK = ["xpath=.//div[contains(@class,'btn')][normalize-space()='Sell Ask']"]
TRADE_QTY_INPUT = ["xpath=.//input[contains(@class,'form-control')]"]
# 'Exit at Mkt & Cxl' flattens the position AND cancels orders - destructive.
TRADE_EXIT_BUTTON = [
    "xpath=.//button[contains(@class,'btn-default')][contains(normalize-space(),'Exit at Mkt')]",
]
TRADE_EXIT_DROPDOWN = ["xpath=.//button[contains(@class,'dropdown-toggle')]"]

# Order confirmation popover.
# [x] CONFIRMED 2026-08-18: clicking Buy Mkt/Sell Mkt raises a '.popover-content'
# dialog titled e.g. 'Buy 1 MKT?' with a confirm button (btn-success 'Buy' /
# btn-danger 'Sell') and a 'Cancel' (btn-default). We KEEP this confirmation on
# (a real safety layer) and read its title to verify side+qty+type before
# committing. It may be suppressed by the user's 'Do not show again' - handler
# treats absence as "went straight through".
ORDER_CONFIRM_POPOVER = ["xpath=//div[contains(@class,'popover-content')]"]
# the whole dialog text, for reading the '<Side> <qty> MKT?' title
ORDER_CONFIRM_TITLE = [
    "xpath=//div[contains(@class,'popover-content')]",
]
ORDER_CONFIRM_CANCEL = [
    "xpath=//div[contains(@class,'popover-content')]//div[contains(@class,'btn')][normalize-space()='Cancel']",
    "xpath=//div[contains(@class,'popover-content')]//*[normalize-space()='Cancel']",
]
# the submit button carries the side word (Buy/Sell); formatted at call time
ORDER_CONFIRM_SUBMIT = [
    "xpath=//div[contains(@class,'popover-content')]//div[contains(@class,'btn')][normalize-space()='{side}']",
]
# the chart pane's own current-symbol header, used to re-verify before trading
TRADE_PANEL_SYMBOL = ["xpath=.//div[contains(@class,'contract-symbol')]"]

# ==========================================================================
# ORDER TICKET widget
# [x] CONFIRMED 2026-08-18. Scope = lm_stack containing the symbol search box.
# Fields resolved by their visible label then the next form-control, which is
# stable against class churn.
# ==========================================================================
TICKET_SCOPE = [
    "xpath=//div[contains(@class,'lm_stack')]"
    "[.//input[contains(@class,'search-box--input')]]",
]
TICKET_SYMBOL_INPUT = ["xpath=.//input[contains(@class,'search-box--input')]"]
TICKET_BUY = ["xpath=.//label[contains(@class,'btn')][normalize-space()='Buy']"]
TICKET_SELL = ["xpath=.//label[contains(@class,'btn')][normalize-space()='Sell']"]
TICKET_QTY_INPUT = [
    "xpath=.//label[normalize-space()='Qty']/following::input[contains(@class,'form-control')][1]",
]
# [x] CONFIRMED 2026-08-18: symbol and qty are comboboxes that DON'T commit
# typed text (input shows it, model stays empty -> 'Symbol should be specified',
# qty stays 'Select'). Symbol commits via the S-sync toggle (follows the chart);
# qty commits by picking from its dropdown.
# The 'S' symbol-sync toggle in the ticket header; class carries 'truthy-value'
# when ON.
TICKET_S_TOGGLE = [
    "xpath=.//*[contains(@class,'btn-icon')][normalize-space()='S']",
    "xpath=.//*[contains(@class,'btn')][normalize-space()='S']",
]
# the dropdown toggle to the right of the qty input
TICKET_QTY_TOGGLE = [
    "xpath=.//label[normalize-space()='Qty']/following::*[contains(@class,'select-input')][1]//*[contains(@class,'btn')]",
    "xpath=.//label[normalize-space()='Qty']/following::*[contains(@class,'select-input')][1]",
]
# a numeric option in the opened qty dropdown; formatted with {value}
TICKET_QTY_OPTION = [
    "xpath=//li[normalize-space()='{value}']",
    "xpath=//*[contains(@class,'option')][normalize-space()='{value}']",
]
TICKET_QTY_PRESETS = {1, 2, 3, 4, 5, 10, 15, 20}
TICKET_PRICE_INPUT = [
    "xpath=.//label[normalize-space()='Price']/following::input[contains(@class,'form-control')][1]",
]
# STOP LIMIT shows a SECOND price field labelled 'Stop Price' (the trigger); 'Price'
# is the limit cap. normalize-space()='Stop Price' matches only this field, not 'Price'.
TICKET_STOP_PRICE_INPUT = [
    "xpath=.//label[normalize-space()='Stop Price']/following::input[contains(@class,'form-control')][1]",
]
TICKET_ORDER_TYPE = [
    "xpath=.//label[normalize-space()='Order Type']/following::*[contains(@class,'select-input')][1]",
]
# after the order-type select-input is clicked, options render as list items;
# formatted with {value} at call time
TICKET_ORDER_TYPE_OPTION = [
    "xpath=//*[contains(@class,'dropdown') or contains(@class,'select')]"
    "//*[normalize-space()='{value}']",
    "xpath=//li[normalize-space()='{value}']",
]
TICKET_FLAG = [
    "xpath=.//label[contains(@class,'btn')][normalize-space()='{value}']",  # DAY/GTC/GTD
]
TICKET_SEND = ["xpath=.//button[normalize-space()='Send']"]
TICKET_RESET = ["xpath=.//button[normalize-space()='Reset']"]

# order-type labels as the widget spells them (probed)
ORDER_TYPES = ["MARKET", "LIMIT", "STOP", "STOP LIMIT", "TRL STOP", "TRL STP"]

# ==========================================================================
# POSITIONS / ORDERS panels (React fixedDataTable)
# [x] CONFIRMED 2026-08-18 columns. Rows are read via JS (see positions.py /
# orders_panel.py) because fixedDataTable duplicates cells for locked columns
# and is far more reliable to read programmatically than by selector.
# ==========================================================================
POSITIONS_STACK_TITLE = "Positions"
ORDERS_STACK_TITLE = "Orders"
ACCOUNTS_STACK_TITLE = "Accounts"


# --------------------------------------------------------------------------
def is_verified() -> bool:
    """Flip to True in settings.local.json once you've confirmed the selectors.

    The engine refuses to arm (place live orders) while this is False, so a
    guessed selector can never place a real trade.
    """
    return False
