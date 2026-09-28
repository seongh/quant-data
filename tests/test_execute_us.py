"""execute.py 회귀 테스트 — 가짜 브로커, 네트워크 없음.  실행: python -m pytest -q tests"""
import json

import pytest

from fakes import FakeAlpaca
from harness import load_us

MR_OLD = {"AES": 1003, "AMZN": 1001, "ABNB": 991, "ALL": 847, "AMP": 606}
CORE = {"SPY": 18000, "BIL": 25000}


def _weights(fake, new_mr=("ADI", "AMGN", "APD", "ARE", "CB", "ADM", "AAPL")):
    eq = fake.equity
    w = {t: v / eq for t, v in CORE.items()}
    w.update({t: 0.01 for t in new_mr})
    return w


def test_hprime1_sell_proceeds_are_waited_for(tmp_path, monkeypatch):
    """9/25 재현: 매도대금이 재조회 2회 뒤에 반영돼도 매수가 전부 체결돼야 한다 (현행 코드는 SKIP)."""
    fake = FakeAlpaca(3170, {**MR_OLD, **CORE}, settle_after=2)
    ex = load_us(tmp_path, monkeypatch, fake, _weights(fake))
    ex.main()
    log = ex.LOG.read_text()
    assert "SKIP(현금부족)" not in log, log
    assert "매도대금 반영: $4,448 / 접수 $4,448 (100%" in log
    assert sum(1 for o in fake.orders if o[0] == "buy") == 7
    assert fake.cash >= 0


def test_hprime1_timeout_falls_back_without_margin(tmp_path, monkeypatch):
    """반영이 끝내 안 되면 폴링 한도 후 보수적으로 진행 — 현금 음수(1조 위반)는 절대 없어야 한다."""
    fake = FakeAlpaca(3170, {**MR_OLD, **CORE}, settle_after=10_000)
    ex = load_us(tmp_path, monkeypatch, fake, _weights(fake))
    ex.main()
    log = ex.LOG.read_text()
    assert f"조회 {ex.SETTLE_POLLS}회" in log
    assert "SKIP(현금부족)" in log
    assert "FAIL buy" not in log
    assert fake.cash >= 0


def test_min_trade_drops_are_logged(tmp_path, monkeypatch):
    """R-55: $200 미만 조정도 로그 1행에 남아야 한다 (현행 코드는 무기록)."""
    fake = FakeAlpaca(10_000, {"SPY": 18_000, "EFA": 11_500}, settle_after=0)
    eq = fake.equity
    w = {"SPY": (18_000 + 150) / eq, "EFA": (11_500 - 90) / eq}
    w["BIL"] = 1 - sum(w.values())
    ex = load_us(tmp_path, monkeypatch, fake, w)
    ex.main()
    log = ex.LOG.read_text()
    assert "미달 생략(MIN_TRADE $200" in log and "SPY +150" in log and "EFA -90" in log


@pytest.mark.parametrize("val,should_run", [("0", False), ("false", False), ("", False), ("1", True)])
def test_execute_force_only_exact_1(tmp_path, monkeypatch, val, should_run):
    fake = FakeAlpaca(50_000, {}, settle_after=0)
    ex = load_us(tmp_path, monkeypatch, fake, {"BIL": 1.0}, env={"EXECUTE_FORCE": val})
    monkeypatch.setattr(ex, "market_open_now", lambda now=None: (False, "주말 (test)"))
    ex.main()
    assert bool(fake.orders) == should_run
    assert ("SKIP(장시간 가드)" in ex.LOG.read_text()) == (not should_run)


@pytest.mark.parametrize("state", ["expired", "missing", "invalid"])
def test_decision_not_valid_halts(tmp_path, monkeypatch, state):
    """C-21: 결정 만료·부재·형식오류면 주문 0건, 실집행 마커 미기록."""
    fake = FakeAlpaca(50_000, {**MR_OLD}, settle_after=0)
    ex = load_us(tmp_path, monkeypatch, fake, {"BIL": 1.0},
                 extra_sig={"decision_state": state, "source": f"F1 원신호 (결정 {state})"})
    ex.main()
    assert fake.orders == []
    assert f"HALT(위원회 결정 {state})" in ex.LOG.read_text()
    assert not ex.MARKER.exists()


def test_legacy_signal_without_state_still_executes(tmp_path, monkeypatch):
    """execute.py만 먼저 배포돼도(구버전 signals.py) 기존 동작 유지 — 독립 배포 가능성."""
    fake = FakeAlpaca(50_000, {}, settle_after=0)
    ex = load_us(tmp_path, monkeypatch, fake, {"BIL": 1.0})
    sig = json.loads(ex.SIGNALS.read_text()); sig.pop("decision_state")
    ex.SIGNALS.write_text(json.dumps(sig))
    ex.main()
    assert fake.orders


def test_marker_and_target_written_before_orders_and_log_survives_exception(tmp_path, monkeypatch):
    """결함 N: 주문 도중 예외가 나도 마커·집행목표·로그가 남아야 재발화 이중 집행을 막는다."""
    fake = FakeAlpaca(50_000, {}, settle_after=0)
    ex = load_us(tmp_path, monkeypatch, fake, {"SPY": 0.5, "BIL": 0.5})
    real = fake.api

    def boom(path, method="GET", body=None):
        if path == "/v2/orders" and body and body["symbol"] == "SPY":
            raise KeyboardInterrupt("runner killed mid-loop")
        return real(path, method, body)
    monkeypatch.setattr(ex, "api", boom)
    with pytest.raises(KeyboardInterrupt):
        ex.main()
    assert ex.MARKER.exists() and ex.LAST_TARGET.exists()
    assert "# 집행 로그" in ex.LOG.read_text()


