"""本机数字识别（app/ocr.py）的准确率评测：不调用大模型、不花钱，也不改任何数据。

按评测曲存好的版面（每行的裁剪范围、弦线、小节范围）逐行调用 ocr.read_system，逐小节把读到的（弦, 品）
和标准答案比：
- 死音（x）不算；倚音的小数字算；
- 人工泛音旁的 <品位+12>、点弦泛音的 <点的品>、跨小节线的延续音 (n) 谱上确实印着，读到不算错、没读到也不算漏
  （“可选”，单独统计读到了几成）；延续音按上一小节这根弦最后一个音判断，给了 --gp-truth 就按 GP 原文件精确判断；
- 两位数被读成两个音（11 → 1、1）按 ocr_disagreements 的规则并回去算对，另外计数。
输出总体和每首曲子的精确率 / 召回率，以及常见的错法（漏读 1、漏读 0、两位数拆开、错弦、读错数字）。

给了 --gp-truth（scripts/ocr_gp_truth.mjs 从 GP 原文件导出的“谱上每个数字和它的括号”），再评测
read_system(detail=True) 报的括号 ( ) / 尖括号 < > 标记：鬼音和小节开头的延续音画成 (n)，自然泛音画成 <n>，
人工 / 点弦泛音的标注画成 <m>。

用法：
    uv run python scripts/ocr_bench.py                                  # 默认的评测曲（含降质版本）
    uv run python scripts/ocr_bench.py --model /path/to/rec.onnx --songs eval-amy,eval-d-amy -v
    uv run python scripts/ocr_bench.py --gp-truth /tmp/gptruth.json --json out.json
数据目录：--data，默认 TAB_TRAINER_DATA / 仓库下的 data/（只读）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import ocr, store
from app.layout import detect_page

SONGS = [
    "eval-amy",
    "eval-andiloveher",
    "eval-kisstherain",
    "eval-r-felicity",
    "eval-r-imagine",
    "eval-r-imagine-tab",
    "eval-r-mother",
    "eval-r-mother-tab",
    "eval-romance",
    "eval-wonderfulworld",
    "example-wuting",
]
DEGRADED = {  # 降质版本 → 原曲（GP 标准答案共用）
    "eval-d-amy": "eval-amy",
    "eval-d-andiloveher": "eval-andiloveher",
    "eval-d-kisstherain": "eval-kisstherain",
    "eval-d-r-felicity": "eval-r-felicity",
    "eval-d-r-imagine": "eval-r-imagine",
    "eval-d-r-mother": "eval-r-mother",
    "eval-d-romance": "eval-romance",
    "eval-d-wuting": "example-wuting",
}


def truth_bag(bar: dict) -> tuple[Counter, Counter]:
    """标准答案一个小节：必须读到的（弦, 品）和读到也不算错的泛音标注。"""
    need, optional = Counter(), Counter()
    for e in bar["events"]:
        for n in e["notes"]:
            if "x" in n["tech"]:
                continue
            need[(n["string"], n["fret"])] += 1
            if n.get("grace", -1) >= 0:
                need[(n["string"], n["grace"])] += 1
            if "ah" in n["tech"]:
                optional[(n["string"], n["fret"] + 12)] += 1
            if n.get("harm"):
                optional[(n["string"], n["harm"])] += 1
    return need, optional


def merge_splits(need: Counter, got: Counter) -> tuple[Counter, int]:
    """两位数被读成几个一位数：能由多出来的几位拼回答案里漏掉的两位数就并回去（和 ocr_disagreements 一样）。"""
    merged = 0
    for (st, f), k in list((need - got).items()):
        parts = Counter((st, int(d)) for d in str(f)) if f >= 10 else None
        for _ in range(k):
            if parts and not (parts - (got - need)):
                got = got - parts
                got[(st, f)] += 1
                merged += 1
    return got, merged


def confusions(miss: Counter, extra: Counter) -> list[str]:
    """把漏读和多读配成对，说明错法。"""
    miss, extra, out = Counter(miss), Counter(extra), []
    for (s, f), k in sorted(miss.items()):
        for _ in range(k):
            same = [key for key in extra if key[0] == s and extra[key] > 0]
            near = [key for key in extra if abs(key[0] - s) == 1 and key[1] == f and extra[key] > 0]
            if same:
                g = min(same, key=lambda x: abs(x[1] - f))
                extra[g] -= 1
                out.append(f"read {f} as {g[1]}")
            elif near:
                extra[near[0]] -= 1
                out.append("wrong string")
            else:
                out.append(f"missed {f}")
    for (_, g), k in (+extra).items():
        out += [f"extra {g}"] * k
    return out


def load_song(data: Path, sid: str) -> dict:
    return json.loads((data / "songs" / sid / "song.json").read_text(encoding="utf-8"))


def read_rows(data: Path, sid: str, song: dict, detail: bool):
    """逐行 OCR。产出 (行号, 这一行的小节, 每个小节读到的)。"""
    pages: dict[int, Image.Image] = {}
    detected: dict[int, list] = {}
    for si, sy in enumerate(song["systems"]):
        bars = [b for b in song["bars"] if b["system"] == si]
        if not sy.get("spans") or len(bars) != len(sy["spans"]):
            yield si, bars, None
            continue
        if sy["page"] not in pages:
            f = data / "songs" / sid / "pages" / song["pages"][sy["page"]]["file"]
            pages[sy["page"]] = Image.open(f).convert("L")
        x0, x1, y0, y1 = sy["crop"]
        lines = sy.get("lines")
        if not lines:  # 旧曲子没存弦线：和 recognize.tab_lines 一样对整页重新检测
            if sy["page"] not in detected:
                detected[sy["page"]] = detect_page(pages[sy["page"]])["systems"]
            lines = next(
                (
                    s["lines"]
                    for s in detected[sy["page"]]
                    if y0 <= s["lines"][0] and s["lines"][-1] <= y1 and s["x0"] < x1 and x0 < s["x1"]
                ),
                None,
            )
        if not lines:
            yield si, bars, None
            continue
        gray = np.asarray(pages[sy["page"]])[y0:y1, x0:x1]
        lines = [y - y0 for y in lines]
        spans = [(a - x0, b - x0) for a, b in sy["spans"]]
        yield si, bars, ocr.read_system(gray, lines, spans, detail=detail)


def bench_song(data: Path, sid: str, gp: dict | None, verbose: bool) -> dict:
    song = load_song(data, sid)
    scored = set(song.get("eval_bars") or [])
    r = Counter()
    conf: Counter = Counter()
    flags = Counter()
    last: dict[int, int] = {}  # 每根弦上最后一个音（判断延续音）
    bar_index = {b["id"]: i for i, b in enumerate(song["bars"])}
    worst = []
    for si, bars, read in read_rows(data, sid, song, detail=gp is not None):
        if read is None:
            r["rows_skipped"] += 1
            continue
        r["rows"] += 1
        for bar, seen in zip(bars, read, strict=True):
            need, optional = truth_bag(bar)
            gpi = gp["bars"][bar_index[bar["id"]]] if gp else None
            if gpi is not None:
                optional += Counter((x["s"], x["f"]) for x in gpi if x["kind"] == "tie")
            else:
                first = {n["string"] for n in (bar["events"][0]["notes"] if bar["events"] else [])}
                optional += Counter((s, f) for s, f in last.items() if s not in first)
            for e in bar["events"]:
                for n in e["notes"]:
                    if "x" not in n["tech"]:
                        last[n["string"]] = n["fret"]
            if scored and bar_index[bar["id"]] + 1 not in scored:
                continue
            got = Counter((d[0], d[1]) for d in seen)
            got, merged = merge_splits(need, got)
            hit = need & got
            extra = got - need
            opt_hit = extra & optional
            extra -= opt_hit
            miss = need - got
            r["truth"] += sum(need.values())
            r["hit"] += sum(hit.values())
            r["fp"] += sum(extra.values())
            r["fn"] += sum(miss.values())
            r["opt"] += sum(optional.values())
            r["opt_hit"] += sum(opt_hit.values())
            r["split"] += merged
            r["bars"] += 1
            r["bars_exact"] += not extra and not miss
            c = confusions(miss, extra)
            conf.update(c)
            if c:
                worst.append((len(c), bar_index[bar["id"]] + 1, si, c))
            if gpi is not None:
                flag_stats(seen, gpi, flags)
    r["conf"] = conf
    r["flags"] = flags
    if verbose:
        for _, n, si, c in sorted(worst, reverse=True)[:8]:
            print(f"    bar {n} (row {si}): {', '.join(c)}")
    return r


def flag_stats(seen: list, items: list[dict], flags: Counter) -> None:
    """按弦把 OCR 读到的数字（从左到右）和 GP 里画出来的数字（按拍的先后）对齐，统计括号 / 尖括号标记。"""
    order = {"grace": 0, "note": 1, "tie": 1, "hmark": 2}
    for s in range(1, 7):
        truth = sorted((x for x in items if x["s"] == s), key=lambda x: (x["t"], order[x["kind"]]))
        read = [d for d in seen if d[0] == s]
        sm = SequenceMatcher(a=[x["f"] for x in truth], b=[d[1] for d in read], autojunk=False)
        matched_t, matched_r = set(), set()
        for blk in sm.get_matching_blocks():
            for k in range(blk.size):
                t, d = truth[blk.a + k], read[blk.b + k]
                matched_t.add(blk.a + k)
                matched_r.add(blk.b + k)
                for name, want, got in (("paren", t["paren"], d.paren), ("angle", t["angle"], d.angle)):
                    flags[f"{name}_tp"] += want and got
                    flags[f"{name}_fp"] += got and not want
                    flags[f"{name}_fn_read"] += want and not got  # 数字读到了、标记没报
        for k, t in enumerate(truth):
            if k not in matched_t:
                for name in ("paren", "angle"):
                    flags[f"{name}_fn_unread"] += t[name]  # 数字本身没读到
        for k, d in enumerate(read):
            if k not in matched_r:
                for name in ("paren", "angle"):
                    flags[f"{name}_fp_unmatched"] += getattr(d, name)


def pr(r: Counter) -> str:
    p = r["hit"] / max(1, r["hit"] + r["fp"])
    rec = r["hit"] / max(1, r["truth"])
    opt = f"{r['opt_hit']}/{r['opt']}" if r["opt"] else "-"
    return (
        f"P {p:6.1%}  R {rec:6.1%}  (truth {r['truth']}, fp {r['fp']}, fn {r['fn']}, split {r['split']}, "
        f"optional read {opt}, exact bars {r['bars_exact']}/{r['bars']})"
    )


def flag_report(f: Counter) -> str:
    out = []
    for name in ("paren", "angle"):
        tp, fp = f[f"{name}_tp"], f[f"{name}_fp"] + f[f"{name}_fp_unmatched"]
        fn_read, fn_unread = f[f"{name}_fn_read"], f[f"{name}_fn_unread"]
        p = tp / max(1, tp + fp)
        rr = tp / max(1, tp + fn_read)
        ra = tp / max(1, tp + fn_read + fn_unread)
        out.append(
            f"{name}: P {p:6.1%} ({tp}/{tp + fp})  R(read digits) {rr:6.1%} ({tp}/{tp + fn_read})"
            f"  R(all) {ra:6.1%} ({tp}/{tp + fn_read + fn_unread})"
        )
    return "  |  ".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, default=store.DATA)
    ap.add_argument("--model", type=Path, help="换一个识别模型（ONNX，字表在元数据 character 里）")
    ap.add_argument("--dark", type=int, help=f"笔画的灰度阈值（默认 ocr.DARK = {ocr.DARK}）")
    ap.add_argument(
        "--digits-only", action="store_true", help="数字块只许读成数字（ocr.MARKS_IN_BLOCK = False）"
    )
    ap.add_argument("--light", type=int, help=f"浅色笔画的灰度阈值（默认 ocr.LIGHT = {ocr.LIGHT}，0 = 不收）")
    ap.add_argument("--songs", help="逗号分隔；默认全部评测曲和降质版本")
    ap.add_argument("--gp-truth", type=Path, help="ocr_gp_truth.mjs 的输出：评测括号 / 尖括号标记")
    ap.add_argument("--json", type=Path, help="把结果写成 JSON")
    ap.add_argument("-v", "--verbose", action="store_true", help="列出每首错得最多的小节")
    a = ap.parse_args()
    if a.model:
        ocr.MODEL = a.model
        ocr._load.cache_clear()
    if a.light is not None:
        ocr.LIGHT = a.light
    if a.digits_only:
        ocr.MARKS_IN_BLOCK = False
    if a.dark:
        ocr.DARK = a.dark
    if not ocr.available():
        sys.exit(f"识别模型不可用：{ocr.MODEL}")
    gp_all = json.loads(a.gp_truth.read_text()) if a.gp_truth else None
    songs = a.songs.split(",") if a.songs else [*SONGS, *DEGRADED]
    total = {"clean": Counter(), "degraded": Counter()}
    conf = {"clean": Counter(), "degraded": Counter()}
    flags = {"clean": Counter(), "degraded": Counter()}
    out = {"model": str(ocr.MODEL), "songs": {}}
    t0 = time.time()
    for sid in songs:
        if not (a.data / "songs" / sid / "song.json").exists():
            print(f"{sid}: 不存在，跳过")
            continue
        gp = gp_all.get(DEGRADED.get(sid, sid)) if gp_all else None
        t = time.time()
        r = bench_song(a.data, sid, gp, a.verbose)
        kind = "degraded" if sid in DEGRADED else "clean"
        c, f = r.pop("conf"), r.pop("flags")
        total[kind].update(r)
        conf[kind].update(c)
        flags[kind].update(f)
        print(f"{sid:22s} {pr(r)}  {time.time() - t:.1f}s")
        if f:
            print(f"{'':22s} {flag_report(f)}")
        out["songs"][sid] = {**r, "confusions": dict(c), "flags": dict(f)}
    for kind in ("clean", "degraded"):
        if total[kind]["truth"]:
            print(f"\n== {kind}: {pr(total[kind])}")
            print("   typical:", ", ".join(f"{k} ×{n}" for k, n in conf[kind].most_common(12)))
            if flags[kind]:
                print("  ", flag_report(flags[kind]))
    print(f"\n{time.time() - t0:.0f}s, model {ocr.MODEL}")
    if a.json:
        out["total"] = {k: dict(v) for k, v in total.items()}
        a.json.write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
