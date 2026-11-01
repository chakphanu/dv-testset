# SPDX-License-Identifier: MIT
"""The ONE module that knows how DV-Router's web pages are built, for the browser cases
(cases/test_web_browser.py, test_web_webrtc.py, test_web_recorder.py, test_web_nat.py). Nothing else of
the cases touches a CSS class, an element id or a text of the UI.

Selectors (calibrated against dv-router:wip-13e7e70): DV-Router's stable rig hooks (WEB2, web/README.md
"Stable browser rig hooks") first: `data-testid` on every interactive control, lowercase kebab-case
`<page-or-feature>-<control-or-action>`, repeated rows reuse one hook (scope to the row first), and a
read-only `data-state` of exactly idle | connecting | connected | failed | closed on the two connection
widgets `listen-connection` (the Listen card) and `dashboard-connection` (the live-update indicator).
Every testid below was taken from the page sources (web/site/public.js, messages.js, auth-page.js,
ops/ops.js), none is guessed. FALLBACK (role, label, text, id or class) only where the SUT defines no hook,
each marked "fallback:" with the reason: read-only elements (headings, status lines, tables, banners,
message bodies, room cards) carry no test id, and the recordings page (recordings/recordings.js) has none.

Pages (web/site of DV-Router, served as /; docs/design/web.md "Hostnames and routing"):

  /                dashboard (Preact app); hash routes #/ #/heard #/modules #/nodes #/peers #/about
  /login.html      sign in (callsign, password, then the authenticator code for an operator)
  /register.html   redeem an invite or reset code
  /totp.html       operator authenticator set-up
  /ops/            operator pages, hash routes #/clients #/events #/accounts #/invites #/audit #/ids
  admin host       https://dvt-admin:8443/ the admin SPA (sign-in form only here)
  /recordings/     recordings list and player (K14b), needs a recorder in the SUT (recorder.toml)

Stage 3b: the Listen / Talk panel of the dashboard route `#/` (section "Listen") and the recordings page.
"""
import re

# ---- paths
DASHBOARD = "/"
LOGIN = "/login.html"
REGISTER = "/register.html"
TOTP = "/totp.html"
OPS = "/ops/"
RECORDINGS = "/recordings/"

ROUTE_TITLES = {"/": "Dashboard", "/heard": "Last heard", "/modules": "Modules", "/nodes": "Nodes",
                "/peers": "Peers", "/about": "About"}
OPS_PAGES = {"clients": "Clients", "events": "Events", "accounts": "Accounts", "invites": "Invite codes",
             "audit": "Audit", "ids": "Radio IDs"}       # ids: UI1, on dv-router:wip-5aa7cfd and later
OPS_HEADINGS = {
    "clients": ["Callsign", "Kind", "Protocol", "Edge", "Circuit", "Room", "Address", "Connected", "Actions"],
    "events": ["ID", "Time (UTC)", "Type", "Event", "Callsign", "Room", "Edge", "Details"],
    "accounts": ["Callsign", "Name", "Role", "Status", "TX", "Limits", "TOTP enrolled", "Actions"],
    "invites": ["Callsign", "Kind", "Role", "TX", "Expires (UTC)", "Actions"],
    "audit": ["Time (UTC)", "Operator", "Role", "Remote", "Action", "Target", "Result"],
    "ids": ["ID", "Kind", "Callsign", "Source", "Status", "Seen (UTC)", "Actions"],
}

# ---- WEB2 hooks (web/README.md "Stable browser rig hooks"); the names as the page sources write them
CONNECTION_STATES = ("idle", "connecting", "connected", "failed", "closed")
TESTIDS = {
    # dashboard (public.js)
    "dashboard_connection": "dashboard-connection",     # live-update indicator, data-state
    "dashboard_home": "dashboard-home",
    "dashboard_recordings": "dashboard-recordings",     # "Recordings" link, signed in and recorder on
    "heard_callsign": "heard-callsign",                 # Last heard filter (input type=search)
    "heard_room": "heard-room",
    # Listen / Talk card (public.js ListenPanel)
    "listen_connection": "listen-connection",           # the Listen card itself, data-state
    "listen_room": "listen-room",                       # module <select>
    "listen_toggle": "listen-toggle",                   # "Listen / Connect" and "Disconnect" (same hook)
    "talk_ptt": "talk-ptt",                             # "Hold Talk" (id talk-button)
    "listen_audio": "listen-audio",                     # <audio aria-label="Module audio">
    # room message panel (messages.js)
    "message_sign_in": "message-sign-in",               # "Sign in" link of a signed-out message panel
    "message_body": "message-body",
    "message_send": "message-send",
    # account pages (auth-page.js)
    "login_callsign": "login-callsign",
    "login_password": "login-password",
    "login_totp": "login-totp",                         # only after a totp_required answer
    "login_submit": "login-submit",
    "auth_sign_out": "auth-sign-out",
    "auth_operator_pages": "auth-operator-pages",
    # operator pages (ops/ops.js)
    "ops_account": "ops-account",
    "ops_logout": "ops-logout",
    "ops_sign_in": "ops-sign-in",
}


