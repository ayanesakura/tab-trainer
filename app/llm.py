"""调用 Claude 识别谱子（客户端：请求、流式重试、用量和费用、错误说明）。提示词和 JSON Schema 在 app/prompts.py。

这几类调用要求按 JSON Schema 输出（structured outputs）：
- read_meta：读首页谱头（曲名、调弦、变调夹、拍号）
- transcribe：识别一行（或整页）谱子里的所有小节
- verify_bar：精修，放大一个小节对照当前读法逐拍核对（app/refine.py）
- explain_bars / practice_plan：给小节写练习提示、根据识别结果写分段练习建议
小节问答（coach_stream / coach）和首页的 AI 对话（chat_stream）要的是纯文字：边生成边给出片段，回答一点点出来。
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import anthropic
from PIL import Image

try:  # anthropic SDK 2.x 用的是它自带的 httpx 分支（httpx2），旧版用 httpx
    import httpx2 as _http
except ImportError:  # pragma: no cover
    import httpx as _http

from app import prompts
from app.errors import Invalid, Upstream
from app.prompts import (
    EXPLAIN_SCHEMA,
    META_SCHEMA,
    PLAN_SCHEMA,
    SYSTEM,
    TRANSCRIBE_SCHEMA,
    VERIFY_SCHEMA,
)

log = logging.getLogger("tab_trainer.llm")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
MAX_EDGE = 2576  # 当前模型支持的最大长边，超过会被缩小
MAX_UPSCALE = 3.0  # 小图最多放大几倍
# 发给模型的图至少放大到多宽（见 image_scale）：识别一行（半行识别时图只有几百像素宽）、精修一个小节、问答的小节图
TRANSCRIBE_MIN_WIDTH = 2400
VERIFY_MIN_WIDTH = 1600
COACH_MIN_WIDTH = 900
FALLBACK_BETA = "server-side-fallback-2026-07-01"
USER_AGENT = "tab-trainer"


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


class Refused(LLMError):
    """模型拒绝了这次请求（stop_reason = refusal）。"""


class Truncated(LLMError):
    """输出超出长度上限被截断（stop_reason = max_tokens）。"""


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


def _dropped_mid_stream(e: Exception) -> bool:
    """回复传到一半出的临时错误：连接断开、读超时（SDK 不包装，抛的是 httpx 的传输错误），或流里收到的
    api_error / overloaded_error（状态码是 200）。只有这些由 _stream 再试：建立连接时的 429 / 5xx / 连不上
    SDK 自己已经重试过（max_retries），再试一轮会让一次调用变成十几个请求。认证、权限、参数错误等重试也没用。"""
    if isinstance(e, _http.TransportError):
        return True
    if not isinstance(e, anthropic.APIStatusError) or e.status_code != 200:
        return False
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    return err.get("type") in ("api_error", "overloaded_error")


def _about_fallbacks(e: anthropic.BadRequestError) -> bool:
    """400 说的是不是 fallbacks 参数（或它的 beta 头）：先看错误体里指明的参数（param，有的网关会给），
    再看错误说明（error.message）；错误体不是 JSON 时退回看整个异常的文字。"""
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    param = err.get("param")
    if isinstance(param, str) and param:
        return "fallback" in param.lower()
    msg = err.get("message")
    return "fallback" in (msg if isinstance(msg, str) else str(e)).lower()


def answer_text(msg) -> str:
    """回复里的回答文字。服务端换了兜底模型时，内容依次是原模型写了一半被放弃的文字、一个 fallback 块、
    兜底模型从头写的回答：只取最后一个 fallback 块之后的（和流式的 _text_once 遇到 fallback 从头再拼一样）。"""
    blocks = list(msg.content)
    last = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    return "".join(b.text for b in blocks[last + 1 :] if b.type == "text")


def image_scale(w: float, h: float, min_width: int = 0) -> float:
    """w×h 的图发给模型前的缩放：比 min_width 窄的放大到 min_width（最多 MAX_UPSCALE 倍，数字更清楚），
    最长边不超过 MAX_EDGE（超过的部分模型那边也会缩小）。"""
    scale = 1.0
    if min_width and w < min_width:
        scale = min(MAX_UPSCALE, min_width / w)
    return min(scale, MAX_EDGE / max(w, h))


def _image_block(img: Image.Image, min_width: int = 0) -> tuple[dict, float]:
    img = img.convert("RGB")
    w, h = img.size
    scale = image_scale(w, h, min_width)
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


# 每百万 token 的美元价格（输入, 输出），用于估算费用（曲库显示每首花了多少、评测报告）。
# 来源：官方价格表 https://platform.claude.com/docs/en/about-claude/pricing （2026-11-11 核对）。
# Haiku 5.5 是提示不超过 10 万 token 的价格（超过的是 $0.50 / $2.50）；识别一行的请求远没有这么长。
# 只收官方写明的兜底目标：Claude Sonnet 5.5 用 fallbacks: "default" 时兜底到 Claude Sonnet 5
# （https://platform.claude.com/docs/en/models/sonnet-5-5/whats-new-sonnet-5-5#refusals-fallback-and-billing）。
# Claude Opus 5.5 的默认兜底按拒绝类别在服务端选，官方没有公布是哪个模型
# （https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback#server-side-fallback），不猜；
# 兜底到价格表里没有的模型时那部分 token 记成“没计价”，并记下模型名（见 _record_usage），曲库的费用提示里看得到。
PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5": (2.0, 10.0),
}

# 拒绝发生在给出任何输出之前时，只有这几类收费（别的类别、没有类别的不收费，token 照样报在 usage 里）。
# 官方：https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback#how-refusals-are-billed
# （“as of September 2026”，类别以后可能变）。写了一部分才拒绝的，输入和已经写出的照常收费。
BILLED_EARLY_REFUSALS = frozenset({"bio", "frontier_llm", "reasoning_extraction"})
_UNKNOWN = object()  # 不知道拒绝的类别（对不上 fallback 块、没有 stop_details）：按收费算，宁可多估


def _billed(refused: bool, output_tokens: int, category) -> bool:
    """一次尝试收不收费：没拒绝、或者写了输出再拒绝的都收；一开头就拒绝的按类别（BILLED_EARLY_REFUSALS）。"""
    if not refused or output_tokens > 0 or category is _UNKNOWN:
        return True
    return category in BILLED_EARLY_REFUSALS


def attempts(msg, model: str) -> list[tuple[str, int, int, bool]]:
    """一次请求里实际跑了的各次尝试：(模型, 输入 token, 输出 token, 收不收费)。model：请求的模型。
    服务端兜底（fallbacks）时顶层 usage 只是最后那次（兜底模型）的，每一次的用量在 usage.iterations 里：
    被拒、交给下一个模型的是 message 项（按顺序对应 content 里的 fallback 块，块上有拒绝的类别），
    最后答出来的是 fallback_message 项；各按各的模型计价。
    官方：https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback#billing-and-rate-limits
    没有换过模型（没有 fallback_message 项）时按顶层 usage 和 msg.model 算。"""
    usage = msg.usage
    served = getattr(msg, "model", None)
    served = served if isinstance(served, str) and served else model
    refused = getattr(msg, "stop_reason", None) == "refusal"
    details = getattr(msg, "stop_details", None)
    final_cat = getattr(details, "category", None) if details is not None else _UNKNOWN
    its = [
        it
        for it in getattr(usage, "iterations", None) or []
        if getattr(it, "type", None) in ("message", "fallback_message")
    ]
    if not any(it.type == "fallback_message" for it in its):
        tin, tout = usage.input_tokens or 0, usage.output_tokens or 0
        return [(served, tin, tout, _billed(refused, tout, final_cat))]
    cats = [
        getattr(getattr(b, "trigger", None), "category", None)
        for b in getattr(msg, "content", None) or []
        if getattr(b, "type", None) == "fallback"
    ]
    out, hop = [], 0
    for it in its:
        tin, tout = it.input_tokens or 0, it.output_tokens or 0
        if it.type == "message":  # 这个模型拒绝了，交给下一个
            cat = cats[hop] if hop < len(cats) else _UNKNOWN
            hop += 1
            out.append((getattr(it, "model", None) or model, tin, tout, _billed(True, tout, cat)))
        else:  # 最后跑的兜底模型（整条链都拒绝时它也是拒绝）
            out.append((getattr(it, "model", None) or served, tin, tout, _billed(refused, tout, final_cat)))
    return out


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
        hint: str = "",
        shapes: dict[str, list[int]] | None = None,
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

    def coach_stream(
        self,
        meta: dict,
        bar: dict,
        neighbours: list[dict],
        question: str,
        history: list[dict],
        practice: str = "",
        image: Image.Image | None = None,
    ) -> Iterator[str | None]: ...

    def practice_plan(self, meta: dict, bars: list[dict]) -> dict: ...

    def chat_stream(
        self, question: str, context: str, history: list[dict], today: str
    ) -> Iterator[str | None]: ...


def join_text(chunks: Iterable[str | None]) -> str:
    """把 coach_stream 给出的片段拼成完整的回答（None：前面的作废，从头再拼）。"""
    parts: list[str] = []
    for t in chunks:
        if t is None:
            parts.clear()
        else:
            parts.append(t)
    return "".join(parts)


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
            api_key=key,
            base_url=cfg.base_url or DEFAULT_BASE_URL,
            max_retries=3,
            timeout=600,
            # 网关（如 magpie）按 User-Agent 区分调用方；不改的话只会显示成 SDK 默认的 "Anthropic"
            default_headers={"User-Agent": USER_AGENT},
        )
        # 同一个客户端会被识别的几个线程同时用：用量累加和兜底开关都要加锁
        self._lock = threading.Lock()
        self._local = threading.local()  # 每个线程最近一次请求的 ID（识别是几个线程同时跑的）
        self.use_fallbacks = True
        self.usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_usd": 0.0,
            "unpriced_tokens": 0,
            "unpriced_models": [],  # 没计价的那部分 token 是哪些模型的
        }

    def _kwargs(
        self,
        content: list,
        schema: dict | None,
        effort: str | None,
        max_tokens: int,
        system: str | None = None,
    ) -> dict:
        """请求参数。schema 为 None 时不要求 JSON，模型直接写文字（流式问答）。system 不给时用识别谱子的 SYSTEM。"""
        output_config: dict = {"effort": effort or self.cfg.effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": schema}
        return {
            "model": self.cfg.model or DEFAULT_MODEL,
            "max_tokens": max_tokens,
            "system": system or SYSTEM,
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
        }

    def _drop_fallbacks(self, e: anthropic.BadRequestError, used_fallbacks: bool) -> bool:
        """代理或旧模型可能不认识 fallbacks 参数：这次请求带了、报错说的又是它，就关掉兜底，
        返回 True 让调用方不带它重试一次（看的是这次请求自己有没有带）。"""
        if not (used_fallbacks and _about_fallbacks(e)):
            return False
        log.warning("request_id=%s fallbacks rejected, retrying without: %s", request_id_of(e), e)
        with self._lock:
            self.use_fallbacks = False
        return True

    def _record_usage(self, msg, model: str) -> None:
        """把一次请求用掉的 token / 费用加到 usage 上（msg 为 None：什么都没收到，不记）。"""
        usage = getattr(msg, "usage", None)
        if usage is None:
            return
        with self._lock:
            for name, tin, tout, billed in attempts(msg, model):
                self.usage["input_tokens"] += tin
                self.usage["output_tokens"] += tout
                if not billed:
                    continue
                price = price_of(name)
                if price:
                    self.usage["cost_usd"] += (tin * price[0] + tout * price[1]) / 1e6
                else:  # 价格表里没有的模型（自定义、兜底到的别的模型）：只记 token，记下模型名（换新列表，不原地改）
                    self.usage["unpriced_tokens"] += tin + tout
                    self.usage["unpriced_models"] = sorted({*self.usage["unpriced_models"], name})

    def _finish(self, msg) -> str:
        """请求结束：记下请求 ID；被拒绝、被截断都算失败。返回请求 ID。"""
        rid = getattr(msg, "_request_id", None) or "none"
        self._local.request_id = rid
        if msg.stop_reason == "refusal":
            raise Refused(f"模型拒绝了这次请求（request_id={rid}）", rid)
        if msg.stop_reason == "max_tokens":
            raise Truncated(f"输出超出长度上限，被截断（request_id={rid}）", rid)
        return rid

    def _call(self, content: list, schema: dict, effort: str | None = None, max_tokens: int = 32000) -> dict:
        kwargs = self._kwargs(content, schema, effort, max_tokens)
        used_fallbacks = self.use_fallbacks
        try:
            msg = self._stream(kwargs, fallbacks=used_fallbacks)
        except anthropic.BadRequestError as e:
            if not self._drop_fallbacks(e, used_fallbacks):
                raise
            msg = self._stream(kwargs, fallbacks=False)
        self._record_usage(msg, kwargs["model"])
        rid = self._finish(msg)
        text = answer_text(msg)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"模型返回的不是合法 JSON（request_id={rid}）：{e}", rid) from e

    def _stream_call(
        self, content: list, effort: str | None = None, max_tokens: int = 32000, system: str | None = None
    ) -> Iterator[str | None]:
        """纯文字输出的流式请求，边收边给出文字片段；None 表示前面给出的都作废、从头再来（回复到一半的临时错误
        重试了，或者服务端换了兜底模型重新回答）。兜底参数、重试、用量和费用、拒绝 / 截断的处理和 _call 一样；
        拼起来是空的也算失败。片段都给完、检查也通过，生成器才正常结束。"""
        kwargs = self._kwargs(content, None, effort, max_tokens, system)
        used_fallbacks = self.use_fallbacks
        parts: list[str] = []
        try:
            msg = yield from self._relay(self._text_stream(kwargs, used_fallbacks), parts)
        except anthropic.BadRequestError as e:  # 建立连接时就报错：还没有给出任何片段
            if not self._drop_fallbacks(e, used_fallbacks):
                raise
            msg = yield from self._relay(self._text_stream(kwargs, False), parts)
        rid = self._finish(msg)
        if not "".join(parts).strip():
            raise LLMError(f"模型没有给出回答，换个问法再试一次（request_id={rid}）", rid)

    @staticmethod
    def _relay(chunks, parts: list[str]):
        """原样往外给片段，同时把拼起来的回答记在 parts 里；返回 chunks 的返回值（最后的完整消息）。
        调用方不再往下读时也关掉 chunks（它在 finally 里记已经花掉的用量）。"""
        try:
            while True:
                try:
                    t = next(chunks)
                except StopIteration as stop:
                    return stop.value
                if t is None:
                    parts.clear()
                else:
                    parts.append(t)
                yield t
        finally:
            chunks.close()

    def _text_stream(self, kwargs: dict, fallbacks: bool):
        """_stream 的纯文字版（同一个重试循环 _retrying）：重试前给出 None（已经给出的片段作废）。
        生成器，返回值是最后的完整消息。"""
        return self._retrying(lambda: self._text_once(kwargs, fallbacks))

    def _open(self, kwargs: dict, fallbacks: bool):
        """开始一次流式请求（带不带服务端兜底），返回 SDK 的 stream manager。"""
        if fallbacks:
            return self.client.beta.messages.stream(**kwargs, betas=[FALLBACK_BETA], fallbacks="default")
        return self.client.messages.stream(**kwargs)

    def _text_once(self, kwargs: dict, fallbacks: bool):
        """一次流式请求，边收边给出文字片段，返回最后的完整消息。用量在这里记：正常结束按最后的消息；
        中途出错、调用方不再往下读（生成器被关掉）时按已经收到的部分——服务端照样算钱。"""
        with self._open(kwargs, fallbacks) as s:
            msg = None
            try:
                for ev in s:
                    if ev.type == "content_block_start" and ev.content_block.type == "fallback":
                        yield None  # 前一个模型拒绝了，兜底模型从头回答：前面写的不算
                    elif ev.type == "content_block_delta" and ev.delta.type == "text_delta":
                        yield ev.delta.text
                msg = s.get_final_message()
            finally:
                if msg is None:
                    self._record_partial(s, kwargs["model"])
        self._record_usage(msg, kwargs["model"])
        return msg

    def _record_partial(self, s, model: str) -> None:
        """流式请求中途出错 / 被关掉：按已经收到的部分记用量（服务端照样算钱；精修最高精细度一次能用几万 token）。"""
        try:
            partial = s.current_message_snapshot
        except AssertionError:  # 连 message_start 都没收到
            partial = None
        self._record_usage(partial, model)

    @property
    def last_request_id(self) -> str:
        """这个线程最近一次成功请求的 ID。"""
        return getattr(self._local, "request_id", "none")

    @staticmethod
    def _retrying(once):
        """流式请求的重试循环（JSON 请求的 _stream 和问答的 _text_stream 共用）。once()：发一次请求的生成器，
        给出文字片段、返回最后的完整消息。SDK 自带的重试只管建立连接；回复传到一半出错（网关断线、服务过载，
        流里收到的 error 事件，状态码是 200）不会重试，整行识别就失败了。这类临时错误这里再试两次，重试前给出 None
        （已经给出的片段作废）；建立连接时的错误 SDK 已经重试过，不再重来（见 _dropped_mid_stream）。
        生成器，返回值是最后的完整消息。"""
        for attempt in range(STREAM_RETRIES + 1):
            try:
                return (yield from once())
            except (anthropic.APIStatusError, anthropic.APIConnectionError, _http.TransportError) as e:
                if attempt == STREAM_RETRIES or not _dropped_mid_stream(e):
                    raise
                log.warning(
                    "request_id=%s transient error mid-stream, retry %d: %s", request_id_of(e), attempt + 1, e
                )
                time.sleep(STREAM_RETRY_WAIT * (attempt + 1))
                yield None
        raise AssertionError("unreachable")

    def _stream(self, kwargs: dict, fallbacks: bool | None = None):
        """JSON 请求的流式请求（重试见 _retrying），返回最后的完整消息（用量由 _call 记）。"""

        def once():  # 生成器，好和问答共用重试循环；JSON 请求不往外给片段
            return self._stream_once(kwargs, fallbacks)
            yield

        chunks = self._retrying(once)
        while True:
            try:
                next(chunks)  # 只有重试前的 None
            except StopIteration as done:
                return done.value

    def _stream_once(self, kwargs: dict, fallbacks: bool | None = None):
        """一次流式请求，返回最后的完整消息（用量由 _call 记）。中途出错时按已经收到的部分记上用量再抛出。"""
        with self._open(kwargs, self.use_fallbacks if fallbacks is None else fallbacks) as s:
            try:
                return s.get_final_message()
            except BaseException:
                self._record_partial(s, kwargs["model"])
                raise

    # -- 各类调用（提示词在 app/prompts.py） ------------------------------------
    def ping(self) -> str:
        r = self._call([{"type": "text", "text": prompts.PING}], META_SCHEMA, effort="low", max_tokens=2000)
        return r.get("title", "")

    def read_meta(self, page: Image.Image) -> dict:
        block, _ = _image_block(page)
        return self._call([block, {"type": "text", "text": prompts.READ_META}], META_SCHEMA, max_tokens=16000)

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
        # 小截图放大一些：和弦图的圆点、符杠更容易看清
        block, scale = _image_block(img, min_width=TRANSCRIBE_MIN_WIDTH)
        text = prompts.transcribe_text(
            meta, first_bar, bar_spans, scale, hint, previous, problems, labeled_strings, start_ts, context
        )
        return self._call([block, {"type": "text", "text": text}], TRANSCRIBE_SCHEMA)

    def verify_bar(
        self,
        img: Image.Image,
        meta: dict,
        span: tuple[float, float],
        events: list[dict],
        previous: list[dict] | None = None,
        time_signature: list[int] | None = None,
        hint: str = "",
        shapes: dict[str, list[int]] | None = None,
    ) -> dict:
        """精修：放大一个小节（img 是它连同左右一点邻居的裁剪图，span 是它在图里的左右边界），对照当前读法逐拍核对。
        previous：上一小节的读法（只当上下文）；time_signature：这个小节的拍号（None = 全曲拍号）；
        hint：用户在小节卡片上写的“哪里不对”（空的时候提示词和以前一样）；shapes：弹唱谱识别时读出的和弦图
        （和弦名 → 1–6 弦品位）。meta 里的记谱说明（notes）也会带上，和识别一行一样。
        返回 {diff, changed, events, uncertain}。"""
        block, scale = _image_block(img, min_width=VERIFY_MIN_WIDTH)
        a, b = span
        text = prompts.verify_text(
            meta, (a * scale, b * scale), events, previous, time_signature, hint, shapes
        )
        content = [block, {"type": "text", "text": text}]
        try:
            # 精修用最高精细度：评测里最难的 25 个错小节改对 24 个、对的 208 个一个没改坏（high 只改对 16 个）
            return self._call(content, VERIFY_SCHEMA, effort=VERIFY_EFFORT, max_tokens=VERIFY_MAX_TOKENS)
        except Truncated:
            # 想得太久、输出超长（很少见）：按设置里的精细度再核对一次
            log.warning("verify_bar truncated at effort %s, retrying with %s", VERIFY_EFFORT, self.cfg.effort)
            return self._call(content, VERIFY_SCHEMA)

    def explain_bars(self, meta: dict, bars: list[dict]) -> dict:
        """给已经有精确数据的小节（GTP / MusicXML 导入）写练习提示，只发文字。"""
        text = prompts.explain_text(meta, bars)
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
        """针对一个小节回答练习问题，等整个回答写完再返回（流式的见 coach_stream）。"""
        return join_text(self.coach_stream(meta, bar, neighbours, question, history, practice, image))

    def coach_stream(
        self,
        meta: dict,
        bar: dict,
        neighbours: list[dict],
        question: str,
        history: list[dict],
        practice: str = "",
        image: Image.Image | None = None,
    ) -> Iterator[str | None]:
        """针对一个小节回答练习问题（指法、技巧、怎么练）：模型直接写文字，边生成边给出片段。
        None 表示前面给出的片段作废、从头再来（见 _stream_call），拼法见 join_text。"""
        content: list = []
        if image is not None:
            content.append(_image_block(image, min_width=COACH_MIN_WIDTH)[0])
        text = prompts.coach_text(meta, bar, neighbours, question, history, practice, image is not None)
        content.append({"type": "text", "text": text})
        return self._stream_call(content, effort="medium", max_tokens=8000)

    def chat_stream(
        self, question: str, context: str, history: list[dict], today: str
    ) -> Iterator[str | None]:
        """首页的 AI 对话：context 是曲库 / 练琴数据（app/chat.py 组装的纯文字），history 是前面几轮 {q, a}。
        模型直接写文字，边生成边给出片段（None 的意思见 _stream_call）。"""
        text = prompts.chat_text(context, question, history, today)
        content = [{"type": "text", "text": text}]
        return self._stream_call(content, effort="medium", max_tokens=8000, system=prompts.CHAT_SYSTEM)

    def practice_plan(self, meta: dict, bars: list[dict]) -> dict:
        text = prompts.plan_text(meta, bars)
        return self._call([{"type": "text", "text": text}], PLAN_SCHEMA, effort="medium", max_tokens=16000)
