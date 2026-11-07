"""合成一首“弹唱谱”评测曲（标准答案 = 生成时的数据）：谱面上方画和弦名和和弦图，六线谱上分解和弦写成 ×、
扫弦画箭头（也是一叠 ×），下面一行简谱旋律带歌词——和国内常见的弹唱谱排版一样。

和弦、节奏型、旋律、歌词都按随机种子生成；× 的标准答案是和弦图里那根弦的品位，扫弦的标准答案是箭头覆盖的弦，
歌词的标准答案按旋律的节奏对到吉他的拍上（和 app/prompts.py 里给模型的规则一样）。

用法：
    uv run --with websockets python scripts/make_chord_eval.py eval-c-strum --seed 1 --style strum
    uv run python -m app.evaluate eval-c-strum --yes
风格：arp（分解为主）、strum（扫弦为主）、mixed（前半分解、后半扫弦，带几个写出品位的过门音）。
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import io
import json
import random
import shutil
import sys
import tempfile
import threading
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_eval_song import _screenshot, _start_chrome

from app import music, recognize, songs, store
from app.layout import detect_page

# 和弦指法：1–6 弦的品位（-1 = 不弹）。有几个高把位的（谱上会标“Nfr.”）
SHAPES = {
    "C": [0, 1, 0, 2, 3, -1],
    "G": [3, 0, 0, 0, 2, 3],
    "Am": [0, 1, 2, 2, 0, -1],
    "Em": [0, 0, 0, 2, 2, 0],
    "D": [2, 3, 2, 0, -1, -1],
    "F": [1, 1, 2, 3, 3, 1],
    "Dm": [1, 3, 2, 0, -1, -1],
    "E": [0, 0, 1, 2, 2, 0],
    "A": [0, 2, 2, 2, 0, -1],
    "Bm": [2, 3, 4, 4, 2, -1],
    "Bm7": [2, 3, 2, 4, 2, -1],
    "D11": [0, 3, 0, 4, 5, -1],
    "Cadd9": [3, 3, 0, 2, 3, -1],
    "Em7": [3, 3, 0, 2, 2, 0],
    "Am7": [0, 1, 0, 2, 0, -1],
    "Fmaj7": [0, 1, 2, 3, -1, -1],
    "G/B": [3, 0, 0, 0, 2, -1],
    "E7": [0, 0, 1, 0, 2, 0],
    "Cm": [3, 4, 5, 5, 3, -1],
    "F#m": [2, 2, 2, 4, 4, 2],
    "Dsus4": [3, 3, 2, 0, -1, -1],
    "Gsus4": [3, 1, 0, 0, 3, 3],
}
PROGRESSIONS = [
    ["G", "D", "Em", "C"],
    ["C", "G", "Am", "Em", "F", "C", "Dm", "G"],
    ["Cadd9", "D11", "Em7", "G/B"],
    ["Am", "F", "C", "G"],
    ["F", "G", "Em", "Am", "Dm", "G", "C", "E7"],
    ["Em", "Cadd9", "G", "D"],
    ["D", "Bm", "G", "A"],
    ["G", "Bm7", "Cadd9", "D11", "Em7", "D11", "Cadd9", "Dsus4"],
    ["A", "F#m", "D", "E"],
    ["Cm", "Gsus4", "Fmaj7", "Am7"],
]
# 分解节奏型：(时值（四分音符）, 弹哪根：bass = 根音弦，其余是弦号)
ARP = [
    [(Fraction(1, 2), s) for s in ("bass", 3, 2, 3, "bass2", 3, 2, 3)],
    [(Fraction(1, 2), s) for s in ("bass", 3, 2, 1, "bass2", 3, 2, 1)],
    [
        (Fraction(1, 2), "bass"),
        (Fraction(1, 2), 3),
        (Fraction(1, 4), 2),
        (Fraction(1, 4), 1),
        (Fraction(1, 2), 2),
    ]
    + [
        (Fraction(1, 2), "bass2"),
        (Fraction(1, 2), 3),
        (Fraction(1, 4), 2),
        (Fraction(1, 4), 1),
        (Fraction(1, 2), 2),
    ],
]
# 扫弦节奏型：(时值, D 下扫 / U 上扫)
STRUM = [
    [
        (Fraction(1), "D"),
        (Fraction(1, 2), "D"),
        (Fraction(1, 2), "U"),
        (Fraction(1, 2), "U"),
        (Fraction(1, 2), "D"),
        (Fraction(1), "U"),
    ],
    [(Fraction(1), "D"), (Fraction(1, 2), "D"), (Fraction(1, 4), "D"), (Fraction(1, 4), "U")] * 2,
    [(Fraction(1, 2), "D"), (Fraction(1, 4), "D"), (Fraction(1, 4), "U")] * 4,
    [(Fraction(1, 2), "D"), (Fraction(1, 2), "D"), (Fraction(1, 2), "U"), (Fraction(1, 2), "D")] * 2,
]
LYRIC_CHARS = "天真地向你展示万物的规则那不过是为了笑容而编造在我看见此刻繁花盛开着未来一片白色别时常怀念短暂人生对言也弥足宝贵每个夏耳边气温还有吹远歌声海风吹过山岗月光照亮回家路"
TICKS = 960


def played(shape: list[int]) -> list[int]:
    return [i + 1 for i, f in enumerate(shape) if f >= 0]


def bass_strings(shape: list[int]) -> tuple[int, int]:
    p = played(shape)
    low = max(p)
    return low, (low - 1 if low - 1 in p and low >= 5 else low)


def note(s: int, f: int, tech: list[str] | None = None) -> dict:
    return {"string": s, "fret": f, "tech": tech or [], "grace": -1, "bend": 0}


def make_bar(rng: random.Random, chords: list[str], style: str, fill: bool) -> list[dict]:
    """一个 4/4 小节的事件（我们的格式，带 chord / strum），chords 是这一小节的一个或两个和弦（第 3 拍换）。"""
    events: list[dict] = []
    t = Fraction(0)
    pattern = rng.choice(STRUM if style == "strum" else ARP)
    for dur, what in pattern:
        name = chords[0] if t < 2 or len(chords) == 1 else chords[1]
        shape = SHAPES[name]
        ev: dict = {"dur": str(dur), "notes": []}
        if not events or (
            len(chords) > 1 and t == 2 and not any(e.get("chord") == name for e in events[-1:])
        ):
            ev["chord"] = name
        if style == "strum":
            strings = played(shape) if what == "D" else [s for s in played(shape) if s <= 3]
            ev["notes"] = [note(s, shape[s - 1]) for s in sorted(strings)]
            ev["strum"] = "down" if what == "D" else "up"
        else:
            b1, b2 = bass_strings(shape)
            s = b1 if what == "bass" else b2 if what == "bass2" else what
            if shape[s - 1] < 0:
                s = b1
            ev["notes"] = [note(s, shape[s - 1])]
        events.append(ev)
        t += dur
    if fill and style != "strum":  # 过门：最后一拍改成写出品位的击弦（3 弦 0 → 2）
        last = events[-1]
        last["notes"] = [note(3, 2, ["h"])]
        events[-2]["notes"] = [note(3, 0)]
        last["x_fill"] = events[-2]["x_fill"] = True
    return events


def melody_bar(rng: random.Random, sing: bool) -> list[tuple[Fraction, str, str]]:
    """一个小节的旋律：[(时值, 音高 / 'r', 歌词字)]。"""
    if not sing:
        return [(Fraction(4), "r", "")]
    out, t = [], Fraction(0)
    if rng.random() < 0.4:
        out.append((Fraction(1), "r", ""))
        t = Fraction(1)
    while t < 4:
        d = rng.choice([Fraction(1, 2), Fraction(1, 2), Fraction(1), Fraction(1, 2)])
        d = min(d, 4 - t)
        pitch = rng.choice(["G4", "A4", "B4", "D5", "E5", "D4", "E4"])
        out.append((d, pitch, rng.choice(LYRIC_CHARS)))
        t += d
    return out


def attach_lyrics(events: list[dict], melody: list[tuple[Fraction, str, str]]) -> None:
    """歌词对到吉他的拍上：唱这个字时正在响的那个吉他起拍（起拍不晚于它的最后一个），同一拍上的几个字连起来。"""
    onsets, t = [], Fraction(0)
    for e in events:
        onsets.append(t)
        t += Fraction(e["dur"])
    t = Fraction(0)
    for d, pitch, ch in melody:
        if pitch != "r" and ch:
            k = max(i for i, o in enumerate(onsets) if o <= t)
            events[k]["lyrics"] = [(events[k].get("lyrics") or [""])[0] + ch]
        t += d


def tex_dur(d: Fraction) -> tuple[int, int]:
    """时值 → (alphaTex 时值, 附点数)。"""
    table = {
        Fraction(4): (1, 0),
        Fraction(2): (2, 0),
        Fraction(3, 2): (4, 1),
        Fraction(1): (4, 0),
        Fraction(3, 4): (8, 1),
        Fraction(1, 2): (8, 0),
        Fraction(1, 4): (16, 0),
    }
    return table[d]


def to_tex(meta: dict, bars: list[dict], melodies: list[list], key: str) -> str:
    shapes = {}
    for b in bars:
        for e in b["events"]:
            if e.get("chord"):
                shapes[e["chord"]] = SHAPES[e["chord"]]
    defs = []
    for name, fr in shapes.items():
        p = [f for f in fr if f > 0]
        first = min(p) if p and max(fr) > 4 and min(p) > 1 else 1
        defs.append(
            f'\\chord ("{name}" {" ".join("x" if f < 0 else str(f) for f in fr)}) {{firstfret {first} showdiagram true}}'
        )
    gtr = []
    for b in bars:
        parts = []
        for e in b["events"]:
            d, dots = tex_dur(Fraction(e["dur"]))
            fx = []
            if e.get("chord"):
                fx.append(f'ch "{e["chord"]}"')
            if e.get("strum") and len(e["notes"]) > 1:
                fx.append("bd" if e["strum"] == "down" else "bu")
            if dots:
                fx.append("d")
            # 和弦音画成 ×（谱上看不到品位，要看和弦图）；过门音写出品位
            ns = [
                f"{n['fret']}.{n['string']}"
                + ("{h}" if "h" in n["tech"] else "" if e.get("x_fill") else "{x}")
                for n in e["notes"]
            ]
            body = ns[0] if len(ns) == 1 else f"({' '.join(ns)})"
            parts.append(f"{body}.{d}" + (f"{{{' '.join(fx)}}}" if fx else ""))
        gtr.append(" ".join(parts))
    mel = []
    for m in melodies:
        parts = []
        for d, pitch, ch in m:
            dd, dots = tex_dur(d)
            fx = (["d"] if dots else []) + ([f'lyrics "{ch}"'] if ch else [])
            parts.append(f"{'r' if pitch == 'r' else pitch}.{dd}" + (f"{{{' '.join(fx)}}}" if fx else ""))
        mel.append(" ".join(parts))
    head = [
        f'\\title "{meta["title"]}"',
        f"\\tempo {meta['tempo']}",
        "\\chordDiagramsInScore",
        ".",
        '\\track "Guitar"',
        "\\staff {tabs}",
        f"\\tuning ({' '.join(meta['tuning'])})",
        *([f"\\capo {meta['capo']}"] if meta["capo"] else []),
        *defs,
        " |\n".join(gtr) + " |",
        "\\staff {numbered}",
        "\\tuning piano",
        f"\\ks {key}",
        " |\n".join(mel) + " |",
    ]
    return "\n".join(head)


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>body{margin:0;background:#fff} #score{width:WIDTHpx;padding:24px 30px}</style>
<script type="module">
import * as alphaTab from './alphatab/alphaTab.min.mjs';
const api = new alphaTab.AlphaTabApi(document.getElementById('score'), {
  core: { fontDirectory: './alphatab/font/', engine: 'svg', enableLazyLoading: false },
  display: { layoutMode: 'Page', scale: 1.0, barsPerRow: BPR, staveProfile: 'Default' },
  notation: { rhythmMode: 'ShowWithBars' },
  player: { playerMode: 'Disabled' },
});
api.scoreLoaded.on(s => { s.stylesheet.globalDisplayChordDiagramsOnTop = false; s.stylesheet.globalDisplayChordDiagramsInScore = true; });
api.postRenderFinished.on(() => { window.rows = api.boundsLookup.staffSystems.map(s => s.bars.length); window.done = true; });
api.tex(await (await fetch('score.tex')).text());
</script></head><body><div id="score"></div></body></html>"""


