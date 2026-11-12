"""曲库页：曲子包导出 / 导入、搜索和标签、菜单、外观、管理原谱页面、键盘和浮层。"""

import contextlib
import json
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.e2e


def _api(server, path, method="GET", body=None, headers=None):
    req = urllib.request.Request(server["base"] + path, data=body, method=method, headers=headers or {})
    return urllib.request.urlopen(req).read()


def _fake_key(b, has_key):
    """页面里 GET /api/settings 的 has_key 当作 has_key（测试环境没有 key，也可能有 ANTHROPIC_API_KEY）：要在 open 之前调。"""
    b._run(
        b._send(
            "Page.addScriptToEvaluateOnNewDocument",
            source="""(() => { const of = window.fetch; window.fetch = async (u, init = {}) => {
              const r = await of(u, init);
              if (!String(u).endsWith('/api/settings') || (init.method || 'GET') !== 'GET') return r;
              const c = await r.json(); c.has_key = %s; if (!c.has_key) { c.api_key_masked = ''; c.env_key = false; }
              return new Response(JSON.stringify(c), { status: 200, headers: { 'Content-Type': 'application/json' } });
            }; })()"""
            % ("true" if has_key else "false"),
        )
    )


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
        assert "精修整首" not in items
        b.js(f"document.querySelector('[data-more=\"{img}\"]').click(); true")
        items = b.js("[...document.querySelectorAll('.menu button')].map(x => x.textContent)")
        assert "管理原谱页面" in items and "精修整首" in items
        # 外观只在设置 › 外观 › 明暗里改（顶栏没有切换按钮）；<html data-theme> 总是实际生效的 light / dark
        b.js("localStorage.setItem('tt-theme', 'system'); true")
        b.open(server["base"] + "/#settings")
        assert b.js("document.getElementById('themeBtn')") is None
        actions = b.js("[...document.querySelectorAll('.topbar .actions > *')].map(x => x.id)")
        assert actions == ["chatBtn"]  # 顶栏右边只有“问 AI”
        assert b.js("document.documentElement.dataset.theme") in ("light", "dark")
        seen = []
        for v in ("light", "dark", "system"):
            b.js(f"document.querySelector('#themeSeg [data-v={v}]').click(); true")
            seen.append(
                (b.js("localStorage.getItem('tt-theme')"), b.js("document.documentElement.dataset.theme"))
            )
        assert [t for t, _ in seen] == ["light", "dark", "system"]
        assert seen[0][1] == "light" and seen[1][1] == "dark" and seen[2][1] in ("light", "dark")
        assert not b.errors, b.errors
    finally:
        _api(server, f"/api/songs/{img}/tags", "PUT", b'{"tags": []}', {"Content-Type": "application/json"})


def test_palette_setting(server, browser):
    """设置 · 配色：和明暗无关的另一个设置，写在 <html data-palette>、记在本机，刷新后还在；每套分浅色 / 深色两组颜色。"""
    b = browser
    accent = "getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
    b.open(server["base"] + "/")  # 先打开页面才能读写 localStorage
    try:
        b.js("localStorage.removeItem('tt-palette'); localStorage.setItem('tt-theme', 'light'); true")
        b.open(server["base"] + "/#settings")
        b.wait_for("!!document.querySelector('#paletteSeg [data-v=walnut]')")
        assert b.js("document.documentElement.dataset.palette") == "indigo"
        assert b.js(accent) == "#4f46e5"
        b.js("document.querySelector('#paletteSeg [data-v=walnut]').click(); true")
        assert b.js("localStorage.getItem('tt-palette')") == "walnut"
        assert b.js("document.documentElement.dataset.palette") == "walnut"
        assert b.js(accent) == "#8a5328"
        b.js("document.querySelector('#themeSeg [data-v=dark]').click(); true")
        assert b.js(accent) == "#d8a670"  # 深色下同一套配色换成亮一些的强调色
        b.open(server["base"] + "/#settings")  # 刷新：首屏前就写好（theme-boot.js），分段开关也对
        assert b.js("document.documentElement.dataset.palette") == "walnut"
        b.wait_for(
            "document.querySelector('#paletteSeg [data-v=walnut]').getAttribute('aria-pressed') === 'true'"
        )
        b.js("document.querySelector('#paletteSeg [data-v=indigo]').click(); true")
        assert b.js(accent) != "#d8a670" and b.js("document.documentElement.dataset.palette") == "indigo"
        assert not b.errors, b.errors
    finally:
        b.js("localStorage.removeItem('tt-palette'); localStorage.setItem('tt-theme', 'system'); true")


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
    # 取代 test_trainer.py 里的 test_library_lists_songs 的来源部分：来源是图标 + 说明行的悬停提示，不再是曲名旁的徽章
    b = browser
    b.open(server["base"] + "/")
    b.wait_for("document.querySelectorAll('.song-row').length >= 3")
    src = b.js(
        "Object.fromEntries([...document.querySelectorAll('.song-row')].map(r => [r.dataset.id, r.querySelector('.src-ic').title]))"
    )
    assert src[server["image"]] == "来源：图片" and src[server["gp"]] == "来源：GP"
    assert b.js("document.querySelectorAll('.song-row .tile').length") == 0
    sub = b.js(f"document.querySelector('.song-row[data-id=\"{server['gp']}\"] .sub').title")
    assert sub.startswith("来源：GP")  # 来源、添加时间、AI 花费在说明行的悬停提示里
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
    # 累计时长的说明（和练琴页同一个算法）挂在悬停提示里
    assert b.js(f"{row}.querySelector('.practice').title").startswith("累计 25 分钟：")
    assert (
        b.js(f"{row}.querySelector('[data-k=resume]').getAttribute('href')") == f"/trainer?id={img}&resume=1"
    )
    # 只有最近练过的一首用主色的“接着练”，其他行都是浅按钮
    assert b.js("document.querySelectorAll('#songs .btn.primary').length") == 1
    assert b.js(f"{row}.querySelector('[data-k=resume]').classList.contains('primary')") is True
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
    b._run(b._send("Page.addScriptToEvaluateOnNewDocument", source=SETTINGS_FAKE % 200))
    b.open(server["base"] + "/")
    b.wait_for("!!document.getElementById('title')")
    b.js("""window.__clicks = [];
      document.getElementById('uploadBtn').click = () => window.__clicks.push('uploadBtn');
      document.getElementById('apiKey').value = 'sk-ant-typed';
      for (const id of ['title', 'apiKey']) for (const isComposing of [true, false])
        document.getElementById(id).dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing, bubbles: true }));
      true""")
    assert b.js("window.__clicks") == ["uploadBtn"]
    b.wait_for("__calls.length === 1")
    assert b.js("__calls") == ['PUT {"api_key":"sk-ant-typed"}']  # 回车保存 key 只发了一次
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


