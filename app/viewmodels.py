"""接口返回的形状（Pydantic，只用来写清楚、在测试里校验 app/views.py 组装出来的结果；不存盘）。
存盘的结构（Song / Bar …）在 app/model.py，这里的模型在它们上面加派生字段（BarView）或者另起（LibraryItem）。
字段说明另见 README 的接口约定。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from app.model import (
    _M,
    Activity,
    Bar,
    Derived,
    Orig,
    Page,
    Progress,
    Refined,
    SourceType,
    Status,
    System,
    Warn,
)


class RefineEstimate(_M):
    """精修的范围和预估：几个小节、大约花多少钱和时间（app/refine.py 的 estimate / scope_estimates）。"""

    bars: int = 0
    cost_usd: float = 0
    minutes: int = 0  # 向上取整，至少 1
    seconds: int = 0  # 按几个小节并行估的秒数（小节卡片上显示成“1–2 分钟”）


class RefineScopes(_M):
    """小节卡片上“精修这个小节 / 这一行”的预估：bar 是一个小节的；rows 按行，裁不出单个小节的行是 null。"""

    bar: RefineEstimate
    rows: list[RefineEstimate | None] = Field(default_factory=list)


class PracticeBar(_M):
    loops: int = 0  # 跟着谱子练循环了几遍（敲节奏不算）
    seconds: float = 0  # 练了多少秒（敲节奏也算）
    best_bpm: int = 0
    last: str = ""
    clean: int = 0  # 标了干净的遍数
    clean_bpm: int = 0  # 标了干净的最快速度
    clean_streak: int = 0  # 最近连着干净、速度都是 clean_streak_bpm 的遍数（见 practice._clean）
    clean_streak_bpm: int = 0  # 最近标过的那一遍的速度


class PracticeLast(_M):
    a: str
    b: str
    bpm: int = 0
    at: str = ""
    a_n: int | None = None  # 现在的小节位置（1 起）；小节不存在了是 null
    b_n: int | None = None


class PracticeTime(_M):
    total_seconds: float = 0  # 一共练了多久：有日期的合并重叠 + undated_seconds（practice.time_of）
    undated_seconds: float = 0  # 其中没有日期的（旧版本的练习记录）


class PracticeView(_M):
    bars: dict[str, PracticeBar] = Field(default_factory=dict)
    last: PracticeLast | None = None
    total_seconds: float = 0  # 每段记下的秒数直接相加（不去重，旧字段）；练了多久看 time
    time: PracticeTime = Field(default_factory=PracticeTime)


StudyState = Literal["new", "learning", "review"]


class StudyPreview(_M):
    state: StudyState
    due_in: int | None = None


class StudyLast(_M):
    day: str
    rating: int
    issues: list[str] = Field(default_factory=list)
    note: str = ""
    bpm: int = 0


class StuckBar(_M):
    id: str
    n: int  # 现在是第几小节（1 起）


class StudyView(_M):
    """曲子在练琴计划里的状态（app/study.py 的 view）：按自评记录重放 FSRS 算出来的。"""

    state: StudyState
    paused: bool = False
    order: int = 0
    added: str = ""
    due: str | None = None  # 下次该练的练琴日；新曲子是 null
    due_in: int | None = None  # 离今天几天，负数 = 过期了
    last: StudyLast | None = None
    rated_today: int | None = None
    reps: int = 0  # 评过几天
    lapses: int = 0  # 复习时卡住、退回学习的次数
    stability: float | None = None
    difficulty: float | None = None
    preview: dict[str, StudyPreview]  # 今天评 1–4 分别会怎样
    stuck_bars: list[StuckBar] = Field(
        default_factory=list
    )  # 最近一次自评时卡住的小节（还在曲子里的，按小节顺序）


class BarView(Bar):
    orig: Orig | None = Field(None, exclude=True)  # _orig 只是撤销基线，不给前端
    n: int
    needs_review: bool = False  # 有警告或模型标了不确定、用户还没确认
    reviewed: bool = False  # 有警告或不确定说明，但用户已经确认没问题（内容没再变）
    reviewed_key: str | None = Field(None, exclude=True)  # 内部字段，不给前端
    reviewed_rev: int | None = Field(None, exclude=True)
    reviewed_warn: list[str] | None = Field(None, exclude=True)
    refine_key: str | None = Field(None, exclude=True)
    locked: str | None = None  # 现在不能改的原因
    refinable: bool = False  # 能单独裁出来精修（小节卡片上显示“精修这个小节”，见 refine.refinable）
    warn: list[Warn] = Field(default_factory=list)


class SongView(_M):
    """GET /api/songs/{id} 的返回。"""

    id: str
    title: str
    created: str = ""
    imported: str | None = None
    source_type: SourceType
    source: str | None = None
    status: Status
    rev: int  # 保存次数（只用来判断要不要重绘，不用于 409）
    meta_rev: int  # 改谱头的 PUT 带这个
    busy: bool  # 有任何后台任务（整首识别 / 单行重识别 / 讲解）
    recognizing: bool  # 整首识别或单行重识别在进行（讲解不算）
    activity: Activity | None = None
    can_edit_bars: bool
    editable_meta: list[str]
    notice: dict | None = None  # {kind: warn|error, msg}
    progress: Progress | None = None
    meta: dict | None = None  # 含派生的 title、tuning_midi
    pages: list[Page] = Field(default_factory=list)
    systems: list[System] = Field(default_factory=list)
    bars: list[BarView] = Field(default_factory=list)
    plan: dict | None = None  # 含派生的 stale
    practice: PracticeView
    usage: dict = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    track: int = 0
    derived: Derived | None = None
    is_score: bool = False  # Guitar Pro / MusicXML（小节来自原文件）
    failed_rows: int = 0  # 识别失败的行数
    refined: Refined | None = None  # 上一次精修整首的结果
    refine_estimate: RefineEstimate | None = None  # 图片曲子：现在精修整首的范围和预估；GP 是 null
    refine_scopes: RefineScopes | None = None  # 图片曲子：精修一个小节 / 一行的预估；GP 是 null
    study: StudyView | None = None  # 不在练琴计划里是 null
    archived: bool = False  # 归档了（不排进今天的练琴清单）


class LibraryPractice(_M):
    last_at: str = ""
    total_seconds: float = 0  # 同 PracticeView.total_seconds
    undated_seconds: float = 0
    last: PracticeLast | None = None


class LibraryItem(_M):
    """GET /api/songs 的一项。"""

    id: str
    title: str
    artist: str = ""
    status: Status | Literal["corrupt"]
    newer: bool = (
        False  # status 是 corrupt 时：更新版本的 tab-trainer 存的（数据是好的，升级后就能打开，不提示删除）
    )
    progress: Progress | None = None
    activity: Activity | None = None
    busy: bool = False
    recognizing: bool = False
    notice: dict | None = None
    bars: int = 0
    needs_review_bars: int = 0
    created: str | None = None
    imported: str | None = None
    source_type: SourceType | None = None
    is_score: bool = False
    failed_rows: int = 0
    tags: list[str] = Field(default_factory=list)
    practice: LibraryPractice | None = None
    cost_usd: float = 0
    cost_estimated: bool = False
    unpriced_tokens: int = 0
    tokens: int = 0
    refined: Refined | None = None
    refine_estimate: RefineEstimate | None = None
    study: StudyView | None = None
    archived: bool = False
