"""曲库页：曲子包导出 / 导入、搜索和标签、菜单、外观、管理原谱页面、键盘和浮层。"""

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
        b.js("tt.reload().then(() => true)")
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


def test_search_tags_menu_and_theme(server, browser):
    b = browser
    img, gp = server["image"], server["gp"]
    _api(
        server,
        f"/api/songs/{img}/tags",
        "PUT",
        b'{"tags": ["e2e-tag"]}',
        {"Content-Type": "application/json"},
    )
    try:
        b.open(server["base"] + "/")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{img}\"] .tags')")
        # 点标签只看这个标签的曲子；搜索按曲名过滤
        b.js("document.querySelector('#tagFilter [data-tag=\"e2e-tag\"]').click(); true")
        assert b.js("[...document.querySelectorAll('.song-row')].map(r => r.dataset.id)") == [img]
        b.js("document.querySelector('#tagFilter [data-tag=\"\"]').click(); true")
        b.js(
            "(s => { s.value = 'E2E 图片'; s.dispatchEvent(new Event('input')); })(document.getElementById('search')); true"
        )
        assert b.js("[...document.querySelectorAll('.song-row')].map(r => r.dataset.id)") == [img]
        b.js(
            "(s => { s.value = ''; s.dispatchEvent(new Event('input')); })(document.getElementById('search')); true"
        )
        # GP 曲子的菜单没有“管理页面”和“整首重新识别”
        b.js(f"document.querySelector('[data-more=\"{gp}\"]').click(); true")
        items = b.js("[...document.querySelectorAll('.menu button')].map(x => x.textContent)")
        assert "重命名" in items and "管理原谱页面" not in items and "整首重新识别" not in items
        b.js(f"document.querySelector('[data-more=\"{img}\"]').click(); true")
        assert "管理原谱页面" in b.js(
            "[...document.querySelectorAll('.menu button')].map(x => x.textContent)"
        )
        # 外观按钮：跟随系统 → 浅色 → 深色 → 跟随系统；<html data-theme> 总是实际生效的 light / dark
        b.js("localStorage.setItem('tt-theme', 'system'); true")
        b.open(server["base"] + "/")
        assert b.js("document.documentElement.dataset.theme") in ("light", "dark")
        seen = []
        for _ in range(3):
            b.js("document.getElementById('themeBtn').click(); true")
            seen.append(
                (b.js("localStorage.getItem('tt-theme')"), b.js("document.documentElement.dataset.theme"))
            )
        assert [t for t, _ in seen] == ["light", "dark", "system"]
        assert seen[0][1] == "light" and seen[1][1] == "dark" and seen[2][1] in ("light", "dark")
        assert not b.errors, b.errors
    finally:
        _api(server, f"/api/songs/{img}/tags", "PUT", b'{"tags": []}', {"Content-Type": "application/json"})


