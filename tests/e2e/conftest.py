"""端到端测试：在临时数据目录上起一个真实的服务，用无界面 Chrome 打开页面操作。

默认不跑（需要 Chrome，也比较慢）：uv run pytest -m e2e
"""

from __future__ import annotations

import io
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from tests.e2e.cdp import Browser, find_chrome, free_port

ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(config, items):
    if not find_chrome():
        skip = pytest.mark.skip(reason="没有找到 Chrome")
        for it in items:
            if "e2e" in it.keywords:
                it.add_marker(skip)


def _seed():
    """一首识别出来的图片曲子（合成的谱子页面，两行各 3 小节）和一个由 alphaTab 导出的 GP 文件。"""
    from app import music, recognize, songs, store
    from tests.test_layout import _score

    buf = io.BytesIO()
    _score().convert("RGB").save(buf, "PNG")
    sid = songs.create_song([("page.png", buf.getvalue())], title="E2E 图片曲")
    song = store.load(sid)
    systems = recognize.plan_systems(sid, song)
    meta = music.normalize_meta({"title": "E2E 图片曲", "tuning": music.STANDARD, "time_signature": [4, 4]})
    bars, n = [], 0
    for si, s in enumerate(systems):
        s["status"] = "done"
        for a, b in s["spans"]:
            n += 1
            events = [
                {
                    "dur": "1",
                    "notes": [
                        {"string": 1 + (n + k) % 6, "fret": (n + k) % 5, "tech": [], "grace": -1, "bend": 0}
                    ],
                }
                for k in range(4)
            ]
            bars.append(
                {
                    "id": f"bar{n}",
                    "n": n,
                    "system": si,
                    "page": s["page"],
                    "region": [a, b, s["crop"][2], s["crop"][3]],
                    "events": events,
                    "tip": f"第 {n} 小节的提示",
                    "uncertain": "",
                    "warn": [],
                }
            )
    plan = {"overview": "慢练", "steps": [{"bars": "1-3", "title": "第一行", "advice": "慢"}]}
    store.update(sid, lambda x: x.update(meta=meta, systems=systems, bars=bars, status="ready", plan=plan))
    return sid


def _gp_bytes() -> bytes:
    """用仓库里的 alphaTab 把一小段 alphaTex 导出成 Guitar Pro 7 文件。"""
    code = """
import * as at from '__ROOT__/app/static/vendor/alphatab/alphaTab.core.min.mjs';
const s = new at.Settings(); const imp = new at.importer.AlphaTexImporter();
imp.initFromString('\\\\title "E2E GP"\\n.\\n\\\\track "Guitar" {{ instrument 25 }}\\n\\\\staff {{score tabs}}\\n0.6.4 2.5.4 2.4.4 1.3.4 | 0.2.2 1.1.2 | 3.1.1', s);
process.stdout.write(Buffer.from(new at.exporter.Gp7Exporter().export(imp.readScore(), s)).toString('base64'));
""".format()
    code = code.replace("__ROOT__", str(ROOT))
    import base64

    out = subprocess.run(
        ["node", "--input-type=module", "-e", code], capture_output=True, text=True, check=True
    )
    return base64.b64decode(out.stdout)


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    import uvicorn

    from app import main, songs, store

    data = tmp_path_factory.mktemp("data")
    mp = pytest.MonkeyPatch()
    mp.setattr(store, "DATA", data)
    mp.setattr(store, "SONGS", data / "songs")
    mp.setattr(store, "CONFIG", data / "config.json")
    image_sid = _seed()
    gp_sid = songs.create_song([("e2e.gp", _gp_bytes())])
    broken_sid = songs.create_song([("broken.gp5", b"\x18FICHIER GUITAR PRO v5.10" + b"\x00garbage" * 20)])

    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/songs")
            break
        except OSError:
            time.sleep(0.1)
    yield {"base": base, "image": image_sid, "gp": gp_sid, "broken": broken_sid, "store": store}
    srv.should_exit = True
    t.join(timeout=10)
    mp.undo()
    shutil.rmtree(data, ignore_errors=True)


@pytest.fixture
def browser():
    with Browser() as b:
        yield b


@pytest.fixture
def trainer(server, browser):
    """打开图片曲子的练习页，等音色和谱面都就绪。"""

    def open_song(sid=None):
        browser.open(f"{server['base']}/trainer?id={sid or server['image']}")
        browser.wait_for(
            "typeof state !== 'undefined' && state.ready && !!document.querySelector('#score .at-surface')",
            timeout=40,
        )
        return browser

    return open_song
