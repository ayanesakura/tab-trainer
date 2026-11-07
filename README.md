# tab-trainer

本地部署的吉他谱练习器：上传六线谱图片 / PDF（用 Claude 识别），或直接导入 Guitar Pro / MusicXML 文件，得到可播放、可循环、带指板显示和 AI 讲解的练习页面。

## 功能

- **导入 Guitar Pro / MusicXML**：`.gp/.gpx/.gp3–5/.musicxml/.mxl` 本来就是精确数据，不用 AI 识别、不花钱；可以再让 AI 根据谱子数据给每个小节写练习提示（只发文字，费用很低）。
- **上传识别**：支持多张图片或 PDF。自动找出每一行六线谱，逐行交给 Claude 识读，读出品位、节奏和演奏技巧（击弦、勾弦、滑音、泛音、闷音、琶音、倚音、推弦等），并为每个小节写练习要点。
- **双重校对**（设置里打开，默认关）：每行识别两遍，两遍不一样的小节再读一遍投票；三遍都不一样的小节标成待核对。评测里完全正确的小节从约 89% 提高到约 95%，费用约 2 倍。
- **弹唱谱**：谱面上方印着和弦名和和弦图、六线谱上用 × 和扫弦箭头记的弹唱谱也能识别：
  - × 按当时的和弦图换算成实际品位（不当成闷音），扫弦箭头记成下扫 / 上扫，播放时有扫弦的先后；
  - 和弦图画在每次和弦变化的拍上方（像原谱那样），弹唱谱默认只看六线谱（切换后记住）；侧栏的小节卡片显示这一小节的和弦图（小节开头延续前面的和弦时也列出来）；
  - 歌词按旋律的节奏对到吉他的拍上，显示在谱面里，多段歌词各占一行（Guitar Pro 文件里的歌词也会读出来）；
  - 同一个和弦在不同行读出的指法不一样时（常见于“3fr.”这类位置标记），以多数行为准，把读错的那几行重新识别一次；
  - 改音面板里可以改这一拍的和弦名和扫弦方向；Guitar Pro 文件里的和弦和扫弦也会读出来。
- **自动校验**：
  - 每个小节的时值加起来必须等于拍号（曲中变拍号时按谱上写的变拍号算，模型会标出在哪个小节变了）；
  - 本机用 OCR 把谱上的品位数字再读一遍（数字压在哪根线上就是哪根弦，不调用模型、不花钱），和 AI 的结果逐小节对比；
  - 有问题的会把具体差异反馈给模型返修一次，仍有问题的小节在练习页用橙色标出。
