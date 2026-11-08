"""把曲库里一首用户核对过的真实谱子做成评测曲：页面、版面和小节原样复制，标准答案 = 现在的小节。

默认只把用户核对过的小节当标准答案：点过“确认没问题”的、手改过的。加 --all 表示整首都核对过了
（每个小节都算）；加 --include-clean 再算上没有任何警告、模型也没标不确定的小节（没人看过，只适合当回归参考）。
评测时只评这些小节（app/evaluate.py 认 song["eval_bars"]）。

用法：
    uv run python scripts/make_real_eval.py <曲子 id> eval-real-三季虫 [--all | --include-clean]
    uv run python -m app.evaluate eval-real-三季虫 --yes
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import store, views
from app.model import now


def verified(bar: dict, include_clean: bool) -> bool:
    if bar.get("edited") or views.reviewed(bar):
        return True
    return include_clean and not views.has_doubts(bar)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("song_id")
    ap.add_argument("eval_id", help="评测曲的 id，建议 eval-real-xxx（已存在会覆盖）")
    ap.add_argument("--all", action="store_true", help="整首都核对过了，每个小节都算标准答案")
    ap.add_argument(
        "--include-clean", action="store_true", help="没有警告、也没标不确定的小节也算（没人看过）"
    )
    a = ap.parse_args()

    song = store.load(a.song_id)
    if song.get("source_type") not in ("image", "pdf"):
        raise SystemExit("只有图片 / PDF 曲子能做成识别评测")
    bars = song["bars"]
    picked = [b["n"] for b in bars if a.all or verified(b, a.include_clean)]
    if not picked:
        raise SystemExit(
            "这首曲子还没有核对过的小节：在练习页点“确认没问题”或改正几个小节，或者加 --all / --include-clean"
        )
    src, dst = store.song_dir(a.song_id), store.SONGS / a.eval_id
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("*.bak", "practice.jsonl"))
    data = json.loads((dst / "song.json").read_text())
    data.update(
        id=a.eval_id,
        title=f"评测：{song.get('title') or a.song_id}",
        eval_bars=picked,
        eval_source={
            "song": a.song_id,
            "at": now(),
            "mode": "all" if a.all else "clean" if a.include_clean else "verified",
        },
    )
    (dst / "song.json").write_text(json.dumps(data, ensure_ascii=False))
    rows = sorted({b["system"] for b in bars if b["n"] in picked})
    print(f"已生成 {a.eval_id}：{len(picked)}/{len(bars)} 个小节算标准答案，分布在 {len(rows)} 行")


if __name__ == "__main__":
    main()
