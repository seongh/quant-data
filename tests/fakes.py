"""테스트용 가짜 브로커 (네트워크 없음). 실제 API 응답 형태만 흉내낸다.

FakeAlpaca: 매도 체결 대금이 `settle_after`번의 /v2/account 조회 뒤에야 cash에 반영되는
'무대기 재조회 레이스'(결함 H′-1)를 재현한다.
FakeKIS:   매도 대금이 `settle_after`번의 매수가능조회 뒤에야 ord_psbl_cash에 반영되는
결함 X-a 거동을 재현한다. max_buy_amt(미수 포함)는 항상 크게 보고한다 — 예산원으로 쓰면 1조 위반.
"""


class FakeAlpaca:
    def __init__(self, cash, positions, settle_after=0):
        self.cash = float(cash)
        self.positions = {k: float(v) for k, v in positions.items()}
        self.pending = []          # [잔여 조회 횟수, 금액]
        self.settle_after = settle_after
        self.orders = []           # (side, symbol, usd)
        self.account_calls = 0

    @property
    def equity(self):
        return self.cash + sum(self.positions.values()) + sum(a for _, a in self.pending)

    def _tick(self):
        still = []
        for n, amt in self.pending:
            if n <= 0:
                self.cash += amt
            else:
                still.append([n - 1, amt])
        self.pending = still

    def api(self, path, method="GET", body=None):
        if path == "/v2/account":
            self.account_calls += 1
            eq = self.equity
            self._tick()
            return {"equity": str(eq), "cash": str(self.cash)}
        if path == "/v2/positions":
            return [{"symbol": s, "market_value": str(v)} for s, v in self.positions.items()]
        if path.startswith("/v2/positions/") and method == "DELETE":
            s = path.rsplit("/", 1)[1]
            amt = self.positions.pop(s)
            self.orders.append(("sell", s, amt))
            self.pending.append([self.settle_after, amt])
            return {"id": f"o{len(self.orders)}", "symbol": s}
        if path == "/v2/orders" and method == "POST":
            s, side, usd = body["symbol"], body["side"], float(body["notional"])
            if side == "buy":
                if usd > self.cash + 1e-9:
                    raise RuntimeError(f"403 insufficient cash (would margin: {usd} > {self.cash})")
                self.cash -= usd
                self.positions[s] = self.positions.get(s, 0.0) + usd
            else:
                self.positions[s] -= usd
                self.pending.append([self.settle_after, usd])
            self.orders.append((side, s, usd))
            return {"id": f"o{len(self.orders)}"}
        raise AssertionError(f"unexpected call {method} {path}")


class FakeKIS:
    def __init__(self, cash, positions, prices, settle_after=0, reuse_stale=6_515_279):
        self.cash = float(cash)                 # dnca_tot_amt (스냅샷 예수금)
        self.ord_cash = float(cash)             # ord_psbl_cash
        self.positions = {k: float(v) for k, v in positions.items()}   # 평가금액
        self.prices = prices
        self.pending = []
        self.settle_after = settle_after
        self.reuse_stale = reuse_stale
        self.orders = []
        self.bp_calls = 0

    def equity(self):
        return self.cash + sum(self.positions.values())

    def get_balance(self):
        return dict(self.positions), self.equity(), self.cash

    def get_buying_power(self):
        self.bp_calls += 1
        still = []
        for n, amt in self.pending:
            if n <= 0:
                self.ord_cash += amt
            else:
                still.append([n - 1, amt])
        self.pending = still
        return {"ord_psbl_cash": self.ord_cash, "ruse_psbl_amt": float(self.reuse_stale),
                "nrcvb_buy_amt": self.ord_cash + 36_068, "max_buy_amt": self.ord_cash * 8.93}

    def get_price(self, code):
        return float(self.prices[code])

    def order(self, code, side, qty):
        px = self.prices[code]
        amt = qty * px
        if side == "buy":
            if amt > self.ord_cash + 1e-9:
                return False, "주문가능금액 초과 (미수 발생)"
            self.ord_cash -= amt
            self.positions[code] = self.positions.get(code, 0.0) + amt
        else:
            self.positions[code] = self.positions.get(code, 0.0) - amt
            if self.positions[code] <= 1:
                self.positions.pop(code)
            self.pending.append([self.settle_after, amt])
        self.orders.append((side, code, qty, amt))
        return True, "주문 완료"
