"""调用 Claude 识别谱子。

三类调用，都要求按 JSON Schema 输出（structured outputs）：
- read_meta：读首页谱头（曲名、调弦、变调夹、拍号）
- transcribe：识别一行（或整页）谱子里的所有小节
- practice_plan：根据识别结果写分段练习建议
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass

import anthropic
from PIL import Image

from app.music import TECHS

log = logging.getLogger("tab_trainer.llm")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
MAX_EDGE = 2576  # 当前模型支持的最大长边，超过会被缩小
FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class LLMConfig:
    api_key: str = ""
    base_url: str = ""
    model: str = DEFAULT_MODEL
    effort: str = "high"


class LLMError(RuntimeError):
    def __init__(self, msg: str, request_id: str | None = None):
        super().__init__(msg)
        self.request_id = request_id


def request_id_of(e: BaseException | None) -> str:
    """异常对应的请求 ID（API 报错、拒绝、截断都有），用来写日志；拿不到就是 none。"""
    rid = getattr(e, "request_id", None)
    if not rid:
        resp = getattr(e, "response", None)
        rid = getattr(resp, "headers", {}).get("request-id") if resp is not None else None
    return rid or "none"


# ---------------------------------------------------------------------------
# JSON Schemas
# ---------------------------------------------------------------------------
def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


NOTE_SCHEMA = _obj(
    {
        "string": {
            "type": "integer",
            "enum": [1, 2, 3, 4, 5, 6],
            "description": "1 = highest (top tab line) ... 6 = lowest",
        },
        "fret": {"type": "integer", "description": "fret number exactly as written in the tab"},
        "tech": {"type": "array", "items": {"type": "string", "enum": TECHS}},
        "grace": {
            "type": "integer",
            "description": "fret of a grace note leading into this note, -1 if none",
        },
        "bend": {"type": "number", "description": "bend amount in semitones, 0 if no bend"},
    }
)
EVENT_SCHEMA = _obj(
    {
        "dur": {
            "type": "string",
            "description": "time from this onset to the next onset (or bar end), in quarter notes, "
            'as a fraction string: "1/4" = 16th, "1/2" = 8th, "1/3" = 8th-note triplet, '
            '"1/6" = 16th sextuplet, "3/4" = dotted 8th, "1", "3/2", "2", "4" ...',
        },
        "notes": {"type": "array", "items": NOTE_SCHEMA, "description": "empty for a rest"},
    }
)
DIRECTIONS = [
    "segno", "segnosegno", "coda", "doublecoda", "fine",
    "dacapo", "dacapoalcoda", "dacapoalfine", "dalsegno", "dalsegnoalcoda", "dalsegnoalfine", "dacoda",
]  # fmt: skip
BAR_SCHEMA = _obj(
    {
        "number": {"type": "integer"},
        "time_signature": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "[numerator, denominator] if a time-signature change is written at the start of this "
            "bar (e.g. [3, 4]); [] if none",
        },
        "events": {"type": "array", "items": EVENT_SCHEMA},
        "repeat_start": {
            "type": "boolean",
            "description": "a start-repeat barline ( |: ) at the start of this bar",
        },
        "repeat_end": {
            "type": "integer",
            "description": "an end-repeat barline ( :| ) at the end of this bar: how many times the section is "
            'played in total (2 for a plain repeat sign, 3 for "x3"); 0 if none',
        },
        "endings": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "volta bracket numbers this bar is under (e.g. [1] or [1, 2]); [] if none",
        },
        "directions": {
            "type": "array",
            "items": {"type": "string", "enum": DIRECTIONS},
            "description": "navigation marks written at this bar; [] if none",
        },
        "tip": {"type": "string"},
        "uncertain": {"type": "string"},
    }
)
TRANSCRIBE_SCHEMA = _obj({"bars": {"type": "array", "items": BAR_SCHEMA}})

META_SCHEMA = _obj(
    {
        "title": {"type": "string"},
        "artist": {"type": "string"},
        "arranger": {"type": "string"},
        "tuning": {"type": "array", "items": {"type": "string"}},
        "capo": {"type": "integer"},
        "time_signature": {"type": "array", "items": {"type": "integer"}},
        "tempo": {"type": "integer"},
        "notes": {"type": "string"},
    }
)

EXPLAIN_SCHEMA = _obj(
    {"bars": {"type": "array", "items": _obj({"number": {"type": "integer"}, "tip": {"type": "string"}})}}
)

COACH_SCHEMA = _obj({"answer": {"type": "string"}})

PLAN_SCHEMA = _obj(
    {
        "overview": {"type": "string"},
        "steps": {
            "type": "array",
            "items": _obj(
                {"bars": {"type": "string"}, "title": {"type": "string"}, "advice": {"type": "string"}}
            ),
        },
    }
)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
SYSTEM = (
    "You transcribe guitar sheet music images into structured data for a practice app that plays the "
    "music back, highlights fingerings on a fretboard, and loops sections. The user will practise "
    "from your transcription, so an exact reading of every fret number and rhythm matters far more "
    "than speed. Read the tablature as the source of truth for strings and frets; use the standard "
    "notation (if present) and the rhythm stems/beams to determine timing and to double-check pitches. "
    "Write all human-facing text (tip, uncertain, notes, advice) in Simplified Chinese."
)

TRANSCRIBE_RULES = """\
How to encode a bar:
- A bar is a list of events in time order. An event is one onset: all notes that start together.
- "dur" is the time from this onset to the next onset in the same bar (for the last event: to the end of the bar), in quarter notes. The durs of a bar must add up exactly to the bar length.
- Notes that keep ringing while other notes are played do not create extra events, and a tied continuation note (a note whose fret number is not re-written, or connected by a tie) is not a new event.
- A rest at the start of a bar or between notes is an event with an empty "notes" list.
- Time signature: a bar where a new time signature is written (e.g. 3/4 or 6/8 printed after the barline) gets "time_signature": [3, 4] etc., and from that bar on every bar lasts the new length until the next change. Bars without a written change get []. Never change durations just to make a bar fit a time signature: if the notes clearly fill a different length, check whether a time-signature change is written.
- string: 1 is the top line of the tab (highest pitch), 6 the bottom line. fret: exactly as written (relative to the capo; 0 = open string).

