"""한국투자증권(KIS) 모의투자 집행 — signals/kr_target_weights.json 대로 리밸런싱.

- 도메인: 모의투자 전용 (openapivts) — 실전 도메인은 코드에 존재하지 않음 (안전장치)
- 헌법 강제: 비중합≤100%, 개별종목(ETF 제외)≤10%, 서킷브레이커 -15% 시 매수 중단
- 주문: 시장가, 정수 주량 (호가 조회 후 수량 계산), 매도 먼저 → 현금 확보 후 매수
Secrets: KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT (모의계좌 8자리)

2026-09-08 패치 (13 A + 결함 G + 결함 E KR 이식):
- 장시간 가드: KRX 정규장(평일 09:00~15:20 KST, 휴장일 제외) 밖 발화는 주문 없이 종료 (수동 강제: EXECUTE_FORCE=1)
- 결함 G: 매도 예산은 주문 성공 확인 후에만 가산 / 매도 FAIL 시 같은 회차 매수 전면 중단 (US 결함 B 이식)
- 결함 E: 매수 직전 브로커 현금 재조회 × 95% 예산 (US 이식)
- 실제 주문 회차마다 signals/last_executed_kr.txt 에 KST 날짜 기록 (워크플로 멱등 가드 입력 — 9/3 이중 집행 재발 방지)
- decisions/HALT_KR 존재 시 주문 없이 종료 (US HALT의 KR판)

2026-09-28 패치 (위원회 배포 대기열):
- R-33′/C-21: 신호의 decision_state 가 valid 가 아니면(approved_kr.json 만료·부재·오류) 주문 없이 종료.
  키가 없는 구버전 신호는 기존 동작(독립 배포 가능)
- 결함 X-a: 매도 전 매수가능조회를 기준값으로 잡고, 매도 후 주문가능현금이 매도액의 90%만큼 늘 때까지
  폴링(최대 SETTLE_POLLS회). 9/28 실측: 1.5초 대기 1회로는 매도 2,283만원이 전혀 반영되지 않아
  매수 16건 SKIP. 필드 의미론과 무관하게 '매도로 늘어난 몫'만 측정하므로 T+2·상수항 가설과 독립적
- 1조 이중방어 유지: 예산 = min(스냅샷+성공매도, 주문가능현금) × 95%.
  ⚠️ 최대매수(max_buy_amt)는 미수 포함 금액(9/28 실측 주문가능현금의 8.93배) — 절대 예산원으로 쓰지 않는다
- R-53: 매수 건마다 잔여 예산을 로그에 기록 (X-a/X-b 원인 분리용)
- R-54: 매도 전·후 매수가능조회 4개 필드를 모두 로그에 기록
- R-55 (6조): MIN_TRADE 미달 생략 종목 로그 1행
- 결함 N: 마커·집행 목표를 첫 주문 전에 기록, 로그는 finally
- H′-5: 같은 날 같은 사유 SKIP 로그 1회만 적재 (추석 휴장 4중 적재 재발 방지)
- EXECUTE_FORCE 는 정확히 "1" 일 때만 장시간 가드 우회
"""
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from preflight import HOLIDAYS  # 휴장일 단일 소스

ROOT = Path(__file__).resolve().parent.parent
SIGNALS = ROOT / "signals" / "kr_target_weights.json"
STATE = ROOT / "signals" / "kr_account_state.json"
LOG = ROOT / "reports" / "kr_trade_log.md"
HALT = ROOT / "decisions" / "HALT_KR"
MARKER = ROOT / "signals" / "last_executed_kr.txt"
LAST_TARGET = ROOT / "signals" / "last_executed_target_kr.json"  # preflight 회전율 기준 (결함 S)
SKIP_MARKER = ROOT / "signals" / "last_skipped_kr.txt"  # SKIP 로그 중복 방지 (H′-5) — 실집행 마커와 분리
KST = ZoneInfo("Asia/Seoul")
CASH_BUFFER = 0.95
SETTLE_POLLS = 20        # 매도대금 반영 대기: 최대 폴링 횟수 (결함 X-a)
SETTLE_WAIT = 2.0        # 폴링 간격(초) — 모의투자 호출 한도 고려
SETTLE_RATIO = 0.90

BASE = "https://openapivts.koreainvestment.com:29443"  # 모의투자 전용
KEY = os.environ.get("KIS_APP_KEY", "")
SECRET = os.environ.get("KIS_APP_SECRET", "")
CANO = os.environ.get("KIS_ACCOUNT", "")  # 8자리
PRDT = "01"
ETFS = {"069500", "229200", "132030", "148070", "153130", "133690"}
MIN_TRADE_KRW = 200_000
DD_LIMIT = 0.15
_token = None


