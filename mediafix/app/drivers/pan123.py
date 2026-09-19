# /opt/mediafix/app/drivers/pan123.py
# 123 OpenAPI 驱动：批量归档实现

import os
import time
from collections import defaultdict
from typing import List, Dict, Any, Tuple
from .base import BaseDriver


class Pan123Driver(BaseDriver):
    type_name = "pan123"

    def _uid(self, source):
        return source.get("user_id", "admin")

    def _aid(self, source):
        return source.get("id", "")

    # ==================== 基础方法 ====================
    def list_dir(self, source, path):
        from app.pan123 import pan123_list_dir, Pan123Error
        try:
            return pan123_list_dir(self._uid(source), self._aid(source), path)
        except Pan123Error as e:
            raise RuntimeError(f"123 列目录失败: {e}")

    def check_exists(self, source, path):
        from app.pan123 import pan123_check_exists
        return pan123_check_exists(self._uid(source), self._aid(source), path)

    def mkdir(self, source, path):
        from app.pan123 import pan123_create_folder_by_path, Pan123Error
        try:
            pan123_create_folder_by_path(self._uid(source), self._aid(source), path)
            return True
        except Pan123Error as e:
            print(f"【123 mkdir 失败】{path}: {e}")
            return False

    def move(self, source, src_path, dst_path):
        from app.pan123 import pan123_move_by_path, Pan123Error
        try:
            dst_dir = "/".join(dst_path.strip("/").split("/")[:-1])
            new_name = dst_path.strip("/").split("/")[-1]
            pan123_move_by_path(
                self._uid(source), self._aid(source),
                src_path, dst_dir, new_name,
            )
            return True, 200, ""
        except Pan123Error as e:
            return False, 500, str(e)
        except Exception as e:
            return False, 500, f"未知错误: {e}"

    def delete(self, source, path):
        from app.pan123 import pan123_delete_by_path, Pan123Error
        try:
            pan123_delete_by_path(self._uid(source), self._aid(source), path)
            return True, 204
        except Pan123Error as e:
            return False, 500, str(e)
        except Exception as e:
            return False, 500, f"未知错误: {e}"

    def scan_videos(self, source, root_path, video_exts,
                    max_depth=8, cancel_check=None):
        from app.pan123 import pan123_scan_videos, Pan123Error
        try:
            return pan123_scan_videos(
                self._uid(source), self._aid(source),
                root_path, video_exts,
                max_depth=max_depth,
                cancel_check=cancel_check,
            )
        except Pan123Error as e:
            print(f"【123 扫描失败】{e}")
            return []

    # ==================== 源列举 ====================
    def list_sources(self, user_id: str) -> list:
        from app.pan123 import pan123_list_accounts
        out = []
        try:
            accounts = pan123_list_accounts(user_id)
        except Exception as e:
            print(f"【123 list_sources】{e}")
            return out
        for a in accounts:
            nick = ""
            if a.get("lastUserInfo"):
                nick = a["lastUserInfo"].get("nickname", "")
            display = a.get("name") or nick or "123 云盘"
            out.append({
                "id": a.get("id", ""),
                "name": display,
                "type": "pan123",
                "user_id": user_id,
                "nickname": nick,
                "spaceUsed": (a.get("lastUserInfo") or {}).get("spaceUsed", 0),
                "spacePermanent": (a.get("lastUserInfo") or {}).get("spacePermanent", 0),
            })
        return out

    def get_source(self, user_id: str, source_id: str):
        from app.pan123 import _get_account
        try:
            a = _get_account(user_id, source_id)
        except Exception:
            return None
        if not a:
            return None
        nick = ""
        if a.get("lastUserInfo"):
            nick = a["lastUserInfo"].get("nickname", "")
        return {
            "id": source_id,
            "name": a.get("name", "123 云盘"),
            "type": "pan123",
            "user_id": user_id,
            "nickname": nick,
        }

    # ==================== 批量归档 ====================
    def execute_archive_plan(self, source, plans, ctx):
        """123 批量归档：改名 + 移动 全走批量 API"""
        from app.pan123 import (
            pan123_batch_resolve_files, pan123_batch_rename,
            pan123_batch_move, pan123_list_dir,
            pan123_resolve_path, Pan123Error,
        )

        log_entry = ctx["log_entry"]
        cancel_evt = ctx["cancel_evt"]
        failed_dir = ctx["failed_dir"]
        move_to_failed = ctx["move_to_failed"]
        admin_user = ctx["admin_user"]
        state = ctx["state"]

        uid = self._uid(source)
        aid = self._aid(source)

        if not plans:
            return {"success": 0, "failed": 0, "skipped": 0}

        # ---------- 1. 批量解析源 fileId ----------
        src_paths = [p["old_path"] for p in plans]
        try:
            resolved = pan123_batch_resolve_files(uid, aid, src_paths)
        except Exception as e:
            print(f"【批量归档】解析失败: {e}")
            resolved = {}

        valid = []
        for p in plans:
            info = resolved.get(p["old_path"])
            if not info:
                log_entry["failed"] += 1
                log_entry["failed_files"].append({
                    "name": p["old_name"], "reason": "源文件不存在"
                })
                continue
            p2 = dict(p)
            p2["file_id"] = info["file_id"]
            p2["size"] = info.get("size", 0)
            valid.append(p2)

        print(f"【批量归档】解析 {len(plans)} → 有效 {len(valid)}")

        # ---------- 2. 拉取所有目标目录内容（去重）----------
        target_dirs = list(set(p["target_folder"] for p in valid))
        target_contents = {}
        for td in target_dirs:
            if cancel_evt and cancel_evt.is_set():
                log_entry["cancelled"] = True
                return {"success": 0, "failed": 0, "skipped": 0}
            try:
                # 确保目标目录存在
                if not self.check_exists(source, td):
                    self.mkdir(source, td)
                items = pan123_list_dir(uid, aid, td.strip("/"))
                target_contents[td] = {
                    it["name"]: it for it in items if not it.get("is_dir")
                }
            except Exception as e:
                print(f"【批量归档】目标目录拉取失败 {td}: {e}")
                target_contents[td] = {}

        # ---------- 3. 本地判断：跳过 / 归档 ----------
        to_archive = []
        for p in valid:
            existing = target_contents.get(p["target_folder"], {})

            # 完全同名 → 跳过
            if p["new_name"] in existing:
                log_entry["skipped"] = log_entry.get("skipped", 0) + 1
                log_entry.setdefault("skipped_files", []).append({
                    "name": p["old_name"],
                    "target": p["target_full_path"],
                    "reason": "目标文件已存在"
                })
                move_to_failed(source, p["old_path"], p["old_name"],
                               failed_dir, cancel_event=cancel_evt)
                continue

            # 同集不同扩展名 → 按优先级比较
            target_base = os.path.splitext(p["new_name"])[0]
            same_base = [n for n in existing
                         if os.path.splitext(n)[0] == target_base]

            if same_base:
                def _ext_rank(name):
                    e = os.path.splitext(name)[1].lower()
                    if e in ('.mkv', '.iso', '.ts'):
                        return 3
                    if e in ('.mp4', '.mov', '.avi'):
                        return 2
                    return 1

                if _ext_rank(p["new_name"]) < max(_ext_rank(n) for n in same_base):
                    log_entry["skipped"] = log_entry.get("skipped", 0) + 1
                    log_entry.setdefault("skipped_files", []).append({
                        "name": p["old_name"],
                        "target": p["target_full_path"],
                        "reason": f"同集已有更优版本（{same_base[0]}）"
                    })
                    move_to_failed(source, p["old_path"], p["old_name"],
                                   failed_dir, cancel_event=cancel_evt)
                    continue
                else:
                    # 新文件更优 → 删旧的
                    for old_name in same_base:
                        old_item = existing[old_name]
                        old_path = p["target_folder"].rstrip("/") + "/" + old_name
                        try:
                            self.delete(source, old_path)
                        except Exception:
                            pass

            to_archive.append(p)

        print(f"【批量归档】判断完成 → 待归档 {len(to_archive)}")

        if not to_archive:
            return {
                "success": log_entry.get("success", 0),
                "failed": log_entry.get("failed", 0),
                "skipped": log_entry.get("skipped", 0),
            }

        # ---------- 4. 批量改名主视频 ----------
        try:
            pairs = [(int(p["file_id"]), p["new_name"]) for p in to_archive]
            rn = pan123_batch_rename(uid, aid, pairs)
            failed_names = set(e.get("new_name", "") for e in rn.get("errors", []))
            print(f"【批量归档】改名: 成功 {rn['success']}，失败 {rn['failed']}")
        except Exception as e:
            print(f"【批量归档】批量改名异常: {e}")
            failed_names = set()

        # ---------- 5. 按目标目录分组批量移动 ----------
        by_target = defaultdict(list)
        for p in to_archive:
            if p["new_name"] not in failed_names:
                by_target[p["target_folder"]].append(p)

        for target_dir, items in by_target.items():
            if cancel_evt and cancel_evt.is_set():
                log_entry["cancelled"] = True
                break
            try:
                dir_id = pan123_resolve_path(uid, aid, target_dir.strip("/"),
                                             create_if_missing=True)
                file_ids = [int(p["file_id"]) for p in items]
                rm = pan123_batch_move(uid, aid, file_ids, dir_id)
                if rm["success"]:
                    for p in items:
                        log_entry["success"] += 1
                        log_entry["success_files"].append({
                            "old": p["old_name"],
                            "new": p["new_name"],
                            "path": p["target_full_path"],
                        })
                        admin_user["today_used"] = admin_user.get("today_used", 0) + 1
                        state["move_success"] = log_entry["success"]
                if rm["failed"]:
                    for e in rm["errors"]:
                        log_entry["failed"] += 1
                        log_entry["failed_files"].append({
                            "name": f"fileId={e.get('file_id')}",
                            "reason": e.get("reason", "移动失败")
                        })
            except Exception as e:
                print(f"【批量归档】移动失败 {target_dir}: {e}")
                for p in items:
                    log_entry["failed"] += 1
                    log_entry["failed_files"].append({
                        "name": p["old_name"], "reason": str(e)[:100]
                    })

        # ---------- 6. 批量处理字幕附件 ----------
        try:
            self._batch_rename_attachments(source, to_archive)
        except Exception as e:
            print(f"【批量归档】字幕处理异常: {e}")

        return {
            "success": log_entry.get("success", 0),
            "failed": log_entry.get("failed", 0),
            "skipped": log_entry.get("skipped", 0),
        }

    def _batch_rename_attachments(self, source, to_archive):
        """批量处理同名字幕/海报附件：改名 + 移动到同一目标目录"""
        from app.pan123 import (
            pan123_batch_rename, pan123_batch_move,
            pan123_list_dir, pan123_resolve_path,
        )

        uid = self._uid(source)
        aid = self._aid(source)

        # 按源目录缓存（每目录只 list 1 次）
        src_dir_cache = {}
        attach_pairs = []

        for p in to_archive:
            src_dir = os.path.dirname(p["old_path"]).strip("/") or "/"
            if src_dir not in src_dir_cache:
                try:
                    src_dir_cache[src_dir] = pan123_list_dir(uid, aid, src_dir)
                except Exception:
                    src_dir_cache[src_dir] = []

            base_old = os.path.splitext(p["old_name"])[0]
            new_base = os.path.splitext(p["new_name"])[0]

            for item in src_dir_cache[src_dir]:
                if item.get("is_dir"):
                    continue
                iname = item.get("name", "")
                if iname == p["old_name"]:
                    continue
                if not iname.startswith(base_old):
                    continue
                sc = iname[len(base_old):len(base_old) + 1]
                if sc not in ('.', '_', '-', ' '):
                    continue
                suffix = iname[len(base_old):]
                attach_pairs.append({
                    "file_id": int(item["file_id"]),
                    "new_name": new_base + suffix,
                    "target_folder": p["target_folder"],
                })

        if not attach_pairs:
            return

        # 批量改名
        pairs = [(a["file_id"], a["new_name"]) for a in attach_pairs]
        try:
            pan123_batch_rename(uid, aid, pairs)
        except Exception as e:
            print(f"【批量归档】字幕改名失败: {e}")
            return

        # 按目标目录分组批量移动
        by_target = defaultdict(list)
        for a in attach_pairs:
            by_target[a["target_folder"]].append(a)

        for td, items in by_target.items():
            try:
                dir_id = pan123_resolve_path(uid, aid, td.strip("/"),
                                             create_if_missing=True)
                pan123_batch_move(uid, aid,
                                  [a["file_id"] for a in items], dir_id)
            except Exception as e:
                print(f"【批量归档】字幕移动失败 {td}: {e}")