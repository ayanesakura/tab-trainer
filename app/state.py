"""曲子的运行状态：status（识别结果的状态）+ activity（正在进行的后台任务）+ notice（给用户看的一条提示）。

所有“忙不忙 / 能不能改 / 哪个小节锁住”都由这里的函数算出来，接口（app/views.py）把结果直接给前端。
都是对内存里 song dict 的操作，由调用方放在 store.update 里执行。

- status：queued / parsing（整首识别）、ready、error、cancelled、imported（GP / MusicXML 还没在浏览器里解析过）。
- activity：None 或 {kind: parse | reparse_row | explain | refine, restaging, systems}。
  parse = 整首识别（restaging = 已有结果时的重新识别：新结果全部成功才替换，期间所有小节不能改）；
  reparse_row = 识别某几行（单行重新识别、补页的新行），systems 是这几行；explain = AI 讲解（不改谱面）；
  refine = 精修（逐小节放大核对，见 app/refine.py）：期间照常能练、能改，写回时小节被改过的就不写。
- notice：None 或 {kind: warn | error | info, msg, source}。source 是留下它的任务类型；同类任务再开始时清掉，整首识别清掉所有。
"""

from __future__ import annotations

from app.model import EDITABLE_META, is_score

RECOGNIZE_KINDS = ("parse", "reparse_row")
META_FIELDS = list(EDITABLE_META)
SETTING_FIELDS = ("tuning", "capo", "time_signature")
SCORE_BARS_LOCKED = "Guitar Pro / MusicXML 曲子的小节来自原文件，不能在这里改"


def begin(
    s: dict,
    kind: str,
    *,
    systems: list[int] | None = None,
    restaging: bool = False,
    progress: dict | None = None,
    status: str | None = None,
) -> None:
    s["activity"] = {"kind": kind, "restaging": restaging, "systems": list(systems or [])}
    if progress is not None:
        s["progress"] = progress
    if status:
        s["status"] = status
    notice = s.get("notice")
    if notice and (kind == "parse" or notice.get("source") == kind):
        s["notice"] = None


def finish(
    s: dict, *, status: str | None = None, notice: tuple[str, str] | None = None, source: str = ""
) -> None:
    """任务结束：清掉 activity 和进度；notice = (warn|error, 说明)。"""
    kind = (s.get("activity") or {}).get("kind")
    s["activity"] = None
    s["progress"] = None
    if status:
        s["status"] = status
    elif kind in RECOGNIZE_KINDS:  # 讲解结束不动识别的状态和提示
        settle(s)
    if notice:
        set_notice(s, *notice, source=source)


def settle(s: dict) -> None:
    """识别失败 / 取消（或有行失败）后，用户逐行重新识别、重试失败的行、补页或删掉失败的行所在的页，每一行都成功了：
    曲子算识别完成，整首识别留下的“识别失败 / 有行没识别出来”的提示也清掉。"""
    systems = s.get("systems") or []
    if not s.get("meta") or not systems or not all(x.get("status") == "done" for x in systems):
        return
    if s.get("status") in ("error", "cancelled"):
        s["status"] = "ready"
    if (s.get("notice") or {}).get("source") == "parse":
        s["notice"] = None


def set_notice(s: dict, kind: str, msg: str, source: str = "") -> None:
    s["notice"] = {"kind": kind, "msg": msg, "source": source}


def recognizing(s: dict) -> bool:
    """整首识别或某几行的识别正在进行（讲解不算）。"""
    a = s.get("activity")
    return bool(a) and a.get("kind") in RECOGNIZE_KINDS


def restaging(s: dict) -> bool:
    return bool((s.get("activity") or {}).get("restaging"))


def busy(s: dict, running: bool) -> bool:
    return running or s.get("activity") is not None


def row_parsing(s: dict, si) -> bool:
    systems = s.get("systems") or []
    return isinstance(si, int) and 0 <= si < len(systems) and systems[si].get("status") == "parsing"


def can_edit_bars(s: dict) -> bool:
    return not is_score(s)


def bar_locked(s: dict, bar: dict) -> str | None:
    """这个小节现在不能改的原因：GP 曲子的小节来自原文件；或者改了会被正在进行的识别结果覆盖。"""
    if is_score(s):
        return SCORE_BARS_LOCKED
    if restaging(s):
        return "正在重新识别整首，完成后再改（识别失败或取消时原来的结果会保留）"
    if row_parsing(s, bar.get("system")):
        return "这一行正在重新识别，完成后再改"
    return None


def settings_locked(s: dict) -> bool:
    """识别时按调弦、拍号检查和返修，改了会和正在写回的结果对不上（曲名之类随时能改）。"""
    return recognizing(s) or any(x.get("status") == "parsing" for x in s.get("systems") or [])


def editable_meta(s: dict) -> list[str]:
    """现在允许改的谱头字段。GP / MusicXML 只有曲名（其余来自原文件）；还没读出谱头时也只有曲名。"""
    if is_score(s) or not s.get("meta"):
        return ["title"]
    if settings_locked(s):
        return [f for f in META_FIELDS if f not in SETTING_FIELDS]
    return list(META_FIELDS)


def flags(s: dict, running: bool) -> dict:
    return {
        "busy": busy(s, running),
        "recognizing": recognizing(s),
        "can_edit_bars": can_edit_bars(s),
        "editable_meta": editable_meta(s),
    }


def public_notice(s: dict) -> dict | None:
    n = s.get("notice")
    return {"kind": n.get("kind", "warn"), "msg": n.get("msg", "")} if n else None


def recover(s: dict) -> bool:
    """服务重启（或导入曲子包）时：上次没跑完的任务标成中断，否则曲子会一直卡在“识别中”。返回有没有改动。"""
    changed = False
    a = s.get("activity")
    kind = (a or {}).get("kind")
    if kind == "refine":
        finish(
            s,
            notice=("warn", "服务重启，精修中断，已经改好的小节会保留，可以再点“精修整首”接着核对剩下的。"),
            source="refine",
        )
        changed = True
    elif kind == "explain":
        finish(
            s, notice=("warn", "服务重启，讲解生成中断，已完成的部分会保留，可以重新生成。"), source="explain"
        )
        changed = True
    elif kind == "parse" and a.get("restaging"):  # 重新识别时原来的结果没动过，只是新结果丢了
        finish(
            s, status="ready", notice=("warn", "服务重启，重新识别中断，原来的结果保留不变。"), source="parse"
        )
        changed = True
    elif kind == "reparse_row":
        finish(s)
        changed = True
    if s.get("status") in ("parsing", "queued"):  # 第一次识别（或还没开始就停了）
        msg = "服务重启，识别中断。可以在曲库的“…”菜单里重新识别。"
        finish(s, status="error", notice=("error", msg), source="parse")
        changed = True
    for x in s.get("systems") or []:
        if x.get("status") in ("parsing", "pending"):
            x.update(status="error", error="服务重启，识别中断")
            changed = True
    return changed