def api(path, tr_id, method="GET", params=None, body=None):
    global _token
    url = BASE + path
    if method == "GET" and params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    headers = {"content-type": "application/json; charset=utf-8",
               "appkey": KEY, "appsecret": SECRET, "tr_id": tr_id}
    if _token:
        headers["authorization"] = f"Bearer {_token}"
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body else None)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get_token():
    global _token
    r = api("/oauth2/tokenP", "", "POST",
            body={"grant_type": "client_credentials", "appkey": KEY, "appsecret": SECRET})
    _token = r["access_token"]


def get_balance():
    p = {"CANO": CANO, "ACNT_PRDT_CD": PRDT, "AFHR_FLPR_YN": "N", "OFL_YN": "",
         "INQR_DVSN": "02", "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
         "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
         "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""}
    r = api("/uapi/domestic-stock/v1/trading/inquire-balance", "VTTC8434R", params=p)
    if r.get("rt_cd") != "0":
        sys.exit(f"잔고 조회 실패: {r.get('msg1')}")
    positions = {row["pdno"]: float(row["evlu_amt"]) for row in r.get("output1", [])
                 if float(row.get("hldg_qty", 0)) > 0}
    o2 = r["output2"][0]
    return positions, float(o2["tot_evlu_amt"]), float(o2["dnca_tot_amt"])


def get_buying_power():
    """KIS 매수가능조회 — 당일 매도대금 재사용분을 포함한 '주문가능현금'.
    잔고조회의 dnca_tot_amt(예수금)는 D+2 정산 기준이라 매도 당일에는 매도대금이 잡히지 않아
    매수 예산이 0으로 계산되는 결함(E-KR, 2026-09-11 실증: 2,065만원 매도 후 매수 전량 SKIP)을 수정."""
    p = {"CANO": CANO, "ACNT_PRDT_CD": PRDT, "PDNO": "069500", "ORD_UNPR": "0",
         "ORD_DVSN": "01", "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    r = api("/uapi/domestic-stock/v1/trading/inquire-psbl-order", "VTTC8908R", params=p)
    if r.get("rt_cd") != "0":
        raise RuntimeError(f"매수가능조회 실패: {r.get('msg1')}")
    o = r["output"]
    return {k: float(o.get(k, 0) or 0) for k in ("ord_psbl_cash", "ruse_psbl_amt", "nrcvb_buy_amt", "max_buy_amt")}


def get_price(code):
    r = api("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    return float(r["output"]["stck_prpr"])


def order(code, side, qty):
    tr = "VTTC0802U" if side == "buy" else "VTTC0801U"  # 모의 매수/매도
    body = {"CANO": CANO, "ACNT_PRDT_CD": PRDT, "PDNO": code,
            "ORD_DVSN": "01", "ORD_QTY": str(int(qty)), "ORD_UNPR": "0"}  # 시장가
    r = api("/uapi/domestic-stock/v1/trading/order-cash", tr, "POST", body=body)
    return r.get("rt_cd") == "0", r.get("msg1", "")


def market_open_now(now=None) -> tuple[bool, str]:
    """KRX 정규장(평일 09:00~15:20 KST, 휴장일 제외) 여부. 15:20 이후는 마감 동시호가라 신규 시장가 제외."""
    now = now or datetime.now(KST)
    hm = now.hour * 60 + now.minute
    if now.weekday() >= 5:
        return False, f"주말 (KST {now:%Y-%m-%d %H:%M})"
    if f"{now:%Y-%m-%d}" in HOLIDAYS["kr"]:
        return False, f"휴장일 (KST {now:%Y-%m-%d %H:%M})"
    if not (9 * 60 <= hm <= 15 * 60 + 20):
        return False, f"정규장 외 (KST {now:%Y-%m-%d %H:%M})"
    return True, f"KST {now:%Y-%m-%d %H:%M}"


def write_log(lines):
    LOG.parent.mkdir(exist_ok=True)
    prev = LOG.read_text() if LOG.exists() else ""
    LOG.write_text("\n".join(lines) + "\n\n---\n\n" + prev)
    print("\n".join(lines))


def skip_once(reason_key: str, lines: list[str]) -> None:
    """SKIP 로그를 하루·사유당 1회만 적재 (H′-5). 실집행 마커(MARKER)는 건드리지 않는다 — 결함 O 방어 유지."""
    stamp = f"{datetime.now(KST):%Y-%m-%d} {reason_key}"
    prev = SKIP_MARKER.read_text().strip() if SKIP_MARKER.exists() else ""
    if prev == stamp:
        print("\n".join(lines) + "\n(같은 날 같은 사유 SKIP 로그는 이미 기록됨 — 중복 적재 생략)")
        return
    SKIP_MARKER.parent.mkdir(exist_ok=True)
    SKIP_MARKER.write_text(stamp + "\n")
    write_log(lines)


def fmt_bp(bp: dict) -> str:
    return (f"주문가능현금 {bp['ord_psbl_cash']:,.0f} / 재사용가능 {bp['ruse_psbl_amt']:,.0f}"
            f" / 미수없는매수 {bp['nrcvb_buy_amt']:,.0f} / 최대매수 {bp['max_buy_amt']:,.0f}원")


def main():
    if not (KEY and SECRET and CANO):
        sys.exit("KIS_APP_KEY/KIS_APP_SECRET/KIS_ACCOUNT 미설정 — GitHub Secrets 확인")
    today_kst = f"{datetime.now(KST):%Y-%m-%d}"
    if HALT.exists():
        reason = HALT.read_text().strip()[:200]
        skip_once("HALT", [f"# KR 집행 로그 {today_kst}", f"- **HALT: decisions/HALT_KR 존재 → 주문 없이 종료** ({reason or '사유 미기재'})"])
        return
    is_open, when = market_open_now()
    if not is_open and os.environ.get("EXECUTE_FORCE", "").strip() != "1":
        skip_once("MARKET_CLOSED", [f"# KR 집행 로그 {today_kst}",
                   f"- **SKIP(장시간 가드): {when} — 정규장 외 발화, 주문 없이 종료** (수동 강제는 EXECUTE_FORCE=1 일 때만)"])
        return
    sig = json.loads(SIGNALS.read_text())
    dstate = str(sig.get("decision_state", "legacy"))
    if dstate not in ("valid", "legacy"):
        skip_once(f"DECISION_{dstate}", [f"# KR 집행 로그 {today_kst}",
                  f"- **HALT(위원회 결정 {dstate}): {sig.get('source', '')} — 헌법 3조, 유효한 KR 승인안 없이 주문하지 않음 (R-33′)**"])
        return
    weights, names = sig["weights"], sig.get("names", {})
    total_w = sum(weights.values())
    if total_w > 1.0 + 1e-6:
        sys.exit(f"헌법 1조 위반: 비중합 {total_w:.4f} — 집행 거부")
    for t, w in weights.items():
        if t not in ETFS and w > 0.10 + 1e-6:
            sys.exit(f"헌법 4조 위반: {t} {w:.1%} — 집행 거부")

    get_token()
    positions, equity, cash = get_balance()
    state = json.loads(STATE.read_text()) if STATE.exists() else {"peak": equity}
    peak = max(state.get("peak", equity), equity)
    dd = equity / peak - 1
    halt_buys = dd <= -DD_LIMIT
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"peak": peak, "last_equity": equity, "dd": round(dd, 4)}))

    lines = [f"# KR 집행 로그 {today_kst}",
             *([f"- **WARN 예수금 음수 {cash:,.0f}원 — 헌법 1조 위반 상태 (매수는 주문가능현금 범위 내로 제한됨)**"] if cash < 0 else []),
             *([f"- 소스: {sig['source']}"] if sig.get("source") else []),
             f"- 신호일: {sig['date']} (VT스케일 {sig.get('vt_scale')})",
             f"- 계좌: {equity:,.0f}원 (현금 {cash:,.0f}원, 고점대비 {dd:.1%})"]
    if halt_buys:
        lines.append("- **서킷브레이커 발동: 매수 중단, 매도만 집행 (재개는 Jamie 승인)**")

    orders = []
    below_min = []
    for s in sorted(set(weights) | set(positions)):
        diff = equity * weights.get(s, 0.0) - positions.get(s, 0.0)
        if abs(diff) < MIN_TRADE_KRW:
            if abs(diff) >= 1000:
                below_min.append(f"{s} {diff:+,.0f}")
            continue
        side = "buy" if diff > 0 else "sell"
        if side == "buy" and halt_buys:
            lines.append(f"- SKIP(서킷브레이커) {s} buy")
            continue
        orders.append((s, side, abs(diff)))
    orders.sort(key=lambda o: (0 if o[1] == "sell" else 1, -o[2]))  # 매도 먼저, 매수는 목표갭 큰 순 (결함 F 패턴 KR 예방)
    if below_min:  # R-55
        lines.append(f"- 미달 생략(MIN_TRADE {MIN_TRADE_KRW:,}원, 목표−보유): " + ", ".join(below_min))

    # 결함 N: 실집행 마커·집행 목표를 첫 주문 전에 기록
    MARKER.parent.mkdir(exist_ok=True)
    MARKER.write_text(today_kst + "\n")
    LAST_TARGET.write_text(json.dumps({"date": today_kst, "source": sig.get("source", "K조합 원신호"),
                                       "weights": weights}, ensure_ascii=False, indent=1))
    try:
        run_orders(orders, names, cash, lines)
    finally:
        if not orders:
            lines.append("- 조정 필요 없음")
        write_log(lines)


