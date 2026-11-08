"""按 Guitar Pro / MusicXML 原文件重新生成评测曲的标准答案（只换小节的音符，图片、版面、小节 id 都不动）。

deriveSong 改了（DERIVE_VERSION +1，比如新认出一种技巧）之后，用 scripts/make_eval_song.py 做的评测曲里
存的还是旧规则转换出来的答案。重新跑 make_eval_song.py 会重新渲染图片、重做版面检测（小节 id 全变），
这个脚本只按原文件重新转换一遍，逐小节换掉变了的音符，并打印哪些小节变了、怎么变的。

默认只换音符（弦、品、技巧、倚音、推弦、点弦泛音点的品），每个事件的时值和拍上的和弦名 / 扫弦 / 歌词保留原样：
评测曲做好以后 deriveSong 新加的拍上记号（比如扫弦）不会顺带混进来，评测指标只因为想改的那一处而变。
节奏（事件的时值）也变了的小节只列出来、不改；加 --whole 整个小节照新的转换结果换（含和弦名、扫弦、歌词）。

用法：
    uv run python scripts/refresh_eval_truth.py 曲子.gp eval-xxx [--track N] [--whole]           # 只打印变化
    uv run python scripts/refresh_eval_truth.py 曲子.gp eval-xxx [--track N] [--whole] --write   # 写回
数据目录照常由 TAB_TRAINER_DATA 指定（默认仓库下的 data/）。小节数、拍号和原文件对不上时报错退出，什么都不改。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from make_eval_song import derive

from app import music, prompts, store


def changed_bars(
    song: dict, truth: dict, whole: bool = False
) -> tuple[list[tuple[int, list[dict]]], list[int]]:
    """([(小节下标, 新的事件)], [节奏变了、没改的小节下标])：按原文件转换出来的和现在存的不一样的小节。
    whole=False 时只换每个事件的音符（时值、和弦名、扫弦、歌词照旧），时值对不上的小节不改。"""
    bars, want = song["bars"], truth["bars"]
    if len(want) != len(bars):
        raise SystemExit(
            f"原文件有 {len(want)} 个小节，评测曲有 {len(bars)} 个：不是同一首，或者不是这样做出来的"
        )
    meta = song["meta"]
    out, skipped = [], []
    for i, (b, tb) in enumerate(zip(bars, want, strict=True)):
        ts = music.clean_time_signature(tb.get("time_signature") or meta["time_signature"])
        if ts != music.bar_time_signature(meta, b):
            raise SystemExit(
                f"第 {i + 1} 小节的拍号对不上（原文件 {ts}，评测曲 {music.bar_time_signature(meta, b)}）"
            )
        events = music.clean_events(tb["events"])
        if not whole:
            old = music.clean_events(b["events"])
            if [e["dur"] for e in old] != [e["dur"] for e in events]:
                skipped.append(i)
                continue
            events = [{**e, "notes": n["notes"]} for e, n in zip(old, events, strict=True)]
        if not music.same_events(events, b["events"]):
            out.append((i, events))
    return out, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", type=Path)
    ap.add_argument("song_id")
    ap.add_argument(
        "--track", type=int, default=-1, help="音轨序号，默认第一条六弦音轨（和 make_eval_song.py 一样）"
    )
    ap.add_argument("--whole", action="store_true", help="整个小节照新的转换结果换（默认只换音符）")
    ap.add_argument("--write", action="store_true", help="把变了的小节写回（默认只打印）")
    a = ap.parse_args()

    song = store.load(a.song_id)
    truth = derive(a.file, a.track)
    changes, skipped = changed_bars(song, truth, a.whole)
    if skipped:
        print(f"节奏变了、没改的小节（要改加 --whole）：{[i + 1 for i in skipped]}")
    for i, events in changes:
        b = song["bars"][i]
        print(f"- 旧 {prompts.bar_text(b)}")
        print(f"+ 新 {prompts.bar_text({**b, 'events': events})}")
    print(
        f"{a.song_id}（{store.song_dir(a.song_id)}）：{len(changes)} 个小节变了 {[i + 1 for i, _ in changes]}"
    )
    if not a.write or not changes:
        return

    def apply(s: dict) -> None:
        for i, events in changes:
            s["bars"][i]["events"] = events

    store.update(a.song_id, apply)
    print("已写回")


if __name__ == "__main__":
    main()
