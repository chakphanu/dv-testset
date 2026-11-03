# SPDX-License-Identifier: MIT
"""rig/asl-lab-registrar.py: the plain-HTTP stand-in for the ASL registrar, six variants, full request log."""
import importlib.util
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "rig" / "asl-lab-registrar.py"


@pytest.fixture
def registrar(tmp_path, monkeypatch):
    monkeypatch.setenv("DVT_ASL_REG_LOG", str(tmp_path / "requests.jsonl"))
    monkeypatch.setenv("DVT_ASL_REG_SLOW_S", "0.4")
    spec = importlib.util.spec_from_file_location("asl_lab_registrar", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    srv = mod.make_server(0, "127.0.0.1")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/" % srv.server_address[1], tmp_path / "requests.jsonl"
    srv.shutdown()
    srv.server_close()


def post(url, node, port=4569, raw=None):
    body = raw if raw is not None else json.dumps(
        {"port": port, "data": {"nodes": {node: {"node": node, "passwd": "test-secret-not-real", "remote": 0}}}}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_ok_reports_the_sender_and_the_request_port(registrar):
    url, _log = registrar
    status, data = post(url, "1991", port=4570)
    reply = json.loads(data)
    assert status == 200 and reply["ipaddr"] == "127.0.0.1" and reply["port"] == 4570 and reply["refresh"] == 180
    assert "successfully registered" in reply["data"]


def test_port_variant_answers_another_port(registrar):
    url, _log = registrar
    reply = json.loads(post(url, "1992", port=4569)[1])
    assert reply["port"] == 4570 and "successfully registered" in reply["data"]


def test_http500_notext_oversize(registrar):
    url, _log = registrar
    assert post(url, "1993")[0] == 500
    status, data = post(url, "1994")
    assert status == 200 and "successfully registered" not in json.loads(data)["data"]
    status, data = post(url, "1996")
    assert status == 200 and len(data) > 4096 and "successfully registered" in json.loads(data)["data"]


def test_slow_variant_waits_then_answers(registrar):
    import time
    url, _log = registrar
    t0 = time.monotonic()
    status, data = post(url, "1995")
    assert status == 200 and time.monotonic() - t0 >= 0.4 and "successfully registered" in json.loads(data)["data"]


def test_every_request_is_logged_in_full(registrar):
    url, log = registrar
    post(url, "1991")
    post(url, "1993")
    post(url, "x", raw=b"not json")
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert [r["variant"] for r in rows] == ["ok", "http500", "bad-request"]
    first = rows[0]
    assert first["headers"]["Content-Type"] == "application/json" and first["path"] == "/"
    assert first["body"]["data"]["nodes"]["1991"] == {"node": "1991", "passwd": "test-secret-not-real", "remote": 0}
    assert rows[2]["status"] == 400 and rows[2]["raw"] == "not json"


def test_unknown_node_gets_ok_and_get_answers_ok(registrar):
    url, _log = registrar
    assert "successfully registered" in json.loads(post(url, "1500")[1])["data"]
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.read() == b"OK"