Techniques (put them on the note that receives them):
- "h": hammer-on into this note (arc + H from the previous note on the same string). "p": pull-off into this note.
- "sl": slide into this note from the previous note on the same string; this note is not re-picked.
- "si": slide in from an unspecified lower fret (a slash before the number with no starting note); the note is picked.
- "pm": palm mute (P.M. line). "x": dead/muted note written as x (use fret 0).
- "nh": natural harmonic, written <n>; fret = the harmonic node n. "ah": artificial harmonic (A.H., e.g. "2<14>"): fret = the fretted note (2), the node is 12 frets higher.
- "roll": arpeggiated chord (wavy vertical line) - put it on every note of the chord.
- "vib": vibrato. "bend": a bend; also set "bend" to the size in semitones (full = 2, 1/2 = 1).
- A small grace note before a note: set "grace" on the main note to the grace note's fret (and add "h"/"sl"/"p" if the grace connects by that technique). Otherwise grace = -1.

Repeats and navigation (the app follows them during playback):
- repeat_start / repeat_end: repeat barlines. repeat_end is the total number of times the section is played (2 for a plain :| sign).
- endings: the numbers of the 1st/2nd-ending (volta) bracket over this bar.
- directions, at the bar where they are written: "segno" (the 𝄋 sign), "coda" (the 𝄌 target, "To Coda" jumps here), "fine" (Fine, where the piece ends after a D.C./D.S.), "dacapo" (D.C.), "dacapoalfine" (D.C. al Fine), "dacapoalcoda" (D.C. al Coda), "dalsegno" (D.S.), "dalsegnoalfine", "dalsegnoalcoda", "dacoda" (the "To Coda" / ⊕ jump point).

