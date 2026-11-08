import pytest
from PIL import Image, ImageDraw, ImageFont

from app.layout import detect_page


def _score(missing_line: bool = False) -> Image.Image:
    """合成一页：两行（五线谱 + 六线谱），每行 3 个小节。"""
    img = Image.new("L", (1200, 900), 255)
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        for i in range(5):  # 五线谱
            y = top + i * 10
            d.line([(60, y), (1140, y)], fill=170)
        tab_top = top + 110
        for i in range(6):  # 六线谱
            y = tab_top + i * 14
            if missing_line and top == 520 and i == 2:
                d.line([(60, y), (300, y)], fill=170)  # 被数字挡掉大半
                continue
            d.line([(60, y), (1140, y)], fill=170)
        for x in (60, 420, 780, 1140):  # 小节线
            d.line([(x, tab_top), (x, tab_top + 70)], fill=40, width=2)
        for x in range(100, 1100, 37):  # 品格数字（不能被当成小节线）
            d.text((x, tab_top + 14 * (x % 6) - 6), str(x % 13), fill=0)
    return img


def test_detects_systems_and_bars():
    r = detect_page(_score())
    assert len(r["systems"]) == 2
    for s in r["systems"]:
        assert len(s["bars"]) == 3
        assert abs(s["bars"][0][0] - 60) <= 3 and abs(s["bars"][-1][1] - 1140) <= 3
        assert abs(s["bars"][1][0] - 420) <= 3


def test_recovers_tab_line_hidden_by_numbers():
    r = detect_page(_score(missing_line=True))
    assert len(r["systems"]) == 2


def test_blank_page_has_no_systems():
    assert detect_page(Image.new("L", (800, 1000), 255))["systems"] == []


def _draw_system(d, top, x0, x1, barlines):
    for i in range(5):
        d.line([(x0, top + i * 10), (x1, top + i * 10)], fill=170)
    tab_top = top + 110
    for i in range(6):
        d.line([(x0, tab_top + i * 14), (x1, tab_top + i * 14)], fill=170)
    for x in barlines:
        d.line([(x, tab_top), (x, tab_top + 70)], fill=40, width=2)


def test_short_last_line_is_kept():
    """最后一行没占满页宽（MuseScore 常见）也要找出来。"""
    img = Image.new("L", (1200, 900), 255)
    d = ImageDraw.Draw(img)
    _draw_system(d, 120, 60, 1140, (60, 600, 1140))
    _draw_system(d, 520, 60, 500, (60, 280, 500))  # 只占 37% 页宽
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2, 2]


def test_two_columns_split_and_read_left_column_first():
    img = Image.new("L", (1400, 900), 255)
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        _draw_system(d, top, 60, 640, (60, 350, 640))
        _draw_system(d, top, 760, 1340, (760, 1050, 1340))
    r = detect_page(img)
    systems = r["systems"]
    assert len(systems) == 4 and all(len(s["bars"]) == 2 for s in systems)
    assert [s["x0"] < 700 for s in systems] == [True, True, False, False]  # 先左栏再右栏
    assert systems[0]["top"] < systems[1]["top"]
    assert all(b[1] - b[0] < 400 for s in systems for b in s["bars"])  # 栏间空白没有变成假小节


def test_dashed_let_ring_line_above_tab_is_not_a_seventh_string():
    # GP / alphaTab 导出的谱子：六线谱上方一线距处有一条 “let ring - - -” 虚线，覆盖率和谱线差不多
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        y = top + 110 - 14
        for x in range(70, 1130, 12):
            d.line([(x, y), (x + 9, y)], fill=120)
    r = detect_page(img)
    assert len(r["systems"]) == 2
    assert all(len(s["bars"]) == 3 for s in r["systems"])


def _draw_tab(d, tab_top, x0, x1, barlines, sp=14):
    for i in range(6):
        d.line([(x0, tab_top + i * sp), (x1, tab_top + i * sp)], fill=170)
    for x in barlines:
        d.line([(x, tab_top), (x, tab_top + 5 * sp)], fill=40, width=2)


def test_short_screenshot_with_one_tab_row():
    """只截了一行六线谱的短图（页高很小）：线距相对页高很大，也要能组成谱表。"""
    img = Image.new("L", (1200, 160), 255)
    _draw_tab(ImageDraw.Draw(img), 40, 40, 1160, (40, 600, 1160))
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2]


