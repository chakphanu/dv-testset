# SPDX-License-Identifier: MIT
"""Pure helpers of cases/test_prod_cross.py: the production cross-mode level check through module E of a LIVE reflector
(docs/prod-checks.md, "Production cross-mode level check").

No host, address, callsign or secret is built in: they come from environment variables (config_from_env). Nothing here opens a
socket. The pieces:

  config_from_env(environ)   the validated run configuration; ConfigMissing (skip) or ConfigRefused (fail, e.g. a module but E)
  source_signals(root)       [(name, pcm)]: the 700 Hz tone and one speech clip at an active level of -20 dBFS, 3 s each
  make_plan / format_plan    the plan printed by DVT_PROD_CROSS_DRY=1 (packets of the links, signals, timing)
  gap_wait(...)              seconds to sleep so that two overs are at least MIN_GAP_S apart
  split_streams / live_and_echo
                             the listener hears the LIVE cross-mode copy of an over first and, because module E is an echo module,
                             the ECHO copy about a second after the end: judge the first stream only, count the rest
  measure_row                one JSON row (record_row of crossaudio, so dvtest.tools.levels_table reads the file too)
  compare_row / report / main
                             the rows against the unity rows of a rig run (transcoder-levels.jsonl of the same tag): PASS / FAIL per
                             row with the difference in dB, a compact table, FINDING lines
"""
import json
import os
import re
import sys
from collections import namedtuple
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:               # also when run as a script: python3 cases/prod_cross_support.py
    sys.path.insert(0, str(ROOT))

from dvtest import crossaudio, echo  # noqa: E402
from dvtest.audio import score, signals  # noqa: E402
from dvtest.proto import ambed, dextra, m17  # noqa: E402

# ---------------------------------------------------------------- what the run is allowed to do (hard rails)

MODULE = "E"                    # the built-in echo module; there is no switch for another one
PORTS = {"dstar": dextra.PORT, "m17": m17.PORT}         # DExtra 30001, M17 17000
KINDS = ("m17", "dstar")
PAIRS = (("m17", "dstar"), ("dstar", "m17"))            # talker, listener; one pair after the other
CODEC = {"m17": ambed.CODEC2_3200, "dstar": ambed.AMBEPLUS}
OVER_S = 3.0                    # length of every over
MAX_OVER_S = 3.2                # hard cap on an over
MIN_GAP_S = 6.0                 # at least this long between the end of one over and the start of the next
WATCH_S = 5.5                   # the listener is read this long after the end of an over (live copy, then the echo copy)
GLOBAL_TIMEOUT_S = 240.0        # no over starts after this many seconds; the clients are unlinked in finally blocks
LIVE_GRACE_S = 0.4              # the live copy starts before the talker ends (+ this); the echo starts 0.8 s or more after the end
GAP = 0.4                       # echo.split_overs gap

FREQ = 700                      # tone frequency (Hz), amp 8000
TONE_AMP = 8000
SPEECH_DBFS = -20               # active level of the speech clip
CLIP = "thai_fixed"
# the same stand-in as cases/test_transcoder_levels.py when the Thai clip is not in .cache/speech-levels/
ARCTIC_FALLBACK = {"thai_fixed": ("bdl", "arctic_a0001")}
RATE = 8000
BYTES_PER_S = RATE * 2
FRAME_BYTES = 320

# ---------------------------------------------------------------- tolerances against the rig unity row of the same tag
# Set by DV-Router (the coordinator's instruction for this check); they replace any value found in docs/transcoder-levels.md
# (that document only has the VOC1 rules for image acceptance, not for a production comparison).
TOL_SPEECH_DB = 1.0             # active speech level within +-1.0 dB of the rig unity row (compared as the change to the source)
TOL_TONE_DB = 1.0               # tone level within +-1.0 dB of the rig unity row
TOL_TONE_HZ = 20.0              # dominant frequency of the tone within +-20 Hz of the rig unity row (of 700 Hz without a reference)
MIN_ENV_CORR = 0.90             # envelope correlation of the speech with the source, at least
MAX_LOSS_FRAMES = 0             # frames heard must be at least frames sent (0 loss, like the same-mode echo)


