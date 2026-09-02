"""Alpaca 모의계좌(paper) 집행 — 목표 비중으로 리밸런싱.

우선순위:
1. DECISION_URL(위원회 결정 JSON, 환경변수)이 설정되어 있고 오늘자면 그 비중 사용
2. 아니면 signals/target_weights.json (검증된 F1 시스템) 사용 — fail-safe 기본값

헌법 강제 (코드 레벨):
- 비중 합 > 100% 거부 (1조: 무레버리지) / 개별 종목(ETF 제외) > 10% 거부 (4조)
- 계좌 고점 대비 -15% 이하면 신규 매수 전량 중단, 매도만 허용 (5조 서킷브레이커)
- 매수는 반드시 현금 범위 내 (Alpaca paper도 마진 계좌지만 코드로 현금 초과 매수 차단)
- 매수 직전 브로커 현금을 다시 조회하고 95%만 사용 (결함 E, 2026-09-02) / 매도 FAIL 시 같은 회차 매수 중단
- decisions/HALT 파일이 있으면 주문 없이 종료 (리스크 거부권 관철 스위치, 2026-09-01 조건 5)
- 뉴욕 정규장(09:30~15:45 ET, 평일) 밖에서 발화하면 주문 없이 종료 (장시간 가드 — 조용한 큐잉·체결일 괴리 방지)
"""
import json
import os
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
SIGNALS = ROOT / "signals" / "target_weights.json"
STATE = ROOT / "signals" / "account_state.json"  # 고점 기록 (서킷브레이커용)
LOG = ROOT / "reports" / "trade_log.md"
HALT = ROOT / "decisions" / "HALT"           # 존재하면 집행 중단 (내용은 사유 메모)

BASE = "https://paper-api.alpaca.markets"
KEY = os.environ.get("ALPACA_KEY", "")
SECRET = os.environ.get("ALPACA_SECRET", "")
ETFS = {"SPY","QQQ","IWM","EFA","EEM","VNQ","GLD","DBC","TLT","IEF","SHY","TIP","LQD","HYG","BIL",
        "XLB","XLE","XLF","XLI","XLK","XLP","XLU","XLV","XLY"}
MIN_TRADE_USD = 200      # 이보다 작은 조정은 생략 (수수료·잡음 방지)
MAX_STOCK_W = 0.10
DD_LIMIT = 0.15
CASH_BUFFER = 0.95       # 매수에 쓰는 현금 비율 — 체결가 변동·수수료 여유 (결함 E)
NY = ZoneInfo("America/New_York")


def market_open_now(now=None) -> tuple[bool, str]:
    """뉴욕 정규장(평일 09:30~15:45 ET) 여부. 장외 발화(사고 #7 지연)는 주문 없이 종료."""
    now = now or datetime.now(NY)
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return False, f"주말 (NY {now:%Y-%m-%d %H:%M})"
    if not (9 * 60 + 30 <= hm <= 15 * 60 + 45):  # 15:45 ET = 04:45 KST 실효 (8/28 결의)
        return False, f"정규장 외 (NY {now:%Y-%m-%d %H:%M})"
    return True, f"NY {now:%Y-%m-%d %H:%M}"


