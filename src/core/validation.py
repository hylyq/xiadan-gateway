"""下单数据校验（纯函数，无外部依赖）"""


def sanitize_price(price: str) -> str:
    """对 A 股价格做格式校验，限制 2 位小数

    Args:
        price: 原始价格字符串

    Returns:
        格式化的价格字符串（最多 2 位小数）

    Raises:
        Exception: 价格格式无效
    """
    try:
        price_float = float(price)
        return f"{price_float:.2f}"
    except ValueError:
        raise Exception(f"价格格式无效: {price}")
