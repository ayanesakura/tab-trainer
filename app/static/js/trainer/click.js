// 节拍器的“咔哒”：播放（alphaTab 的节拍器、预备拍）和敲节奏（WebAudio 自己排的节拍器）共用一套定义——
// 音量怎么从设置换算、预备拍什么时候响、声音是什么样的。这个文件不碰 DOM 和 alphaTab（Node 测试直接用）。
//
// 声音：敲节奏用 alphaTab 自己的合成器渲染出来的节拍器声音（at-adapter.js 的 renderMetronomeClick：同一个音色库里的
// GM 33 号 Metronome Click），和播放时听到的一样；渲染不出来（谱子还没加载好）时退回一个短促的正弦“咔哒”（FALLBACK）。
// alphaTab 的节拍器每拍都一样响；敲节奏在小节第一拍稍微响一点（ACCENT / WEAK），好认出小节的开头。

/** alphaTab 的音量（metronomeVolume / countInVolume）= 这里的 0–1 × GAIN；敲节奏的增益也乘它，两边一样响 */
export const GAIN = 1.2;
/** 敲节奏：小节第一拍和其他拍的相对音量 */
export const ACCENT = 1, WEAK = 0.7;
/** 渲染不出 alphaTab 的声音时的替代：正弦波的频率（重拍 / 其他拍）、起音、衰减（秒）、相对增益（正弦波比采样“实”，压低一点） */
export const FALLBACK = { freq: [1760, 1180], attack: 0.002, decay: 0.06, gain: 0.6 };

const vol = p => (p.metroVol ?? 70) / 100;
/** 节拍器（每一遍里的拍子）：关着就是 0 */
export const metronomeLevel = p => (p.metro ? vol(p) : 0);
/** 一定要听得见的咔哒（预备拍、敲节奏的延迟校准）：跟着节拍器音量走但不低于一半（节拍器关着、音量拉到底也听得见） */
export const audibleLevel = p => Math.max(0.5, vol(p));
/** 预备拍：开着就响 */
export const countInLevel = p => (p.countIn ? audibleLevel(p) : 0);
/** 设给 alphaTab 的值 */
export const metronomeGain = p => metronomeLevel(p) * GAIN;
export const countInGain = p => countInLevel(p) * GAIN;
/** 敲节奏里一下咔哒的增益：level 是上面两个之一，accent = 小节第一拍 */
export const clickGain = (level, accent) => level * GAIN * (accent ? ACCENT : WEAK);
