// 用 alphaTab 自带的合成器把曲子离线渲染成 WAV，用来比较音色库的响度和音色（不需要浏览器）。
//   node scripts/render_audio.mjs SONG.json SOUNDFONT OUT.wav [--program 25] [--bars 8] [--metronome]
// SONG.json 是 data/songs/<id>/song.json 那种格式（识别出来的曲子）。
import { readFileSync, writeFileSync } from 'node:fs';
import { parseArgs } from 'node:util';
import * as at from '../app/static/vendor/alphatab/alphaTab.core.min.mjs';
import { toAlphaTex } from '../app/static/js/score.js';

const { values: opt, positionals } = parseArgs({
  allowPositionals: true,
  options: { program: { type: 'string', default: '25' }, bars: { type: 'string', default: '8' }, metronome: { type: 'boolean', default: false } },
});
const [songPath, sfPath, out] = positionals;
if (!out) { console.error('用法：node scripts/render_audio.mjs SONG.json SOUNDFONT OUT.wav [--program 25] [--bars 8] [--metronome]'); process.exit(1); }

const song = JSON.parse(readFileSync(songPath, 'utf8'));
song.bars = song.bars.slice(0, +opt.bars);
const { tex } = toAlphaTex(song, { tempo: song.meta?.tempo || 70, program: +opt.program });
const settings = new at.Settings();
const imp = new at.importer.AlphaTexImporter();
imp.initFromString(tex, settings);
const score = imp.readScore();
const midi = new at.midi.MidiFile();
new at.midi.MidiFileGenerator(score, settings, new at.midi.AlphaSynthMidiFileHandler(midi)).generate();

const noop = () => {}, ev = { on: noop, off: noop };
const output = { sampleRate: 44100, open: noop, play: noop, destroy: noop, pause: noop, addSamples: noop, resetSamples: noop, activate: noop,
  ready: ev, samplesPlayed: ev, sampleRequest: ev, enumerateOutputDevices: async () => [], setOutputDevice: async () => {}, getOutputDevice: async () => null };
const opts = new at.synth.AudioExportOptions();
opts.soundFonts = [new Uint8Array(readFileSync(sfPath))];
opts.sampleRate = 44100; opts.masterVolume = 1; opts.metronomeVolume = opt.metronome ? 1 : 0;
const exp = new at.synth.AlphaSynth(output, 500).exportAudio(opts, midi, [], new Map());
const chunks = [];
for (let c; (c = exp.render(1000));) chunks.push(c.samples);

// 交错立体声 float → 16-bit WAV
const n = chunks.reduce((a, s) => a + s.length, 0);
const pcm = Buffer.alloc(n * 2);
let o = 0, peak = 0, sq = 0;
for (const s of chunks) for (const v of s) { peak = Math.max(peak, Math.abs(v)); sq += v * v; pcm.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(v * 32767))), o); o += 2; }
const h = Buffer.alloc(44);
h.write('RIFF', 0); h.writeUInt32LE(36 + pcm.length, 4); h.write('WAVEfmt ', 8); h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20); h.writeUInt16LE(2, 22);
h.writeUInt32LE(44100, 24); h.writeUInt32LE(44100 * 4, 28); h.writeUInt16LE(4, 32); h.writeUInt16LE(16, 34); h.write('data', 36); h.writeUInt32LE(pcm.length, 40);
writeFileSync(out, Buffer.concat([h, pcm]));
console.log(JSON.stringify({ out, seconds: +(n / 2 / 44100).toFixed(2), peak: +peak.toFixed(3), rmsDb: +(10 * Math.log10(sq / Math.max(1, n))).toFixed(1) }));
