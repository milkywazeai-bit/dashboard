import json
from datetime import date

import pytest

from collector import collect
from collector import metrics as m
from collector.crypto import decrypt, encrypt
from tests.simulate import simulate


def test_num_parses_api_strings():
    assert m.num("2,860") == 2860
    assert m.num("-12.50000") == -12.5
    assert m.num("") == 0 and m.num(None) == 0 and m.num("0.000") == 0


def test_flow_signs_and_exclusions():
    base = {"date": "2026-07-01", "name": "이체입금", "io": "1", "cancelled": False,
            "foreign": False, "amount": 1000.0, "frcr_amount": 0.0}
    assert m.tx_flow({**base, "type": "01"}) == 1000
    assert m.tx_flow({**base, "type": "02"}) == -1000
    assert m.tx_flow({**base, "type": "05"}) == 0                     # 매수는 원금 변동이 아니다
    assert m.tx_flow({**base, "type": "01", "cancelled": True}) == 0
    assert m.tx_flow({**base, "type": "02", "name": "환전출금"}) == 0
    assert m.tx_flow({**base, "type": "01", "foreign": True, "frcr_amount": 10.0}, 1400) == 14000


def test_twr_ignores_deposits():
    snaps = [{"date": "2026-07-01", "total": 1000.0},
             {"date": "2026-07-02", "total": 1600.0},     # 500 입금 + 10% 상승(100)
             {"date": "2026-07-03", "total": 1600.0}]
    h = m.compute_history(snaps, {"2026-07-02": 500.0})
    assert h[1]["daily_ret"] == pytest.approx(10.0)
    assert h[1]["pnl_cum"] == pytest.approx(100.0)
    assert h[2]["twr"] == pytest.approx(10.0)
    assert m.period_return(h, "2026-07-03") == pytest.approx(0.0)


def test_crypto_roundtrip():
    env = encrypt({"a": "한글"}, "pw")
    assert decrypt(env, "pw") == {"a": "한글"}
    with pytest.raises(Exception):
        decrypt(env, "wrong")


def test_holdings_overseas_merge():
    rows = [m.norm_holding({"iscd": "AAPL.OQ", "isnm": "애플", "mksc_cls_name": "해외주식",
                            "rmnd_qty": 0, "rmnd_dqty": "1.5", "vltn_amt": 500000, "byng_amt": 400000})]
    ovs = [m.norm_ovs_holding({"iscd": "AAPL.OQ", "crcd": "USD", "vltn_ert": "5.0", "exrt": "1400"})]
    h = m.merge_holdings(rows, ovs)[0]
    assert h["qty"] == 1.5 and h["currency"] == "USD" and h["local_ret"] == 5.0 and h["value"] == 500000


def test_simulated_runs_incremental(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKFILL_DAYS", "30")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    clients = simulate(date(2026, 7, 1), date(2026, 9, 30), tmp_path)
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))

    weekdays = sum(1 for c in clients)
    assert len([h for h in state["history"] if not h.get("estimated")]) == weekdays
    # 두 번째 실행부터는 마지막 동기화일 근처만 다시 받는다
    tx_days = [p["from"] for k, p in clients[-1].calls if k == "transactions"]
    assert len(tx_days) <= collect.RESYNC_DAYS + 4
    # 같은 거래가 중복으로 쌓이지 않는다
    keys = [(t["date"], t["srno"]) for t in state["transactions"]]
    assert len(keys) == len(set(keys))
    # 입금일 스냅샷의 일간 수익률에 입금액이 섞이지 않는다
    by_date = {h["date"]: h for h in state["history"]}
    assert by_date["2026-07-15"]["flow"] == 2_000_000
    assert abs(by_date["2026-07-15"]["daily_ret"]) < 5
    assert by_date["2026-09-03"]["flow"] == -1_000_000
    assert any(r["code"] == "NVDA.OQ" for r in state["realized_overseas"])
    assert state["holdings"][0]["value"] >= state["holdings"][-1]["value"]


def test_encrypted_state_and_plaintext_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret")
    (tmp_path / "data.json").write_text("{}", "utf-8")
    simulate(date(2026, 9, 28), date(2026, 9, 29), tmp_path, password="secret")
    assert not (tmp_path / "data.json").exists()
    env = json.loads((tmp_path / "data.enc.json").read_text("utf-8"))
    assert len([h for h in decrypt(env, "secret")["history"] if not h.get("estimated")]) == 2


