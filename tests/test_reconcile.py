"""reconcile.py 테스트 — 결함 Q(실보유 대 목표 대사)."""
import importlib
import json
from datetime import date

import pytest


def setup(tmp_path, monkeypatch, mkt, log_lines, target, equity, cash, pos):
    R = importlib.reload(importlib.import_module("reconcile"))
    monkeypatch.setattr(R, "ROOT", tmp_path)
    monkeypatch.setattr(R, "SETTLE_WAIT", 0)
    monkeypatch.setattr(R, "has_keys", lambda m: True)
    monkeypatch.setattr(R, "fetch_us", lambda: (equity, cash, pos))
    monkeypatch.setattr(R, "fetch_kr", lambda: (equity, cash, pos))
    (tmp_path / "reports").mkdir(); (tmp_path / "signals").mkdir()
    name = "trade_log.md" if mkt == "us" else "kr_trade_log.md"
    head = "# 집행 로그" if mkt == "us" else "# KR 집행 로그"
    (tmp_path / "reports" / name).write_text("\n".join([f"{head} {date.today()}", *log_lines]) + "\n\n---\n\nold")
    (tmp_path / "signals" / f"last_executed_target_{mkt}.json").write_text(json.dumps({"weights": target}))
    monkeypatch.setattr("sys.argv", ["reconcile.py", mkt])
    return R


def test_clean_pass_and_reports_written(tmp_path, monkeypatch):
    target = {"SPY": 0.5, "BIL": 0.49, "AAPL": 0.01}
    pos = {"SPY": 50_000, "BIL": 49_000, "AAPL": 1_000}
    R = setup(tmp_path, monkeypatch, "us", ["- BUY AAPL $1,000"], target, 100_000, 0, pos)
    R.main()
    j = json.loads((tmp_path / "reports/positions_us.json").read_text())
    assert j["positions"]["SPY"] == 50_000 and j["problems"] == []
    assert "| AAPL | 1.00% |" in (tmp_path / "reports/reconcile_us.md").read_text()


def test_silent_drift_fails(tmp_path, monkeypatch):
    """목표 21% EEM 이 실보유 15% — 로그에 EEM 이 한 줄도 없으면 조용한 실패."""
    target = {"EEM": 0.21, "BIL": 0.79}
    pos = {"EEM": 15_000, "BIL": 79_000}
    R = setup(tmp_path, monkeypatch, "us", ["- 조정 없음"], target, 100_000, 6_000, pos)
    with pytest.raises(SystemExit) as e:
        R.main()
    assert "조용한 괴리: EEM" in str(e.value)


def test_logged_skip_is_explained(tmp_path, monkeypatch):
    target = {"EEM": 0.21, "BIL": 0.79}
    pos = {"EEM": 15_000, "BIL": 79_000}
    R = setup(tmp_path, monkeypatch, "us", ["- SKIP(현금부족) EEM buy"], target, 100_000, 6_000, pos)
    R.main()  # 사유가 기록돼 있으면 괴리는 설명됨 — PASS


def test_art4_on_actual_holdings(tmp_path, monkeypatch):
    target = {"AAPL": 0.10, "BIL": 0.90}
    pos = {"AAPL": 12_000, "BIL": 88_000}
    R = setup(tmp_path, monkeypatch, "us", ["- BUY AAPL $10,000"], target, 100_000, 0, pos)
    with pytest.raises(SystemExit) as e:
        R.main()
    assert "헌법 4조(실보유): AAPL 12.00%" in str(e.value)


def test_mr_eleven_names_held_with_hold_policy(tmp_path, monkeypatch):
    """9/28 재구성: MR 11종 보유·목표 0% — HOLD 로 기록돼 있으면 PASS, 기록 없으면 FAIL."""
    mr = ["ADI", "AIZ", "AMGN", "APD", "APO", "ARE", "AWK", "BEN", "BLK", "BNY", "CSX"]
    pos = {t: 1_000 for t in mr} | {"BIL": 35_000, "SPY": 18_000}
    target = {"BIL": 35_000 / 64_000, "SPY": 18_000 / 64_000}
    lines = [f"- HOLD(MR 동결 mr_policy=hold) {t} ≈$1,000 보유 유지" for t in mr]
    R = setup(tmp_path, monkeypatch, "us", lines, target, 64_000, 0, pos)
    R.main()
    R = setup(tmp_path / "b", monkeypatch, "us", lines[:-1], target, 64_000, 0, pos) if (tmp_path / "b").mkdir() is None else None
    with pytest.raises(SystemExit) as e:
        R.main()
    assert "조용한 괴리: CSX" in str(e.value)


def test_negative_cash_fails(tmp_path, monkeypatch):
    R = setup(tmp_path, monkeypatch, "us", ["- BUY SPY $100"], {"SPY": 1.0}, 100_000, -2.0, {"SPY": 100_002})
    with pytest.raises(SystemExit) as e:
        R.main()
    assert "헌법 1조" in str(e.value)


def test_kr_codes_and_halt_day(tmp_path, monkeypatch):
    target = {"153130": 0.83, "005930": 0.0055}
    pos = {"153130": 419_150_000, "005930": 2_780_000}
    R = setup(tmp_path, monkeypatch, "kr", ["- OK BUY 삼성전자(005930) 1주 약 60,000원"], target, 505_000_000, 1e6, pos)
    R.main()
    assert "| 005930 |" in (tmp_path / "reports/reconcile_kr.md").read_text()
    R2 = setup(tmp_path / "h", monkeypatch, "kr", ["- **HALT(위원회 결정 missing): ...**"], target, 1, 1, {}) \
        if (tmp_path / "h").mkdir() is None else None
    R2.main()  # 주문 없는 회차는 대사 생략
