"""JS → Python 金标准：浏览器（score.js 的 deriveSong）从 Guitar Pro 文件转换出的数据，后端原样接收。
tests/fixtures/golden.gp 和 golden_derived.json 由 tests/js/fixture.test.mjs 生成并校验。"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app import main, model, songs, store

FIX = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((FIX / "golden_derived.json").read_text(encoding="utf-8"))


def test_backend_accepts_the_golden_derived_song(data_dir):
    sid = songs.create_song([("golden.gp", (FIX / "golden.gp").read_bytes())])
    c = TestClient(main.app, base_url="http://127.0.0.1:8765")
    body = {
        "meta": GOLDEN["meta"],
        "bars": GOLDEN["bars"],
        "track": 0,
        "derived": {"version": GOLDEN["version"], "alphatab": "test"},
    }
    r = c.put(f"/api/songs/{sid}/derived", json=body)
    assert r.status_code == 200, r.text
    v = r.json()
    model.SongView.model_validate(v)
    assert v["status"] == "ready" and v["source_type"] == "gp" and v["title"] == "golden"  # 曲名用文件名
    assert v["meta"]["tuning"] == GOLDEN["meta"]["tuning"] and v["meta"]["capo"] == 2
    assert len(v["bars"]) == len(GOLDEN["bars"])
    assert all(not b["warn"] for b in v["bars"]), [b["warn"] for b in v["bars"]]
    for got, want in zip(v["bars"], GOLDEN["bars"], strict=True):
        assert got["events"] == want["events"]  # 时值、音符、技巧、倚音都原样保存
        for k in ("time_signature", "repeat_start", "repeat_end", "endings", "directions"):
            assert got.get(k) == want.get(k), k
    # 同一份数据再转换一次（比如 alphaTab 升级）：小节对得上，id 不变
    ids = [b["id"] for b in v["bars"]]
    v2 = c.put(f"/api/songs/{sid}/derived", json=body).json()
    assert [b["id"] for b in v2["bars"]] == ids
    assert store.load(sid)["derived"]["version"] == GOLDEN["version"]
