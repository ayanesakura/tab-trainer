"""图片 / PDF 曲子的页面管理：补页、删页、调整页序。换顺序和删页不调用模型；补的页做版面检测后只识别新页上的行
（app/recognize.py 的 recognize_rows_job；还没有任何小节的曲子整首识别）。"""

from __future__ import annotations

from app import config, jobs, recognize, songs, staleness, state, store
from app.errors import Busy, Invalid
from app.model import is_score
from app.music import apply_time_signatures


def arrange_pages(sid: str, order: list[str], uploads: list[tuple[str, bytes]]) -> dict:
    """调整图片 / PDF 曲子的页。order 是新的页序，每项是已有页的文件名，或 "new:k"（第 k 个上传文件的全部页）。
    不在 order 里的已有页删掉，连同上面的小节和练习记录。已有的行不重新识别（不花钱）；
    新页做版面检测，之后在后台只识别新页上的行（还没有任何小节的曲子整首识别）。"""
    cancel = jobs.claim_or_busy(sid, "这首曲子正在识别或生成讲解，完成后再调整页面")
    started = False
    try:
        song = store.load(sid)
        if is_score(song):
            raise Invalid("Guitar Pro / MusicXML 曲子没有页面可以调整")
        if song.get("status") in ("parsing", "queued") or state.recognizing(song):
            raise Busy("这首曲子正在识别，完成后再调整页面")
        files = [pg["file"] for pg in song["pages"]]
        new_ix = _check_order(order, files, len(uploads))
        if uploads:
            songs.check_upload_sizes(uploads)
            config.check_llm_ready()  # 新页要识别：没配 key 就先别存

        # 新页先存盘、做版面检测（都在改 song.json 之前，失败了曲子不受影响）
        pages_dir = store.song_dir(sid) / "pages"
        kept = [pg for pg in song["pages"] if pg["file"] in order]
        added: dict[int, list[dict]] = {}
        try:
            for k in sorted(new_ix):
                so_far = kept + [pg for pgs in added.values() for pg in pgs]
                added[k] = songs.save_pages([uploads[k]], pages_dir, so_far)
                if not added[k]:
                    raise Invalid(f"“{uploads[k][0]}”里没有可用的页面")
            new_pages, from_old = [], []  # 新页序；每页原来是第几页（新上传的是 None）
            for x in order:
                if x.startswith("new:"):
                    new_pages += added[int(x[4:])]
                    from_old += [None] * len(added[int(x[4:])])
                else:
                    new_pages.append(song["pages"][files.index(x)])
                    from_old.append(files.index(x))
            planned = {
                npi: recognize.plan_page_systems(sid, pg, npi)
                for npi, pg in enumerate(new_pages)
                if from_old[npi] is None
            }
        except Exception:
            for pgs in added.values():
                for pg in pgs:
                    (pages_dir / pg["file"]).unlink(missing_ok=True)
            raise

        pending: list[int] = []

        def apply(s):
            systems, sys_map = [], {}
            kept_pages = {opi for opi in from_old if opi is not None}
            kept_rows = {
                si for si, info in enumerate(s.get("systems") or []) if info.get("page") in kept_pages
            }
            # 留下的页上还有小节：只识别新行；一个都不剩：整首识别（新行先是 pending，见下面）
            any_left = any(b.get("system") in kept_rows for b in s.get("bars") or [])
            for npi, opi in enumerate(from_old):
                if opi is None:
                    for info in planned[npi]:
                        pending.append(len(systems))
                        systems.append({**info, "status": "parsing" if any_left else "pending"})
                    continue
                for si, info in enumerate(s.get("systems") or []):
                    if info.get("page") == opi:
                        sys_map[si] = len(systems)
                        systems.append({**info, "page": npi})
            bars = [b for b in s.get("bars") or [] if b.get("system") in sys_map]
            for k, b in enumerate(bars):
                b["system"], b["_order"] = sys_map[b["system"]], k
                b["page"] = systems[b["system"]]["page"]
            bars.sort(key=lambda b: (b["system"], b["_order"]))
            renumbered = len(bars) != len(s.get("bars") or []) or any(
                b["n"] != i + 1 for i, b in enumerate(bars)
            )
            for i, b in enumerate(bars):
                b["n"] = i + 1
                b.pop("_order", None)
            s.update(pages=new_pages, systems=systems, bars=bars)
            if s.get("meta"):
                apply_time_signatures(bars, s["meta"])
            if renumbered or pending:  # 小节号变了或多了小节，练习建议里的小节号对不上
                staleness.invalidate_plan(s)
            state.settle(s)  # 删掉的是识别失败的行所在的页：剩下的都成功了，曲子算识别完成
            if pending and bars:
                state.begin(
                    s,
                    "reparse_row",
                    systems=pending,
                    progress={"stage": "识别新加的页", "done": 0, "total": len(pending)},
                )

        song = store.update(sid, apply)
        for f in set(files) - set(order):  # 删掉的页：曲子已经不再引用它们
            (pages_dir / f).unlink(missing_ok=True)
        if pending and song["bars"]:
            jobs.spawn(sid, lambda: recognize.recognize_rows_job(sid, pending, cancel))
            started = True
        elif pending:  # 原来一个小节都没识别出来（或者都在删掉的页上）：整首识别（包括读谱头）
            started = recognize.start_parse(sid, cancel)  # 一直占着任务登记，中间不会被别的任务插进来
        return store.load(sid)
    finally:
        if not started:
            jobs.jobs.release(sid)


def _check_order(order: list[str], files: list[str], n_uploads: int) -> set[int]:
    """检查页序，返回其中新上传文件的序号。"new:k" 的 k 按数值比（"new:0" 和 "new:00" 是同一个文件）。"""
    if not order:
        raise Invalid("至少要留一页")
    new_ix, seen = set(), set()
    for x in order:
        if x.startswith("new:"):
            k = x[4:]
            if not (k.isascii() and k.isdigit()) or int(k) >= n_uploads:
                raise Invalid("页序里的新文件对不上上传的文件")
            x = f"new:{int(k)}"
            new_ix.add(int(k))
        elif x not in files:
            raise Invalid("页序里有不认识的页（可能页面已经被改过，刷新后再试）")
        if x in seen:
            raise Invalid("页序里有重复的页")
        seen.add(x)
    if len(new_ix) != n_uploads:
        raise Invalid("有上传的文件没有放进页序")
    return new_ix
