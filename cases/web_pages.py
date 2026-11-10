# SPDX-License-Identifier: MIT
"""The ONE module that knows how DV-Router's web pages are built, for the browser cases
(cases/test_web_browser.py, test_web_webrtc.py, test_web_recorder.py, test_web_nat.py, test_web_nat_ice1.py). Nothing else of
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
    "events": ["ID", "Time (local)", "Type", "Event", "Mode", "Edge", "Callsign", "Room", "Details"],     # WEBEV1 / WEBEV2 (2026-11-04)
    "accounts": ["Callsign", "Name", "Role", "Status", "TX", "Hide position", "Limits", "TOTP enrolled", "Actions"],   # "Hide position" since DP7 (wip-ac750c7); older images lack it
    "invites": ["Callsign", "Kind", "Role", "TX", "Expires (local)", "Actions"],
    "audit": ["Time (local)", "Operator", "Role", "Remote", "Action", "Target", "Result"],
    "ids": ["ID", "Kind", "Callsign", "Source", "Status", "Seen (local)", "Actions"],
}
# I18N1 (web.md "English and Thai"): the time columns say "(local)" and show the browser's time zone (the rig's contexts
# use timezone_id UTC, locale en-US) as Gregorian "YYYY-MM-DD HH:MM:SS", 24-hour, Latin digits, in both languages.
# Was "(UTC)" before; verified in DV-Router web/site/ops/ops.js (read only).
EVENTS_TIME_COLUMN = 1                                  # index of the Time (local) column of the events table (dvtest.browser.is_ops_time)

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
    "message_open": "message-open",                     # compact "Send message" button of a signed-in sender (room without messages, WEBMSG1)
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


def message_open(page, room):
    """The compact Send message opener of one room (a signed-in sender, room without messages): scoped to the room by its
    aria-controls (message-<R>-compose); a signed-out viewer has none."""
    return page.locator("[data-testid='%s'][aria-controls='message-%s-compose']" % (TESTIDS["message_open"], room))


def message_send(page, room):
    """The composer's submit button of one room (form message-<R>-compose)."""
    return page.locator("#message-%s-compose" % room).get_by_test_id(TESTIDS["message_send"])


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
ADMIN_SIGN_IN_HEADING = "Admin sign in"                                    # UIR1 / I18N1: English is the default language
ADMIN_SIGN_IN_LEAD = "Manage node configuration and operation."


def admin_sign_in_heading(page):
    """fallback: the admin SPA (not part of WEB2's hook list): the h1 of the sign-in card."""
    return page.get_by_role("heading", name=ADMIN_SIGN_IN_HEADING, level=1, exact=True)


def admin_sign_in_lead(page):
    return page.get_by_text(ADMIN_SIGN_IN_LEAD, exact=True)


def language_switch(page):
    """fallback: the EN | TH switch (group "Language", two toggle buttons with aria-pressed; shared/i18n-ui.js)."""
    return page.get_by_role("group", name="Language")


def language_button(page, code, pressed):
    """The EN or TH button of the switch in the given pressed state."""
    return language_switch(page).get_by_role("button", name=code.upper(), exact=True, pressed=pressed)


# ---- Listen / Talk panel of the dashboard (public.js ListenPanel; shown on route #/ to a signed-in user)
LISTEN_IDLE_TEXT = "Idle"
LISTEN_INTERRUPTED_TEXT = "Connection interrupted. Waiting to resume."      # ICE1a: data-state disconnected
LISTEN_LOST_TEXT = "Connection lost. Connect again manually."
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


# ICE1 (cases/test_web_nat_ice1.py). UNVERIFIED: none of the following was confirmed from DV-Router's page sources (web/site/public.js
# was not read for ICE1); the case records what they match and judges the verified parts (data-state, the status line) first.
LISTEN_LISTENING_PREFIX = "Listening on module"         # verified: webrtc_support.Rtc.connect waits for "Listening on module <X>"
LISTEN_ERROR_WORDS = ("error", "failed", "lost", "denied", "unable", "refused", "timed out", "closed")   # UNVERIFIED: words of an error status line


def listen_alerts(page):
    """UNVERIFIED: an error banner inside the Listen card (role=alert or the dashboard's danger alert class, the one
    dashboard_alerts uses for the page). The card is known to report an error through its status line (LISTEN_LOST_TEXT), so the
    case judges the status line and data-state first and records this count."""
    return listen_panel(page).locator("[role=alert], .alert-danger")