"tip": 1-4 sentences of practical Chinese guidance for this bar, naming strings and frets the way a teacher would, e.g. "5 弦空弦 + 2 弦空弦一起弹，2 弦击弦到 2 品再勾弦回空弦（只拨一次）。第 3 拍和弦：2 弦 4 品、3 弦 2 品、4 弦 3 品；第 4 拍三连音：4 弦 3 品、3 弦空弦、2 弦空弦。" Mention what is hard and how to practise it when it helps.
"uncertain": in Chinese, anything you could not read with confidence (which beat, which string, what you assumed). Empty string if everything is clear."""


STREAM_RETRIES = 2
STREAM_RETRY_WAIT = 3.0  # 秒，第 n 次重试前等 n 倍


def _transient(e: Exception) -> bool:
    """值得重试的临时错误：连接断开、流里的 api_error / overloaded_error、5xx 和 429。
    认证、权限、参数错误等重试也没用。"""
    if isinstance(e, anthropic.APIConnectionError):
        return True
    if not isinstance(e, anthropic.APIStatusError):
        return False
    if e.status_code in (429, 500, 502, 503, 504, 529):
        return True
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    return e.status_code == 200 and err.get("type") in ("api_error", "overloaded_error")


def _image_block(img: Image.Image, min_width: int = 0) -> tuple[dict, float]:
    img = img.convert("RGB")
    w, h = img.size
    scale = 1.0
    if min_width and w < min_width:
        scale = min(2.0, min_width / w)  # 小图适当放大，数字更清楚
    scale = min(scale, MAX_EDGE / max(w, h))
    if abs(scale - 1) > 0.01:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.standard_b64encode(buf.getvalue()).decode(),
        },
    }, scale


def _note_text(n: dict) -> str:
    s = f"{n['string']}弦{n['fret']}品"
    if n.get("grace", -1) >= 0:
        s = f"(倚音{n['grace']}品)" + s
    return s + (f"[{','.join(n['tech'])}]" if n.get("tech") else "")


def bar_text(b: dict) -> str:
    """一个小节的文字版：'第N小节: <时值>拍 <音> | ...'，给只发文字的调用用。"""
    ts = b.get("time_signature")  # 只有和全曲拍号不同的小节才有
    head = f"第{b['n']}小节" + (f"（{ts[0]}/{ts[1]} 拍）" if ts else "")
    return (
        head
        + ": "
        + " | ".join(
            f"{e['dur']}拍 " + (" ".join(_note_text(n) for n in e["notes"]) or "休止") for e in b["events"]
        )
    )


NOTATION_KEY = (
    "each event is '<duration in quarter notes> <notes>', notes as '<string>弦<fret>品' (string 1 = highest), "
    "techniques in brackets (h hammer-on, p pull-off, sl slide from the previous note, si slide in, pm palm mute, "
    "nh natural harmonic, ah artificial harmonic, roll arpeggio, x dead note, vib vibrato, bend)"
)


class Claude:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        # 只用设置页里填的 key 或 ANTHROPIC_API_KEY；地址也显式指定。
        # 不让 SDK 自动读取 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL 等环境变量——
        # 那些可能是别的工具（如 Claude Code）的凭据和网关，不能被这个应用顺手用掉。
        key = cfg.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMError(
                "还没有设置 API key：请在首页“模型设置”里填写，或启动前设置环境变量 ANTHROPIC_API_KEY。"
            )
        self.client = anthropic.Anthropic(
            api_key=key, base_url=cfg.base_url or DEFAULT_BASE_URL, max_retries=3, timeout=600
        )
        # 同一个客户端会被识别的几个线程同时用：用量累加和兜底开关都要加锁
        self._lock = threading.Lock()
        self._local = threading.local()  # 每个线程最近一次请求的 ID（识别是几个线程同时跑的）
        self.use_fallbacks = True
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def _call(self, content: list, schema: dict, effort: str | None = None, max_tokens: int = 32000) -> dict:
        kwargs = {
            "model": self.cfg.model or DEFAULT_MODEL,
            "max_tokens": max_tokens,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": content}],
            "output_config": {
                "effort": effort or self.cfg.effort,
                "format": {"type": "json_schema", "schema": schema},
            },
        }
        used_fallbacks = self.use_fallbacks
        try:
            msg = self._stream(kwargs, fallbacks=used_fallbacks)
        except anthropic.BadRequestError as e:
            # 代理或旧模型可能不认识 fallbacks 参数：去掉后重试一次（看的是这次请求自己有没有带）
            if used_fallbacks and "fallback" in str(e).lower():
                log.warning("request_id=%s fallbacks rejected, retrying without: %s", request_id_of(e), e)
                with self._lock:
                    self.use_fallbacks = False
                msg = self._stream(kwargs, fallbacks=False)
            else:
                raise
        rid = getattr(msg, "_request_id", None) or "none"
        self._local.request_id = rid
        with self._lock:
            self.usage["input_tokens"] += msg.usage.input_tokens or 0
            self.usage["output_tokens"] += msg.usage.output_tokens or 0
        if msg.stop_reason == "refusal":
            raise LLMError(f"模型拒绝了这次请求（request_id={rid}）", rid)
        if msg.stop_reason == "max_tokens":
            raise LLMError(f"输出超出长度上限，被截断（request_id={rid}）", rid)
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"模型返回的不是合法 JSON（request_id={rid}）：{e}", rid) from e

    @property
    def last_request_id(self) -> str:
        """这个线程最近一次成功请求的 ID。"""
        return getattr(self._local, "request_id", "none")

    def _stream(self, kwargs: dict, fallbacks: bool | None = None):
        """流式请求。SDK 自带的重试只管建立连接；回复传到一半出错（网关断线、服务过载，流里收到的 error 事件，
        状态码是 200）不会重试，整行识别就失败了。这类临时错误这里再试两次。"""
        for attempt in range(STREAM_RETRIES + 1):
            try:
                return self._stream_once(kwargs, fallbacks)
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
                if attempt == STREAM_RETRIES or not _transient(e):
                    raise
                log.warning(
                    "request_id=%s transient error mid-stream, retry %d: %s", request_id_of(e), attempt + 1, e
                )
                time.sleep(STREAM_RETRY_WAIT * (attempt + 1))
        raise AssertionError("unreachable")

    def _stream_once(self, kwargs: dict, fallbacks: bool | None = None):
        if self.use_fallbacks if fallbacks is None else fallbacks:
            with self.client.beta.messages.stream(**kwargs, betas=[FALLBACK_BETA], fallbacks="default") as s:
                return s.get_final_message()
        with self.client.messages.stream(**kwargs) as s:
            return s.get_final_message()

    # -- 三类调用 -------------------------------------------------------------
    def ping(self) -> str:
        r = self._call(
            [
                {
                    "type": "text",
                    "text": 'Reply with {"title": "ok", ...} filling other fields with empty values.',
                }
            ],
            META_SCHEMA,
            effort="low",
            max_tokens=2000,
        )
        return r.get("title", "")

    def read_meta(self, page: Image.Image) -> dict:
        block, _ = _image_block(page)
        text = (
            "This is the first page of a guitar score. Read its header and return:\n"
            "- title / artist / arranger (empty string if absent)\n"
            "- tuning: 6 note names with octave, for string 1 (highest) to string 6 (lowest), e.g. "
            '["E4","B3","G3","D3","A2","E2"]. Tuning text such as "DADGAD" or "Tuning=DAC#F#AD" lists strings '
            "from 6 (lowest) to 1. If no tuning is written, assume standard tuning.\n"
            "- capo: the capo fret, 0 if none.\n"
            "- time_signature: [numerator, denominator] of the first bar, e.g. [4, 4].\n"
            "- tempo: quarter-note BPM if written, else 0.\n"
            "- notes: in Chinese, anything a player should know (e.g. tuning changes, the written capo looking "
            "inconsistent with the standard-notation pitches). "
            "Empty string if nothing."
        )
        return self._call([block, {"type": "text", "text": text}], META_SCHEMA, max_tokens=16000)

    def transcribe(
        self,
        img: Image.Image,
        meta: dict,
        first_bar: int,
        bar_spans: list[tuple[int, int]] | None,
        hint: str = "",
        previous: dict | None = None,
        problems: list[str] | None = None,
        labeled_strings: bool = False,
        start_ts: list[int] | None = None,
    ) -> dict:
        """start_ts：前面的行里变过拍号时，这一行开头实际的拍号（这张图上看不到那个变拍号）。"""
        block, scale = _image_block(img, min_width=1800)
        ts = meta["time_signature"]
        bar_len = ts[0] * 4 / ts[1]
        info = (
            f"Score info: tuning (string 1 -> 6) {' '.join(meta['tuning'])}; capo {meta['capo']} "
            f"(copy fret numbers as written). "
        )
        if start_ts and start_ts != ts:
            n_len = start_ts[0] * 4 / start_ts[1]
            info += (
                f"The piece starts in {ts[0]}/{ts[1]}, but an earlier line changed the time signature: at the start "
                f"of this line it is {start_ts[0]}/{start_ts[1]}, so a bar lasts {n_len:g} quarter notes unless "
                "another time-signature change is written in this image (see below)."
            )
        else:
            info += (
                f"Time signature at the start of the piece {ts[0]}/{ts[1]}: a bar lasts {bar_len:g} quarter notes "
                "unless a time-signature change is written (see below)."
            )
        lines = [info]
        if bar_spans:
            last = first_bar + len(bar_spans) - 1
            spans = "; ".join(
                f"bar {first_bar + i}: x={round(a * scale)}-{round(b * scale)}"
                for i, (a, b) in enumerate(bar_spans)
            )
            lines.append(
                f"This image is one line (system) of the score with {len(bar_spans)} bars, numbered {first_bar}-{last}. "
                f"Horizontal extent of each bar in this image (pixels): {spans}. Return exactly these bars."
            )
            if labeled_strings:
                lines.append(
                    "The small red numbers 1-6 at the left edge of the tab staff were added by the app, not part of "
                    "the score: they label the six tab lines (1 = top line, 6 = bottom line). Never transcribe them. "
                    "For every fret number, follow its line to the left to the red label to decide its string, instead "
                    "of counting lines by eye: a common mistake is putting a number one line too low (string 5 read "
                    "as string 6), especially bass notes and open strings (0) on the lower lines."
                )
        else:
            lines.append(
                f"This image is a whole page. Transcribe every bar on it in reading order, numbering from {first_bar}. "
                "If the page contains no guitar tablature, return an empty list."
            )
        lines.append(TRANSCRIBE_RULES)
        if previous is not None:
            lines.append(
                "Your previous transcription of this image is below, but it has problems:\n- "
                + "\n- ".join(problems or [])
                + "\nRe-read the image carefully and return a corrected transcription of all the bars.\n"
                + json.dumps(previous, ensure_ascii=False)
            )
        if hint:
            lines.append(f"The user reviewed an earlier transcription and says (in Chinese): {hint}")
        return self._call([block, {"type": "text", "text": "\n\n".join(lines)}], TRANSCRIBE_SCHEMA)

    def explain_bars(self, meta: dict, bars: list[dict]) -> dict:
        """给已经有精确数据的小节（GTP / MusicXML 导入）写练习提示，只发文字。"""

        lines = [bar_text(b) for b in bars]
        text = (
            f"Tuning (string 1 -> 6): {' '.join(meta['tuning'])}; capo {meta['capo']}; time signature "
            f"{meta['time_signature'][0]}/{meta['time_signature'][1]}. Below is exact tablature data for some bars: "
            + NOTATION_KEY
            + ".\n"
            + "\n".join(lines)
            + "\n\nFor every bar, write a tip in Chinese: 1-4 sentences of practical guidance naming strings and frets "
            "the way a teacher would (what to play together, what is fretted vs open, which technique and how to do it, "
            "what is hard and how to practise it). Return one entry per bar with its number."
        )
        return self._call([{"type": "text", "text": text}], EXPLAIN_SCHEMA, effort="medium", max_tokens=32000)

    def coach(
        self,
        meta: dict,
        bar: dict,
        neighbours: list[dict],
        question: str,
        history: list[dict],
        practice: str = "",
        image: Image.Image | None = None,
    ) -> str:
        """针对一个小节回答练习问题（指法、技巧、怎么练）。"""
        parts = [
            f"A guitarist is practising {meta.get('title') or 'a piece'} (tuning string 1 -> 6: {' '.join(meta['tuning'])}, "
            f"capo {meta['capo']}, time signature {meta['time_signature'][0]}/{meta['time_signature'][1]}) and asks about "
            f"bar {bar['n']}. Tablature data, " + NOTATION_KEY + ":",
            "This bar: " + bar_text(bar),
            "Neighbouring bars for context:\n" + "\n".join(bar_text(b) for b in neighbours)
            if neighbours
            else "",
            f"Practice tip already shown for this bar: {bar['tip']}" if bar.get("tip") else "",
            f"Their practice log for this bar: {practice}" if practice else "",
            (
                "The image is this bar cut from the original score; the data above was transcribed from it and "
                "may contain mistakes - if the image clearly disagrees, say so."
                if image is not None
                else ""
            ),
        ]
        for h in history[-4:]:
            parts.append(f"Earlier question: {h['q']}\nYour answer: {h['a']}")
        parts.append(
            f"Their question (in Chinese): {question}\n\nAnswer in Simplified Chinese like a patient guitar teacher: "
            "concrete fingering (which left-hand finger on which string/fret, right-hand p i m a where useful), what "
            "to listen for, and a short practice routine if it helps. Refer to notes as 'N 弦 M 品'. Be concise: "
            "a few short paragraphs or a short list, no headings."
        )
        content: list = []
        if image is not None:
            content.append(_image_block(image, min_width=900)[0])
        content.append({"type": "text", "text": "\n\n".join(p for p in parts if p)})
        return self._call(content, COACH_SCHEMA, effort="medium", max_tokens=8000).get("answer", "")

    def practice_plan(self, meta: dict, bars: list[dict]) -> dict:
        summary = [
            {
                "bar": b["n"],
                "techniques": sorted({t for e in b["events"] for n in e["notes"] for t in n["tech"]}),
                "tip": b["tip"],
            }
            for b in bars
        ]
        text = (
            f"A guitarist is learning this piece: {meta.get('title') or '(untitled)'}, tuning "
            f"{' '.join(meta['tuning'])}, capo {meta['capo']}. Per-bar notes from the transcription:\n"
            + json.dumps(summary, ensure_ascii=False)
            + "\n\nWrite a practice plan in Chinese. overview: 2-3 sentences on what makes this piece hard and how "
            "to approach it. steps: 4-8 steps in the order to practise, each covering a range of bars written like "
            '"6-11" (use the bar numbers above; the whole piece as the final step), with a short title and 1-3 '
            "sentences of concrete advice (which technique to isolate, which bars resemble each other, tempo tips)."
        )
        return self._call([{"type": "text", "text": text}], PLAN_SCHEMA, effort="medium", max_tokens=16000)
