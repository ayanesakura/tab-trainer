import pytest
from PIL import Image, ImageDraw, ImageFont

from app import ocr, recognize, songs, store
from tests.fakes import FakeClaude, score_png

pytestmark = pytest.mark.skipif(
    not ocr.available(), reason="本地数字识别不可用（缺 onnxruntime / opencv 或模型文件）"
)

FONT = "/System/Library/Fonts/Helvetica.ttc"


def _tab_image(notes, sp=20, bars=2, width=900, stems=(), ink=0):
    """画一行六线谱：notes = [(小节, 小节内第几个位置 0..7, 弦, 品)]，品也可以写成 "(5)"、"<7>" 这样的字符串。
    stems = [(小节, 位置, 从第几弦, 到第几弦)]：只有六线谱的谱子里画在谱线中间的符干（和音一样被数字截断）。
    ink：数字的灰度（0 = 黑）。
    返回 (灰度图, 弦线 y, 小节范围)。"""
    img = Image.new("L", (width, 8 * sp), 255)
    d = ImageDraw.Draw(img)
    lines = [sp * 1.5 + i * sp for i in range(6)]
    x0, x1 = 60, width - 20
    for y in lines:
        d.line([(x0, y), (x1, y)], fill=120, width=1)
    bw = (x1 - x0) / bars
    spans = [(x0 + i * bw, x0 + (i + 1) * bw) for i in range(bars)]
    for a, _ in spans:
        d.line([(a, lines[0]), (a, lines[-1])], fill=0, width=2)
    try:
        font = ImageFont.truetype(FONT, int(sp * 0.85))
    except OSError:
        font = ImageFont.load_default(size=int(sp * 0.85))

    def at(bar, pos):
        return spans[bar][0] + 20 + pos * (bw - 40) / 7

    for bar, pos, s0, s1 in stems:
        d.line([(at(bar, pos), lines[s0 - 1]), (at(bar, pos), lines[s1 - 1] + 2.5 * sp)], fill=0, width=2)
    for bar, pos, string, fret in notes:
        x = at(bar, pos)
        y = lines[string - 1]
        if str(fret)[0] in "(<":  # 括号和数字之间留一点空（Helvetica 的 > 会贴到数字上，印刷谱里是分开的）
            fret = f"{fret[0]}\u200a{fret[1:-1]}\u200a{fret[-1]}"
        box = d.textbbox((x, y), str(fret), font=font, anchor="mm")
        # 数字压断谱线和符干，和印刷谱一样
        d.rectangle([box[0] - 2, box[1] - 3, box[2] + 2, box[3] + 3], fill=255)
        d.text((x, y), str(fret), font=font, fill=ink, anchor="mm")
    import numpy as np

    return np.asarray(img), lines, spans


def test_reads_frets_on_the_right_strings():
    notes = [
        (0, 0, 5, 0),
        (0, 1, 1, 12),
        (0, 2, 2, 3),
        (0, 4, 6, 7),
        (1, 0, 4, 10),
        (1, 3, 3, 2),
        (1, 5, 6, 0),
    ]
    gray, lines, spans = _tab_image(notes)
    got = ocr.read_system(gray, lines, spans)
    want = [sorted((s, f) for b, _, s, f in notes if b == i) for i in range(2)]
    assert [sorted(x) for x in got] == want


def test_detail_reports_brackets_without_changing_the_digits():
    """detail=True：每个音多了括号标记（鬼音 / 小节开头的延续音画成 (n)，泛音画成 <n>），读到的音和顺序不变。"""
    notes = [
        (0, 0, 2, "(5)"),
        (0, 2, 3, 7),
        (0, 4, 4, "<12>"),
        (1, 0, 1, "<7>"),
        (1, 3, 5, "(10)"),
        (1, 5, 6, 3),
    ]
    gray, lines, spans = _tab_image(notes)
    plain = ocr.read_system(gray, lines, spans)
    detail = ocr.read_system(gray, lines, spans, detail=True)
    assert [[(d.string, d.fret) for d in bar] for bar in detail] == plain
    flags = {(d.string, d.fret): (d.paren, d.angle) for bar in detail for d in bar}
    assert flags == {
        (2, 5): (True, False),
        (3, 7): (False, False),
        (4, 12): (False, True),
        (1, 7): (False, True),
        (5, 10): (True, False),
        (6, 3): (False, False),
    }
    # 括号不会被当成数字读出来（"<7>" 读成 77、")" 读成 1）
    assert sorted(plain[0]) == [(2, 5), (3, 7), (4, 12)]


