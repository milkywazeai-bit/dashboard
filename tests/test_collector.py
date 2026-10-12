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
    assert len(state["history"]) == weekdays
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
    assert len(decrypt(env, "secret")["history"]) == 2


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
    assert len(state["history"]) == 2

    # 다음 날에는 과거 구간을 다시 받지 않는다
    clients = simulate(date(2026, 10, 14), date(2026, 10, 14), tmp_path)
    tx_days = [p["from"] for k, p in clients[-1].calls if k == "transactions"]
    assert min(tx_days) >= "20261010"
