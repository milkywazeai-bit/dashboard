"""메리츠 API 를 흉내 내는 가짜 호출기. 테스트와 샘플 데이터(scripts/make_sample.py)에 쓴다.

응답 모양은 공식 카탈로그(meritz/data/catalog.json)의 필드명을 따른다.
"""
from __future__ import annotations

import math
from datetime import date, datetime


class FakeClient:
    """ApiClient.call 과 같은 모양({"ok", "body"})을 돌려준다. day 로 '오늘'의 시세를 바꾼다."""

    def __init__(self, day: date, deposits: dict[str, int] | None = None):
        self.day = day
        self.deposits = deposits or {}          # "YYYYMMDD" → 입금액(음수는 출금)
        self.calls: list[tuple[str, dict]] = []

    def _price(self, base: float, drift: float, vol: float, seed: int) -> float:
        t = (self.day - date(2026, 1, 1)).days
        return base * (1 + drift * t / 1000 + vol * math.sin(t / 7 + seed))

    def call(self, api, params: dict, _retried: bool = False) -> dict:
        key = api["key"]
        self.calls.append((key, dict(params)))
        data = getattr(self, "_" + key)(params)
        body = {"rsp_cd": "5766", "rsp_msg": "조회가 완료되었습니다.", "tr_cont": "0", "data": data}
        if isinstance(data, tuple):                      # (data, summary)
            body["data"], body["summary"] = data
        return {"ok": True, "http_status": 200, "api_type": key, "body": body}

    # ── 현재 상태 ──
    def _positions(self):
        sam = self._price(71000, 0.4, 0.05, 0)
        hynix = self._price(180000, 0.8, 0.08, 1)
        aapl = self._price(230, 0.3, 0.04, 2)
        usd = 1380 + 20 * math.sin((self.day - date(2026, 1, 1)).days / 9)
        return [
            ("A005930", "삼성전자", "주식일반", 120, 68500, sam, sam, 1.0),
            ("A000660", "SK하이닉스", "주식일반", 15, 172000, hynix, hynix, 1.0),
            ("AAPL.OQ", "애플", "해외주식", 10, 210.5, aapl, aapl * usd, usd),
        ]

    def _valuation(self, p):
        stock = sum(qty * krw for _c, _n, _k, qty, _avg, _px, krw, _fx in self._positions())
        cash = 3_500_000 + sum(v for d, v in self.deposits.items() if d <= self.day.strftime("%Y%m%d"))
        fx = 420.37 * self._positions()[2][7]
        cost = 120 * 68500 + 15 * 172000 + 10 * 210.5 * 1350
        return {"tfam": cash, "d2_aft_tfam": cash, "mksc_vltn_amt": round(stock), "loan_amt": 0,
                "vltn_amt": round(cash + stock + fx), "wtmn_able_cash_amt": cash,
                "byng_amt": round(cost), "aset_vltn_amt": round(cash + stock + fx), "frcr_tfam": round(fx)}

    def _deposit(self, p):
        v = self._valuation(p)
        return {"tfam": v["tfam"], "d1_aft_tfam": v["tfam"], "d2_aft_tfam": v["tfam"],
                "drng_able_amt": v["tfam"], "oder_able_cash_amt": v["tfam"], "cash_unam": 0,
                "crdt_loan_amt": 0}

    def _holdings(self, p):
        rows = []
        for code, name, kind, qty, avg, px, krw, fx in self._positions():
            cost = qty * avg * (1350 if kind == "해외주식" else 1)
            val = qty * krw
            rows.append({"iscd": code, "isnm": name, "mksc_cls_name": kind,
                         "pros_trgt_cls_code": "J" if kind == "해외주식" else "1",
                         "rmnd_qty": qty, "rmnd_dqty": "0.0000000000",
                         "byng_unpr": f"{avg:.5f}", "psnt_prc": f"{px:.5f}", "prdy_last_prc": f"{px:.8f}",
                         "vltn_amt": round(val), "byng_amt": round(cost), "vltn_prls_amt": round(val - cost),
                         "ert": f"{(val / cost - 1) * 100:.5f}", "loan_amt": 0})
        return rows

    def _ovs_holdings(self, p):
        code, name, kind, qty, avg, px, krw, fx = self._positions()[2]
        return [{"iscd": code, "isnm": name, "crcd": "USD", "ovrs_stck_symb_iscd": "AAPL",
                 "rmnd_dqty": f"{qty:.6f}", "avrg_unpr": f"{avg:.4f}", "psnt_prc": f"{px:.4f}",
                 "byov_camt": f"{qty * avg:.3f}", "frcr_vltn_amt": f"{qty * px:.3f}",
                 "vltn_prls_camt": f"{qty * (px - avg):.3f}", "vltn_ert": f"{(px / avg - 1) * 100:.4f}",
                 "exrt": f"{fx:.4f}"}]

    def _ovs_currencies(self, p):
        fx = self._positions()[2][7]
        return [{"crcd": "USD", "frcr_amt": "420.37", "estm_frcr_amt": "420.37",
                 "vltn_exrt": f"{fx:.4f}", "excl_wncr_vltn_amt": round(420.37 * fx),
                 "ovrs_stck_vltn_amt": round(self._holdings(p)[2]["vltn_amt"])}]

    # ── 기간 조회 ──
    def _cash_on(self, d: str) -> float:
        return 3_500_000 + sum(v for k, v in self.deposits.items() if k <= d)

    def _transactions(self, p):
        d = p["from"]
        rows = []
        if d in self.deposits:
            amt = self.deposits[d]
            rows.append({"tran_srno": 1, "tr_type_code": "01" if amt > 0 else "02",
                         "smcd_name": "이체입금" if amt > 0 else "이체출금", "io_cls_code": "1" if amt > 0 else "2",
                         "if_fee_excl_amt": f"{abs(amt):,}", "cncl_yn": "N", "issu_type_code": "23",
                         "frcr_tr_amt": "0.000", "tfam_nwrm": self._cash_on(d), "frcr_tfam_nwrm": "0.000",
                         "if_tr_qty": "0", "byng_unpr": "0"})
        dt = datetime.strptime(d, "%Y%m%d").date()
        if dt.weekday() == 2:                            # 수요일마다 삼성전자 매매
            buy = dt.isocalendar().week % 2 == 0
            rows.append({"tran_srno": 2, "tr_type_code": "05" if buy else "06",
                         "smcd_name": "주식매수" if buy else "주식매도", "io_cls_code": "2" if buy else "1",
                         "iscd": "A005930", "kor_isnm": "삼성전자", "if_tr_qty": "5",
                         "byng_unpr": "71,200", "if_fee_excl_amt": "356,000", "deal_fee": 53,
                         "trtx": 0 if buy else 641, "cncl_yn": "N", "issu_type_code": "02",
                         "frcr_tr_amt": "0.000", "tfam_nwrm": self._cash_on(d), "frcr_tfam_nwrm": "0.000"})
        return rows

    def _profit_loss(self, p):
        out = []
        a = datetime.strptime(p["iqry_sta_date"], "%Y%m%d").date()
        b = datetime.strptime(p["iqry_fin_date"], "%Y%m%d").date()
        for n in range((b - a).days + 1):
            dt = date.fromordinal(a.toordinal() + n)
            if dt.weekday() == 2 and dt.isocalendar().week % 2 == 1:
                pnl = 12000 if dt.day % 3 else -8000
                out.append({"deal_date": dt.strftime("%Y%m%d"), "byov_amt": 0, "seln_amt": 356000,
                            "deal_fee": 53, "wtax_amt": 641, "prls_amt": pnl, "plrt": round(pnl / 3440, 2)})
        return out

    def _ovs_profit_loss(self, p):
        rows = []
        if p["oder_sta_date"][:6] == "202609":
            rows.append({"oder_date": "20260915", "iscd": "NVDA.OQ", "kor_issu_abwr_name": "엔비디아",
                         "crcd": "USD", "seln_qty": 3, "wncr_seln_amt": 560000, "wncr_byov_amt": 480000,
                         "rlzt_prls_amt": 78000, "wncr_fed_prls_amt": 4000, "frcr_rlzt_ernn_amt": 54.1,
                         "frcr_plrt": 15.2})
        return rows, {"rlzt_prls_amt": sum(r["rlzt_prls_amt"] for r in rows)}

    def _profit_loss_summary(self, p):
        rows = self._profit_loss(p)
        return {"seln_amt": sum(r["seln_amt"] for r in rows), "byov_amt": 0,
                "deal_fee": sum(r["deal_fee"] for r in rows), "wtax_amt": sum(r["wtax_amt"] for r in rows),
                "prls_amt": sum(r["prls_amt"] for r in rows), "plrt": 1.2}

    # ── 시세 ──
    def _at(self, d: str) -> "FakeClient":
        return FakeClient(datetime.strptime(d, "%Y%m%d").date(), self.deposits)

    def _days(self, p):
        a = datetime.strptime(p["from"], "%Y%m%d").date()
        b = datetime.strptime(p["to"], "%Y%m%d").date()
        return [date.fromordinal(a.toordinal() + n) for n in range((b - a).days + 1)
                if date.fromordinal(a.toordinal() + n).weekday() < 5]

    def _market_candles_days(self, p):
        idx = {"005930": 0, "000660": 1}[p["iscd"]]
        return [{"date": d.strftime("%Y%m%d"),
                 "stck_clpr": str(round(FakeClient(d)._positions()[idx][5]))} for d in self._days(p)][::-1]

    def _ovs_market_candles_days(self, p):
        return [{"date": d.strftime("%Y%m%d"), "prpr": f"{FakeClient(d)._positions()[2][5]:.4f}"}
                for d in self._days(p)][::-1]

    def _fx_rates(self, p):
        d = datetime.strptime(p["stnd_date"], "%Y%m%d").date()
        return [{"crcd": "USD", "deal_stnd_exrt": f"{FakeClient(d)._positions()[2][7]:.4f}"}]
