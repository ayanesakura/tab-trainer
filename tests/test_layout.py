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
