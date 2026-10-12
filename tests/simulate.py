"""가짜 API 로 수집기를 여러 날 돌려 본다. 테스트와 샘플 데이터 생성에 쓴다."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from collector import collect
from tests.fake_api import FakeClient

DEPOSITS = {"20260715": 2_000_000, "20260903": -1_000_000}


def simulate(start: date, end: date, out_dir: Path, password: str | None = None) -> list[FakeClient]:
    collect.PLAIN, collect.ENC = out_dir / "data.json", out_dir / "data.enc.json"
    clients, day = [], start
    orig_client, orig_now = collect.client, collect.m.now_kst
    try:
        while day <= end:
            if day.weekday() < 5:
                fake = FakeClient(day, DEPOSITS)
                clients.append(fake)
                collect.client = lambda f=fake: f
                collect.m.now_kst = lambda d=day: datetime(d.year, d.month, d.day, 16, 40,
                                                           tzinfo=ZoneInfo("Asia/Seoul"))
                rc = collect.run(require_password=bool(password))
                assert rc == 0, f"{day} 수집 실패"
            day += timedelta(days=1)
    finally:
        collect.client, collect.m.now_kst = orig_client, orig_now
    return clients