def wait_for_sell_proceeds(base_ord: float, sold: float, lines: list[str]) -> dict:
    """결함 X-a: 매도 전 주문가능현금(base_ord) 대비 매도액의 SETTLE_RATIO 만큼 늘 때까지 폴링."""
    bp, polls = None, 0
    for polls in range(1, SETTLE_POLLS + 1):
        time.sleep(SETTLE_WAIT)
        bp = get_buying_power()
        if sold <= 0 or bp["ord_psbl_cash"] >= base_ord + sold * SETTLE_RATIO:
            break
    grew = max(0.0, bp["ord_psbl_cash"] - base_ord)
    ratio = grew / sold if sold > 0 else 1.0
    lines.append(f"- 매수가능조회(매도 후): {fmt_bp(bp)}")
    lines.append(f"- 매도대금 반영: 주문가능현금 +{grew:,.0f} / 매도 {sold:,.0f}원 ({ratio:.0%}, 조회 {polls}회)")
    return bp


def run_orders(orders, names, cash, lines):
    budget = cash
    sold = 0.0
    sell_failed = False
    base_ord = None
    if any(side == "buy" for _, side, _ in orders):
        try:  # R-54: 매도 전 기준값 — 매도로 늘어난 몫만 측정하기 위함 (필드 의미론 가설과 독립)
            bp0 = get_buying_power()
            base_ord = bp0["ord_psbl_cash"]
            lines.append(f"- 매수가능조회(매도 전): {fmt_bp(bp0)}")
        except Exception as e:
            lines.append(f"- WARN 매도 전 매수가능조회 실패({str(e)[:60]}) → 기준값 없이 진행")
    buys_started = False
    for s, side, krw in orders:
        if side == "buy" and sell_failed:
            lines.append(f"- SKIP(매도 FAIL 발생 → 이번 회차 매수 중단) {names.get(s, s)}({s})")
            continue
        try:
            if side == "buy" and not buys_started:
                try:
                    if base_ord is not None:
                        bp = wait_for_sell_proceeds(base_ord, sold, lines)
                    else:
                        time.sleep(1.5)
                        bp = get_buying_power()
                        lines.append(f"- 매수가능조회: {fmt_bp(bp)}")
                    live_cash = bp["ord_psbl_cash"]   # ⚠️ max_buy_amt(미수 포함) 사용 금지 — 헌법 1조
                except Exception as e:
                    live_cash = max(0.0, min(budget, cash))
                    lines.append(f"- WARN 매수가능조회 실패({str(e)[:60]}) → 보수적 예산 {live_cash:,.0f}원")
                bound = min(budget, live_cash)   # 1조 이중방어 (R-28: 단순 삭제 금지)
                budget = max(0.0, bound) * CASH_BUFFER
                lines.append(f"- 매수 예산: min(스냅샷 {cash:,.0f} + 매도 {sold:,.0f}, 주문가능 {live_cash:,.0f})"
                             f" = {bound:,.0f}원 × {CASH_BUFFER:.0%} = {budget:,.0f}원")
                buys_started = True
            px = get_price(s)
            time.sleep(0.6)  # 모의투자 호출 한도 예의
            if side == "buy":
                krw = min(krw, budget)
                qty = int(krw // px)
                if qty < 1:
                    lines.append(f"- SKIP(현금/수량부족) {s} (잔여예산 {budget:,.0f}원, 1주 {px:,.0f}원)")
                    continue
            else:
                qty = max(1, int(krw // px))
            ok, msg = order(s, side, qty)
            nm = names.get(s, s)
            line = f"- {'OK' if ok else 'FAIL'} {side.upper()} {nm}({s}) {qty}주 약 {qty*px:,.0f}원" + ("" if ok else f" [{msg[:60]}]")
            # 결함 G: 주문 성공 시에만 예산 반영 (매도 실패분을 현금으로 오인 → 마진 차단, US 결함 B 이식)
            if ok:
                if side == "buy":
                    budget -= qty * px
                    line += f" → 잔여예산 {budget:,.0f}원"   # R-53
                else:
                    budget += qty * px
                    sold += qty * px
            elif side == "sell":
                sell_failed = True
            lines.append(line)
            time.sleep(0.6)
        except Exception as e:
            lines.append(f"- FAIL {side} {s}: {str(e)[:100]}")
            if side == "sell":
                sell_failed = True


if __name__ == "__main__":
    main()
