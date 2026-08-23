"""한국투자증권(KIS) 모의투자 집행 — signals/kr_target_weights.json 대로 리밸런싱.

- 도메인: 모의투자 전용 (openapivts) — 실전 도메인은 코드에 존재하지 않음 (안전장치)
- 헌법 강제: 비중합≤100%, 개별종목(ETF 제외)≤10%, 서킷브레이커 -15% 시 매수 중단
- 주문: 시장가, 정수 주량 (호가 조회 후 수량 계산), 매도 먼저 → 현금 확보 후 매수
Secrets: KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT (모의계좌 8자리)
"""
import json
import os
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIGNALS = ROOT / "signals" / "kr_target_weights.json"
STATE = ROOT / "signals" / "kr_account_state.json"
LOG = ROOT / "reports" / "kr_trade_log.md"

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


def main():
    if not (KEY and SECRET and CANO):
        sys.exit("KIS_APP_KEY/KIS_APP_SECRET/KIS_ACCOUNT 미설정 — GitHub Secrets 확인")
    sig = json.loads(SIGNALS.read_text())
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

    lines = [f"# KR 집행 로그 {date.today()}",
             f"- 신호일: {sig['date']} (VT스케일 {sig.get('vt_scale')})",
             f"- 계좌: {equity:,.0f}원 (현금 {cash:,.0f}원, 고점대비 {dd:.1%})"]
    if halt_buys:
        lines.append("- **서킷브레이커 발동: 매수 중단, 매도만 집행 (재개는 Jamie 승인)**")

    orders = []
    for s in sorted(set(weights) | set(positions)):
        diff = equity * weights.get(s, 0.0) - positions.get(s, 0.0)
        if abs(diff) < MIN_TRADE_KRW:
            continue
        side = "buy" if diff > 0 else "sell"
        if side == "buy" and halt_buys:
            lines.append(f"- SKIP(서킷브레이커) {s} buy")
            continue
        orders.append((s, side, abs(diff)))
    orders.sort(key=lambda o: 0 if o[1] == "sell" else 1)

    budget = cash
    for s, side, krw in orders:
        try:
            px = get_price(s)
            time.sleep(0.6)  # 모의투자 호출 한도 예의
            if side == "buy":
                krw = min(krw, budget)
                qty = int(krw // px)
                if qty < 1:
                    lines.append(f"- SKIP(현금/수량부족) {s}")
                    continue
                budget -= qty * px
            else:
                qty = max(1, int(krw // px))
                budget += qty * px
            ok, msg = order(s, side, qty)
            nm = names.get(s, s)
            lines.append(f"- {'OK' if ok else 'FAIL'} {side.upper()} {nm}({s}) {qty}주 약 {qty*px:,.0f}원" + ("" if ok else f" [{msg[:60]}]"))
            time.sleep(0.6)
        except Exception as e:
            lines.append(f"- FAIL {side} {s}: {str(e)[:100]}")
    if not orders:
        lines.append("- 조정 필요 없음")
    LOG.parent.mkdir(exist_ok=True)
    prev = LOG.read_text() if LOG.exists() else ""
    LOG.write_text("\n".join(lines) + "\n\n---\n\n" + prev)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