def test_stems_between_strings_are_not_read_as_ones():
    """只有六线谱的谱子：符干画在谱线中间、被数字截成一段段，夹在两根弦线之间的一段不能当成 1；真的 1 照读。"""
    notes = [(0, 0, 2, 3), (0, 0, 4, 2), (0, 3, 3, 1), (1, 2, 1, 1), (1, 2, 4, 5)]
    stems = [(0, 0, 2, 6), (1, 2, 1, 6)]
    gray, lines, spans = _tab_image(notes, stems=stems)
    got = ocr.read_system(gray, lines, spans)
    assert [sorted(b) for b in got] == [[(2, 3), (3, 1), (4, 2)], [(1, 1), (4, 5)]]


def test_reads_light_gray_digits():
    """浅灰色的数字（有的软件把第二声部画成灰色，压缩、模糊后更浅）也读得到。"""
    notes = [(0, 0, 5, 0), (0, 2, 2, 3), (1, 1, 6, 2), (1, 4, 3, 12)]
    gray, lines, spans = _tab_image(notes, ink=190)
    got = ocr.read_system(gray, lines, spans)
    assert [sorted(b) for b in got] == [[(2, 3), (5, 0)], [(3, 12), (6, 2)]]


def test_disagreements_rules():
    def bar(*notes):
        return {
            "events": [
                {
                    "dur": "1",
                    "notes": [{"string": s, "fret": f, "tech": list(t), "grace": g} for s, f, t, g in notes],
                }
            ]
        }

    bars = [
        bar((5, 0, (), -1), (1, 2, (), -1)),  # 和 OCR 一致
        bar((6, 0, (), -1)),  # AI 把 5 弦读成了 6 弦
        bar((1, 2, ("ah",), -1), (3, 0, ("x",), -1)),  # 人工泛音的 <14> 标注、死音
        bar((2, 3, ("h",), 1)),  # 倚音 1 → 3
        bar((4, 4, (), -1)),  # OCR 什么都没读到：不判断
    ]
    seen = [[(5, 0), (1, 2)], [(5, 0)], [(1, 2), (1, 14)], [(2, 1), (2, 3)], []]
    out = recognize.ocr_disagreements(bars, seen)
    assert list(out) == [1]
    only_ocr, only_ai = out[1]
    assert dict(only_ocr) == {(5, 0): 1} and dict(only_ai) == {(6, 0): 1}
    # 两位数被 OCR 拆成两个数字：算一致；但真的是两个音时（AI 只写了一个 1）照样报
    assert (
        recognize.ocr_disagreements([bar((1, 11, (), -1), (2, 0, (), -1))], [[(1, 1), (1, 1), (2, 0)]]) == {}
    )
    assert list(
        recognize.ocr_disagreements(
            [bar((1, 1, (), -1), (2, 0, (), -1))], [[(1, 1), (1, 1), (2, 0)]], tied_in={}
        )
    ) == [0]
    # 跨小节线的延续音“(2)”：OCR 多读一个和上一小节最后一个音相同的品位，不算；同时 AI 也有对不上的音时照样报
    tie = [bar((1, 2, (), -1), (5, 0, (), -1)), bar((2, 3, (), -1), (4, 0, (), -1))]
    assert recognize.ocr_disagreements(tie, [[(1, 2), (5, 0)], [(1, 2), (2, 3), (4, 0)]]) == {}
    shifted = [bar((5, 0, (), -1), (1, 2, (), -1)), bar((6, 0, (), -1), (1, 3, (), -1))]
    assert list(recognize.ocr_disagreements(shifted, [[(5, 0), (1, 2)], [(5, 0), (1, 3)]])) == [1]
    # 一致率太低：多半是 OCR 读不准这种排版，整行不判断
    assert recognize.ocr_disagreements(bars[:2], [[(3, 3)], [(4, 4)]]) == {}


