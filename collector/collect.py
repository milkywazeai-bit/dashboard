"""메리츠증권 Open API 로 내 계좌를 조회해 대시보드 데이터를 갱신한다.

    python -m collector.collect                 # docs/data(.enc).json 갱신
    python -m collector.collect --require-password

환경변수
    MERITZ_APP_KEY / MERITZ_APP_SECRET   포털에서 발급 (앱키에 계좌가 묶여 있다)
    DASHBOARD_PASSWORD                   있으면 데이터를 암호화해 docs/data.enc.json 에 쓴다
    BACKFILL_FROM                        거래내역·실현손익을 이 날짜(YYYY-MM-DD)부터 받는다.
                                         나중에 더 앞당기면 빈 구간을 한 번 더 받는다
    BACKFILL_DAYS                        BACKFILL_FROM 이 없을 때 첫 실행에서 며칠 거슬러 받을지 (기본 90)
    PRINCIPAL                            원금(원). 정한 날을 기준으로 이후 입출금을 더하고 빼서
                                         "원금 대비 수익률"을 낸다. 공개 로그에 남지 않게 Secret 으로 넣는다
    PRINCIPAL_DATE                       원금 기준일(YYYY-MM-DD). 없으면 원금을 처음 정한 날

조회 전용이다. 주문·환전 API 는 부르지 않으며 MERITZ_READ_ONLY 도 강제로 켠다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

os.environ["MERITZ_READ_ONLY"] = "1"      # 이 프로그램은 어떤 경우에도 주문을 내지 않는다

from meritz import MeritzError, client, load_catalog, paginate  # noqa: E402

from . import metrics as m  # noqa: E402
from .crypto import decrypt, encrypt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PLAIN = ROOT / "docs" / "data.json"
ENC = ROOT / "docs" / "data.enc.json"
# 늦게 반영되는 거래(정정·취소 등)를 잡기 위해 마지막 동기화일 며칠 전부터 다시 받는다
RESYNC_DAYS = 3
# 추정 계산 방식이 바뀌면 올린다 — 저장된 추정을 다음 실행에서 다시 만든다
ESTIMATE_VERSION = 4

log = logging.getLogger("collector")

# 연결이 순간적으로 끊기거나 초당 호출 한도에 걸리면 잠시 뒤 다시 보낸다 (조회 전용이라 안전하다)
_TRANSIENT = {"UNREACHABLE", "TIMEOUT", "RATE_LIMITED"}
RETRIES = 3


def short_error(e: MeritzError) -> str:
    """화면에 띄울 짧은 사유. 연결 오류의 긴 원문(라이브러리 메시지)은 싣지 않는다."""
    if e.code in ("UNREACHABLE", "TIMEOUT"):
        return "서버 연결이 끊겨 받지 못했습니다. 다음 실행에서 다시 받습니다."
    return str(e).splitlines()[0][:200]


def with_retry(fn):
    for i in range(RETRIES):
        try:
            res = fn()
        except MeritzError as e:
            if e.code not in _TRANSIENT or i == RETRIES - 1:
                raise
        else:
            if not (isinstance(res, dict) and res.get("error") in _TRANSIENT) or i == RETRIES - 1:
                return res
        time.sleep(2 * (i + 1))


class Collector:
    def __init__(self):
        self.cat, self.c = load_catalog(), client()
        self.warnings: list[str] = []

    def _warn(self, msg: str):
        log.warning(msg)
        self.warnings.append(msg)

    def call(self, key: str, params: dict | None = None, title: str = ""):
        """한 번 조회해 data 를 돌려준다. 실패하면 경고를 남기고 None."""
        try:
            res = with_retry(lambda: self.c.call(self.cat.get(key), params or {}))
        except MeritzError as e:
            self._warn(f"{title or key}: {short_error(e)}")
            return None
        if not res["ok"]:
            hint = " (포털에서 이 API 사용 권한을 신청하세요)" if res.get("rsp_cd") == "0760" else ""
            self._warn(f"{title or key}: {res.get('error')} {res.get('rsp_cd') or ''} "
                       f"{res.get('message') or ''}{hint}".strip())
            return None
        body = res["body"]
        return body if isinstance(body, dict) else None

    @staticmethod
    def rows(body) -> list[dict]:
        if not body:
            return []
        d = body.get("data")
        return d if isinstance(d, list) else ([d] if isinstance(d, dict) and d else [])

    # ── 현재 상태 ──────────────────────────────────────────────

    def snapshot(self) -> dict:
        val = self.rows(self.call("valuation", title="자산평가"))
        dep = self.rows(self.call("deposit", title="예수금"))
        hold = self.rows(self.call("holdings", {"nthn_rmnd_incu_yn": "N"}, "종목별 평가"))
        ovs = self.rows(self.call("ovs_holdings", title="해외주식 잔고"))
        cur = self.rows(self.call("ovs_currencies", title="외화잔고"))

        holdings = m.merge_holdings([m.norm_holding(r) for r in hold],
                                    [m.norm_ovs_holding(r) for r in ovs])
        currencies = [{
            "currency": (r.get("crcd") or "").strip(),
            "cash": m.num(r.get("frcr_amt")),
            "orderable": m.num(r.get("estm_frcr_amt")),
            "fx_rate": m.num(r.get("vltn_exrt")),
            "cash_krw": m.num(r.get("excl_wncr_vltn_amt")),
            "stock_krw": m.num(r.get("ovrs_stck_vltn_amt")),
        } for r in cur if (r.get("crcd") or "").strip()]

        v = val[0] if val else {}
        d = dep[0] if dep else {}
        return {
            "ok": bool(val),
            "valuation": {
                "total": m.num(v.get("aset_vltn_amt")),
                "stock": m.num(v.get("mksc_vltn_amt")),
                "cash": m.num(v.get("tfam")),
                "cash_d2": m.num(v.get("d2_aft_tfam")),
                "fx_cash": m.num(v.get("frcr_tfam")),
                "loan": m.num(v.get("loan_amt")),
                "cost": m.num(v.get("byng_amt")),
                "withdrawable": m.num(v.get("wtmn_able_cash_amt")),
            },
            "deposit": {
                "cash": m.num(d.get("tfam")),
                "cash_d1": m.num(d.get("d1_aft_tfam")),
                "cash_d2": m.num(d.get("d2_aft_tfam")),
                "withdrawable": m.num(d.get("drng_able_amt")),
                "orderable": m.num(d.get("oder_able_cash_amt")),
                "unpaid": m.num(d.get("cash_unam")),
                "credit_loan": m.num(d.get("crdt_loan_amt")),
            } if d else None,
            "holdings": holdings,
            "currencies": currencies,
        }

    # ── 기간 조회 ──────────────────────────────────────────────

    def transactions(self, start: date, end: date) -> tuple[list[dict], date | None]:
        """입출금 내역. 행에 날짜가 없어 하루씩 조회한다. 끝까지 받은 마지막 날을 함께 돌려준다."""
        api, out, done = self.cat.get("transactions"), [], None
        day = start
        while day <= end:
            s = m.ymd(day)
            try:
                res = with_retry(lambda: paginate(self.c, api, {"from": s, "to": s, "sort": "ASC"}))
            except MeritzError as e:
                self._warn(f"입출금 내역 {m.iso(s)}: {short_error(e)}")
                break                         # 다음 실행에서 이 날부터 다시 받는다
            if res["truncated"]:
                self._warn(f"입출금 내역 {m.iso(s)}: {res['note']}")
            out += [m.norm_tx(r, s) for r in res["rows"] if isinstance(r, dict)]
            done = day
            day += timedelta(days=1)
        return out, done

    def realized_domestic(self, start: date, end: date) -> list[dict] | None:
        """국내 실현손익(일별). 한 달이라도 실패하면 None — 있던 자료를 빈 값으로 덮지 않도록."""
        rows = []
        for a, b in month_chunks(start, end):
            body = self.call("profit_loss", {"iqry_sta_date": m.ymd(a), "iqry_fin_date": m.ymd(b)},
                             f"국내 실현손익 {a:%Y-%m}")
            if body is None:
                return None
            rows += [m.norm_realized_day(r) for r in self.rows(body)]
        return rows

    def realized_overseas(self, start: date, end: date) -> list[dict] | None:
        rows = []
        for a, b in month_chunks(start, end):
            body = self.call("ovs_profit_loss", {"oder_sta_date": m.ymd(a), "oder_fin_date": m.ymd(b),
                                                 "ctcd": "000"}, f"해외 실현손익 {a:%Y-%m}")
            if body is None:
                return None
            rows += [m.norm_ovs_realized(r) for r in self.rows(body)]
        return rows

    def sync_range(self, state: dict, start: date, end: date, realized_from: date | None = None) -> date | None:
        """[start, end] 의 거래내역과 실현손익을 받아 state 에 갈아 끼운다.

        거래내역을 끝까지 받은 마지막 날을 돌려준다(하나도 못 받으면 None).
        실현손익은 realized_from(기본 start)부터 end 까지 받는다.
        """
        new_tx, done = self.transactions(start, end)
        if done:
            state["transactions"] = replace_range(state.get("transactions", []), new_tx, start, done)
        rl = realized_from or start
        for key, rows in (("realized_domestic", self.realized_domestic(rl, end)),
                          ("realized_overseas", self.realized_overseas(rl, end))):
            if rows is not None:
                state[key] = replace_range(state.get(key, []), rows, rl, end)
        return done

    def realized_summary(self, start: date, end: date) -> dict | None:
        body = self.call("profit_loss_summary", {"iqry_sta_date": m.ymd(start),
                                                 "iqry_fin_date": m.ymd(end)}, "국내 실현손익 합계")
        r = self.rows(body)
        if not r:
            return None
        r = r[0]
        return {"from": start.isoformat(), "to": end.isoformat(),
                "sell": m.num(r.get("seln_amt")), "buy": m.num(r.get("byov_amt")),
                "fee": m.num(r.get("deal_fee")), "tax": m.num(r.get("wtax_amt")),
                "pnl": m.num(r.get("prls_amt")), "ret": m.num(r.get("plrt"))}

    # ── 과거 시세 (총자산 추정용) ──────────────────────────────────

    def daily_closes(self, code: str, start: date, end: date) -> dict[str, float] | None:
        """종목의 {YYYY-MM-DD: 종가}. 해외는 현지 거래일·현지통화 기준. 실패하면 None."""
        out: dict[str, float] = {}
        overseas = m.is_overseas_code(code)
        for a, b in month_chunks(start, end):
            if overseas:
                sym, _, exch = code.rpartition(".")
                body = self.call("ovs_market_candles_days",
                                 {"mrkt_div_code": "OV", "mrkt_cls_code": exch, "iscd": sym,
                                  "dely_rltm_cls_code": "1", "mod_stpr_cls_code": "0",
                                  "from": m.ymd(a), "to": m.ymd(b)}, f"일봉 {code}")
                field = "prpr"
            else:
                body = self.call("market_candles_days",
                                 {"mrkt_div_code": "J", "iscd": code.lstrip("A")[-6:],
                                  "from": m.ymd(a), "to": m.ymd(b)}, f"일봉 {code}")
                field = "stck_clpr"
            if body is None:
                return None
            for r in self.rows(body):
                d, px = str(r.get("date") or ""), m.num(r.get(field))
                if len(d) == 8 and px > 0:
                    out[m.iso(d)] = px
        return out

    def usd_rates(self, days: list[str]) -> dict[str, float]:
        """날짜별 원/달러 매매기준율. 고시가 없는 날은 빠진다."""
        out = {}
        for d in days:
            body = self.call("fx_rates", {"stnd_date": d.replace("-", ""), "crcd": "USD"}, f"환율 {d}")
            for r in self.rows(body):
                v = m.num(r.get("deal_stnd_exrt")) or m.num(r.get("stnd_exrt"))
                if v:
                    out[d] = v
        return out


def build_estimate(col: Collector, txs: list[dict], snap: dict, start: date, end: date, today: date,
                   anchor: dict):
    """start~end 영업일의 총자산을 추정한다.

    그래프에 쓰는 값은 첫 실제 기록(anchor)에서 하루씩 손익을 거꾸로 빼 나간 값이다
    (metrics.estimate_by_pnl). 예수금 잔액으로 쌓아 올린 값은 진단용으로만 남긴다.
    """
    days = [d.isoformat() for d in daterange(start, end) if d.weekday() < 5] + [today.isoformat()]
    current = {}
    for h in snap["holdings"]:
        current[h["code"]] = current.get(h["code"], 0.0) + h["qty"]
    pos, _ = m.positions_by_day(txs, current, days)
    codes = sorted({c for p in pos.values() for c in p})

    prices = {}
    for c in codes:
        series = col.daily_closes(c, start - timedelta(days=10), today)
        if series:
            prices[c] = series
    usd = next((c["fx_rate"] for c in snap["currencies"] if c["currency"] == "USD"), 0.0)
    needs_fx = any(m.is_overseas_code(c) for c in codes) or any(t["foreign"] for t in txs)
    fx = col.usd_rates(days) if needs_fx else {}
    fallback = {h["code"]: h["price"] for h in snap["holdings"]}

    points, diag = m.estimate_history(txs, current, days, prices, fx, fallback, usd,
                                      snap["valuation"]["fx_cash"] / usd if usd else 0.0)
    check = next((p for p in points if p["date"] == today.isoformat()), None)
    actual = snap["valuation"]["total"]
    diag.update({
        "from": start.isoformat(), "to": end.isoformat(),
        "days": len(points) - (1 if check else 0),
        "today_estimate": check["total"] if check else None,
        "today_actual": actual,
        "error_pct": ((check["total"] - actual) / actual * 100) if check and actual else None,
        "cash_error": (check["cash"] - snap["valuation"]["cash"]) if check else None,
        # 오늘 값을 항목별로 맞춰 본다 — 어느 쪽이 어긋나는지 보려는 진단용
        "parts": {k: {"estimate": check[k] if check else None, "actual": snap["valuation"][k]}
                  for k in ("cash", "fx_cash", "stock")},
    })
    pnl_points, pnl_diag = m.estimate_by_pnl(txs, current, days, prices, fx, usd,
                                             anchor["date"], anchor["total"])
    diag.update(method="pnl", min_total=pnl_diag["min_total"],
                negative_qty=pnl_diag["negative_qty"],
                unpriced=sorted(set(diag["unpriced"]) | set(pnl_diag["unpriced"])),
                days=len(pnl_points))
    return pnl_points, diag


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def month_chunks(start: date, end: date):
    a = start
    while a <= end:
        nxt = (a.replace(day=1) + timedelta(days=32)).replace(day=1)
        b = min(end, nxt - timedelta(days=1))
        yield a, b
        a = nxt


def replace_range(old: list[dict], new: list[dict], start: date, end: date) -> list[dict]:
    """[start, end] 구간의 행은 새로 받은 것으로 갈아 끼운다."""
    lo, hi = start.isoformat(), end.isoformat()
    kept = [r for r in old if not (lo <= r["date"] <= hi)]
    return sorted(kept + new, key=lambda r: (r["date"], r.get("srno", 0)))


# ── 저장 ───────────────────────────────────────────────────────

def load_state(password: str | None) -> dict:
    if password and ENC.exists():
        return decrypt(json.loads(ENC.read_text("utf-8")), password)
    if PLAIN.exists():
        return json.loads(PLAIN.read_text("utf-8"))
    if ENC.exists():
        raise SystemExit("docs/data.enc.json 이 있는데 DASHBOARD_PASSWORD 가 없습니다.")
    return {}


def save_state(state: dict, password: str | None):
    if password:
        ENC.write_text(json.dumps(encrypt(state, password)), "utf-8")
        PLAIN.unlink(missing_ok=True)          # 평문이 남아 공개되지 않도록
    else:
        PLAIN.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


def load_dotenv():
    """저장소 루트의 .env 를 읽는다(로컬 실행용). 이미 있는 환경변수는 덮지 않는다."""
    from meritz.core.config import _parse_env_line
    p = ROOT / ".env"
    if p.is_file():
        for line in p.read_text("utf-8").splitlines():
            k, v = _parse_env_line(line)
            if k and v:
                os.environ.setdefault(k, v)


def run(require_password: bool = False) -> int:
    load_dotenv()
    os.environ["MERITZ_READ_ONLY"] = "1"
    password = os.getenv("DASHBOARD_PASSWORD") or None
    if require_password and not password:
        print("DASHBOARD_PASSWORD 가 없습니다. 공개 페이지에 계좌 정보를 평문으로 올리지 않도록 "
              "중단합니다. 저장소 Settings → Secrets 에 추가하세요.", file=sys.stderr)
        return 2

    state = load_state(password)
    now = m.now_kst()
    today = now.date()
    backfill_from = (date.fromisoformat(os.environ["BACKFILL_FROM"]) if os.getenv("BACKFILL_FROM")
                     else today - timedelta(days=int(os.getenv("BACKFILL_DAYS") or 90)))
    col = Collector()

    snap = col.snapshot()
    if not snap["ok"]:
        print("자산평가 조회에 실패해 갱신을 중단합니다:\n  " + "\n  ".join(col.warnings), file=sys.stderr)
        return 1

    # 거래내역·실현손익
    #  · 처음 실행: BACKFILL_FROM(또는 BACKFILL_DAYS 일 전)부터 오늘까지
    #  · 이후: 마지막으로 받은 날 며칠 전부터 오늘까지 (실현손익은 지난달 1일부터)
    #  · BACKFILL_FROM 을 예전 날짜로 당기면 그 사이 빈 구간을 한 번 더 받는다
    until, since = state.get("tx_synced_until"), state.get("tx_synced_from")
    if until and not since:
        # 이전 버전이 만든 상태 — 처음 받은 날을 기록하지 않았다. 가장 이른 거래일로 본다.
        since = min((t["date"] for t in state.get("transactions", [])), default=until)
    if not until:
        done = col.sync_range(state, backfill_from, today)
        if done:
            state["tx_synced_from"], state["tx_synced_until"] = backfill_from.isoformat(), done.isoformat()
    else:
        if backfill_from.isoformat() < since:
            gap_end = date.fromisoformat(since) - timedelta(days=1)
            if col.sync_range(state, backfill_from, gap_end) == gap_end:
                since = backfill_from.isoformat()
            else:
                col._warn(f"{backfill_from} 부터의 과거 내역을 다 받지 못했습니다. 다음 실행에서 다시 받습니다.")
        state["tx_synced_from"] = since
        start = date.fromisoformat(until) - timedelta(days=RESYNC_DAYS)
        prev_month = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        done = col.sync_range(state, start, today, realized_from=min(start, prev_month))
        if done:
            state["tx_synced_until"] = done.isoformat()
    warns_before = len(col.warnings)
    if any("frcr_balance" not in t for t in state.get("transactions", [])) and state.get("tx_synced_from"):
        # 이전 버전이 받은 내역에는 외화 잔액이 없다 — 추정에 필요해 한 번 다시 받는다
        col.sync_range(state, date.fromisoformat(state["tx_synced_from"]), today)
    txs = state.get("transactions", [])

    # 일별 스냅샷 — 같은 날 다시 돌리면 덮어쓴다
    v = snap["valuation"]
    unrealized = sum(h["pnl"] for h in snap["holdings"])
    point = {"date": today.isoformat(), "total": v["total"], "stock": v["stock"],
             "cash": v["cash"], "fx_cash": v["fx_cash"], "loan": v["loan"],
             "cost": v["cost"], "unrealized": unrealized}
    snaps = [s for s in state.get("history", [])
             if s["date"] != point["date"] and not s.get("estimated")] + [point]

    # 실제 기록이 시작되기 전 구간은 거래내역과 종가로 추정한다. 한 번 만들어 두고,
    # 받아 둔 거래내역의 시작일이 바뀌었을 때만 다시 만든다.
    first_real = min(s["date"] for s in snaps)
    est = state.get("estimate") or {}
    tx_from = min((t["date"] for t in txs), default=None)
    if tx_from and tx_from < first_real and (est.get("from") != tx_from or not est.get("complete")
                                             or est.get("version") != ESTIMATE_VERSION
                                             or os.getenv("REBUILD_ESTIMATE")):
        anchor = min(snaps, key=lambda s: s["date"])
        points, diag = build_estimate(col, txs, snap, date.fromisoformat(tx_from),
                                      date.fromisoformat(first_real) - timedelta(days=1), today, anchor)
        # 조회가 하나라도 실패했거나 외화 잔액이 빠진 거래가 남았으면 다음 실행에서 다시 만든다
        complete = (len(col.warnings) == warns_before
                    and all("frcr_balance" in t for t in txs))
        est = {"from": tx_from, "points": points, "check": diag, "complete": complete,
               "version": ESTIMATE_VERSION}
        state["estimate"] = est
    # 정확도 검사를 통과한 추정만 기록 앞에 붙인다. 틀린 추정은 수익률을 망가뜨린다.
    est["used"] = m.estimate_usable(est.get("check"))
    estimated = [p for p in est.get("points", []) if p["date"] < first_real] if est["used"] else []

    usd = next((c["fx_rate"] for c in snap["currencies"] if c["currency"] == "USD"), 0.0)
    history = m.compute_history(estimated + snaps, m.flows_by_date(txs, usd))

    # 원금 — PRINCIPAL(원)을 정한 날을 기준일로 삼고, 그 뒤 입출금은 대시보드가 더하고 뺀다.
    # 금액이 바뀌면 기준일도 새로 잡는다. PRINCIPAL_DATE 로 기준일을 직접 정할 수도 있다.
    amount = m.num(os.getenv("PRINCIPAL"))
    if amount > 0:
        pr = state.get("principal") or {}
        when = os.getenv("PRINCIPAL_DATE") or (pr.get("date") if pr.get("amount") == amount else None)
        state["principal"] = {"amount": amount, "date": when or today.isoformat()}
    elif "PRINCIPAL" in os.environ and not os.environ["PRINCIPAL"].strip():
        state.pop("principal", None)

    state.update({
        "version": 1,
        "updated_at": now.isoformat(timespec="seconds"),
        "valuation": v,
        "deposit": snap["deposit"],
        "holdings": snap["holdings"],
        "currencies": snap["currencies"],
        "realized_ytd": col.realized_summary(today.replace(month=1, day=1), today),
        "history": history,
        # 원금에 반영된 금액을 행마다 남긴다 — 대시보드가 어떤 행이 원금에 들어갔는지 보여 준다
        "transactions": [{**t, "flow": m.tx_flow(t)} for t in txs],
        "warnings": col.warnings,
    })
    save_state(state, password)
    # Actions 로그는 공개 저장소에서 누구나 볼 수 있다 — 금액은 남기지 않는다
    print(f"{today} 갱신 완료 · 보유 {len(snap['holdings'])}종목 · 거래내역 {len(txs)}건 · "
          f"기록 {len(snaps)}일 + 추정 {len(estimated)}일"
          + (f" · 경고 {len(col.warnings)}건" if col.warnings else ""))
    for w in col.warnings:
        print(f"  ! {w}")
    return 0


def main():
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--require-password", action="store_true",
                   help="DASHBOARD_PASSWORD 가 없으면 평문으로 쓰지 않고 중단")
    sys.exit(run(p.parse_args().require_password))


if __name__ == "__main__":
    main()
