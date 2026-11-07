"""用 Guitar Pro / MusicXML 文件制作一首评测用的曲子（标准答案 = 文件里的精确数据）。

流程：用 alphaTab 在无头 Chrome 里把谱子渲染成图片（默认五线谱 + 六线谱，和 GP 导出的谱子截图差不多），
按真实上传的流程做版面检测，再把文件解析出的小节按检测到的谱行分配进去。
版面检测找到的小节数和文件对不上时直接报错——这本身说明版面检测在这种排版上有问题。

用法：
    uv run --with websockets python scripts/make_eval_song.py 曲子.gp5 eval-xxx [--title 名字] [--per-page 4]
        [--stave score-tab|tab|tab-below] [--width 1100] [--bars-per-row N] [--scale 1.5]
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

from app import music, recognize, songs, store
from app.layout import detect_page

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0;background:#fff} #score{width:WIDTHpx;padding:24px 30px}</style>
<script type="module">
import * as alphaTab from './alphatab/alphaTab.min.mjs';
const api = new alphaTab.AlphaTabApi(document.getElementById('score'), {
  core: { fontDirectory: './alphatab/font/', engine: 'svg', enableLazyLoading: false },
  ...SETTINGS,
  player: { playerMode: 'Disabled' },
});
api.postRenderFinished.on(() => {
  window.rows = api.boundsLookup.staffSystems.map(s => s.bars.length);
  window.done = true;
});
api.load(new Uint8Array(await (await fetch('score.bin')).arrayBuffer()), [TRACK]);
</script></head><body><div id="score"></div></body></html>"""
# 谱面样式（--stave）：
#   score-tab  五线谱 + 六线谱（GP 导出的样子），六线谱上不画节奏
#   tab        只有六线谱，节奏画在六线谱上（alphaTab 自己的画法：符干从音符处往下画，穿过下面的弦线）
#   tab-below  只有六线谱，符干只挂在最低弦下方、不穿过弦线
_TAB = {"display": {"staveProfile": "Tab"}, "notation": {"rhythmMode": "ShowWithBars"}}
STAVES = {"score-tab": {"display": {"staveProfile": "ScoreTab"}}, "tab": _TAB, "tab-below": _TAB}
# tab-below：alphaTab 没有这个选项，渲染时改一份临时的 alphaTab 副本（六线谱的符干朝下）：
# 起点改到最低弦下方，并且画成一整段（原来是在各个品格数字之间分段画，会穿过弦线）
_STEM_PATCHES = [
    (
        "getFlagTopY(t,e){const s=t.maxStringNote,i=0===e?0:6;",
        (
            "getFlagTopY(t,e){if(1===e)return this.getLineY(this.drawnLineCount-1)+.6*this.lineSpacing;"
            "const s=t.maxStringNote,i=0===e?0:6;"
        ),
    ),
    (
        "const a=yh.applyBeat(r,7,t);try{let a=[];",
        "const a=yh.applyBeat(r,7,t);try{return void r.fillRect(s,i,this.smuflMetrics.stemThickness,n-i);let a=[];",
    ),
]


def alphatab_settings(stave: str, bars_per_row: int, hide: list[str]) -> dict:
    s = json.loads(json.dumps(STAVES[stave]))
    s["display"].update(layoutMode="Page", scale=1.0, barsPerRow=bars_per_row)
    if hide:
        s.setdefault("notation", {})["elements"] = dict.fromkeys(hide, False)
    return s


def _alphatab_dir(web: Path, stems_below: bool) -> None:
    vendor = ROOT / "app/static/vendor/alphatab"
    if not stems_below:
        (web / "alphatab").symlink_to(vendor)
        return
    (web / "alphatab").mkdir()
    for f in vendor.iterdir():
        (web / "alphatab" / f.name).symlink_to(f)
    core = web / "alphatab/alphaTab.core.min.mjs"
    code = core.read_text()
    for old, new in _STEM_PATCHES:
        if code.count(old) != 1:
            raise SystemExit(
                "alphaTab 版本变了，tab-below 的补丁对不上（scripts/make_eval_song.py 里的 _STEM_PATCHES）"
            )
        code = code.replace(old, new)
    core.unlink()
    core.write_text(code)


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