def api(path, method="GET", body=None):
    req = urllib.request.Request(BASE + path, method=method,
                                 headers={"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET,
                                          "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def load_targets() -> tuple[dict, str]:
    url = os.environ.get("DECISION_URL", "").strip()
    if url:
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                dec = json.loads(r.read())
            if dec.get("date") == str(date.today()) and "weights" in dec:
                return dec["weights"], f"위원회 결정 ({dec.get('meeting_id','?')})"
            print(f"[info] 위원회 결정이 오늘자 아님({dec.get('date')}) → 시스템 기본값 사용")
        except Exception as e:
            print(f"[warn] 위원회 결정 조회 실패: {e} → 시스템 기본값 사용")
    sig = json.loads(SIGNALS.read_text())
    # 신호 파일이 스스로 소스를 밝힘 ("F1 원신호" / "위원회 결정 오버레이 (...)") — 하드코딩 금지
    # (2026-09-01 위원회 발견: 라벨 하드코딩으로 8/28 이후 소스 라인 전부 오기)
    label = sig.get("source") or "F1 시스템 신호"
    return sig["weights"], f"{label} ({sig['date']})"


def mr_hold_policy() -> bool:
    """signals 파일의 mr_policy == 'hold' 이면 목표 0%인 개별종목(MR 슬리브)을 매도하지 않고 보유 유지."""
    try:
        sig = json.loads(SIGNALS.read_text())
        return float(sig.get("mr_scale", 1.0)) < 1.0 and str(sig.get("mr_policy", "")).lower() == "hold"
    except Exception:
        return False


def validate(weights: dict) -> None:
    total = sum(weights.values())
    if total > 1.0 + 1e-6:
        sys.exit(f"헌법 1조 위반: 비중 합 {total:.4f} > 1 — 집행 거부")
    for t, w in weights.items():
        if t not in ETFS and w > MAX_STOCK_W + 1e-6:
            sys.exit(f"헌법 4조 위반: {t} {w:.1%} > 10% — 집행 거부")


def main():
    if not KEY or not SECRET:
        sys.exit("ALPACA_KEY/ALPACA_SECRET 미설정 — GitHub Secrets 확인")
    if HALT.exists():
        reason = HALT.read_text().strip()[:200]
        write_log([f"# 집행 로그 {date.today()}", f"- **HALT: decisions/HALT 존재 → 주문 없이 종료** ({reason or '사유 미기재'})"])
        return
    is_open, when = market_open_now()
    if not is_open and not os.environ.get("EXECUTE_FORCE"):
        write_log([f"# 집행 로그 {date.today()}", f"- **SKIP(장시간 가드): {when} — 정규장 외 발화, 주문 없이 종료** (수동 강제: EXECUTE_FORCE=1)"])
        return
    weights, source = load_targets()
    validate(weights)
    hold_mr = mr_hold_policy()

    acct = api("/v2/account")
    equity = float(acct["equity"])
    cash = float(acct["cash"])

    # 서킷브레이커: 고점 갱신 및 낙폭 점검
    state = json.loads(STATE.read_text()) if STATE.exists() else {"peak": equity}
    peak = max(state.get("peak", equity), equity)
    dd = equity / peak - 1
    halt_buys = dd <= -DD_LIMIT
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"peak": peak, "last_equity": equity, "dd": round(dd, 4)}))

    positions = {p["symbol"]: float(p["market_value"]) for p in api("/v2/positions")}
    lines = [f"# 집행 로그 {date.today()}", f"- 소스: {source}", f"- 계좌: ${equity:,.0f} (현금 ${cash:,.0f}, 고점대비 {dd:.1%})"]
    if halt_buys:
        lines.append(f"- **서킷브레이커 발동 (낙폭 {dd:.1%} ≤ -15%): 신규 매수 전량 중단, 매도만 집행. 재개는 Jamie 승인 필요 (헌법 5조)**")

    # 목표 금액 vs 현재 → 주문 목록 (매도 먼저 → 현금 확보 후 매수)
    orders = []
    all_syms = set(weights) | set(positions)
    for s in sorted(all_syms):
        target_usd = equity * weights.get(s, 0.0)
        cur = positions.get(s, 0.0)
        diff = target_usd - cur
        if abs(diff) < MIN_TRADE_USD:
            continue
        side = "buy" if diff > 0 else "sell"
        if side == "buy" and halt_buys:
            lines.append(f"- SKIP(서킷브레이커) {s} buy ${diff:,.0f}")
            continue
        if side == "sell" and hold_mr and s not in ETFS and weights.get(s, 0.0) == 0.0:
            lines.append(f"- HOLD(MR 동결 mr_policy=hold) {s} ≈${cur:,.0f} 보유 유지")
            continue
        orders.append((s, side, abs(diff)))
    orders.sort(key=lambda o: 0 if o[1] == "sell" else 1)  # 매도 먼저

    budget = cash
    sell_failed = False
    buys_started = False
    for s, side, usd in orders:
        if side == "buy":
            if sell_failed:
                lines.append(f"- SKIP(매도 FAIL 발생 → 이번 회차 매수 중단) {s} buy")
                continue
            if not buys_started:
                # 결함 E (2026-08-27 등록 → 09-02 수정): 매도 주문 접수 후 브로커 현금을 다시 조회해
                # 실제 정산 가능 현금의 95%만 예산으로 사용. 스냅샷 현금 + 매도 추정액으로
                # 매수하면 체결가 차이로 현금이 음수(1조 위반)가 되는 것을 차단.
                try:
                    live_cash = float(api("/v2/account")["cash"])
                except Exception as e:
                    live_cash = min(budget, cash)
                    lines.append(f"- WARN 현금 재조회 실패({str(e)[:60]}) → 보수적 예산 ${live_cash:,.0f}")
                budget = max(0.0, min(budget, live_cash)) * CASH_BUFFER
                lines.append(f"- 매수 예산: 재조회 현금 ${live_cash:,.0f} × {CASH_BUFFER:.0%} = ${budget:,.0f}")
                buys_started = True
            usd = min(usd, budget)          # 현금 범위 내 (헌법 1조)
            if usd < MIN_TRADE_USD:
                lines.append(f"- SKIP(현금부족) {s} buy")
                continue
        try:
            if side == "sell" and weights.get(s, 0.0) == 0.0:
                # 전량 청산은 금액(notional) 지정 대신 포지션 청산 API 사용 —
                # 스냅샷 이후 가격 하락 시 매도액 > 보유액이 되어 공매도 시도로
                # 간주(403)되는 것을 차단 (2026-08-26 결함 D 수정)
                api(f"/v2/positions/{s}", "DELETE")
                usd = positions.get(s, usd)
                lines.append(f"- SELL {s} 전량 ≈${usd:,.0f}")
            else:
                if side == "sell":
                    usd = min(usd, positions.get(s, usd) * 0.98)  # 잔량 초과 매도 방지
                api("/v2/orders", "POST", {"symbol": s, "notional": round(usd, 2),
                                           "side": side, "type": "market", "time_in_force": "day"})
                lines.append(f"- {side.upper()} {s} ${usd:,.0f}")
            # 주문이 성공했을 때만 예산 반영 — 매도 실패분을 현금으로 오인해
            # 마진(1조 위반)이 발생하는 것을 차단 (2026-08-26 결함 B 수정)
            budget = budget - usd if side == "buy" else budget + usd
        except Exception as e:
            lines.append(f"- FAIL {side} {s}: {str(e)[:120]}")
            if side == "sell":
                sell_failed = True   # 매도 실패 시 같은 회차 매수 전면 중단 (현금 오인 방지)

    if len(orders) == 0:
        lines.append("- 조정 필요 없음 (목표와 현재 일치)")
    write_log(lines)


def write_log(lines):
    LOG.parent.mkdir(exist_ok=True)
    prev = LOG.read_text() if LOG.exists() else ""
    LOG.write_text("\n".join(lines) + "\n\n---\n\n" + prev)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
