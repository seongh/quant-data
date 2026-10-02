"""집행 후 대사(Reconciliation) — "조용한 실패" 봉쇄 (계층 3 인터록).

집행 직후 실행되어, 문제가 있으면 exit 1로 워크플로를 실패 처리 → 실패 메일 경보.
사용: python pipeline/reconcile.py us|kr
주의: 이 스크립트는 어떤 주문도 내지 않는다. 읽기와 판정만 한다.

검사:
1. 오늘자 집행 로그 존재 / FAIL 주문 0건
2. 계좌 현금(예수금) ≥ 0 (헌법 1조)
3. [2026-09-28 결함 Q] 실보유 대 목표 대사 — 이전 버전은 로컬 로그만 읽어 실제 보유를 한 번도 보지 않았다.
   a. 실보유 기준 개별종목(ETF 제외) 비중 ≤ 10% (헌법 4조를 '목표'가 아니라 '실보유'로 검사)
   b. 목표와 실보유 차이가 MIN_TRADE 이상인 종목이 오늘 로그에 한 줄도 없으면 FAIL
      (주문·SKIP·HOLD·미달생략 어느 것으로도 설명되지 않는 괴리 = 조용한 실패).
      execute 가 본 종목은 전부 로그에 한 번은 등장하므로(R-55 미달 생략 행 포함), 이 검사가 잡는 것은
      '집행기가 보지 못한 보유' — 포지션 조회 누락, 로그 유실(9/17 유형), 위원회 외 수동 매매(헌법 3조)다
   c. 결과를 reports/positions_{mkt}.json (기계용) · reports/reconcile_{mkt}.md (사람용)에 기록 →
      위원회가 Actions 로그·브로커 접속 없이 저장소만으로 실보유를 볼 수 있다
      (4조 분모, T7 단일자산 ±7%, 코어 주문 사전등록이 이 파일을 전제로 한다)
"""
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
LOGS = {"us": "reports/trade_log.md", "kr": "reports/kr_trade_log.md"}
SIGS = {"us": "signals/target_weights.json", "kr": "signals/kr_target_weights.json"}
ETFS = {
    "us": {"SPY", "QQQ", "IWM", "EFA", "EEM", "VNQ", "GLD", "DBC", "TLT", "IEF", "SHY", "TIP", "LQD", "HYG", "BIL",
           "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"},
    "kr": {"069500", "229200", "132030", "148070", "153130", "133690"},
}
MIN_TRADE = {"us": 200.0, "kr": 200_000.0}
MAX_STOCK_W = 0.10
SETTLE_WAIT = 30  # 시장가 체결·현금 반영 대기(초)
TZ = {"us": ZoneInfo("America/New_York"), "kr": ZoneInfo("Asia/Seoul")}
# 집행기 자신이 남기는 '주문 없는 회차' 표식만 인정 — 줄 머리 고정 (자유 텍스트의 "HALT" 오인 방지, 리뷰 지적 5)
NO_ORDER_RUN = re.compile(r"^- \*\*(HALT|SKIP\()", re.M)


def fetch_us():
    """(equity, cash, {symbol: market_value}) — Alpaca paper 읽기 전용."""
    key, sec = os.environ.get("ALPACA_KEY", ""), os.environ.get("ALPACA_SECRET", "")
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}

    def get(path):
        with urllib.request.urlopen(urllib.request.Request("https://paper-api.alpaca.markets" + path, headers=h),
                                    timeout=30) as r:
            return json.loads(r.read())
    acct = get("/v2/account")
    pos = {p["symbol"]: float(p["market_value"]) for p in get("/v2/positions")}
    return float(acct["equity"]), float(acct["cash"]), pos


def fetch_kr():
    import execute_kr as kr
    kr.get_token()
    pos, equity, cash = kr.get_balance()
    return equity, cash, pos


def has_keys(mkt):
    if mkt == "us":
        return bool(os.environ.get("ALPACA_KEY") and os.environ.get("ALPACA_SECRET"))
    return bool(os.environ.get("KIS_APP_KEY") and os.environ.get("KIS_APP_SECRET") and os.environ.get("KIS_ACCOUNT"))


def load_target(mkt):
    """집행기가 실제로 사용한 목표(last_executed_target_*) 우선, 없으면 신호 파일."""
    le = ROOT / "signals" / f"last_executed_target_{mkt}.json"
    for f in (le, ROOT / SIGS[mkt]):
        if f.exists():
            try:
                return json.loads(f.read_text())["weights"], f.name
            except Exception:
                continue
    return {}, "없음"


def mentioned(sym: str, block: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])", block) is not None


