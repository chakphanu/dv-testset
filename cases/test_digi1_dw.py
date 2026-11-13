# SPDX-License-Identifier: MIT
"""DV-Router DIGI1 cases, tier B (docs/digi1-cases.md "Tier B"): the fill-in digipeater of the AX.25 edge behind a REAL Dire Wolf TNC on the modem loop of
the DP15 tier B rig (profile dw: dw-a .48 and dw-b .49, image dvt-direwolf:local, AFSK 1200 baud audio relayed over UDP, no hardware;
docs/direwolf-cases.md "Modem loop"). Tier A (cases/test_digi1.py) plays the TNC with a fake and proves the logic byte for byte; tier B adds the real KISS
bytes of Dire Wolf and a real modulator and demodulator between the tester and the edge.

  DIGI-R1b  a frame the tester sends into dw-b's KISS port is transmitted by dw-b, demodulated by dw-a (the same bytes come out of dw-a's KISS port),
            heard by the edge on rf-a; the edge's repeat is written to dw-a, transmitted, demodulated by dw-b and comes out of dw-b's KISS port: the repeat
            of data.md 8.3 (G1, G2, G7, G12: our call in one hop, H bit set, everything else untouched) after a real modem; G4 (WIDE2-1 first) and G10
            (non-contiguous H bits) are silent. Nothing else comes out of dw-b.

The Dire Wolf TNCs here are the SUT's modems, with NO digipeater configured (the loop configuration has no DIGIPEAT line, as the edge requires). Dire Wolf
as the ORACLE (a DIGIPEAT rule on a node of its own) is the offline job `python3 -m dvtest.oracle_direwolf digi` (dvtest/digi1_oracle.py).

Run shape: `scripts/run.sh --profile web,dw` with rig/config/dv-router/digi1-dw.toml in DVT_SUT_CONFIG_EXTRA and DVT_DIGI1=on; every case skips with the
reason when its prerequisite is missing. A Dire Wolf is deaf while its own transmission would still be on the air: the cases pause PAUSE_S between
vectors. NOT RUN ON A RIG: written offline.
"""
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "proto"))

from dvtest import ax25, digi1  # noqa: E402

import data_support as ds  # noqa: E402
from data_support import write_note  # noqa: E402
from test_dp15_rf import read_rendered_doc  # noqa: E402
from test_dp15_rf_dw import LOOP_WAIT_S, PAUSE_S, dw  # noqa: E402,F401  (the fixture of tier B, reused as it is)

pytestmark = pytest.mark.rig

LOG = "digi1-dw.txt"
SETTING = "digi1 tier B"
NONCE = "%04d" % (int(time.time()) % 10000)
SILENCE_S = 12.0
VECTORS = ("G1", "G2", "G7", "G12", "G4", "G10")


def note(target, case, tag, text):
    write_note(target, case, tag, text, setting=SETTING, log=LOG)


@pytest.fixture(scope="module", autouse=True)
def digi1_switch():
    if os.environ.get("DVT_DIGI1") != "on":
        pytest.skip("DVT_DIGI1 is not on: the DIGI1 statements are asserted only on an image that has the fill-in digipeater (docs/digi1-cases.md)")


@pytest.fixture(scope="module")
def hub(rig, target, rig_control):
    yield from ds.hub_session(target, rig_control, lambda line: note(target, "hub", "RECORD", "end of run: " + line))


@pytest.fixture(scope="module")
def shape(target, dw):
    got = digi1.read_shape(read_rendered_doc(target), "rf-a")
    if got.name == "none" or got.problems:
        pytest.skip("the rendered config has no digipeating port rf-a with effective TX (%s): add digi1-dw.toml to DVT_SUT_CONFIG_EXTRA instead of "
                    "dp15-rf-dw.toml" % "; ".join(got.problems))
    if got.name != "roomy":
        pytest.skip("this case needs the roomy budget of digi1-dw.toml (rendered shape: %s)" % got.name)
    res = dw.tx_guard("DIGI1b") if getattr(dw, "tx_guard", None) else None
    if res is None or getattr(res, "state", "no_reader") not in ("unfenced", "clear"):
        time.sleep(13.0 + 3.0)
    return got


@pytest.mark.parametrize("vid", VECTORS)
def test_digi1_r1b_repeat_through_a_real_dire_wolf(rig, target, hub, dw, shape, vid):
    """data.md 8.3 after a real modem: see the module docstring. For a vector with result `repeat` exactly one frame, equal to the model's repeat, comes out of
    dw-b's KISS port (a real modulation and demodulation of the edge's bytes); the original comes out of dw-a's KISS port unchanged (the loop is intact).
    For a silent vector nothing comes out of dw-b for SILENCE_S seconds. The latency of the repeat (heard to demodulated) is recorded."""
    g = next(x for x in list(digi1.GOLDENS) + list(digi1.extra_vectors()) if x.id == vid)
    case = "DIGI-R1b[%s]" % vid
    frame = digi1.golden_frame(g, (" %sw%s" % (NONCE, vid)).encode("ascii"))
    assert ax25.dvrouter_accepts_frame(frame)
    time.sleep(PAUSE_S)
    t0 = time.monotonic()
    assert dw.b.send(frame), "the KISS connection to dw-b is gone"
    heard = dw.a.wait_frames(1, timeout=LOOP_WAIT_S, since=t0)
    assert [f for _t, _c, f in heard] == [frame], "dw-a handed out %s, not the frame sent into dw-b: the loop failed (cases/test_dp15_rf_dw.py 6l)" % [
        ax25.path_text(f) for _t, _c, f in heard]
    result, want = digi1.decide(frame, digi1.OURS, digi1.DedupeTable(), 0.0)
    assert result == g.result
    if want is not None:
        back = dw.b.wait_frames(1, timeout=LOOP_WAIT_S + 10.0, since=t0)
        time.sleep(3.0)
        back = dw.b.frames(t0)
        note(target, case, "RECORD", "%s heard at dw-a %.2f s after it was sent into dw-b; repeat at dw-b %s s: %s" % (
            ax25.path_text(frame), heard[0][0] - t0, None if not back else "%.2f" % (back[0][0] - t0), [ax25.path_text(f) for _t, _c, f in back]))
        assert [f for _t, _c, f in back] == [want], "dw-b handed out %s, expected one repeat %s" % ([ax25.path_text(f) for _t, _c, f in back], ax25.path_text(want))
        assert not digi1.repeat_problems(frame, back[0][2])
    else:
        time.sleep(SILENCE_S)
        back = dw.b.frames(t0)
        note(target, case, "RECORD", "%s heard at dw-a; nothing expected: dw-b handed out %s" % (ax25.path_text(frame), [ax25.path_text(f) for _t, _c, f in back]))
        assert back == [], "dw-b handed out %s for %s (result %s): a frame that fails a step must not be repeated" % (
            [ax25.path_text(f) for _t, _c, f in back], g.id, g.result)
