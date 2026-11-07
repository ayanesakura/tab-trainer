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
  globalThis.fetch = reply(400, '{"detail":[{"msg":"标题太长"}]}');
  await assert.rejects(api.post('/a', {}), { message: '标题太长' });
  globalThis.fetch = reply(502, '<html>Bad gateway</html>', 'text/html');
  await assert.rejects(api.get('/a'), { message: '请求失败（502）' });
  globalThis.fetch = reply(500, 'Internal Server Error', 'text/plain');
  await assert.rejects(api.get('/a'), { message: 'Internal Server Error' });
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(api.get('/a'), e => e.status === 0 && /连不上/.test(e.message));
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
  for (const st of [400, 404, 409, 500]) assert.equal(api.transient(new api.ApiError('x', st, { detail: 'x' })), false, st);
});