- **练习页**：
  - 谱面渲染和播放用 [alphaTab](https://github.com/CoderLine/alphaTab)：五线谱 + 六线谱（显示节奏），跟随光标，节拍器和预备拍；
  - 示范音是逐音录制的真实木吉他采样，可以在钢弦和尼龙弦之间切换；
  - 可以导出 Guitar Pro 文件（`.gp`），在其他软件里继续用；
  - 可以循环任意小节，按原谱的每一行一键选段，支持自动提速；
  - 自动跟随：播放时光标走到看不见的行，谱面自己滚过去（手动滚动后停几秒，不和你抢；可以在“更多”里关掉）；
  - 节拍器、预备拍、音量、自动提速、自动跟随这些设置记在本机，所有曲子共用；
  - 指板实时显示正在弹和下一个要弹的位置；
  - 显示当前小节的原谱截图，方便对照；
  - 调弦表（含参考音）和麦克风调音器，调弦、变调夹、拍号都跟着曲子走；
  - AI 根据全曲生成的分段练习建议；
  - **问 AI**：在小节详情里直接问这一小节的问题（“左手怎么安排指法”“这个泛音总弹不响怎么办”），AI 会结合这个小节的数据、前后小节、原谱截图和你的练习记录回答，问答会保存下来；
  - **敲节奏**：控制条里点“敲节奏”，不拿琴、只练节奏：跟着示范（或只有节拍器）用空格 / F / J 键敲出循环段里每个音的时间。每个音按早晚上色（±40 ms 内算准），每遍打分，并指出问题：整体偏早还是偏晚、快慢稳不稳、哪个位置总是错。有三种练法：跟着示范、听一遍再敲、只有节拍器。音符下面标着口令（1 e & a、1 trip let）。节拍器和示范音由页面自己用 WebAudio 排程，敲击和声音用同一个音频时钟比较；用蓝牙耳机时可以先“校准”，补偿声音延迟。打开自动提速后，要连续几遍 90 分以上才加速；
  - **练习记录**：每循环完一遍自动记下速度和用时，小节详情里显示“循环几遍、最快多少 BPM、上次什么时候”；打开曲子时提示上次练到哪里，一键接着练。
- **曲库**：
  - 按曲名、艺人、标签搜索，按标签筛选，按最近添加 / 最近练习 / 曲名排序；
  - 每首曲子显示 AI 一共花了多少钱（按模型价格估算；早期曲子只记了 token，按 Opus 价格估算）；
  - 图片 / PDF 曲子可以“管理页面”：补传漏掉的页、删页、调整页序。换顺序和删页不调用 AI，补的页只识别新页上的行；
  - 整首重新识别时，和原来一样的小节保留提示、问答和练习记录，手改过的小节保留你的修改，手改过的调弦、变调夹、拍号以你的为准。
- **纠错**：
  - 打开谱面上的“改音”，点击音符就能改弦、品位、技巧和这一拍的时值（小节总长自动保持不变），或删掉它、在同一拍加音；点击休止符可以在那一拍加音；<kbd>⌘Z</kbd> 撤销；
  - 倚音等更复杂的改动可以用“修改本小节”直接改数据；
  - 也可以附上一句说明（如“第 3 拍应该是 2 弦 4 品”），让 AI 重新识别那一行；
  - 有识别失败的行时，练习页和曲库里可以“重试失败的 N 行”，只重新识别这几行；
  - 调弦、变调夹、拍号识别错了可以手工改（Guitar Pro / MusicXML 曲子的这些来自原文件，只能改曲名）。
- **曲子包（导出 / 导入）**：曲库里点“导出…”勾选几首（或全选），或在某首的“…”菜单里单独导出，得到一个 `.zip`。里面有识别结果和手工修改、每小节的提示、练习建议、问答、练习记录，以及原图页面或 GP / MusicXML 原文件；不含模型设置和 API key。在另一台电脑（或删掉后在本机）点“导入曲子包”，或把 `.zip` 拖进上传框就能恢复，可以一次选多个包。曲库里已经有的曲子（同一个 id）会跳过，不会重复；想用包里的版本替换，先删掉曲库里的那首。

## 界面

界面参考了 [magpie](https://github.com/yetone/magpie) 的设计语言：
- 浅灰底配白卡片，细线分隔，靛蓝强调色；
- 跟随系统切换亮/暗主题，也可以手动切换（顶栏按钮：跟随系统 → 浅色 → 深色）；
- 选项用分段开关，状态用圆点（失败用 × 形状），操作结果用底部的轻提示，确认和输入用界面里的对话框（不用浏览器自带的弹窗）；
- 全部操作都能用键盘完成：上传框可以 Tab 到再按 Enter / 空格，菜单用 ↑ ↓ 选择，Esc 关闭最上面的浮层，关闭后焦点回到原来的按钮。

用词：**曲子** = 曲库里的一条；**谱面** = 渲染出来、可以播放的谱；**原谱** = 上传的图片 / PDF。

曲库里每首曲子显示上次练习的时间、练到哪几个小节和累计时长，点“接着练”直接回到上次的循环范围和速度；可以按最近练习 / 最近添加 / 曲名排序。

全部是原生 HTML/CSS/JS，不需要前端构建，脚本都是 ES 模块（`<script type="module">`）。共用部分：

| 文件 | 内容 |
|---|---|
| `app/static/ui.css` | 设计变量（颜色、字号 4 档、圆角 3 档、间距 4 档、阴影、原谱底色、指板配色，亮暗两套）和通用组件（按钮、分段开关、卡片、列表行、chips、通知、菜单、对话框） |
| `app/static/ui.js` | `$` / `esc`、图标（`esc` 和 `icon` 本身在不碰 DOM 的 `js/lib/html.js`，Node 测试也能用）、主题、轻提示、浮层栈 `layer`（Esc / 点外面关闭、焦点移入和还回）、`menu`、`confirm` / `prompt`、`notice`、`seg` / `tabs` |
| `app/static/js/lib/api.js` | 和后端通信：`get` / `post` / `put` / `del` / `upload` / `download`（出错统一抛带中文说明的 `ApiError`）、`withBusy`（按钮忙状态、防重复提交）、`poll`（轮询，出错退避重试） |
| `app/static/js/lib/theme-boot.js` | 在 `<head>` 里同步加载的小脚本，首屏前写好主题，避免暗色下闪白 |
| `app/static/js/vocab.js` | 前后端共用词表 `js/vocab.json` 的常量：弦数和标准调弦、技巧名称和记号、反复记号、来源、直接导入的曲谱扩展名（`SCORE_EXT` / `isScoreFile`）、警告代码、`parseDur` |

主题：`<html data-theme>` 总是写实际生效的 `light` / `dark`（选“跟随系统”时由脚本监听系统外观变化），CSS 里暗色只有一个选择器 `:root[data-theme="dark"]`。页面自己的样式只用变量，不写死颜色。

## 第三方组件

- alphaTab 1.9.0（`app/static/vendor/alphatab/`，MPL-2.0）：谱面渲染、播放和 Guitar Pro 读写。它要求渲染出的谱面上保留“♪ alphaTab.net”署名，不得去掉、隐藏或遮挡（见 https://alphatab.net/docs/attribution）。
- Bravura 字体（SIL OFL 1.1）：随 alphaTab 附带，许可证文件在同一目录。
- 吉他音色（`app/static/vendor/soundfont/guitars.sf3`）：FreePats 的 FSS 钢弦吉他（GPL-3.0+，附音色库例外条款）和西班牙古典吉他（CC0），节拍器和 GP 文件里的其他乐器用 alphaTab 自带的 Sonivox（Apache-2.0）。来源、改动和重新生成的方法见该目录的 README。

## 快速开始

需要 [uv](https://docs.astral.sh/uv/)。

在访达里双击 `start.command`：自动安装依赖、启动服务并打开浏览器（http://127.0.0.1:8765）。关掉弹出的终端窗口就停止服务。

也可以在终端里手动启动：

```bash
uv sync
uv run python -m app.main          # 打开 http://127.0.0.1:8765
```

第一次使用先在首页“模型设置”里填 Anthropic API key，点“测试连接”确认能用，再上传谱子。

key 只保存在本机 `data/config.json`（权限 600）。也可以不在页面里填，改为启动前设置环境变量 `ANTHROPIC_API_KEY`。

程序**只**使用这两处的 key，不会读取 `ANTHROPIC_AUTH_TOKEN`、`ANTHROPIC_BASE_URL` 等其他工具留下的环境变量。

可选参数：`--port` 改端口；`--host 0.0.0.0` 让局域网内其他设备（如平板）访问。开放局域网时会生成一个访问令牌，启动信息里会打印带令牌的地址（`http://<本机IP>:8765/?token=…`），其他设备用这个地址打开一次即可；本机访问不需要令牌。想固定令牌可以设置环境变量 `TAB_TRAINER_TOKEN`。

访问限制：只接受用 `127.0.0.1`、`localhost`、局域网 IP 或 `xxx.local` 打开的页面（防止恶意网页通过 DNS rebinding 读写接口、骗走 API key）；其他网站发来的写请求一律拒绝。上传上限：单个文件 50 MB、一次 200 MB、40 个文件、一首曲子 60 页、单页 6000 万像素（见 `app/songs.py` 开头）。

## 模型与费用

- **默认模型**：`claude-opus-5-5`，识别精细度默认 `high`，开启了服务端兜底（`fallbacks: "default"`）。
- **省钱选项**：可以在设置里改成 `claude-sonnet-5-5`（实测见下表，排版干净的谱子和 Opus 一样准，复杂的谱子略差），或把精细度调到 `medium`。
- **调用次数**：每行谱子 1 次调用（时值不对或和 OCR 对不上时再加 1 次返修），另外每首曲子还有 1 次读谱头和 1 次生成练习建议。
- **实测**（2026-11-07，精细度 high，按标准价格估算；每次运行会有波动；加弦号标注和 OCR 校对之前，同一首曲子 Opus 跑过 96.7%–99.8%）：

  | 曲子 | 模型 | 音符准确率（F1） | 小节完全一致 | 费用 | 用时 |
  |---|---|---|---|---|---|
  | 勿听（37 小节，中文网站的六线谱截图） | Opus（2 次） | 99.8% / 99.8% | 97.3% / 97.3% | $1.26–1.31 | 2.5 分钟 |
  | 勿听 | Sonnet（2 次） | 99.3% / 99.1% | 97.3% / 94.6% | $0.95–1.04 | 3.6 分钟 |
  | Romance（32 小节，GP 风格的五线谱 + 六线谱，两个声部） | Opus | 100% | 100% | $0.71 | 1.3 分钟 |
  | Romance | Sonnet | 100% | 100% | $0.41 | 1.2 分钟 |

  （弦号标注 + OCR 校对之后的结果。）这 6 次运行里弦号和品位一处没错，剩下的错误都是节奏（十六分音符怎么分组、三连音）。
  对比：同样的谱子，GPT-6 Astra / GPT-6.1 Sol 只有 42%–90%，而且慢 6–10 倍。
- **用量记录**：每首曲子的 token 用量和按价格表（`app/llm.py` 的 `PRICES`）算出的费用保存在 `song.json` 的 `usage` 字段里，曲库里会显示。自定义模型不在价格表里，只记 token。

## 工作原理

1. `app/layout.py`：用像素分析找出六线谱：横向贯穿大半页宽的暗行是谱线，6 根一组是六线谱、5 根一组是五线谱；六线谱上下线之间全暗的竖列是小节线。
2. `app/llm.py`：
   - 把每一行（五线谱 + 六线谱）裁出来交给 Claude，同时告诉它这一行有几个小节、每个小节的横向位置，要求按 JSON Schema 输出；
   - 裁出来的图上，六线谱左边用红色标出弦号 1–6。模型数线时容易把低音弦上的数字往下错一根（5 弦读成 6 弦），标上之后评测里这类错误从每次 1–4 处降到 0（只在文字里告诉它弦线坐标没有用）；
   - 首页另做一次调用，读出曲名、调弦、变调夹和拍号。
3. `app/recognize.py`：
   - 4 路并发识别（所有曲子合计最多 4 行同时进行），校验时值、返修，再给全曲统一编号；
   - `app/ocr.py`：本机读每一行的品位数字（版面用我们自己检测到的弦线位置，数字识别用 PaddleOCR 的识别模型，只解码 0–9），和 AI 的结果对比后把差异交给返修。评测曲上它读出的（弦, 品）有 99.4%–100% 是对的；
   - 最后根据全曲内容生成分段练习建议。
4. `app/static/`：曲库页和练习页。谱面和示范音由 alphaTab 在浏览器里渲染和合成，音色库是本地文件，不需要联网。

如果某一页检测不到六线谱（比如手机拍的照片有倾斜和透视），会退回为把整页交给模型识别。这种情况准确率较低，原谱对照也只能显示整页。

## 限制

- 只支持六弦吉他、有六线谱的谱子。只有五线谱或简谱的不行。
- 反复记号、跳房子和 D.C. / D.S. / Coda / Fine 会按谱上的记号播放（识别出来的曲子要靠模型读对这些记号）。循环某一段时，只弹选中的这几个小节（取播放顺序里依次经过它们的那一遍）；按记号根本弹不到的小节（比如 Fine 之后的）没法单独循环。
- 曲中变拍号要靠模型读出谱上写的新拍号；漏读时，之后的小节会被标成时值不对（重新识别那一行通常能修正）。
- 节奏只支持到 64 分音符和三连音、六连音；五连音、七连音这类时值会在小节上提示，播放时按最接近的位置近似。
- 改过音、重新识别过、改过调弦以后，受影响小节的 AI 提示会清掉，问答标成“改谱前”，练习建议标为可能过时；点“让 AI 补写提示和练习建议”重新生成。
- 识别结果一定要对照原谱检查，尤其是滑音、倚音和连音密集的地方。

## 识别准确率评测

`app/evaluate.py` 拿一首人工校对过的曲子当标准答案，用当前设置重新识别它的每一行，逐小节比对：
- 音符：以（起拍, 弦, 品）为单位算 F1；
- 节奏：以起拍位置算 F1；
- 技巧记号：同样算 F1；
- 小节完全一致率、时值凑满率。

报告里会列出错得最多的小节。

```bash
uv run python -m app.evaluate <曲子id>                 # 只打印调用次数和预估费用
uv run python -m app.evaluate <曲子id> --yes           # 真正运行（产生 API 费用）
uv run python -m app.evaluate <曲子id> --yes --systems 0,5 --model claude-sonnet-5-5
```

结果写在 `data/evals/<时间>-<曲子>-<模型>/` 下，不改动曲库里的数据。`report.json` 里还保存了模型每次调用的原始输出，方便排查。

### 用 Guitar Pro 文件做评测曲

GP / MusicXML 文件本身就是精确数据，可以直接当标准答案。`scripts/make_eval_song.py` 用 alphaTab 在无头 Chrome 里把它渲染成五线谱 + 六线谱的图片（和 GP 导出的谱子截图差不多），按真实上传的流程做版面检测，再把文件里的小节按检测到的谱行分配进去：

```bash
uv run --with websockets python scripts/make_eval_song.py 曲子.gp5 eval-xxx --title "名字"
uv run python -m app.evaluate eval-xxx --yes
```

版面检测找到的小节数和文件对不上时脚本会报错，这本身就说明版面检测在这种排版上有问题。报错里会同时列出 alphaTab 实际每行几个小节，对照一下就知道是哪一行出的问题；加 `--save-full 图.png` 可以另存整张渲染图。

谱面样式可以换，让评测集不全是一个样子：

| 选项 | 作用 |
| --- | --- |
| `--stave score-tab` | 默认：五线谱 + 六线谱 |
| `--stave tab` | 只有六线谱，节奏画在谱上（alphaTab 原样：符干从音符往下穿过弦线） |
| `--stave tab-below` | 只有六线谱，符干挂在最低弦下方、不穿过弦线（渲染时临时改一份 alphaTab 副本） |
| `--width 760` | 谱面宽度（CSS 像素，默认 1100），窄一些每行小节就少 |
| `--bars-per-row 3` | 每行固定几个小节 |
| `--scale 1.25` | 渲染倍率（默认 1.5） |
| `--per-page 4` | 每页几行谱 |
| `--hide effectLetRing` | 不画某种记号（alphaTab 的 NotationElement 名字，可重复） |

目前版面检测在这些情况下会数错小节（做评测曲时要避开，或者用上面的选项绕开）：

- `--stave tab`：符干从一弦的音往下穿过整个六线谱，被当成小节线。
- 扫弦箭头（brush）从一弦画到六弦时同样被当成小节线，两种样式都会出问题。
- 紧贴在六线谱上方的整行 “LetRing - - -” 虚线（离一弦大约 1.6 倍线距）会和一弦凑成一组，剩下 5 根线被当成五线谱，整行丢失。用 `--hide effectLetRing` 绕开。
- 一页图太矮（谱线间距超过图高的 3%，比如一页只有一两行六线谱）时整页检测不到谱线；最后一行很短（不到 12 倍线距宽，比如只有一个全音符小节）时这一行会被丢掉。调 `--per-page`、`--bars-per-row` 绕开。

## 代码结构

后端按职责分层，下层不依赖上层（HTTP → 编排 → 业务 → 数据模型 / 存储）：

| 模块 | 职责 |
|---|---|
| `app/main.py` | 只做 HTTP：路由、参数、文件、响应；业务错误（`AppError`）由统一的异常处理转成 `{"detail": 中文说明}` 和状态码 |
| `app/service.py` | 接口背后的编排，所有路由都只调它：上传后启动识别、删除、整首 / 单行重新识别、重试失败的行、页面、讲解、问答、曲子包、取消、文件路径；写接口返回改完的曲子视图。不少函数只是一行转发，留着让 `main` 只依赖这一个模块 |
| `app/views.py` | 接口返回的形状：曲子详情（SongView）和曲库列表项，忙不忙 / 能不能改 / 小节锁定 / 待核对都在这里算好（见下面的接口约定） |
| `app/auth.py` | 局域网访问令牌、Host / Origin 检查 |
| `app/songs.py` | 曲库：新建（图片 / PDF / GP / MusicXML）、保存浏览器转换的小节（GP 小节是只读的缓存）、手工修改（按小节 `rev` / 谱头 `meta_rev` 的乐观锁）、启动恢复 |
| `app/recognize.py` | 图片谱子识别：版面 → 逐行调模型（整首、补页的新行、重试失败的行共用一套 `_recognize_rows`）→ OCR 校对 → 返修 → 按前面的变拍号重做 → 练习建议；单行重新识别 |
| `app/pages.py` | 图片 / PDF 曲子的页面管理：补页、删页、调整页序（新页上的行交给 `recognize.recognize_rows_job`） |
| `app/coach.py` | AI 讲解（补写小节提示、重新生成练习建议）和小节问答 |
| `app/practice.py` | 练习记录：`practice.jsonl` 追加写，读取时按小节 id 聚合（按文件修改时间缓存；格式不对的行跳过），练习历史 |
| `app/state.py` | 曲子的运行状态 `status` + `activity` + `notice`，以及由它们算出的忙 / 可改 / 锁定（唯一一处） |
| `app/staleness.py` | 改谱后 AI 内容的失效与恢复；`reconcile_bars`：新旧两批小节按内容 + 位置对上（GP 重新转换、整首 / 单行重新识别共用） |
| `app/model.py` | 唯一的数据定义（Pydantic）：song.json 的形状和字段间的不变量（`problems`，每次写盘前检查；测试里违反就报错）、派生字段“读时算、写时去”（`derive` / `strip`）、给模型的音符 schema、接口返回的模型 |
| `app/migrate.py` | song.json 格式升级（`SCHEMA_VERSION = 2`），所有旧数据兼容都在这里，业务代码只认当前格式 |
| `app/text.py` | GP 老格式 GBK 中文乱码的还原（导入时和旧数据升级共用；前端 `score.js` 同一套规则） |
| `app/music.py` | 调弦、拍号、时值、事件和反复记号的整理与校验、小节警告 `{code, msg}`（纯函数）；常量来自 `app/static/js/vocab.json`（含直接导入的文件扩展名 `score_files`） |
| `app/config.py` | 运行参数（并发数、上传 / 导入上限等，`config.settings`）和模型设置（读写、打码、建客户端） |
| `app/jobs.py` | 后台任务登记 / 取消（每首曲子同时一个）；`start`（登记 → 标开始 → 后台跑，标开始失败时释放）；`UsageMeter`（把模型用量记到曲子上，失败 / 取消时补记） |
| `app/errors.py` | 错误类型：Invalid 400 / NotFound 404 / Busy、Conflict 409 / CorruptSong 500 / Upstream 502（`llm.LLMError` 也是） |
| `app/llm.py` | Claude 客户端：请求、流式重试、用量和费用（`PRICES`、设置页的预设模型）、错误说明 `friendly`；`LLM` 接口 |
| `app/prompts.py` | 给模型的提示词和 JSON Schema（`tests/test_prompts.py` 是快照：有意改提示词时用 `UPDATE_PROMPTS=1` 重新生成，再核对 diff） |
| `app/layout.py`、`app/ocr.py` | 版面检测、本机数字识别 |
| `app/schemas.py` | 接口请求体的类型、长度和数值范围（只在这里检查），格式不对返回 400 和中文说明 |
| `app/bundles.py` | 曲子包：多首曲子（含练习记录）打包成 zip 导出、导入（只按清单和 song.json 取文件，检查大小 / 图片 / 原文件 / 数据形状（时值、拍号、练习记录按 `model` 检查），先解到临时目录再改名；单首有问题只跳过那一首） |
| `app/store.py` | `data/` 下的文件读写：加锁的读-改-写（顺带维护 `rev` 和按对象的 `bar.rev` / `meta_rev`）、`.bak` 备份、坏文件回退、锁内删除、`practice.jsonl` 追加、单实例锁 |
| `app/evaluate.py` | 识别准确率评测 |

### 并发模型

后台线程（识别、讲解）和接口请求会同时改同一首曲子。六种机制，各防一件事：

| 机制 | 在哪 | 防什么 |
|---|---|---|
| 任务登记 `jobs.claim` / `jobs.start` | 进程内，每首曲子一个 | 同一首曲子同时跑两个任务（连点两次、识别时又点讲解）。登记后在 `store.update` 里标上 `activity` 才算开始，标失败就释放 |
| `activity` + `systems[i].status == "parsing"` | 存盘，`state.py` 解释 | 告诉前端忙不忙、哪些小节不能改（`state.bar_locked`）；服务重启后由 `state.recover` 标成中断 |
| `store._lock`（全局可重入锁）+ `store.update` | 文件读写 | 读-改-写不互相覆盖。规则：**挑选要改的东西和标上锁必须在同一次 `update` 里**（例如按变拍号重做哪些行：`recognize._lock_redo`），否则中间用户的改动会被覆盖 |
| `bar.rev` / `meta_rev` | 存盘，`model.bump_revs` 维护 | 用户的两次编辑互相覆盖（乐观锁，409）。后台写回不会让它们过期 |
| 写回前比较快照（`seen`） | `coach._explain_chunks`、`staleness.store_plan` | 后台按旧内容写的提示 / 练习建议，不能写到等待期间已经被改过的小节上 |
| 全局信号量 `recognize._system_slots` | 进程内 | 所有曲子加起来同时识别的行数（连续上传多首时不会成倍地发请求、跑 OCR） |

删除曲子在锁里先把目录改名，之后后台任务的写回一律得到 `NotFound`（`jobs.spawn` 只记一笔），不会把目录重新建出来。模型用量由 `jobs.UsageMeter` 随结果一起写回，失败 / 取消时在 `finally` 里补记。

数据文件：`data/songs/<id>/song.json`（只存一份事实：小节编号、时值等按内容算的警告、`meta.title`、`meta.tuning_midi`、`plan.stale` 都是读取时算的）、`practice.jsonl`（一段练习一行）、`pages/` 或 `source.gp5` 等原文件；`data/config.json` 存模型设置。

### 接口约定（前端只显示，不自己推断）

- `GET /api/songs/{id}`：曲子本身的字段（不含 `_` 开头的内部字段），另加 `rev`（每次保存 +1，只用来判断要不要重绘，不用于 409）、`meta_rev`（谱头版本号，见下）、`busy`（有任何后台任务）、`recognizing`（整首或某几行在识别，讲解不算）、`activity`（`null` 或 `{kind: parse|reparse_row|explain, restaging, systems}`）、`can_edit_bars`（GP / MusicXML 为 false）、`editable_meta`、`notice`（`null` 或 `{kind: warn|error, msg}`）、`practice`（`{bars: {id: {loops, seconds, best_bpm, last}}, last: {a, b, bpm, at, a_n, b_n} | null, total_seconds}`）。每个小节另有 `rev`（这个小节内容的版本号，见下）、`n`、`warn: [{code, msg}]`（code 见 `vocab.json` 的 `warn_codes`）、`needs_review`、`locked`（不能改的原因或 null）、`edited`。
- 两个接口都另有 `is_score`（GP / MusicXML，小节来自原文件）和 `failed_rows`（识别失败的行数）；`POST /api/songs/{id}/retry-failed` 只重新识别这些行（一个小节都还没有时整首识别），没有失败的行返回 400。
- `GET /api/songs`：每首另有 `busy`、`recognizing`、`activity`、`notice`、`needs_review_bars`、`practice`（`null` 或 `{last_at, total_seconds, last: {a, b, bpm, a_n, b_n}}`）。
- 整首重新识别已有结果的曲子时 `status` 不变（原来的结果照常能练），看 `recognizing` / `activity.restaging`。
- 乐观锁按对象：`PUT /api/songs/{id}/bars/{bar_id}` 可以带 `rev` = 这个小节的 `rev`，`PUT /api/songs/{id}/meta` 可以带 `rev` = 曲子的 `meta_rev`；和当前不一致返回 409（“曲子在别处改过了，已刷新，请再改一次”），不带照旧。小节的 `rev` 只在它的内容变了时 +1（手改音符或提示、重新识别 / GP 重新转换换了内容、改调弦清掉了提示）；`meta_rev` 只在可改的谱头字段（曲名、艺人、调弦、变调夹、拍号）变了时 +1。问 AI、讲解补提示、练习记录、进度、识别别的行、改别的小节都不会让手里的版本号过期。旧数据没有的按 0 算。改 GP / MusicXML 曲子的小节返回 400。
- `GET /api/songs/{id}/practice/history` → `{sessions: [{at, a, b, bpm, seconds, loops}]}`。

前端没有构建步骤：`app/static/trainer.html` 和 `index.html` 只放页面结构，样式在 `css/`，脚本在 `js/`，全部是 ES 模块。曲库页是 `js/index.js`（事件和 DOM）+ `js/library-view.js`（纯函数：一行曲子的 HTML、状态 / 练习 / 花费说明、筛选排序，`tests/js/library.test.mjs` 直接测）。

练习页从 `js/trainer/main.js` 进入，按功能拆成模块。模块之间的规矩：**`song.js` 和 `player.js` 是可以直接调用的服务模块，其余模块之间只通过 store 通信**（订阅 `ui.bar`、`play.*` 等路径），不互相 import 对方的函数。具体：
- `store.js`：一个小 store（`song` 后端数据、`view` 派生结构、`play` 播放状态和设置、`ui` 界面状态、`rhythm` 敲节奏），按路径 `set` / `get` / `on`，值没变不通知；`dom.js`：`$` / `UI` / `songUrl` / `setButton` / `svgEl` 这些 DOM 小工具；`page-images.js`：原谱页面图片的共用缓存（加载好发 `tt-images` 事件）。
- 服务模块：`player.js`（alphaTab 谱面和播放，对外 `play` / `stop` / `setLoop` / `setTempo` / `setStatus` / `barBox`；`setPlayerHooks` 让曲子、改谱、敲节奏把各自的处理挂进来，`holdBarWhile` 让侧栏在编辑器开着或正在输入问题时不跟着播放切小节，播放模块不反过来依赖它们）、`song.js`（取曲子、轮询、通知、练习建议、曲子信息；对外 `refresh` / `applySong` / `conflict`）。
- 纯函数（Node 测试直接用）：`view.js`（`deriveView(song)`、段落、换段、练习记录文案等）、`prefs.js`（本机记住的播放设置：读的时候逐项检查，坏值用默认值）。
- 功能模块：`bar-card.js` / `orig.js` / `coach.js` / `practice-log.js` / `editor.js` / `fretboard.js`（各自订阅状态）、`follow.js`（自动跟随：光标所在行看不见时自己滚过去，用户手动滚动后停几秒）、`tuner.js` / `glossary.js` / `audio.js`（调弦页、记号说明和示范音）、`rhythm-drill.js`（敲节奏：节奏条、WebAudio 排程的节拍器和示范音、敲击打分；状态在 store 的 `rhythm` 切片，播放键 / 空格 / 换段通过 `player.js` 的钩子交给它）。

页面上 `window.tt` 是给端到端测试和调试用的句柄。`js/score.js` 是曲谱数据 ⇄ alphaTab 的转换（改 `deriveSong` 要同时加 `DERIVE_VERSION`），也负责认出中文 GP3–5 文件的 GBK 乱码（练习页和 `scripts/make_eval_song.py` 遇到时按 GBK 重新读）。`js/rhythm.js` 是敲节奏的打分逻辑（要敲的点和口令、配对、打分、诊断；不碰 DOM 和音频，`tests/js/rhythm.test.mjs` 直接测）。

测试基本按模块分（`tests/test_<模块>.py`；`service` / `views` / `state` / `jobs` 由 `test_api`、`test_pages` 等间接覆盖；测试里每次写盘都检查 `model.problems`），替身模型和合成谱子在 `tests/fakes.py`，公共 fixture 在 `tests/conftest.py`。`tests/fixtures/llm_schemas.json` 是重构前给模型的 schema（`test_model.py` 断言逐字不变）；`golden.gp` / `golden_derived.json` 是浏览器转换的金标准（`tests/js/fixture.test.mjs` 和 `tests/test_golden.py` 两边各查一遍）。

## 开发

```bash
scripts/check.sh           # lint + 格式 + Python 单元测试 + JS 数据转换测试
scripts/check.sh --e2e     # 再加浏览器端到端测试（需要 Chrome，约 1–2 分钟）
uv run pytest tests/test_recognize.py -q      # 单独跑某个模块
node --test tests/js/*.test.mjs               # 只跑 JS 测试（要写成通配符：Node 24 的 --test 不接受目录）
UPDATE_PROMPTS=1 uv run pytest tests/test_prompts.py   # 有意改了提示词后重新生成快照，再看 git diff 核对
```

上传的谱子、识别结果和配置都在 `data/` 下，不进版本库。每次写入 `song.json` / `config.json` 前，上一版会留成同名的 `.bak`；文件损坏时自动用备份，两份都坏的曲子在曲库里显示“数据损坏”，可以删除。同一个数据目录只能有一个 tab-trainer 在运行。
