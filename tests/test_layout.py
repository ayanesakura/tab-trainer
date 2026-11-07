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