def test_reorder_and_delete_pages(server, browser):
    from app import recognize, songs
    from tests.fakes import score_png

    store = server["store"]
    sid = songs.create_song([("a.png", score_png()), ("b.png", score_png())], title="E2E 两页")
    song = store.load(sid)
    systems = recognize.plan_systems(sid, song)
    bars = []
    for si, s in enumerate(systems):
        s["status"] = "done"
        for a, z in s["spans"]:
            n = len(bars) + 1
            ev = [
                {"dur": "1", "notes": [{"string": 1, "fret": n % 5, "tech": [], "grace": -1, "bend": 0}]}
            ] * 4
            bars.append({"id": f"{sid[-4:]}b{n}", "n": n, "system": si, "page": s["page"], "region": [a, z, *s["crop"][2:]],
                         "events": ev, "tip": "", "uncertain": "", "warn": []})  # fmt: skip
    meta = {
        "title": "E2E 两页",
        "tuning": ["E4", "B3", "G3", "D3", "A2", "E2"],
        "capo": 0,
        "time_signature": [4, 4],
    }
    store.update(sid, lambda x: x.update(meta=meta, systems=systems, bars=bars, status="ready"))
    ids = [b["id"] for b in bars]
    try:
        b = browser
        b.open(server["base"] + "/")
        b.wait_for(f"!!document.querySelector('[data-more=\"{sid}\"]')")
        b.js(f"document.querySelector('[data-more=\"{sid}\"]').click(); true")
        b.js(
            "[...document.querySelectorAll('.menu button')].find(x => x.textContent === '管理原谱页面').click(); true"
        )
        b.wait_for("document.querySelectorAll('.page-card').length === 2")
        assert b.js("document.getElementById('pagesSave').disabled") is True
        b.js('document.querySelector(\'.page-card[data-i="1"] [data-mv="-1"]\').click(); true')
        assert "调整了顺序" in b.js("document.getElementById('pagesMsg').textContent")
        b.js("document.getElementById('pagesSave').click(); true")
        b.wait_for("!document.getElementById('pagesDlg').open")
        after = json.loads(_api(server, f"/api/songs/{sid}"))
        assert [p["file"] for p in after["pages"]] == ["p2.png", "p1.png"]
        assert [x["id"] for x in after["bars"]] == ids[6:] + ids[:6]
        # 删掉一页：确认后它上面的小节一起删掉
        b.js(f"tt.openPages('{sid}').then(() => true)")
        b.wait_for("document.querySelectorAll('.page-card').length === 2")
        b.js("document.querySelector('.page-card[data-i=\"0\"] [data-rm]').click(); true")
        assert "删除 1 页（6 小节）" in b.js("document.getElementById('pagesMsg').textContent")
        b.js("document.getElementById('pagesSave').click(); true")
        # 界面里的确认框（不是原生 confirm），叠在“管理原谱页面”上面
        b.wait_for("!!document.querySelector('.dlg-msg[open] [data-ok]')")
        assert "6 个小节" in b.js("document.querySelector('.dlg-msg').textContent")
        b.js("document.querySelector('.dlg-msg [data-ok]').click(); true")
        b.wait_for("!document.getElementById('pagesDlg').open")
        after = json.loads(_api(server, f"/api/songs/{sid}"))
        assert [p["file"] for p in after["pages"]] == ["p1.png"] and [x["id"] for x in after["bars"]] == ids[
            :6
        ]
        assert not b.errors, b.errors
    finally:
        _api(server, f"/api/songs/{sid}", "DELETE")


def test_keyboard_overlays_and_focus(server, browser):
    b = browser
    img = server["image"]
    b.open(server["base"] + "/")
    b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{img}\"]')")
    # 上传框能用键盘打开文件选择
    assert b.js(
        "(d => d.tabIndex === 0 && d.getAttribute('role') === 'button')(document.getElementById('drop'))"
    )
    b.js(
        "window.__picked = 0; document.getElementById('file').click = () => window.__picked++; document.getElementById('drop').focus(); true"
    )
    b.key("Enter", vk=13)
    b.key(" ", code="Space", vk=32)
    assert b.js("window.__picked") == 2
    # 顶栏导航是 tablist；排序是带 aria-pressed 的分段开关
    assert b.js("document.getElementById('nav').getAttribute('role')") == "tablist"
    assert b.js("document.querySelector('#nav [data-v=library]').getAttribute('aria-selected')") == "true"
    assert b.js("document.querySelectorAll('#sortSeg [aria-pressed=true]').length") == 1
    # 菜单：焦点移进菜单，Esc 关掉后回到“更多”按钮；hasLayer 反映是否有浮层
    more = f"document.querySelector('[data-more=\"{img}\"]')"
    b.js(f"{more}.focus(); {more}.click(); true")
    assert b.js("!!document.querySelector('.menu') && document.activeElement.closest('.menu') !== null")
    assert b.js("UI.hasLayer()") is True
    b.key("Escape", vk=27)
    assert b.js("!document.querySelector('.menu')")
    assert b.js(f"document.activeElement === {more}")
    assert b.js("UI.hasLayer()") is False
    # 确认框：Esc 取消（返回 false），点确定返回 true
    b.js("window.__r = 'pending'; UI.confirm('测试？').then(v => { window.__r = v; }); true")
    b.wait_for("!!document.querySelector('.dlg-msg[open]')")
    b.key("Escape", vk=27)
    b.wait_for("window.__r !== 'pending'")
    assert b.js("window.__r") is False and b.js("!document.querySelector('.dlg-msg')")
    b.js("window.__r = 'pending'; UI.confirm('测试？').then(v => { window.__r = v; }); true")
    b.wait_for("!!document.querySelector('.dlg-msg[open]')")
    b.js("document.querySelector('.dlg-msg [data-ok]').click(); true")
    b.wait_for("window.__r === true")
    # 轮询刷新列表时，没变的行原样保留，焦点不丢
    b.js(f"{more}.focus(); window.__row = {more}.closest('.song-row'); true")
    b.js("tt.reload().then(() => true)")
    assert b.js(f"document.activeElement === {more} && window.__row === {more}.closest('.song-row')")
    assert not b.errors, b.errors


