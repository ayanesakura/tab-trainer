"""测试用的替身：不联网的模型、合成的谱子图片、GP 导入的小节数据。"""

import io
import threading
import time
from typing import ClassVar

from app.llm import LLM, join_text
from tests.test_layout import _score

_lock = threading.Lock()


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
        context="",
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

    # 精修的替身：默认什么都没改；测试换掉 verify_result（参数同 verify_bar，返回模型的回答）
    verify_result: ClassVar = None

    def verify_bar(self, img, meta, span, events, previous=None, time_signature=None, hint="", shapes=None):
        FakeClaude.calls.append(
            {
                "shapes": shapes,
                "verify": [e["notes"] for e in events],
                "previous": previous is not None,
                "size": img.size,
                "span": span,
                "ts": time_signature,
                "hint": hint,
            }
        )
        with _lock:  # 每次核对记一笔用量（测费用记到曲子上）；几个小节是并行核对的
            u = dict(self.usage)
            u["input_tokens"] += 1000
            u["cost_usd"] = round(u.get("cost_usd", 0) + 0.05, 4)
            self.usage = u
        if FakeClaude.verify_result is not None:
            return FakeClaude.verify_result(img, meta, span, events, previous, time_signature)
        return {"diff": "", "changed": False, "events": events, "uncertain": ""}

    def explain_bars(self, meta, bars):
        FakeClaude.calls.append({"explain": [b["n"] for b in bars]})
        return {"bars": [{"number": b["n"], "tip": f"讲解 {b['n']}"} for b in bars]}

    # 问答的替身：回答是“回答：<问题>”，每 3 个字一段给出；stream_delay 是每段之前停的秒数（端到端测试看回答一点点出来）
    stream_delay: ClassVar[float] = 0.0

    def coach(self, meta, bar, neighbours, question, history, practice="", image=None):
        return join_text(self.coach_stream(meta, bar, neighbours, question, history, practice, image))

    def coach_stream(self, meta, bar, neighbours, question, history, practice="", image=None):
        FakeClaude.calls.append(
            {
                "coach": bar["n"],
                "neighbours": [b["n"] for b in neighbours],
                "history": len(history),
                "practice": practice,
                "image": image.size if image else None,
            }
        )
        return self._chunks(f"回答：{question}")

    def _chunks(self, answer):
        for k in range(0, len(answer), 3):
            if FakeClaude.stream_delay:
                time.sleep(FakeClaude.stream_delay)
            yield answer[k : k + 3]
        with _lock:  # 一次回答记一笔用量（测费用记到曲子 / 对话上）
            u = dict(self.usage)
            u["output_tokens"] += 100
            u["cost_usd"] = round(u.get("cost_usd", 0) + 0.01, 4)
            self.usage = u

    def practice_plan(self, meta, bars):
        return {"overview": "ok", "steps": [{"bars": "1-6", "title": "全曲", "advice": "慢练"}]}

    # 首页 AI 对话的替身：回答是 chat_answer（默认“回答：<问题>”），分段和用量同 coach_stream
    chat_answer: ClassVar[str | None] = None

    def chat_stream(self, question, context, history, today):
        FakeClaude.calls.append({"chat": question, "context": context, "history": history, "today": today})
        return self._chunks(FakeClaude.chat_answer or f"回答：{question}")


def score_png() -> bytes:
    """合成的六线谱页面：两行，各 3 小节（见 tests/test_layout.py）。"""
    buf = io.BytesIO()
    _score().convert("RGB").save(buf, "PNG")
    return buf.getvalue()


def derived_bars(n: int) -> list[dict]:
    """练习页从 GP 文件转换出的 n 个小节（每小节一个全音符）。"""
    return [{"events": [{"dur": "4", "notes": [{"string": 2, "fret": i % 5, "tech": []}]}]} for i in range(n)]
