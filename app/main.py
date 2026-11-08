"""FastAPI 服务：只做 HTTP（路由、参数、文件、响应）。业务编排在 app/service.py，返回的形状在 app/views.py。

启动：uv run python -m app.main   （默认 http://127.0.0.1:8765）
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from app import auth, config, schemas, service, store
from app.config import settings
from app.errors import AppError

STATIC = store.ROOT / "app" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    service.startup()  # 上次服务关掉时没跑完的任务标成中断；旧格式的曲子存成当前格式
    yield


app = FastAPI(title="tab-trainer", lifespan=lifespan)
auth.install(app)


@app.exception_handler(RequestValidationError)
async def bad_request(_request: Request, e: RequestValidationError):
    """请求体格式不对：400 + 一句中文（默认的 422 是一串英文，前端没法直接显示）。"""
    return JSONResponse({"detail": schemas.describe(e.errors())}, status_code=400)


@app.exception_handler(AppError)
async def app_error(_request: Request, e: AppError):
    """业务错误（见 app/errors.py，含模型错误和数据损坏）：按类型返回 400 / 404 / 409 / 423 / 500 / 502，消息原样给前端显示。"""
    return JSONResponse({"detail": str(e)}, status_code=e.status)


def _files(files: list[UploadFile]) -> list[tuple[str, bytes]]:
    return service.read_uploads([(f.filename or "upload", f.size, f.file) for f in files])


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
    return FileResponse(service.source_file(sid))


@app.get("/files/{sid}/{name}")
def page_file(sid: str, name: str):
    return FileResponse(service.page_file(sid, name))


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
@app.get("/api/settings")
def get_settings():
    return config.public_settings()


@app.put("/api/settings")
def put_settings(body: schemas.SettingsIn):
    fields = {
        k: getattr(body, k)
        for k in body.model_fields_set & {"base_url", "model", "effort", "double_check", "refine"}
    }
    return config.update_settings(body.api_key, body.clear_api_key, fields)


@app.post("/api/settings/test")
def test_settings():
    return config.test_connection()


# ---------------------------------------------------------------------------
# 曲库
# ---------------------------------------------------------------------------
@app.get("/api/songs")
def list_songs():
    return service.library()


def _upload_limit(method: str, path: str) -> int | None:
    """上传接口的请求体上限（字节，多给 1 MB 表单开销）；其他请求不限。"""
    if method != "POST":
        return None
    limit = {"/api/songs": settings.max_total_bytes, "/api/import": settings.max_import_bytes}.get(path)
    if path.startswith("/api/songs/") and path.endswith("/pages"):
        limit = settings.max_total_bytes
    return limit


class _TooLarge(Exception):
    pass


class LimitUploadSize:
    """表单解析之前就挡掉过大的上传（否则 Starlette 会先把整个请求收下来存到临时文件）：
    有 Content-Length 的直接看它；分块上传（没有 Content-Length）边收边数，超过上限就不再往下读、回 413。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        limit = (
            _upload_limit(scope.get("method", ""), scope.get("path", "")) if scope["type"] == "http" else None
        )
        if not limit:
            return await self.app(scope, receive, send)
        too_large = JSONResponse({"detail": f"文件总大小超过 {limit // 1024 // 1024} MB"}, 413)
        headers = dict(scope.get("headers") or [])
        try:
            size = int(headers.get(b"content-length") or 0)
        except ValueError:
            size = 0
        if size > limit + 1024 * 1024:
            return await too_large(scope, receive, send)
        got, tripped, started = 0, False, False

        async def counted():
            nonlocal got, tripped
            message = await receive()
            if message["type"] == "http.request":
                got += len(message.get("body") or b"")
                if got > limit + 1024 * 1024:
                    tripped = True
                    raise _TooLarge  # 表单解析会把它变成 400：下面按 tripped 改成 413
            return message

        async def guarded(message):
            nonlocal started
            if tripped:  # 读到一半超了：应用给的（解析失败之类的）响应不要，换成 413
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, guarded)
        except _TooLarge:
            pass
        except Exception:
            if not tripped:
                raise
        if tripped and not started:
            await too_large(scope, receive, send)


app.add_middleware(LimitUploadSize)


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """页面和前端代码每次都向服务器确认有没有更新（没变只回 304）。不加的话浏览器按“最后修改时间”自己猜能缓存多久，
    普通刷新只重新验证 HTML，JS 模块还用旧的：改了前端，用户刷新也看不到。"""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/trainer") or path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.post("/api/songs")
def upload(files: Annotated[list[UploadFile], File()], title: Annotated[str, Form()] = ""):
    # 普通 def：FastAPI 放到线程池里跑，PDF 渲染、PNG 压缩不会卡住其他请求
    return {"id": service.upload(_files(files), title)}


