"""弹窗/提交错误分类规则测试

重点：STOCK_NOT_FOUND 与 T1_RESTRICTION 的关键字边界——
「不存在该证券」（买入无效代码）必须归 STOCK_NOT_FOUND，
「该证券不存在可卖数量」（T+1 卖出）必须留在 T1_RESTRICTION。
"""
from src.core.popup_rules import match_popup_rule, match_submit_error
from src.exceptions import ErrorCode


# ------------------------------------------------------------
# STOCK_NOT_FOUND（2026-10-10 模拟盘实弹文本）
# ------------------------------------------------------------

def test_stock_not_found_full_text():
    r = match_submit_error("提交失败：600028：不存在该证券。\n报告问题")
    assert r.error_code == ErrorCode.STOCK_NOT_FOUND


def test_stock_not_found_popup_rule_with_prefix():
    rule = match_popup_rule("提交失败：600028：不存在该证券。", "报告问题")
    assert rule is not None
    assert rule.action == "raise_error"
    assert rule.error_code == ErrorCode.STOCK_NOT_FOUND


def test_stock_not_found_popup_rule_without_prefix():
    # 无「提交失败」前缀的变体不能落进「价格」关键词被误判 PRICE_OUT_OF_RANGE
    rule = match_popup_rule("600028：不存在该证券。", "")
    assert rule is not None
    assert rule.error_code == ErrorCode.STOCK_NOT_FOUND


def test_stock_not_found_ci_variant():
    assert match_submit_error("提交失败：600028：不存在此证券。").error_code == \
        ErrorCode.STOCK_NOT_FOUND


# ------------------------------------------------------------
# T1 边界：卖出场景的「不存在」必须留在 T1_RESTRICTION
# ------------------------------------------------------------

def test_t1_sell_missing_sellable_kept():
    # 「该证券不存在可卖数量」包含子串「证券不存在」——关键字不能用宽泛短语的原因
    assert match_submit_error("提交失败：该证券不存在可卖数量。").error_code == \
        ErrorCode.T1_RESTRICTION


def test_t1_explicit_phrase():
    assert match_submit_error("T+1 当日买入次日方可卖出").error_code == \
        ErrorCode.T1_RESTRICTION


# ------------------------------------------------------------
# 既有分类不回归
# ------------------------------------------------------------

def test_clearing_priority():
    assert match_submit_error("系统当前可能正在清算中,暂不支持委托交易").error_code == \
        ErrorCode.SERVER_CLEARING


def test_outside_trading_hours_live_text():
    assert match_submit_error(
        "提交失败：[120141][当前时间不允许委托][init_date=20261009,curr_date=20261010]。"
    ).error_code == ErrorCode.OUTSIDE_TRADING_HOURS


def test_short_selling_variant3_kept():
    # 变体3（实盘 2026-10-08）：「该客户无证券:000001持仓」→ 卖空而非 STOCK_NOT_FOUND
    assert match_submit_error("提交失败：该客户无证券:000001持仓。").error_code == \
        ErrorCode.SHORT_SELLING_FORBIDDEN


def test_generic_fallback_still_works():
    assert match_submit_error("提交失败：某种未知错误。").error_code == \
        ErrorCode.ORDER_SUBMIT_FAILED
