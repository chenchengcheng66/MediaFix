# /opt/mediafix/app/drivers/base.py
# 所有网盘驱动的抽象基类

from typing import List, Dict, Any, Optional, Tuple


class BaseDriver:
    """网盘驱动抽象基类。

    子类必须：
      - 定义 type_name（如 "webdav"、"pan123"）
      - 实现下面 8 个方法
    """

    type_name: str = ""

    def list_dir(self, source: Dict[str, Any], path: str) -> List[Dict[str, Any]]:
        """列目录。返回：[{name, is_dir, file_id, size}]"""
        raise NotImplementedError

    def check_exists(self, source: Dict[str, Any], path: str) -> bool:
        """检查路径是否存在"""
        raise NotImplementedError

    def mkdir(self, source: Dict[str, Any], path: str) -> bool:
        """递归创建目录（已存在返回 True）"""
        raise NotImplementedError

    def move(self, source: Dict[str, Any], src_path: str, dst_path: str) -> Tuple[bool, int, str]:
        """移动文件（含重命名）。返回 (ok, status_code, err)"""
        raise NotImplementedError

    def delete(self, source: Dict[str, Any], path: str) -> Tuple[bool, int]:
        """删除文件或目录。返回 (ok, status_code)"""
        raise NotImplementedError

    def scan_videos(self, source: Dict[str, Any], root_path: str,
                    video_exts: tuple, max_depth: int = 8,
                    cancel_check=None) -> List[Dict[str, Any]]:
        """递归扫描视频文件。返回 [{name, path, file_id, size}]"""
        raise NotImplementedError

    def list_sources(self, user_id: str) -> List[Dict[str, Any]]:
        """列出该驱动下所有可用源。

        返回：[{id, name, type, user_id, ...}]
        """
        raise NotImplementedError

    def get_source(self, user_id: str, source_id: str) -> Optional[Dict[str, Any]]:
        """根据 source_id 获取完整源信息（含连接信息）"""
        raise NotImplementedError

    def execute_archive_plan(self, source: Dict[str, Any],
                             plans: List[Dict[str, Any]],
                             ctx: Dict[str, Any]) -> Dict[str, int]:
        """执行归档计划（由 run_auto_task 调用）。

        plans: [{
            "old_path": 源路径,
            "old_name": 源文件名,
            "new_name": 目标文件名（不含目录）,
            "target_folder": 目标目录路径（不含文件名）,
            "target_full_path": 目标完整路径,
        }, ...]

        ctx: {
            "log_entry": dict,          用于写成功/失败/跳过清单
            "cancel_evt": threading.Event,
            "failed_dir": str,
            "move_to_failed": callable, 移入失败目录的函数
            "admin_user": dict,
            "state": dict,
        }

        返回: {"success": int, "failed": int, "skipped": int}
        """
        raise NotImplementedError