def test_rows_show_source_and_no_initial_tiles(server, browser):
    # 取代 test_trainer.py 里的 test_library_lists_songs 的来源部分：来源是图标 + 说明行里的文字，不再是曲名旁的徽章
    b = browser
    b.open(server["base"] + "/")
    b.wait_for("document.querySelectorAll('.song-row').length >= 3")
    src = b.js(
        "Object.fromEntries([...document.querySelectorAll('.song-row')].map(r => [r.dataset.id, r.querySelector('.src-ic').title]))"
    )
    assert src[server["image"]] == "来源：图片" and src[server["gp"]] == "来源：GP"
    assert b.js("document.querySelectorAll('.song-row .tile').length") == 0
    sub = b.js(f"document.querySelector('.song-row[data-id=\"{server['gp']}\"] .sub').textContent")
    assert sub.startswith("GP")
    assert not b.errors, b.errors


def test_practice_line_and_resume_link(server, browser):
    img = server["image"]
    body = b'{"bar_ids": ["bar2", "bar3"], "loops": 2, "bpm": 60, "seconds": 1500}'
    _api(server, f"/api/songs/{img}/practice", "POST", body, {"Content-Type": "application/json"})
    b = browser
    b.open(server["base"] + "/")
    row = f"document.querySelector('.song-row[data-id=\"{img}\"]')"
    b.wait_for(f"!!{row} && !!{row}.querySelector('.practice')")
    text = b.js(f"{row}.querySelector('.practice').textContent")
    assert "练到第 2–3 小节" in text and "累计 25 分钟" in text
    assert (
        b.js(f"{row}.querySelector('[data-k=resume]').getAttribute('href')") == f"/trainer?id={img}&resume=1"
    )
    # 按最近练习排序时练过的排最前
    b.js("document.querySelector('#sortSeg [data-v=practiced]').click(); true")
    assert b.js("document.querySelector('.song-row').dataset.id") == img
    assert not b.errors, b.errors


def _patch_songs_response(b, fn_js):
    """把页面里 /api/songs 的返回改一下（fn_js：songs => void），不动真实数据。"""
    b.js(f"""(() => {{ const of = window.fetch; window.fetch = async (u, init) => {{
      const r = await of(u, init);
      if (!String(u).endsWith('/api/songs')) return r;
      const songs = await r.json(); ({fn_js})(songs);
      return new Response(JSON.stringify(songs), {{ status: 200, headers: {{ 'Content-Type': 'application/json' }} }});
    }}; return true; }})()""")


def test_practice_line_escapes_backend_text(server, browser):
    # 练习时间认不出时 ago() 原样返回：不能当 HTML 插进去
    img = server["image"]
    b = browser
    b.open(server["base"] + "/")
    b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{img}\"]')")
    evil = '<img src=x onerror="window.__xss=true">'  # 不能带数字，否则 Date 能“认出”它
    _patch_songs_response(
        b,
        f"songs => songs.forEach(s => {{ if (s.id === '{img}') {{ s.practice = {{ last_at: {json.dumps(evil)}, total_seconds: 60, last: null }}; }} }})",
    )
    b.js("tt.reload().then(() => true)")
    row = f"document.querySelector('.song-row[data-id=\"{img}\"]')"
    b.wait_for(f"!!{row}.querySelector('.practice')")
    assert b.js(f"{row}.querySelector('.practice img')") is None
    assert evil in b.js(f"{row}.querySelector('.practice').textContent")
    assert not b.js("window.__xss")
    assert not b.errors, b.errors


