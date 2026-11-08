// 前端共用模块：js/vocab.js、js/lib/api.js（在 Node 里替换 fetch / document 测）。
// 运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import * as V from '../../app/static/js/vocab.js';
import * as api from '../../app/static/js/lib/api.js';

const raw = JSON.parse(readFileSync(new URL('../../app/static/js/vocab.json', import.meta.url)));

test('vocab：常量和 vocab.json 一致', () => {
  assert.equal(V.STRINGS, raw.instrument.strings);
  assert.deepEqual([...V.STANDARD], raw.instrument.standard_tuning);
  assert.equal(V.GRID, raw.grid_ticks);
  assert.equal(V.TECH_SYM.h, 'H');
  assert.equal(V.TECH_NAME.pm, '闷音');
  assert.equal(V.TECHS.length, raw.techs.length);
  assert.equal(V.DIRECTIONS[0], 'fine');
  assert.equal(V.SOURCES.gp, 'GP');
  assert.deepEqual([...V.SCORE_EXT], [...raw.score_files.gp, ...raw.score_files.musicxml]);
  assert.ok(V.isScoreFile('Song.GP5') && V.isScoreFile('a.mxl') && !V.isScoreFile('page.png') && !V.isScoreFile('x.zip'));
  assert.equal(V.WARN_CODES.length, '时值合计不对'); // code 'length'，不是数组长度
  assert.ok(Object.isFrozen(V.TECH_SYM));
});

test('vocab：parseDur', () => {
  assert.equal(V.parseDur('1'), 1);
  assert.equal(V.parseDur('3/2'), 1.5);
  assert.equal(V.parseDur(' 1/4 '), 0.25);
  assert.equal(V.parseDur(0.5), 0.5);
  assert.ok(Number.isNaN(V.parseDur('abc')));
});

const reply = (status, body, type = 'application/json') => async () => new Response(body, { status, headers: { 'content-type': type } });

test('api：成功返回 JSON，JSON 请求体带 Content-Type', async () => {
  let seen;
  globalThis.fetch = async (path, init) => { seen = { path, init }; return new Response('{"ok":1}', { status: 200 }); };
  assert.deepEqual(await api.put('/api/x', { a: 1 }), { ok: 1 });
  assert.equal(seen.init.method, 'PUT');
  assert.equal(seen.init.headers['Content-Type'], 'application/json');
  assert.equal(seen.init.body, '{"a":1}');
  globalThis.fetch = reply(200, '');
  assert.equal(await api.del('/api/x'), null);
});

test('api：错误统一成 ApiError(detail)，非 JSON 的错误体也不崩', async () => {
  globalThis.fetch = reply(409, '{"detail":"曲子在别处改过了"}');
  await assert.rejects(api.get('/a'), e => e instanceof api.ApiError && e.message === '曲子在别处改过了' && e.status === 409);
  // 有任务在跑（423 Locked）和 409 一样：把服务器的说明原样给出来
  globalThis.fetch = reply(423, '{"detail":"这首曲子正在识别，完成后再调整页面"}');
  await assert.rejects(api.upload('/a', []), e => e.message === '这首曲子正在识别，完成后再调整页面' && e.status === 423);
  globalThis.fetch = reply(400, '{"detail":[{"msg":"标题太长"}]}');
  await assert.rejects(api.post('/a', {}), { message: '标题太长' });
  globalThis.fetch = reply(502, '<html>Bad gateway</html>', 'text/html');
  await assert.rejects(api.get('/a'), { message: '请求失败（502）' });
  globalThis.fetch = reply(500, 'Internal Server Error', 'text/plain');
  await assert.rejects(api.get('/a'), { message: 'Internal Server Error' });
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(api.get('/a'), e => e.status === 0 && /连不上/.test(e.message));
});

// 把字节按给定的切点分成几块，一块块吐出来（切在一行中间、一个汉字的 UTF-8 字节中间都要能拼回来）
const chunked = (text, cuts, { cutOff = false } = {}) => {
  const bytes = new TextEncoder().encode(text), parts = [];
  let a = 0;
  for (const b of [...cuts, bytes.length]) { parts.push(bytes.slice(a, b)); a = b; }
  return new ReadableStream({
    pull(c) {
      if (parts.length) c.enqueue(parts.shift());
      else if (cutOff) c.error(new TypeError('network error'));
      else c.close();
    },
  });
};

