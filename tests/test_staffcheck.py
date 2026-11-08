import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app import staffcheck as sc

FONT = "/System/Library/Fonts/Helvetica.ttc"
STD = [64, 59, 55, 50, 45, 40]  # 标准定弦 E4 B3 G3 D3 A2 E2


def _ev(*notes, dur="1"):
    return {"dur": dur, "notes": [{"string": s, "fret": f, "tech": list(t)} for s, f, *t in notes]}


def _row(heads, digits, d=12, tab_sp=18, width=700):
    """画一行五线谱 + 六线谱。heads：[(x, 音级)]（0 = 五线谱最下面一线）；digits：[(x, 弦, 品)]。
    返回 (灰度图, 五线谱线 y, 六线谱线 y)。"""
    img = Image.new("L", (width, 330), 255)
    dr = ImageDraw.Draw(img)
    staff = [60 + i * d for i in range(5)]
    tab = [190 + i * tab_sp for i in range(6)]
    for y in staff + tab:
        dr.line([(20, y), (width - 10, y)], fill=110, width=1)
    for x, step in heads:
        cy = staff[-1] - step * d / 2
        dr.ellipse([x - 0.65 * d, cy - 0.45 * d, x + 0.65 * d, cy + 0.45 * d], fill=0)
        dr.line([(x + 0.6 * d, cy), (x + 0.6 * d, cy - 3 * d)], fill=0, width=1)  # 符干
        for k in range(1, 6):  # 加线
            if step <= -2 * k:
                dr.line([(x - d, staff[-1] + k * d), (x + d, staff[-1] + k * d)], fill=0, width=1)
            if step >= 8 + 2 * k:
                dr.line([(x - d, staff[0] - k * d), (x + d, staff[0] - k * d)], fill=0, width=1)
    try:
        font = ImageFont.truetype(FONT, int(tab_sp * 0.85))
    except OSError:
        font = ImageFont.load_default(size=int(tab_sp * 0.85))
    for x, string, fret in digits:
        y = tab[string - 1]
        box = dr.textbbox((x, y), str(fret), font=font, anchor="mm")
        dr.rectangle([box[0] - 2, box[1], box[2] + 2, box[3]], fill=255)
        dr.text((x, y), str(fret), font=font, fill=0, anchor="mm")
    return np.asarray(img), staff, tab


def test_head_pitch_names():
    # 高音谱号最下面一线是 E4；记谱比实际高八度
    assert sc.Head(0, 0, 0, 1, 1).natural_midi() == 64
    assert sc.Head(0, 0, 1, 1, 1).natural_midi() == 65  # F4
    assert sc.Head(0, 0, -2, 1, 1).natural_midi() == 60  # C4，下加一线
    assert sc.Head(0, 0, 8, 1, 1).natural_midi() == 77  # F5，最上面一线


def test_find_staff_and_heads_on_a_drawn_row():
    # 1 弦 0 品 = E4，记谱 E5（第 4 间，音级 7）；6 弦 3 品 = G2，记谱 G3（下加三线下面，音级 -5）
    g, staff, tab = _row([(200, 7), (400, -5)], [(200, 1, 0), (400, 6, 3)])
    found = sc.find_staff(g, tab, 20, 690)
    assert found is not None and abs(found[0] - staff[0]) <= 1 and abs(found[-1] - staff[-1]) <= 1
    heads = sc.find_heads(g, found, 20, 690, tab[0] - 20)
    assert [(round(h.x / 10) * 10, h.step) for h in heads] == [(200, 7), (400, -5)]


def test_no_staff_above_a_plain_tab():
    g, _, tab = _row([], [(200, 1, 0)])
    g = g.copy()
    g[:170] = 255  # 擦掉五线谱
    assert sc.find_staff(g, tab, 20, 690) is None