def test_enter_while_composing_does_not_submit(server, browser):
    # 输入法选词时按回车只是确认候选词，不能提交（曲名、API key）
    b = browser
    b.open(server["base"] + "/")
    b.wait_for("!!document.getElementById('title')")
    b.js("""window.__clicks = [];
      for (const id of ['uploadBtn', 'saveKey']) document.getElementById(id).click = () => window.__clicks.push(id);
      for (const id of ['title', 'apiKey']) for (const isComposing of [true, false])
        document.getElementById(id).dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing, bubbles: true }));
      true""")
    assert b.js("window.__clicks") == ["uploadBtn", "saveKey"]
    assert not b.errors, b.errors


def test_search_keeps_hidden_selection(server, browser):
    img, gp = server["image"], server["gp"]
    b = browser
    b.open(server["base"] + "/")
    b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{gp}\"]')")
    b.js("document.getElementById('selectBtn').click(); true")
    for sid in (img, gp):
        b.js(f"document.querySelector('.song-row[data-id=\"{sid}\"] .who').click(); true")
    assert b.js("document.getElementById('selectCount').textContent") == "已选 2 首"
    search = "(s => {{ s.value = '{}'; s.dispatchEvent(new Event('input')); }})(document.getElementById('search')); true"
    b.js(search.format("E2E 图片"))
    assert "已选 2 首（1 首不在当前列表里）" == b.js("document.getElementById('selectCount').textContent")
    assert b.js("document.getElementById('selectAll').checked") is True  # 当前列表里的都选了
    b.js("tt.reload().then(() => true)")  # 轮询刷新也不丢
    b.js(search.format(""))
    assert b.js("[...document.querySelectorAll('.song-row.on')].map(r => r.dataset.id).sort()") == sorted(
        [img, gp]
    )
    b.js("document.getElementById('selectCancel').click(); true")
    assert not b.errors, b.errors


def test_drop_page_card_on_itself_is_noop(server, browser):
    b = browser
    b.open(server["base"] + "/")
    b.wait_for(f"!!document.querySelector('[data-more=\"{server['image']}\"]')")
    b.js(f"tt.openPages('{server['image']}').then(() => true)")
    b.wait_for("document.querySelectorAll('.page-card').length === 1")
    # 加一张新页（只在对话框里，不保存），凑成两张卡片
    b.js("""const dt = new DataTransfer(); dt.items.add(new File(['x'], 'new.png', { type: 'image/png' }));
      const input = document.getElementById('pagesFile'); input.files = dt.files;
      input.dispatchEvent(new Event('change')); true""")
    b.wait_for("document.querySelectorAll('.page-card').length === 2")
    # 第一张拖起来，放在它自己的右半边
    b.js("""(() => { const card = document.querySelector('.page-card[data-i="0"]'), r = card.getBoundingClientRect();
      const ev = (type, x) => new DragEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: r.top + r.height / 2, dataTransfer: new DataTransfer() });
      card.dispatchEvent(ev('dragstart', r.left + 5));
      card.dispatchEvent(ev('dragover', r.right - 5));
      card.dispatchEvent(ev('drop', r.right - 5));
      card.dispatchEvent(ev('dragend', r.right - 5)); return true; })()""")
    assert b.js("[...document.querySelectorAll('.page-card')].map(c => c.classList.contains('new'))") == [
        False,
        True,
    ]
    b.js("document.getElementById('pagesCancel').click(); true")
    b.wait_for("!document.getElementById('pagesDlg').open")
    assert not b.errors, b.errors
