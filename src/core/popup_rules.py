"""弹窗/提交错误分类规则表

从 trader.py 的 if 链提取，规则表驱动：
- SUBMIT_ERROR_RULES: 提交错误分类（错误码归属），顺序敏感，先命中优先
- POPUP_RULES:        下单弹窗处理动作（报错/点否取消/点Y继续），顺序敏感

添加新场景 = 加一条规则，无需改动业务逻辑。

【顺序敏感说明】
多个规则共享关键词（如"可卖数量"同时出现在 T1 与 INSUFFICIENT_SHARES、
"余额不足"出现在干净错误与提交失败路径），顺序决定归属——不要随意调整。

【弹窗文本来源】
- primary: cid=1040 提取的弹窗文本（可能不完整）
- extract: _extract_popup_error_text 全控件扫描兜底文本
匹配统一用 combo（primary + extract 拼接），保证 cid=1040 提取失败时
错误弹窗仍能被精确分类，而不是被当作通用警告点「是(Y)」确认。
"""
from dataclasses import dataclass
from typing import Optional, Tuple

from src.exceptions import ErrorCode


# ============================================================
# 匹配工具
# ============================================================

def _or(*groups) -> Tuple[Tuple[str, ...], ...]:
    """构造 OR 子句：外层 tuple 是 OR 组，内层 tuple 是 AND 关键词。

    _or("清算") → (("清算",),)
    _or(("提交失败", "余额", "还差"), ("提交失败", "资金", "还差"))
      → (("提交失败","余额","还差"), ("提交失败","资金","还差"))
    """
    return tuple(g if isinstance(g, tuple) else (g,) for g in groups)


def _match_clause(text: str, clause: Tuple[Tuple[str, ...], ...]) -> bool:
    """任一 AND 组全部命中即匹配"""
    return any(all(kw in text for kw in group) for group in clause)


# ============================================================
# 提交错误分类表（原 _classify_submit_error 的 if 链）
# ============================================================

@dataclass(frozen=True)
class ErrorRule:
    """提交错误分类规则（纯数据）"""
    clause: Tuple[Tuple[str, ...], ...]
    error_code: str
    message_template: str          # 含 {text} 占位（自动替换为弹窗文本前 150 字符）
    suggestion: str


# 顺序敏感规则说明:
# 1. T1 的"可卖数量"必须先于 INSUFFICIENT_SHARES 的"可卖数量"
# 2. "可用资金不足"/"可用余额不够"是 INSUFFICIENT_BALANCE 的精确短语，
#    不能被子串"余额不足"（INSUFFICIENT_SHARES 的"可用余额不足"）提前截胡
# 3. 卖空检测在余额不足之前（"股票余额不足，不允许卖空"必须归卖空）
SUBMIT_ERROR_RULES: Tuple[ErrorRule, ...] = (
    ErrorRule(
        _or("清算"),
        ErrorCode.SERVER_CLEARING,
        "券商系统清算中: {text}",
        "请等待券商清算结束后重试（通常交易日 15:30-次日 9:00）。",
    ),
    ErrorRule(
        _or("当前时间不允许委托", "非交易"),
        ErrorCode.OUTSIDE_TRADING_HOURS,
        "非交易时段: {text}",
        "请在交易时段内操作（工作日 9:30-11:30, 13:00-15:00）。",
    ),
    ErrorRule(
        # T+1 制度限制（仅卖出）：当日买入的股票次日才能卖出
        _or("T+1", "t+1", "当日买入", "当天买入", "未交收", "不存在", "可卖数量", "没有可卖"),
        ErrorCode.T1_RESTRICTION,
        "T+1 制度限制: {text}",
        "A 股实行 T+1 交易制度，当日买入的股票需至下一个交易日方可卖出。",
    ),
    # 卖空变体1: "提交失败：股票余额不足，不允许卖空。"
    ErrorRule(
        _or("不允许卖空"),
        ErrorCode.SHORT_SELLING_FORBIDDEN,
        "不允许卖空: {text}",
        "A 股不允许卖空，请检查持仓可卖数量后调整。",
    ),
    # 卖空变体2: "提交失败：当前账户10****88无证券601991的持仓信息。"
    ErrorRule(
        _or(("提交失败", "无证券", "持仓信息")),
        ErrorCode.SHORT_SELLING_FORBIDDEN,
        "不允许卖空: {text}",
        "A 股不允许卖空，请检查持仓可卖数量后调整。",
    ),
    # 余额不足（防御性：若从其他路径进入也能正确分类）
    ErrorRule(
        _or("可用资金不足", "可用余额不够"),
        ErrorCode.INSUFFICIENT_BALANCE,
        "账户余额不足: {text}",
        "请检查账户可用资金后调整委托数量或价格。",
    ),
    ErrorRule(
        # "可卖数量"被 T1 规则优先捕获；"可用余额不足"精确短语避免
        # 与上一条的"可用余额不够"冲突（注意：不用于串"余额不足"）
        _or("可卖数量", "可用余额不足"),
        ErrorCode.INSUFFICIENT_SHARES,
        "可卖数量不足: {text}",
        "请检查持仓的可卖数量（冻结数量/当日买入不可卖出）后调整委托数量。",
    ),
    ErrorRule(
        _or("事务处理机", "转发数据失败"),
        ErrorCode.SERVER_UNAVAILABLE,
        "券商服务器不可用: {text}",
        "请确认券商服务器正常运行后重试。若为交易时段外，请等待交易时段再操作。",
    ),
)


