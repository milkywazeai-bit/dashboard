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


def now_kst() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Seoul"))