class ConfigError(Exception):
    pass


class ConfigMissing(ConfigError):
    """A required environment variable is not set: the case is skipped with this reason."""


class ConfigRefused(ConfigError):
    """A value the run must never accept (a module but E): the case fails, it does not skip."""


class MissingClip(Exception):
    pass


Config = namedtuple("Config", "host call_dstar call_m17 module dry ref tag reflector")

ENV_HOST = "DVT_PROD_HOST"
ENV_CALL_DSTAR = "DVT_PROD_CALL_DSTAR"
ENV_CALL_M17 = "DVT_PROD_CALL_M17"
ENV_MODULE = "DVT_PROD_MODULE"
ENV_REF = "DVT_PROD_CROSS_REF"
ENV_TAG = "DVT_PROD_CROSS_TAG"
ENV_REFLECTOR = "DVT_PROD_REFLECTOR"

_DSTAR_CALL = re.compile(r"^[A-Z0-9][A-Z0-9 /]{7}$")
_M17_CALL = re.compile(r"^[A-Z0-9][A-Z0-9/.-]{0,8}$")


def require_module(module):
    """Hard rail: the check keys module E and nothing else. Returns "E"."""
    if (module or "").strip().upper() != MODULE:
        raise ConfigRefused("module %r refused: this check only keys module %s (the echo module); there is no switch for "
                            "another module" % (module, MODULE))
    return MODULE


def short_tag(tag):
    """The image tag without repository and prefix: 'repo/vocoder-ambed:64s-pcm-688d9bf7-unity' -> '688d9bf7-unity'."""
    return (tag or "").split(":")[-1].replace("64s-pcm-", "")


def config_from_env(environ=None):
    """The run configuration from the environment. DVT_PROD_CROSS must be '1' (ConfigMissing otherwise), DVT_PROD_HOST and
    both callsigns must be set (ConfigMissing listing the names); DVT_PROD_MODULE, if set, must be E (ConfigRefused)."""
    env = os.environ if environ is None else environ
    if env.get("DVT_PROD_CROSS") != "1":
        raise ConfigMissing("production cross-mode check: set DVT_PROD_CROSS=1 (and %s, %s, %s)" % (ENV_HOST, ENV_CALL_DSTAR, ENV_CALL_M17))
    missing = [n for n in (ENV_HOST, ENV_CALL_DSTAR, ENV_CALL_M17) if not env.get(n)]
    if missing:
        raise ConfigMissing("production cross-mode check: not set: %s (no default is built in)" % ", ".join(missing))
    if ENV_MODULE in env:
        require_module(env[ENV_MODULE])
    host = env[ENV_HOST].strip()
    if not host or " " in host:
        raise ConfigRefused("%s is empty or has a space" % ENV_HOST)
    call_dstar = env[ENV_CALL_DSTAR].upper()
    if not _DSTAR_CALL.match(call_dstar):
        raise ConfigRefused("%s must be exactly 8 characters of A-Z 0-9 / and space (a 'CALL T' form), got %d" % (ENV_CALL_DSTAR, len(call_dstar)))
    call_m17 = env[ENV_CALL_M17].strip().upper()
    if not _M17_CALL.match(call_m17):
        raise ConfigRefused("%s must be 1 to 9 characters of A-Z 0-9 / . -" % ENV_CALL_M17)
    reflector = (env.get(ENV_REFLECTOR) or "XLX999").strip().upper()[:7]
    return Config(host=host, call_dstar=call_dstar, call_m17=call_m17, module=require_module(MODULE),
                  dry=env.get("DVT_PROD_CROSS_DRY") == "1", ref=env.get(ENV_REF) or None,
                  tag=short_tag(env.get(ENV_TAG) or "") or "unknown", reflector=reflector)


def callsign_of(cfg, kind):
    return cfg.call_dstar if kind == "dstar" else cfg.call_m17


# ---------------------------------------------------------------- source signals