def test_very_short_last_row_is_kept():
    """最后一行只有一个全音符小节，占不到页宽的十分之一。"""
    img = Image.new("L", (1800, 900), 255)
    d = ImageDraw.Draw(img)
    _draw_system(d, 120, 60, 1740, (60, 900, 1740))
    _draw_system(d, 520, 60, 220, (60, 220))  # 160 像素 ≈ 11 个线距、页宽的 9%
    d.text((120, 520 + 110 + 14 * 2 - 6), "0", fill=0)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2, 1]


def test_strum_arrows_are_not_barlines():
    """扫弦箭头几乎贯穿六线谱：尾巴到不了 6 弦，或者箭头、尾巴伸出谱线外半个线距。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        t = top + 110
        for x, y0, y1 in ((150, t + 4, t + 64), (500, t - 8, t + 78), (860, t, t + 78)):
            d.line([(x, y0), (x, y1)], fill=0, width=2)
            d.polygon([(x - 5, y0 + 10), (x + 6, y0 + 10), (x + 1, y0)], fill=0)  # 箭头
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]


def test_dense_strum_arrows_inside_the_staff_are_not_barlines():
    """弹唱谱的扫弦行：一小节 8 个箭头，从 6 弦贯穿到 1 弦、箭头停在谱线以内（伸出去不到 0.3 个线距）。
    以前被当成小节线，一行里“小节线”太密又被当成一排和弦图，整行丢掉。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        t = top + 110
        for k in range(8 * 3):
            x = 75 + k * 45
            if any(abs(x - b) < 8 for b in (60, 420, 780, 1140)):
                continue
            d.line([(x, t), (x, t + 70)], fill=0, width=1)
            if k % 2:  # 向下的箭头：箭头在 6 弦一端
                d.polygon([(x - 5, t + 58), (x + 5, t + 58), (x, t + 70)], fill=0)
            else:  # 向上的箭头：箭头在 1 弦一端
                d.polygon([(x - 5, t + 12), (x + 5, t + 12), (x, t)], fill=0)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]


def test_dashed_line_further_above_does_not_eat_string_one():
    """let ring 虚线离 1 弦 1.6–2 个线距：不能和 1 弦配成一组、把六线谱拆成 5 根。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        y = top + 110 - 26
        for x in range(70, 1130, 12):
            d.line([(x, y), (x + 9, y)], fill=120)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]
    assert all(abs(s["lines"][0] - (t + 110)) <= 2 for s, t in zip(r["systems"], (120, 520)))


def test_page_with_only_a_short_row():
    """切页后最后一页只剩一个短小节：整页没有占满页宽的线，也要找到。"""
    img = Image.new("L", (1800, 360), 255)
    d = ImageDraw.Draw(img)
    _draw_system(d, 60, 60, 300, (60, 300))
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [1]


def test_chord_diagrams_are_not_a_tab_row():
    """一排和弦图：格子也是 6 根等距横线，竖线一个线距左右一根。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for k in range(8):  # 谱子上方一排和弦图，每个 5 格宽、6 根横线
        x = 80 + k * 130
        for i in range(6):
            d.line([(x, 20 + i * 14), (x + 75, 20 + i * 14)], fill=40)
        for j in range(6):
            d.line([(x + j * 15, 20), (x + j * 15, 90)], fill=40)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]


def test_staff_with_ledger_lines_is_not_a_tab():
    """五线谱下面一排加线（连在一起的低音）和五线谱凑成 6 根等距线，符干又贯穿它们：不能当成六线谱。
    靠和这页其他谱表比线距来认（Greensleeves 里只有一行这样），所以这里只有第二行有加线。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (520,):
        y = top + 50  # 五线谱线距 10，第 6 根位置
        for x in range(80, 1120, 24):
            d.line([(x, y), (x + 14, y)], fill=60)  # 加线
            d.line([(x + 12, top - 20), (x + 12, y + 4)], fill=0, width=2)  # 符干
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]


def test_beam_rows_above_do_not_eat_string_one():
    """纯六线谱（不带五线谱）：上一行的符杠横梁又粗又长也像谱线，两条横梁和 1 弦间距差不多时会凑成一组，
    不能因此把 1 弦丢掉、只剩 5 根线。"""
    img = Image.new("L", (1200, 700), 255)
    d = ImageDraw.Draw(img)
    for tab_top in (150, 450):
        _draw_tab(d, tab_top, 60, 1140, (60, 600, 1140))
        for y in (tab_top - 60, tab_top - 30):  # 两条横梁，间距和到 1 弦的距离一样
            d.rectangle([(70, y), (1130, y + 2)], fill=0)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2, 2]
    assert all(abs(s["lines"][0] - t) <= 2 for s, t in zip(r["systems"], (150, 450)))


def test_barline_touched_by_a_slur_is_still_a_barline():
    """小节线顶上被连线 / 弧线碰到，伸出谱线一点点（0.2 个线距）：还是小节线（扫弦箭头伸出 0.4 以上）。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        t = top + 110
        d.line([(420, t - 5), (421, t)], fill=0, width=2)  # 连线的端点接在 420 处小节线顶上，伸出 4 像素
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]


