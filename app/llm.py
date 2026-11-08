"""调用 Claude 识别谱子（客户端：请求、流式重试、用量和费用、错误说明）。提示词和 JSON Schema 在 app/prompts.py。

三类调用，都要求按 JSON Schema 输出（structured outputs）：
- read_meta：读首页谱头（曲名、调弦、变调夹、拍号）
- transcribe：识别一行（或整页）谱子里的所有小节
- verify_bar：精修，放大一个小节对照当前读法逐拍核对（app/refine.py）
- practice_plan：根据识别结果写分段练习建议
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import anthropic
from PIL import Image

try:  # anthropic SDK 2.x 用的是它自带的 httpx 分支（httpx2），旧版用 httpx
    import httpx2 as _http
except ImportError:  # pragma: no cover
    import httpx as _http

from app.errors import Invalid, Upstream
from app.prompts import (
    COACH_SCHEMA,
    EXPLAIN_SCHEMA,
    META_SCHEMA,
    NOTATION_KEY,
    PLAN_SCHEMA,
    SYSTEM,
    TRANSCRIBE_RULES,
    TRANSCRIBE_SCHEMA,
    VERIFY_SCHEMA,
    VERIFY_TASK,
    bar_text,
    verify_events,
)

log = logging.getLogger("tab_trainer.llm")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
MAX_EDGE = 2576  # 当前模型支持的最大长边，超过会被缩小
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = DEFAULT_MODEL
    effort: str = "high"


class LLMError(Upstream):
    """模型调用失败（拒绝、截断、不是合法 JSON）：502。"""

    def __init__(self, msg: str, request_id: str | None = None):
        super().__init__(msg)
        self.request_id = request_id


class MissingKey(LLMError, Invalid):
    """还没有设置 API key：用户自己能解决，400。"""

    status = 400


def request_id_of(e: BaseException | None) -> str:
    """异常对应的请求 ID（API 报错、拒绝、截断都有），用来写日志；拿不到就是 none。"""
    rid = getattr(e, "request_id", None)
    if not rid:
        resp = getattr(e, "response", None)
        rid = getattr(resp, "headers", {}).get("request-id") if resp is not None else None
    return rid or "none"


STREAM_RETRIES = 2
STREAM_RETRY_WAIT = 3.0  # 秒，第 n 次重试前等 n 倍


def _transient(e: Exception) -> bool:
    """值得重试的临时错误：连接断开（包括回复传到一半断开——SDK 不包装，抛的是 httpx 的传输错误）、
    流里的 api_error / overloaded_error、5xx 和 429。认证、权限、参数错误等重试也没用。"""
    if isinstance(e, (anthropic.APIConnectionError, _http.TransportError)):
        return True
    if not isinstance(e, anthropic.APIStatusError):
        return False
    if e.status_code in (429, 500, 502, 503, 504, 529):
        return True
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    return e.status_code == 200 and err.get("type") in ("api_error", "overloaded_error")


def _image_block(img: Image.Image, min_width: int = 0) -> tuple[dict, float]:
    img = img.convert("RGB")
    w, h = img.size
    scale = 1.0
    if min_width and w < min_width:
        scale = min(3.0, min_width / w)  # 小图适当放大，数字更清楚（半行识别时图只有几百像素宽）
    scale = min(scale, MAX_EDGE / max(w, h))
    if abs(scale - 1) > 0.01:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(buf.getvalue()).decode(),
        },
    }, scale


# 每百万 token 的美元价格（输入, 输出），用于估算费用（曲库显示每首花了多少、评测报告）
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}


# 设置页“识别模型”的预设按钮（app/static/index.html 的 #modelSeg；tests/test_vocab.py 检查两边一致、都有价格）
PRESET_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")


def price_of(model: str | None) -> tuple[float, float] | None:
    """模型的价格；网关加了前缀的名字（如 compass/claude-opus-5-5）按最后一段查。"""
    name = (model or "").rsplit("/", 1)[-1]
    return PRICES.get(name)


@runtime_checkable
class LLM(Protocol):
    """模型客户端的接口：Claude 是真的，tests/fakes.py 的 FakeClaude 是替身。usage 累计这个客户端用掉的 token / 费用。"""

    usage: dict

    @property
    def last_request_id(self) -> str: ...

    def ping(self) -> str: ...

    def read_meta(self, page: Image.Image) -> dict: ...

    def transcribe(
        self,
        img: Image.Image,
        meta: dict,
        first_bar: int,
        bar_spans: list[tuple[int, int]] | None,
        hint: str = "",
        previous: dict | None = None,
        problems: list[str] | None = None,
        labeled_strings: bool = False,
        start_ts: list[int] | None = None,
        context: str = "",
    ) -> dict: ...

    def verify_bar(
        self,
        img: Image.Image,
        meta: dict,
        span: tuple[float, float],
        events: list[dict],
        previous: list[dict] | None = None,
        time_signature: list[int] | None = None,
    ) -> dict: ...

    def explain_bars(self, meta: dict, bars: list[dict]) -> dict: ...

    def coach(
        self,
        meta: dict,
        bar: dict,
        neighbours: list[dict],
        question: str,
        history: list[dict],
        practice: str = "",
        image: Image.Image | None = None,
    ) -> str: ...

    def practice_plan(self, meta: dict, bars: list[dict]) -> dict: ...


REASON_MAX = 200


def upstream_reason(e: anthropic.APIStatusError) -> str:
    """服务端给的错误说明（只取 error.message 字段，截断），不带请求内容和 key。
    经过网关时 403 不一定是 key 的问题——例如网关把请求转给了只接受某种客户端的上游渠道，原因只在这里。"""
    body = e.body
    msg = None
    if isinstance(body, dict):
        err = body.get("error")
        msg = err.get("message") if isinstance(err, dict) else body.get("message")
    if not isinstance(msg, str) or not msg.strip():
        return ""
    msg = " ".join(msg.split())
    return msg if len(msg) <= REASON_MAX else msg[: REASON_MAX - 1] + "…"


def friendly(e: Exception) -> str:
    """把模型调用的异常翻译成用户能看懂、知道怎么处理的说明。"""
    if isinstance(e, anthropic.AuthenticationError):
        return "API key 无效，请在曲库页顶部的“设置”里检查。"
    if isinstance(e, anthropic.PermissionDeniedError):
        reason = upstream_reason(e)
        return "模型服务拒绝了这次请求（403）" + (
            f"：{reason}" if reason else "，可能是 API key 没有调用该模型的权限。"
        )
    if isinstance(e, anthropic.NotFoundError):
        return "模型不存在或无权访问，请检查“设置”里的模型名。"
    if isinstance(e, anthropic.RateLimitError):
        return "调用太频繁或额度不足（429），稍后再试。"
    if isinstance(e, anthropic.APIConnectionError):
        return "连不上模型服务，请检查网络或 Base URL。"
    if isinstance(e, _http.TransportError):  # 回复传到一半连接断开、读超时
        return "和模型服务的连接中断了，请稍后重试（网络不稳定时可以检查网络或 Base URL）。"
    if isinstance(e, anthropic.APIStatusError):
        return f"模型服务返回错误 {e.status_code}：{e.message}"
    if isinstance(e, LLMError):
        return str(e)
    return f"{type(e).__name__}: {e}"


# 精修（逐小节核对）的精细度和输出上限：最高档想得久，输出要给够
VERIFY_EFFORT = "max"
VERIFY_MAX_TOKENS = 64000


class Claude(LLM):
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        # 只用设置页里填的 key 或 ANTHROPIC_API_KEY；地址也显式指定。
        # 不让 SDK 自动读取 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL 等环境变量——
        # 那些可能是别的工具（如 Claude Code）的凭据和网关，不能被这个应用顺手用掉。
        key = cfg.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise MissingKey(
                "还没有设置 API key：请在曲库页顶部的“设置”里填写，或启动前设置环境变量 ANTHROPIC_API_KEY。"
            )
        self.client = anthropic.Anthropic(
            api_key=key, base_url=cfg.base_url or DEFAULT_BASE_URL, max_retries=3, timeout=600
        )
        # 同一个客户端会被识别的几个线程同时用：用量累加和兜底开关都要加锁
        self._lock = threading.Lock()
        self._local = threading.local()  # 每个线程最近一次请求的 ID（识别是几个线程同时跑的）
        self.use_fallbacks = True
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "unpriced_tokens": 0}

    def _call(self, content: list, schema: dict, effort: str | None = None, max_tokens: int = 32000) -> dict:
        kwargs = {
            "model": self.cfg.model or DEFAULT_MODEL,
            "max_tokens": max_tokens,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": content}],
            "output_config": {
                "effort": effort or self.cfg.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }
        used_fallbacks = self.use_fallbacks
        try:
            msg = self._stream(kwargs, fallbacks=used_fallbacks)
        except anthropic.BadRequestError as e:
            # 代理或旧模型可能不认识 fallbacks 参数：去掉后重试一次（看的是这次请求自己有没有带）
            if used_fallbacks and "fallback" in str(e).lower():
                log.warning("request_id=%s fallbacks rejected, retrying without: %s", request_id_of(e), e)
                with self._lock:
                    self.use_fallbacks = False
                msg = self._stream(kwargs, fallbacks=False)
            else:
                raise
        rid = getattr(msg, "_request_id", None) or "none"
        self._local.request_id = rid
        with self._lock:
            tin, tout = msg.usage.input_tokens or 0, msg.usage.output_tokens or 0
            self.usage["input_tokens"] += tin
            self.usage["output_tokens"] += tout
            model = getattr(msg, "model", None)
            price = price_of(model if isinstance(model, str) and model else kwargs["model"])
            if price:
                self.usage["cost_usd"] += (tin * price[0] + tout * price[1]) / 1e6
            else:  # 价格表里没有的模型（自定义）：只记 token
                self.usage["unpriced_tokens"] += tin + tout
        if msg.stop_reason == "refusal":
            raise LLMError(f"模型拒绝了这次请求（request_id={rid}）", rid)
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"输出超出长度上限，被截断（request_id={rid}）", rid)
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"模型返回的不是合法 JSON（request_id={rid}）：{e}", rid) from e

    @property
    def last_request_id(self) -> str:
        """这个线程最近一次成功请求的 ID。"""
        return getattr(self._local, "request_id", "none")

    def _stream(self, kwargs: dict, fallbacks: bool | None = None):
        """流式请求。SDK 自带的重试只管建立连接；回复传到一半出错（网关断线、服务过载，流里收到的 error 事件，
        状态码是 200）不会重试，整行识别就失败了。这类临时错误这里再试两次。"""
        for attempt in range(STREAM_RETRIES + 1):
            try:
                return self._stream_once(kwargs, fallbacks)
            except (anthropic.APIStatusError, anthropic.APIConnectionError, _http.TransportError) as e:
                if attempt == STREAM_RETRIES or not _transient(e):
                    raise
                log.warning(
                    "request_id=%s transient error mid-stream, retry %d: %s", request_id_of(e), attempt + 1, e
                )
                time.sleep(STREAM_RETRY_WAIT * (attempt + 1))
        raise AssertionError("unreachable")

    def _stream_once(self, kwargs: dict, fallbacks: bool | None = None):
        if self.use_fallbacks if fallbacks is None else fallbacks:
            with self.client.beta.messages.stream(**kwargs, betas=[FALLBACK_BETA], fallbacks="default") as s:
                return s.get_final_message()
        with self.client.messages.stream(**kwargs) as s:
            return s.get_final_message()

    # -- 三类调用 -------------------------------------------------------------
    def ping(self) -> str:
        r = self._call(
            [
                {
                    "type": "text",
                    "text": 'Reply with {"title": "ok", ...} filling other fields with empty values.',
                }
            ],
            META_SCHEMA,
            effort="low",
            max_tokens=2000,
        )
        return r.get("title", "")

    def read_meta(self, page: Image.Image) -> dict:
        block, _ = _image_block(page)
        text = (
            "This is the first page of a guitar score. Read its header and return:\n"
            "- title / artist / arranger (empty string if absent)\n"
            "- tuning: 6 note names with octave, for string 1 (highest) to string 6 (lowest), e.g. "
            '["E4","B3","G3","D3","A2","E2"]. Tuning text such as "DADGAD" or "Tuning=DAC#F#AD" lists strings '
            "from 6 (lowest) to 1. If no tuning is written, assume standard tuning.\n"
            "- capo: the capo fret, 0 if none.\n"
            "- time_signature: [numerator, denominator] of the first bar, e.g. [4, 4].\n"
            "- tempo: quarter-note BPM if written, else 0.\n"
            "- notes: in Chinese, anything a player should know (e.g. tuning changes, the written capo looking "
            "inconsistent with the standard-notation pitches), and the notation conventions this score uses that "
            "matter for reading every line - for example x marks meaning chord tones of the diagrams above, a "
            "pattern of x marks labelled once as a slap / percussive hit (拍弦, 打板, 切音) that later lines repeat "
            "without the label, or what a special symbol means. Empty string if nothing."
        )
        return self._call([block, {"type": "text", "text": text}], META_SCHEMA, max_tokens=16000)

    def transcribe(
        self,
        img: Image.Image,
        meta: dict,
        first_bar: int,
        bar_spans: list[tuple[int, int]] | None,
        hint: str = "",
        previous: dict | None = None,
        problems: list[str] | None = None,
        labeled_strings: bool = False,
        start_ts: list[int] | None = None,
        context: str = "",
    ) -> dict:
        """start_ts：前面的行里变过拍号时，这一行开头实际的拍号（这张图上看不到那个变拍号）。
        context：程序给的上下文（半行识别时左半结束时的和弦等），不是用户说的话。"""
        block, scale = _image_block(img, min_width=2400)  # 小截图放大一些：和弦图的圆点、符杠更容易看清
        ts = meta["time_signature"]
        bar_len = ts[0] * 4 / ts[1]
        info = (
            f"Score info: tuning (string 1 -> 6) {' '.join(meta['tuning'])}; capo {meta['capo']} "
            f"(copy fret numbers as written). "
        )
        if start_ts and start_ts != ts:
            n_len = start_ts[0] * 4 / start_ts[1]
            info += (
                f"The piece starts in {ts[0]}/{ts[1]}, but an earlier line changed the time signature: at the start "
                f"of this line it is {start_ts[0]}/{start_ts[1]}, so a bar lasts {n_len:g} quarter notes unless "
                "another time-signature change is written in this image (see below)."
            )
        else:
            info += (
                f"Time signature at the start of the piece {ts[0]}/{ts[1]}: a bar lasts {bar_len:g} quarter notes "
                "unless a time-signature change is written (see below)."
            )
        lines = [info]
        if (meta.get("notes") or "").strip():
            # 首页读谱头时记下的说明（记谱约定、标在别处的拍弦 / 切音等）：每一行单独识别，看不到别的行
            lines.append(
                "Notes about this score, written when its first page was read (they apply to every line, including "
                f"marks that are only labelled once in the score): {meta['notes'].strip()}"
            )
        if bar_spans:
            last = first_bar + len(bar_spans) - 1
            spans = "; ".join(
                f"bar {first_bar + i}: x={round(a * scale)}-{round(b * scale)}"
                for i, (a, b) in enumerate(bar_spans)
            )
            lines.append(
                f"This image is one line (system) of the score with {len(bar_spans)} bars, numbered {first_bar}-{last}. "
                f"Horizontal extent of each bar in this image (pixels): {spans}. Return exactly these bars."
            )
            if labeled_strings:
                lines.append(
                    "The small red numbers 1-6 at the left edge of the tab staff were added by the app, not part of "
                    "the score: they label the six tab lines (1 = top line, 6 = bottom line). Never transcribe them. "
                    "For every fret number, follow its line to the left to the red label to decide its string, instead "
                    "of counting lines by eye: a common mistake is putting a number one line too low (string 5 read "
                    "as string 6), especially bass notes and open strings (0) on the lower lines."
                )
        else:
            lines.append(
                f"This image is a whole page. Transcribe every bar on it in reading order, numbering from {first_bar}. "
                "If the page contains no guitar tablature, return an empty list."
            )
        if context:
            lines.append(context)
        lines.append(TRANSCRIBE_RULES)
        if previous is not None:
            lines.append(
                "Your previous transcription of this image is below, but it has problems:\n- "
                + "\n- ".join(problems or [])
                + "\nRe-read the image carefully and return a corrected transcription of all the bars.\n"
                + json.dumps(previous, ensure_ascii=False)
            )
        if hint:
            lines.append(f"The user reviewed an earlier transcription and says (in Chinese): {hint}")
        return self._call([block, {"type": "text", "text": "\n\n".join(lines)}], TRANSCRIBE_SCHEMA)

    def verify_bar(
        self,
        img: Image.Image,
        meta: dict,
        span: tuple[float, float],
        events: list[dict],
        previous: list[dict] | None = None,
        time_signature: list[int] | None = None,
    ) -> dict:
        """精修：放大一个小节（img 是它连同左右一点邻居的裁剪图，span 是它在图里的左右边界），对照当前读法逐拍核对。
        previous：上一小节的读法（只当上下文）；time_signature：这个小节的拍号（None = 全曲拍号）。
        返回 {diff, changed, events, uncertain}。"""
        block, scale = _image_block(img, min_width=1600)
        ts = time_signature or meta["time_signature"]
        length = ts[0] * 4 / ts[1]
        a, b = span
        text = [
            (
                f"Score info: tuning (string 1 -> 6) {' '.join(meta['tuning'])}; capo {meta['capo']}. "
                f"Time signature {ts[0]}/{ts[1]}: this bar lasts {length:g} quarter notes."
            ),
            VERIFY_TASK.format(a=round(a * scale), b=round(b * scale)),
            TRANSCRIBE_RULES,
            "Current transcription of this bar (events in time order):\n"
            + json.dumps(verify_events(events), ensure_ascii=False),
        ]
        if previous is not None:
            text.append(
                "For context only, the previous bar was read as:\n"
                + json.dumps(verify_events(previous), ensure_ascii=False)
            )
        content = [block, {"type": "text", "text": "\n\n".join(text)}]
        try:
            # 精修用最高精细度：评测里最难的 25 个错小节改对 24 个、对的 208 个一个没改坏（high 只改对 16 个）
            return self._call(content, VERIFY_SCHEMA, effort=VERIFY_EFFORT, max_tokens=VERIFY_MAX_TOKENS)
        except LLMError as e:
            if "截断" not in str(e):
                raise
            # 想得太久、输出超长（很少见）：按设置里的精细度再核对一次
            log.warning("verify_bar truncated at effort %s, retrying with %s", VERIFY_EFFORT, self.cfg.effort)
            return self._call(content, VERIFY_SCHEMA)

    def explain_bars(self, meta: dict, bars: list[dict]) -> dict:
        """给已经有精确数据的小节（GTP / MusicXML 导入）写练习提示，只发文字。"""

        lines = [bar_text(b) for b in bars]
        text = (
            f"Tuning (string 1 -> 6): {' '.join(meta['tuning'])}; capo {meta['capo']}; time signature "
            f"{meta['time_signature'][0]}/{meta['time_signature'][1]}. Below is exact tablature data for some bars: "
            + NOTATION_KEY
            + ".\n"
            + "\n".join(lines)
            + "\n\nFor every bar, write a tip in Chinese: 1-4 sentences of practical guidance naming strings and frets "
            "the way a teacher would (what to play together, what is fretted vs open, which technique and how to do it, "
            "what is hard and how to practise it). Return one entry per bar with its number."
        )
        return self._call([{"type": "text", "text": text}], EXPLAIN_SCHEMA, effort="medium", max_tokens=32000)

    def coach(
        self,
        meta: dict,
        bar: dict,
        neighbours: list[dict],
        question: str,
        history: list[dict],
        practice: str = "",
        image: Image.Image | None = None,
    ) -> str:
        """针对一个小节回答练习问题（指法、技巧、怎么练）。"""
        parts = [
            f"A guitarist is practising {meta.get('title') or 'a piece'} (tuning string 1 -> 6: {' '.join(meta['tuning'])}, "
            f"capo {meta['capo']}, time signature {meta['time_signature'][0]}/{meta['time_signature'][1]}) and asks about "
            f"bar {bar['n']}. Tablature data, " + NOTATION_KEY + ":",
            "This bar: " + bar_text(bar),
            "Neighbouring bars for context:\n" + "\n".join(bar_text(b) for b in neighbours)
            if neighbours
            else "",
            f"Practice tip already shown for this bar: {bar['tip']}" if bar.get("tip") else "",
            f"Their practice log for this bar: {practice}" if practice else "",
            (
                "The image is this bar cut from the original score; the data above was transcribed from it and "
                "may contain mistakes - if the image clearly disagrees, say so."
                if image is not None
                else ""
            ),
        ]
        for h in history[-4:]:
            parts.append(f"Earlier question: {h['q']}\nYour answer: {h['a']}")
        parts.append(
            f"Their question (in Chinese): {question}\n\nAnswer in Simplified Chinese like a patient guitar teacher: "
            "concrete fingering (which left-hand finger on which string/fret, right-hand p i m a where useful), what "
            "to listen for, and a short practice routine if it helps. Refer to notes as 'N 弦 M 品'. Be concise: "
            "a few short paragraphs or a short list, no headings."
        )
        content: list = []
        if image is not None:
            content.append(_image_block(image, min_width=900)[0])
        content.append({"type": "text", "text": "\n\n".join(p for p in parts if p)})
        return self._call(content, COACH_SCHEMA, effort="medium", max_tokens=8000).get("answer", "")

    def practice_plan(self, meta: dict, bars: list[dict]) -> dict:
        summary = [
            {
                "bar": b["n"],
                "techniques": sorted({t for e in b["events"] for n in e["notes"] for t in n["tech"]}),
                "tip": b["tip"],
            }
            for b in bars
        ]
        text = (
            f"A guitarist is learning this piece: {meta.get('title') or '(untitled)'}, tuning "
            f"{' '.join(meta['tuning'])}, capo {meta['capo']}. Per-bar notes from the transcription:\n"
            + json.dumps(summary, ensure_ascii=False)
            + "\n\nWrite a practice plan in Chinese. overview: 2-3 sentences on what makes this piece hard and how "
            "to approach it. steps: 4-8 steps in the order to practise, each covering a range of bars written like "
            '"6-11" (use the bar numbers above; the whole piece as the final step), with a short title and 1-3 '
            "sentences of concrete advice (which technique to isolate, which bars resemble each other, tempo tips)."
        )
        return self._call([{"type": "text", "text": text}], PLAN_SCHEMA, effort="medium", max_tokens=16000)
