# SPDX-License-Identifier: MIT
"""The ONE module that knows how DV-Router's web pages are built, for the browser cases
(cases/test_web_browser.py). Selectors use role, visible text, aria-label and element ids; nothing
else of the cases touches a CSS class or a text of the UI. DV-Router's WEB2 hooks (data-testid) are
not on the calibrated image (dv-router:wip-f3adb62) yet: when they land, switch the helpers below
to page.get_by_test_id() and the cases stay as they are.

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

# texts the UI shows (shared/auth.js authError, ops.js errorText): compared as written
LOGIN_FAILED_TEXT = "The callsign, password or code is incorrect."
TOTP_WRONG_TEXT = "The authenticator code is wrong or was already used."
SIGNED_OUT_TEXT = "You are signed out."
OPS_DENIED_HEADING = "Operator access required"


def dashboard_url(route="/"):
    return DASHBOARD + ("#" + route if route != "/" else "")


def ops_url(page="clients"):
    return OPS + "#/" + page


# ---- dashboard
def heading(page):
    """The page's h1 (dashboard, ops and admin pages all have one)."""
    return page.get_by_role("heading", level=1)


def connection_state(page):
    """'Live', 'Connecting', 'Polling', 'Reconnecting' or 'Offline' (the SSE state in the header)."""
    return page.locator(".conn-state")


def dashboard_alerts(page):
    """Error banners of the dashboard (state could not be loaded, ...)."""
    return page.locator("main .alert-danger")


def room_messages(page, room):
    """The message panel of one module (section titled 'Messages')."""
    return page.locator("section[aria-labelledby='message-%s-title']" % room)


def message_texts(page, room):
    """Locator of the message body elements of one module."""
    return room_messages(page, room).locator(".message-text")


def sign_in_for_history_link(page, room):
    return room_messages(page, room).get_by_role("link", name="Sign in")


def heard_filter(page):
    return page.locator("#heard-call")


# ---- account pages
def callsign_field(page):
    return page.get_by_label("Base callsign")


def password_field(page):
    return page.get_by_label("Password", exact=True)


def totp_field(page):
    return page.get_by_label("Authenticator code")


def sign_in_button(page):
    return page.get_by_role("button", name="Sign in", exact=True)


def sign_out_button(page):
    return page.get_by_role("button", name="Sign out", exact=True)


def auth_error(page):
    return page.locator("#auth-error")


def auth_notice(page):
    return page.locator("#auth-notice")


def signed_in_as(page):
    return page.get_by_text(re.compile(r"^Signed in as "))


def operator_pages_link(page):
    return page.get_by_role("link", name="Operator pages")


# ---- operator pages
def ops_header_nav(page):
    return page.get_by_role("navigation", name="Operator pages")


def ops_table(page, caption):
    return page.get_by_role("table", name=caption)


def ops_headings(page, caption):
    return ops_table(page, caption).locator("thead th")


def ops_rows(page, caption):
    return ops_table(page, caption).locator("tbody tr")


def ops_column(page, caption, index):
    """Locator of the cells of one column (0-based) of an ops table."""
    return ops_table(page, caption).locator("tbody tr td:nth-child(%d)" % (index + 1))


def ops_denied(page):
    return page.get_by_role("heading", name=OPS_DENIED_HEADING)


def ops_toolbar_total(page):
    return page.locator(".ops-toolbar p").first


# ---- admin SPA
def admin_sign_in_heading(page):
    return page.get_by_text("Sign in to the admin interface")


# ---- Listen / Talk panel of the dashboard (public.js ListenPanel; shown on route #/ to a signed-in user)
RECORDINGS = "/recordings/"
LISTEN_IDLE_TEXT = "Idle"
TALK_READY_TEXT = "Ready"
TALK_HELP_START = "Listen first. Hold Talk, Space or Enter to transmit."
RECORDING_NOTICE = "Recording is enabled. Room transmissions may be retained."


def listen_panel(page):
    return page.locator("section.listen-panel")


def listen_room_select(page):
    return page.locator("#listen-room")


def listen_button(page):
    """'Listen / Connect' while idle, 'Disconnect' while a circuit is active."""
    return listen_panel(page).locator(".listen-controls button")


def listen_status(page):
    """The first status line of the panel: 'Idle', 'Connecting to module X', 'Listening on module X',
    'Switching to module X' or the error text."""
    return listen_panel(page).locator("p[role=status]").first


def talk_button(page):
    return page.locator("#talk-button")


def talk_status(page):
    """'Ready', 'Requesting', 'Pending - queued / waiting for the floor', 'On air', 'Denied: ...', 'Ended: ...'."""
    return page.locator("#talk-status")


def module_audio(page):
    return page.get_by_label("Module audio")


# ---- recordings page (web/site/recordings/recordings.js)
def recordings_heading(page):
    return page.locator("#recordings-title")


def recording_notice(page):
    return page.locator(".callout-warning")


def recording_rows(page):
    return page.locator("table.recording-table tbody tr")


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