def test_skip_log_not_duplicated_same_day(tmp_path, monkeypatch):
    """H′-5: 같은 날 같은 사유 SKIP은 로그에 1회만 적재."""
    fake = FakeAlpaca(50_000, {}, settle_after=0)
    ex = load_us(tmp_path, monkeypatch, fake, {"BIL": 1.0})
    monkeypatch.setattr(ex, "market_open_now", lambda now=None: (False, "휴장일 (test)"))
    for _ in range(4):
        ex.main()
    assert ex.LOG.read_text().count("SKIP(장시간 가드)") == 1


def test_hold_policy_keeps_mr_positions(tmp_path, monkeypatch):
    """승인안(mr_scale 0 / hold)이면 MR 기보유는 매도하지 않는다 — 9/25 승인안 시뮬레이션."""
    fake = FakeAlpaca(3170, {**MR_OLD, **CORE}, settle_after=0)
    eq = fake.equity
    w = {"SPY": 18_000 / eq, "BIL": 1 - 18_000 / eq}
    ex = load_us(tmp_path, monkeypatch, fake, w, extra_sig={"mr_scale": 0.0, "mr_policy": "hold"})
    ex.main()
    log = ex.LOG.read_text()
    assert all(f"HOLD(MR 동결 mr_policy=hold) {t}" in log for t in MR_OLD)
    assert not any(o[0] == "sell" and o[1] in MR_OLD for o in fake.orders)
    assert fake.cash >= 0


def test_broker_side_dedupe_blocks_second_run(tmp_path, monkeypatch):
    """리뷰 지적 3: 대기열 런이 옛 커밋(마커 이전)을 읽어도 브로커에 오늘 주문이 있으면 재집행하지 않는다."""
    fake = FakeAlpaca(50_000, {}, settle_after=0)
    fake.extra_orders = [{"id": "x", "symbol": "SPY", "side": "buy", "status": "filled"}]
    ex = load_us(tmp_path, monkeypatch, fake, {"SPY": 0.5, "BIL": 0.5})
    ex.main()
    assert fake.orders == []
    assert "SKIP(이중 집행 차단)" in ex.LOG.read_text() and ex.MARKER.exists()


def test_rejected_orders_today_do_not_block(tmp_path, monkeypatch):
    fake = FakeAlpaca(50_000, {}, settle_after=0)
    fake.extra_orders = [{"id": "x", "symbol": "SPY", "side": "buy", "status": "rejected"}]
    ex = load_us(tmp_path, monkeypatch, fake, {"SPY": 0.5, "BIL": 0.5})
    ex.main()
    assert fake.orders


def test_r28_min_guard_caps_budget_even_if_broker_overreports(tmp_path, monkeypatch):
    """R-28: 브로커 현금이 부풀려 보고돼도 예산은 (스냅샷+성공 매도)×95%를 넘지 않는다."""
    fake = FakeAlpaca(3170, {**MR_OLD, **CORE}, settle_after=0)
    real = fake.api

    def inflated(path, method="GET", body=None):
        r = real(path, method, body)
        if path == "/v2/account" and fake.account_calls > 1:
            r = dict(r, cash=str(float(r["cash"]) + 50_000))   # 가상의 과대 보고
        return r
    eq = fake.equity
    w = {"SPY": 18_000 / eq} | {t: 0.03 for t in ("ADI", "AMGN", "APD", "ARE", "CB", "ADM", "AAPL")}
    w["BIL"] = 1 - sum(w.values())          # 합계 1.0 (1조) — 부족분은 BIL 매도로 조달되지 않도록 BIL 은 축소만
    ex = load_us(tmp_path, monkeypatch, fake, w)
    monkeypatch.setattr(ex, "api", inflated)
    ex.main()
    bought = sum(o[2] for o in fake.orders if o[0] == "buy")
    sold = sum(o[2] for o in fake.orders if o[0] == "sell")
    need = 7 * 0.03 * eq
    assert need > (3170 + sold) * 0.95          # 가드가 실제로 구속하는 상황인지 확인
    assert bought <= (3170 + sold) * 0.95 + 1, (bought, sold)


def test_marker_exists_at_first_order(tmp_path, monkeypatch):
    """결함 N: 첫 주문이 브로커에 닿는 순간 이미 실집행 마커가 디스크에 있어야 한다."""
    fake = FakeAlpaca(50_000, {"AES": 1000}, settle_after=0)
    ex = load_us(tmp_path, monkeypatch, fake, {"SPY": 0.5, "BIL": 0.5})
    real, seen = fake.api, []

    def spy(path, method="GET", body=None):
        if method in ("POST", "DELETE"):
            seen.append(ex.MARKER.exists() and ex.LAST_TARGET.exists())
        return real(path, method, body)
    monkeypatch.setattr(ex, "api", spy)
    ex.main()
    assert seen and all(seen)
