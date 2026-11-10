"""给模型的提示词和输出格式：JSON Schema（音符 / 事件从 app/model.py 生成）、系统提示、识别规则、小节的文字版。
app/llm.py 的 Claude 客户端拼请求时用。改这里会改变发给模型的内容：tests/test_prompts.py 的快照会提醒你核对。"""

from __future__ import annotations

import json

from app import model
from app.music import PROMPT_DIRECTIONS


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


# 音符和事件的 schema 由 app/model.py 的 Note / Event 定义生成（tests/test_model.py 断言和原来手写的逐字一致）
NOTE_SCHEMA = model.llm_schema(model.Note)
EVENT_SCHEMA = model.llm_schema(model.Event)
DIRECTIONS = list(PROMPT_DIRECTIONS)  # vocab.json 里 prompt 为 true 的反复记号
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
CHORD_SCHEMA = _obj(
    {
        "name": {"type": "string", "description": 'the chord name exactly as printed, e.g. "Dsus2/G"'},
        "frets": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "the diagram as actual frets for strings 1 (highest) to 6: 0 = open (o), -1 = not "
            'played (x above the diagram); add the position marker ("3fr." = the top row of the grid is fret 3). '
            "A position marker is printed just left of the grid it belongs to, so it can sit close to the "
            "previous diagram - it never belongs to the diagram on its left",
        },
    }
)
TRANSCRIBE_SCHEMA = _obj(
    {
        "bars": {"type": "array", "items": BAR_SCHEMA},
        "chords": {
            "type": "array",
            "items": CHORD_SCHEMA,
            "description": "every chord diagram printed in this image (each name once); [] if there are none",
        },
    }
)

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
- Fret numbers in parentheses, e.g. (7), are either tied continuations or ghost notes; decide by the tie arc:
  - Tied continuation: a tie arc (a curve) ends at it, coming from the same fret on the same string - drawn in the tab, or in the standard notation above, where the tied-to notehead is NOT in parentheses. Tablature only re-writes a tied note at the start of a bar (tied over the barline) or at the start of a line (the arc comes in from the left edge; the previous line is not in this image); a tie inside a bar is not written in the tab at all. It is not a new onset: leave it out, and if no other note starts at that time, write an event with an empty "notes" list for it.
  - Ghost note: no tie arc ends at it. It is a real, softly played note: write it as a normal note. This includes a parenthesized number in the middle of a bar, repeated parenthesized notes on the same fret (e.g. 5 (5) (5) are three onsets), and a note whose notehead in the standard notation is itself in parentheses.
- Beamed groups do not always line up with beats. A group can contain a note that crosses the beat (e.g. eighth + sixteenth + eighth = 1 1/4 beats) and the next group then starts off the beat. Take each note's value from its own stem (one beam or flag = eighth, two = sixteenth, three = 32nd; a short beam stub belongs to the note it touches; a dot adds half) and add them up, instead of assuming every beamed group fills exactly one beat.

Techniques (put them on the note that receives them):
- "h": hammer-on into this note (arc + H from the previous note on the same string). "p": pull-off into this note.
- "sl": slide into this note from the previous note on the same string; this note is not re-picked.
- "si": slide in from an unspecified lower fret (a slash before the number with no starting note); the note is picked.
- "pm": palm mute (P.M. line). "x": dead/muted note written as x (use fret 0) - but not in chord sheets, see below.
- "nh": natural harmonic, written <n>; fret = the harmonic node n. A natural harmonic written with a fractional node, e.g. <21.7> or <14.7>, gets fret = the node rounded to the nearest whole number (22, 15). "ah": artificial harmonic (A.H., e.g. "2<14>"): fret = the fretted note (2), the node is 12 frets higher.
- "th": tapped harmonic, labelled T.H. above the staff and written as a fretted number followed by the tapped fret in <>, e.g. "0 <19>" or "5<22>" (the left hand frets the first number, the right hand taps the fret in <>): fret = the fretted number (0, 5), tech "th", "harm" = the number in <> (19, 22; a fractional one such as <23.7> rounded to the nearest whole number, 24). Every note that is not a tapped harmonic has harm = 0.
- "roll": arpeggiated chord (wavy vertical line) - put it on every note of the chord.
- "vib": vibrato. "bend": a bend; also set "bend" to the size in semitones (full = 2, 1/2 = 1).
- Circled numbers ①-⑥ next to notes (common in classical scores) are string indications - which string the note is played on - not frets and not notes. In a header, "①=D ②=A ..." lists the tuning of each string.
- A small grace note before a note: set "grace" on the main note to the grace note's fret (and add "h"/"sl"/"p" if the grace connects by that technique). Otherwise grace = -1.

