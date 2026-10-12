"""메리츠증권 Open API 로 내 계좌를 조회해 대시보드 데이터를 갱신한다.

    python -m collector.collect                 # docs/data(.enc).json 갱신
    python -m collector.collect --require-password

환경변수
    MERITZ_APP_KEY / MERITZ_APP_SECRET   포털에서 발급 (앱키에 계좌가 묶여 있다)
    DASHBOARD_PASSWORD                   있으면 데이터를 암호화해 docs/data.enc.json 에 쓴다
    BACKFILL_DAYS                        첫 실행 때 거래내역을 며칠 거슬러 받을지 (기본 90)

조회 전용이다. 주문·환전 API 는 부르지 않으며 MERITZ_READ_ONLY 도 강제로 켠다.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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

log = logging.getLogger("collector")


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
            res = self.c.call(self.cat.get(key), params or {})
        except MeritzError as e:
            self._warn(f"{title or key}: {e}")
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
                res = paginate(self.c, api, {"from": s, "to": s, "sort": "ASC"})
            except MeritzError as e:
                self._warn(f"입출금 내역 {m.iso(s)}: {e}")
                break                         # 다음 실행에서 이 날부터 다시 받는다
            if res["truncated"]:
                self._warn(f"입출금 내역 {m.iso(s)}: {res['note']}")
            out += [m.norm_tx(r, s) for r in res["rows"] if isinstance(r, dict)]
            done = day
            day += timedelta(days=1)
        return out, done

    def realized_domestic(self, start: date, end: date) -> list[dict]:
        rows = []
        for a, b in month_chunks(start, end):
            body = self.call("profit_loss", {"iqry_sta_date": m.ymd(a), "iqry_fin_date": m.ymd(b)},
                             f"국내 실현손익 {a:%Y-%m}")
            rows += [m.norm_realized_day(r) for r in self.rows(body)]
        return rows

    def realized_overseas(self, start: date, end: date) -> list[dict]:
        rows = []
        for a, b in month_chunks(start, end):
            body = self.call("ovs_profit_loss", {"oder_sta_date": m.ymd(a), "oder_fin_date": m.ymd(b),
                                                 "ctcd": "000"}, f"해외 실현손익 {a:%Y-%m}")
            rows += [m.norm_ovs_realized(r) for r in self.rows(body)]
        return rows

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
    backfill = int(os.getenv("BACKFILL_DAYS") or 90)
    col = Collector()

    snap = col.snapshot()
    if not snap["ok"]:
        print("자산평가 조회에 실패해 갱신을 중단합니다:\n  " + "\n  ".join(col.warnings), file=sys.stderr)
        return 1

    # 거래내역 — 마지막으로 끝까지 받은 날 며칠 전부터 오늘까지
    synced = state.get("tx_synced_until")
    tx_start = (date.fromisoformat(synced) - timedelta(days=RESYNC_DAYS) if synced
                else today - timedelta(days=backfill))
    new_tx, done = col.transactions(tx_start, today)
    txs = state.get("transactions", [])
    if done:
        txs = replace_range(txs, new_tx, tx_start, done)
        state["tx_synced_until"] = done.isoformat()

    # 실현손익 — 첫 실행은 백필 기간 전부, 이후는 지난달 1일부터
    rl_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1) if synced else tx_start
    dom = col.realized_domestic(rl_start, today)
    ovs = col.realized_overseas(rl_start, today)
    if not any("국내 실현손익 " in w for w in col.warnings):
        state["realized_domestic"] = replace_range(state.get("realized_domestic", []), dom, rl_start, today)
    if not any("해외 실현손익 " in w for w in col.warnings):
        state["realized_overseas"] = replace_range(state.get("realized_overseas", []), ovs, rl_start, today)

    # 일별 스냅샷 — 같은 날 다시 돌리면 덮어쓴다
    v = snap["valuation"]
    unrealized = sum(h["pnl"] for h in snap["holdings"])
    point = {"date": today.isoformat(), "total": v["total"], "stock": v["stock"],
             "cash": v["cash"], "fx_cash": v["fx_cash"], "loan": v["loan"],
             "cost": v["cost"], "unrealized": unrealized}
    snaps = [s for s in state.get("history", []) if s["date"] != point["date"]] + [point]
    usd = next((c["fx_rate"] for c in snap["currencies"] if c["currency"] == "USD"), 0.0)
    history = m.compute_history(snaps, m.flows_by_date(txs, usd))

    state.update({
        "version": 1,
        "updated_at": now.isoformat(timespec="seconds"),
        "valuation": v,
        "deposit": snap["deposit"],
        "holdings": snap["holdings"],
        "currencies": snap["currencies"],
        "realized_ytd": col.realized_summary(today.replace(month=1, day=1), today),
        "history": history,
        "transactions": txs,
        "warnings": col.warnings,
    })
    save_state(state, password)
    print(f"{today} 총자산 {v['total']:,.0f}원 · 보유 {len(snap['holdings'])}종목 · "
          f"거래내역 {len(txs)}건 · 스냅샷 {len(history)}일"
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