@pytest.mark.parametrize("size", [(1, 1), (5, 400), (400, 3)])
def test_tiny_images_have_no_systems(size):
    assert detect_page(Image.new("RGB", size, "white"))["systems"] == []


def test_first_row_crop_includes_chord_diagrams_above_a_tab_only_row():
    """弹唱谱：没有五线谱，每行上方是和弦名和和弦图。第一行也要把它们裁进来（否则模型看不到和弦，只能瞎猜）；
    再往上隔着一大段空白的曲名不要。"""
    img = Image.new("L", (1200, 700), 255)
    d = ImageDraw.Draw(img)
    d.text((500, 40), "TITLE", fill=0)  # 曲名：离谱面很远
    tab_top = 300
    for i in range(6):
        d.line([(60, tab_top + i * 14), (1140, tab_top + i * 14)], fill=170)
    for x in (60, 600, 1140):
        d.line([(x, tab_top), (x, tab_top + 70)], fill=40, width=2)
    for k in range(4):  # 和弦图：名字 + 小格子，底边离 1 弦 25 像素，顶上的名字在 1 弦上方约 110 像素
        x = 100 + k * 250
        d.text((x + 10, tab_top - 112), "Em", fill=0)
        for i in range(6):
            d.line([(x, tab_top - 90 + i * 12), (x + 50, tab_top - 90 + i * 12)], fill=0)
    r = detect_page(img)
    assert len(r["systems"]) == 1
    top = r["systems"][0]["top"]
    assert tab_top - 125 <= top <= tab_top - 110 and top > 60


def test_clef_before_a_start_repeat_is_not_a_bar():
    """行首先是 TAB 谱号，再是反复开始记号 ‖:：谱号那一小段不是小节。"""
    img = Image.new("L", (1200, 400), 255)
    d = ImageDraw.Draw(img)
    tab_top = 150
    for i in range(6):
        d.line([(60, tab_top + i * 14), (1140, tab_top + i * 14)], fill=170)
    d.text((70, tab_top + 20), "TAB", fill=0)
    for x in (120, 126):  # 反复开始记号：粗细两条线（谱号后面约 4.5 个线距）
        d.line([(x, tab_top), (x, tab_top + 70)], fill=40, width=3 if x == 120 else 1)
    for x in (630, 1140):
        d.line([(x, tab_top), (x, tab_top + 70)], fill=40, width=2)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2]
    assert r["systems"][0]["bars"][0][0] >= 115


def test_rows_far_apart_keep_their_lyrics_below_and_chords_above():
    """弹唱谱：每行下面有简谱和歌词（离六线谱 10 个线距左右），下一行上面有和弦图。分界不能取中点
    （会把歌词拦腰切掉）：下一行只要它上方 9 个线距，其余归上一行；最后一行也要把下面的歌词带上。"""
    img = Image.new("L", (1200, 1000), 255)
    d = ImageDraw.Draw(img)
    tops = (150, 520)
    for t in tops:
        for i in range(6):
            d.line([(60, t + i * 14), (1140, t + i * 14)], fill=170)
        for x in (60, 600, 1140):
            d.line([(x, t), (x, t + 70)], fill=40, width=2)
        for x in range(80, 1120, 60):  # 符干（六线谱下面 0–30 像素）
            d.line([(x, t + 70), (x, t + 100)], fill=40)
        d.text((100, t + 70 + 60), "5 6 1 1 2 3 3", fill=0)  # 简谱：下面 60 像素
        d.text((100, t + 70 + 100), "LYRICS", fill=0)  # 歌词：六线谱下面 100 像素
        for k in range(3):  # 和弦图：六线谱上面 25–110 像素
            for i in range(6):
                d.line([(100 + k * 300, t - 90 + i * 12), (150 + k * 300, t - 90 + i * 12)], fill=0)
    r = detect_page(img)
    (a, b) = r["systems"]
    lyric_bottom = 150 + 70 + 100 + 12
    assert a["bottom"] >= lyric_bottom and b["top"] >= lyric_bottom  # 第一行的歌词完整地在第一行里
    assert b["top"] <= 520 - 95  # 第二行的和弦图在第二行里
    assert b["bottom"] >= 520 + 70 + 100 + 12  # 最后一行：下面的歌词也带上


