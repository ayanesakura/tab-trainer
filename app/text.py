"""文字处理：GP 老格式里按 GBK 存的中文被当成 windows-1252 读出来的乱码（"Ñº¹ãÌ«ÀÉ"）还原成中文。
读取时（GP 文件重新转换、写入谱头）和旧数据升级（app/migrate.py）都用；前端 js/score.js 的 gbkFromMojibake 是同一套规则。"""

from __future__ import annotations

_CJK_RANGES = (("\u3040", "\u30ff"), ("\u3400", "\u4dbf"), ("\u4e00", "\u9fff"), ("\uf900", "\ufaff"))


def _cp1252_bytes(text: str) -> bytes | None:
    """按 windows-1252 把字符还原成字节：Latin-1 能表示的（含 0x80–0x9F 的控制字符）直接取码位，
    其余按 cp1252 编码（‚ƒ„… € 之类 0x80–0x9F 区的字符）；还原不了返回 None。"""
    out = bytearray()
    for c in text:
        if ord(c) < 256:
            out.append(ord(c))
            continue
        try:
            out += c.encode("cp1252")
        except UnicodeEncodeError:
            return None
    return bytes(out)


def fix_mojibake(text: str) -> str:
    """GBK 编码的中文被当成 windows-1252 / Latin-1 读出来的乱码（如 "Ñº¹ãÌ«ÀÉ"）还原成中文。
    规则和前端 score.js 一致（见 FIX-CONTRACT）：按 cp1252 还原字节再按 GBK 严格解码，任一步失败不改；
    而且要同时满足：非 ASCII 字符 ≥ 4 个、比 ASCII 字母多，解出来的中日韩字符 ≥ 2 个、没有替换符。
    正常的西文（"Canção"、"Mötley Crüe"、"Beyoncé" 之类）不动。"""
    if not text or text.isascii():
        return text
    raw = _cp1252_bytes(text)
    if raw is None:
        return text
    try:
        fixed = raw.decode("gbk")
    except UnicodeDecodeError:
        return text
    non_ascii = sum(not c.isascii() for c in text)
    letters = sum(c.isascii() and c.isalpha() for c in text)
    cjk = sum(any(lo <= c <= hi for lo, hi in _CJK_RANGES) for c in fixed)
    if non_ascii >= 4 and non_ascii > letters and cjk >= 2 and "\ufffd" not in fixed:
        return fixed
    return text


def fix_meta_text(meta: dict) -> dict:
    """谱头里的曲名、艺人、编配：修复乱码（见 fix_mojibake）。"""
    return {
        **meta,
        **{k: fix_mojibake(meta[k]) for k in ("title", "artist", "arranger") if isinstance(meta.get(k), str)},
    }
