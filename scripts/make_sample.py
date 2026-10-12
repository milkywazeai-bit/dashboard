"""가짜 API 로 docs/sample.json(데모 데이터)을 만든다. 실제 계좌와 무관하다.

    python scripts/make_sample.py
"""
import os
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop("DASHBOARD_PASSWORD", None)
os.environ["BACKFILL_FROM"] = "2026-01-01"   # 1~8월은 추정, 9월부터 실제 기록

from tests.simulate import simulate  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    simulate(date(2026, 9, 1), date(2026, 10, 12), Path(tmp))
    shutil.copy(Path(tmp) / "data.json", ROOT / "docs" / "sample.json")
print("docs/sample.json 을 만들었습니다.")