test('api：postStream 逐行交出事件，done 作为结果，error 抛 ApiError，断流也抛', async () => {
  const ndjson = '{"type":"delta","text":"用食指"}\n{"type":"reset"}\n{"type":"delta","text":"按 2 弦"}\n{"type":"done","answer":"按 2 弦","qa":[]}\n';
  let seen;
  globalThis.fetch = async (p, init) => { seen = init; return new Response(chunked(ndjson, [5, 20, 33, 40]), { status: 200 }); };
  const events = [];
  const done = await api.postStream('/api/x', { question: 'q' }, ev => events.push(ev));
  assert.equal(seen.method, 'POST');
  assert.equal(seen.body, '{"question":"q"}');
  assert.deepEqual(events, [{ type: 'delta', text: '用食指' }, { type: 'reset' }, { type: 'delta', text: '按 2 弦' }]);
  assert.deepEqual(done, { type: 'done', answer: '按 2 弦', qa: [] });

  globalThis.fetch = async () => new Response(chunked('{"type":"delta","text":"a"}\n{"type":"error","detail":"模型服务返回错误"}\n', [3]));
  await assert.rejects(api.postStream('/a', {}, () => {}), e => e instanceof api.ApiError && e.message === '模型服务返回错误');
  globalThis.fetch = async () => new Response(chunked('{"type":"delta","text":"a"}\n', [], { cutOff: true }));
  await assert.rejects(api.postStream('/a', {}, () => {}), e => e.status === 0 && /连接断了/.test(e.message));
  globalThis.fetch = async () => new Response('{"type":"delta","text":"a"}\n');  // 正常结束但没有最后一行
  await assert.rejects(api.postStream('/a', {}, () => {}), e => e.status === 0);
  globalThis.fetch = reply(404, '{"detail":"小节不存在"}');
  await assert.rejects(api.postStream('/a', {}, () => {}), e => e.status === 404 && e.message === '小节不存在');
});

test('api：upload 组装 FormData', async () => {
  let body;
  globalThis.fetch = async (p, init) => { body = init.body; return new Response('{}'); };
  await api.upload('/api/songs', [new File(['x'], 'a.png')], { title: '曲名', skip: null });
  assert.ok(body instanceof FormData);
  assert.equal(body.getAll('files').length, 1);
  assert.equal(body.get('title'), '曲名');
  assert.equal(body.has('skip'), false);
});

test('api：poll 出错退避重试，while 为假时停下', async () => {
  globalThis.document = { hidden: false, addEventListener() {}, removeEventListener() {} };
  const origWarn = console.warn; console.warn = () => {};
  let n = 0;
  const p = api.poll(async () => { n++; if (n === 1) throw new Error('网络'); return n; }, { interval: 5, while: x => x < 3 });
  await new Promise(r => setTimeout(r, 80));
  assert.equal(n, 3); // 第 1 次出错，退避后第 2 次、第 3 次（返回 3，停下）
  p.now();
  await new Promise(r => setTimeout(r, 20));
  assert.equal(n, 4); // now() 重新开始
  p.stop();
  console.warn = origWarn;
});

test('api：transient 只把连不上和 502 / 503 / 504 当成可以自动重试', () => {
  assert.equal(api.transient(new api.ApiError('连不上', 0)), true);
  for (const st of [502, 503, 504]) assert.equal(api.transient(new api.ApiError('x', st)), true, st);
  for (const st of [400, 404, 409, 423, 500]) assert.equal(api.transient(new api.ApiError('x', st, { detail: 'x' })), false, st);
});

test('api：poll stop() 之后 now() 重新开始，页面从后台回来照样补一次（监听只挂一个）', async () => {
  const listeners = new Set();
  globalThis.document = { hidden: false, addEventListener: (t, f) => listeners.add(f), removeEventListener: (t, f) => listeners.delete(f) };
  let n = 0;
  const p = api.poll(async () => { n++; return true; }, { interval: 5 });
  try {
    assert.equal(listeners.size, 1);
    p.stop();
    assert.equal(listeners.size, 0);
    p.now(); p.now(); // “重试”：重新开始
    assert.equal(listeners.size, 1);
    document.hidden = true;
    await new Promise(r => setTimeout(r, 30)); // 在后台：跳过
    const before = n;
    document.hidden = false;
    listeners.forEach(f => f());
    await new Promise(r => setTimeout(r, 2));
    assert.ok(n > before, '回到前台没有恢复轮询');
  } finally { p.stop(); }
});
