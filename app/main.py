"""FastAPI 服务：曲库 / 上传 / 识别进度 / 修改 / 模型设置，以及前端静态页面。

启动：uv run python -m app.main   （默认 http://127.0.0.1:8765）
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import threading
from typing import Annotated

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import pipeline, store
from app.llm import DEFAULT_MODEL, Claude

STATIC = store.ROOT / "app" / "static"
log = logging.getLogger("tab_trainer")
app = FastAPI(title="tab-trainer")


def _song(sid: str) -> dict:
    try:
        return store.load(sid)
    except KeyError:
        raise HTTPException(404, "曲子不存在") from None


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/trainer")
def trainer():
    return FileResponse(STATIC / "trainer.html")


@app.get("/files/{sid}/{name}")
def page_file(sid: str, name: str):
    _song(sid)
    p = store.song_dir(sid) / "pages" / name
    if "/" in name or not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
def _mask(key: str) -> str:
    return key[:7] + "…" + key[-4:] if len(key) > 14 else ("已设置" if key else "")


@app.get("/api/settings")
def get_settings():
    c = store.load_config()
    return {
        "api_key_masked": _mask(c.get("api_key", "")),
        "env_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "base_url": c.get("base_url", ""),
        "model": c.get("model") or DEFAULT_MODEL,
        "effort": c.get("effort") or "high",
    }


@app.put("/api/settings")
def put_settings(body: Annotated[dict, Body()]):
    c = store.load_config()
    if body.get("api_key"):
        c["api_key"] = body["api_key"].strip()
    if body.get("clear_api_key"):
        c.pop("api_key", None)
    for k in ("base_url", "model", "effort"):
        if k in body:
            c[k] = (body[k] or "").strip()
    store.save_config(c)
    return get_settings()


@app.post("/api/settings/test")
def test_settings():
    try:
        Claude(pipeline.llm_config()).ping()
    except Exception as e:  # noqa: BLE001 - 任何错误都原样展示给用户
        return {"ok": False, "error": pipeline._friendly(e)}
    return {"ok": True}


# ---------------------------------------------------------------------------
# 曲子
# ---------------------------------------------------------------------------
@app.get("/api/songs")
def songs():
    return store.list_songs()


@app.post("/api/songs")
async def upload(files: Annotated[list[UploadFile], File()], title: Annotated[str, Form()] = ""):
    uploads = [(f.filename or "upload", await f.read()) for f in files]
    try:
        sid = pipeline.create_song(uploads, title)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except Exception as e:  # noqa: BLE001 - 图片/PDF 解码失败的异常类型很多
        raise HTTPException(400, f"文件无法读取：{e}") from None
    return {"id": sid}


@app.get("/api/songs/{sid}")
def song(sid: str):
    s = _song(sid)
    s["running"] = pipeline.is_running(sid)
    return s


@app.delete("/api/songs/{sid}")
def delete_song(sid: str):
    _song(sid)
    if pipeline.is_running(sid):
        raise HTTPException(409, "正在识别中，完成后再删除")
    shutil.rmtree(store.song_dir(sid))
    return {"ok": True}


@app.post("/api/songs/{sid}/parse")
def reparse_all(sid: str):
    _song(sid)
    if not pipeline.start_parse(sid):
        raise HTTPException(409, "已经在识别中")
    return {"ok": True}


@app.put("/api/songs/{sid}/meta")
def put_meta(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    try:
        return pipeline.edit_meta(sid, body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.put("/api/songs/{sid}/bars/{n}")
def put_bar(sid: str, n: int, body: Annotated[dict, Body()]):
    _song(sid)
    try:
        return pipeline.edit_bar(sid, n, body["events"], body.get("tip"))
    except KeyError:
        raise HTTPException(404, "小节不存在") from None
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f"小节数据格式不对：{e}") from None


@app.post("/api/songs/{sid}/systems/{si}/reparse")
def reparse_line(sid: str, si: int, body: Annotated[dict | None, Body()] = None):
    s = _song(sid)
    if not 0 <= si < len(s.get("systems", [])):
        raise HTTPException(404, "这一行不存在")
    if pipeline.is_running(sid):
        raise HTTPException(409, "正在识别中")
    hint = ((body or {}).get("hint") or "").strip()

    def run():
        try:
            pipeline.reparse_system(sid, si, hint)
        except Exception as e:
            log.exception("request_id=unknown song=%s system=%d reparse failed", sid, si)
            msg = pipeline._friendly(e)
            store.update(sid, lambda x: x["systems"][si].update(status="error", error=msg))

    store.update(sid, lambda x: x["systems"][si].update(status="parsing", error=None))
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main() -> None:
    ap = argparse.ArgumentParser(description="tab-trainer 本地服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import uvicorn

    print(f"\n  tab-trainer 已启动：http://{a.host}:{a.port}\n")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
