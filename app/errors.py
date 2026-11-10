"""业务错误：消息是给用户看的中文说明，接口按 status 返回对应的 HTTP 状态码（见 main.py 的异常处理）。
模型调用的错误 llm.LLMError 也是 AppError（Upstream）。

各类同时继承对应的内置异常（ValueError / LookupError / RuntimeError），内部按内置类型捕获的地方照常工作。
"""

from __future__ import annotations


class AppError(Exception):
    status = 400


class Invalid(AppError, ValueError):
    """用户输入或文件内容不对，用户自己能改。"""

    status = 400


class NotFound(AppError, LookupError):
    status = 404

    def __init__(self, msg: str = "曲子不存在"):
        super().__init__(msg)


class BarNotFound(NotFound):
    """按 id 找不到小节：小节可能刚被重新识别，id 都变了。"""

    def __init__(self, bar_id: str = ""):
        super().__init__("小节不存在（可能刚被重新识别，请刷新页面）")
        self.bar_id = bar_id


class Busy(AppError, RuntimeError):
    """这首曲子已有任务在跑（识别、重新识别某一行、生成讲解同一时间只能有一个），或者这个小节正在识别、暂时不能改。
    423（Locked），和 Conflict 的 409 分开：前端只把 409 当成“别处改过了”（刷新、丢掉这个小节的撤销记录），
    423 只是提示稍后再试。"""

    status = 423


class Upstream(AppError, RuntimeError):
    """模型服务出错或没给出可用结果。"""

    status = 502


class Conflict(AppError, RuntimeError):
    """请求和曲子当前的状态对不上（别处已经改过、没有可以取消的任务）。"""

    status = 409


STALE_REV = "曲子在别处改过了，已刷新，请再改一次"


class CorruptSong(AppError, ValueError):
    """song.json 和备份都读不出来。"""

    status = 500

    def __init__(self, song_id: str, reason: str):
        super().__init__(f"曲子数据损坏，无法读取：{reason}")
        self.song_id = song_id


class NewerSchema(CorruptSong):
    """song.json 是更新版本的 tab-trainer 存的（schema_version 比这个版本认识的大）：不读，更不能按旧格式写回去
    （会悄悄丢掉新版本的字段）。曲库里列出来，说明要升级（不提示删除：数据是好的）。"""

    def __init__(self, song_id: str, version: int):
        AppError.__init__(self, f"这首曲子是更新版本的 tab-trainer 存的（格式 {version}），请升级后再打开")
        self.song_id = song_id