def test_mixed_zip_and_other_files_warns(server, browser):
    b = browser
    b.open(server["base"] + "/")
    b.wait_for("!!document.getElementById('file')")
    b.js("""window.__imports = 0; const of = window.fetch;
      window.fetch = (u, init) => { if (String(u).includes('/api/import')) window.__imports++; return of(u, init); };
      const dt = new DataTransfer();
      dt.items.add(new File(['x'], 'a.zip', { type: 'application/zip' }));
      dt.items.add(new File(['x'], 'b.png', { type: 'image/png' }));
      const input = document.getElementById('file'); input.files = dt.files;
      input.dispatchEvent(new Event('change')); true""")
    b.wait_for("[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('请单独导入'))")
    assert b.js("window.__imports") == 0
    assert not b.errors, b.errors


def test_retry_failed_rows_from_library_and_practice_page(server, browser, trainer):
    """有识别失败的行：曲库行上和“…”菜单里有“重试失败的 N 行”，练习页的通知里也有；确认后调用 retry-failed。"""
    sid, store = server["image"], server["store"]
    raw = store.load(sid)
    store.update(sid, lambda s: s["systems"][1].update(status="error"))
    fake = """(() => { window.__posts = []; const of = window.fetch; window.fetch = (u, init) => {
      if (init && init.method === 'POST' && String(u).includes('/retry-failed')) { __posts.push(String(u));
        return Promise.resolve(new Response('{"ok": true}', {status: 200, headers: {'Content-Type': 'application/json'}})); }
      return of(u, init); }; return true; })()"""
    try:
        b = browser
        _fake_key(b, True)  # 重试要调用 AI：没有 key 时只说明（见 test_ai_actions_without_key）
        b.open(server["base"] + "/")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{sid}\"] [data-retry-failed]')")
        assert "重试失败的 1 行" in b.js(
            f"document.querySelector('.song-row[data-id=\"{sid}\"] .ctl').textContent"
        )
        b.js(fake)
        b.js(f"document.querySelector('.song-row[data-id=\"{sid}\"] [data-retry-failed]').click(); true")
        b.wait_for("!!document.querySelector('.dlg-msg[open] [data-ok]')")
        b.js("document.querySelector('.dlg-msg [data-ok]').click(); true")
        b.wait_for("__posts.length === 1")
        assert __import__("re").search(r"/api/songs/[^/]+/retry-failed$", b.js("__posts[0]"))
        # 练习页：通知里带“重试失败的 1 行”
        b = trainer(sid)
        b.wait_for(
            "[...document.querySelectorAll('#notices button')].some(x => x.textContent.includes('重试失败的 1 行'))"
        )
        b.js(fake)
        # 没有 API key 时这个按钮灰掉（点了只说明去设置）：当作有 key
        b.wait_for("tt.get('ui.hasKey') !== undefined")
        b.js("tt.set('ui.hasKey', true); true")
        b.js(
            "[...document.querySelectorAll('#notices button')].find(x => x.textContent.includes('重试失败')).click(); true"
        )
        b.wait_for("!!document.querySelector('.dlg-msg[open] [data-ok]')")
        b.js("document.querySelector('.dlg-msg [data-ok]').click(); true")
        b.wait_for("__posts.length === 1")
        assert not b.errors, b.errors
    finally:
        store.update(sid, lambda s: s.update(systems=raw["systems"]))


def _put_json(server, path, body):
    return _api(server, path, "PUT", json.dumps(body).encode(), {"Content-Type": "application/json"})


def test_library_refreshes_after_back_and_on_return(server, browser):
    # 没有曲子在忙时轮询是停的：从练习页“后退”回来（往返缓存恢复）、从别的标签页切回来，都要刷新
    gp = server["gp"]
    old = json.loads(_api(server, f"/api/songs/{gp}"))["title"]
    title = f"document.querySelector('.song-row[data-id=\"{gp}\"] .title').textContent"
    try:
        b = browser
        b.open(server["base"] + "/")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{gp}\"]')")
        b.js("window.__same = 1; true")
        b.js(f"location.href = '/trainer?id={gp}'; true")
        b.wait_for("location.pathname === '/trainer' && document.readyState === 'complete'")
        _put_json(server, f"/api/songs/{gp}/meta", {"title": "E2E 后退后的新名字"})  # 相当于在练习页里改了名
        b.js("history.back(); true")
        b.wait_for("location.pathname === '/' && document.readyState === 'complete'")
        assert b.js("window.__same") == 1  # 确实是从往返缓存恢复的（不是重新加载）
        b.wait_for(f"{title} === 'E2E 后退后的新名字'", timeout=5)
        # 切到别的标签页再回来（页面没换，只有 visibilitychange）
        _put_json(server, f"/api/songs/{gp}/meta", {"title": "E2E 切回来的新名字"})
        b.js("document.dispatchEvent(new Event('visibilitychange')); true")
        b.wait_for(f"{title} === 'E2E 切回来的新名字'", timeout=5)
        assert not b.errors, b.errors
    finally:
        _put_json(server, f"/api/songs/{gp}/meta", {"title": old})


