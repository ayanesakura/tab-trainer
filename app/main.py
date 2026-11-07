"""FastAPI 服务：曲库 / 上传 / 识别进度 / 修改 / 模型设置，以及前端静态页面。

启动：uv run python -m app.main   （默认 http://127.0.0.1:8765）
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import auth, coach, jobs, recognize, songs, store
from app.errors import AppError
from app.llm import DEFAULT_MODEL

STATIC = store.ROOT / "app" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    songs.recover_interrupted()  # 上次服务关掉时没跑完的任务，标成中断
    yield


app = FastAPI(title="tab-trainer", lifespan=lifespan)
auth.install(app)


@app.exception_handler(store.CorruptSong)
async def corrupt_song(_request: Request, e: store.CorruptSong):
    """song.json 和备份都坏了：说明情况，曲库里可以删掉这首。"""
    return JSONResponse({"detail": str(e)}, status_code=500)


@app.exception_handler(AppError)
async def app_error(_request: Request, e: AppError):
    """业务错误（见 app/errors.py）：按类型返回 400 / 404 / 409 / 502，消息原样给前端显示。"""
    return JSONResponse({"detail": str(e)}, status_code=e.status)


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


@app.get("/files/{sid}/source")
def source_file(sid: str):
    s = _song(sid)
    if not s.get("source"):
        raise HTTPException(404)
    return FileResponse(store.song_dir(sid) / s["source"])


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
        jobs.new_claude().ping()
    except Exception as e:  # noqa: BLE001 - 任何错误都原样展示给用户
        return {"ok": False, "error": jobs.friendly(e)}
    return {"ok": True}


# ---------------------------------------------------------------------------
# 曲子
# ---------------------------------------------------------------------------
@app.get("/api/songs")
def list_songs():
    return store.list_songs()


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """表单解析之前就按 Content-Length 挡掉过大的上传（否则 Starlette 会先把整个请求收下来存到临时文件）。"""
    if request.method == "POST" and request.url.path == "/api/songs":
        try:
            size = int(request.headers.get("content-length") or 0)
        except ValueError:
            size = 0
        if size > songs.MAX_TOTAL_BYTES + 1024 * 1024:
            return JSONResponse({"detail": f"文件总大小超过 {songs.MAX_TOTAL_BYTES // 1024 // 1024} MB"}, 413)
    return await call_next(request)


@app.post("/api/songs")
def upload(files: Annotated[list[UploadFile], File()], title: Annotated[str, Form()] = ""):
    # 普通 def：FastAPI 放到线程池里跑，PDF 渲染、PNG 压缩不会卡住其他请求
    if len(files) > songs.MAX_FILES:
        raise HTTPException(400, f"一次最多上传 {songs.MAX_FILES} 个文件")
    uploads = []
    for f in files:
        if f.size is not None and f.size > songs.MAX_FILE_BYTES:
            raise HTTPException(
                400, f"“{f.filename}”太大了（单个文件最大 {songs.MAX_FILE_BYTES // 1024 // 1024} MB）"
            )
        uploads.append((f.filename or "upload", f.file.read(songs.MAX_FILE_BYTES + 1)))
    try:
        sid = songs.create_song(uploads, title)
    except AppError:
        raise
    except Exception as e:  # noqa: BLE001 - 图片/PDF 解码失败的异常类型很多
        raise HTTPException(400, f"文件无法读取：{e}") from None
    if not songs.is_score(store.load(sid)):
        recognize.start_parse(sid)
    return {"id": sid}


@app.get("/api/songs/{sid}")
def song(sid: str):
    s = _song(sid)
    if any(not b.get("id") for b in s.get("bars", [])):  # 旧格式数据：补上小节 id，前端按 id 定位小节
        s = store.update(sid, songs.ensure_bar_ids)
    s["running"] = jobs.jobs.running(sid)
    return s


@app.delete("/api/songs/{sid}")
def delete_song(sid: str):
    try:
        _song(sid)
    except store.CorruptSong:
        pass  # 数据坏了的曲子也要能删掉
    if jobs.jobs.running(sid):
        raise HTTPException(409, "正在识别中，完成后再删除")
    shutil.rmtree(store.song_dir(sid))
    return {"ok": True}


@app.post("/api/songs/{sid}/parse")
def reparse_all(sid: str):
    if songs.is_score(_song(sid)):
        raise HTTPException(400, "Guitar Pro / MusicXML 曲子不需要识别")
    jobs.check_llm_ready()
    if not recognize.start_parse(sid):
        raise HTTPException(409, "已经在识别中")
    return {"ok": True}


@app.put("/api/songs/{sid}/derived")
def put_derived(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    return songs.set_derived(
        sid, body.get("meta") or {}, body.get("bars") or [], body.get("track") or 0, body.get("derived")
    )


@app.post("/api/songs/{sid}/import-error")
def import_error(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    return songs.import_failed(sid, str(body.get("error") or ""))


@app.post("/api/songs/{sid}/explain")
def explain(sid: str):
    _song(sid)
    coach.start_explain(sid)
    return {"ok": True}


@app.post("/api/songs/{sid}/cancel")
def cancel(sid: str):
    _song(sid)
    if not jobs.jobs.cancel(sid):
        raise HTTPException(409, "没有正在进行的识别")
    return {"ok": True}


@app.put("/api/songs/{sid}/meta")
def put_meta(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    return songs.edit_meta(sid, body)


@app.put("/api/songs/{sid}/bars/{bar_id}")
def put_bar(sid: str, bar_id: str, body: Annotated[dict, Body()]):
    _song(sid)
    if "events" not in body:
        raise HTTPException(400, "缺少 events")
    return songs.edit_bar(sid, bar_id, body["events"], body.get("tip"))


@app.post("/api/songs/{sid}/bars/{bar_id}/ask")
def ask_bar(sid: str, bar_id: str, body: Annotated[dict, Body()]):
    _song(sid)
    return coach.ask_bar(sid, bar_id, str(body.get("question") or ""))


@app.post("/api/songs/{sid}/practice")
def practice(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    return songs.record_practice(sid, body)


@app.post("/api/songs/{sid}/systems/{si}/reparse")
def reparse_line(sid: str, si: int, body: Annotated[dict | None, Body()] = None):
    s = _song(sid)
    if not 0 <= si < len(s.get("systems", [])):
        raise HTTPException(404, "这一行不存在")
    recognize.start_reparse(sid, si, str((body or {}).get("hint") or "").strip())
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _lan_addresses() -> list[str]:
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # 不会真的发包，只是让系统选出对外的网卡地址
            return [s.getsockname()[0]]
    except OSError:
        return ["<本机的局域网 IP>"]


def main() -> None:
    ap = argparse.ArgumentParser(description="tab-trainer 本地服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import uvicorn

    if not store.lock_data_dir():
        # 任务登记和存储锁都在进程内：两个实例同时用一个数据目录会重复识别、互相覆盖
        raise SystemExit(
            f"\n  另一个 tab-trainer 正在使用数据目录 {store.DATA}，请先关掉它（或用 TAB_TRAINER_DATA 指定别的目录）。\n"
        )

    if a.host in auth.LOOPBACK:
        print(f"\n  tab-trainer 已启动：http://{a.host}:{a.port}\n")
    else:
        # 开放给局域网：其他设备必须带令牌（谱子、API key 设置、删除和会产生费用的模型调用都在这些接口后面）
        app.state.token = os.environ.get("TAB_TRAINER_TOKEN") or secrets.token_urlsafe(16)
        print(f"\n  tab-trainer 已启动，本机打开：http://127.0.0.1:{a.port}")
        for ip in _lan_addresses():
            print(f"  局域网里的其他设备打开：http://{ip}:{a.port}/?token={app.state.token}")
        print("  （令牌每次启动都会变；想固定可以设置环境变量 TAB_TRAINER_TOKEN）\n")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
