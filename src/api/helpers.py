"""API 层共享工具函数"""
from typing import Optional

from flask import request


def get_param(name: str, default: Optional[str] = None) -> Optional[str]:
    """统一参数获取：优先 JSON body，回退 query string

    Args:
        name: 参数名
        default: 默认值

    Returns:
        参数值字符串，未找到返回 default
    """
    body = request.get_json(silent=True)
    if isinstance(body, dict) and name in body:
        val = body.get(name)
        return val if val is None else str(val)
    return request.args.get(name, default)
