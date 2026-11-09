# SPDX-License-Identifier: MIT
"""cases/position_map2_support.py (DP7 / MAP2 `#/positions` DOM case): the descriptor checks, the MANIFEST v2 checks, the tile arithmetic
and the staging of a tree written by `dv-svc-basemap install`, against a tiny generated tree (no binary is committed)."""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import position2_support as p2  # noqa: E402
import position_map2_support as m2  # noqa: E402

pytestmark = pytest.mark.unit

GEN = "0123456789abcdef"


def good_descriptor():
    return {"installed": True, "gen": GEN, "tiles": ["/basemap/tiles/%s/{z}/{x}/{y}.mvt" % GEN], "minzoom": 0, "maxzoom": 12,
            "overview_maxzoom": 5, "coverage": {"boxes": [[99.9, 13.0, 101.1, 14.1]], "cells": ["10/797/487"]}, "builds": ["20261107"],
            "fetch_enabled": False, "attribution": "(c) OpenStreetMap contributors",
            "files": [{"path": "glyphs/FONT-FACES.json", "bytes": 10, "sha256": "0" * 64}, {"path": "glyphs/OFL.txt", "bytes": 5, "sha256": "1" * 64},
                      {"path": "glyphs/NotoSansRegular/0-255.pbf", "bytes": 7, "sha256": "2" * 64}]}


def compact_descriptor():
    desc = good_descriptor()
    del desc["files"]
    return desc


def test_good_descriptor_has_no_problems():
    assert m2.descriptor_problems(good_descriptor()) == []


def test_compact_descriptor_has_no_problems():
    # MAP2f: no per-file glyph inventory; the shape is chosen by the presence of the key `files`
    assert m2.descriptor_problems(compact_descriptor()) == []
    assert m2.descriptor_problems(compact_descriptor(), 5850) == []


@pytest.mark.parametrize("value", [[], ["10/793/457"], True, 0, {"cells": []}, None])
def test_preparing_is_optional_and_its_shape_is_not_asserted(value):
    # MAP2d (wip-f9aaebc) sends `preparing`; caebc57 and c12db8b do not. Either is a valid descriptor, whatever the value is.
    with_key = compact_descriptor()
    with_key["preparing"] = value
    assert m2.descriptor_problems(with_key) == []
    assert m2.descriptor_problems(good_descriptor() | {"preparing": value}) == []
    assert m2.descriptor_problems(compact_descriptor()) == []                      # absent stays valid
    assert "preparing=" in m2.optional_notes(with_key)


def test_optional_notes_record_type_and_value_or_absence():
    desc = compact_descriptor()
    assert m2.optional_notes(desc) == "preparing absent"
    desc["preparing"] = ["10/793/457"]
    assert m2.optional_notes(desc) == 'preparing=list ["10/793/457"]'
    desc["preparing"] = {"a": 1}
    assert m2.optional_notes(desc) == 'preparing=dict {"a": 1}'
    assert m2.optional_notes(None) == "descriptor is not an object"
    assert len(m2.optional_notes({"preparing": "x" * 500})) < 160


def test_unknown_keys_next_to_preparing_still_fail():
    desc = compact_descriptor()
    desc.update(preparing=[], callsign="N0PGA")
    assert any("keys" in p for p in m2.descriptor_problems(desc))
    desc = compact_descriptor()
    desc["preparing"] = []
    desc.pop("attribution")
    assert any("keys" in p for p in m2.descriptor_problems(desc))


def test_compact_descriptor_is_bounded():
    problems = m2.descriptor_problems(compact_descriptor(), m2.MAX_DESCRIPTOR_BYTES + 1)
    assert any("bytes" in p for p in problems), problems
    assert m2.descriptor_problems(compact_descriptor(), m2.MAX_DESCRIPTOR_BYTES) == []