# 兜底规则（clause 永不参与匹配，仅作无命中时的返回值）
_FALLBACK_SUBMIT_ERROR_RULE = ErrorRule(
    _or("__fallback_never_matches__"),
    ErrorCode.ORDER_SUBMIT_FAILED,
    "订单提交失败: {text}",
    "请检查交易条件（余额、交易时间、涨跌停限制等）后重试",
)


def match_submit_error(text: str) -> ErrorRule:
    """按顺序返回首个命中的错误分类规则（无命中返回兜底规则）"""
    for rule in SUBMIT_ERROR_RULES:
        if _match_clause(text, rule.clause):
            return rule
    return _FALLBACK_SUBMIT_ERROR_RULE


# ============================================================
# 下单弹窗处理表（原 place_order 弹窗循环的布尔判断链）
# ============================================================

@dataclass(frozen=True)
class PopupRule:
    """下单弹窗处理规则（纯数据）

    action:
        "raise_error" — 关闭弹窗后报错（error_code 为 None 时委托
                         match_submit_error 分类，如余额不足变体/提交失败类）
        "click_no"    — 点「否(N)」取消委托后报错（价格超限，干净退出）
        "click_yes"   — 点「是(Y)」继续提交（通用警告，循环继续）
    clean_dismiss:
        True = 弹窗被正常关闭，窗口状态可信，下次同组操作可跳过准备
    """
    clause: Tuple[Tuple[str, ...], ...]
    action: str
    error_code: Optional[str] = None
    message_template: Optional[str] = None   # {text} 占位
    suggestion: Optional[str] = None
    clean_dismiss: bool = False


# 优先级（前→后，与原标题图弹窗分支顺序一致）:
# 余额不足三连 > 卖空 > 余额不足(clean) > 提交失败类 > 价格超限 > 通用警告(点Y)
POPUP_RULES: Tuple[PopupRule, ...] = (
    # 余额不足（组合关键词）：同时满足 "提交失败" + ("余额"或"资金") + "还差"
    # 实际弹窗文本示例:
    #   "提交失败：当前账户10****88可用资金不足，还差600.200元。"
    #   "提交失败：柜台：可用余额不够。还差：300.30。"
    PopupRule(
        _or(("提交失败", "余额", "还差"), ("提交失败", "资金", "还差")),
        "raise_error",
        ErrorCode.INSUFFICIENT_BALANCE,
        "账户余额不足: {text}",
        "请检查账户可用资金后调整委托数量或价格。",
        clean_dismiss=True,
    ),
    # 卖空限制：无持仓或超出可卖数量（组合文本检测，cid=1040 可能不完整）
    # 变体1: "提交失败：股票余额不足，不允许卖空。"
    # 变体2: "提交失败：当前账户10****88无证券601991的持仓信息。"
    PopupRule(
        _or("不允许卖空"),
        "raise_error",
        ErrorCode.SHORT_SELLING_FORBIDDEN,
        "不允许卖空: {text}",
        "A 股不允许卖空，请检查持仓可卖数量后调整。",
        clean_dismiss=True,
    ),
    PopupRule(
        _or(("提交失败", "无证券", "持仓信息")),
        "raise_error",
        ErrorCode.SHORT_SELLING_FORBIDDEN,
        "不允许卖空: {text}",
        "A 股不允许卖空，请检查持仓可卖数量后调整。",
        clean_dismiss=True,
    ),
    # 余额不足（无"提交失败"前缀的变体，如"可用余额不足，请调整委托数量"）
    # 错误码由 match_submit_error 决定（"可用余额不足"→INSUFFICIENT_SHARES）
    PopupRule(
        _or("余额不足"),
        "raise_error",
        clean_dismiss=True,
    ),
    # 提交失败类（清算/非交易/T+1/服务器不可用等）：错误码由 match_submit_error 决定
    PopupRule(
        _or("提交失败", "清算中", "暂不支持"),
        "raise_error",
    ),
    # 价格超限警告 → 点「否(N)」取消，干净退出（窗口状态可信，下次同向可跳过）
    PopupRule(
        _or("涨跌停", "超出", "价格"),
        "click_no",
        ErrorCode.PRICE_OUT_OF_RANGE,
        "价格超出涨跌停限制: {text}",
        "请调整委托价格至涨跌停范围内后重试。",
        clean_dismiss=True,
    ),
)


def match_popup_rule(primary_text: str, extract_text: str) -> Optional[PopupRule]:
    """按优先级返回首个命中的弹窗处理规则

    Args:
        primary_text: cid=1040 / 弹窗容器提取的弹窗文本（可能不完整）
        extract_text: 全控件扫描兜底提取的弹窗文本

    Returns:
        命中的规则；无命中返回 None（调用方按通用警告点「是(Y)」处理）
    """
    combo = f"{primary_text or ''}\n{extract_text or ''}"
    for rule in POPUP_RULES:
        if _match_clause(combo, rule.clause):
            return rule
    return None
