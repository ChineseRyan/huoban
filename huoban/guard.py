"""用户输入的正则与可中断搜索的护栏。

这一版**只交付工具 + 规范**，没有任何调用方：现有正则全是内部可信输入，
不为了用它去套一层（见 `todo/0.3.0.md` 的「用户输入的正则」一节）。插件期
（1.x 有用户规则入口之后）由插件层直接调用这里的两个东西。

两条规范（插件条目里必须写明，实现就在这里）：

1. 用户可填的正则 / 参数，**落盘前必须试一次**，坏了当场报「位置 + 原因」且不保存
   —— 见 `compile_checked`。
2. 用户可触发的全书搜索 / 正则，**必须有超时与上限**，超限要明确说出停在哪，
   不许静默停、不许静默禁用 —— 见 `Budget`。

为什么不用 signal 做超时：Windows 上没有可靠的实现（`signal.setitimer` 不存在，
`SIGALRM` 没有），拿不到的东西不许写进设计。所以超时是**协作式**的：
调用方在自己的循环里调 `tick()`，到线就抛。

库代码只返回数据、不 print：本模块一个 print 都没有，也不抛 NotImplemented 之外
的意外异常（`compile_checked` 对任何输入都只返回）。
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable

__all__ = ["compile_checked", "Budget", "BudgetExceeded", "explain_re_error"]


# Python 的 re.error 消息是英文，而且相当一部分是"机翻味"的短语。
# 用户看到的必须是「哪里错了、怎么改」，不是 "missing ), unterminated subpattern"。
_REASON_HINTS: tuple[tuple[str, str], ...] = (
    ("missing ), unterminated subpattern", "括号没闭合，少了一个右括号"),
    ("unbalanced parenthesis", "多了一个右括号"),
    ("unterminated character set", "方括号没闭合，少了一个右方括号"),
    ("bad character range", "字符范围写反了（比如 z-a），要从小的写到大的"),
    ("nothing to repeat", "量词前面没有东西可以重复（比如直接以 * 或 + 开头）"),
    ("multiple repeat", "量词叠在一起了（比如 a**）"),
    ("bad escape", "反斜杠后面的转义不认识"),
    ("unexpected end of pattern", "正则写到一半就断了"),
    ("bad group name", "分组的名字不合法（只能用字母、数字、下划线）"),
    ("redefinition of group name", "同一个分组名字用了两次"),
    ("invalid group reference", "引用了不存在的分组编号"),
    ("unknown extension", "括号里的扩展写法不认识（比如 (?< 开头的）"),
    ("invalid group name", "分组的名字不合法"),
    ("cannot refer to an open group", "分组还没闭合就被引用了"),
    ("look-behind requires fixed-width pattern", "后顾断言只能是固定宽度，不能用 * 或 +"),
    ("global flags not at the start of the expression", "内联开关（(?i) 这类）只能写在最前面"),
)


def explain_re_error(error: re.error) -> str:
    """把 `re.error` 翻成一句人话。认不出来的就如实给原文，不硬编一句假话。"""
    message = str(getattr(error, "msg", "") or error)
    for needle, plain in _REASON_HINTS:
        if needle in message:
            return plain
    return f"正则语法有问题：{message}"


def _line_col(pattern: str, pos: int | None) -> tuple[int, int] | None:
    """把 `re.error.pos`（0 起下标）换算成 (行, 列)，都是 1 起。

    正则可能是多行的（插件里贴一段进来很常见），只说"第 17 个字符"没法让人
    在自己的编辑器里找到它。
    """
    if pos is None or pos < 0 or pos > len(pattern):
        return None
    line = pattern.count("\n", 0, pos) + 1
    last_break = pattern.rfind("\n", 0, pos)
    column = pos - (last_break + 1) + 1
    return (line, column)


def compile_checked(pattern, flags: int = 0):
    """试编译用户填的正则：**只返回，不抛**。

    返回三元组 `(编译好的正则 | None, 人话原因, (行, 列) | None)`：
    - 成功：`(compiled, "", None)`；
    - 失败：`(None, "方括号没闭合…", (2, 1))` —— 原因与位置都给，界面直接显示；
    - 类型不对（None / 数字 / 字节）：`(None, "正则必须是文本…", None)`，
      位置无意义，给 None。

    为什么不在坏的时候抛异常：这是"用户填错了"的常规路径，不是程序出错。
    调用方要拿结果决定「保存按钮置灰 + 红字提示」，try/except 会把这条常规
    路径写成分支。
    """
    if not isinstance(pattern, str):
        return (
            None,
            f"正则必须是一段文本，收到的是 {type(pattern).__name__}",
            None,
        )
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        position = _line_col(pattern, getattr(exc, "pos", None))
        return (None, explain_re_error(exc), position)
    except (TypeError, ValueError, OverflowError) as exc:
        # flags 传了非法值这类调用方错误：同样只返回，把原文带上便于定位。
        return (None, f"正则编译不了：{exc}", None)
    return (compiled, "", None)


class BudgetExceeded(Exception):
    """到线了。**这是"说出来"而不是"静默停"的唯一出口。**

    `reason` 是给用户看的一句话；`done` 是已处理量 —— 用户要能判断
    "是这本书太大，还是我的正则写坏了"。
    """

    def __init__(self, reason: str, done: int, limit: int, unit: str) -> None:
        self.reason = reason
        self.done = done
        self.limit = limit
        self.unit = unit
        super().__init__(f"{reason}：已处理 {done} {unit}（上限 {limit}）")


class Budget:
    """协作式的时间 / 数量预算：调用方在循环里 `tick()`，到线抛 `BudgetExceeded`。

    为什么是协作式而不是 signal：Windows 上没有可靠的 signal 超时实现，
    而本项目只跑在 Windows（见模块 docstring）。

    两个上限各自独立，**0 表示不限**：
      - `seconds`：从构造那一刻起算的墙钟时间；
      - `max_hits`：累计处理量上限（`tick(n)` 的 n 之和）。

    用法（插件期）：

        budget = Budget(seconds=3.0, max_hits=100_000)
        for line in lines:
            hit = pattern.search(line)
            budget.tick()          # 到线就抛，带着原因与已处理量
            ...

    别把 `tick()` 放进热内层的每一字符上：它每次要看一次 `perf_counter`，
    一行查一次是合适的粒度。
    """

    def __init__(self, seconds: float = 0.0, max_hits: int = 0) -> None:
        self.seconds = float(seconds or 0.0)
        self.max_hits = int(max_hits or 0)
        self.done = 0
        self._started = time.perf_counter()
        self._raised = False

    @property
    def exceeded(self) -> bool:
        """是否已经到线（抛过之后为真）。给调用方做收尾判断用。"""
        if self._raised:
            return True
        if self.max_hits and self.done > self.max_hits:
            return True
        return bool(self.seconds and self.elapsed > self.seconds)

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._started

    def tick(self, n: int = 1) -> None:
        """报一次进度。到线抛 `BudgetExceeded`，并把 `done` 记到当下。"""
        self.done += int(n)
        if self.max_hits and self.done > self.max_hits:
            self._raised = True
            raise BudgetExceeded("搜索超过数量上限", self.done, self.max_hits, "项")
        if self.seconds and self.elapsed > self.seconds:
            self._raised = True
            raise BudgetExceeded(
                f"搜索超时（{self.seconds:g} 秒）", self.done, self.max_hits or 0, "项"
            )

    def reset(self) -> None:
        """重新起算（同一份预算给下一次搜索复用）。不改两个上限值。"""
        self.done = 0
        self._started = time.perf_counter()
        self._raised = False


def iter_budgeted(items: Iterable, budget: Budget) -> Iterable:
    """示例性的写法，不是硬要求：逐个产出，同时把预算往前推一步。

    插件作者可以直接用，也可以自己在循环里 `tick()` —— 只要**有一次** tick
    落在可能很慢的操作旁边就行。
    """
    for item in items:
        budget.tick()
        yield item
