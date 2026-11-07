"""用 Guitar Pro / MusicXML 文件制作一首评测用的曲子（标准答案 = 文件里的精确数据）。

流程：用 alphaTab 在无头 Chrome 里把谱子渲染成五线谱 + 六线谱的图片（和 GP 导出的谱子截图差不多），
按真实上传的流程做版面检测，再把文件解析出的小节按检测到的谱行分配进去。
版面检测找到的小节数和文件对不上时直接报错——这本身说明版面检测在这种排版上有问题。

用法：
    uv run --with websockets python scripts/make_eval_song.py 曲子.gp5 eval-xxx [--title 名字] [--per-page 4]
然后：
    uv run python -m app.evaluate eval-xxx --yes

需要：Google Chrome、node（用 alphaTab 解析文件）。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import functools
import http.server
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import pipeline, store
from app.layout import detect_page

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0;background:#fff} #score{width:1100px;padding:24px 30px}</style>
<script type="module">
import * as alphaTab from './alphatab/alphaTab.min.mjs';
const api = new alphaTab.AlphaTabApi(document.getElementById('score'), {
  core: { fontDirectory: './alphatab/font/', engine: 'svg', enableLazyLoading: false },
  display: { layoutMode: 'Page', staveProfile: 'ScoreTab', scale: 1.0 },
  player: { playerMode: 'Disabled' },
});
api.renderFinished.on(() => { window.done = true; });
api.load(new Uint8Array(await (await fetch('score.bin')).arrayBuffer()), [TRACK]);
</script></head><body><div id="score"></div></body></html>"""

DERIVE = """
import * as at from '{root}/app/static/vendor/alphatab/alphaTab.core.min.mjs';
import {{ deriveSong }} from '{root}/app/static/js/score.js';
import fs from 'node:fs';
at.Logger.logLevel = at.LogLevel.None;
const sc = at.importer.ScoreLoader.loadScoreFromBytes(new Uint8Array(fs.readFileSync(process.argv[1])), new at.Settings());
let track = {track};
if (track < 0) track = Math.max(0, sc.tracks.findIndex(t => t.staves[0] && t.staves[0].tuning.length === 6));
process.stdout.write(JSON.stringify({{ track, ...deriveSong(sc, track) }}));
"""