async def _screenshot(ws_url: str, url: str, scale: float, width: int) -> tuple[Image.Image, list[int]]:
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
            width=width + 60,
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
        rows = (await send("Runtime.evaluate", expression="window.rows", returnByValue=True))["result"][
            "value"
        ]
        return Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB"), rows


def render(
    path: Path,
    track: int,
    scale: float,
    width: int = 1100,
    stave: str = "score-tab",
    bars_per_row: int = -1,
    hide: list[str] | None = None,
) -> tuple[Image.Image, list[int]]:
    web = Path(tempfile.mkdtemp())
    _alphatab_dir(web, stave == "tab-below")
    shutil.copy(path, web / "score.bin")
    settings = alphatab_settings(stave, bars_per_row, hide or [])
    page = (
        PAGE.replace("TRACK", str(track))
        .replace("WIDTH", str(width))
        .replace("SETTINGS", json.dumps(settings))
    )
    (web / "index.html").write_text(page)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    handler = functools.partial(Quiet, directory=str(web))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc, ws_url = _start_chrome(9335)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/index.html"
        return asyncio.run(_screenshot(ws_url, url, scale, width))
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
    ap.add_argument("--stave", choices=list(STAVES), default="score-tab", help="谱面样式，见文件开头的说明")
    ap.add_argument("--width", type=int, default=1100, help="谱面宽度（CSS 像素，图片宽 ≈ (宽度+60)×倍率）")
    ap.add_argument("--bars-per-row", type=int, default=-1, help="每行固定几个小节，默认自动")
    ap.add_argument(
        "--hide",
        action="append",
        default=[],
        metavar="ELEMENT",
        help="不画某种记号，可重复，名字见 alphaTab 的 NotationElement，如 effectLetRing",
    )
    ap.add_argument("--save-full", type=Path, help="另存一份未切页的整张渲染图（排查版面检测用）")
    a = ap.parse_args()

    truth = derive(a.file, a.track)
    full, rows = render(a.file, truth["track"], a.scale, a.width, a.stave, a.bars_per_row, a.hide)
    if a.save_full:
        full.save(a.save_full)
    systems = detect_page(full)["systems"]
    if not systems:
        raise SystemExit("整张谱子上没检测到六线谱")
    cuts = [0] + [systems[i]["top"] for i in range(a.per_page, len(systems), a.per_page)] + [full.height]
    pages = []
    for k in range(len(cuts) - 1):
        buf = io.BytesIO()
        full.crop((0, cuts[k], full.width, cuts[k + 1])).save(buf, "PNG")
        pages.append((f"page{k + 1}.png", buf.getvalue()))

    sid = songs.create_song(pages, title=a.title or truth["meta"].get("title") or a.song_id)
    song = store.load(sid)
    planned = recognize.plan_systems(sid, song)
    counts = [len(s["spans"] or []) for s in planned]
    if sum(counts) != len(truth["bars"]):
        shutil.rmtree(store.SONGS / sid)
        raise SystemExit(
            f"版面检测找到 {sum(counts)} 个小节（每行 {counts}），文件里有 {len(truth['bars'])} 个"
            f"（alphaTab 实际每行 {rows}）"
        )

    meta = music.normalize_meta({**truth["meta"], "title": a.title or truth["meta"].get("title", "")})
    bars, k = [], 0
    for si, s in enumerate(planned):
        s["status"] = "done"
        for x_a, x_b in s["spans"]:
            tb = truth["bars"][k]
            events = music.clean_events(tb["events"])
            ts = music.clean_time_signature(tb.get("time_signature") or meta["time_signature"])
            own = {"time_signature": ts} if ts and ts != meta["time_signature"] else {}
            bars.append(
                {
                    **own,
                    "id": music.new_bar_id(),
                    "n": k + 1,
                    "system": si,
                    "page": s["page"],
                    "region": [max(0, x_a - 8), x_b + 8, s["crop"][2], s["crop"][3]],
                    "events": events,
                    "tip": "",
                    "uncertain": "",
                    "warn": music.check_bar(events, meta, bar=own),
                    **music.clean_repeats(tb),
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