def dashboard_nav_testid(route):
    """dashboard-nav-overview for '/', dashboard-nav-heard for '/heard' (public.js: route name appended)."""
    return "dashboard-nav-" + (route.lstrip("/") or "overview")


def ops_nav_testid(page):
    """ops-nav-clients .. ops-nav-ids (ops.js)."""
    return "ops-nav-" + page


def _tid(page, key):
    return page.get_by_test_id(TESTIDS[key])


# texts the UI shows (shared/auth.js authError, ops.js errorText): compared as written
LOGIN_FAILED_TEXT = "The callsign, password or code is incorrect."
TOTP_WRONG_TEXT = "The authenticator code is wrong or was already used."     # start of the totp_invalid text
TOTP_REQUIRED_TEXT = "Enter the current six-digit authenticator code and sign in again."
SIGNED_OUT_TEXT = "You are signed out."
OPS_DENIED_HEADING = "Operator access required"


def dashboard_url(route="/"):
    return DASHBOARD + ("#" + route if route != "/" else "")


def ops_url(page="clients"):
    return OPS + "#/" + page


# ---- dashboard
def heading(page):
    """fallback: the page's h1 (dashboard, ops and admin pages all have one; headings carry no test id)."""
    return page.get_by_role("heading", level=1)


def connection_state(page):
    """The live-update indicator; its text is 'Live', 'Connecting', 'Reconnecting', 'Polling' or 'Offline' and
    its data-state one of CONNECTION_STATES (live SSE = connected, polling fallback = failed)."""
    return _tid(page, "dashboard_connection")


def dashboard_nav(page, route):
    return page.get_by_test_id(dashboard_nav_testid(route))


def dashboard_alerts(page):
    """fallback: error banners of the dashboard (state could not be loaded, ...): read-only, no test id."""
    return page.locator("main .alert-danger")


def room_cards(page):
    """fallback: the cards of the rendered dashboard body (room cards, tiles): read-only, no test id."""
    return page.locator("main .card")


def room_messages(page, room):
    """fallback: the message panel of one module (section titled 'Messages', aria-labelledby message-<R>-title)."""
    return page.locator("section[aria-labelledby='message-%s-title']" % room)


def message_texts(page, room):
    """fallback: the message body elements of one module (span.message-text, filled with textContent)."""
    return room_messages(page, room).locator(".message-text")


def sign_in_for_history_link(page, room):
    """The 'Sign in' link of a signed-out message panel (message-sign-in, scoped to the room's panel)."""
    return room_messages(page, room).get_by_test_id(TESTIDS["message_sign_in"])


def heard_filter(page):
    return _tid(page, "heard_callsign")


def heard_cells(page):
    """fallback: the cells of the Last heard table (read-only, no test id)."""
    return page.locator("main table tbody td")


def recordings_link(page):
    return _tid(page, "dashboard_recordings")


def recording_callout(page):
    """fallback: the recording notice callout of the dashboard and the recordings page (read-only)."""
    return page.locator(".callout-warning")


# ---- account pages
def callsign_field(page):
    return _tid(page, "login_callsign")


def password_field(page):
    return _tid(page, "login_password")


def totp_field(page):
    return _tid(page, "login_totp")


def sign_in_button(page):
    return _tid(page, "login_submit")


def sign_out_button(page):
    return _tid(page, "auth_sign_out")


def auth_error(page):
    """fallback: the error banner of the account pages (div#auth-error role=alert, no test id)."""
    return page.locator("#auth-error")


def auth_notice(page):
    """fallback: the status line of the account pages (p#auth-notice, no test id)."""
    return page.locator("#auth-notice")


def signed_in_as(page):
    """fallback: the 'Signed in as <callsign>.' text (read-only)."""
    return page.get_by_text(re.compile(r"^Signed in as "))


def operator_pages_link(page):
    return _tid(page, "auth_operator_pages")


# ---- operator pages
def ops_header_nav(page):
    """fallback: the navigation landmark of the ops pages (nav aria-label 'Operator pages')."""
    return page.get_by_role("navigation", name="Operator pages")


def ops_nav_links(page):
    """The ops-nav-* links (one per page)."""
    return page.locator("[data-testid^='ops-nav-']")