def test_chord_diagrams_with_finger_dots_are_not_a_tab_row():
    """和弦图上按弦的圆点让竖线（弦）看着像扫弦箭头：两三个挨着的和弦图仍然不能当成一行六线谱。"""
    img = Image.new("L", (1300, 700), 255)
    d = ImageDraw.Draw(img)
    tab_top = 400
    for i in range(6):
        d.line([(60, tab_top + i * 15), (1240, tab_top + i * 15)], fill=170)
    for x in (60, 650, 1240):
        d.line([(x, tab_top), (x, tab_top + 75)], fill=40, width=2)
    for x0 in (120, 190, 420, 700, 770):  # 和弦图：6 根弦（间距 11）、6 根品线（间距 14）、几个圆点
        for i in range(6):
            d.line([(x0, 250 + i * 14), (x0 + 55, 250 + i * 14)], fill=0)
            d.line([(x0 + i * 11, 250), (x0 + i * 11, 320)], fill=0)
        for i, fr in ((1, 1), (3, 2), (4, 3)):
            cx, cy = x0 + i * 11, 250 + fr * 14 - 7
            d.ellipse([(cx - 4, cy - 4), (cx + 4, cy + 4)], fill=0)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2]


def test_gap_before_a_coda_section_is_not_a_bar():
    """D.S. 之后同一行另起一段 Coda：两段谱线之间断开 3 个线距左右，两头各有一条线，中间不是小节。"""
    img = Image.new("L", (1200, 400), 255)
    d = ImageDraw.Draw(img)
    tab_top = 150
    for x0, x1 in ((60, 640), (690, 1140)):  # 断开 50 像素（3.6 个线距）
        for i in range(6):
            d.line([(x0, tab_top + i * 14), (x1, tab_top + i * 14)], fill=170)
        for x in (x0, (x0 + x1) // 2, x1):
            d.line([(x, tab_top), (x, tab_top + 70)], fill=40, width=2)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [4]


def test_split_just_above_the_next_rows_chord_diagrams():
    """两行挨得较近（呆头鸟这类排版）：上一行的歌词过了中点，下一行的和弦图只占它上方 5 个线距。
    分界取在和弦图上方，歌词归上一行。"""
    img = Image.new("L", (1200, 600), 255)
    d = ImageDraw.Draw(img)
    for t in (120, 350):
        for i in range(6):
            d.line([(60, t + i * 12), (1140, t + i * 12)], fill=170)
        for x in (60, 600, 1140):
            d.line([(x, t), (x, t + 60)], fill=40, width=2)
        for x in range(80, 1120, 60):  # 符干
            d.line([(x, t + 60), (x, t + 85)], fill=40)
        d.text((100, t + 60 + 50), "5 6 1 1 2", fill=0)  # 简谱
        d.text((100, t + 60 + 78), "LYRIC", fill=0)  # 歌词：过了两行之间的中点
        for k in range(3):  # 和弦图：谱线上方 12–50 像素，名字在 65 像素处
            x0 = 120 + k * 300
            d.text((x0 + 10, t - 65), "Em", fill=0)
            for i in range(5):
                d.line([(x0, t - 50 + i * 9), (x0 + 50, t - 50 + i * 9)], fill=0)
            for i in range(6):
                d.line([(x0 + i * 10, t - 50), (x0 + i * 10, t - 14)], fill=0)
    (a, b) = detect_page(img)["systems"]
    assert a["bottom"] >= 120 + 60 + 78 + 11  # 第一行的歌词完整
    assert b["top"] <= 350 - 65  # 第二行的和弦名、和弦图都在第二行里


def test_clef_and_time_signature_before_a_heavy_double_bar_are_not_a_bar():
    """行首“TAB 4/4”后面是加粗双线（‖、‖:）：这一段 7–10 个线距宽，也不是小节（弱起小节后面不会是加粗双线）。"""
    img = Image.new("L", (1200, 400), 255)
    d = ImageDraw.Draw(img)
    tab_top = 150
    for i in range(6):
        d.line([(60, tab_top + i * 14), (1140, tab_top + i * 14)], fill=170)
    d.line([(60, tab_top), (60, tab_top + 70)], fill=40, width=2)
    d.text((70, tab_top + 20), "TAB 4/4", fill=0)
    d.line([(170, tab_top), (170, tab_top + 70)], fill=0, width=5)  # 粗线
    d.line([(178, tab_top), (178, tab_top + 70)], fill=0, width=1)  # 细线（谱号后约 8 个线距）
    for x in (660, 1140):
        d.line([(x, tab_top), (x, tab_top + 70)], fill=40, width=2)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2]