def test_older_descriptor_with_files_has_no_size_bound():
    # an image before MAP2f carries the glyph inventory; 35767 bytes were seen on caebc57
    assert m2.descriptor_problems(good_descriptor(), 35767) == []
    assert m2.descriptor_problems(good_descriptor(), m2.MAX_DESCRIPTOR_BYTES + 1) == []


def test_compact_descriptor_over_the_bound_fails_at_the_same_size():
    assert any("bytes" in p for p in m2.descriptor_problems(compact_descriptor(), 35767))


@pytest.mark.parametrize("change,needle", [
    (lambda d: d.update(callsign="N0PGA"), "keys"),
    (lambda d: d.pop("attribution"), "keys"),
    (lambda d: d.update(gen="ABC"), "gen"),
    (lambda d: d.update(fetch_enabled=True), "fetch_enabled"),
])
def test_compact_descriptor_stays_strict(change, needle):
    desc = compact_descriptor()
    change(desc)
    problems = m2.descriptor_problems(desc)
    assert problems and any(needle in p for p in problems), problems


def test_files_key_keeps_the_older_shape():
    desc = compact_descriptor()
    desc["files"] = "not a list"
    assert any("files is not a list" in p for p in m2.descriptor_problems(desc))


@pytest.mark.parametrize("change,needle", [
    (lambda d: d.update(installed=False), "installed"),
    (lambda d: d.update(gen="ABC"), "gen"),
    (lambda d: d.update(tiles=["/basemap/tiles/ffffffffffffffff/{z}/{x}/{y}.mvt"]), "tiles"),
    (lambda d: d.update(minzoom=1), "minzoom"),
    (lambda d: d.update(maxzoom=16), "maxzoom"),
    (lambda d: d.update(overview_maxzoom=12), "overview_maxzoom"),
    (lambda d: d.update(fetch_enabled=True), "fetch_enabled"),
    (lambda d: d.update(attribution="OSM"), "attribution"),
    (lambda d: d.update(builds=["2026-11-07"]), "builds"),
    (lambda d: d.update(callsign="N0PGA"), "keys"),
    (lambda d: d["coverage"].update(boxes=[[99.9, 14.1, 101.1, 13.0]]), "boxes"),
    (lambda d: d["coverage"].update(cells=["10/1024/5"] * 0 + ["9/1/1"]), "cells"),
    (lambda d: d["files"].pop(0), "FONT-FACES"),
    (lambda d: d["files"].append({"path": "../etc/passwd"}), "outside glyphs"),
])
def test_descriptor_problems_name_the_field(change, needle):
    desc = good_descriptor()
    change(desc)
    problems = m2.descriptor_problems(desc)
    assert problems and any(needle in p for p in problems), problems


def test_descriptor_not_an_object():
    assert m2.descriptor_problems([]) == ["the descriptor is not a JSON object"]
    assert m2.descriptor_problems(m2.NOT_INSTALLED)        # installed false is a problem for an INSTALLED descriptor


def test_font_faces():
    files = good_descriptor()["files"] + [{"path": "glyphs/fonts/NotoSans-Regular.ttf"}]
    faces = {"NotoSansRegular": [{"url": "glyphs/fonts/NotoSans-Regular.ttf", "unicode-range": ["U+20-7E"]}]}
    assert m2.font_face_problems(faces, files) == []
    assert m2.font_face_problems({"Other": []}, files)
    assert m2.font_face_problems({"NotoSansRegular": []}, files)
    unlisted = {"NotoSansRegular": [{"url": "glyphs/fonts/Missing.ttf", "unicode-range": ["U+20-7E"]}]}
    assert any("not a listed" in p for p in m2.font_face_problems(unlisted, files))
    norange = {"NotoSansRegular": [{"url": "glyphs/fonts/NotoSans-Regular.ttf", "unicode-range": []}]}
    assert any("unicode-range" in p for p in m2.font_face_problems(norange, files))