def audit_positions(mkt, equity, cash, pos, target, block, check_drift=True):
    """실보유 대 목표 대사. (문제 목록, 표 행 목록, 기계용 dict) 반환."""
    problems, rows = [], []
    etfs, min_trade = ETFS[mkt], MIN_TRADE[mkt]
    detail = {}
    for s in sorted(set(pos) | set(target)):
        mv = pos.get(s, 0.0)
        aw = mv / equity if equity > 0 else 0.0
        tw = float(target.get(s, 0.0))
        dev = mv - tw * equity
        logged = mentioned(s, block)
        detail[s] = {"target_w": round(tw, 6), "actual_w": round(aw, 6), "market_value": round(mv, 2),
                     "deviation": round(dev, 2), "in_log": logged}
        flag = ""
        if s not in etfs and aw > MAX_STOCK_W + 1e-6:
            problems.append(f"헌법 4조(실보유): {s} {aw:.2%} > 10%")
            flag = "🔴 4조"
        if check_drift and abs(dev) >= min_trade and not logged:
            problems.append(f"조용한 괴리: {s} 목표 {tw:.2%} vs 실보유 {aw:.2%} (편차 {dev:+,.0f}) — 오늘 로그에 사유 없음")
            flag = (flag + " 🔴 무기록").strip()
        rows.append(f"| {s} | {tw:.2%} | {aw:.2%} | {dev:+,.0f} | {'O' if logged else '-'} | {flag} |")
    return problems, rows, detail


def write_reports(mkt, equity, cash, pos, target, target_src, rows, detail, problems):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    unit = "$" if mkt == "us" else "원"
    (ROOT / "reports").mkdir(exist_ok=True)
    (ROOT / "reports" / f"positions_{mkt}.json").write_text(json.dumps({
        "asof": now, "equity": equity, "cash": cash, "target_source": target_src,
        "positions": {s: round(v, 2) for s, v in sorted(pos.items())}, "detail": detail,
        "problems": problems}, ensure_ascii=False, indent=1))
    invested = sum(pos.values())
    md = [f"# 실보유 대 목표 대사 ({mkt.upper()}) — {now}", "",
          f"- 평가 {equity:,.0f}{unit} / 현금 {cash:,.0f}{unit} / 투자 {invested:,.0f}{unit}",
          f"- 목표 출처: `{target_src}`",
          f"- 판정: {'**FAIL** — ' + ' / '.join(problems) if problems else 'PASS'}", "",
          "| 종목 | 목표 | 실보유 | 편차 | 로그 | 비고 |", "|---|---|---|---|---|---|", *rows, ""]
    (ROOT / "reports" / f"reconcile_{mkt}.md").write_text("\n".join(md))


def main():
    mkt = sys.argv[1] if len(sys.argv) > 1 else "us"
    today = f"{datetime.now(TZ[mkt]):%Y-%m-%d}"   # 시장 현지 날짜 (러너 UTC 와 어긋남 방지, 리뷰 지적 7)
    log_path = ROOT / LOGS[mkt]
    problems = []

    block = ""
    if not log_path.exists():
        problems.append("집행 로그 파일 없음")
    else:
        block = log_path.read_text().split("\n---\n")[0]
        header = block.splitlines()[0] if block.splitlines() else ""
        if today not in header:
            problems.append(f"오늘({today}) 집행 로그 없음 — preflight 정지일이면 reports/preflight_{mkt}.md 확인 (최신: {header[:40]})")
            block = ""
    fails = [l.strip() for l in block.splitlines() if l.strip().startswith("- FAIL")]
    if fails:
        problems.append(f"실패 주문 {len(fails)}건 — " + " / ".join(f[:70] for f in fails[:3]))
    no_order_run = bool(NO_ORDER_RUN.search(block)) or not block

    # 계좌 검사는 로그 상태와 무관하게 항상 수행 — HALT·SKIP·preflight 정지일에도 미결제 매수 정산으로
    # 예수금이 음수가 될 수 있다 (결함 H: 9/11 −35,969원). 리뷰 지적 6
    if not has_keys(mkt):
        print(f"[reconcile] {mkt.upper()} 브로커 키 없음 — 계좌 검사 생략")
    else:
        if not no_order_run:
            time.sleep(SETTLE_WAIT)
        equity, cash, pos = fetch_us() if mkt == "us" else fetch_kr()
        unit = "$" if mkt == "us" else "원"
        print(f"[reconcile] 평가 {equity:,.0f}{unit} / 현금 {cash:,.0f}{unit} / 보유 {len(pos)}종목")
        if cash < 0.0:  # 허용오차 없음 (게이트 B1: -$2도 1조 위반, 2026-09-08)
            problems.append(f"현금 음수 {cash:,.2f}{unit} — 헌법 1조(무레버리지) 위반 상태")
        target, target_src = load_target(mkt)
        # 조용한 괴리 검사는 주문이 실제로 나간 회차에만 (주문 없는 회차엔 로그에 종목이 없는 게 정상)
        pos_problems, rows, detail = audit_positions(mkt, equity, cash, pos, target, block,
                                                     check_drift=not no_order_run)
        problems += pos_problems
        write_reports(mkt, equity, cash, pos, target, target_src, rows, detail, pos_problems)
        print(f"[reconcile] 실보유 대사 기록 → reports/reconcile_{mkt}.md, reports/positions_{mkt}.json")

    if problems:
        sys.exit("[RECONCILE FAIL] " + " | ".join(problems))
    print(f"[reconcile] PASS — {mkt} 대사 이상 없음" + (" (주문 없는 회차)" if no_order_run else ""))


if __name__ == "__main__":
    main()