def talk_button(page):
    return _tid(page, "talk_ptt")


def talk_controls(page):
    """fallback: the Talk block (div.talk-controls) as a whole; the button inside is talk-ptt."""
    return page.locator(".talk-controls")


TALK_STATUS_CSS = "#talk-status"                             # the Talk status line (talk_status below); the recorder's MutationObserver reads it
LISTEN_STATUS_CSS = "[data-testid='listen-connection'] p[role=status]"      # the Listen card's status line (listen_status above)


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


# ---- #/positions (DP7, MAP2; web/site/positions.js, positions-map.js, public.js): public position table and optional map
# Calibrated against the page SOURCES of the DV-Router lead's tree (not against a running image): UNVERIFIED until the first run.
# The markers are drawn by MapLibre into a WebGL canvas (a GeoJSON circle layer), NOT as DOM elements: the table below the map is
# the accessible list of the same rows and is the DOM evidence of "what is on the map"; markers are counted from the pixels.
POSITIONS_ROUTE = "/positions"
POSITIONS_TITLE = "Positions"
POSITIONS_TESTIDS = {
    "page": "positions-page",               # the section of the route
    "room": "positions-room",               # room <select> (value = room letter)
    "refresh": "positions-refresh",         # button, reloads the snapshot
    "map": "positions-map",                 # the map container (class `hidden` unless status loading/ready)
    "map_note": "positions-map-note",       # status line, present unless the map is ready
    "attribution": "positions-attribution", # OpenStreetMap link, present when the map is shown
}
POSITIONS_NOTE_MISSING = "Basemap not installed."               # status `missing`: GET /basemap/tiles.json answered {"installed": false} (or 404)
POSITIONS_NOTE_WEBGL = "The map needs WebGL2. Positions are listed below."
POSITIONS_NOTE_UNAVAILABLE = "Basemap unavailable. Positions are listed below."
POSITIONS_NOTE_LOADING = "Loading basemap..."
POSITIONS_ATTRIBUTION_TEXT = "(c) OpenStreetMap contributors"
POSITIONS_ATTRIBUTION_HREF = "https://www.openstreetmap.org/copyright"
POSITIONS_EMPTY_TEXT = "No retained positions in this room."
BASEMAP_MANIFEST = "/basemap/MANIFEST"                          # MAP1 only: MAP2 does not serve it (404)
BASEMAP_TILES_JSON = "/basemap/tiles.json"                      # MAP2c descriptor: 200 {"installed": false} without an install
POSITIONS_COLUMNS = ["Callsign / object", "Latitude", "Longitude", "Precision", "Time (local)"]
# DP6c (4ac79f26): two more columns after the time, one row per station or object (guard identity, SSID kept); the first five are unchanged
POSITIONS_COLUMNS_DP6C = POSITIONS_COLUMNS + ["Weather", "Station details"]


def positions_url():
    return dashboard_url(POSITIONS_ROUTE)


def positions_page(page):
    return page.get_by_test_id(POSITIONS_TESTIDS["page"])


def positions_room(page):
    return page.get_by_test_id(POSITIONS_TESTIDS["room"])


def positions_room_option(page, room):
    """fallback: one <option value=R> of the room select (read-only, no test id)."""
    return positions_room(page).locator("option[value='%s']" % room)


def positions_refresh(page):
    return page.get_by_test_id(POSITIONS_TESTIDS["refresh"])


def positions_map(page):
    return page.get_by_test_id(POSITIONS_TESTIDS["map"])


def positions_canvas(page):
    """fallback: the WebGL canvas MapLibre creates inside the map container."""
    return positions_map(page).locator("canvas")


def positions_map_note(page):
    return page.get_by_test_id(POSITIONS_TESTIDS["map_note"])


def positions_attribution(page):
    return page.get_by_test_id(POSITIONS_TESTIDS["attribution"])


def positions_rows(page):
    """fallback: the rows of the coordinate table of the route (read-only, no test id): one per retained position / object."""
    return positions_page(page).locator("table tbody tr")


def positions_cells(row):
    """fallback: the five cells of one table row (Callsign / object, Latitude, Longitude, Precision, Time)."""
    return row.locator("td")


def positions_empty_note(page):
    """fallback: the `No retained positions in this room.` status line."""
    return positions_page(page).get_by_text(POSITIONS_EMPTY_TEXT)


