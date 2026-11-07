import test from 'node:test';
import assert from 'node:assert/strict';
import { parsePrefs, serializePrefs, PREF_DEFAULTS } from '../../app/static/js/trainer/prefs.js';

test('parsePrefs: missing / broken / old values fall back to defaults per field', () => {
  assert.deepEqual(parsePrefs(null), PREF_DEFAULTS);
  assert.deepEqual(parsePrefs('not json'), PREF_DEFAULTS);
  assert.deepEqual(parsePrefs('[1,2]'), PREF_DEFAULTS);
  assert.deepEqual(parsePrefs('"x"'), PREF_DEFAULTS);
  const p = parsePrefs(JSON.stringify({ metro: false, gtVol: 33, autoStep: 999, autoEvery: 2.5, metroVol: '50', follow: 0, extra: 1 }));
  assert.equal(p.metro, false);
  assert.equal(p.gtVol, 33);
  assert.equal(p.autoStep, PREF_DEFAULTS.autoStep); // 超出范围
  assert.equal(p.autoEvery, PREF_DEFAULTS.autoEvery); // 不是整数
  assert.equal(p.metroVol, PREF_DEFAULTS.metroVol); // 字符串
  assert.equal(p.follow, true); // 不是布尔
  assert.equal('extra' in p, false);
});

test('serializePrefs keeps only pref fields and replaces invalid ones', () => {
  const play = { ...PREF_DEFAULTS, bpm: 90, a: 3, gtVol: 12, autoEvery: 0, follow: false };
  assert.deepEqual(JSON.parse(serializePrefs(play)), { ...PREF_DEFAULTS, gtVol: 12, follow: false });
});