def test_upper_rows_own_stems_and_beams_stay_with_it():
    """上一行六线谱下面挂着长符干和横梁（2.7 个线距），离下一行只隔一小段空白：这一块和上一行连着，
    不是下一行的和弦图，分界不能取在上一行谱线下面 1 个线距（横梁会全跑到下一行里）。"""
    sp = 12
    img = Image.new("L", (1200, 600), 255)
    d = ImageDraw.Draw(img)
    t1, stem = 120, 32
    t2 = t1 + 60 + stem + 30
    for t in (t1, t2):
        for i in range(6):
            d.line([(60, t + i * sp), (1140, t + i * sp)], fill=170)
        for x in (60, 600, 1140):
            d.line([(x, t), (x, t + 60)], fill=40, width=2)
        for x in range(80, 1120, 40):  # 符干
            d.line([(x, t + 60), (x, t + 60 + stem)], fill=0)
        for x0 in range(80, 1120, 80):  # 横梁
            d.line([(x0, t + 60 + stem), (x0 + 40, t + 60 + stem)], fill=0, width=3)
        for x in range(90, 1120, 40):
            d.text((x, t + 8), "5", fill=0)
    (a, b) = detect_page(img)["systems"]
    beam_bottom = t1 + 60 + stem + 2
    assert a["bottom"] >= beam_bottom and b["top"] >= beam_bottom  # 横梁完整地在第一行里
    assert b["top"] <= t2 - 3


def test_narrow_first_bar_after_a_single_barline_is_kept():
    """行首第一小节很窄（只有一个全音符和弦，5 个线距）、后面是普通的单小节线：是小节，不能当成谱号那一段丢掉。"""
    sp = 14
    img = Image.new("L", (1200, 400), 255)
    d = ImageDraw.Draw(img)
    tab_top = 150
    for i in range(6):
        d.line([(60, tab_top + i * sp), (1140, tab_top + i * sp)], fill=170)
    for x in (60, 130, 600, 1140):
        d.line([(x, tab_top), (x, tab_top + 70)], fill=40, width=2)
    d.text((95, tab_top + 10), "3", fill=0)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3]
    assert abs(r["systems"][0]["bars"][0][0] - 60) <= 3


def test_barlines_found_when_lines_have_a_faint_row_above():
    """缩小过的截图：线距小（9 像素），每根谱线上面挨着一行浅灰的抗锯齿（不到 215、不到 200），线心落在半像素上。
    小节线停在深色的那一行：判断两端是否连到 1 弦 / 6 弦时不能把谱线外面那行浅灰也算进去。"""
    sp = 9
    img = Image.new("L", (900, 200), 255)
    d = ImageDraw.Draw(img)
    tab_top = 80
    for i in range(6):
        y = tab_top + i * sp
        d.line([(40, y - 1), (860, y - 1)], fill=210)
        d.line([(40, y), (860, y)], fill=120)
    for x in (40, 300, 580, 860):
        d.line([(x, tab_top), (x, tab_top + 5 * sp)], fill=40, width=2)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3]


@pytest.mark.parametrize("top", [0, 1])
def test_staff_line_on_the_first_or_last_pixel_row(top):
    """裁得很紧的截图：1 弦就在图片第一行（或 6 弦在最后一行）也不能崩。"""
    sp = 14
    img = Image.new("L", (900, top + 5 * sp + 1), 255)
    d = ImageDraw.Draw(img)
    for i in range(6):
        d.line([(10, top + i * sp), (890, top + i * sp)], fill=120)
    for x in (10, 450, 890):
        d.line([(x, top), (x, top + 5 * sp)], fill=40, width=2)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2]


def test_thick_lines_of_an_upscaled_screenshot():
    """Retina 截图（2 倍放大、不模糊）：谱线有 3 像素粗、和相邻行合并后 4–5 行，页面又不高；
    谱线粗细的上限要按线距算，不能按页高算。"""
    sp = 28
    img = Image.new("L", (1600, 600), 255)
    d = ImageDraw.Draw(img)
    for top in (120, 380):
        for i in range(6):
            y = top + i * sp
            d.rectangle([(40, y), (1560, y + 3)], fill=150)
        for x in (40, 800, 1560):
            d.rectangle([(x, top), (x + 3, top + 5 * sp + 3)], fill=40)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2, 2]


