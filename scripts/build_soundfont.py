"""把几个 SF2 音色库里挑出来的音色合成一个小音色库（SF2 或 SF3）。

练习页只需要木吉他和节拍器。完整音色库动辄几十上百 MB，挑出来再用 Ogg 压缩（SF3，alphaTab 能直接读）只有几 MB。

用法：
    uv run --with soundfile --with numpy python scripts/build_soundfont.py OUT.sf3 \\
        --take FSS.sf2:0:0=0:25 --take Spanish.sf2:0:0=0:24 \\
        --take sonivox.sf2:128:0=128:0@33 --name "tab-trainer guitars"

--take SRC:BANK:PROG[=NEWBANK:NEWPROG][@KEY,KEY...][+atten=CB][+lowpass=HZ][#NAME]
    从 SRC 里取一个音色，可改编号；@ 后面是只保留的键（鼓组里只要节拍器的 33 号键时用）；
    +atten 加衰减（单位 0.1 dB），+lowpass 加低通滤波（Hz），#NAME 改音色名。
输出文件以 .sf3 结尾时用 Ogg Vorbis 压缩采样（需要 soundfile、numpy）。
注意：alphaTab 只加载单声道采样，立体声采样会被跳过。
"""

from __future__ import annotations

import argparse
import io
import re
import struct
from dataclasses import dataclass

GEN_KEY_RANGE = 43
GEN_INSTRUMENT = 41
GEN_SAMPLE_ID = 53
GEN_FILTER_FC = 8  # 低通截止频率，绝对音分（8.176 Hz × 2^(c/1200)），默认 13500 = 不滤波
GEN_ATTENUATION = 48  # 衰减，单位 0.1 dB
SAMPLE_PAD = 46  # SF2 规定每个采样后面至少补 46 个零点
SF3_COMPRESSED = 0x10


@dataclass
class Bag:
    gens: list[tuple[int, bytes]]  # (oper, 2 字节原始 amount)
    mods: list[bytes]  # 每个 10 字节

    def gen(self, oper: int) -> bytes | None:
        for o, a in self.gens:
            if o == oper:
                return a
        return None


