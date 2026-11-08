import pytest
from PIL import Image, ImageDraw

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