def test_back_to_library_without_bfcache_shows_fresh_data(server):
    # Chrome 没能从往返缓存恢复曲库页（在练习页待久了、缓存被清掉）时，“后退”会重新加载页面，
    # 而且优先用 HTTP 缓存：/api/songs 不带 no-store 时拿到的是旧数据（刚确认的小节，“待核对”数量不变）
    from tests.e2e.cdp import Browser

    gp = server["gp"]
    old = json.loads(_api(server, f"/api/songs/{gp}"))["title"]
    title = f"document.querySelector('.song-row[data-id=\"{gp}\"] .title').textContent"
    try:
        with Browser(args=("--disable-features=BackForwardCache",)) as b:
            b.open(server["base"] + "/")
            b.wait_for(f"{title} === {json.dumps(old)}")
            b.js(f"location.href = '/trainer?id={gp}'; true")
            b.wait_for("location.pathname === '/trainer' && document.readyState === 'complete'")
            # 相当于在练习页里改了数据
            _put_json(server, f"/api/songs/{gp}/meta", {"title": "E2E 后退重新加载"})
            b.js("window.__same = 1; history.back(); true")
            b.wait_for("location.pathname === '/' && document.readyState === 'complete'")
            assert b.js("window.__same") is None  # 确实是重新加载（没有往返缓存）
            b.wait_for(f"{title} === 'E2E 后退重新加载'", timeout=5)
            assert not b.errors, b.errors
    finally:
        _put_json(server, f"/api/songs/{gp}/meta", {"title": old})


def test_tag_click_filters_in_select_mode(server, browser):
    img = server["image"]
    _put_json(server, f"/api/songs/{img}/tags", {"tags": ["e2e-sel"]})
    try:
        b = browser
        b.open(server["base"] + "/")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{img}\"] [data-tag]')")
        b.js("document.getElementById('selectBtn').click(); true")
        b.js(f'document.querySelector(\'.song-row[data-id="{img}"] [data-tag="e2e-sel"]\').click(); true')
        assert b.js("[...document.querySelectorAll('.song-row')].map(r => r.dataset.id)") == [img]
        assert b.js("document.getElementById('selectCount').textContent") == "已选 0 首"  # 点标签不勾选
        b.js("document.getElementById('selectCancel').click(); true")
        assert not b.errors, b.errors
    finally:
        _put_json(server, f"/api/songs/{img}/tags", {"tags": []})


def _drag_file(b, x, y, path):
    data = {"items": [], "files": [str(path)], "dragOperationsMask": 1}
    for t in ("dragEnter", "dragOver", "drop"):
        b._run(b._send("Input.dispatchDragEvent", type=t, x=x, y=y, data=data))


def test_file_dropped_outside_drop_zone_stays_in_app(server, browser, tmp_path):
    from tests.fakes import score_png

    b = browser
    b.open(server["base"] + "/")
    b.wait_for("!!document.querySelector('.song-row')")
    # 无头 Chrome 不会真的去打开拖进来的文件，所以看 dragover / drop 有没有被拦下（拦下了 Chrome 就不会离开曲库）
    blocked = """(el => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'a.png', { type: 'image/png' }));
      const ev = t => new DragEvent(t, { bubbles: true, cancelable: true, dataTransfer: dt });
      return [!el.dispatchEvent(ev('dragover')), dt.dropEffect, !el.dispatchEvent(ev('drop'))]; })(document.querySelector('{}'))"""
    assert b.js(blocked.replace("{}", "#songs")) == [True, "none", True]  # 落在曲子列表上：拦下，光标“不能放”
    b.js(f"tt.openPages('{server['image']}').then(() => true)")
    b.wait_for("document.querySelectorAll('.page-card').length === 1")
    # “管理原谱页面”里也拦下：没保存的页面顺序不会丢
    assert b.js(blocked.replace("{}", "#pageGrid")) == [True, "none", True]
    b.js("document.getElementById('pagesCancel').click(); true")
    b.wait_for("!document.getElementById('pagesDlg').open")
    # 上传框照样能放（真的拖一个文件进去）
    png = tmp_path / "dropped.png"
    png.write_bytes(score_png())
    x, y = b.js(
        "(r => [r.x + r.width / 2, r.y + r.height / 2])(document.getElementById('drop').getBoundingClientRect())"
    )
    _drag_file(b, x, y, png)
    b.wait_for("document.getElementById('picked').textContent.includes('dropped.png')", timeout=5)
    assert not b.errors, b.errors


def test_toast_shows_above_modal_dialog(server, browser):
    b = browser
    b.open(server["base"] + "/")
    b.wait_for("!!document.querySelector('.song-row')")
    b.js(f"tt.openPages('{server['image']}').then(() => true)")
    b.wait_for("document.getElementById('pagesDlg').open")
    hit = """(() => { const t = document.querySelector('.toast'), r = t.getBoundingClientRect();
      return t.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2)); })()"""
    parent = "document.querySelector('.toast').parentNode"
    b.js("UI.toast('保存失败：这首曲子正在识别', 'err', 60000); true")
    # 在对话框里（读屏软件看得到）、盖在遮罩上面（点得到 = 没被压在下面）
    assert b.js(f"{parent}.id") == "pagesDlg"
    assert b.js(hit) is True
    # 再叠一个确认框：提示挪到确认框里，还在最上面；确认框关掉后回到下面的对话框
    b.js("UI.confirm('叠一层'); true")
    b.wait_for("!!document.querySelector('.dlg-msg[open]')")
    assert b.js(f"{parent}.classList.contains('dlg-msg')") is True
    assert b.js(hit) is True
    b.js("document.querySelector('.dlg-msg [data-cancel]').click(); true")
    b.wait_for("!document.querySelector('.dlg-msg')")
    assert b.js(f"{parent}.id") == "pagesDlg"
    # 对话框关上（比如保存成功后先提示再关）：提示还在、看得见
    b.js("document.getElementById('pagesCancel').click(); true")
    b.wait_for("!document.getElementById('pagesDlg').open")
    assert b.js(f"{parent} === document.body") is True
    assert b.js(hit) is True
    assert not b.errors, b.errors


