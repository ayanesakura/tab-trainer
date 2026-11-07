"""曲库页的曲子包：勾选导出、再从导入按钮导回来。"""

import json
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


def _api(server, path, method="GET", body=None, headers=None):
    req = urllib.request.Request(server["base"] + path, data=body, method=method, headers=headers or {})
    return urllib.request.urlopen(req).read()


def test_export_selected_then_import(server, browser):
    # 单独复制一首 GP 曲子来导出、删除、再导入，不影响其他测试用的曲子
    src = _api(server, f"/files/{server['gp']}/source")
    boundary = "e2eboundary"
    form = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="copy.gp"\r\n\r\n'.encode()
        + src
        + f"\r\n--{boundary}--\r\n".encode()
    )
    sid = json.loads(
        _api(
            server, "/api/songs", "POST", form, {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
    )["id"]
    try:
        b = browser
        b.open(server["base"] + "/")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{sid}\"]')")
        # 下载改成记下文件名和内容（base64），不真的弹出保存
        b.js("""window.__dl = null;
          HTMLAnchorElement.prototype.click = function () {
            const name = this.download;
            fetch(this.href).then(r => r.blob()).then(bl => new Promise(ok => {
              const fr = new FileReader(); fr.onload = () => ok(fr.result.split(',')[1]); fr.readAsDataURL(bl);
            })).then(data => { window.__dl = { name, data }; });
          }; true""")
        b.js("document.getElementById('selectBtn').click(); true")
        assert b.js("document.getElementById('selectBar').hidden") is False
        assert b.js("document.getElementById('exportBtn').disabled") is True
        b.js(f"document.querySelector('.song-row[data-id=\"{sid}\"] .who').click(); true")
        assert b.js("document.getElementById('selectCount').textContent") == "已选 1 首"
        assert b.js("document.querySelectorAll('.song-row.on').length") == 1
        b.js("document.getElementById('exportBtn').click(); true")
        b.wait_for("!!window.__dl")
        assert b.js("window.__dl.name").endswith("曲子包.zip")
        assert b.js("document.getElementById('selectBar').hidden") is True  # 导出后退出选择模式

        _api(server, f"/api/songs/{sid}", "DELETE")
        b.js("loadSongs(); true")
        b.wait_for(f"!document.querySelector('.song-row[data-id=\"{sid}\"]')")
        # 把刚导出的包放进“导入曲子包”的文件框
        b.js("""const bin = atob(window.__dl.data), u8 = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
          const dt = new DataTransfer(); dt.items.add(new File([u8], window.__dl.name, { type: 'application/zip' }));
          const input = document.getElementById('bundleFile'); input.files = dt.files;
          input.dispatchEvent(new Event('change')); true""")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{sid}\"]')")
        assert "已导入 1 首" in b.js("document.querySelector('.toast').textContent")
        assert json.loads(_api(server, f"/api/songs/{sid}"))["source"] == "source.gp"
        assert not b.errors, b.errors
    finally:
        try:
            _api(server, f"/api/songs/{sid}", "DELETE")
        except OSError:
            pass
