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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import pipeline, store
from app.llm import DEFAULT_MODEL, Claude

STATIC = store.ROOT / "app" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pipeline.recover_interrupted()  # 上次服务关掉时没跑完的识别，标成中断
    yield


app = FastAPI(title="tab-trainer", lifespan=lifespan)
app.state.token = None  # 绑定到非本机地址时才设置（见 main）：局域网里的其他设备要带这个令牌才能访问

LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"}
TOKEN_COOKIE = "tt_token"
DENIED_PAGE = """<!doctype html><meta charset="utf-8"><title>tab-trainer</title>
<body style="font-family:system-ui;max-width:520px;margin:80px auto;line-height:1.7;color:#333">
<h2>需要访问令牌</h2><p>tab-trainer 开放了局域网访问，其他设备需要用带令牌的地址打开一次。</p>
<p>请在运行 tab-trainer 的电脑上，从启动时打印的信息里复制完整地址（以 <code>?token=</code> 结尾）。</p></body>"""


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(
        a.encode("utf-8"), b.encode("utf-8")
    )  # 直接比较 str 时非 ASCII 会抛 TypeError


@app.middleware("http")
async def require_token(request: Request, call_next):
    """局域网访问时校验令牌：本机访问不需要；带 ?token= 的地址校验通过后写进 Cookie 并去掉地址里的令牌。"""
    token = app.state.token
    client = request.client.host if request.client else ""
    if not token or client in LOOPBACK:
        return await call_next(request)
    given = request.query_params.get("token")
    if given is not None and _same(given, token):
        url = request.url.remove_query_params("token")
        resp = RedirectResponse(str(url), status_code=303)
        resp.set_cookie(TOKEN_COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax")
        return resp
    if _same(request.cookies.get(TOKEN_COOKIE, ""), token):
        return await call_next(request)
    if request.url.path.startswith("/api/") or request.url.path.startswith("/files/"):
        return JSONResponse({"detail": "需要访问令牌"}, status_code=401)
    return HTMLResponse(DENIED_PAGE, status_code=401)


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
    if any(not b.get("id") for b in s.get("bars", [])):  # 旧格式数据：补上小节 id，前端按 id 定位小节
        s = store.update(sid, pipeline.ensure_bar_ids)
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
    s = _song(sid)
    if s.get("kind") == "score":
        raise HTTPException(400, "Guitar Pro / MusicXML 曲子不需要识别")
    try:
        pipeline.check_llm_ready()
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if not pipeline.start_parse(sid):
        raise HTTPException(409, "已经在识别中")
    return {"ok": True}


@app.put("/api/songs/{sid}/derived")
def put_derived(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    try:
        return pipeline.set_derived(
            sid, body.get("meta") or {}, body.get("bars") or [], body.get("track") or 0, body.get("derived")
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/songs/{sid}/import-error")
def import_error(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    try:
        return pipeline.import_failed(sid, str(body.get("error") or ""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/songs/{sid}/explain")
def explain(sid: str):
    _song(sid)
    try:
        pipeline.start_explain(sid)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
    return {"ok": True}


@app.post("/api/songs/{sid}/cancel")
def cancel(sid: str):
    _song(sid)
    if not pipeline.cancel_parse(sid):
        raise HTTPException(409, "没有正在进行的识别")
    return {"ok": True}


@app.put("/api/songs/{sid}/meta")
def put_meta(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    try:
        return pipeline.edit_meta(sid, body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.put("/api/songs/{sid}/bars/{bar_id}")
def put_bar(sid: str, bar_id: str, body: Annotated[dict, Body()]):
    _song(sid)
    if "events" not in body:
        raise HTTPException(400, "缺少 events")
    try:
        return pipeline.edit_bar(sid, bar_id, body["events"], body.get("tip"))
    except pipeline.BarNotFound:
        raise HTTPException(404, "小节不存在（可能刚被重新识别，请刷新页面）") from None
    except ValueError as e:
        raise HTTPException(400, f"小节数据格式不对：{e}") from None


@app.post("/api/songs/{sid}/bars/{bar_id}/ask")
def ask_bar(sid: str, bar_id: str, body: Annotated[dict, Body()]):
    _song(sid)
    try:
        return pipeline.ask_bar(sid, bar_id, str(body.get("question") or ""))
    except pipeline.BarNotFound:
        raise HTTPException(404, "小节不存在（可能刚被重新识别，请刷新页面）") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except pipeline.CoachError as e:
        raise HTTPException(502, str(e)) from None


@app.post("/api/songs/{sid}/practice")
def practice(sid: str, body: Annotated[dict, Body()]):
    _song(sid)
    try:
        return pipeline.record_practice(sid, body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@app.post("/api/songs/{sid}/systems/{si}/reparse")
def reparse_line(sid: str, si: int, body: Annotated[dict | None, Body()] = None):
    s = _song(sid)
    if not 0 <= si < len(s.get("systems", [])):
        raise HTTPException(404, "这一行不存在")
    hint = str((body or {}).get("hint") or "").strip()
    try:
        pipeline.start_reparse(sid, si, hint)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from None
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

    if a.host in LOOPBACK:
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
