# 练习页音色库

练习页先加载 `sonivox.sf2`（alphaTab 自带的完整通用音色库，Apache-2.0），再叠加 `guitars.sf3`。alphaTab 查音色时后加载的优先，所以：
- 钢弦 / 尼龙吉他（24、25 号）用 `guitars.sf3` 里的真实录音；
- 节拍器、GP 文件里的贝斯、鼓、钢琴等其他乐器用 Sonivox。

`guitars.sf3` 的采样用 Ogg Vorbis 压缩（SF3，alphaTab 能直接读）：

| 音色位 | 内容 | 来源 | 许可 |
|---|---|---|---|
| 0:25 钢弦吉他 | FSS Steel-String Acoustic Guitar（2020-05-21，SF2 高音质版），加了 7 dB 衰减和 6 kHz 低通（原采样高音弦的高频很刺） | [FreePats](https://freepats.zenvoid.org/Guitar/steel-acoustic-guitar.html)，采样来自 FlameStudios 的 Seagull 钢弦吉他 | GPL-3.0+，附音色库例外条款：用它做出来的音乐不受 GPL 约束 |
| 0:24 尼龙吉他 | Spanish classical guitar（2019-06-18），加了 5 dB 衰减 | [FreePats](https://freepats.zenvoid.org/Guitar/acoustic-guitar.html) | CC0 |

许可证原文在 `licenses/`。

为什么吉他不用 Sonivox：它整个通用音色库才 1.3 MB，吉他是很短的循环采样，每个音的衰减曲线都一样，听起来偏“电子”。FreePats 这两个是逐音录的真实吉他，每个音平均约 5 秒，自然衰减。

## 重新生成

```bash
# 下载上面两个 FreePats 的 SF2 包并解压
uv run --with soundfile --with numpy python scripts/build_soundfont.py app/static/vendor/soundfont/guitars.sf3 \
  --ogg-level 0.3 --name "tab-trainer guitars" \
  --take "FSS-SteelStringGuitar-20200521.sf2:0:0=0:25+atten=70+lowpass=6000#Steel Guitar (FSS)" \
  --take "SpanishClassicalGuitar-20190618.sf2:0:0=0:24+atten=50#Nylon Guitar"
```

调整后可以用 `node scripts/render_audio.mjs` 离线渲染成 WAV 来对比响度和音色（离线渲染只用一个音色库，听节拍器时传 `sonivox.sf2`）。
