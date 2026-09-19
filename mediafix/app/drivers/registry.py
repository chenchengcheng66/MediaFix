# /opt/mediafix/app/drivers/registry.py
# 驱动注册表：所有驱动在这里注册，主流程通过 type 获取实例

from typing import Dict
from .base import BaseDriver


_DRIVERS: Dict[str, BaseDriver] = {}


def register(driver: BaseDriver) -> None:
    """注册驱动（重复注册会覆盖，用于热重载）"""
    if not driver.type_name:
        raise ValueError("驱动必须定义 type_name")
    _DRIVERS[driver.type_name] = driver


def get_driver(source_type: str) -> BaseDriver:
    """根据源类型获取驱动实例"""
    d = _DRIVERS.get(source_type)
    if not d:
        raise ValueError(f"未知的源类型: {source_type}")
    return d


def list_registered_types() -> list:
    """列出所有已注册的类型"""
    return list(_DRIVERS.keys())


def has_driver(source_type: str) -> bool:
    return source_type in _DRIVERS




# ==================== 自动注册 ====================

def _auto_register():

    """导入所有内置驱动，注册到 _DRIVERS"""

    from .webdav import WebDAVDriver

    from .pan123 import Pan123Driver



    register(WebDAVDriver())

    register(Pan123Driver())

    # 未来加 115：这里加一行 register(Pan115Driver())





_auto_register()

