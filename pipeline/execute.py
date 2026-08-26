"""Alpaca 모의계좌(paper) 집행 — 목표 비중으로 리밸런싱.

우선순위:
1. DECISION_URL(위원회 결정 JSON, 환경변수)이 설정되어 있고 오늘자면 그 비중 사용
2. 아니면 signals/target_weights.json (검증된 F1 시스템) 사용 — fail-safe 기본값

헌법 강제 (코드 레벨):
- 비중 합 > 100% 거부 (1조: 무레버리지) / 개별 종목(ETF 제외) > 10% 거부 (4조)
- 계좌 고점 대비 -15% 이하면 신규 매수 전량 중단, 매도만 허용 (5조 서킷브레이커)
- 매수는 반드시 현금 범위 내 (Alpaca paper도 마진 계좌지만 코드로 현금 초과 매수 차단)
"""
import json
import os
import sys
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIGNALS = ROOT / "signals" / "target_weights.json"
STATE = ROOT / "signals" / "account_state.json"  # 고점 기록 (서킷브레이커용)
LOG = ROOT / "reports" / "trade_log.md"

BASE = "https://paper-api.alpaca.markets"
KEY = os.environ.get("ALPACA_KEY", "")
SECRET = os.environ.get("ALPACA_SECRET", "")
ETFS = {"SPY","QQQ","IWM","EFA","EEM","VNQ","GLD","DBC","TLT","IEF","SHY","TIP","LQD","HYG","BIL",
        "XLB","XLE","XLF","XLI","XLK","XLP","XLU","XLV","XLY"}
MIN_TRADE_USD = 200      # 이보다 작은 조정은 생략 (수수료·잡음 방지)
MAX_STOCK_W = 0.10
DD_LIMIT = 0.15


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
    return sig["weights"], f"F1 시스템 신호 ({sig['date']})"


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
    weights, source = load_targets()
    validate(weights)

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
        orders.append((s, side, abs(diff)))
    orders.sort(key=lambda o: 0 if o[1] == "sell" else 1)  # 매도 먼저

    budget = cash
    for s, side, usd in orders:
        if side == "buy":
            usd = min(usd, budget)          # 현금 범위 내 (헌법 1조)
            if usd < MIN_TRADE_USD:
                lines.append(f"- SKIP(현금부족) {s} buy")
                continue
        try:
            api("/v2/orders", "POST", {"symbol": s, "notional": round(usd, 2),
                                       "side": side, "type": "market", "time_in_force": "day"})
            lines.append(f"- {side.upper()} {s} ${usd:,.0f}")
            # 주문이 성공했을 때만 예산 반영 — 매도 실패분을 현금으로 오인해
            # 마진(1조 위반)이 발생하는 것을 차단 (2026-08-26 결함 B 수정)
            budget = budget - usd if side == "buy" else budget + usd
        except Exception as e:
            lines.append(f"- FAIL {side} {s}: {str(e)[:120]}")

    if len(orders) == 0:
        lines.append("- 조정 필요 없음 (목표와 현재 일치)")
    LOG.parent.mkdir(exist_ok=True)
    prev = LOG.read_text() if LOG.exists() else ""
    LOG.write_text("\n".join(lines) + "\n\n---\n\n" + prev)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