SETTINGS_FAKE = """(() => { window.__calls = []; window.__putStatus = %d; const of = window.fetch;
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  window.fetch = async (u, init = {}) => {
    const url = String(u), m = init.method || 'GET';
    if (url.endsWith('/api/settings') && m === 'PUT') {
      __calls.push('PUT ' + init.body);
      if (__putStatus !== 200) return json({ detail: '保存失败（测试）' }, __putStatus);
      return json({ api_key_masked: 'sk-ant…test', env_key: false, base_url: '', model: 'claude-opus-5-5', effort: 'high', double_check: 'off' });
    }
    if (url.endsWith('/api/settings/test')) { __calls.push('TEST'); return json({ ok: true }); }
    return of(u, init);
  }; return true; })()"""


def test_api_key_kept_on_failed_save_and_saved_before_test(server, browser):
    # 不调用真的模型：PUT /api/settings 和测试接口都在页面里拦下
    b = browser
    b._run(b._send("Page.addScriptToEvaluateOnNewDocument", source=SETTINGS_FAKE % 400))
    b._run(b._send("Emulation.setFocusEmulationEnabled", enabled=True))  # 无界面时也触发 focus / blur
    b.open(server["base"] + "/#settings")
    b.wait_for("!document.getElementById('view-settings').hidden")
    assert b.js("document.getElementById('saveKey')") is None  # 和其他设置一样自动保存，没有“保存”按钮
    idle = "!document.getElementById('{}').hasAttribute('aria-busy')"
    key = "document.getElementById('apiKey')"
    # 离开输入框时保存
    b.js(f"{key}.focus(); {key}.value = 'sk-ant-typed'; {key}.blur(); true")
    b.wait_for(f"__calls.length === 1 && {idle.format('apiKey')}")
    assert b.js(f"{key}.value") == "sk-ant-typed"  # 保存失败：不用再粘贴
    b.js(f"{key}.focus(); {key}.blur(); true")  # 同一个 key 失败过：失去焦点不再自动重试
    assert b.js("__calls.length") == 1
    # “测试”：先保存填了的 key；保存失败就不测
    b.js("document.getElementById('testBtn').click(); true")
    b.wait_for(f"__calls.length === 2 && {idle.format('testBtn')}")
    assert b.js("__calls") == ['PUT {"api_key":"sk-ant-typed"}'] * 2
    # 保存成功：先保存再测试，框清空，打码的 key 显示出来
    b.js("__putStatus = 200; __calls.length = 0; document.getElementById('testBtn').click(); true")
    b.wait_for("document.getElementById('testState').textContent.includes('连接正常')")
    assert b.js("__calls") == ['PUT {"api_key":"sk-ant-typed"}', "TEST"]
    assert b.js(f"{key}.value") == ""
    assert "sk-ant…test" in b.js(f"{key}.placeholder")
    assert "sk-ant…test" in b.js("document.getElementById('keyState').textContent")
    # 没有没保存的改动：直接测试
    b.js("__calls.length = 0; document.getElementById('testBtn').click(); true")
    b.wait_for(f"__calls.includes('TEST') && {idle.format('testBtn')}")
    assert b.js("__calls") == ["TEST"]
    # 回车也保存（有轻提示），焦点留在输入框里
    b.js(f"__calls.length = 0; {key}.focus(); {key}.value = 'sk-ant-new'; true")
    b.key("Enter", vk=13)
    b.wait_for(f"__calls.length === 1 && {key}.value === '' && {idle.format('apiKey')}")
    assert b.js("document.querySelector('.toast').textContent") == "API key 已保存"
    assert b.js(f"document.activeElement === {key}")
    b.js(f"{key}.blur(); true")  # 已经保存了：失去焦点不再发
    assert b.js("__calls.length") == 1
    assert not b.errors, b.errors


def test_second_bundle_import_while_importing_says_so(server, browser):
    b = browser
    b.open(server["base"] + "/")
    b.wait_for("!!document.querySelector('.song-row')")
    # 第一次导入一直不返回
    b.js("""window.__imports = 0; const of = window.fetch;
      window.fetch = (u, init) => { if (String(u).includes('/api/import')) { window.__imports++; return new Promise(() => {}); } return of(u, init); };
      window.__zip = () => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'a.zip', { type: 'application/zip' }));
        const input = document.getElementById('file'); input.files = dt.files; input.dispatchEvent(new Event('change')); };
      __zip(); true""")
    b.wait_for("window.__imports === 1")
    b.js("__zip(); true")
    b.wait_for(
        "[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('正在导入，稍后再试'))",
        timeout=5,
    )
    assert b.js("window.__imports") == 1
    assert not b.errors, b.errors


def test_library_works_when_site_storage_is_blocked(server, browser):
    b = browser
    b._run(
        b._send(
            "Page.addScriptToEvaluateOnNewDocument",
            source="Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('blocked', 'SecurityError'); } });",
        )
    )
    b.open(server["base"] + "/")
    b.wait_for("document.querySelectorAll('.song-row').length >= 3")
    b.js("document.querySelector('#sortSeg [data-v=title]').click(); true")
    assert b.js("document.querySelector('#sortSeg [data-v=title]').getAttribute('aria-pressed')") == "true"
    assert not b.errors, b.errors