def test_font_faces_against_the_manifest_files():
    manifest_files = [{"path": "glyphs/fonts/NotoSans-Regular.ttf", "bytes": 3, "sha256": "3" * 64}]
    faces = {"NotoSansRegular": [{"url": "glyphs/fonts/NotoSans-Regular.ttf", "unicode-range": ["U+20-7E"]}]}
    assert m2.font_face_problems(faces, manifest_files) == []
    other = {"NotoSansRegular": [{"url": "glyphs/fonts/Other.ttf", "unicode-range": ["U+20-7E"]}]}
    assert any("not a listed" in p for p in m2.font_face_problems(other, manifest_files))


def test_tile_requests_and_arithmetic():
    paths = ["/basemap/tiles.json", "/basemap/tiles/%s/10/797/472.mvt" % GEN, "/basemap/tiles/%s/0/0/0.mvt" % GEN, "/basemap/tiles/xyz/0/0/0.mvt",
             "/basemap/glyphs/FONT-FACES.json"]
    assert [t[:4] for t in m2.tile_requests(paths)] == [(GEN, 10, 797, 472), (GEN, 0, 0, 0)]
    assert m2.lonlat_tile(100.5, 13.75, 10) == (797, 472)       # Bangkok at z10 (y = (1 - asinh(tan 13.75 deg) / pi) / 2 * 1024 = 472.5)
    assert m2.lonlat_tile(-179.9, 80.0, 12) == (1, 459) and m2.lonlat_tile(179.99, -85.0, 3) == (7, 7)
    assert m2.other_gen(GEN) != GEN and m2.other_gen("0" * 16) != "0" * 16


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_tree(root, mutate=None):
    """A minimal tree shaped like `dv-svc-basemap install --state X` output: X/basemap/base/{MANIFEST, archives, glyphs/}, regions/INDEX."""
    base = root / "basemap" / "base"
    (base / "glyphs" / "NotoSansRegular").mkdir(parents=True)
    (root / "basemap" / "regions").mkdir()
    (root / "basemap" / "regions" / "INDEX").write_text("{}\n", encoding="ascii")
    overview, detail = b"O" * 200, b"D" * 300
    pbf, lic = b"pbf-bytes", b"Fixture font licence.\n"
    (base / "overview-20261107-z0-5.pmtiles").write_bytes(overview)
    (base / "dvt-20261107-z6-12.pmtiles").write_bytes(detail)
    (base / "glyphs" / "NotoSansRegular" / "0-255.pbf").write_bytes(pbf)
    (base / "glyphs" / "OFL.txt").write_bytes(lic)
    manifest = {"version": 2, "schema": "4.15.2", "overview_maxzoom": 5, "detail_maxzoom": 12,
                "tiers": [{"role": "overview", "name": "world", "archive": "overview-20261107-z0-5.pmtiles", "build": "20261107",
                           "bbox": [-180.0, -85.0511287798, 180.0, 85.0511287798], "minzoom": 0, "maxzoom": 5, "bytes": len(overview), "sha256": sha(overview)},
                          {"role": "detail", "name": "dvt", "archive": "dvt-20261107-z6-12.pmtiles", "build": "20261107",
                           "bbox": [99.9, 13.0, 101.1, 14.1], "minzoom": 6, "maxzoom": 12, "bytes": len(detail), "sha256": sha(detail)}],
                "files": [{"path": "glyphs/NotoSansRegular/0-255.pbf", "bytes": len(pbf), "sha256": sha(pbf)},
                          {"path": "glyphs/OFL.txt", "bytes": len(lic), "sha256": sha(lic)}]}
    if mutate:
        mutate(manifest)
    (base / "MANIFEST").write_text(json.dumps(manifest), encoding="ascii")
    return root / "basemap", manifest


