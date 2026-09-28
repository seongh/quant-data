"""decision.py 단위 테스트 — C-21(만료=정지), R-26(ISO 날짜), #21(override cap)."""
import json
from datetime import date

import pytest

import decision as D

TODAY = date(2026, 9, 28)
BASE = {"meeting_id": "t", "approved_date": "2026-09-25", "valid_until": "2026-10-16",
        "market": "us", "mr_scale": 0.0, "mr_policy": "hold", "overrides": {}}


def _write(tmp_path, obj, name="approved.json"):
    (tmp_path / name).write_text(obj if isinstance(obj, str) else json.dumps(obj))


def test_valid(tmp_path):
    _write(tmp_path, BASE)
    dec, st, _ = D.load_decision("us", tmp_path, TODAY)
    assert st == "valid" and dec["mr_scale"] == 0.0


def test_repo_file_of_0902_is_expired(tmp_path):
    _write(tmp_path, {**BASE, "valid_until": "2026-09-12"})
    dec, st, note = D.load_decision("us", tmp_path, TODAY)
    assert dec is None and st == "expired" and "2026-09-12" in note


def test_last_valid_day_inclusive(tmp_path):
    _write(tmp_path, {**BASE, "valid_until": "2026-09-28"})
    assert D.load_decision("us", tmp_path, TODAY)[1] == "valid"


def test_missing(tmp_path):
    assert D.load_decision("us", tmp_path, TODAY)[1] == "missing"


@pytest.mark.parametrize("vu", ["저장소 도달 +14일", "repo_arrival+14d", "D+14", "+14d", "2026/10/16",
                                "2026-10-16T00:00", "2026-02-30", "", None])
def test_r26_non_iso_is_invalid_not_eternal(tmp_path, vu):
    """9/23 실측: 옛 문자열 비교는 '저장소 도달 +14일' 등을 영구 유효로 판정했다."""
    _write(tmp_path, {**BASE, "valid_until": vu})
    assert D.load_decision("us", tmp_path, TODAY)[1] == "invalid"


def test_bad_json_and_wrong_market(tmp_path):
    _write(tmp_path, "{not json")
    assert D.load_decision("us", tmp_path, TODAY)[1] == "invalid"
    _write(tmp_path, {**BASE, "market": "kr"})
    assert D.load_decision("us", tmp_path, TODAY)[1] == "invalid"


def test_kr_file_is_separate(tmp_path):
    _write(tmp_path, BASE)  # US 파일만 있음
    assert D.load_decision("kr", tmp_path, TODAY)[1] == "missing"
    _write(tmp_path, {**BASE, "market": "kr"}, "approved_kr.json")
    assert D.load_decision("kr", tmp_path, TODAY)[1] == "valid"


def test_unknown_semantics_and_bad_weights_invalid(tmp_path):
    _write(tmp_path, {**BASE, "override_semantics": "pin"})
    assert D.load_decision("us", tmp_path, TODAY)[1] == "invalid"
    _write(tmp_path, {**BASE, "overrides": {"EEM": 1.5}})
    assert D.load_decision("us", tmp_path, TODAY)[1] == "invalid"


F1 = {"BIL": 0.28, "EEM": 0.21, "SPY": 0.18, "EFA": 0.1145, "QQQ": 0.0738, "DBC": 0.0707, "ADM": 0.01,
      "APA": 0.01, "ARE": 0.01, "AWK": 0.01, "CB": 0.01, "BAX": 0.01, "CDW": 0.0055, "CF": 0.0055}
assert abs(sum(F1.values()) - 1) < 1e-9


def test_empty_overrides_identity():
    out = D.apply_overrides(F1, {"overrides": {}}, "BIL")
    assert out == pytest.approx({k: round(v, 6) for k, v in F1.items()})
    assert abs(sum(out.values()) - 1) < 1e-6


def test_cap_only_reduces_risk_and_never_creates():
    """9/18 규명: 옛 코드는 SET이라 F1이 override 아래로 내려가면 확대 방향으로 반전됐다."""
    out = D.apply_overrides(F1, {"overrides": {"EEM": 0.13, "HYG": 0.10, "SPY": 0.30}}, "BIL")
    assert out["EEM"] == 0.13            # 21% → 13% 축소
    assert "HYG" not in out              # cap 은 신규 편입 불가
    assert out["SPY"] == 0.18            # 캡(30%)이 F1(18%)보다 크면 F1 유지 — 확대 불가
    assert out["BIL"] == pytest.approx(0.28 + 0.08, abs=1e-6)
    assert abs(sum(out.values()) - 1) < 1e-6 and min(out.values()) >= 0


def test_set_semantics_explicit_only():
    out = D.apply_overrides(F1, {"override_semantics": "set", "overrides": {"HYG": 0.10}}, "BIL")
    assert out["HYG"] == 0.10 and abs(sum(out.values()) - 1) < 1e-6 and min(out.values()) >= 0


def test_set_overflow_shrinks_risk_not_negative_cash():
    out = D.apply_overrides(F1, {"override_semantics": "set", "overrides": {"SPY": 0.6, "EEM": 0.5}}, "BIL")
    assert abs(sum(out.values()) - 1) < 1e-6 and min(out.values()) >= 0