def test_one_row_split_by_a_coda_gap_does_not_make_the_page_two_columns():
    """只有一行在 Coda 前隔开一大段（≥4 个线距）：整页还是单栏，缩进的首行仍排在最前面。"""
    img = Image.new("L", (1200, 1000), 255)
    d = ImageDraw.Draw(img)
    _draw_tab(d, 150, 200, 1140, (200, 670, 1140))  # 首行缩进
    _draw_tab(d, 400, 60, 600, (60, 330, 600))
    _draw_tab(d, 400, 700, 1140, (700, 920, 1140))  # 同一行的 Coda，隔开 100 像素
    _draw_tab(d, 650, 60, 1140, (60, 600, 1140))
    r = detect_page(img)
    assert [(s["x0"], round(s["lines"][0])) for s in r["systems"]] == [
        (200, 150),
        (60, 400),
        (700, 400),
        (60, 650),
    ]
    # 同一行的两段各自的纵向范围都包住整行谱线，和上下两行不重叠
    for s in r["systems"]:
        assert s["top"] < s["lines"][0] and s["lines"][-1] < s["bottom"]
    a, b = r["systems"][1], r["systems"][2]
    assert (a["top"], a["bottom"]) == (b["top"], b["bottom"])
    assert r["systems"][0]["bottom"] <= a["top"] and a["bottom"] <= r["systems"][3]["top"]


@pytest.mark.parametrize("offset", [0, 3, 40])
def test_two_columns_slightly_offset_vertically(offset):
    """双栏排版、右栏比左栏低几个像素到半行：两栏都要找到，先左栏再右栏。"""
    img = Image.new("L", (1400, 1000), 255)
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        _draw_system(d, top, 60, 640, (60, 350, 640))
        _draw_system(d, top + offset, 760, 1340, (760, 1050, 1340))
    r = detect_page(img)
    systems = r["systems"]
    assert [s["x0"] for s in systems] == [60, 60, 760, 760]
    assert [len(s["bars"]) for s in systems] == [2, 2, 2, 2]
    assert systems[0]["top"] < systems[1]["top"] and systems[2]["top"] < systems[3]["top"]


def _digit(d, x, y, s):
    """品格数字：中心在 (x, y)（骑在弦线上），字号和真谱子里的数字差不多（约 0.85 个线距高）。"""
    d.text((x, y), s, fill=0, font=ImageFont.load_default(size=17), anchor="mm")


def test_chord_stem_through_the_whole_staff_is_not_a_barline():
    """1 弦和 6 弦上各有一个音的和弦，符干从 1 弦的数字一直连到 6 弦的数字、再往下接横梁：贯穿整个六线谱，
    伸出谱线的部分被数字挡住（不到 0.3 个线距），以前被当成小节线（雨后初晴第 2 行）。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        t = top + 110
        _digit(d, 600, t, "7")
        _digit(d, 600, t + 70, "0")
        d.line([(600, t), (600, t + 69)], fill=0)  # 从 7 中间穿出来，穿过 0 的上半圈（0 中间是空的）
        d.line([(600, t + 78), (600, t + 100)], fill=0)  # 隔着 0 再往下到横梁
        d.line([(600, t + 100), (640, t + 100)], fill=0, width=3)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]


def test_stem_from_a_digit_on_string_one_down_past_the_staff_is_not_a_barline():
    """符干从 1 弦上的数字出发，贯穿六线谱、伸出 6 弦将近 3 个线距去接横梁（放大 1.5 倍的 Imagine）：
    伸出去太长、超出扫弦箭头那条规则的范围，但它的上端扎在数字里，不是小节线。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        t = top + 110
        _digit(d, 600, t, "3")
        d.line([(600, t), (600, t + 70 + 40)], fill=0)
        d.line([(600, t + 110), (640, t + 110)], fill=0, width=3)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]


def test_barlines_with_a_grey_edge_survive_a_2x_nearest_upscale():
    """2 倍最近邻放大的截图：小节线一侧的灰边（抗锯齿）变成 2 像素宽，按“比竖线宽 2 像素”算会被当成扫弦箭头，
    小节线整条丢掉。反复记号的两个点贴在细线一侧，也不是箭头。"""
    img = Image.new("L", (600, 260), 255)
    d = ImageDraw.Draw(img)
    t = 80
    for i in range(6):
        d.line([(30, t + i * 12), (570, t + i * 12)], fill=170)
    for x in (30, 160, 570):
        d.line([(x, t), (x, t + 60)], fill=0)
        for y in range(t + 2, t + 58, 6):  # 断断续续的灰边，只在一侧、一次两行
            d.line([(x + 1, y), (x + 1, y + 1)], fill=150)
    d.rectangle([(300, t), (302, t + 60)], fill=0)  # 反复开始记号：粗线 + 细线 + 两个点
    d.line([(305, t), (305, t + 60)], fill=0)
    for y in (t + 18, t + 42):
        d.ellipse([(306, y - 2), (310, y + 2)], fill=0)
    for x in range(60, 560, 37):
        if all(abs(x - b) > 12 for b in (160, 300)):
            _digit(d, x, t + 12 * (x % 6), str(x % 10))
    big = img.resize((img.width * 2, img.height * 2), Image.NEAREST)
    assert [len(s["bars"]) for s in detect_page(img)["systems"]] == [3]
    assert [len(s["bars"]) for s in detect_page(big)["systems"]] == [3]


