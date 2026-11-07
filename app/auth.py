"""访问控制。

- Host 校验：只接受 localhost、IP 地址和局域网的 .local 名字。挡住 DNS rebinding——恶意网页把自己的域名
  解析到 127.0.0.1 后，浏览器发来的 Host 还是那个域名；不挡的话它能读写所有接口（包括把模型地址改成
  自己的服务器、骗走 API key）。本机来的请求不用令牌，所以更严：.local 只认这台电脑自己的名字
  （局域网里别人可以用 mDNS 把任意 xxx.local 指向 127.0.0.1）。
- 跨站写请求：POST / PUT / DELETE 带的 Origin 和 Host 不一致就拒绝（别的网站用表单偷偷上传、花模型额度）。
  不带 Origin 的（curl 等非浏览器客户端）放行。
- 局域网访问令牌：服务绑定到非本机地址时 app.state.token 才会被设置（见 main.main）：
  本机访问不需要令牌；其他设备要用带 ?token= 的地址打开一次，校验通过后令牌写进 Cookie。
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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


def _own_local_names() -> set[str]:
    name = socket.gethostname().lower().removesuffix(".local")
    return {name + ".local"} if name else set()


def _host_ok(host: str, loopback: bool = False) -> bool:
    """Host 头（可能带端口）是不是本机 / 局域网能直接访问的名字。loopback：请求来自本机（不需要令牌）。"""
    name = (urlsplit("//" + host).hostname or "").lower()
    if name == "localhost":
        return True
    if name.endswith(".local"):
        return not loopback or name in _own_local_names()
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def install(app: FastAPI) -> None:
    app.state.token = None

    @app.middleware("http")
    async def require_token(request: Request, call_next):
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
        if request.url.path.startswith(("/api/", "/files/")):
            return JSONResponse({"detail": "需要访问令牌"}, status_code=401)
        return HTMLResponse(DENIED_PAGE, status_code=401)

    # 后注册的中间件先执行：先挡掉不认识的 Host 和跨站写请求，再看令牌
    @app.middleware("http")
    async def check_origin(request: Request, call_next):
        host = request.headers.get("host", "")
        client = request.client.host if request.client else ""
        if not _host_ok(host, loopback=client in LOOPBACK):
            return JSONResponse(
                {"detail": "不接受这个访问地址，请用 127.0.0.1、localhost 或局域网 IP 打开"}, 403
            )
        origin = request.headers.get("origin")
        if request.method in UNSAFE and origin is not None and urlsplit(origin).netloc != host:
            return JSONResponse({"detail": "拒绝来自其他网站的请求"}, 403)
        return await call_next(request)