def test_manifest_problems(tmp_path):
    _src, manifest = make_tree(tmp_path)
    assert m2.manifest_problems(manifest) == []
    assert m2.detail_bbox(manifest, [0, 0, 1, 1]) == [99.9, 13.0, 101.1, 14.1]
    assert m2.detail_bbox({}, [0, 0, 1, 1]) == [0, 0, 1, 1]
    v1 = {"version": 1, "archive": "basemap-20261106-z8.pmtiles", "files": []}
    assert any("v1" in p for p in m2.manifest_problems(v1))
    for change, needle in [(lambda m: m["tiers"].pop(0), "tiers"), (lambda m: m.update(schema="3.1"), "schema"),
                           (lambda m: m["tiers"][1].update(minzoom=7), "detail tier"), (lambda m: m["tiers"][1].update(build="20261108"), "builds"),
                           (lambda m: m["files"].pop(1), "OFL"), (lambda m: m["files"][0].update(path="fonts/x.pbf"), "glyphs/"),
                           (lambda m: m.update(overview_maxzoom=12), "overview_maxzoom")]:
        broken = copy.deepcopy(manifest)
        change(broken)
        assert any(needle in p for p in m2.manifest_problems(broken)), (needle, m2.manifest_problems(broken))


def test_install_tree_copies_a_verified_base_and_removes_only_its_own(tmp_path):
    src, manifest = make_tree(tmp_path / "product")
    assert m2.flavor(src) == "map2" and m2.flavor(src.parent) == "map2" and m2.map2_root(src.parent) == src
    state = tmp_path / "state"
    state.mkdir()
    got = m2.install_tree(state, src.parent)           # the --state directory named instead of its basemap/
    assert got == manifest
    assert (state / "basemap" / "base" / "MANIFEST").is_file() and (state / "basemap" / "regions" / "INDEX").is_file()
    assert (state / "basemap" / m2.INSTALLED_MARK).is_file() and not (state / "basemap.dvt-staging").exists()
    with pytest.raises(ValueError, match="exists already"):
        m2.install_tree(state, src)
    assert m2.remove_tree(state) is True and not (state / "basemap").exists()
    (state / "basemap").mkdir()
    assert m2.remove_tree(state) is False and (state / "basemap").is_dir()      # no marker: not ours


def test_install_tree_refuses_what_is_not_a_complete_v2_tree(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    src, _m = make_tree(tmp_path / "bad_hash")
    (src / "base" / "glyphs" / "OFL.txt").write_bytes(b"changed in transit\n")
    with pytest.raises(ValueError, match="OFL.txt"):
        m2.install_tree(state, src)
    src, _m = make_tree(tmp_path / "no_index")
    (src / "regions" / "INDEX").unlink()
    with pytest.raises(ValueError, match="regions/INDEX"):
        m2.install_tree(state, src)
    src, _m = make_tree(tmp_path / "v1_manifest", mutate=lambda m: m.update(version=1))
    with pytest.raises(ValueError, match="version"):
        m2.install_tree(state, src)
    with pytest.raises(ValueError, match="no base/MANIFEST"):
        m2.install_tree(state, tmp_path / "nothing")
    src, _m = make_tree(tmp_path / "link")
    (src / "base" / "glyphs" / "OFL.txt").unlink()
    (src / "base" / "glyphs" / "OFL.txt").symlink_to(tmp_path / "bad_hash" / "basemap" / "base" / "glyphs" / "OFL.txt")
    with pytest.raises(ValueError, match="regular file"):
        m2.install_tree(state, src)
    assert not (state / "basemap").exists() and not (state / "basemap.dvt-staging").exists()


def test_flavor_tells_map1_from_map2(tmp_path):
    v1 = tmp_path / "v1dir"
    v1.mkdir()
    (v1 / "MANIFEST").write_text("{}", encoding="ascii")
    assert m2.flavor(v1) == "v1"
    archive = tmp_path / "basemap.pmtiles"
    archive.write_bytes(p2.make_pmtiles_header(97.3, 5.6, 105.7, 20.5))
    assert m2.flavor(archive) == "v1"
    tar = tmp_path / "old.tar.gz"
    tar.write_bytes(b"x")
    assert m2.flavor(tar) == "v1"
    assert m2.flavor(tmp_path / "missing") is None and m2.flavor(tmp_path) is None