def derive(path: Path, track: int) -> dict:
    code = DERIVE.format(root=ROOT, track=track)
    out = subprocess.run(
        ["node", "--input-type=module", "-e", code, str(path)], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


def _start_chrome(port: int) -> tuple[subprocess.Popen, str]:
    prof = tempfile.mkdtemp()
    args = [
        CHROME,
        "--headless=new",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={prof}",
        "--no-proxy-server",
    ]
    proc = subprocess.Popen([*args, "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
            return proc, next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
        except OSError:
            time.sleep(0.2)
    proc.terminate()
    raise SystemExit("Chrome 启动失败")


async def _screenshot(ws_url: str, url: str, scale: float) -> Image.Image:
    import websockets

    async with websockets.connect(ws_url, max_size=300_000_000) as ws:
        mid = 0

        async def send(method, **params):
            nonlocal mid
            mid += 1
            me = mid
            await ws.send(json.dumps({"id": me, "method": method, "params": params}))
            while True:
                d = json.loads(await ws.recv())
                if d.get("id") == me:
                    return d.get("result", {})

        await send(
            "Emulation.setDeviceMetricsOverride",
            width=1160,
            height=1000,
            deviceScaleFactor=scale,
            mobile=False,
        )
        await send("Page.navigate", url=url)
        for _ in range(150):
            r = await send("Runtime.evaluate", expression="window.done===true", returnByValue=True)
            if r["result"].get("value"):
                break
            await asyncio.sleep(0.2)
        else:
            raise SystemExit("alphaTab 渲染超时")
        await asyncio.sleep(1.0)
        r = await send(
            "Runtime.evaluate",
            expression="[document.documentElement.scrollWidth, document.documentElement.scrollHeight]",
            returnByValue=True,
        )
        w, h = r["result"]["value"]
        shot = await send(
            "Page.captureScreenshot",
            format="png",
            captureBeyondViewport=True,
            clip={"x": 0, "y": 0, "width": w, "height": h, "scale": 1},
        )
        return Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")


def render(path: Path, track: int, scale: float) -> Image.Image:
    web = Path(tempfile.mkdtemp())
    (web / "alphatab").symlink_to(ROOT / "app/static/vendor/alphatab")
    shutil.copy(path, web / "score.bin")
    (web / "index.html").write_text(PAGE.replace("TRACK", str(track)))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(web))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc, ws_url = _start_chrome(9335)
    try:
        return asyncio.run(_screenshot(ws_url, f"http://127.0.0.1:{srv.server_address[1]}/index.html", scale))
    finally:
        proc.terminate()
        srv.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("song_id", help="曲库里的 id，如 eval-romance（已存在会覆盖）")
    ap.add_argument("--title", default="")
    ap.add_argument("--track", type=int, default=-1, help="音轨序号，默认第一条六弦音轨")
    ap.add_argument("--per-page", type=int, default=4, help="每页几行谱")
    ap.add_argument("--scale", type=float, default=1.5, help="渲染倍率（1.5 ≈ 1800 像素宽）")
    a = ap.parse_args()

    truth = derive(a.file, a.track)
    full = render(a.file, truth["track"], a.scale)
    systems = detect_page(full)["systems"]
    if not systems:
        raise SystemExit("整张谱子上没检测到六线谱")
    cuts = [0] + [systems[i]["top"] for i in range(a.per_page, len(systems), a.per_page)] + [full.height]
    pages = []
    for k in range(len(cuts) - 1):
        buf = io.BytesIO()
        full.crop((0, cuts[k], full.width, cuts[k + 1])).save(buf, "PNG")
        pages.append((f"page{k + 1}.png", buf.getvalue()))

    pipeline.start_parse = lambda sid: None  # 只建曲子，不调用模型
    sid = pipeline.create_song(pages, title=a.title or truth["meta"].get("title") or a.song_id)
    song = store.load(sid)
    planned = pipeline._plan_systems(sid, song)
    counts = [len(s["spans"] or []) for s in planned]
    if sum(counts) != len(truth["bars"]):
        shutil.rmtree(store.SONGS / sid)
        raise SystemExit(
            f"版面检测找到 {sum(counts)} 个小节（每行 {counts}），文件里有 {len(truth['bars'])} 个"
        )

    meta = pipeline.normalize_meta({**truth["meta"], "title": a.title or truth["meta"].get("title", "")})
    bars, k = [], 0
    for si, s in enumerate(planned):
        s["status"] = "done"
        for x_a, x_b in s["spans"]:
            tb = truth["bars"][k]
            events = pipeline.clean_events(tb["events"])
            bars.append(
                {
                    "id": pipeline.new_bar_id(),
                    "n": k + 1,
                    "system": si,
                    "page": s["page"],
                    "region": [max(0, x_a - 8), x_b + 8, s["crop"][2], s["crop"][3]],
                    "events": events,
                    "tip": "",
                    "uncertain": "",
                    "warn": pipeline.check_bar(events, meta),
                    **pipeline.clean_repeats(tb),
                }
            )
            k += 1

    def apply(x):
        x.update(meta=meta, systems=planned, bars=bars, status="ready", error="", progress={})

    store.update(sid, apply)
    dst = store.SONGS / a.song_id
    if dst.exists():
        shutil.rmtree(dst)
    (store.SONGS / sid).rename(dst)
    data = json.loads((dst / "song.json").read_text())
    data["id"] = a.song_id
    (dst / "song.json").write_text(json.dumps(data, ensure_ascii=False))
    notes = sum(len(e["notes"]) for b in bars for e in b["events"])
    print(
        f"已生成 {a.song_id}：{len(pages)} 页、{len(planned)} 行、{len(bars)} 小节、{notes} 个音，每行小节数 {counts}"
    )


if __name__ == "__main__":
    main()
