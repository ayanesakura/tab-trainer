"""配置集中在这里：运行参数（并发数、各种上限）和模型设置（API key、Base URL、模型、精细度）。

- settings：进程内的运行参数，用的时候才读，测试可以直接改字段（例外：max_rows_global 在导入 recognize 时就建成了信号量）。
- 模型设置存在 data/config.json（读写和损坏处理在 store），这里负责合并、打码显示和建模型客户端。
- 数据目录：环境变量 TAB_TRAINER_DATA，默认仓库下的 data/（见 store.DATA）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app import store
from app.llm import DEFAULT_MODEL, LLM, Claude, LLMConfig, friendly


@dataclass
class Settings:
    row_concurrency: int = 4  # 一首曲子同时识别几行
    max_rows_global: int = 4  # 所有曲子加起来同时识别几行（连续上传多首时不会成倍地发请求、跑 OCR）
    # 上传上限：防止一个超大文件把内存、CPU 和磁盘吃光（开了局域网访问时其他设备也能上传）
    max_files: int = 40
    max_file_bytes: int = 50 * 1024 * 1024
    max_total_bytes: int = 200 * 1024 * 1024
    max_pages: int = 60  # 一首曲子最多几页（PDF 的页数也算在内）
    max_pixels: int = 60_000_000  # 一页最多多少像素（微信长截图 1080×20000 约 2200 万）
    pdf_dpi: int = 200
    # 曲子包
    max_bundle_songs: int = 500
    max_song_json: int = 20 * 1024 * 1024
    max_import_bytes: int = 2 * 1024 * 1024 * 1024  # 一次导入解出来的总大小（也用来挡上传的请求体）
    # AI 讲解 / 问答
    explain_chunk: int = 16  # 一次请求讲解几个小节
    qa_keep: int = 20  # 每个小节保留最近几条问答


settings = Settings()


# ---------------------------------------------------------------------------
# 模型设置
# ---------------------------------------------------------------------------
def mask(key: str) -> str:
    """设置页显示的 key：长的（官方 key）露前 4 位和后 4 位，中等长度（网关的短 key）只露后 4 位，太短的一位都不露。"""
    if len(key) >= 32:
        return key[:4] + "…" + key[-4:]
    if len(key) >= 16:
        return "…" + key[-4:]
    return "已设置" if key else ""


def public_settings() -> dict:
    """设置页显示的内容（key 打码）。"""
    c = store.load_config()
    return {
        "api_key_masked": mask(c.get("api_key", "")),
        "env_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "base_url": c.get("base_url", ""),
        "model": c.get("model") or DEFAULT_MODEL,
        "effort": c.get("effort") or "high",
        "double_check": double_check_mode(c),
    }


def update_settings(api_key: str | None, clear_api_key: bool, fields: dict) -> dict:
    """fields：这次请求里带了的 base_url / model / effort（None 当成清空）、double_check。
    只改这次带了的字段，读-改-写在锁里（store.update_config）。"""

    def edit(c: dict) -> None:
        if api_key:
            c["api_key"] = api_key.strip()
        if clear_api_key:
            c.pop("api_key", None)
        for k in fields.keys() & {"base_url", "model", "effort"}:
            c[k] = (fields[k] or "").strip()
        if "double_check" in fields:
            c["double_check"] = (
                fields["double_check"] if fields["double_check"] in DOUBLE_CHECK_MODES else "off"
            )

    store.update_config(edit)
    return public_settings()


DOUBLE_CHECK_MODES = ("off", "doubts", "all")


def double_check_mode(c: dict | None = None) -> str:
    """双重校对：off 不做；doubts 第一遍里有警告或“不确定”的行再读一遍；all 每行都再读一遍。
    两遍不一致的小节读第三遍投票。旧设置存的是 true / false。"""
    v = (store.load_config() if c is None else c).get("double_check")
    if v is True:
        return "all"
    return v if v in DOUBLE_CHECK_MODES else "off"


def double_check() -> str:
    return double_check_mode()


def llm_config() -> LLMConfig:
    c = store.load_config()
    return LLMConfig(
        api_key=c.get("api_key", ""),
        base_url=c.get("base_url", ""),
        model=c.get("model") or LLMConfig.model,
        effort=c.get("effort") or LLMConfig.effort,
    )


llm_factory: type[LLM] = Claude  # 测试换成替身模型（tests/fakes.py）


def new_llm() -> LLM:
    """按当前设置建一个模型客户端；没配 key 时抛 llm.MissingKey（400）。"""
    return llm_factory(llm_config())  # type: ignore[call-arg]


def check_llm_ready() -> None:
    """发请求前先确认 key 配好了，免得任务先显示“进行中”过一会儿才失败。"""
    new_llm()


def test_connection() -> dict:
    try:
        new_llm().ping()
    except Exception as e:  # noqa: BLE001 - 任何错误都原样展示给用户
        return {"ok": False, "error": friendly(e)}
    return {"ok": True}