def render(tex: str, scale: float, width: int, bpr: int):
    web = Path(tempfile.mkdtemp())
    (web / "alphatab").symlink_to(ROOT / "app/static/vendor/alphatab")
    (web / "score.tex").write_text(tex)
    (web / "index.html").write_text(PAGE.replace("WIDTH", str(width)).replace("BPR", str(bpr)))

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(web)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    proc, ws = _start_chrome(9337)
    try:
        return asyncio.run(
            _screenshot(ws, f"http://127.0.0.1:{srv.server_address[1]}/index.html", scale, width)
        )
    finally:
        proc.terminate()
        srv.shutdown()


def generate(seed: int, style: str, n_bars: int) -> tuple[dict, list[dict], list[list], str]:
    rng = random.Random(seed)
    prog = rng.choice(PROGRESSIONS)
    key = rng.choice(["G", "C", "D"])
    meta = {
        "title": f"合成弹唱 {style} {seed}",
        "tuning": music.STANDARD,
        "capo": rng.choice([0, 0, 2, 3]),
        "time_signature": [4, 4],
        "tempo": rng.choice([72, 80, 90, 100]),
    }
    bars, melodies, k = [], [], 0
    for i in range(n_bars):
        st = style if style != "mixed" else ("arp" if i < n_bars // 2 else "strum")
        two = rng.random() < 0.5
        chords = [prog[k % len(prog)], prog[(k + 1) % len(prog)]] if two else [prog[k % len(prog)]]
        k += len(chords)
        ev = make_bar(rng, chords, st, fill=(style == "mixed" and rng.random() < 0.25))
        sing = i >= 2  # 前奏两小节不唱
        mel = melody_bar(rng, sing)
        attach_lyrics(ev, mel)
        bars.append({"events": ev})
        melodies.append(mel)
    for b in bars:
        for e in b["events"]:
            e.pop("x_fill", None)
    return meta, bars, melodies, key


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("song_id")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--style", choices=["arp", "strum", "mixed"], default="mixed")
    ap.add_argument("--bars", type=int, default=16)
    ap.add_argument("--bars-per-row", type=int, default=4)
    ap.add_argument("--scale", type=float, default=1.2)
    ap.add_argument("--width", type=int, default=1000)
    ap.add_argument("--per-page", type=int, default=5)
    ap.add_argument("--save-tex", type=Path)
    ap.add_argument("--save-full", type=Path, help="另存整张渲染图（排查版面检测用）")
    a = ap.parse_args()

    meta, truth_bars, melodies, key = generate(a.seed, a.style, a.bars)
    tex = to_tex(meta, truth_bars, melodies, key)
    if a.save_tex:
        a.save_tex.write_text(tex)
    full, rows = render(tex, a.scale, a.width, a.bars_per_row)
    if a.save_full:
        full.save(a.save_full)
    systems = detect_page(full)["systems"]
    if not systems:
        raise SystemExit("没检测到六线谱")
    cuts = [0] + [systems[i]["top"] for i in range(a.per_page, len(systems), a.per_page)] + [full.height]
    pages = []
    for k in range(len(cuts) - 1):
        buf = io.BytesIO()
        full.crop((0, cuts[k], full.width, cuts[k + 1])).save(buf, "PNG")
        pages.append((f"page{k + 1}.png", buf.getvalue()))
    sid = songs.create_song(pages, title=meta["title"])
    song = store.load(sid)
    planned = recognize.plan_systems(sid, song)
    counts = [len(s["spans"] or []) for s in planned]
    if sum(counts) != len(truth_bars):
        shutil.rmtree(store.SONGS / sid)
        raise SystemExit(
            f"版面检测找到 {sum(counts)} 个小节（每行 {counts}），应有 {len(truth_bars)}（alphaTab 每行 {rows}）"
        )
    nmeta = music.normalize_meta(meta)
    bars, k = [], 0
    for si, s in enumerate(planned):
        s["status"] = "done"
        for xa, xb in s["spans"]:
            events = music.clean_events(truth_bars[k]["events"])
            shapes = {e["chord"]: SHAPES[e["chord"]] for e in events if e.get("chord")}
            bars.append(
                {
                    "id": music.new_bar_id(),
                    "n": k + 1,
                    "system": si,
                    "page": s["page"],
                    "region": [max(0, xa - 8), xb + 8, s["crop"][2], s["crop"][3]],
                    "events": events,
                    "tip": "",
                    "uncertain": "",
                    "warn": [],
                    **({"shapes": shapes} if shapes else {}),
                }
            )
            k += 1
    store.update(sid, lambda x: x.update(meta=nmeta, systems=planned, bars=bars, status="ready", progress={}))
    dst = store.SONGS / a.song_id
    if dst.exists():
        shutil.rmtree(dst)
    (store.SONGS / sid).rename(dst)
    data = json.loads((dst / "song.json").read_text())
    data["id"] = a.song_id
    (dst / "song.json").write_text(json.dumps(data, ensure_ascii=False))
    print(f"已生成 {a.song_id}：{len(pages)} 页、{len(planned)} 行、{len(bars)} 小节，每行 {counts}")


if __name__ == "__main__":
    main()