# ---- every public dashboard route (the CSP probe, cases/test_web_csp_probe.py, visits all of them)
# ROUTE_TITLES lists the six routes of the first browser stage; #/map (Map, TWIN1) and #/positions (DP7) came later.
DASHBOARD_ROUTES = ["/", "/map", "/positions", "/heard", "/modules", "/nodes", "/peers", "/about"]


# ---- #/map: the digital-twin building (TWIN1 / TWIN2b; web/site/map.js, map-layout.js, map-building.js, shared/dv.css)
# SOURCE-DERIVED, NEVER RUN: every name below was read from the page sources of the product image dv-router:wip-1bf347c
# (files under /usr/share/dv-router/web/site and shared/dv.css, read only, extracted with docker create + docker export), not
# from a running page. UNVERIFIED selector: the class names (`twin-fading`, `twin-fade-N`, `twin-room-call`, `twin-lamp`) have no
# data-testid hook; they are the page's own styling classes, so a style refactor in the product breaks them. The cases therefore
# judge the fade by COMPUTED style (opacity, fill, transition-duration) and use the classes only as a second witness.
# What the sources say (the case relies on exactly this, docs/web-browser-cases.md "TWIN2b map fade"):
#   * a room is drawn as <rect data-testid=map-room data-id=R data-state=onair|idle|down class="map-room is-onair|is-idle ...">;
#   * after an over ends and for 5 s (rooms[].last.end_ms .. +5000 on the server clock) the room keeps data-state=onair, gains the
#     classes `twin-fading twin-fade-N` (N = floor((now - end_ms) / 1000), 0..4) and the caption reads "Room R: <call> finished.";
#   * `.map-building .twin-fading` has opacity var(--twin-fade-level) (1, .75, .5, .25, 0 for N = 0..4) with `transition: opacity 1s
#     linear`; the room tile itself keeps opacity 1 and fades its FILL (color-mix of --twin-room-lit and --twin-room);
#   * prefers-reduced-motion: reduce => `.twin-fading` opacity .45 for the whole fade, `transition: none`;
#   * at rest (>= 5 s, plus the page's sampling lag of up to 1.25 s) the room is data-state=idle with none of those classes and
#     the caption says "No one is on air.".
MAP_ROUTE = "/map"
MAP_TITLE = "Map"
MAP_TESTIDS = {
    "page": "map-page",             # the route's wrapper (data-mode wide|tower)
    "building": "map-building",     # the <svg> of the building
    "room": "map-room",             # one <rect> per module; data-id = room letter, data-state onair | idle | down
    "caption": "map-caption",       # <p id=map-caption>: the text equivalent of the picture, one source for the sighted and AT
}
MAP_FADING_CSS = ".map-building .twin-fading"              # UNVERIFIED selector: every element in a fade step (room tile and art)
MAP_CALL_TEXT_CSS = ".map-building text.twin-room-call"    # UNVERIFIED selector: the talker's callsign on the room (detail rooms only)
MAP_LAMP_CSS = ".map-building .twin-lamp"                  # UNVERIFIED selector: the on-air lamp path of the room
MAP_FADE_CLASS_RE = re.compile(r"\btwin-fade-([0-4])\b")   # UNVERIFIED selector: the fade step class of a fading element
MAP_FADING_CLASS = "twin-fading"                           # UNVERIFIED selector
MAP_CAPTION_TALKING = "Room {room}: {call} talking from"   # captionText: "Room R: CALL talking from MODE for T, N listening."
MAP_CAPTION_FINISHED = "{call} finished"                   # finishedWords: t('{call} finished'); the caption line is "Room R: CALL finished."
MAP_CAPTION_NO_ONE = "No one is on air."


def map_url():
    return dashboard_url(MAP_ROUTE)


def map_page(page):
    return page.get_by_test_id(MAP_TESTIDS["page"])


def map_building(page):
    return page.get_by_test_id(MAP_TESTIDS["building"])


def map_caption(page):
    return page.get_by_test_id(MAP_TESTIDS["caption"])


def map_rooms(page):
    """All room tiles of the building."""
    return page.get_by_test_id(MAP_TESTIDS["room"])


def map_room(page, room):
    """One room tile (module letter), scoped by its data-id."""
    return page.locator("[data-testid='%s'][data-id='%s']" % (MAP_TESTIDS["room"], room))