def test_require_password_refuses_plaintext(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(collect, "load_dotenv", lambda: None)
    assert collect.run(require_password=True) == 2


def test_backfill_from_fills_earlier_gap(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("BACKFILL_FROM", raising=False)
    monkeypatch.setenv("BACKFILL_DAYS", "10")
    simulate(date(2026, 10, 12), date(2026, 10, 12), tmp_path)
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))
    assert min(t["date"] for t in state["transactions"]) >= "2026-10-02"
    # 이전 버전 상태처럼 시작일 기록을 지운다
    state.pop("tx_synced_from")
    (tmp_path / "data.json").write_text(json.dumps(state), "utf-8")

    monkeypatch.setenv("BACKFILL_FROM", "2026-01-01")
    clients = simulate(date(2026, 10, 13), date(2026, 10, 13), tmp_path)
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))
    assert state["tx_synced_from"] == "2026-01-01"
    assert min(t["date"] for t in state["transactions"]) < "2026-01-15"
    assert any(r["date"] < "2026-02-01" for r in state["realized_domestic"])
    keys = [(t["date"], t["srno"]) for t in state["transactions"]]
    assert len(keys) == len(set(keys))
    assert len([h for h in state["history"] if not h.get("estimated")]) == 2
    # 거래내역 시작일이 앞당겨졌으니 추정 구간도 다시 만들어졌다
    assert state["estimate"]["from"] < "2026-01-15"

    # 다음 날에는 과거 구간을 다시 받지 않는다
    clients = simulate(date(2026, 10, 14), date(2026, 10, 14), tmp_path)
    tx_days = [p["from"] for k, p in clients[-1].calls if k == "transactions"]
    assert min(tx_days) >= "20261010"


def _tx(d, srno, type_, code="", qty=0.0, price=0.0, balance=0.0, foreign=False, frcr_balance=0.0):
    return {"date": d, "srno": srno, "type": type_, "name": "", "code": code, "isnm": "", "qty": qty,
            "price": price, "amount": 0.0, "frcr_amount": 0.0, "foreign": foreign, "fee": 0.0, "tax": 0.0,
            "io": "", "cancelled": False, "balance": balance, "frcr_balance": frcr_balance}


def test_estimate_history_rebuilds_positions_and_cash():
    txs = [_tx("2026-01-02", 1, "01", balance=1_000_000),                         # 100만 입금
           _tx("2026-01-05", 1, "05", "A005930", 10, 50_000, balance=500_000),    # 10주 매수
           _tx("2026-01-07", 1, "06", "A005930", 4, 60_000, balance=740_000),     # 4주 매도
           _tx("2026-01-07", 2, "05", "AAPL.OQ", 1, 200, foreign=True, frcr_balance=0)]
    days = ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    prices = {"A005930": {"2026-01-05": 50_000, "2026-01-06": 55_000, "2026-01-07": 60_000},
              "AAPL.OQ": {"2026-01-06": 200, "2026-01-07": 210}}
    fx = {"2026-01-07": 1400}
    pts, diag = m.estimate_history(txs, {"A005930": 6, "AAPL.OQ": 1}, days, prices, fx, {}, 1400, 0)
    by = {p["date"]: p for p in pts}
    assert "2026-01-01" not in by                         # 첫 잔액 이전은 추정하지 않는다
    assert by["2026-01-02"]["total"] == 1_000_000
    assert by["2026-01-05"]["total"] == 500_000 + 10 * 50_000
    assert by["2026-01-06"]["total"] == 500_000 + 10 * 55_000
    # 해외 종가는 한국 날짜 기준 전날 것을 쓴다 (1/6 종가 200)
    assert by["2026-01-07"]["total"] == 740_000 + 6 * 60_000 + 200 * 1400
    assert diag["negative_qty"] == []


def test_estimate_flags_inconsistent_holdings():
    txs = [_tx("2026-01-05", 1, "05", "A000660", 5, 100, balance=10)]   # 잔고에 없는데 매수 기록만 있다
    _, bad = m.positions_by_day(txs, {}, ["2026-01-02"])
    assert bad == {"A000660"}


