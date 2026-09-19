# /opt/mediafix/app/unified_source.py
# 统一数据源接口（基于 drivers registry）

from typing import List, Dict, Any, Optional
from app.drivers.registry import list_registered_types, get_driver


def list_all_sources(user_id: str, webdav_file: str = None) -> List[Dict[str, Any]]:
    """遍历所有注册的驱动，汇总源列表。

    参数 webdav_file 保留仅为兼容旧调用（不再使用）
    """
    out = []
    for type_name in list_registered_types():
        try:
            driver = get_driver(type_name)
            sources = driver.list_sources(user_id)
            out.extend(sources)
        except Exception as e:
            print(f"【unified_source】驱动 {type_name} 列源失败: {e}")
    return out


def resolve_source(user_id: str, source_id: str, webdav_file: str = None) -> Optional[Dict[str, Any]]:
    """根据 source_id 找到对应驱动，返回源信息。

    自动根据 source_id 前缀判断类型，或遍历所有驱动查找。
    """
    if not source_id:
        return None

    # 快速路径：按前缀判断
    if source_id.startswith("p123-"):
        try:
            return get_driver("pan123").get_source(user_id, source_id)
        except Exception:
            pass
    elif source_id.startswith("p115-"):
        try:
            return get_driver("pan115").get_source(user_id, source_id)
        except Exception:
            pass

    # 慢速路径：遍历所有驱动
    for type_name in list_registered_types():
        try:
            driver = get_driver(type_name)
            s = driver.get_source(user_id, source_id)
            if s:
                return s
        except Exception:
            continue

    return None