Chord sheets (accompaniment scores with chord names and chord diagrams printed above the tab, often with numbered-notation melody and lyrics below - ignore the melody and lyrics):
- First read every chord diagram in "chords": frets for strings 1-6, with the "Nfr." position marker added.
- "chord": on the event where a chord name starts, write that name; otherwise "". A name is printed above (often centered over) the onset where the chord begins; when it sits between two onsets, choose the one on the stronger beat or where a new bass note / strum starts - chords rarely change half a beat late.
- Small x marks on the tab lines are NOT dead notes here: each x means "play this string with the current chord" (the last chord name at or before this point, which may come from an earlier bar). Write a normal note with the fret of that string from the chord's diagram, no "x" technique. A fret number written on the line instead of an x is played as written (it overrides the diagram).
- A vertical arrow drawn across the strings is a strum: one event whose notes are the strings the arrow covers, each with the fret from the current chord diagram (leave out strings the diagram marks as not played). "strum": "down" when the arrowhead points to the top line (string 1) - a downstroke that starts on the low strings; "up" when it points to the bottom line. Its duration comes from the stem and beams below the tab like any other note. A wavy arrow is a rolled chord: also add "roll" to every note.
- A circled x (or x marks with a slash, often on several strings at once) is a percussive slap/hit: write those strings with tech "x" and fret 0.
- An x on a string that the current diagram marks as not played: a single bass x in an arpeggio pattern is an alternate bass note (use the usual alternate-bass fret for that chord, e.g. string 6 fret 3 under C); x marks stacked on the lowest two strings and hit together, between chord strokes, are a slap (tech "x", fret 0) - especially when the score labels them 拍弦 / 打板 / 切音 anywhere (see the notes about this score).

Lyrics (scores for singing, usually with a numbered-notation melody line and the lyrics under it):
- Put each syllable on the guitar event that is sounding when it is sung: use the melody's rhythm (its beat positions in the bar) to place the syllable in time, then attach it to the last guitar onset at or before that time. When several syllables fall on the same guitar event, join them in order ("天真").
- "lyrics" holds one string per printed verse line, verse 1 first (e.g. ["天真", "多浅"] when two verses are printed one under the other); [] on events where no syllable starts, and everywhere when the score has no lyrics. Copy the characters exactly; leave out punctuation.

Repeats and navigation (the app follows them during playback):
- repeat_start / repeat_end: repeat barlines. repeat_end is the total number of times the section is played (2 for a plain :| sign).
- endings: the numbers of the 1st/2nd-ending (volta) bracket over this bar.
- directions, at the bar where they are written: "segno" (the 𝄋 sign), "coda" (the 𝄌 target, "To Coda" jumps here), "fine" (Fine, where the piece ends after a D.C./D.S.), "dacapo" (D.C.), "dacapoalfine" (D.C. al Fine), "dacapoalcoda" (D.C. al Coda), "dalsegno" (D.S.), "dalsegnoalfine", "dalsegnoalcoda", "dacoda" (the "To Coda" / ⊕ jump point).

