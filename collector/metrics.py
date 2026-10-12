"""API 응답 정리와 수익률 계산. 네트워크를 쓰지 않는 순수 함수만 둔다(테스트 대상)."""
from __future__ import annotations

from datetime import date, datetime

# 입출금 내역의 거래유형코드(tr_type_code) 중 계좌 밖과 현금이 오간 것.
# 이 금액은 수익이 아니라 원금 변동이라 수익률 계산에서 빼야 한다.
DEPOSIT_TYPES = {"01"}      # 입금
WITHDRAW_TYPES = {"02"}     # 출금
# 환전은 원화 출금 + 외화 입금 두 행으로 내려와 서로 상쇄된다. 한쪽만 환산 오차가
# 생기지 않도록 둘 다 원금 변동에서 뺀다.
_FLOW_EXCLUDE_WORDS = ("환전",)


def num(v) -> float:
    """API 숫자 → float. 천단위 콤마·빈 값·None 을 처리한다."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "+"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def iso(s: str) -> str:
    """YYYYMMDD → YYYY-MM-DD"""
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


# ── 정리 ──────────────────────────────────────────────────────────

def norm_holding(r: dict) -> dict:
    """내 계좌 종목별 평가(holdings) 한 행. 금액은 국내·해외 모두 원화 환산액이다."""
    overseas = (r.get("mksc_cls_name") == "해외주식" or r.get("pros_trgt_cls_code") == "J")
    qty = num(r.get("rmnd_qty")) + num(r.get("rmnd_dqty"))
    return {
        "code": (r.get("iscd") or "").strip(),
        "name": (r.get("isnm") or "").strip(),
        "type": (r.get("mksc_cls_name") or "").strip(),
        "market": "해외" if overseas else "국내",
        "qty": qty,
        "avg_price": num(r.get("byng_unpr")),     # 해외는 거래 통화
        "price": num(r.get("psnt_prc")),          # 해외는 거래 통화
        "prev_close": num(r.get("prdy_last_prc")),
        "value": num(r.get("vltn_amt")),          # 원화
        "cost": num(r.get("byng_amt")),           # 원화
        "pnl": num(r.get("vltn_prls_amt")),       # 원화
        "ret": num(r.get("ert")),                 # %, 해외는 원화 환산 기준
        "loan": num(r.get("loan_amt")),
    }


def norm_ovs_holding(r: dict) -> dict:
    """해외주식 잔고(ovs_holdings) 한 행. 금액은 거래 통화 기준."""
    return {
        "code": (r.get("iscd") or "").strip(),
        "symbol": (r.get("ovrs_stck_symb_iscd") or "").strip(),
        "currency": (r.get("crcd") or "").strip(),
        "fx_rate": num(r.get("exrt")),
        "local_ret": num(r.get("vltn_ert")),      # 환율 영향 제외 수익률
        "local_value": num(r.get("frcr_vltn_amt")),
        "local_cost": num(r.get("byov_camt")),
        "local_pnl": num(r.get("vltn_prls_camt")),
    }


def merge_holdings(rows: list[dict], ovs_rows: list[dict]) -> list[dict]:
    """종목별 평가에 해외 잔고의 거래 통화 정보를 붙인다. 합산하지 않는다(이중 계산 방지)."""
    by_code = {o["code"]: o for o in ovs_rows if o["code"]}
    out = []
    for h in rows:
        o = by_code.get(h["code"]) if h["market"] == "해외" else None
        if o:
            h = {**h, **{k: v for k, v in o.items() if k != "code"}}
        out.append(h)
    out.sort(key=lambda h: h["value"], reverse=True)
    return out


def norm_tx(r: dict, day: str) -> dict:
    """입출금 내역 한 행. 이 API 의 행에는 날짜가 없어 하루씩 조회한 날짜를 붙인다."""
    frcr = num(r.get("frcr_tr_amt"))
    foreign = r.get("issu_type_code") == "35" or frcr != 0
    settled = num(r.get("if_fee_excl_amt"))
    return {
        "date": iso(day),
        "srno": int(num(r.get("tran_srno"))),
        "type": (r.get("tr_type_code") or "").strip(),
        "name": (r.get("smcd_name") or "").strip(),
        "code": (r.get("iscd") or "").strip(),
        "isnm": (r.get("kor_isnm") or "").strip(),
        "qty": num(r.get("if_tr_qty")),
        "price": num(r.get("byng_unpr")),
        "amount": 0.0 if foreign else settled,   # 원화 정산금액
        "frcr_amount": frcr if frcr else (settled if foreign else 0.0),
        "foreign": foreign,
        "fee": num(r.get("deal_fee")) + num(r.get("fee")) + num(r.get("ovsc_deal_fee")),
        "tax": sum(num(r.get(k)) for k in ("intx", "rstx", "srtx", "trtx")),
        "io": (r.get("io_cls_code") or "").strip(),
        "cancelled": (r.get("cncl_yn") or "").strip() == "Y",
        "balance": num(r.get("tfam_nwrm")),
        "frcr_balance": num(r.get("frcr_tfam_nwrm")),
    }


def tx_flow(tx: dict, fx_rate: float = 0.0) -> float:
    """거래 한 건이 원금을 얼마나 바꿨는가(원). 입금 +, 출금 −, 그 밖은 0."""
    if tx["cancelled"] or tx["io"] == "3":
        return 0.0
    if any(w in tx["name"] for w in _FLOW_EXCLUDE_WORDS):
        return 0.0
    sign = 1 if tx["type"] in DEPOSIT_TYPES else -1 if tx["type"] in WITHDRAW_TYPES else 0
    if not sign:
        return 0.0
    amt = tx["amount"] if not tx["foreign"] else tx["frcr_amount"] * fx_rate
    return sign * abs(amt)


def norm_realized_day(r: dict) -> dict:
    return {
        "date": iso(r.get("deal_date") or "00000000"),
        "buy": num(r.get("byov_amt")),
        "sell": num(r.get("seln_amt")),
        "fee": num(r.get("deal_fee")),
        "tax": num(r.get("wtax_amt")),
        "pnl": num(r.get("prls_amt")),
        "ret": num(r.get("plrt")),
    }


def norm_ovs_realized(r: dict) -> dict:
    return {
        "date": iso(r.get("oder_date") or "00000000"),
        "code": (r.get("iscd") or "").strip(),
        "name": (r.get("kor_issu_abwr_name") or "").strip(),
        "currency": (r.get("crcd") or "").strip(),
        "qty": num(r.get("seln_qty")),
        "sell_krw": num(r.get("wncr_seln_amt")),
        "buy_krw": num(r.get("wncr_byov_amt")),
        "pnl": num(r.get("rlzt_prls_amt")),           # 원화 (매매손익 + 환차손익)
        "fx_pnl": num(r.get("wncr_fed_prls_amt")),
        "local_pnl": num(r.get("frcr_rlzt_ernn_amt")),
        "local_ret": num(r.get("frcr_plrt")),
    }


# ── 수익률 ────────────────────────────────────────────────────────

def flows_by_date(txs: list[dict], fx_rate: float = 0.0) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in txs:
        f = tx_flow(t, fx_rate)
        if f:
            out[t["date"]] = out.get(t["date"], 0.0) + f
    return out


def compute_history(snapshots: list[dict], flows: dict[str, float]) -> list[dict]:
    """일별 스냅샷에 원금 변동을 반영한 수익률을 붙인다.

    일간 수익률 r_t = (V_t − F_t) / V_{t−1} − 1
      F_t 는 직전 스냅샷 다음날부터 오늘까지의 순입금(입금 − 출금).
      입출금이 장 마감 뒤에 들어온 것으로 본다 — 입금이 수익으로 잡히지 않는다.
    누적 수익률(TWR) 은 일간 수익률을 곱해 이어 붙인 값이라 입출금 시점과 금액에
    영향을 받지 않는다. 손익금액은 V_t − V_0 − ΣF.
    """
    snaps = sorted(snapshots, key=lambda s: s["date"])
    flow_dates = sorted(flows)
    out, growth, flow_sum = [], 1.0, 0.0
    prev = None
    for s in snaps:
        s = dict(s)
        if prev is None:
            f = 0.0
            s.update(flow=0.0, daily_ret=None, twr=0.0, pnl_cum=0.0)
            base_value = s["total"]
        else:
            f = sum(flows[d] for d in flow_dates if prev["date"] < d <= s["date"])
            flow_sum += f
            r = ((s["total"] - f) / prev["total"] - 1) if prev["total"] > 0 else None
            if r is not None:
                growth *= 1 + r
            s.update(flow=f, daily_ret=None if r is None else r * 100,
                     twr=(growth - 1) * 100,
                     pnl_cum=s["total"] - base_value - flow_sum)
        out.append(s)
        prev = s
    return out


def period_return(history: list[dict], start_iso: str) -> float | None:
    """start_iso 직전 스냅샷 대비 최신 TWR 수익률(%)."""
    if not history:
        return None
    before = [h for h in history if h["date"] < start_iso]
    base = before[-1] if before else history[0]
    g0, g1 = 1 + base["twr"] / 100, 1 + history[-1]["twr"] / 100
    return (g1 / g0 - 1) * 100 if g0 else None


# ── 과거 총자산 추정 ──────────────────────────────────────────────
#
# API 에는 과거 일자의 총자산이 없다. 거래내역으로 그날의 보유 수량과 예수금을
# 되짚고, 일봉 종가를 곱해 추정한다. 실제 스냅샷이 시작되기 전 구간에만 쓴다.

QTY_UP = {"03", "05", "07", "09"}      # 입고·매수·조건부매수·조건부환매수
QTY_DOWN = {"04", "06", "08", "10"}    # 출고·매도·조건부매도·조건부환매도
# 잔액이 0 으로 내려와도 진짜 0 일 수 있는 거래(출금·매수). 그 밖의 0 은 '잔액 없음' 으로 본다.
_ZERO_BALANCE_OK = {"02", "05"}


def is_overseas_code(code: str) -> bool:
    return "." in code                  # 해외는 "AAPL.OQ", 국내는 "A005930"


def qty_delta(tx: dict) -> float:
    if tx["cancelled"] or not tx["code"] or not tx["qty"]:
        return 0.0
    # 해외주식은 한 체결이 "해외주식매수" + "해외주식매수대금" 두 행으로 내려온다(수량도 같다).
    # "~대금" 행은 돈이 오간 기록이라 수량으로 세면 매수·매도가 두 배가 된다.
    if tx["name"].replace(" ", "").endswith("대금"):
        return 0.0
    if tx["type"] in QTY_UP:
        return tx["qty"]
    if tx["type"] in QTY_DOWN:
        return -tx["qty"]
    return 0.0


def positions_by_day(txs: list[dict], current: dict[str, float], days: list[str]):
    """각 날짜 장 마감 기준 보유 수량. 현재 잔고에서 거래를 거꾸로 되돌려 구한다.

    돌려주는 것: ({날짜: {종목: 수량}}, 수량이 음수가 된 종목 집합)
    음수는 거래내역과 현재 잔고가 맞지 않는다는 뜻이라 0 으로 두고 알린다.
    """
    by_date: dict[str, list[dict]] = {}
    for t in txs:
        by_date.setdefault(t["date"], []).append(t)
    pending = sorted(by_date, reverse=True)
    qty, out, bad = dict(current), {}, set()
    for d in sorted(days, reverse=True):
        while pending and pending[0] > d:            # d 이후의 거래를 되돌린다
            for t in by_date[pending.pop(0)]:
                q = qty_delta(t)
                if q:
                    qty[t["code"]] = qty.get(t["code"], 0.0) - q
        snap = {}
        for c, q in qty.items():
            if q < -1e-6:
                bad.add(c)
            elif q > 1e-9:
                snap[c] = q
        out[d] = snap
    return out, bad


def balances_by_day(txs: list[dict], days: list[str], foreign: bool) -> dict[str, float]:
    """각 날짜 마지막 거래 직후의 예수금 잔액(원화 또는 외화). 첫 잔액 이전 날짜는 빠진다.

    원화 잔액은 외화가 걸린 행(환전 등)에도 담겨 온다 — 그 행을 빼면 환전으로 빠져나간
    원화가 잔액에 남아 총자산이 부풀려진다. 그래서 외화 여부와 무관하게 잔액이 있는 행을 쓴다.
    해외 매매 행은 원화 잔액이 0 으로 내려오므로(문서) 0 은 '잔액 없음' 으로 본다.
    외화 잔액도 0 은 '잔액 없음' 으로 본다.
    """
    key = "frcr_balance" if foreign else "balance"
    rows = sorted(txs, key=lambda t: (t["date"], t["srno"]))
    out, last, i = {}, None, 0
    for d in sorted(days):
        while i < len(rows) and rows[i]["date"] <= d:
            t = rows[i]
            i += 1
            v = t.get(key, 0.0)
            if foreign:
                if t["foreign"] and v:
                    last = v
            elif v or (not t["foreign"] and t["type"] in _ZERO_BALANCE_OK):
                last = v
        if last is not None:
            out[d] = last
    return out


def ffill(series: dict[str, float], d: str, strict: bool = False) -> float | None:
    """d 이전(또는 같은 날)의 가장 최근 값."""
    best = None
    for k in sorted(series):
        if k < d or (not strict and k == d):
            best = series[k]
        else:
            break
    return best


def estimate_history(txs: list[dict], current_qty: dict[str, float], days: list[str],
                     prices: dict[str, dict[str, float]], fx: dict[str, float],
                     fallback_price: dict[str, float], fx_now: float, fx_cash_now: float):
    """날짜별 추정 총자산. prices 는 종목별 {날짜: 종가}(해외는 현지통화), fx 는 {날짜: 원/달러}.

    해외 종가는 현지 거래일 기준이라 한국 날짜 d 에는 d 전날까지의 종가를 쓴다.
    시세를 못 받은 종목은 마지막 거래 단가(없으면 현재가)로 평가한다.
    """
    pos, bad = positions_by_day(txs, current_qty, days)
    krw = balances_by_day(txs, days, foreign=False)
    has_fx_rows = any(t["foreign"] for t in txs)
    usd = balances_by_day(txs, days, foreign=True) if has_fx_rows else {}
    trade_px: dict[str, dict[str, float]] = {}
    for t in txs:
        if t["code"] and t["price"] and not t["cancelled"]:
            trade_px.setdefault(t["code"], {})[t["date"]] = t["price"]

    points, unpriced = [], set()
    for d in sorted(days):
        if d not in krw:
            continue                         # 첫 원화 잔액 이전은 추정할 수 없다
        rate = ffill(fx, d) or fx_now
        stock = 0.0
        for c, q in pos[d].items():
            ov = is_overseas_code(c)
            px = ffill(prices.get(c, {}), d, strict=ov)
            if px is None:
                px = ffill(trade_px.get(c, {}), d) or fallback_price.get(c)
                unpriced.add(c)
            if px is None:
                continue
            stock += q * px * (rate if ov else 1)
        fx_cash = (usd.get(d, 0.0) if has_fx_rows else fx_cash_now) * rate
        points.append({"date": d, "total": krw[d] + stock + fx_cash, "stock": stock,
                       "cash": krw[d], "fx_cash": fx_cash, "estimated": True})
    return points, {"negative_qty": sorted(bad), "unpriced": sorted(unpriced),
                    "codes": code_breakdown(txs, current_qty, bad)}


def estimate_by_pnl(txs: list[dict], current_qty: dict[str, float], days: list[str],
                    prices: dict[str, dict[str, float]], fx: dict[str, float], fx_now: float,
                    anchor_date: str, anchor_total: float):
    """실제 총자산 하나(anchor)에서 출발해 하루씩 거꾸로 그날 손익과 입출금을 빼 나간다.

        V(전날) = V(그날) − 손익(그날) − 순입금(그날)
        손익 = Σ 전날 보유수량 × (종가 변동, 해외는 원화 환산)
             + Σ 그날 매매수량 × (종가 − 체결가) − 국내 수수료·세금

    예수금 잔액을 쓰지 않으므로 거래내역의 잔액이 결제일 기준이어도 출렁이지 않고,
    실제 기록과 끊김 없이 이어진다. 배당·이자는 빠지므로 그만큼은 오차로 남는다.
    """
    days = sorted(d for d in days if d <= anchor_date)
    if not days or days[-1] != anchor_date:
        days.append(anchor_date)
    pos, bad = positions_by_day(txs, current_qty, days)
    flows = flows_by_date(txs, fx_now)
    trade_px: dict[str, dict[str, float]] = {}
    for t in txs:
        if t["code"] and t["price"] and not t["cancelled"]:
            trade_px.setdefault(t["code"], {})[t["date"]] = t["price"]

    rate = lambda d: ffill(fx, d) or fx_now  # noqa: E731
    unpriced = set()

    def val(c: str, d: str) -> float:
        ov = is_overseas_code(c)
        px = ffill(prices.get(c, {}), d, strict=ov)
        if px is None:
            px = ffill(trade_px.get(c, {}), d) or 0.0
            unpriced.add(c)
        return px * (rate(d) if ov else 1)

    by_date: dict[str, list[dict]] = {}
    for t in txs:
        if qty_delta(t):
            by_date.setdefault(t["date"], []).append(t)
    flow_dates, trade_dates = sorted(flows), sorted(by_date)

    pnl: dict[str, float] = {}
    for prev, d in zip(days, days[1:]):
        p = sum(q * (val(c, d) - val(c, prev)) for c, q in pos[prev].items())
        for td in (x for x in trade_dates if prev < x <= d):
            for t in by_date[td]:
                c, dq = t["code"], qty_delta(t)
                ov = is_overseas_code(c)
                p += dq * (val(c, d) - t["price"] * (rate(d) if ov else 1))
                if not ov:
                    p -= t["fee"] + t["tax"]
        pnl[d] = p

    total = {anchor_date: anchor_total}
    for prev, d in reversed(list(zip(days, days[1:]))):
        f = sum(flows[x] for x in flow_dates if prev < x <= d)
        total[prev] = total[d] - pnl[d] - f
    points = [{"date": d, "total": total[d], "estimated": True} for d in days if d != anchor_date]
    return points, {"negative_qty": sorted(bad), "unpriced": sorted(unpriced),
                    "min_total": min((p["total"] for p in points), default=None)}


def code_breakdown(txs: list[dict], current_qty: dict[str, float], codes) -> list[dict]:
    """수량이 맞지 않는 종목의 거래를 유형·적요별로 묶는다. 어떤 거래 유형을 놓쳤는지 보려는 진단용."""
    out = []
    for c in sorted(codes):
        groups: dict[tuple, dict] = {}
        for t in txs:
            if t["code"] != c:
                continue
            g = groups.setdefault((t["type"], t["name"], t["cancelled"]),
                                  {"type": t["type"], "name": t["name"], "cancelled": t["cancelled"],
                                   "count": 0, "qty": 0.0, "counted": qty_delta(t) != 0 or not t["qty"]})
            g["count"] += 1
            g["qty"] += t["qty"]
        out.append({"code": c, "current_qty": current_qty.get(c, 0.0),
                    "groups": sorted(groups.values(), key=lambda g: (g["type"], g["name"]))})
    return out


# 추정을 화면에 쓰는 기준. 오늘 값을 같은 방법으로 계산했을 때 이보다 많이 어긋나거나,
# 거래내역으로 되짚은 수량이 음수가 된 종목이 있으면 추정은 쓰지 않는다.
ESTIMATE_MAX_ERROR_PCT = 3.0


def estimate_usable(check: dict | None) -> bool:
    if check and check.get("method") == "pnl":
        # 실제 총자산에서 거꾸로 이어 붙이므로 오늘 오차 비교는 의미가 없다.
        # 수량이 맞고 총자산이 끝까지 양수면 쓴다.
        return not check.get("negative_qty") and (check.get("min_total") or 0) > 0
    if not check or check.get("error_pct") is None:
        return False
    return abs(check["error_pct"]) <= ESTIMATE_MAX_ERROR_PCT and not check.get("negative_qty")


def now_kst() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Seoul"))