def test_simulated_estimate_before_first_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("BACKFILL_FROM", "2026-06-01")
    simulate(date(2026, 10, 12), date(2026, 10, 12), tmp_path)
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))
    est = [h for h in state["history"] if h.get("estimated")]
    real = [h for h in state["history"] if not h.get("estimated")]
    assert len(real) == 1 and real[0]["date"] == "2026-10-12"
    assert est and est[0]["date"] <= "2026-06-05" and est[-1]["date"] == "2026-10-09"
    chk = state["estimate"]["check"]
    assert chk["negative_qty"] == [] and abs(chk["error_pct"]) < 3
    # 입금(7/15)·출금(9/3)은 수익률에서 빠진다
    by = {h["date"]: h for h in state["history"]}
    assert by["2026-07-15"]["flow"] == 2_000_000 and abs(by["2026-07-15"]["daily_ret"]) < 5

    # 다음 날은 추정을 다시 만들지 않는다
    clients = simulate(date(2026, 10, 13), date(2026, 10, 13), tmp_path)
    assert not any(k in ("market_candles_days", "fx_rates") for k, _ in clients[-1].calls)
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))
    assert [h["date"] for h in state["history"] if not h.get("estimated")] == ["2026-10-12", "2026-10-13"]


def test_transient_errors_are_retried_and_incomplete_estimate_rebuilt(tmp_path, monkeypatch):
    from meritz import MeritzError
    from tests.fake_api import FakeClient

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("BACKFILL_FROM", "2026-09-01")
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)

    # 일봉 첫 호출이 끊긴다 → 재시도로 성공해야 한다
    fails = {"market_candles_days": 1, "transactions": 1}
    orig = FakeClient.call

    def flaky(self, api, params, _retried=False):
        if fails.get(api["key"]):
            fails[api["key"]] -= 1
            raise MeritzError("연결 끊김", code="UNREACHABLE")
        return orig(self, api, params)

    monkeypatch.setattr(FakeClient, "call", flaky)
    simulate(date(2026, 10, 12), date(2026, 10, 12), tmp_path)
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))
    assert state["warnings"] == [] and state["estimate"]["complete"] is True

    # 환율이 계속 실패하면 추정은 미완성으로 남고, 다음 실행에서 다시 만든다
    always = {"fx_rates"}

    def broken(self, api, params, _retried=False):
        if api["key"] in always:
            raise MeritzError("연결 끊김", code="UNREACHABLE")
        return orig(self, api, params)

    monkeypatch.setattr(FakeClient, "call", broken)
    monkeypatch.setenv("REBUILD_ESTIMATE", "1")
    simulate(date(2026, 10, 13), date(2026, 10, 13), tmp_path)
    monkeypatch.delenv("REBUILD_ESTIMATE")
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))
    assert state["estimate"]["complete"] is False

    always.clear()
    clients = simulate(date(2026, 10, 14), date(2026, 10, 14), tmp_path)
    assert any(k == "fx_rates" for k, _ in clients[-1].calls)
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))
    assert state["estimate"]["complete"] is True


def test_inaccurate_estimate_is_not_used(tmp_path, monkeypatch):
    assert m.estimate_usable({"error_pct": 1.2, "negative_qty": []})
    assert not m.estimate_usable({"error_pct": 11.48, "negative_qty": []})
    assert not m.estimate_usable({"error_pct": 0.1, "negative_qty": ["SOXL.AX"]})

    # 잔고에 없는 매수 기록 + 모르는 유형의 행 → 진단에 유형별로 드러난다
    txs = [_tx("2026-02-03", 1, "05", "SOXL.AX", 10, 20, foreign=True),
           _tx("2026-03-03", 1, "99", "SOXL.AX", 4, 25, foreign=True)]
    _, diag = m.estimate_history(txs, {"SOXL.AX": 6}, ["2026-02-02"], {}, {}, {}, 1400, 0)
    assert diag["negative_qty"] == ["SOXL.AX"]
    g = {x["type"]: x for x in diag["codes"][0]["groups"]}
    assert g["05"]["counted"] and not g["99"]["counted"] and diag["codes"][0]["current_qty"] == 6

    # 수집기: 정확도가 떨어지는 추정은 기록 앞에 붙이지 않는다
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("BACKFILL_FROM", "2026-09-01")
    monkeypatch.setattr(m, "ESTIMATE_MAX_ERROR_PCT", -1.0)
    simulate(date(2026, 10, 12), date(2026, 10, 12), tmp_path)
    state = json.loads((tmp_path / "data.json").read_text("utf-8"))
    assert state["estimate"]["points"] and state["estimate"]["used"] is False
    assert not any(h.get("estimated") for h in state["history"])