def test_library_pages(server, browser):
    # 曲子多了分页（每页 20 首）：翻页、焦点、换搜索词回到第一页、刷新 / 后退还在原来那一页、删到最后一页没了时退回前一页
    from app import songs
    from tests.e2e.conftest import _gp_bytes

    gp = _gp_bytes()
    ids = [songs.create_song([("p.gp", gp)], title=f"E2E 翻页 {i:02d}") for i in range(1, 22)]
    rows = "[...document.querySelectorAll('#songs .song-row')]"
    names = f"{rows}.map(r => r.querySelector('.title').textContent)"
    try:
        b = browser
        b.open(server["base"] + "/")
        b.wait_for("!document.getElementById('pager').hidden")
        b.js("document.querySelector('#sortSeg [data-v=\"title\"]').click(); true")  # 按曲名排，顺序确定
        total = b.js("document.querySelector('#pager .range').textContent").split("/ ")[1].split(" ")[0]
        assert int(total) >= 23
        assert b.js(f"{rows}.length") == 20
        assert b.js("document.querySelector('#pager [data-page=\"prev\"]').disabled")
        first = b.js(names)
        # 下一页：最后一页“下一页”不可用，焦点落到当前页码上
        b.js(
            "document.querySelector('#pager [data-page=\"next\"]').focus(); document.querySelector('#pager [data-page=\"next\"]').click(); true"
        )
        assert b.js("document.querySelector('#pager [aria-current]').textContent") == "2"
        second = b.js(names)
        assert len(second) == int(total) - 20 and not set(first) & set(second)
        assert b.js("document.activeElement.getAttribute('aria-current')") == "page"
        # 刷新后还在第 2 页（记在 history.state 里）
        b.js("location.reload(); true")
        b.wait_for("document.querySelector('#pager [aria-current]')?.textContent === '2'")
        assert b.js(names) == second
        # 页码按钮回到第 1 页
        b.js("document.querySelector('#pager [data-page=\"1\"]').click(); true")
        assert b.js(names) == first
        # 在第 2 页换搜索词：回到第 1 页；结果不满一页时不显示翻页条
        b.js("document.querySelector('#pager [data-page=\"2\"]').click(); true")
        b.js(
            "{ const s = document.getElementById('search'); s.value = '翻页 1'; s.dispatchEvent(new Event('input')); } true"
        )
        assert b.js("document.getElementById('pager').hidden")
        assert b.js(f"{rows}.length") == 10  # 翻页 10–19
        b.js(
            "{ const s = document.getElementById('search'); s.value = ''; s.dispatchEvent(new Event('input')); } true"
        )
        assert b.js("document.querySelector('#pager [aria-current]').textContent") == "1"
        # 第 2 页上的曲子都删了：退回第 1 页，翻页条隐藏
        b.js("document.querySelector('#pager [data-page=\"2\"]').click(); true")
        for sid in ids[-(int(total) - 20) :]:
            _api(server, f"/api/songs/{sid}", "DELETE")
        b.js("document.dispatchEvent(new Event('visibilitychange')); true")
        b.wait_for("document.getElementById('pager').hidden", timeout=5)
        assert b.js(f"{rows}.length") == 20
        assert not b.errors, b.errors
    finally:
        for sid in ids:
            with contextlib.suppress(urllib.error.HTTPError):  # 上面已经删掉的：404
                _api(server, f"/api/songs/{sid}", "DELETE")
        browser.js("localStorage.removeItem('tt.libSort'); true")


def test_settings_rows_are_terse(server, browser):
    # 设置的每一行只有名字和控件，说明在悬停提示里；双重校对和精修放在“识别后复核”下面，“?”里写效果和费用
    b = browser
    b.open(server["base"] + "/#settings")
    b.wait_for("!document.getElementById('view-settings').hidden")
    subs = b.js("[...document.querySelectorAll('#view-settings .set .sub')].filter(x => !x.hidden).length")
    assert subs == 0
    who = "document.getElementById('{}').closest('.row').querySelector('.who').title"
    assert "环境变量" in b.js(who.format("apiKey")) and "离开输入框" in b.js(who.format("apiKey"))
    assert "进度环" in b.js(who.format("goalSeg"))
    titles = b.js("[...document.querySelectorAll('#view-settings .section-title')].map(x => x.textContent)")
    assert titles == ["模型", "识别后复核", "练琴", "外观"]
    group = "document.querySelectorAll('#view-settings .list.set')[1]"
    assert b.js(f"!!{group}.querySelector('#doubleSeg') && !!{group}.querySelector('#refineSeg')")
    assert "2 倍" in b.js("document.getElementById('doubleHelp').textContent")
    assert "$0.2" in b.js("document.getElementById('refineHelp').textContent")
    # 曲库：识别要多久写在上传框的悬停提示里，不再是列表下面的一行字
    b.js("location.hash = 'library'; true")
    assert b.js("document.querySelector('.foot-hint')") is None
    assert "1–3 分钟" in b.js("document.getElementById('drop').title")
    assert not b.errors, b.errors


def _post(server, path, body):
    return json.loads(
        _api(server, path, "POST", json.dumps(body).encode(), {"Content-Type": "application/json"})
    )


def test_archive_hides_songs_and_filter_shows_them(server, browser):
    # 归档：“…”菜单 / 选择模式里归档，默认不显示；“已归档 N”只看归档的（记在浏览记录里），淡色显示；不排进今日，计划里淡色留在原位
    img, gp = server["image"], server["gp"]
    rows = "[...document.querySelectorAll('#songs .song-row')].map(r => r.dataset.id)"
    b = browser
    try:
        _api(
            server,
            f"/api/songs/{gp}/study",
            "PUT",
            b'{"in_plan": true}',
            {"Content-Type": "application/json"},
        )
        b.open(server["base"] + "/")
        b.wait_for(f"!!document.querySelector('[data-more=\"{gp}\"]')")
        assert b.js("document.getElementById('archFilter').hidden") is True  # 没有归档的曲子：不显示
        b.js(f"document.querySelector('[data-more=\"{gp}\"]').click(); true")
        b.js(
            "[...document.querySelectorAll('.menu button')].find(x => x.textContent === '归档').click(); true"
        )
        b.wait_for(f"!{rows}.includes('{gp}')")
        assert "已归档" in b.js("document.querySelector('.toast').textContent")
        assert b.js("document.getElementById('archFilter').textContent") == "已归档 1"
        # 今日不排归档的；计划里还在，淡色，写明不排进今日
        today = json.loads(_api(server, "/api/study/today"))
        assert (
            gp not in [x["id"] for x in today["queue"] + today["done"]] and today["counts"]["archived"] == 1
        )
        b.js("location.hash = 'study'; true")
        b.wait_for(f"!!document.querySelector('#plan .plan-row.archived[data-id=\"{gp}\"]')")
        assert "不排进今日" in b.js(
            f"document.querySelector('#plan .plan-row[data-id=\"{gp}\"] .sub').textContent"
        )
        assert "计划里的曲子都归档了" in b.js("document.getElementById('queue').textContent")
        # 选择模式里批量归档
        b.js("location.hash = 'library'; true")
        b.js("document.getElementById('selectBtn').click(); true")
        b.js(f"document.querySelector('.song-row[data-id=\"{img}\"] .who').click(); true")
        assert b.js("document.getElementById('archiveBtn').textContent") == "归档"
        b.js("document.getElementById('archiveBtn').click(); true")
        b.wait_for(f"!{rows}.includes('{img}') && document.getElementById('selectBar').hidden")
        b.wait_for("document.getElementById('archFilter').textContent === '已归档 2'")
        # 只看归档的：淡色；刷新后还在这个视图里
        b.js("document.getElementById('archFilter').click(); true")
        assert sorted(b.js(rows)) == sorted([img, gp])
        assert b.js("document.getElementById('archFilter').getAttribute('aria-pressed')") == "true"
        assert b.js(
            "[...document.querySelectorAll('#songs .song-row')].every(r => r.classList.contains('archived'))"
        )
        b.js("location.reload(); true")
        b.wait_for(f"!!document.querySelector('.song-row[data-id=\"{img}\"]')")
        assert sorted(b.js(rows)) == sorted([img, gp])
        # 归档视图里的选择：批量“取消归档”
        b.js(
            "document.getElementById('selectBtn').click(); document.getElementById('selectAll').click(); true"
        )
        assert b.js("document.getElementById('archiveBtn').textContent") == "取消归档"
        b.js("document.getElementById('archiveBtn').click(); true")
        # 都取消了：“已归档”收起来，回到平时的列表
        b.wait_for("document.getElementById('archFilter').hidden")
        b.wait_for(f"{rows}.includes('{img}') && {rows}.includes('{gp}')")
        assert not json.loads(_api(server, f"/api/songs/{gp}"))["archived"]
        assert not b.errors, b.errors
    finally:
        for sid in (img, gp):
            _post(server, f"/api/songs/{sid}/archive", {"archived": False})
        _api(
            server,
            f"/api/songs/{gp}/study",
            "PUT",
            b'{"in_plan": false}',
            {"Content-Type": "application/json"},
        )