def _clip_label_pcm(root, name):
    """(label, pcm) of a clip: the Thai clip under .cache/speech-levels, else the ARCTIC stand-in (as test_transcoder_levels.py)."""
    path = Path(root) / ".cache" / "speech-levels" / (name + ".raw")
    if path.is_file():
        data = path.read_bytes()
        return name, data[:len(data) // 2 * 2]
    speaker, uid = ARCTIC_FALLBACK[name]
    for base in (Path(root) / ".cache" / "speech-levels" / "8k", Path(root) / ".cache" / "speech" / "8k"):
        path = base / speaker / (uid + ".raw")
        if path.is_file():
            data = path.read_bytes()
            return "arctic_" + uid[-4:], data[:len(data) // 2 * 2]
    raise MissingClip("no speech clip %s: copy it to .cache/speech-levels/%s.raw or run scripts/pre-fetch-speech.sh" % (name, name))


def check_over_frames(n_frames):
    """An over of n_frames 20 ms frames must not be longer than MAX_OVER_S."""
    if n_frames * echo.FRAME_S > MAX_OVER_S + 1e-9:
        raise ConfigRefused("an over of %d frames is %.2f s, the cap is %.1f s" % (n_frames, n_frames * echo.FRAME_S, MAX_OVER_S))
    return n_frames


def source_signals(root=ROOT):
    """[(name, pcm)]: the 700 Hz tone (amp 8000, 3 s) and the first 3 s of the speech clip at an active level of -20 dBFS.
    The speech name is '<clip>@-20' like the rows of test_transcoder_levels.py (a 3 s excerpt of the 6 s clip there)."""
    tone = b"".join(signals.frames(signals.tone(FREQ, int(OVER_S * 1000), amp=TONE_AMP)))
    label, raw = _clip_label_pcm(root, CLIP)
    raw = raw[:int(OVER_S * BYTES_PER_S)]
    raw = raw[:len(raw) // FRAME_BYTES * FRAME_BYTES]
    if not raw:
        raise MissingClip("speech clip %s is empty" % label)
    speech = crossaudio.scale_active(raw, SPEECH_DBFS)
    for pcm in (tone, speech):
        check_over_frames(len(pcm) // FRAME_BYTES)
    return [("tone", tone), ("%s@%d" % (label, SPEECH_DBFS), speech)]


# ---------------------------------------------------------------- plan (DVT_PROD_CROSS_DRY=1)

def pair_label(talker, listener):
    return "%s>%s" % (talker, listener)


def link_packet(kind, callsign, module):
    """The first packet of a link (hex-able bytes), built without a socket."""
    if kind == "dstar":
        return dextra.link_packet(dextra._cs8(callsign), module)
    return m17.conn_packet(callsign, module)


def estimate_seconds(n_pairs=len(PAIRS), n_signals=2):
    """About how long a run takes: per over the talk (settle included), the watch and the gap."""
    return n_pairs * n_signals * (OVER_S + 0.3 + max(WATCH_S, MIN_GAP_S)) + 10.0 * n_pairs


def make_plan(cfg, sigs):
    """The plan as data. No socket is opened and the host name is not resolved."""
    steps = []
    for talker, listener in PAIRS:
        for name, pcm in sigs:
            steps.append({"pair": pair_label(talker, listener), "signal": name, "frames": len(pcm) // FRAME_BYTES,
                          "over_s": len(pcm) / BYTES_PER_S, "in_active_dbfs": round(crossaudio.active_dbfs(pcm), 1),
                          "talker_codec": talker, "listener_codec": listener})
    links = {}
    for kind in KINDS:
        links[kind] = {"port": PORTS[kind], "callsign": callsign_of(cfg, kind),
                       "link_packet_hex": link_packet(kind, callsign_of(cfg, kind), cfg.module).hex()}
    return {"host": cfg.host, "module": cfg.module, "links": links, "steps": steps, "min_gap_s": MIN_GAP_S, "watch_s": WATCH_S,
            "global_timeout_s": GLOBAL_TIMEOUT_S, "estimate_s": round(estimate_seconds(len(PAIRS), len(sigs))),
            "reference": cfg.ref, "tag": cfg.tag, "tolerances": tolerances()}


def tolerances():
    return {"speech_db": TOL_SPEECH_DB, "tone_db": TOL_TONE_DB, "tone_hz": TOL_TONE_HZ, "env_corr_min": MIN_ENV_CORR,
            "loss_frames_max": MAX_LOSS_FRAMES}


def format_plan(plan, dry=True):
    out = ["DRY RUN (DVT_PROD_CROSS_DRY=1): nothing is sent, no socket is opened, the host name is not resolved, the local transcoder is not used"
           if dry else "REAL RUN: the plan below is sent to the live reflector now (module E only)",
           "host %s  module %s  tag %s" % (plan["host"], plan["module"], plan["tag"]),
           "order: one pair after the other, one over at a time, at least %.0f s between overs, each over at most %.1f s, "
           "listener read %.1f s after each over (live copy first, then the echo copy), global timeout %.0f s (about %d s planned)" % (
               plan["min_gap_s"], MAX_OVER_S, plan["watch_s"], plan["global_timeout_s"], plan["estimate_s"])]
    for kind in KINDS:
        link = plan["links"][kind]
        out.append("link %-5s port %d callsign %r first packet %s" % (kind, link["port"], link["callsign"], link["link_packet_hex"]))
    for i, step in enumerate(plan["steps"], 1):
        out.append("over %d: %-10s %-16s %d frames (%.1f s) source active %.1f dBFS" % (
            i, step["pair"], step["signal"], step["frames"], step["over_s"], step["in_active_dbfs"]))
    tol = plan["tolerances"]
    out.append("judged against the rig unity row%s: speech level +-%.1f dB, tone level +-%.1f dB, tone +-%.0f Hz, envelope correlation >= %.2f, "
               "frames heard >= frames sent" % (" of " + plan["reference"] if plan["reference"] else " (no DVT_PROD_CROSS_REF: absolute checks only)",
                                                tol["speech_db"], tol["tone_db"], tol["tone_hz"], tol["env_corr_min"]))
    if dry:
        out.append("To run for real: unset DVT_PROD_CROSS_DRY (the owner of the reflector must have approved the check).")
    return "\n".join(out)


# ---------------------------------------------------------------- timing

def gap_wait(last_end, now, min_gap=MIN_GAP_S):
    """Seconds still to wait so that the next over starts at least `min_gap` after the end of the last one (None: no over yet)."""
    if last_end is None:
        return 0.0
    return max(0.0, last_end + min_gap - now)


# ---------------------------------------------------------------- live copy and echo copy

def split_streams(evs, gap=GAP):
    """Streams of a time ordered event list: a new one starts after a silence of more than `gap` seconds or when the stream id
    changes (the echo copy may follow the live copy closer than `gap`). Returns echo.Over objects with voice only."""
    groups, cur, cur_sid = [], [], None
    for ev in evs:
        new = bool(cur) and (ev.t - cur[-1].t > gap or (ev.sid is not None and cur_sid is not None and ev.sid != cur_sid))
        if new:
            groups.append(cur)
            cur, cur_sid = [], None
        cur.append(ev)
        if ev.sid is not None:
            cur_sid = ev.sid
    if cur:
        groups.append(cur)
    overs = []
    for group in groups:
        overs.extend(echo.split_overs(group, gap=1e9))
    return [o for o in overs if o.n_voice > 0]


def live_and_echo(evs, t0, t_end):
    """Split what a listener heard of one over (events with arrival times, the talker keyed at t0 and sent its last packet at
    t_end, same clock) into {"frames": [...], "live": bool, "echo_overs": n, "echo_frames": n}.

    Module E replays every over to everybody on the module about a second after its end. The LIVE cross-mode copy is the first
    stream after t0 - 0.5 s that starts no later than t_end + LIVE_GRACE_S; its frames are the ones to score. Any other stream
    after t0 - 0.5 s is an echo copy: counted, never scored. When the first stream starts later than that, the live copy is
    missing (live False, no frames) and that stream is counted as an echo."""
    overs = [o for o in split_streams(evs) if o.start > t0 - 0.5]
    live = None
    if overs and overs[0].start <= t_end + LIVE_GRACE_S:
        live, overs = overs[0], overs[1:]
    return {"frames": list(live.frames) if live else [], "live": live is not None,
            "echo_overs": len(overs), "echo_frames": sum(len(o.frames) for o in overs),
            "live_start_s": round(live.start - t0, 2) if live else None}


# ---------------------------------------------------------------- measurement rows

def content(name, src_pcm, out_pcm):
    """{"ok", "dominant", "share", "env_corr"} of decoded audio against the source (the check of test_transcoder_levels.py)."""
    spec = crossaudio.tone_report(out_pcm, FREQ)
    if name == "tone":
        return {"ok": bool(spec["dominant"] and abs(spec["dominant"] - FREQ) <= 80 and spec["share"] >= 0.5),
                "dominant": spec["dominant"], "share": round(spec["share"], 2), "env_corr": None}
    corr, _lag = score.envelope_correlation(src_pcm, out_pcm)
    return {"ok": corr >= 0.5 and crossaudio.active_dbfs(out_pcm) > -70, "dominant": None, "share": None, "env_corr": round(corr, 2)}


def measure_row(tag, pair, name, src_pcm, sent, got, out_pcm, split):
    """One row for out/<run>/prod-cross.jsonl. `sent` is the number of frames sent, `got` the frames of the live copy, `out_pcm` its
    decoded audio (empty when nothing was heard), `split` the result of live_and_echo. The echo copy is only counted."""
    if out_pcm:
        summary, cont = crossaudio.level_summary(out_pcm), content(name, src_pcm, out_pcm)
    else:
        summary, cont = {"rms": 0.0, "rms_dbfs": -120.0, "active_dbfs": -120.0}, {"ok": False, "dominant": None, "share": None, "env_corr": None}
    extra = {"source": "prod", "live": split["live"], "echo_overs": split["echo_overs"], "echo_frames": split["echo_frames"]}
    return crossaudio.record_row(tag, pair, name, crossaudio.active_dbfs(src_pcm), sent, got, summary, cont, extra)


# ---------------------------------------------------------------- the comparison with the rig unity rows

def read_rows(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                row["tag"] = short_tag(row.get("tag", ""))
                rows.append(row)
    return rows


def pick_ref(ref_rows, tag):
    """The rows of the reference file for `tag`. With tag 'unknown' or empty: the only tag of the file; several tags need an explicit one."""
    tags = sorted({r["tag"] for r in ref_rows})
    if tag and tag != "unknown":
        rows = [r for r in ref_rows if r["tag"] == tag]
        if not rows:
            raise ConfigError("the reference has no rows of tag %s (it has: %s)" % (tag, ", ".join(tags) or "none"))
        return rows
    if len(tags) > 1:
        raise ConfigError("the reference has rows of several tags (%s): set %s" % (", ".join(tags), ENV_TAG))
    return list(ref_rows)


def compare_row(row, ref_rows):
    """Verdict of one measured row against the matching reference row (same pair and signal). Returns
    {"pair", "signal", "status": PASS|FAIL, "diff_db", "checks": [(name, ok, text)], "ref": row or None}.
    Without a reference row the level and the frequency-against-reference are not judged (status stays PASS or FAIL on the absolute checks)."""
    ref = next((r for r in ref_rows if r["pair"] == row["pair"] and r["signal"] == row["signal"]), None) if ref_rows is not None else None
    tone = row["signal"] == "tone"
    checks = []
    lost = max(0, row["sent"] - row["got"])
    checks.append(("frames", lost <= MAX_LOSS_FRAMES, "%d of %d frames heard" % (row["got"], row["sent"])))
    diff = None
    if ref is not None:
        diff = round(row["delta_db"] - ref["delta_db"], 1)
        tol = TOL_TONE_DB if tone else TOL_SPEECH_DB
        checks.append(("level", abs(diff) <= tol + 1e-9, "%+.1f dB against the rig (change to the source %+.1f, rig %+.1f), tolerance +-%.1f" % (
            diff, row["delta_db"], ref["delta_db"], tol)))
    if tone:
        want = ref["dominant"] if ref is not None and ref.get("dominant") else FREQ
        got = row.get("dominant")
        checks.append(("frequency", got is not None and abs(got - want) <= TOL_TONE_HZ + 1e-9,
                       "%s Hz against %s Hz, tolerance +-%.0f" % (got, want, TOL_TONE_HZ)))
    else:
        corr = row.get("env_corr")
        checks.append(("envelope", corr is not None and corr >= MIN_ENV_CORR - 1e-9, "envelope correlation %s, at least %.2f" % (corr, MIN_ENV_CORR)))
    return {"pair": row["pair"], "signal": row["signal"], "status": "PASS" if all(c[1] for c in checks) else "FAIL",
            "diff_db": diff, "checks": checks, "ref": ref, "row": row}


def compare_all(rows, ref_rows):
    return [compare_row(r, ref_rows) for r in rows]


def findings(results):
    """FINDING lines, one per failed check, with the per-row difference."""
    out = []
    for res in results:
        for name, ok, text in res["checks"]:
            if not ok:
                out.append("FINDING %s %s: %s: %s" % (res["pair"], res["signal"], name, text))
    return out


def report(results, has_ref):
    """Compact table: one line per measurement, next to the rig unity values."""
    head = "%-10s %-16s %9s %6s %8s %7s %8s %6s %5s %-4s %s" % (
        "pair", "signal", "heard/sent", "rms", "act dBFS", "delta", "rig dlt", "diff", "Hz", "env", "echo  verdict")
    lines = [head, "-" * len(head)]
    for res in results:
        r, ref = res["row"], res["ref"]
        lines.append("%-10s %-16s %9s %6d %8.1f %+7.1f %8s %6s %5s %-4s %-5s %s" % (
            r["pair"], r["signal"], "%d/%d" % (r["got"], r["sent"]), r["rms"], r["out_active_dbfs"], r["delta_db"],
            "%+.1f" % ref["delta_db"] if ref else "-", "%+.1f" % res["diff_db"] if res["diff_db"] is not None else "-",
            "%.0f" % r["dominant"] if r.get("dominant") else "-", "%.2f" % r["env_corr"] if r.get("env_corr") is not None else "-",
            "%d" % r.get("echo_overs", 0), res["status"]))
    if not has_ref:
        lines.append("(no reference rows: level not judged; set DVT_PROD_CROSS_REF to a transcoder-levels.jsonl of the unity tag)")
    lines.append("tolerances (DV-Router): speech +-%.1f dB, tone +-%.1f dB, tone +-%.0f Hz, envelope >= %.2f, loss %d frames; 'echo' = streams after the live copy (counted, not judged)" % (
        TOL_SPEECH_DB, TOL_TONE_DB, TOL_TONE_HZ, MIN_ENV_CORR, MAX_LOSS_FRAMES))
    return "\n".join(lines)


def main(argv=None):
    """python3 cases/prod_cross_support.py out/<run>/prod-cross.jsonl [--ref out/<run>/transcoder-levels.jsonl] [--tag 688d9bf7-unity]
    Prints the table and the FINDING lines; exit 1 when a row fails, 2 on unusable input."""
    args = list(sys.argv[1:] if argv is None else argv)
    ref_path = tag = None
    paths = []
    while args:
        a = args.pop(0)
        if a == "--ref" and args:
            ref_path = args.pop(0)
        elif a == "--tag" and args:
            tag = short_tag(args.pop(0))
        else:
            paths.append(a)
    if len(paths) != 1:
        print("usage: python3 cases/prod_cross_support.py PROD-CROSS.jsonl [--ref TRANSCODER-LEVELS.jsonl] [--tag TAG]", file=sys.stderr)
        return 2
    try:
        rows = read_rows(paths[0])
        ref_rows = pick_ref(read_rows(ref_path), tag or (rows[0]["tag"] if rows else "")) if ref_path else None
    except (OSError, ValueError, KeyError, ConfigError) as exc:
        print("prod_cross: %s" % exc, file=sys.stderr)
        return 2
    results = compare_all(rows, ref_rows)
    print(report(results, ref_rows is not None))
    for line in findings(results):
        print(line)
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
