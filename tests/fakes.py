"""测试用的替身：不联网的模型、合成的谱子图片、GP 导入的小节数据。"""

import io
from typing import ClassVar

from app.llm import LLM
from tests.test_layout import _score


class FakeClaude(LLM):
    """替身模型：每行返回检测到的小节数；第一次识别第 1 行时故意把时值写错，验证返修。"""

    calls: ClassVar[list] = []
    tab_lines: ClassVar[list] = []
    last_request_id = "req_fake"

    def __init__(self, cfg):
        self.usage = {"input_tokens": 10, "output_tokens": 20}

    def ping(self):
        return "ok"

    def read_meta(self, page):
        return {
            "title": "测试曲",
            "artist": "",
            "arranger": "",
            "tuning": ["D", "A", "F#", "C#", "A", "D"],  # 不带八度，应按标准调弦补全
            "capo": 2,
            "time_signature": [4, 4],
            "tempo": 0,
            "notes": "",
        }

    def transcribe(
        self,
        img,
        meta,
        first_bar,
        spans,
        hint="",
        previous=None,
        problems=None,
        labeled_strings=False,
        start_ts=None,
    ):
        FakeClaude.calls.append({"first_bar": first_bar, "retry": previous is not None, "hint": hint})
        FakeClaude.tab_lines.append((first_bar, labeled_strings))
        bad = first_bar == 1 and previous is None
        bars = []
        for i in range(len(spans)):
            dur = "1/2" if bad else "1"
            bars.append(
                {
                    "number": first_bar + i,
                    "events": [
                        {
                            "dur": dur,
                            "notes": [{"string": 1, "fret": i, "tech": ["h"], "grace": -1, "bend": 0}],
                        }
                        for _ in range(4)
                    ],
                    "tip": f"tip {first_bar + i}",
                    "uncertain": "",
                }
            )
        return {"bars": bars}

    def explain_bars(self, meta, bars):
        FakeClaude.calls.append({"explain": [b["n"] for b in bars]})
        return {"bars": [{"number": b["n"], "tip": f"讲解 {b['n']}"} for b in bars]}

    def coach(self, meta, bar, neighbours, question, history, practice="", image=None):
        FakeClaude.calls.append(
            {
                "coach": bar["n"],
                "neighbours": [b["n"] for b in neighbours],
                "history": len(history),
                "practice": practice,
                "image": image.size if image else None,
            }
        )
        return f"回答：{question}"

    def practice_plan(self, meta, bars):
        return {"overview": "ok", "steps": [{"bars": "1-6", "title": "全曲", "advice": "慢练"}]}


def score_png() -> bytes:
    """合成的六线谱页面：两行，各 3 小节（见 tests/test_layout.py）。"""
    buf = io.BytesIO()
    _score().convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def derived_bars(n: int) -> list[dict]:
    """练习页从 GP 文件转换出的 n 个小节（每小节一个全音符）。"""
    return [{"events": [{"dur": "4", "notes": [{"string": 2, "fret": i % 5, "tech": []}]}]} for i in range(n)]