"tip": 1-4 sentences of practical Chinese guidance for this bar, naming strings and frets the way a teacher would, e.g. "5 弦空弦 + 2 弦空弦一起弹，2 弦击弦到 2 品再勾弦回空弦（只拨一次）。第 3 拍和弦：2 弦 4 品、3 弦 2 品、4 弦 3 品；第 4 拍三连音：4 弦 3 品、3 弦空弦、2 弦空弦。" Mention what is hard and how to practise it when it helps.
"uncertain": in Chinese, only doubts that could make the player play something wrong - a note, string, fret, technique or rhythm in this bar that you could not read with confidence and that the user should check against the image (say which beat and what you chose). Leave it empty when the notes are clear, including when you only made a routine reading decision. Do not mention: ties, slurs or slaps that are clearly drawn and that you simply followed, parts of a chord diagram on strings that are not played in this bar, exactly where a chord name sits when the chord change is clear from the music, notes you played as written, a reading that follows standard notation conventions, or anything about the melody or lyrics lines."""


PING = 'Reply with {"title": "ok", ...} filling other fields with empty values.'

READ_META = (
    "This is the first page of a guitar score. Read its header and return:\n"
    "- title / artist / arranger (empty string if absent)\n"
    "- tuning: 6 note names with octave, for string 1 (highest) to string 6 (lowest), e.g. "
    '["E4","B3","G3","D3","A2","E2"]. Tuning text such as "DADGAD" or "Tuning=DAC#F#AD" lists strings '
    "from 6 (lowest) to 1. If no tuning is written, assume standard tuning.\n"
    "- capo: the capo fret, 0 if none.\n"
    "- time_signature: [numerator, denominator] of the first bar, e.g. [4, 4].\n"
    "- tempo: quarter-note BPM if written, else 0.\n"
    "- notes: in Chinese, anything a player should know (e.g. tuning changes, the written capo looking "
    "inconsistent with the standard-notation pitches), and the notation conventions this score uses that "
    "matter for reading every line - for example x marks meaning chord tones of the diagrams above, a "
    "pattern of x marks labelled once as a slap / percussive hit (拍弦, 打板, 切音) that later lines repeat "
    "without the label, or what a special symbol means. Empty string if nothing."
)

LABELED_STRINGS = (
    "The small red numbers 1-6 at the left edge of the tab staff were added by the app, not part of "
    "the score: they label the six tab lines (1 = top line, 6 = bottom line). Never transcribe them. "
    "For every fret number, follow its line to the left to the red label to decide its string, instead "
    "of counting lines by eye: a common mistake is putting a number one line too low (string 5 read "
    "as string 6), especially bass notes and open strings (0) on the lower lines."
)


def score_notes(meta: dict) -> str:
    """首页读谱头时记下的说明（记谱约定、标在别处的拍弦 / 切音等）：每一行、每个小节都是单独发给模型的，
    看不到别的行。没有说明时是空字符串。"""
    notes = (meta.get("notes") or "").strip()
    if not notes:
        return ""
    return (
        "Notes about this score, written when its first page was read (they apply to every line, including "
        f"marks that are only labelled once in the score): {notes}"
    )


def transcribe_text(
    meta: dict,
    first_bar: int,
    bar_spans: list[tuple[int, int]] | None,
    scale: float,
    hint: str = "",
    previous: dict | None = None,
    problems: list[str] | None = None,
    labeled_strings: bool = False,
    start_ts: list[int] | None = None,
    context: str = "",
) -> str:
    """识别一行（或整页）的文字部分。bar_spans 是原图上的小节区间，scale 是发出去的图相对原图的缩放。
    其余参数见 llm.Claude.transcribe。"""
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
    if notes := score_notes(meta):
        lines.append(notes)
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
            lines.append(LABELED_STRINGS)
    else:
        lines.append(
            f"This image is a whole page. Transcribe every bar on it in reading order, numbering from {first_bar}. "
            "If the page contains no guitar tablature, return an empty list."
        )
    if context:
        lines.append(context)
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
    return "\n\n".join(lines)


def right_half_context(context: str, chord: str | None, frets: list[int] | None) -> str:
    """一行切成两半识别时，右半的补充说明：左半结束时还在响的和弦（右半开头的 x 按它换算；frets 是左半读出的
    它的和弦图），只返回列出的小节。context：整行本来就有的补充说明。"""
    notes = [context] if context else []
    if chord:
        diagram = f" (diagram frets for strings 1-6: {frets})" if frets is not None else ""
        notes.append(
            f"This image is the right half of a line. The chord still sounding at its left edge is {chord}{diagram}: "
            "x marks before the first chord name in this image belong to it. Return only the bars listed above."
        )
    else:
        notes.append(
            "This image is the right half of a line; the left half was read separately. "
            "Return only the bars listed above."
        )
    return " ".join(notes)


def shape_consensus_context(odd: dict[str, list[int]]) -> str:
    """按全曲多数行的读法重读一行时的补充说明：这一行读得和多数不一样的和弦图（和弦名 → 1–6 弦品位）。"""
    return (
        "The chord diagrams below were read the same way in the other lines of this score; use them "
        "(and resolve the x marks with them): "
        + "; ".join(f"{k} = frets for strings 1-6 {v}" for k, v in odd.items())
    )


def explain_text(meta: dict, bars: list[dict]) -> str:
    """给已有精确数据的小节写练习提示（只发文字）。"""
    lines = [bar_text(b) for b in bars]
    return (
        f"Tuning (string 1 -> 6): {' '.join(meta['tuning'])}; capo {meta['capo']}; time signature "
        f"{meta['time_signature'][0]}/{meta['time_signature'][1]}. Below is exact tablature data for some bars: "
        + NOTATION_KEY
        + ".\n"
        + "\n".join(lines)
        + "\n\nFor every bar, write a tip in Chinese: 1-4 sentences of practical guidance naming strings and frets "
        "the way a teacher would (what to play together, what is fretted vs open, which technique and how to do it, "
        "what is hard and how to practise it). Return one entry per bar with its number."
    )


def coach_text(
    meta: dict,
    bar: dict,
    neighbours: list[dict],
    question: str,
    history: list[dict],
    practice: str = "",
    with_image: bool = False,
) -> str:
    """小节问答（纯文字回答）。with_image：同时发了这个小节的原谱裁图。"""
    parts = [
        f"A guitarist is practising {meta.get('title') or 'a piece'} (tuning string 1 -> 6: {' '.join(meta['tuning'])}, "
        f"capo {meta['capo']}, time signature {meta['time_signature'][0]}/{meta['time_signature'][1]}) and asks about "
        f"bar {bar['n']}. Tablature data, " + NOTATION_KEY + ":",
        "This bar: " + bar_text(bar),
        "Neighbouring bars for context:\n" + "\n".join(bar_text(b) for b in neighbours) if neighbours else "",
        f"Practice tip already shown for this bar: {bar['tip']}" if bar.get("tip") else "",
        f"Their practice log for this bar: {practice}" if practice else "",
        (
            "The image is this bar cut from the original score; the data above was transcribed from it and "
            "may contain mistakes - if the image clearly disagrees, say so."
            if with_image
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
    return "\n\n".join(p for p in parts if p)


def plan_text(meta: dict, bars: list[dict]) -> str:
    """根据识别结果写分段练习建议。"""
    summary = [
        {
            "bar": b["n"],
            "techniques": sorted({t for e in b["events"] for n in e["notes"] for t in n["tech"]}),
            "tip": b["tip"],
        }
        for b in bars
    ]
    return (
        f"A guitarist is learning this piece: {meta.get('title') or '(untitled)'}, tuning "
        f"{' '.join(meta['tuning'])}, capo {meta['capo']}. Per-bar notes from the transcription:\n"
        + json.dumps(summary, ensure_ascii=False)
        + "\n\nWrite a practice plan in Chinese. overview: 2-3 sentences on what makes this piece hard and how "
        "to approach it. steps: 4-8 steps in the order to practise, each covering a range of bars written like "
        '"6-11" (use the bar numbers above; the whole piece as the final step), with a short title and 1-3 '
        "sentences of concrete advice (which technique to isolate, which bars resemble each other, tempo tips)."
    )


def _event_text(e: dict) -> str:
    head = (f"[{e['chord']}] " if e.get("chord") else "") + f"{e['dur']}拍 "
    if e.get("lyrics") and e["lyrics"][0]:
        head += f"「{e['lyrics'][0]}」"
    strum = {"down": "下扫 ", "up": "上扫 "}.get(e.get("strum", ""), "")
    return head + strum + (" ".join(_note_text(n) for n in e["notes"]) or "休止")


def _note_text(n: dict) -> str:
    s = f"{n['string']}弦{n['fret']}品"
    if n.get("grace", -1) >= 0:
        s = f"(倚音{n['grace']}品)" + s
    if n.get("harm"):  # 点弦泛音：右手点的品，和谱上一样写在 <> 里
        s += f"<{n['harm']}>"
    return s + (f"[{','.join(n['tech'])}]" if n.get("tech") else "")


def bar_text(b: dict) -> str:
    """一个小节的文字版：'第N小节: <时值>拍 <音> | ...'，给只发文字的调用用。"""
    ts = b.get("time_signature")  # 只有和全曲拍号不同的小节才有
    head = f"第{b['n']}小节" + (f"（{ts[0]}/{ts[1]} 拍）" if ts else "")
    return head + ": " + " | ".join(_event_text(e) for e in b["events"])


NOTATION_KEY = (
    "each event is '<duration in quarter notes> <notes>', notes as '<string>弦<fret>品' (string 1 = highest), "
    "a chord name in [brackets] starts that chord, 下扫/上扫 = strummed down/up, 「」 = lyric sung there, "
    "techniques in brackets (h hammer-on, p pull-off, sl slide from the previous note, si slide in, pm palm mute, "
    "nh natural harmonic, ah artificial harmonic, th tapped harmonic (<n> = the fret tapped by the right hand), roll arpeggio, x dead note, vib vibrato, bend)"
)


# ---------------------------------------------------------------------------
# 精修：逐小节放大核对（app/refine.py、Claude.verify_bar）。单独放在这里，和上面的识别提示词分开改
# ---------------------------------------------------------------------------
VERIFY_SCHEMA = _obj(
    {
        "diff": {"type": "string", "description": "in Chinese: every difference you found, or empty if none"},
        "changed": {"type": "boolean"},
        "events": {"type": "array", "items": EVENT_SCHEMA},
        "uncertain": {
            "type": "string",
            "description": 'in Chinese, following the rules for "uncertain" above, about the bar as returned in '
            "events: only doubts that could make the player play something wrong; empty when the notes are clear",
        },
    }
)

# {a} / {b}：要核对的小节在放大后的图里的左右边界（像素）
VERIFY_TASK = (
    "The image shows ONE bar of a guitar score, enlarged: the bar to check lies between x={a} and "
    "x={b} pixels; the edges of the neighbouring bars are visible only for context (ties coming "
    "in, beams). Below is the current transcription of this bar. Check it against the image beat by beat: "
    "every fret number and its string, every onset and every duration (stems, beams, flags, dots, tuplets, "
    "rests), parenthesized numbers (tie or ghost, see the rules), techniques. If everything matches, set "
    "changed=false and return the events unchanged. If anything differs, set changed=true, describe each "
    "difference in diff, and return the corrected events for the whole bar."
)

VERIFY_EVENT_KEYS = ("dur", "notes", "chord", "strum", "lyrics")


def verify_text(
    meta: dict,
    span: tuple[float, float],
    events: list[dict],
    previous: list[dict] | None = None,
    time_signature: list[int] | None = None,
    hint: str = "",
) -> str:
    """精修一个小节的文字部分。span：小节在发出去的（已放大的）图里的左右边界；其余见 llm.Claude.verify_bar。"""
    ts = time_signature or meta["time_signature"]
    length = ts[0] * 4 / ts[1]
    a, b = span
    text = [
        (
            f"Score info: tuning (string 1 -> 6) {' '.join(meta['tuning'])}; capo {meta['capo']}. "
            f"Time signature {ts[0]}/{ts[1]}: this bar lasts {length:g} quarter notes."
        ),
        VERIFY_TASK.format(a=round(a), b=round(b)),
        TRANSCRIBE_RULES,
        "Current transcription of this bar (events in time order):\n"
        + json.dumps(verify_events(events), ensure_ascii=False),
    ]
    if previous is not None:
        text.append(
            "For context only, the previous bar was read as:\n"
            + json.dumps(verify_events(previous), ensure_ascii=False)
        )
    if hint:
        text.append(f"The user reviewed this bar and says (in Chinese): {hint}")
    return "\n\n".join(text)


def verify_events(events: list[dict]) -> list[dict]:
    """发给模型核对的小节内容：只留事件里模型输出的那几个字段（和识别时模型给出的一样）。"""
    return [{k: v for k, v in e.items() if k in VERIFY_EVENT_KEYS} for e in events]