def map_sampler_selectors(room):
    """CSS selectors the in-page sampler of cases/map_fade_support.py needs (it runs inside the page, one evaluate per sample)."""
    return {
        "room": "[data-testid='%s'][data-id='%s']" % (MAP_TESTIDS["room"], room),
        "room_any": "[data-testid='%s']" % MAP_TESTIDS["room"],
        "caption": "[data-testid='%s']" % MAP_TESTIDS["caption"],
        "fading": MAP_FADING_CSS,
        "call": MAP_CALL_TEXT_CSS,
        "building": "[data-testid='%s']" % MAP_TESTIDS["building"],
    }


# ---- #/map, TWIN1d: the Thai screenshot matrix and the polite status region (cases/test_web_map_twin1d.py)
# Calibrated against the page sources of dv-router:wip-efd9eca (web/site/map.js, map-building.js, map-layout.js,
# shared/i18n.js, shared/i18n-ui.js, shared/i18n/public-th.js, shared/dv.css; read only, extracted with docker create + cp),
# never against a running page. Confirmed in those sources: the testids map-hub-down (callout, role=status), map-desk
# (data-id = protocol, data-state up|down|tx|rx|cross|xc|rest), map-service, language-en / language-th, the storage key
# dv-lang (shared/i18n.js), the Closed sign string (key "Closed"; TH value in public-th.js), the .sr-only class (dv.css).
# NOT in wip-efd9eca: the polite status region `map-status` (TWIN1d, the product owner's spec for the next image).
MAP_STATUS_TESTID = "map-status"             # UNVERIFIED selector: from the owner's TWIN1d spec, absent from wip-efd9eca
MAP_STATUS_CLASS = "sr-only"                 # UNVERIFIED selector: the owner's spec says the region is visually hidden by this class
MAP_HUB_DOWN_TESTID = "map-hub-down"         # <div class="callout callout-warning" role="status"> above the map card
MAP_DESK_TESTID = "map-desk"
MAP_SERVICE_TESTID = "map-service"
LANGUAGE_TESTIDS = {"en": "language-en", "th": "language-th"}
LANG_STORAGE_KEY = "dv-lang"                 # localStorage value en | th, read at page start (shared/i18n.js); the switch reloads
MAP_ROOF_SIGN_CSS = ".map-building .twin-roof-sign"                 # UNVERIFIED selector: the rooftop name plate rect
MAP_CLOSED_SIGN_TEXT_CSS = ".map-building text.twin-sign-text"      # UNVERIFIED selector: the text of the closed desk sign
MAP_HEADING_SCOPE_CSS = ".content-header"                           # UNVERIFIED selector: the page heading block
# shared/i18n/public-en.js and public-th.js, key "Closed": the label the desk sign shows (written as escapes: ASCII source)
CLOSED_SIGN_TEXT = {"en": "Closed", "th": "\u0e1b\u0e34\u0e14"}


def map_status(page):
    """The polite live region of the Map (TWIN1d); count() is 0 on an image before TWIN1d."""
    return page.get_by_test_id(MAP_STATUS_TESTID)


def map_hub_down(page):
    return page.get_by_test_id(MAP_HUB_DOWN_TESTID)


def map_desks(page):
    return page.get_by_test_id(MAP_DESK_TESTID)


def language_toggle(page, code):
    """The EN or TH button of the language switch by its test id (the group's accessible name is translated, so the role
    lookup of language_button() cannot be used once the page is in Thai). Absent on images before the hook."""
    return page.get_by_test_id(LANGUAGE_TESTIDS[code])


def map_matrix_selectors():
    """CSS selectors the in-page measurement of cases/map_matrix_support.py needs (it runs inside the page)."""
    return {
        "scopes": ["main", MAP_HEADING_SCOPE_CSS],
        "building": "[data-testid='%s']" % MAP_TESTIDS["building"],
        "caption": "[data-testid='%s']" % MAP_TESTIDS["caption"],
        "page": "[data-testid='%s']" % MAP_TESTIDS["page"],
        "room_any": "[data-testid='%s']" % MAP_TESTIDS["room"],
        "desk_any": "[data-testid='%s']" % MAP_DESK_TESTID,
        "desk_down": "[data-testid='%s'][data-state='down']" % MAP_DESK_TESTID,
        "service_any": "[data-testid='%s']" % MAP_SERVICE_TESTID,
        "roof_sign": MAP_ROOF_SIGN_CSS,
        "closed_text": MAP_CLOSED_SIGN_TEXT_CSS,
        "hub_down": "[data-testid='%s']" % MAP_HUB_DOWN_TESTID,
        "status": "[data-testid='%s']" % MAP_STATUS_TESTID,
    }

