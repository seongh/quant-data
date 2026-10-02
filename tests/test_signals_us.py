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


def _preflight(tmp_sig: dict, mkt="us", last_exec: dict | None = None):
    """실제 저장소 데이터로 preflight 실행 — 신호·실집행 목표 파일을 임시로 바꿔치기 후 원상 복원.
    달력 신선도 검사는 벽시계에 의존하므로 테스트에선 끈다(PREFLIGHT_MAX_STALE_TDAYS)."""
    import os
    files = [ROOT / ("signals/target_weights.json" if mkt == "us" else "signals/kr_target_weights.json"),
             ROOT / "reports" / f"preflight_{mkt}.md", ROOT / "signals" / f"last_executed_target_{mkt}.json"]
    saved = {f: (f.read_text() if f.exists() else None) for f in files}
    try:
        files[0].write_text(json.dumps(tmp_sig))
        if last_exec is not None:
            files[2].write_text(json.dumps(last_exec))
        elif files[2].exists():
            files[2].unlink()
        env = dict(os.environ, PREFLIGHT_MAX_STALE_TDAYS="100000")
        r = subprocess.run([sys.executable, "pipeline/preflight.py", mkt], cwd=ROOT, capture_output=True, text=True, env=env)
        return r.returncode, r.stdout + r.stderr
    finally:
        for f, txt in saved.items():
            if txt is None:
                f.unlink(missing_ok=True)
            else:
                f.write_text(txt)


def test_preflight_blocks_expired_and_passes_valid(tmp_path, monkeypatch):
    valid = run_signals(tmp_path, monkeypatch, APPROVED_0925)
    rc, out = _preflight(valid)
    assert rc == 0, out
    expired = dict(valid, decision_state="expired", source="F1 원신호 (결정 만료) — 집행 정지(C-21)")
    rc, out = _preflight(expired)
    assert rc != 0 and "위원회 결정 상태 'expired'" in out


def test_defect_s_turnover_measured_against_last_executed_target(tmp_path, monkeypatch):
    """9/28 실증: 토요일 SKIP 런이 커밋한 신호와 비교해 0.0%가 보고됐다. 실집행 목표 대비면 교체분이 잡혀야 한다.
    커밋된 신호 파일에 의존하지 않도록 F1 신호(mr_scale 1.0)를 직접 산출해 사용한다."""
    from datetime import date
    cur = run_signals(tmp_path, monkeypatch, {**APPROVED_0925, "mr_scale": 1.0, "mr_policy": "liquidate"})
    mr_today = sorted(t for t, v in cur["weights"].items() if abs(v - 0.01) < 1e-9)
    assert len(mr_today) >= 3, "오늘 MR 신호가 비어 있으면 이 테스트는 의미 없음"
    executed = {k: v for k, v in cur["weights"].items() if k not in mr_today}
    for t in ["ZZZ1", "ZZZ2", "ZZZ3"] + mr_today[3:]:      # 신규 3종을 뺀 나머지는 동일 보유
        executed[t] = 0.01
    executed["BIL"] = round(1 - sum(v for k, v in executed.items() if k != "BIL"), 6)
    rc, out = _preflight(cur, last_exec={"date": str(date.today()), "weights": executed})
    assert rc == 0, out
    assert "기준: 마지막 실집행" in out
    turn = float(out.split("매수 회전율 ")[1].split("%")[0])
    assert abs(turn - 3.0) < 0.05, out    # 신규 3종 × 1% = 3pp (HEAD 비교였다면 0.0% 가능)


def test_stale_baseline_falls_back_to_head(tmp_path, monkeypatch):
    """리뷰 지적 4: 장기 정지 뒤 낡은 실집행 목표로 영구 차단되지 않도록 HEAD 로 대체."""
    cur = run_signals(tmp_path, monkeypatch, APPROVED_0925)
    rc, out = _preflight(cur, last_exec={"date": "2026-01-02", "weights": {"SPY": 1.0}})
    assert rc == 0 and "낡은 기준, HEAD 신호로 대체" in out, out
