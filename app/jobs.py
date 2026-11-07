"""后台任务的公共部分：每首曲子一个任务的登记与取消、模型客户端、把异常翻译成给用户看的中文。"""

from __future__ import annotations

import threading
from collections.abc import Callable

import anthropic

from app import store
from app.errors import Busy, Invalid
from app.llm import Claude, LLMConfig, LLMError

USAGE_KEYS = ("input_tokens", "output_tokens")


class Cancelled(Exception):
    pass


class Jobs:
    """每首曲子同一时间只允许一个任务（整首识别、单行重新识别或生成讲解）。登记、取消、查询都在一把锁里。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._active: dict[str, threading.Event] = {}

    def claim(self, sid: str) -> threading.Event | None:
        """登记任务，返回它的取消信号；已经有任务在跑就返回 None。"""
        with self._lock:
            if sid in self._active:
                return None
            ev = self._active[sid] = threading.Event()
            return ev

    def release(self, sid: str) -> None:
        with self._lock:
            self._active.pop(sid, None)

    def running(self, sid: str) -> bool:
        with self._lock:
            return sid in self._active

    def cancel(self, sid: str) -> bool:
        with self._lock:
            ev = self._active.get(sid)
        if ev:
            ev.set()
        return ev is not None


jobs = Jobs()


def claim_or_busy(sid: str, message: str) -> threading.Event:
    cancel = jobs.claim(sid)
    if cancel is None:
        raise Busy(message)
    return cancel


def spawn(sid: str, target: Callable[[], None]) -> None:
    """在后台线程里跑已登记（jobs.claim）的任务，结束后释放登记。target 自己处理并记录错误。"""

    def run():
        try:
            target()
        finally:
            jobs.release(sid)

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
def llm_config() -> LLMConfig:
    c = store.load_config()
    return LLMConfig(
        api_key=c.get("api_key", ""),
        base_url=c.get("base_url", ""),
        model=c.get("model") or LLMConfig.model,
        effort=c.get("effort") or LLMConfig.effort,
    )


def new_claude() -> Claude:
    """按当前设置建一个模型客户端；没配 key 时抛 LLMError。测试替换这里的 Claude 即可换成替身模型。"""
    return Claude(llm_config())


def check_llm_ready() -> None:
    """发请求前先确认 key 配好了，免得任务先显示“进行中”过一会儿才失败。"""
    try:
        new_claude()
    except LLMError as e:
        raise Invalid(str(e)) from None


def add_usage(s: dict, usage: dict, before: dict | None = None) -> None:
    """把这次任务用掉的 token（usage 减去开始时的 before）累加到曲子上。"""
    old = s.get("usage") or {}
    s["usage"] = {k: old.get(k, 0) + usage[k] - (before or {}).get(k, 0) for k in USAGE_KEYS}


def friendly(e: Exception) -> str:
    """把模型调用的异常翻译成用户能看懂、知道怎么处理的说明。"""
    if isinstance(e, anthropic.AuthenticationError):
        return "API key 无效，请在首页“模型设置”里检查。"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "API key 没有调用该模型的权限。"
    if isinstance(e, anthropic.NotFoundError):
        return "模型不存在或无权访问，请检查“模型设置”里的模型名。"
    if isinstance(e, anthropic.RateLimitError):
        return "调用太频繁或额度不足（429），稍后再试。"
    if isinstance(e, anthropic.APIConnectionError):
        return "连不上模型服务，请检查网络或 Base URL。"
    if isinstance(e, anthropic.APIStatusError):
        return f"模型服务返回错误 {e.status_code}：{e.message}"
    if isinstance(e, LLMError):
        return str(e)
    return f"{type(e).__name__}: {e}"