IMPORT_FAKE = """(() => { const of = window.fetch;
  window.fetch = async (u, init) => {
    if (!String(u).includes('/api/import')) return of(u, init);
    return new Response(JSON.stringify(%s), { status: %d, headers: { 'Content-Type': 'application/json' } });
  };
  const dt = new DataTransfer(); dt.items.add(new File(['x'], 'a.zip', { type: 'application/zip' }));
  const input = document.getElementById('bundleFile'); input.files = dt.files; input.dispatchEvent(new Event('change'));
  return true; })()"""


def test_bundle_import_report_per_bundle(server, browser):
    # 导入了几个包，有跳过 / 失败的：对话框里按包列出明细（不只是第一条错误）
    report = {
        "imported": [{"id": "x1", "title": "第一首", "bundle": "a.zip"}],
        "skipped": [
            {"title": "已有的", "reason": "曲库里已经有这首", "duplicate": True, "bundle": "a.zip"},
            {"title": "坏.zip", "reason": "不是有效的 zip 文件", "duplicate": False, "bundle": "坏.zip"},
        ],
        "bundles": [
            {"file": "a.zip", "ok": True, "imported": 1, "skipped": 1, "error": None},
            {"file": "坏.zip", "ok": False, "imported": 0, "skipped": 1, "error": "不是有效的 zip 文件"},
        ],
    }
    b = browser
    b.open(server["base"] + "/")
    b.wait_for("!!document.querySelector('.song-row')")
    b.js(IMPORT_FAKE % (json.dumps(report), 200))
    b.wait_for("document.getElementById('importDlg').open")
    assert (
        b.js("document.getElementById('importTitle').textContent")
        == "已导入 1 首 · 1 首已有 · 1 个包无法导入"
    )
    sections = b.js(
        "[...document.querySelectorAll('#importList .ib')].map(x => x.textContent.replace(/\\s+/g, ' ').trim())"
    )
    assert "a.zip" in sections[0] and "第一首" in sections[0] and "已有的" in sections[0]
    assert "坏.zip" in sections[1] and "不是有效的 zip 文件" in sections[1]
    b.key("Escape", vk=27)
    b.wait_for("!document.getElementById('importDlg').open")
    # 上传太大（413）、格式不对（400）：显示后端的说明
    b.open(server["base"] + "/")
    b.wait_for("!!document.querySelector('.song-row')")
    b.js(IMPORT_FAKE % (json.dumps({"detail": "文件总大小超过 2048 MB"}), 413))
    b.wait_for(
        "[...document.querySelectorAll('.toast.err')].some(t => t.textContent === '文件总大小超过 2048 MB')"
    )
    assert not b.js("document.getElementById('importDlg').open")
    assert not b.errors, b.errors


def test_upload_too_large_shows_the_reason(server, browser):
    # 真的超过上限：服务端在读请求体之前就回 413，页面显示它的说明
    from app.config import settings

    old = settings.max_total_bytes
    settings.max_total_bytes = 1  # 上限 = 1 字节 + 1 MB 表单开销（0 会被当成“没有上限”）
    try:
        b = browser
        _fake_key(b, True)  # 上传原谱要有 key，没有时根本不发请求
        b.open(server["base"] + "/")
        b.wait_for("!!document.getElementById('file')")
        b.js("""const dt = new DataTransfer(); dt.items.add(new File([new Uint8Array(3 * 1024 * 1024)], 'big.png', { type: 'image/png' }));
          const input = document.getElementById('file'); input.files = dt.files; input.dispatchEvent(new Event('change'));
          document.getElementById('uploadBtn').click(); true""")
        b.wait_for(
            "[...document.querySelectorAll('.toast.err')].some(t => t.textContent.includes('文件总大小超过'))",
            timeout=10,
        )
        assert not b.errors, b.errors
    finally:
        settings.max_total_bytes = old


