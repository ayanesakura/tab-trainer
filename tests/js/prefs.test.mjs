import test from 'node:test';
import assert from 'node:assert/strict';
import { parsePrefs, serializePrefs, PREF_DEFAULTS, clampPref } from '../../app/static/js/trainer/prefs.js';

test('parsePrefs: missing / broken / old values fall back to defaults per field', () => {
  assert.deepEqual(parsePrefs(null), PREF_DEFAULTS);
  assert.deepEqual(parsePrefs('not json'), PREF_DEFAULTS);
  assert.deepEqual(parsePrefs('[1,2]'), PREF_DEFAULTS);
  assert.deepEqual(parsePrefs('"x"'), PREF_DEFAULTS);
  const p = parsePrefs(JSON.stringify({ metro: false, gtVol: 33, metroVol: 2.5, follow: 0, extra: 1, auto: true, autoStep: 4 }));
  assert.equal(p.metro, false);
  assert.equal(p.gtVol, 33);
  assert.equal(p.metroVol, PREF_DEFAULTS.metroVol); // 不是整数
  assert.equal('auto' in p || 'autoStep' in p, false); // 旧版本存的自动提速设置读的时候丢掉
  assert.deepEqual(parsePrefs(JSON.stringify({ gtVol: 999 })).gtVol, PREF_DEFAULTS.gtVol); // 超出范围
  assert.equal(parsePrefs(JSON.stringify({ metroVol: '50' })).metroVol, PREF_DEFAULTS.metroVol); // 字符串
  assert.equal(p.follow, true); // 不是布尔
  assert.equal('extra' in p, false);
});

test('serializePrefs keeps only pref fields and replaces invalid ones', () => {
  const play = { ...PREF_DEFAULTS, bpm: 90, a: 3, gtVol: 12, metroVol: -1, follow: false };
  assert.deepEqual(JSON.parse(serializePrefs(play)), { ...PREF_DEFAULTS, gtVol: 12, follow: false });
});

test('typed numbers are rounded and clamped', () => {
  assert.equal(clampPref('gtVol', -4), 0);
  assert.equal(clampPref('gtVol', 150), 100);
  assert.equal(clampPref('metroVol', 55.6), 56);
  assert.equal(clampPref('metroVol', 90, 80), 80); // 不超过输入框当时的上限
  assert.equal(clampPref('gtVol', NaN), null);
  assert.equal(clampPref('follow', 1), null);
  assert.equal(clampPref('autoStep', 4), null); // 自动提速已经去掉
});