def _chunks(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        cid, size = struct.unpack_from("<4sI", data, pos)
        yield cid.decode("latin-1"), pos + 8, size
        pos += 8 + size + (size & 1)


def _name(rec: bytes) -> str:
    return rec[:20].split(b"\0", 1)[0].decode("latin-1")


def read_sf2(path: str):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"RIFF" or data[8:12] != b"sfbk":
        raise SystemExit(f"{path} 不是 SF2 文件")
    lists = {}
    for cid, off, size in _chunks(data, 12, len(data)):
        if cid == "LIST":
            lists[data[off : off + 4].decode()] = (off + 4, off + size)
    smpl = b""
    for cid, off, size in _chunks(data, *lists["sdta"]):
        if cid == "smpl":
            smpl = data[off : off + size]
    pd = {cid: (off, size) for cid, off, size in _chunks(data, *lists["pdta"])}
    sizes = {
        "phdr": 38,
        "pbag": 4,
        "pmod": 10,
        "pgen": 4,
        "inst": 22,
        "ibag": 4,
        "imod": 10,
        "igen": 4,
        "shdr": 46,
    }
    r = {k: [data[pd[k][0] + i : pd[k][0] + i + n] for i in range(0, pd[k][1], n)] for k, n in sizes.items()}
    return smpl, r


class Source:
    def __init__(self, path: str):
        self.path = path
        self.smpl, self.r = read_sf2(path)
        self.index = {struct.unpack_from("<HH", p, 20)[::-1]: i for i, p in enumerate(self.r["phdr"][:-1])}

    def _zones(self, bag, gen, mod, first, last) -> list[Bag]:
        r = self.r
        out = []
        for b in range(first, last):
            g0, m0 = struct.unpack("<HH", r[bag][b])
            g1, m1 = struct.unpack("<HH", r[bag][b + 1])
            gens = [(struct.unpack("<H", x[:2])[0], x[2:4]) for x in r[gen][g0:g1]]
            out.append(Bag(gens, r[mod][m0:m1]))
        return out

    def preset_zones(self, i):
        p = self.r["phdr"]
        return self._zones(
            "pbag",
            "pgen",
            "pmod",
            struct.unpack_from("<H", p[i], 24)[0],
            struct.unpack_from("<H", p[i + 1], 24)[0],
        )

    def inst_zones(self, i):
        p = self.r["inst"]
        return self._zones(
            "ibag",
            "igen",
            "imod",
            struct.unpack_from("<H", p[i], 20)[0],
            struct.unpack_from("<H", p[i + 1], 20)[0],
        )


def _key_ok(zone: Bag, keys) -> bool:
    if not keys:
        return True
    kr = zone.gen(GEN_KEY_RANGE)
    return kr is None or any(kr[0] <= k <= kr[1] for k in keys)


def _apply_tweak(zone: Bag, glob: Bag | None, tweak: dict) -> None:
    """给采样区加衰减 / 低通。区里的生成器会覆盖全局区的同名生成器，所以要先取到实际生效的值。"""

    def current(oper, default):
        for z in (zone, glob):
            v = z.gen(oper) if z else None
            if v is not None:
                return struct.unpack("<h", v)[0]
        return default

    def put(oper, value):
        zone.gens = [g for g in zone.gens if g[0] != oper] + [(oper, struct.pack("<h", value))]

    if tweak.get("atten"):
        put(GEN_ATTENUATION, max(0, min(1440, current(GEN_ATTENUATION, 0) + tweak["atten"])))
    if tweak.get("lowpass"):
        import math

        fc = round(1200 * math.log2(tweak["lowpass"] / 8.176))
        put(GEN_FILTER_FC, min(current(GEN_FILTER_FC, 13500), fc))


def _encode_ogg(pcm16: bytes, rate: int, quality: float) -> bytes:
    import numpy as np
    import soundfile as sf

    buf = io.BytesIO()
    sf.write(
        buf,
        np.frombuffer(pcm16, dtype="<i2"),
        rate,
        format="OGG",
        subtype="VORBIS",
        compression_level=quality,
    )
    return buf.getvalue()


def build(out: str, takes: list[dict], name: str, ogg_level: float = 0.6) -> dict:
    sf3 = out.lower().endswith(".sf3")
    sources: dict[str, Source] = {}
    new_inst: list[tuple[str, list[Bag]]] = []
    inst_map: dict = {}
    new_samples: list[tuple[Source, int]] = []
    sample_map: dict = {}

    def use_sample(src: Source, old: int) -> int:
        k = (src.path, old)
        if k not in sample_map:
            stype = struct.unpack_from("<H", src.r["shdr"][old], 44)[0]
            if stype & 0x0E:
                raise SystemExit(f"{src.path} 的采样 {_name(src.r['shdr'][old])} 是立体声，alphaTab 不会加载")
            sample_map[k] = len(new_samples)
            new_samples.append((src, old))
        return sample_map[k]

    def use_inst(src: Source, old: int, keys, tweak: dict) -> int:
        k = (src.path, old, frozenset(keys or ()), tuple(sorted(tweak.items())))
        if k in inst_map:
            return inst_map[k]
        zones = []
        glob = None
        for z in src.inst_zones(old):
            sid = z.gen(GEN_SAMPLE_ID)
            if sid is None:
                glob = z
            else:
                if not _key_ok(z, keys):
                    continue
                new = use_sample(src, struct.unpack("<H", sid)[0])
                z.gens = [(o, struct.pack("<H", new) if o == GEN_SAMPLE_ID else a) for o, a in z.gens]
                _apply_tweak(z, glob, tweak)
            zones.append(z)
        inst_map[k] = len(new_inst)
        new_inst.append((_name(src.r["inst"][old]), zones))
        return inst_map[k]

    new_presets = []
    for t in takes:
        src = sources.setdefault(t["src"], Source(t["src"]))
        if (t["bank"], t["prog"]) not in src.index:
            raise SystemExit(f"{t['src']} 里没有音色 {t['bank']}:{t['prog']}")
        i = src.index[(t["bank"], t["prog"])]
        zones = []
        for z in src.preset_zones(i):
            ins = z.gen(GEN_INSTRUMENT)
            if ins is not None:
                if not _key_ok(z, t["keys"]):
                    continue
                ni = use_inst(src, struct.unpack("<H", ins)[0], t["keys"], t.get("tweak") or {})
                z.gens = [(o, struct.pack("<H", ni) if o == GEN_INSTRUMENT else a) for o, a in z.gens]
            zones.append(z)
        new_presets.append((t.get("label") or _name(src.r["phdr"][i]), t["new_prog"], t["new_bank"], zones))

    # 采样数据
    data = bytearray()
    shdr = []
    for src, old in new_samples:
        rec = src.r["shdr"][old]
        start, end, ls, le, rate = struct.unpack_from("<5I", rec, 20)
        pitch, corr = rec[40], rec[41]
        stype = struct.unpack_from("<H", rec, 44)[0]
        pcm = src.smpl[start * 2 : end * 2]
        if sf3:  # SF3：start/end 是 smpl 里的字节位置，循环点相对采样开头
            b0 = len(data)
            data += _encode_ogg(pcm, rate, ogg_level)
            pos = (b0, len(data), ls - start, le - start)
            stype |= SF3_COMPRESSED
        else:
            base = len(data) // 2
            data += pcm + b"\0" * (SAMPLE_PAD * 2)
            pos = (base, base + end - start, base + ls - start, base + le - start)
        shdr.append(rec[:20] + struct.pack("<5IBBHH", *pos, rate, pitch, corr, 0, stype))
    shdr.append(b"EOS".ljust(20, b"\0") + b"\0" * 26)
    if len(data) & 1:  # alphaTab 读 RIFF 时不跳过奇数长度块后面的补位字节，所以自己补成偶数
        data += b"\0"

    def emit(zones, bag_out, gen_out, mod_out):
        for z in zones:
            bag_out.append(struct.pack("<HH", len(gen_out), len(mod_out)))
            # 规范要求 keyRange 在最前、instrument/sampleID 在最后
            order = lambda g: (
                0 if g[0] == GEN_KEY_RANGE else 2 if g[0] in (GEN_INSTRUMENT, GEN_SAMPLE_ID) else 1
            )
            gen_out += [struct.pack("<H", o) + a for o, a in sorted(z.gens, key=order)]
            mod_out += z.mods

    phdr, pbag, pgen, pmod = [], [], [], []
    for nm, prog, bank, zones in new_presets:
        phdr.append(
            nm.encode("latin-1", "replace")[:20].ljust(20, b"\0")
            + struct.pack("<HHHIII", prog, bank, len(pbag), 0, 0, 0)
        )
        emit(zones, pbag, pgen, pmod)
    phdr.append(b"EOP".ljust(20, b"\0") + struct.pack("<HHHIII", 0, 0, len(pbag), 0, 0, 0))
    pbag.append(struct.pack("<HH", len(pgen), len(pmod)))
    pgen.append(b"\0" * 4)
    pmod.append(b"\0" * 10)

    inst, ibag, igen, imod = [], [], [], []
    for nm, zones in new_inst:
        inst.append(nm.encode("latin-1", "replace")[:20].ljust(20, b"\0") + struct.pack("<H", len(ibag)))
        emit(zones, ibag, igen, imod)
    inst.append(b"EOI".ljust(20, b"\0") + struct.pack("<H", len(ibag)))
    ibag.append(struct.pack("<HH", len(igen), len(imod)))
    igen.append(b"\0" * 4)
    imod.append(b"\0" * 10)

    def chunk(cid: str, body: bytes) -> bytes:
        return cid.encode() + struct.pack("<I", len(body)) + body + (b"\0" if len(body) & 1 else b"")

    def zstr(s: str) -> bytes:  # 以 0 结尾、长度为偶数
        b = s.encode("utf-8") + b"\0"
        return b + b"\0" * (len(b) & 1)

    info = (
        chunk("ifil", struct.pack("<HH", 3 if sf3 else 2, 1))
        + chunk("isng", zstr("EMU8000"))
        + chunk("INAM", zstr(name))
    )
    pdta = b"".join(
        chunk(k, b"".join(v))
        for k, v in [
            ("phdr", phdr),
            ("pbag", pbag),
            ("pmod", pmod),
            ("pgen", pgen),
            ("inst", inst),
            ("ibag", ibag),
            ("imod", imod),
            ("igen", igen),
            ("shdr", shdr),
        ]
    )
    lst = lambda kind, body: chunk("LIST", kind.encode() + body)
    body = b"sfbk" + lst("INFO", info) + lst("sdta", chunk("smpl", bytes(data))) + lst("pdta", pdta)
    with open(out, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", len(body)) + body)
    return {
        "presets": [(b, p, n) for n, p, b, _ in new_presets],
        "samples": len(new_samples),
        "bytes": len(body) + 8,
    }


TAKE = re.compile(
    r"^(?P<src>.+?):(?P<bank>\d+):(?P<prog>\d+)(?:=(?P<nb>\d+):(?P<np>\d+))?(?:@(?P<keys>[\d,]+))?(?P<tw>(?:\+\w+=[\d.]+)*)(?:#(?P<label>.+))?$"
)


def parse_take(s: str) -> dict:
    m = TAKE.match(s)
    if not m:
        raise SystemExit(f"--take 格式不对：{s}")
    bank, prog = int(m["bank"]), int(m["prog"])
    return {
        "src": m["src"],
        "bank": bank,
        "prog": prog,
        "new_bank": int(m["nb"]) if m["nb"] else bank,
        "new_prog": int(m["np"]) if m["np"] else prog,
        "keys": {int(k) for k in m["keys"].split(",")} if m["keys"] else None,
        "label": m["label"],
        "tweak": {
            k: float(v) if k == "lowpass" else int(v)
            for k, v in re.findall(r"\+(\w+)=([\d.]+)", m["tw"] or "")
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("out")
    ap.add_argument(
        "--take",
        action="append",
        required=True,
        help="SRC:BANK:PROG[=NEWBANK:NEWPROG][@KEYS][+atten=CB][+lowpass=HZ][#NAME]",
    )
    ap.add_argument("--name", default="tab-trainer guitars")
    ap.add_argument(
        "--ogg-level", type=float, default=0.3, help="SF3 压缩程度，0 = 音质最好、文件最大，1 = 最小"
    )
    a = ap.parse_args()
    print(build(a.out, [parse_take(t) for t in a.take], a.name, a.ogg_level))


if __name__ == "__main__":
    main()