def test_ai_actions_without_key(server, browser):
    # 没有 API key：上传原谱、整首重新识别、精修整首先说明去设置填 key（不发请求、不弹带费用的确认框）；GP 导入照常
    img = server["image"]
    b = browser
    _fake_key(b, False)
    b.open(server["base"] + "/")
    b.wait_for(
        f"!!document.querySelector('[data-more=\"{img}\"]') && !document.getElementById('keyNotice').hidden"
    )
    b.js("""window.__posts = []; const of = window.fetch; window.fetch = (u, init = {}) => {
      if ((init.method || 'GET') !== 'GET') { __posts.push(String(u));
        if (String(u).endsWith('/api/songs')) return Promise.resolve(new Response('{"id": "x"}', { status: 200, headers: { 'Content-Type': 'application/json' } })); }
      return of(u, init); }; true""")
    pick = """(name, type) => { const dt = new DataTransfer(); dt.items.add(new File(['x'], name, { type }));
      const input = document.getElementById('file'); input.files = dt.files; input.dispatchEvent(new Event('change')); }"""
    # 上传图片：直接说明，不上传
    b.js(f"({pick})('p.png', 'image/png'); document.getElementById('uploadBtn').click(); true")
    b.wait_for("!!document.querySelector('.dlg-msg[open]')")
    assert b.js("document.querySelector('.dlg-msg .dlg-t').textContent") == "还没有 API key"
    assert b.js("__posts") == []
    # “去设置”：切到设置页，焦点在 key 的输入框
    b.js("document.querySelector('.dlg-msg [data-ok]').click(); true")
    b.wait_for("!document.getElementById('view-settings').hidden")
    assert b.js("location.hash") == "#settings"
    assert b.js("document.activeElement.id") == "apiKey"
    # “…”菜单：整首重新识别、精修整首灰掉，点了是说明（不是带费用的确认框）
    b.js("location.hash = 'library'; true")
    b.wait_for("!document.getElementById('view-library').hidden")
    more = f"document.querySelector('[data-more=\"{img}\"]')"
    item = "[...document.querySelectorAll('.menu button')].find(x => x.textContent === '{}')"
    for label in ("整首重新识别", "精修整首"):
        b.js(f"{more}.click(); true")
        b.wait_for(f"!!{item.format(label)}")
        assert b.js(f"{item.format(label)}.classList.contains('off')") is True
        assert b.js(f"{item.format(label)}.title") == "先在设置里填 API key"
        assert b.js(f"{item.format('重命名')}.classList.contains('off')") is False
        b.js(f"{item.format(label)}.click(); true")
        b.wait_for("!!document.querySelector('.dlg-msg[open]')")
        assert b.js("document.querySelector('.dlg-msg .dlg-t').textContent") == "还没有 API key"
        b.js("document.querySelector('.dlg-msg [data-cancel]').click(); true")
        b.wait_for("!document.querySelector('.dlg-msg')")
    assert b.js("__posts") == []
    # GP 文件不需要 key：照常导入
    b.js(f"({pick})('a.gp', ''); document.getElementById('uploadBtn').click(); true")
    b.wait_for("__posts.length === 1")
    assert b.js("__posts[0]").endswith("/api/songs")
    assert not b.errors, b.errors


def test_ai_actions_with_key(server, browser):
    # 有 key：菜单项不灰，整首重新识别照常弹确认框（写明费用）
    img = server["image"]
    b = browser
    _fake_key(b, True)
    b.open(server["base"] + "/")
    more = f"document.querySelector('[data-more=\"{img}\"]')"
    b.wait_for(f"!!{more}")
    b.wait_for("document.getElementById('keyNotice').hidden")
    b.js(f"{more}.click(); true")
    item = "[...document.querySelectorAll('.menu button')].find(x => x.textContent === '整首重新识别')"
    b.wait_for(f"!!{item}")
    assert b.js(f"{item}.classList.contains('off')") is False
    b.js(f"{item}.click(); true")
    b.wait_for("!!document.querySelector('.dlg-msg[open]')")
    assert "整首重新识别" in b.js("document.querySelector('.dlg-msg .dlg-t').textContent")
    b.js("document.querySelector('.dlg-msg [data-cancel]').click(); true")
    assert not b.errors, b.errors


def test_filters_survive_tab_switch_and_reload(server, browser):
    # 切过顶栏标签（新的浏览记录）之后，标签筛选还要记在浏览记录里：刷新还在原来的筛选
    img = server["image"]
    _put_json(server, f"/api/songs/{img}/tags", {"tags": ["e2e-route"]})
    try:
        b = browser
        b.open(server["base"] + "/#library")
        b.wait_for("!!document.querySelector('#tagFilter [data-tag=\"e2e-route\"]')")
        b.js("document.querySelector('#tagFilter [data-tag=\"e2e-route\"]').click(); true")
        b.js("document.querySelector('#nav [data-v=\"study\"]').click(); true")
        b.wait_for("!document.getElementById('view-study').hidden")
        b.js("document.querySelector('#nav [data-v=\"library\"]').click(); true")
        b.wait_for("!document.getElementById('view-library').hidden")
        assert b.js("history.length") >= 3
        b.js("location.reload(); true")
        b.wait_for("!!document.querySelector('#tagFilter [aria-pressed=\"true\"]')")
        assert b.js("document.querySelector('#tagFilter [aria-pressed=\"true\"]').dataset.tag") == "e2e-route"
        assert b.js("[...document.querySelectorAll('.song-row')].map(r => r.dataset.id)") == [img]
        assert not b.errors, b.errors
    finally:
        _put_json(server, f"/api/songs/{img}/tags", {"tags": []})


