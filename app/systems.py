"""谱行（song["systems"]）：版面检测的结果变成要识别的行（裁剪范围、小节位置、弦线），发给模型前在裁剪图上标弦号，
判断一行要不要切成两半识别、怎么切。只看图和版面，不调用模型。"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

from app import store
from app.layout import detect_page
from app.llm import TRANSCRIBE_MIN_WIDTH, image_scale

ROW_LEFT = 70  # 裁剪时行首往左多带的宽度：带上谱号 / 小节号
ROW_PAD = 12  # 行尾往右多带的宽度
BAR_PAD = 8  # 每个小节的区域（精修裁图用）左右各多带的宽度；refine.refinable 按它认出“这个小节自己的区域”
WHOLE_PAGE_BARS = 8  # 整页一行、还没识别时按几个小节估起始小节号


def plan_systems(sid: str, song: dict) -> list[dict]:
    return [s for pi, page in enumerate(song["pages"]) for s in plan_page_systems(sid, page, pi)]


def plan_page_systems(sid: str, page: dict, pi: int) -> list[dict]:
    """一页上的谱行（裁剪范围、小节位置、弦线）。检测不到六线谱时整页算一行（spans 为 None）。"""
    lay = detect_page(store.page_image(sid, page))
    if not lay["systems"]:
        return [
            {"page": pi, "crop": [0, page["width"], 0, page["height"]], "spans": None, "status": "pending"}
        ]
    systems = []
    for s in lay["systems"]:
        x0 = max(0, s["x0"] - ROW_LEFT)
        crop = [x0, min(page["width"], s["x1"] + ROW_PAD), s["top"], s["bottom"]]
        systems.append(
            {"page": pi, "crop": crop, "spans": s["bars"], "lines": s["lines"], "status": "pending"}
        )
    return systems


def tab_lines(sid: str, song: dict, sysinfo: dict) -> list[float] | None:
    """这一行六线谱 6 根弦线在裁剪图里的纵坐标。旧曲子没存，就对整页重新检测，取落在裁剪范围里的那一行。"""
    x0, x1, y0, y1 = sysinfo["crop"]
    lines = sysinfo.get("lines")
    if not lines:
        page = song["pages"][sysinfo["page"]]
        for s in detect_page(store.page_image(sid, page))["systems"]:
            if y0 <= s["lines"][0] and s["lines"][-1] <= y1 and s["x0"] < x1 and x0 < s["x1"]:
                lines = s["lines"]
                break
    return [y - y0 for y in lines] if lines else None


def label_strings(img: Image.Image, lines: list[float], x_start: float) -> Image.Image:
    """在六线谱左边每根弦线旁标上红色的弦号 1–6。模型数线时容易把低音弦的数字往下错一根，
    直接看图对照弦号比数线可靠（只告诉它坐标没有用，评测里错误照旧）。"""
    img = img.convert("RGB").copy()
    sp = (lines[-1] - lines[0]) / 5
    size = max(9, round(sp * 0.85))
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:  # 老版本 Pillow
        font = ImageFont.load_default()
    d = ImageDraw.Draw(img)
    x = max(1, x_start - size * 1.6)
    for i, y in enumerate(lines):
        d.text((x, y), str(i + 1), fill=(220, 30, 30), font=font, anchor="lm")
    return img


def should_split(sysinfo: dict) -> bool:
    """这一行切成两半能不能明显看得更清楚：整行发给模型时受最长边限制放不大（宽的行、高分辨率截图），
    切开以后每半能多放大 1.4 倍以上；至少 2 个小节。放大倍数和真正发图时一样算（llm.image_scale）。"""
    spans = sysinfo.get("spans") or []
    if len(spans) < 2:
        return False
    x0, x1, y0, y1 = sysinfo["crop"]
    w, h = x1 - x0, y1 - y0
    return image_scale(w / 2 + 40, h, TRANSCRIBE_MIN_WIDTH) >= 1.4 * image_scale(w, h, TRANSCRIBE_MIN_WIDTH)


def split_halves(sysinfo: dict) -> tuple[dict, dict]:
    """把一行切成左右两半（左半多一半的小节）：各自的裁剪范围和小节位置。切口附近两边都多带一点：
    左半多带一个线距（看得到切口处的小节线），右半往左多带 2.5 个线距（看得到连进来的连音线、和弦名）。"""
    spans = sysinfo["spans"]
    k = (len(spans) + 1) // 2
    x0, x1, y0, y1 = sysinfo["crop"]
    lines = sysinfo.get("lines") or []
    sp = (lines[-1] - lines[0]) / 5 if len(lines) == 6 else 12
    cut = spans[k][0]
    return (
        {**sysinfo, "crop": [x0, min(x1, int(cut + sp)), y0, y1], "spans": spans[:k]},
        {**sysinfo, "crop": [max(x0, int(cut - sp * 2.5)), x1, y0, y1], "spans": spans[k:]},
    )


def bar_regions(sysinfo: dict, page_width: int, n: int) -> list[list[float]]:
    """一行识别出 n 个小节时每个小节在页面上的区域（原谱对照、精修裁图）。小节数和检测到的小节线对得上，
    才按位置给每个小节它自己的区间；对不上（多了 / 漏了小节）时第 k 个小节不一定是第 k 个区间，按位置分会让
    精修拿别的小节的图去核对（refine.refinable 也会挡掉这样的行），都给整行的区域。"""
    x0, x1, y0, y1 = sysinfo["crop"]
    spans = sysinfo.get("spans")
    if spans and len(spans) == n:
        return [[max(0, a - BAR_PAD), min(page_width, c + BAR_PAD), y0, y1] for a, c in spans]
    return [[x0, x1, y0, y1] for _ in range(n)]