def test_tab_columns_group_digits_by_x():
    g, _, tab = _row([], [(150, 1, 0), (150, 2, 1), (300, 6, 3), (450, 3, 12)])
    cols = sc.tab_columns(g, tab, 60, 690)
    assert [(round(c.x / 10) * 10, sorted(c.strings), c.paren) for c in cols] == [
        (150, [1, 2], False),
        (300, [6], False),
        (450, [3], False),
    ]


def test_note_pitches_harmonics_allow_the_sounding_pitch():
    assert sc.note_pitches({"string": 1, "fret": 3, "tech": []}, STD, 0) == {67}
    assert sc.note_pitches({"string": 1, "fret": 3, "tech": []}, STD, 2) == {69}  # 变调夹
    # 自然泛音 7 品：比空弦高 19 个半音；五线谱可能写泛音音高，也可能写按的品
    assert sc.note_pitches({"string": 5, "fret": 7, "tech": ["nh"]}, STD, 0) == {52, 64}


def _cols(*spec):
    return [sc.Column(x, frozenset(s), False) for x, s in spec]


def test_check_bar_all_agree():
    cols = _cols((100, {1}), (200, {6}))
    heads = [sc.Head(100, 0, 7, 10, 8), sc.Head(200, 0, -5, 10, 8)]  # E5、G3（记谱）
    r = sc.check_bar(cols, heads, [_ev((1, 0)), _ev((6, 3))], STD, 0, tol=15, sp=18)
    assert r["string_miss"] == 0 and r["staff_miss"] == 0 and not r["extra_cols"] and not r["extra_onsets"]


def test_check_bar_wrong_string_and_wrong_fret():
    cols = _cols((100, {1}), (200, {6}))
    heads = [sc.Head(100, 0, 7, 10, 8), sc.Head(200, 0, -5, 10, 8)]
    # 第一个音写到 2 弦（弦对不上）；第二个音写成 6 弦 8 品 = C3（弦对得上，音高和五线谱的 G 对不上）
    r = sc.check_bar(cols, heads, [_ev((2, 0)), _ev((6, 8))], STD, 0, tol=15, sp=18)
    assert r["string_miss"] == 2  # 第一列：图上 1 弦、AI 2 弦
    assert r["staff_miss"] == 2


def test_check_bar_missing_onset_is_an_extra_column():
    cols = _cols((100, {1}), (200, {6}), (300, {6}))
    r = sc.check_bar(cols, [], [_ev((1, 0)), _ev((6, 3))], STD, 0, tol=0, sp=18)
    assert len(r["extra_cols"]) == 1 and r["string_miss"] == 0  # 哪一列多出来说不清（两列一样），只看个数


def test_check_bar_grace_column_next_to_its_note_is_free():
    # 滑音的起点 / 倚音：挨着下一列、弦是它的一部分，AI 写进同一个起拍
    cols = _cols((100, {2}), (120, {2, 3}))
    r = sc.check_bar(cols, [], [_ev((2, 3), (3, 2))], STD, 0, tol=0, sp=18)
    assert not r["extra_cols"] and r["string_miss"] == 0


def test_check_bar_tied_head_from_an_earlier_onset_is_not_a_miss():
    # 另一个声部用连线延续下来的音：五线谱上又画了一个符头（E5），六线谱上只写新弹的 6 弦
    cols = _cols((100, {1}), (200, {6}))
    heads = [sc.Head(100, 0, 7, 10, 8), sc.Head(200, 0, 7, 10, 8), sc.Head(200, 0, -5, 10, 8)]
    r = sc.check_bar(cols, heads, [_ev((1, 0)), _ev((6, 3))], STD, 0, tol=15, sp=18)
    assert r["staff_miss"] == 0


def test_parenthesized_columns_do_not_take_part():
    cols = [sc.Column(100, frozenset({1}), False), sc.Column(200, frozenset({6}), True)]
    r = sc.check_bar(cols, [], [_ev((1, 0))], STD, 0, tol=0, sp=18)
    assert not r["extra_cols"] and r["cols"] == 1
