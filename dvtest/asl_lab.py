# SPDX-License-Identifier: MIT
"""Client side of the AllStarLink test lab (profile asl-lab, docs/asl-lab.md).

The lab hosts (asl-reg, asl-a, asl-b, asl-e) each run a small control agent
(rig/asl-lab-agent.py) because the tester has no docker socket. This module
talks to it: allow-listed Asterisk CLI, one tcpdump per scenario, the tone
file, registration lines, and the pcap / recording files that come back into
out/<run>/asl-lab/<scenario>/.

    lab = from_env()                      # None unless DVT_ASL_LAB=on
    with lab.scenario("link-basic", capture=["a", "e"]) as sc:
        lab.put_tone()
        lab.node("a").cli("rpt fun 199901 *3299901")
        ...
    # sc.dir now holds a.pcap, e.pcap, rec/<host>/*.sln and scenario.json

Tones are generated here (1 kHz, mu-law, deterministic); their sha256 is
recorded in scenario.json. Secrets of the lab are fixtures, not credentials.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .audio import score, signals

AGENT_PORT = 8099
# fixtures of the private lab (rig/asl-lab-entry.sh defaults); not credentials of any service
ACCT_USER = "acct"
ACCT_SECRET = "test-secret-not-real"
REG_SECRET = "test-secret-not-real"
TONE_HZ = 1000
TONE_NAME = "tone1k.ulaw"
HOSTS = ("reg", "a", "b", "e")


class LabError(RuntimeError):
    pass


def lin2ulaw(sample):
    """One S16 sample to G.711 mu-law (the classic bias-and-segment encoder)."""
    bias, clip = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    sample = min(sample, clip) + bias
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not sample & mask:
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0f
    return (~(sign | (exponent << 4) | mantissa)) & 0xff


def ulaw_bytes(pcm):
    """S16LE PCM bytes to mu-law bytes."""
    return bytes(lin2ulaw(int.from_bytes(pcm[i:i + 2], "little", signed=True)) for i in range(0, len(pcm) - 1, 2))


def ulaw2lin(byte):
    """One G.711 mu-law byte to an S16 sample."""
    byte = ~byte & 0xff
    magnitude = (((byte & 0x0f) << 3) + 0x84) << ((byte & 0x70) >> 4)
    return (0x84 - magnitude) if byte & 0x80 else (magnitude - 0x84)


def ulaw_to_pcm(data):
    """mu-law bytes to S16LE PCM bytes."""
    return b"".join(ulaw2lin(b).to_bytes(2, "little", signed=True) for b in data)


def tone_ulaw(ms=3000, freq=TONE_HZ, amp=8000):
    """The scenario tone: a pure sine, 8 kHz, mu-law. Deterministic on one platform."""
    return ulaw_bytes(signals.tone(freq, ms, amp=amp))


def tone_energy(pcm, freq=TONE_HZ):
    """(rms, share of the power at freq) of S16LE PCM; the share is ~1 for a clean tone."""
    return score.rms(pcm), score.tone_ratio(pcm, freq)


def loud_part(pcm, floor=500, size=160):
    """The 20 ms frames of pcm whose rms exceeds floor, joined (silence around the tone dropped)."""
    out = bytearray()
    for i in range(0, len(pcm) - size * 2 + 1, size * 2):
        chunk = pcm[i:i + size * 2]
        if score.rms(chunk) > floor:
            out += chunk
    return bytes(out)


@dataclass
class Host:
    name: str
    ip: str
    node: str = ""

    def _req(self, method, path, data=None, ctype="application/json", timeout=40):
        req = urllib.request.Request("http://%s:%d%s" % (self.ip, AGENT_PORT, path), data=data, method=method,
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            raise LabError("%s %s on %s: HTTP %d %s" % (method, path, self.name, err.code, err.read()[:200])) from None
        except (urllib.error.URLError, OSError) as err:
            raise LabError("%s %s on %s: %s" % (method, path, self.name, err)) from None

    def post(self, path, obj):
        return json.loads(self._req("POST", path, json.dumps(obj).encode()))

    def ready(self, timeout=90):
        """The agent answers and Asterisk takes CLI commands (the host may still be starting)."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                if json.loads(self._req("GET", "/health", timeout=3)).get("ok"):
                    reply = self.post("/cli", {"cmd": "core show uptime"})
                    if reply["rc"] == 0 and "System uptime" in reply["out"]:
                        return
                    last = reply["out"][:80]
            except LabError as err:
                last = str(err)
            time.sleep(1)
        raise LabError("lab host %s (%s) not ready: %s" % (self.name, self.ip, last))

    def cli(self, cmd):
        reply = self.post("/cli", {"cmd": cmd})
        if reply["rc"] != 0:
            raise LabError("%s: cli %r -> rc %d: %s" % (self.name, cmd, reply["rc"], reply["out"][:200]))
        return reply["out"]

    def capture_start(self, name):
        self.post("/capture/start", {"name": name})

    def capture_stop(self, name):
        return self.post("/capture/stop", {"name": name})["bytes"]

    def set_scenario(self, name):
        self.post("/scenario", {"name": name})

    def put_media(self, name, data):
        self._req("PUT", "/media/" + name, data, "application/octet-stream")

    def register_lines(self, lines):
        self.post("/register", {"lines": list(lines)})

    def files(self, area):
        return json.loads(self._req("GET", "/files/" + area))

    def fetch(self, area, rel):
        return self._req("GET", "/files/%s/%s" % (area, rel))

    def versions_text(self):
        return self._req("GET", "/versions").decode()

    def disconnect_links(self):
        """`rpt fun <node> *6` (disconnect all links). app_rpt runs the function a few hundred ms later, so a call
        placed right after it can be torn down by it: wait (Lab.quiet does) before the next call."""
        if self.node:
            self.cli("rpt fun %s *6" % self.node)

    def hangup_all(self, settle=5.0):
        """Drop every IAX2 channel of this host (after disconnect_links; a scenario starts from a quiet host)."""
        deadline = time.monotonic() + settle
        while True:
            names = [ln.split("!")[0] for ln in self.cli("core show channels concise").splitlines()
                     if ln.startswith("IAX2/")]
            if not names:
                return
            for name in names:
                self.cli("channel request hangup " + name)
            if time.monotonic() > deadline:
                raise LabError("%s: IAX2 channels remain: %s" % (self.name, names))
            time.sleep(0.3)

    def wait_link(self, peer_node, timeout=15.0):
        """Poll `rpt lstats` until the link to peer_node is ESTABLISHED; returns the CLI table."""
        deadline = time.monotonic() + timeout
        out = ""
        while time.monotonic() < deadline:
            out = self.cli("rpt lstats %s" % self.node)
            if any(ln.split()[:1] == [str(peer_node)] and "ESTABLISHED" in ln for ln in out.splitlines()):
                return out
            time.sleep(0.25)
        raise LabError("%s: no ESTABLISHED link to %s after %.0f s:\n%s" % (self.name, peer_node, timeout, out))

    def wait_calls(self, count, timeout=15.0):
        """Poll until `iax2 show channels` lists `count` active IAX2 channels (as the CLI counts them)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            out = self.cli("iax2 show channels")
            if "%d active IAX channel" % count in out:
                return
            time.sleep(0.25)
        raise LabError("%s: not %d IAX2 channels after %.0f s" % (self.name, count, timeout))

    def versions(self):
        """Asterisk version line and the loaded app_rpt build, for the run note."""
        return self.cli("core show version").strip()


@dataclass
class Scenario:
    name: str
    dir: Path
    notes: dict = field(default_factory=dict)
    recs: dict = field(default_factory=dict)       # host key -> paths of the recordings fetched by this run

    def note(self, key, value):
        self.notes[key] = value


@dataclass
class Lab:
    hosts: dict
    out: Path
    port: int
    domain: str
    tester_node: str = "299902"      # the tester as a node of the lab zone (its own address)

    def node(self, key):
        return self.hosts[key]

    def wait_ready(self):
        for h in self.hosts.values():
            h.ready()

    def write_versions(self):
        """out/asl-lab/versions.txt: the Asterisk / app_rpt package versions (dpkg-query) and the CLI version line of
        every lab host, for the run note."""
        lines = []
        for key, host in self.hosts.items():
            lines.append("== %s (%s)" % (host.name, host.ip))
            lines.append(host.versions_text().strip())
            lines.append(host.cli("core show version").strip())
            lines.append(host.cli("module show like app_rpt").strip())
        path = self.out / "asl-lab" / "versions.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        return path

    def quiet(self, keys=("a", "b", "e")):
        """Disconnect every link of the hosts, let app_rpt run it, drop what is left."""
        for key in keys:
            self.hosts[key].disconnect_links()
        time.sleep(1.2)
        for key in keys:
            self.hosts[key].hangup_all()

    def put_tone(self, ms=3000, hosts=("a", "b", "e")):
        """Upload the 1 kHz tone to the hosts; returns (sha256, bytes). The asterisk path is /var/lib/asterisk/sounds/dvt/tone1k."""
        data = tone_ulaw(ms)
        for key in hosts:
            self.hosts[key].put_media(TONE_NAME, data)
        return hashlib.sha256(data).hexdigest(), len(data)

    @contextmanager
    def scenario(self, name, capture=("a",), hosts=None):
        """Name the scenario on every host (recording directory), run tcpdump on `capture` hosts, and on exit
        stop them and copy pcaps and recordings into out/asl-lab/<name>/ (pcap per host: <host>.pcap)."""
        sdir = self.out / "asl-lab" / name
        (sdir / "rec").mkdir(parents=True, exist_ok=True)
        sc = Scenario(name, sdir)
        keys = list(hosts or self.hosts)
        for key in keys:
            self.hosts[key].post("/rec/clear", {"scenario": name})         # a rerun starts empty
            self.hosts[key].set_scenario(name)
        for key in capture:
            self.hosts[key].capture_start(name)
        started = time.time()
        try:
            yield sc
        finally:
            fetched = {}
            for key in capture:
                try:
                    self.hosts[key].capture_stop(name)
                    (sdir / ("%s.pcap" % key)).write_bytes(self.hosts[key].fetch("pcap", name + ".pcap"))
                    fetched[key] = (sdir / ("%s.pcap" % key)).stat().st_size
                except LabError as err:
                    sc.notes["capture_error_" + key] = str(err)
            recs = {}
            for key in keys:
                try:
                    for ent in self.hosts[key].files("rec"):
                        if not ent["path"].startswith(name + "/"):
                            continue
                        dest = sdir / "rec" / key / ent["path"][len(name) + 1:]
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(self.hosts[key].fetch("rec", ent["path"]))
                        recs.setdefault(key, []).append(dest.name)
                        sc.recs.setdefault(key, []).append(dest)
                except LabError as err:
                    sc.notes["rec_error_" + key] = str(err)
            meta = {"scenario": name, "started": started, "seconds": round(time.time() - started, 2),
                    "pcaps": fetched, "recordings": recs, "notes": sc.notes}
            (sdir / "scenario.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")


def from_env(env=None):
    """The lab described by the tester's DVT_ASL_* variables, or None when profile asl-lab is not up."""
    env = os.environ if env is None else env
    if env.get("DVT_ASL_LAB") != "on":
        return None
    hosts = {
        "reg": Host("asl-reg", env["DVT_ASL_REG"]),
        "a": Host("asl-a", env["DVT_ASL_A"], env["DVT_ASL_NODE_A"]),
        "b": Host("asl-b", env["DVT_ASL_B"], env["DVT_ASL_NODE_B"]),
        "e": Host("asl-e", env["DVT_ASL_E"], env["DVT_ASL_NODE_E"]),
    }
    return Lab(hosts, Path(env.get("DVT_OUT", "out")), int(env.get("DVT_ASL_IAX_PORT", "4569")),
               env.get("DVT_ASL_DOMAIN", "nodes.asl.test"), env.get("DVT_ASL_NODE_T", "299902"))