def test_menu_tab_select_all_label_and_empty_library(server, browser):
    b = browser
    b.open(server["base"] + "/")
    more = "document.querySelector('.song-row [data-more]')"  # 第一行：后面还有别的控件
    b.wait_for("document.querySelectorAll('.song-row').length > 1")
    # 菜单打开时按 Tab：菜单关掉，焦点从“更多”按钮往后走（不留在菜单里、不掉到 body）
    b.js(f"{more}.focus(); {more}.click(); true")
    assert b.js("document.activeElement.closest('.menu') !== null")
    b.key("Tab", vk=9)
    assert b.js("!document.querySelector('.menu')")
    assert b.js("UI.hasLayer()") is False
    assert b.js(f"{more}.getAttribute('aria-expanded')") == "false"
    assert b.js("document.activeElement !== document.body")
    assert b.js(
        f"!!({more}.compareDocumentPosition(document.activeElement) & Node.DOCUMENT_POSITION_FOLLOWING)"
    )
    # Shift+Tab 同样关掉，焦点往前走
    b.js(f"{more}.focus(); {more}.click(); true")
    b.key("Tab", vk=9, modifiers=8)
    assert b.js("!document.querySelector('.menu')")
    assert b.js(
        f"!!({more}.compareDocumentPosition(document.activeElement) & Node.DOCUMENT_POSITION_PRECEDING)"
    )
    # “全选”复选框读出来是“全选当前列表”，不是“已选 N 首”
    b.js("document.getElementById('selectBtn').click(); true")
    assert b.js("document.getElementById('selectAll').getAttribute('aria-label')") == "全选当前列表"
    assert b.js("document.getElementById('selectAll').getAttribute('aria-describedby')") == "selectCount"
    b.js("document.getElementById('selectCancel').click(); true")
    assert b.js("document.getElementById('selectBtn').hidden") is False
    # 空曲库：没有“选择”
    _patch_songs_response(b, "songs => songs.splice(0)")
    b.js("tt.reload().then(() => true)")
    b.wait_for("!!document.querySelector('#songs .empty-state')")
    assert b.js("document.getElementById('selectBtn').hidden") is True
    assert not b.errors, b.errors


def test_narrow_screen_layout(server):
    # 390×844：不出横向滚动条；标签多时曲名不被挤没，来源图标和曲名在同一行；“?”说明、确认框、补记框都在屏幕里、两边留空
    from tests.e2e.cdp import Browser

    img = server["image"]
    _put_json(
        server, f"/api/songs/{img}/tags", {"tags": ["指弹", "练习中", "民谣", "古典", "很长很长的标签名字"]}
    )
    no_hscroll = "document.documentElement.scrollWidth <= innerWidth"
    inside = "(r => r.left >= {m} - 0.5 && r.right <= innerWidth - {m} + 0.5 && r.width > 0)({el}.getBoundingClientRect())"
    try:
        with Browser(width=600, height=844) as b:  # 无界面 Chrome 的窗口最窄 500：用设备尺寸模拟
            b._run(
                b._send(
                    "Emulation.setDeviceMetricsOverride",
                    width=390,
                    height=844,
                    deviceScaleFactor=1,
                    mobile=False,
                )
            )
            b.open(server["base"] + "/#library")
            row = f"document.querySelector('.song-row[data-id=\"{img}\"]')"
            b.wait_for(f"!!{row}?.querySelector('.tag')")
            assert b.js("innerWidth") == 390
            assert b.js(no_hscroll)
            assert b.js(f"{row}.querySelector('.title').getBoundingClientRect().width") > 40
            assert b.js(
                f"{row}.querySelector('.who').getBoundingClientRect().top < {row}.querySelector('.src-ic').getBoundingClientRect().bottom"
            )
            # 设置：Base URL 输入框不撑出横向滚动；“?”的说明在屏幕里
            b.js("location.hash = 'settings'; true")
            b.wait_for("!document.getElementById('view-settings').hidden")
            assert b.js(no_hscroll)
            b.js("document.querySelector('[aria-describedby=\"doubleHelp\"]').focus(); true")
            assert b.js(inside.format(m=0, el="document.getElementById('doubleHelp')"))
            assert b.js(
                "(p => p.top >= document.querySelector('[aria-describedby=\"doubleHelp\"]').getBoundingClientRect().bottom)(document.getElementById('doubleHelp').getBoundingClientRect())"
            )
            # 确认框：两边各留 16px
            b.js("UI.confirm('测试'); true")
            b.wait_for("!!document.querySelector('.dlg-msg[open]')")
            assert b.js(inside.format(m=16, el="document.querySelector('.dlg-msg')"))
            b.key("Escape", vk=27)
            b.wait_for("!document.querySelector('.dlg-msg')")
            # 练琴页：补记框同样
            b.js("location.hash = 'study'; true")
            b.wait_for("!!document.querySelector('#hero .ring')")
            assert b.js(no_hscroll)
            b.js("document.getElementById('logAdd').click(); true")
            b.wait_for("document.getElementById('logDlg').open")
            assert b.js(inside.format(m=16, el="document.getElementById('logDlg')"))
            b.js("document.getElementById('logCancel').click(); true")
            assert not b.errors, b.errors
    finally:
        _put_json(server, f"/api/songs/{img}/tags", {"tags": []})


def test_pages_save_without_key_says_so_before_asking_to_delete(server, browser):
    # 没有 key、新加了页又删了有小节的旧页：先说明要填 key，不先弹“删除 N 页原谱？”（确认了也保存不了）
    img = server["image"]
    b = browser
    _fake_key(b, False)
    b.open(server["base"] + "/")
    b.wait_for(
        f"!!document.querySelector('[data-more=\"{img}\"]') && !document.getElementById('keyNotice').hidden"
    )
    b.js(f"tt.openPages('{img}').then(() => true)")
    b.wait_for("document.querySelectorAll('.page-card').length === 1")
    b.js("""(() => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'more.png', { type: 'image/png' }));
      const input = document.getElementById('pagesFile'); input.files = dt.files; input.dispatchEvent(new Event('change')); })(); true""")
    b.wait_for("document.querySelectorAll('.page-card').length === 2")
    b.js("document.querySelector('.page-card[data-i=\"0\"] [data-rm]').click(); true")
    b.wait_for(
        "document.querySelectorAll('.page-card').length === 1 && !document.getElementById('pagesSave').disabled"
    )
    b.js(
        "window.__posts = []; const of = window.fetch; window.fetch = (u, init = {}) => { if ((init.method || 'GET') !== 'GET') __posts.push(String(u)); return of(u, init); }; true"
    )
    b.js("document.getElementById('pagesSave').click(); true")
    b.wait_for(
        "[...document.querySelectorAll('.toast')].some(t => t.textContent.includes('API key'))", timeout=5
    )
    assert b.js("!!document.querySelector('.dlg-msg')") is False  # 没弹删除确认
    assert b.js("__posts") == []
    assert b.js("document.getElementById('pagesDlg').open") is True  # 对话框还开着，调整没丢
    assert not b.errors, b.errors
