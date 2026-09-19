# /opt/mediafix/app/source_dispatch.py
# 统一分派层（基于 drivers registry）
# 主流程调用这里的函数，不需要关心底层是 WebDAV 还是 123/115

from typing import List, Dict, Any, Tuple
from app.drivers.registry import get_driver


def _driver(source: Dict[str, Any]):
    """根据 source.type 获取驱动实例"""
    return get_driver(source.get("type", ""))


def dispatch_propfind(source, path, depth=1, cancel_event=None):
    """兼容旧 PROPFIND 风格：返回 (items, err)
    items 里每项：{name, is_dir, href}
    """
    try:
        items = _driver(source).list_dir(source, path)
    except Exception as e:
        return [], str(e)
    out = []
    for it in items:
        out.append({
            "name": it["name"],
            "is_dir": it["is_dir"],
            "href": path.rstrip("/") + "/" + it["name"],
        })
    return out, None


def dispatch_move(source, src_path, dst_path, cancel_event=None):
    """移动文件。返回 (ok, status_code, err_text)"""
    try:
        return _driver(source).move(source, src_path, dst_path)
    except Exception as e:
        return False, 500, str(e)


def dispatch_delete(source, path, cancel_event=None):
    """删除。返回 (ok, status_code)"""
    try:
        return _driver(source).delete(source, path)
    except Exception as e:
        return False, 500


def dispatch_mkdir(source, path, cancel_event=None):
    """递归建目录。返回 bool"""
    try:
        return _driver(source).mkdir(source, path)
    except Exception:
        return False


def dispatch_check_exists(source, path, cancel_event=None):
    """检查路径是否存在"""
    try:
        return _driver(source).check_exists(source, path)
    except Exception:
        return False


def dispatch_scan_videos(source, root_path, video_exts,
                         max_depth=8, cancel_check=None):
    """递归扫描视频。返回 [{name, path, file_id, size}]"""
    try:
        return _driver(source).scan_videos(
            source, root_path, video_exts,
            max_depth=max_depth,
            cancel_check=cancel_check,
        )
    except Exception as e:
        print(f"【dispatch_scan_videos】{e}")
        return []