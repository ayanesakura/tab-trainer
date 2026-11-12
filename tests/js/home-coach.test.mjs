// 首页 AI 对话的纯函数：js/home-coach-view.js。运行：node --test tests/js/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { answerHTML, settled, costTip, EXAMPLES } from '../../app/static/js/home-coach-view.js';

const songs = [
  { id: '20261101-120000-abc123', title: '晴天', status: 'ready', bars: 8, is_score: true },
  { id: '20261101-120000-def456', title: '识别中', status: 'parsing', bars: 0, is_score: false },
];

test('answerHTML：曲库里能练的曲子换成练习页链接，曲名用曲库里的', () => {
  const html = answerHTML('先练 [[20261101-120000-abc123|晴天吧]]。', songs);
  assert.equal(html, '先练 <a class="song-link" href="/trainer?id=20261101-120000-abc123" title="打开练习页">晴天</a>。');
});

test('answerHTML：不认识的 id、还不能练的曲子只显示文字，不出坏链接', () => {
  assert.equal(answerHTML('[[nope-0000|编的]]', songs), '<span class="song-ref">编的</span>');
  assert.equal(answerHTML('[[20261101-120000-def456|x]]', songs), '<span class="song-ref">识别中</span>');
  assert.equal(answerHTML('[[nope-0000|]]', []), '<span class="song-ref">这首曲子</span>');
  assert.equal(answerHTML('[[Bad Id|x]] 和 [[a|b]]', songs), '[[Bad Id|x]] 和 [[a|b]]'); // 不是标记的原样（转义后）显示
});

test('answerHTML：全部转义，标记里的曲名也转义', () => {
  assert.equal(answerHTML('<b>x</b> & "y"'), '&lt;b&gt;x&lt;/b&gt; &amp; &quot;y&quot;');
  assert.equal(answerHTML('[[nope-0000|<img src=x onerror=alert(1)>]]'), '<span class="song-ref">&lt;img src=x onerror=alert(1)&gt;</span>');
  const evil = [{ ...songs[0], title: '<script>' }];
  assert.ok(answerHTML('[[20261101-120000-abc123|x]]', evil).includes('>&lt;script&gt;</a>'));
});

test('settled：流式显示时还没收完的半截标记先不显示', () => {
  assert.equal(settled('先练 [[20261101-120'), '先练 ');
  assert.equal(settled('先练 [[20261101-120000-abc123|晴天]] 再练'), '先练 [[20261101-120000-abc123|晴天]] 再练');
  assert.equal(settled('[[a|b]] 然后 [[c'), '[[a|b]] 然后 ');
  assert.equal(settled('[[ 换行\n之后'), '[[ 换行\n之后'); // 不是标记：照常显示
  assert.equal(settled(''), '');
});

test('costTip / EXAMPLES', () => {
  assert.match(costTip(null), /只给建议/);
  assert.equal(costTip({ tokens: 1200, cost_usd: 0.034 }), 'AI 对话累计花费 $0.03（新对话不清零）');
  assert.equal(costTip({ tokens: 10, cost_usd: 0.001 }), 'AI 对话累计花费 不到 $0.01（新对话不清零）');
  assert.match(costTip({ tokens: 10, cost_usd: 0, unpriced_tokens: 10 }), /10 token.*未计价/);
  assert.equal(EXAMPLES.length, 4);
});