@app.get("/api/export")
def export(ids: Annotated[list[str], Query()] = []):  # noqa: B006 - FastAPI 的查询参数默认值
    """导出曲子包（zip）。不带 ids 时导出整个曲库。"""
    path, name = service.export_bundle(ids)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=name,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@app.post("/api/import")
def import_bundles(files: Annotated[list[UploadFile], File()]):
    """导入一个或多个曲子包，每个包里可以有多首曲子。"""
    return service.import_bundles([(f.filename or "曲子包", f.file) for f in files])


# ---------------------------------------------------------------------------
# 一首曲子
# ---------------------------------------------------------------------------
@app.get("/api/songs/{sid}")
def song(sid: str):
    return service.song(sid)


@app.delete("/api/songs/{sid}")
def delete_song(sid: str):
    service.delete(sid)
    return {"ok": True}


@app.post("/api/songs/{sid}/parse")
def reparse_all(sid: str):
    service.reparse_all(sid)
    return {"ok": True}


@app.post("/api/songs/{sid}/retry-failed")
def retry_failed(sid: str):
    """只重新识别失败的行（一个小节都还没有时整首识别）。"""
    service.retry_failed(sid)
    return {"ok": True}


@app.put("/api/songs/{sid}/derived")
def put_derived(sid: str, body: schemas.DerivedIn):
    derived = body.derived.model_dump() if body.derived else None
    return service.set_derived(sid, body.meta, body.bars, body.track, derived)


@app.post("/api/songs/{sid}/import-error")
def import_error(sid: str, body: schemas.ImportErrorIn):
    return service.import_failed(sid, body.error)


@app.post("/api/songs/{sid}/pages")
def arrange_pages(
    sid: str,
    order: Annotated[str, Form(max_length=20000)],
    files: Annotated[list[UploadFile], File()] = [],  # noqa: B006 - FastAPI 的表单参数默认值
):
    """补页、删页、调整页序（order 是 JSON 数组：已有页的文件名或 "new:k"）。新页上的行在后台识别。"""
    return service.arrange_pages(sid, schemas.page_order(order), _files(files))


@app.put("/api/songs/{sid}/tags")
def put_tags(sid: str, body: schemas.TagsIn):
    return service.set_tags(sid, body.tags)


@app.post("/api/songs/{sid}/explain")
def explain(sid: str):
    service.explain(sid)
    return {"ok": True}


@app.post("/api/songs/{sid}/refine")
def refine(sid: str, body: schemas.RefineIn | None = None):
    """精修：逐小节放大核对（慢、贵）。不带请求体 = 整首；{"bars": [...]} / {"system": 行号} = 这几个小节 / 这一行
    （手改过、确认过的也核对），可以带 hint。小节 / 行不存在、GP / MusicXML 曲子 400，有任务在跑 423。"""
    b = body or schemas.RefineIn()
    service.refine_song(sid, b.bars, b.system, b.hint)
    return {"ok": True}


@app.post("/api/songs/{sid}/cancel")
def cancel(sid: str):
    service.cancel(sid)
    return {"ok": True}


@app.put("/api/songs/{sid}/meta")
def put_meta(sid: str, body: schemas.MetaIn):
    fields = body.model_dump(exclude_unset=True, exclude={"rev"})
    return service.edit_meta(sid, fields, body.rev)


@app.put("/api/songs/{sid}/bars/{bar_id}")
def put_bar(sid: str, bar_id: str, body: schemas.BarIn):
    events = [e.model_dump() for e in body.events]
    return service.edit_bar(sid, bar_id, events, body.tip, body.rev)


@app.put("/api/songs/{sid}/bars/{bar_id}/review")
def review_bar(sid: str, bar_id: str, body: schemas.ReviewIn):
    """待核对的小节：确认识别没问题（ok=false 撤销）。"""
    return service.review_bar(sid, bar_id, body.ok, body.rev)


@app.put("/api/songs/{sid}/bars/{bar_id}/undo-refine")
def undo_refine(sid: str, bar_id: str, body: schemas.RevIn):
    """撤销这个小节的精修改动（音、提示、不确定说明恢复成精修之前的）。"""
    return service.undo_refine(sid, bar_id, body.rev)


@app.post("/api/songs/{sid}/bars/{bar_id}/ask")
def ask_bar(sid: str, bar_id: str, body: schemas.AskIn):
    return service.ask_bar(sid, bar_id, body.question)


@app.post("/api/songs/{sid}/practice")
def practice(sid: str, body: schemas.PracticeIn):
    return service.record_practice(sid, body.bar_ids, body.loops, body.bpm, body.seconds)


@app.get("/api/songs/{sid}/practice/history")
def practice_history(sid: str):
    return service.practice_history(sid)


@app.post("/api/songs/{sid}/systems/{si}/reparse")
def reparse_line(sid: str, si: int, body: schemas.ReparseIn | None = None):
    service.reparse_row(sid, si, body.hint if body else "")
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
