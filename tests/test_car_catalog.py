"""Regression tests for ACC car discovery — specifically the UTF-16 decals.json
crash that took the whole app down at launch (UnicodeDecodeError on byte 0xff).
"""

import json
from pathlib import Path

import fd6.ac.car_catalog as car_catalog
from fd6.ac.car_catalog import _read_json_tolerant, _user_discovered_cars, list_cars
from fd6.ac.profiles import get_profile


def test_read_json_tolerant_handles_utf16(tmp_path: Path):
    p = tmp_path / "decals.json"
    p.write_bytes(json.dumps({"carModel": "ferrari_488_gt3"}).encode("utf-16"))  # BOM ff fe
    assert _read_json_tolerant(p) == {"carModel": "ferrari_488_gt3"}


def test_read_json_tolerant_handles_utf8_bom(tmp_path: Path):
    p = tmp_path / "decals.json"
    p.write_bytes(b"\xef\xbb\xbf" + json.dumps({"carModel": "bmw_m4_gt3"}).encode("utf-8"))
    assert _read_json_tolerant(p) == {"carModel": "bmw_m4_gt3"}


def test_read_json_tolerant_handles_plain_utf8(tmp_path: Path):
    p = tmp_path / "decals.json"
    p.write_text(json.dumps({"carModel": "audi_r8_lms_evo"}), encoding="utf-8")
    assert _read_json_tolerant(p) == {"carModel": "audi_r8_lms_evo"}


def test_read_json_tolerant_returns_none_on_binary_garbage(tmp_path: Path):
    p = tmp_path / "decals.json"
    p.write_bytes(bytes(range(256)))  # not decodable as any JSON
    assert _read_json_tolerant(p) is None


def test_read_json_tolerant_returns_none_for_non_dict(tmp_path: Path):
    p = tmp_path / "decals.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert _read_json_tolerant(p) is None


def _acc_profile():
    try:
        return get_profile("acc")
    except Exception:
        return None


def test_user_discovery_survives_utf16_file(tmp_path: Path, monkeypatch):
    """A UTF-16 decals.json must not raise — it used to crash MainWindow init."""
    prof = _acc_profile()
    if prof is None:
        return  # ACC profile not present in this build; nothing to assert
    team = tmp_path / "MyTeam"
    team.mkdir()
    (team / "decals.json").write_bytes(
        json.dumps({"carModel": "porsche_992_gt3_r"}).encode("utf-16")
    )
    monkeypatch.setattr(car_catalog, "livery_root", lambda _p: tmp_path)

    cars = _user_discovered_cars(prof)  # must not raise
    assert any(c.car_model == "porsche_992_gt3_r" for c in cars)

    # And the full catalog path (what the GUI calls) also stays crash-free.
    assert isinstance(list_cars(prof), list)