def test_high_frets_split_by_ocr_still_agree():
    """OCR 把 24 以上的数拆成单个数字（多半是两个挨着的音）；AI 读的 25、30 能由这几位拼出来，不算不一致。"""

    def ev(st, f):
        return {"dur": "1", "notes": [{"string": st, "fret": f, "tech": [], "grace": -1}]}

    bars = [{"events": [ev(1, 25), ev(2, 30), ev(3, 5), ev(4, 0)]}]
    assert recognize.ocr_disagreements(bars, [[(1, 2), (1, 5), (2, 3), (2, 0), (3, 5), (4, 0)]]) == {}


def test_parse_retries_on_ocr_disagreement_and_warns_if_unresolved(data_dir, monkeypatch):
    monkeypatch.setattr(recognize, "OCR_CHECK", True)

    def fake_ocr(gray, lines, spans):
        # 替身模型每个小节写 4 个 1 弦 i 品；OCR 说第 1 行第 2 小节的 1 弦 1 品其实在 2 弦
        return [[(1, i)] * 4 if i != 1 else [(1, 1)] * 3 + [(2, 1)] for i in range(len(spans))]

    monkeypatch.setattr(recognize.ocr, "read_system", fake_ocr)
    sid = songs.create_song([("p1.png", score_png())])
    recognize.run_parse(sid)
    song = store.load(sid)
    retries = [c for c in FakeClaude.calls if c.get("retry")]
    assert {c["first_bar"] for c in retries} == {1, 4}  # 两行的第 2 小节都和 OCR 不一致
    warned = [b["n"] for b in song["bars"] if any(w["code"] == "ocr_mismatch" for w in b["warn"])]
    assert warned == [2, 5]  # 替身模型返修后还是那样：标出来
    msg = next(w["msg"] for w in song["bars"][1]["warn"] if w["code"] == "ocr_mismatch")
    assert msg.startswith("本地数字识别") and "谱上读到 2弦1品" in msg


def test_rows_where_ocr_reads_little_are_not_compared():
    """OCR 只读对了 AI 写的音的不到 60%（斜体数字、很糊的截图）：这一行不比较，免得把读错的 OCR 当成证据。"""
    from collections import Counter

    def bar(*notes):
        return {
            "events": [
                {"dur": "1", "notes": [{"string": s, "fret": f, "tech": [], "grace": -1} for s, f in notes]}
            ]
        }

    ai = [bar((1, 0), (2, 1), (3, 0), (4, 2), (5, 3)) for _ in range(4)]
    # 只在两个小节里读到了数字（读到的大多对得上），其余小节什么都没读到：整行只对上 AI 的 9/20
    ocr = [[(1, 0), (2, 1), (3, 0), (4, 2), (5, 3)], [(1, 0), (2, 1), (3, 0), (4, 2), (5, 8)], [], []]
    assert recognize.ocr_disagreements(ai, ocr) == {}
    good = [[(1, 0), (2, 1), (3, 0), (4, 2), (5, 3)]] * 3 + [[(1, 0), (2, 1), (3, 0), (4, 2), (5, 8)]]
    assert recognize.ocr_disagreements(ai, good) == {3: (Counter({(5, 8): 1}), Counter({(5, 3): 1}))}


def test_chord_sheet_rows_judge_ocr_by_what_it_read():
    """弹唱谱：AI 的音大多是按和弦图换算的 ×（OCR 看不到），不能按“读到了 AI 的几成”判断 OCR 靠不靠得住，
    改看 OCR 读到的数字有几成对得上；写出来的品位读错了照样报。"""

    def bar(*notes):
        return {
            "events": [
                {"dur": "1", "notes": [{"string": s, "fret": f, "tech": [], "grace": -1} for s, f in notes]}
            ]
        }

    ai = [bar((1, 0), (2, 1), (3, 0), (4, 2), (5, 3)), bar((1, 0), (2, 1), (3, 0), (4, 2), (3, 1))]
    ocr = [[(5, 3)], [(4, 2), (3, 0), (3, 0)]]  # 只读到写出来的几个数字：AI 第 2 小节的 3 弦 1 品，谱上是 0
    assert recognize.ocr_disagreements(ai, ocr) == {}  # 普通谱：OCR 只读到 AI 的 3/10，不判断
    out = recognize.ocr_disagreements(ai, ocr, chord_sheet=True)
    # 只有 AI 有的音（× 换算出来的）调用方会忽略；OCR 读到、AI 没写的才算：第 2 小节的 3 弦 0 品
    assert {i: dict(o) for i, (o, _) in out.items() if o} == {1: {(3, 0): 1}}
