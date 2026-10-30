"""日志兜底：不依赖标准库日志模块，也不依赖 astrbot。

为什么需要它：AstrBot 上架规则要求 logger 必须且只能来自 `from astrbot.api import logger`，
各模块不允许自行创建日志器。但这些模块又被设计成「纯标准库、可脱离 astrbot 单测」，
所以这里提供一个零依赖的静默实现兜底，运行期由 main.py 注入 astrbot 的 logger。

用法：
    class Mailer:
        def __init__(self, ..., logger=None):
            self._log = logger or SILENT
"""


class SilentLogger:
    """什么都不做的 logger，只在没人注入时兜底，避免把可测性换成 crash。"""

    def debug(self, *args, **kwargs):
        pass

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


SILENT = SilentLogger()
