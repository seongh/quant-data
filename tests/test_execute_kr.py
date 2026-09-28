"""execute_kr.py 회귀 테스트 — 가짜 KIS, 네트워크 없음."""
import json

import pytest

from fakes import FakeKIS
from harness import load_kr

PRICES = {"153130": 113_200, "132030": 23_840, "005930": 60_000, "000660": 1_840_000,
          "066570": 213_000, "402340": 1_045_000, "069500": 106_695}


def _case_0928(settle_after):
    """9/28 재현: 스냅샷 예수금 13.28M, 주문가능 7.39M, 단기채권 19.58M 매도 후 매수 다수."""
    pos = {"153130": 419_000_000, "132030": 17_000_000}
    fake = FakeKIS(13_284_986, pos, PRICES, settle_after=settle_after)
    fake.ord_cash = 7_394_403
    eq = fake.equity()
    w = {"153130": (419_000_000 - 19_584_465) / eq, "132030": 24_000_000 / eq,
         "005930": 0.006, "000660": 0.0045, "066570": 0.0075, "402340": 0.0043, "069500": 0.0127}
    return fake, w


def test_xa_sell_proceeds_are_waited_for(tmp_path, monkeypatch):
    fake, w = _case_0928(settle_after=3)
    ex = load_kr(tmp_path, monkeypatch, fake, w)
    ex.main()
    log = ex.LOG.read_text()
    assert "매수가능조회(매도 전)" in log and "매수가능조회(매도 후)" in log      # R-54
    assert "SKIP(현금/수량부족)" not in log, log                                 # 9/28 은 16건 SKIP
    buys = [o for o in fake.orders if o[0] == "buy"]
    assert len(buys) == 6
    assert "→ 잔여예산" in log                                                   # R-53


def test_xa_never_uses_max_buy_amt_and_never_margins(tmp_path, monkeypatch):
    """주문가능현금이 끝내 안 늘어나도(가설: T+2) 최대매수(미수 포함)로 예산을 잡지 않는다 — 1조."""
    fake, w = _case_0928(settle_after=10_000)
    ex = load_kr(tmp_path, monkeypatch, fake, w)
    ex.main()
    log = ex.LOG.read_text()
    assert f"조회 {ex.SETTLE_POLLS}회" in log
    assert "FAIL BUY" not in log                     # 가짜 KIS 는 주문가능 초과 매수를 거부 → FAIL 이 없어야 함
    spent = sum(o[3] for o in fake.orders if o[0] == "buy")
    assert spent <= 7_394_403 * 0.95 + 1


def test_decision_not_valid_halts_kr(tmp_path, monkeypatch):
    fake, w = _case_0928(settle_after=0)
    ex = load_kr(tmp_path, monkeypatch, fake, w,
                 extra_sig={"decision_state": "missing", "source": "K조합 원신호 (approved_kr.json 없음)"})
    ex.main()
    assert fake.orders == [] and not ex.MARKER.exists()
    assert "HALT(위원회 결정 missing)" in ex.LOG.read_text()


def test_legacy_kr_signal_still_executes(tmp_path, monkeypatch):
    fake, w = _case_0928(settle_after=0)
    ex = load_kr(tmp_path, monkeypatch, fake, w)
    sig = json.loads(ex.SIGNALS.read_text()); sig.pop("decision_state"); sig.pop("source")
    ex.SIGNALS.write_text(json.dumps(sig))
    ex.main()
    assert fake.orders


def test_holiday_skip_logged_once(tmp_path, monkeypatch):
    """H′-5: 추석 휴장 4중 발화·4중 적재(9/24·9/25) 재발 방지. 실집행 마커는 건드리지 않음."""
    fake, w = _case_0928(settle_after=0)
    ex = load_kr(tmp_path, monkeypatch, fake, w)
    monkeypatch.setattr(ex, "market_open_now", lambda now=None: (False, "휴장일 (test)"))
    for _ in range(4):
        ex.main()
    assert ex.LOG.read_text().count("SKIP(장시간 가드)") == 1
    assert not ex.MARKER.exists()


@pytest.mark.parametrize("val,should_run", [("0", False), ("1", True)])
def test_execute_force_exact_kr(tmp_path, monkeypatch, val, should_run):
    fake, w = _case_0928(settle_after=0)
    ex = load_kr(tmp_path, monkeypatch, fake, w, env={"EXECUTE_FORCE": val})
    monkeypatch.setattr(ex, "market_open_now", lambda now=None: (False, "정규장 외 (test)"))
    ex.main()
    assert bool(fake.orders) == should_run


def test_kr_poll_failure_never_falls_back_to_dnca(tmp_path, monkeypatch):
    """리뷰 지적 1: 매도 후 조회가 전부 실패해도 스냅샷 예수금(13.28M, D+2 기준)으로 폴백하지 않는다.
    폴백은 매도 전 주문가능현금(7.39M) — 9/28 수치로 옛 폴백은 −5,198,857원 미수를 만들었다."""
    fake, w = _case_0928(settle_after=0)
    real = fake.get_buying_power
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("초당 거래건수 초과")
        return real()
    ex = load_kr(tmp_path, monkeypatch, fake, w)
    monkeypatch.setattr(ex, "get_buying_power", flaky)
    ex.main()
    log = ex.LOG.read_text()
    spent = sum(o[3] for o in fake.orders if o[0] == "buy")
    assert "FAIL BUY" not in log and spent <= 7_394_403 * 0.95 + 1, (spent, log)
    assert "매도 전 주문가능현금 기준 보수적 예산" in log


def test_kr_intermittent_poll_error_is_retried(tmp_path, monkeypatch):
    fake, w = _case_0928(settle_after=2)
    real = fake.get_buying_power
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise RuntimeError("HTTP 500")
        return real()
    ex = load_kr(tmp_path, monkeypatch, fake, w)
    monkeypatch.setattr(ex, "get_buying_power", flaky)
    ex.main()
    assert "SKIP(현금/수량부족)" not in ex.LOG.read_text()


def test_kr_budget_capped_by_snapshot_plus_sold(tmp_path, monkeypatch):
    """R-28: 주문가능현금이 부풀려져도(예: 미수 포함 값이 섞임) 예산은 (예수금+매도)×95% 이하."""
    fake, w = _case_0928(settle_after=0)
    real = fake.get_buying_power

    def inflated():
        bp = real()
        fake.ord_cash = max(fake.ord_cash, 500_000_000)   # 브로커 측도 허용한다고 가정
        return dict(bp, ord_psbl_cash=500_000_000)
    w = dict(w, **{"005930": 0.05, "000660": 0.05, "066570": 0.05})
    w["153130"] = 1 - sum(v for k, v in w.items() if k != "153130")   # 합계 1.0 (1조)
    ex = load_kr(tmp_path, monkeypatch, fake, w)
    monkeypatch.setattr(ex, "get_buying_power", inflated)
    ex.main()
    spent = sum(o[3] for o in fake.orders if o[0] == "buy")
    sold = sum(o[3] for o in fake.orders if o[0] == "sell")
    assert spent <= (13_284_986 + sold) * 0.95 + 1, (spent, sold)
