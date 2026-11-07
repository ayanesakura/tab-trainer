"""给端到端测试用的极简 Chrome DevTools 客户端：无界面启动 Chrome，执行脚本、按键、截图、收集控制台错误。"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "",
]


def find_chrome() -> str | None:
    return next((c for c in CHROME_CANDIDATES if c and Path(c).exists()), None)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    """同步接口（内部用 asyncio）：with Browser() as b: b.open(url); b.js("1+1")。"""

    def __init__(self, width: int = 1400, height: int = 1800):
        self.width, self.height = width, height
        self.errors: list[str] = []
        self._loop = asyncio.new_event_loop()

    def __enter__(self):
        chrome = find_chrome()
        port = free_port()
        self._profile = tempfile.mkdtemp()
        self._proc = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self._profile}",
                "--no-proxy-server",
                "--autoplay-policy=no-user-gesture-required",
                f"--window-size={self.width},{self.height}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                break
            except OSError:
                time.sleep(0.1)
        url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
        self._run(self._connect(url))
        return self

    def __exit__(self, *exc):
        try:
            self._run(self._ws.close())
        finally:
            self._proc.terminate()
            self._proc.wait(timeout=10)
            shutil.rmtree(self._profile, ignore_errors=True)
            self._loop.close()

    # -- 内部 -------------------------------------------------------------
    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    async def _connect(self, url):
        import websockets

        self._ws = await websockets.connect(url, max_size=100_000_000)
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader = self._loop.create_task(self._read())
        for m in ("Runtime.enable", "Log.enable", "Page.enable"):
            await self._send(m)

    async def _read(self):
        async for raw in self._ws:
            d = json.loads(raw)
            if "id" in d and d["id"] in self._pending:
                self._pending.pop(d["id"]).set_result(d)
            elif d.get("method") == "Runtime.exceptionThrown":
                e = d["params"]["exceptionDetails"]
                self.errors.append((e.get("exception") or {}).get("description") or e.get("text", ""))
            elif d.get("method") == "Runtime.consoleAPICalled" and d["params"]["type"] == "error":
                self.errors.append(
                    " ".join(str(a.get("value", a.get("description", ""))) for a in d["params"]["args"])
                )

    async def _send(self, method, **params):
        self._id += 1
        fut = self._loop.create_future()
        self._pending[self._id] = fut
        await self._ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        d = await asyncio.wait_for(fut, 60)
        if "error" in d:
            raise RuntimeError(f"{method}: {d['error']}")
        return d.get("result", {})

    # -- 对外 -------------------------------------------------------------
    def open(self, url: str):
        """打开页面并等它加载完（之后的脚本才不会跑在上一个页面或空白页上）。"""
        self._run(self._send("Page.navigate", url=url))
        # 比较时去掉 #：练习页会在地址后面加上 #practice 这样的视图名
        self.wait_for(
            f"location.href.split('#')[0] === {url.split('#')[0]!r} && document.readyState === 'complete'"
        )

    def js(self, expr: str):
        """执行一段 JS（可以返回 Promise），返回结果；页面里抛异常时在测试里抛 AssertionError。"""
        r = self._run(self._send("Runtime.evaluate", expression=expr, awaitPromise=True, returnByValue=True))
        if "exceptionDetails" in r:
            ex = r["exceptionDetails"]
            raise AssertionError((ex.get("exception") or {}).get("description") or ex.get("text"))
        return r["result"].get("value")

    def wait_for(self, expr: str, timeout: float = 20, interval: float = 0.2):
        end, last = time.time() + timeout, None
        while time.time() < end:
            try:
                v = self.js(expr)
                if v:
                    return v
            except AssertionError as e:  # 页面还没准备好（元素还不存在等）：继续等
                last = e
            time.sleep(interval)
        raise AssertionError(f"超时：{expr}" + (f"（最后一次出错：{last}）" if last else ""))

    def key(
        self, key: str, code: str | None = None, vk: int = 0, modifiers: int = 0, text: str | None = None
    ):
        for t in ("keyDown", "keyUp"):
            extra = {"text": text} if t == "keyDown" and text else {}
            self._run(
                self._send(
                    "Input.dispatchKeyEvent",
                    type=t,
                    key=key,
                    code=code or key,
                    windowsVirtualKeyCode=vk,
                    modifiers=modifiers,
                    **extra,
                )
            )

    def screenshot(self, path: str):
        r = self._run(self._send("Page.captureScreenshot", format="png"))
        Path(path).write_bytes(base64.b64decode(r["data"]))