def ops_nav(page, name):
    return page.get_by_test_id(ops_nav_testid(name))


def ops_identity(page):
    """fallback: the signed-in callsign in the ops header (span.ops-identity, read-only)."""
    return page.locator(".ops-identity")


def ops_table(page, caption):
    """fallback: an ops table by its caption (tables are read-only, no test id)."""
    return page.get_by_role("table", name=caption)


def ops_headings(page, caption):
    return ops_table(page, caption).locator("thead th")


def ops_rows(page, caption):
    return ops_table(page, caption).locator("tbody tr")


def ops_column(page, caption, index):
    """Locator of the cells of one column (0-based) of an ops table."""
    return ops_table(page, caption).locator("tbody tr td:nth-child(%d)" % (index + 1))


def ops_denied(page):
    """fallback: the 'Operator access required' heading."""
    return page.get_by_role("heading", name=OPS_DENIED_HEADING)


def ops_sign_in_link(page):
    return _tid(page, "ops_sign_in")


def ops_busy(page):
    """fallback: elements still loading (aria-busy)."""
    return page.locator("[aria-busy='true']")


# ---- admin SPA
def admin_sign_in_heading(page):
    """fallback: the admin SPA (not part of WEB2's hook list)."""
    return page.get_by_text("Sign in to the admin interface")


# ---- Listen / Talk panel of the dashboard (public.js ListenPanel; shown on route #/ to a signed-in user)
LISTEN_IDLE_TEXT = "Idle"
TALK_READY_TEXT = "Ready"
TALK_ON_AIR_TEXT = "On air"
TALK_RELEASED_TEXT = "Ended: Button released."          # talkStatus('ended', 'released')
TALK_HELP_START = "Listen first. Hold Talk, Space or Enter to transmit."
RECORDING_NOTICE = "Recording is enabled. Room transmissions may be retained."


def listen_panel(page):
    """The Listen card (section data-testid=listen-connection, data-state one of CONNECTION_STATES)."""
    return _tid(page, "listen_connection")


def listen_state(page):
    """data-state of the Listen card, or None when the card is not rendered."""
    loc = listen_panel(page)
    return loc.get_attribute("data-state") if loc.count() else None


def dashboard_state(page):
    """data-state of the live-update indicator, or None when it is not rendered."""
    loc = connection_state(page)
    return loc.get_attribute("data-state") if loc.count() else None


def listen_room_select(page):
    return _tid(page, "listen_room")


def listen_button(page):
    """'Listen / Connect' while idle, 'Disconnect' while a circuit is active (one hook, listen-toggle)."""
    return _tid(page, "listen_toggle")


def listen_status(page):
    """fallback (read-only status line, no test id): the first status line of the panel: 'Idle', 'Connecting to
    module X', 'Listening on module X', 'Switching to module X' or the error text."""
    return listen_panel(page).locator("p[role=status]").first


def talk_button(page):
    return _tid(page, "talk_ptt")


def talk_controls(page):
    """fallback: the Talk block (div.talk-controls) as a whole; the button inside is talk-ptt."""
    return page.locator(".talk-controls")


def talk_status(page):
    """fallback (read-only, p#talk-status): 'Ready', 'Requesting', 'Pending - queued / waiting for the floor',
    'On air', 'Denied: ...', 'Ended: ...'."""
    return page.locator("#talk-status")


def module_audio(page):
    return _tid(page, "listen_audio")


MODULE_AUDIO_CSS = "[data-testid='listen-audio']"


# ---- recordings page (web/site/recordings/recordings.js): no WEB2 hooks on this page, all fallback
RECORDING_AUDIO_CSS = "audio[aria-label=\"Recording audio\"]"
RECORDING_ROWS_CSS = "table.recording-table tbody tr"


def recordings_heading(page):
    return page.locator("#recordings-title")


def recording_notice(page):
    return recording_callout(page)


def recording_rows(page):
    return page.locator(RECORDING_ROWS_CSS)


def recording_row(page, rec_id):
    return page.locator("tr:has(button[data-play='%s'])" % rec_id)


def recording_play_button(page, rec_id):
    return page.locator("button[data-play='%s']" % rec_id)


def recording_player(page):
    return page.get_by_label("Recording audio")


def recording_player_status(page):
    return page.locator("section.recording-player p[role=status]")


def recording_alert(page):
    return page.locator("p.alert-danger")


def recording_room_filter(page):
    return page.locator("#recording-room")


def recording_day_filter(page):
    return page.locator("#recording-day")


def recording_apply(page):
    return page.get_by_role("button", name="Apply filters")
