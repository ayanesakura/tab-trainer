"""业务错误：消息是给用户看的中文说明，接口按 status 返回对应的 HTTP 状态码（见 main.py 的异常处理）。

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


class BarNotFound(NotFound):
    """按 id 找不到小节：小节可能刚被重新识别，id 都变了。"""

    def __init__(self, bar_id: str = ""):
        super().__init__("小节不存在（可能刚被重新识别，请刷新页面）")
        self.bar_id = bar_id


class Busy(AppError, RuntimeError):
    """这首曲子已有任务在跑（识别、重新识别某一行、生成讲解同一时间只能有一个）。"""

    status = 409


class Upstream(AppError, RuntimeError):
    """模型服务出错或没给出可用结果。"""

    status = 502
