"""signals.py + preflight.py 통합 테스트 — 저장소의 실제 가격 데이터 사용."""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA_OK = (ROOT / "data" / "prices").exists()
pytestmark = pytest.mark.skipif(not DATA_OK, reason="가격 데이터 없음")

APPROVED_0925 = {"meeting_id": "2026-09-25 정례위원회 승인안", "approved_date": "2026-09-25",
                 "valid_until": "2026-10-16", "market": "us", "mr_scale": 0.0, "mr_policy": "hold",
                 "overrides": {}}


def run_signals(tmp_path, monkeypatch, approved):
    S = importlib.reload(importlib.import_module("signals"))
    dd = tmp_path / "decisions"; dd.mkdir(exist_ok=True)
    if approved is not None:
        (dd / "approved.json").write_text(json.dumps(approved))
    monkeypatch.setattr(S, "ROOT", tmp_path)
    out = tmp_path / "signals" / "target_weights.json"
    monkeypatch.setattr(S, "OUT", out)
    S.main()
    return json.loads(out.read_text())


def test_approved_0925_gives_bil35_mr_hold(tmp_path, monkeypatch):
    sig = run_signals(tmp_path, monkeypatch, APPROVED_0925)
    assert sig["decision_state"] == "valid"
    assert sig["source"].startswith("위원회 결정 오버레이")
    assert sig["mr_scale"] == 0.0 and sig["mr_policy"] == "hold"
    assert sig["weights"]["BIL"] == pytest.approx(0.35, abs=1e-4)
    assert abs(sum(sig["weights"].values()) - 1) < 1e-6
    assert len(sig["weights"]) == 6


def test_expired_0902_file_blocks(tmp_path, monkeypatch):
    sig = run_signals(tmp_path, monkeypatch, {**APPROVED_0925, "valid_until": "2026-09-12",
                                              "overrides": {"EEM": 0.13, "HYG": 0.10, "BIL": 0.184657}})
    assert sig["decision_state"] == "expired"
    assert "집행 정지(C-21)" in sig["source"]


def test_missing_file_blocks(tmp_path, monkeypatch):
    assert run_signals(tmp_path, monkeypatch, None)["decision_state"] == "missing"


def _preflight(tmp_sig: dict, mkt="us"):
    """실제 저장소 데이터로 preflight 실행 — 신호 파일만 임시로 바꿔치기 후 복원."""
    path = ROOT / ("signals/target_weights.json" if mkt == "us" else "signals/kr_target_weights.json")
    orig = path.read_text()
    rep = ROOT / "reports" / f"preflight_{mkt}.md"
    rep_orig = rep.read_text() if rep.exists() else None
    try:
        path.write_text(json.dumps(tmp_sig))
        r = subprocess.run([sys.executable, "pipeline/preflight.py", mkt], cwd=ROOT, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr
    finally:
        path.write_text(orig)
        if rep_orig is None:
            rep.unlink(missing_ok=True)
        else:
            rep.write_text(rep_orig)


def test_preflight_blocks_expired_and_passes_valid(tmp_path, monkeypatch):
    valid = run_signals(tmp_path, monkeypatch, APPROVED_0925)
    rc, out = _preflight(valid)
    assert rc == 0, out
    expired = dict(valid, decision_state="expired", source="F1 원신호 (결정 만료) — 집행 정지(C-21)")
    rc, out = _preflight(expired)
    assert rc != 0 and "위원회 결정 상태 'expired'" in out


def test_defect_s_turnover_measured_against_last_executed_target():
    """9/28 실증: 토요일 SKIP 런이 커밋한 신호와 비교해 0.0%가 보고됐다. 실집행 목표 대비면 8pp가 잡혀야 한다."""
    cur = json.loads((ROOT / "signals/target_weights.json").read_text())
    cur["decision_state"] = "valid"
    mr_today = [t for t in cur["weights"] if cur["weights"][t] == 0.01]
    executed = {k: v for k, v in cur["weights"].items() if k not in mr_today}
    for t in ["ADI", "AMGN", "APD"] + mr_today[:2] + ["AAPL", "CB", "ADM"][:0]:
        executed[t] = 0.01
    executed["BIL"] = round(1 - sum(v for k, v in executed.items() if k != "BIL"), 6)
    le = ROOT / "signals" / "last_executed_target_us.json"
    had = le.exists()
    le.write_text(json.dumps({"date": "2026-09-25", "weights": executed}))
    try:
        rc, out = _preflight(cur)
    finally:
        if not had:
            le.unlink()
    assert rc == 0, out
    assert "기준: 마지막 실집행 2026-09-25" in out
    turn = float(out.split("매수 회전율 ")[1].split("%")[0])
    assert 7.0 <= turn <= 9.0, out   # 신규 MR 8종 × 1% = 8pp (HEAD 비교였다면 0.0%)
