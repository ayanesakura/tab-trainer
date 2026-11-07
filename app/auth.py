"""局域网访问令牌。服务绑定到非本机地址时 app.state.token 才会被设置（见 main.main）：
本机访问不需要令牌；其他设备要用带 ?token= 的地址打开一次，校验通过后令牌写进 Cookie。"""

from __future__ import annotations

import secrets

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
