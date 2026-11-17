"""guard.py 的测试：用户输入的正则护栏与可中断搜索。

照项目惯例：一个 case 一个函数，纯 assert，直接 `python tests/test_guard.py` 跑。
每个 case 都对着「用户会怎么被坑」来写，不对着实现细节写。

为什么要有这条护栏（`todo/0.3.0.md` 的「用户输入的正则」一节）：竞品对用户填的
正则**做了**试编译，可它的样式正则漏了，坏着也能存，出书时被静默禁用 ——
用户以为生效了。反向的坑是全书搜索没有上限，一条灾难性回溯的正则能把界面
拖死。所以两条规范都要能用假的坏输入验出来。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from huoban.guard import Budget, BudgetExceeded, compile_checked  # noqa: E402


# ---------------------------------------------------------------------------
# 坏正则：要给出「位置 + 原因」，不许静默变成 None
# ---------------------------------------------------------------------------

def case_bad_pattern_reports_line_and_column():
    """方括号没闭合：原因说人话，位置换算到行列（不是原始下标）。"""
    pattern = "第一行\n[abc"
    compiled, reason, position = compile_checked(pattern)
    assert compiled is None, "坏正则居然编译成功了"
    assert reason, "坏正则没给原因"
    assert "方括号" in reason, f"原因不是人话：{reason}"
    assert position == (2, 1), f"位置该是第 2 行第 1 列，实际 {position}"


def case_bad_pattern_position_is_line_one_column_n():
    """同一行里的多处错误：列号要指到错的那个字符。"""
    pattern = "abc(def"
    compiled, reason, position = compile_checked(pattern)
    assert compiled is None
    assert position is not None and position[0] == 1, position
    assert position[1] == 4, f"该指到第 4 列（左括号），实际 {position[1]}"


def case_bad_pattern_never_raises():
    """不管多离谱的输入都只返回，不抛 —— 前端要拿它直接显示给用户。"""
    for bad in ("(", "[", "*", "a**", "\\", "(?P<1>x)", "[z-a]", "\\q"):
        compiled, reason, position = compile_checked(bad)
        assert compiled is None, (bad, "居然接受")
        assert isinstance(reason, str) and reason, (bad, "没给原因")


def case_non_string_pattern_is_refused_with_words():
    """用户可能把别的东西填进来（None、数字）；同样只返回不抛。"""
    for bad in (None, 123, b"x"):
        compiled, reason, position = compile_checked(bad)
        assert compiled is None, bad
        assert reason, f"{bad!r} 没给原因"
        assert position is None, (bad, position)


# ---------------------------------------------------------------------------
# 好正则：正常路径不能被护栏拖累或改味
# ---------------------------------------------------------------------------

def case_good_pattern_compiles_and_matches():
    """好正则原样编译、真的能匹配，且不产生噪音字段。"""
    compiled, reason, position = compile_checked(r"第\s*\d+\s*章")
    assert compiled is not None, reason
    assert reason == "" and position is None, (reason, position)
    assert compiled.search("正文 第 12 章 开始"), "编译出来的正则不匹配"


def case_flags_are_passed_through():
    """flags 要透传（大小写不敏感这类），不能只管 pattern。"""
    import re

    compiled, _, _ = compile_checked("hello", re.IGNORECASE)
    assert compiled is not None
    assert compiled.search("HELLO"), "flags 没透传"


# ---------------------------------------------------------------------------
# Budget：超时要停、超限要停、且必须说出「为什么停、停在哪」
# ---------------------------------------------------------------------------

def case_budget_timeout_stops_and_says_so():
    """超时抛 BudgetExceeded，原因说超时、带上已处理量。"""
    budget = Budget(seconds=0.01, max_hits=0)
    hits = 0
    for _ in range(5000):
        time.sleep(0.002)
        hits += 1
        try:
            budget.tick()
        except BudgetExceeded as exc:
            assert "超时" in str(exc), f"原因没说是超时：{exc}"
            assert str(budget.done) in str(exc), f"没带上已处理量：{exc}"
            return
    raise AssertionError("Budget 超时了也不停 —— 灾难性回溯会把界面拖死")


def case_budget_max_hits_stops_and_says_so():
    """超过命中上限同样要停，并且说得清上限是多少。"""
    budget = Budget(seconds=0, max_hits=5)
    for _ in range(5):
        budget.tick()
    try:
        budget.tick()
    except BudgetExceeded as exc:
        assert "上限" in str(exc), f"原因没说是超限：{exc}"
        assert "5" in str(exc), f"没说清上限是多少：{exc}"
        return
    raise AssertionError("超过 max_hits 也没停")


def case_budget_normal_path_does_not_interrupt():
    """没到线就不许打断：正常搜索必须跑完。"""
    budget = Budget(seconds=30, max_hits=1000)
    for i in range(100):
        budget.tick()
    assert budget.done == 100, budget.done
    assert not budget.exceeded, "没到线就报了超限"


def case_budget_zero_means_no_limit():
    """0 表示不限（两个上限各自独立）：插件可以不设某一项。"""
    budget = Budget(seconds=0, max_hits=0)
    for _ in range(2000):
        budget.tick()
    assert budget.done == 2000
    assert not budget.exceeded


def case_budget_counts_batches():
    """tick 可以一次报一批（调用方自己决定多久查一次），计数要准。"""
    budget = Budget(seconds=0, max_hits=10)
    budget.tick(4)
    budget.tick(5)
    assert budget.done == 9
    try:
        budget.tick(2)  # 9 + 2 = 11 > 10
    except BudgetExceeded:
        assert budget.done == 11, budget.done
        return
    raise AssertionError("批量 tick 没算进上限")


# ---------------------------------------------------------------------------
# 边界：这一版只交付工具，不许拿去套现有内部正则
# ---------------------------------------------------------------------------

def case_guard_is_not_wired_into_the_pipeline():
    """护栏不许做成全局装饰器自动生效，也不许这一版就套到现有正则上。

    条目原话：内部正则是可信输入，套上只增加噪音与开销。所以除了它自己
    与测试，任何产品代码都不许 import guard。
    """
    offenders = []
    for path in sorted((ROOT / "huoban").glob("*.py")):
        if path.name == "guard.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "guard" in text:
            offenders.append(path.name)
    for path in sorted((ROOT / "gui").glob("*.py")):
        if "guard" in path.read_text(encoding="utf-8"):
            offenders.append(f"gui/{path.name}")
    assert not offenders, f"这些文件已经挂上护栅了（这一版不该有调用方）：{offenders}"


# ---------- 用例表 ----------

_CASES = [
    case_bad_pattern_reports_line_and_column,
    case_bad_pattern_position_is_line_one_column_n,
    case_bad_pattern_never_raises,
    case_non_string_pattern_is_refused_with_words,
    case_good_pattern_compiles_and_matches,
    case_flags_are_passed_through,
    case_budget_timeout_stops_and_says_so,
    case_budget_max_hits_stops_and_says_so,
    case_budget_normal_path_does_not_interrupt,
    case_budget_zero_means_no_limit,
    case_budget_counts_batches,
    case_guard_is_not_wired_into_the_pipeline,
]


def main() -> int:
    failed = 0
    for case in _CASES:
        try:
            case()
            print(f"ok   {case.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {case.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 一条炸了不许打断整轮
            failed += 1
            print(f"ERROR {case.__name__}: {type(exc).__name__}: {exc}")
    total = len(_CASES)
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