def test_truncated_tab_under_chord_diagrams_is_not_a_row():
    """截图底边截断的六线谱只露出 4 根线，上方一排和弦图的格子横线正好和它等距：不能凑成一个“六线谱”行
    （C 50 放大 1.5 倍时凑出过一个只有一个小节的假行）。"""
    img = Image.new("L", (1200, 290), 255)
    d = ImageDraw.Draw(img)
    t = 220  # 1 弦；4 根线之后就到图片底边了
    for i in range(4):
        d.line([(40, t + i * 20), (1160, t + i * 20)], fill=120)
    d.line([(40, t), (40, 289)], fill=0, width=2)
    for k in range(3):  # 和弦图：6 根横线、6 根竖线，最下面一根横线在 1 弦上方一个线距
        x = 200 + k * 400
        for i in range(6):
            d.line([(x, t - 20 - i * 20), (x + 70, t - 20 - i * 20)], fill=0)
            d.line([(x + i * 14, t - 120), (x + i * 14, t - 20)], fill=0)
    assert detect_page(img)["systems"] == []


def _score_tab_system(d, top, x0=60, x1=1140, sp=14):
    """五线谱（线距 10）+ 下方 110 像素处的六线谱，小节线在两端和正中。"""
    for i in range(5):
        d.line([(x0, top + i * 10), (x1, top + i * 10)], fill=120)
    _draw_tab(d, top + 110, x0, x1, (x0, (x0 + x1) // 2, x1), sp)


def test_split_keeps_the_next_rows_high_notes_and_beams():
    """五线谱 + 六线谱：下一行五线谱上方的高音（加线上的符头）、向上的长符干和横梁伸得很高，上一行六线谱下面却什么都没有。
    取中点会把下一行的符干、横梁切掉一截（模型说“节奏顶部被裁切”），分界要挪到上面那段空白里。"""
    img = Image.new("L", (1200, 900), 255)
    d = ImageDraw.Draw(img)
    _score_tab_system(d, 120)
    _score_tab_system(d, 520)
    beam = 392  # 下一行的横梁：比两行谱线的中点（410）还高
    for x in range(200, 1100, 60):
        d.line([(x, 520), (x, beam)], fill=0)
        d.ellipse([(x - 8, 430), (x, 437)], fill=0)  # 加线上的符头
        d.line([(x - 12, 434), (x + 4, 434)], fill=0)
    for x in range(200, 1100, 120):
        d.rectangle([(x, beam), (x + 60, beam + 3)], fill=0)
    a, b = detect_page(img)["systems"]
    assert a["bottom"] == b["top"] and 300 < b["top"] <= beam


def test_split_below_the_upper_rows_lyrics():
    """弹唱谱：上一行六线谱下面是简谱和一行歌词，下一行上方只有和弦名（没有和弦图）。中点正好落在歌词上，
    分界要挪到歌词和下一行和弦名之间那段（更宽的）空白里，歌词留在上一行。"""
    img = Image.new("L", (1200, 600), 255)
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default(size=13)
    _draw_tab(d, 150, 60, 1140, (60, 600, 1140))
    _draw_tab(d, 320, 60, 1140, (60, 600, 1140))
    for x in range(100, 1100, 50):
        d.text((x, 248), "5", fill=0, font=f)  # 简谱：255–264 行
        d.text((x, 264), "ly", fill=0, font=f)  # 歌词：中点 270 落在这一行上
        if x % 200 == 100:
            d.text((x, 290), "Am", fill=0, font=f)  # 下一行的和弦名
    lyrics_bottom, chord_top = (
        d.textbbox((100, 264), "ly", font=f)[3],
        d.textbbox((100, 290), "Am", font=f)[1],
    )
    a, b = detect_page(img)["systems"]
    assert a["bottom"] == b["top"] and lyrics_bottom <= b["top"] <= chord_top


def test_half_width_last_row_with_dense_notes_keeps_its_staff():
    """最后一行只有半页宽，五线谱里挤满了音符（线和线之间的行也有一成多是黑的）：低门槛找谱线时五根线会连成一片，
    这一行的五线谱找不到，分界线从五线谱中间穿过（罗曼史第 2 页最后一行）。"""
    img = Image.new("L", (1200, 1100), 255)
    d = ImageDraw.Draw(img)
    _score_tab_system(d, 100, sp=20)
    _score_tab_system(d, 470, sp=20)
    _score_tab_system(d, 760, 60, 640, sp=20)  # 两行谱线的中点（775）落在这一行的五线谱上
    for x in range(62, 640, 4):  # 一排排挤在一起的符干、符头
        d.line([(x, 762), (x, 798)], fill=0)
    systems = detect_page(img)["systems"]
    assert len(systems) == 3 and systems[1]["bottom"] == systems[2]["top"] <= 760 - 2


def test_staff_far_above_its_tab_still_belongs_to_the_row():
    """五线谱下面挂着低音的加线、六线谱上方又有两排标记时，五线谱和六线谱能隔开 12 个线距以上：
    还是同一行的五线谱，不能被裁进上一行（Mother 第 7 页最后一行）。"""
    img = Image.new("L", (1200, 1100), 255)
    d = ImageDraw.Draw(img)
    _score_tab_system(d, 100)
    staff, tab = 450, 450 + 40 + 13 * 14  # 六线谱在五线谱下面 13 个线距
    for i in range(5):
        d.line([(60, staff + i * 10), (1140, staff + i * 10)], fill=120)
    _draw_tab(d, tab, 60, 1140, (60, 600, 1140))
    a, b = detect_page(img)["systems"]
    assert a["bottom"] == b["top"] and b["top"] <= staff - 2


def test_light_playback_cursor_is_not_a_barline():
    """播放器截图里浅色的播放光标（灰度 190，刚好算进深色）贯穿整个六线谱、上下一样宽：比这一行别的小节线浅得多，不是小节线。"""
    img = _score()
    d = ImageDraw.Draw(img)
    for top in (120, 520):
        t = top + 110
        d.line([(600, t), (600, t + 70)], fill=190, width=2)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3]


@pytest.mark.parametrize("step,pad", [(26, 3), (30, 4), (28, 4)])
def test_row_crowded_with_digits_on_every_string_is_found(step, pad):
    """整段写出来的和弦扫弦：六根弦上每个十六分音符都有数字，数字的白底把谱线挡掉一半（谱线只有别的行一半深），
    数字的上下沿（两位数）在低门槛下又够得上门槛、夹在谱线之间，这一行以前找不到（Oshio 绿袖子倒数第 2 行）。"""
    sp = 20
    img = Image.new("L", (1200, 900), 255)
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default(size=17)
    for top in (100, 380, 660):
        for i in range(6):
            d.line([(60, top + i * sp), (1140, top + i * sp)], fill=170)
    for x in range(78, 1130, step):
        if min(abs(x - b) for b in (420, 780)) < sp:  # 小节线上不会有音符
            continue
        for i, s in enumerate(("0", "0", "12", "x", "10", "0")):
            y = 380 + i * sp
            if s != "x":  # alphaTab 在数字底下垫一块白底，盖住谱线（x 直接压在线上）
                x0, _, x1, _ = d.textbbox((x, y), s, font=f, anchor="mm")
                d.rectangle([x0 - pad, y - sp * 0.45, x1 + pad, y + sp * 0.45], fill=255)
            d.text((x, y), s, fill=0, font=f, anchor="mm")
    for top in (100, 380, 660):
        for x in (60, 420, 780, 1140):
            d.line([(x, top), (x, top + 5 * sp)], fill=40, width=2)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [3, 3, 3]
    assert all(abs(a - b) <= 1 for a, b in zip(r["systems"][1]["lines"], range(380, 481, sp)))


def test_tab_row_whose_string_one_is_mostly_hidden_is_not_taken_for_a_staff():
    """一行的 1 弦被数字挡掉一大半（或缩小后淡了）：第一遍只找到下面 5 根线，被当成一组五线谱，这一行就没了。
    后面低门槛找到的 6 根线距均匀、包含这 5 根，换成它（绿袖子缩小到 0.75 倍、Mother 第 5 页缩小后）。"""
    sp = 20
    img = Image.new("L", (1200, 900), 255)
    d = ImageDraw.Draw(img)
    for top in (100, 380, 660):
        _draw_tab(d, top, 60, 1140, (), sp)
    for x in range(70, 1130, 24):  # 1 弦只剩一小截一小截
        d.rectangle([x, 377, x + 15, 383], fill=255)
    for top in (100, 380, 660):
        for x in (60, 600, 1140):
            d.line([(x, top), (x, top + 5 * sp)], fill=40, width=2)
    r = detect_page(img)
    assert [len(s["bars"]) for s in r["systems"]] == [2, 2, 2]
    assert r["systems"][1]["lines"][0] == 380
