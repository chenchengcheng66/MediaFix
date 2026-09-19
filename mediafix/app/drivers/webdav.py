# /opt/mediafix/app/drivers/webdav.py
# WebDAV 驱动：包装现有 webdav_xxx 函数
# 关键：惰性 import main 里的函数，避免循环导入

import os
import time
from typing import List, Dict, Any, Tuple
from .base import BaseDriver


class WebDAVDriver(BaseDriver):
    type_name = "webdav"

    def _creds(self, source: Dict[str, Any]):
        return (
            source.get("url", ""),
            source.get("username", ""),
            source.get("password", ""),
        )

    def list_dir(self, source, path):
        from app.main import webdav_propfind
        url, user, pwd = self._creds(source)
        items, err = webdav_propfind(url, user, pwd, path, depth=1)
        if err:
            raise RuntimeError(f"WebDAV 列目录失败: {err}")
        out = []
        for it in items:
            if it["name"] == path.strip("/").split("/")[-1]:
                continue
            out.append({
                "name": it["name"],
                "is_dir": it["is_dir"],
                "file_id": "",
                "size": 0,
                "href": it.get("href", ""),
            })
        return out

    def check_exists(self, source, path):
        from app.main import webdav_check_exists
        url, user, pwd = self._creds(source)
        return webdav_check_exists(url, user, pwd, path)

    def mkdir(self, source, path):
        from app.main import webdav_mkcol_recursive
        url, user, pwd = self._creds(source)
        try:
            webdav_mkcol_recursive(url, user, pwd, path)
            return True
        except Exception as e:
            print(f"【WebDAV mkdir 失败】{path}: {e}")
            return False

    def move(self, source, src_path, dst_path):
        from app.main import webdav_move
        url, user, pwd = self._creds(source)
        return webdav_move(url, user, pwd, src_path, dst_path)

    def delete(self, source, path):
        from app.main import webdav_delete
        url, user, pwd = self._creds(source)
        return webdav_delete(url, user, pwd, path)

    def scan_videos(self, source, root_path, video_exts,
                    max_depth=8, cancel_check=None):
        url, user, pwd = self._creds(source)
        from app.main import webdav_propfind

        out = []
        stack = [(root_path.rstrip("/"), 0)]
        visited = set()

        while stack:
            if cancel_check and cancel_check():
                break
            cur_path, depth = stack.pop()
            if cur_path in visited or depth > max_depth:
                continue
            visited.add(cur_path)

            items, err = webdav_propfind(url, user, pwd, cur_path, depth=1)
            if err:
                print(f"【WebDAV 扫描】{cur_path} 失败: {err}")
                continue

            dir_name = cur_path.strip("/").split("/")[-1]
            subdirs = []
            for it in items:
                if it["name"] == dir_name:
                    continue
                full = cur_path.rstrip("/") + "/" + it["name"]
                if it["is_dir"]:
                    subdirs.append(full)
                else:
                    if it["name"].lower().endswith(video_exts):
                        out.append({
                            "name": it["name"],
                            "path": full,
                            "file_id": "",
                            "size": 0,
                        })

            subdirs.sort()
            for d in reversed(subdirs):
                stack.append((d, depth + 1))

        return out

    def list_sources(self, user_id: str) -> list:
        import json, os as _os
        WEBDAV_FILE = "/data/webdav_list.json"
        out = []
        if not _os.path.exists(WEBDAV_FILE):
            return out
        try:
            with open(WEBDAV_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for s in data.get(user_id, []):
                out.append({
                    "id": s.get("id", ""),
                    "name": s.get("name", "WebDAV"),
                    "type": "webdav",
                    "user_id": user_id,
                    "url": s.get("url", ""),
                    "username": s.get("username", ""),
                    "password": s.get("password", ""),
                })
        except Exception as e:
            print(f"【WebDAV list_sources】{e}")
        return out

    def get_source(self, user_id: str, source_id: str):
        import json, os as _os
        WEBDAV_FILE = "/data/webdav_list.json"
        if not _os.path.exists(WEBDAV_FILE):
            return None
        try:
            with open(WEBDAV_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for s in data.get(user_id, []):
                if s.get("id") == source_id:
                    return {
                        "id": source_id,
                        "name": s.get("name", "WebDAV"),
                        "type": "webdav",
                        "user_id": user_id,
                        "url": s.get("url", ""),
                        "username": s.get("username", ""),
                        "password": s.get("password", ""),
                    }
        except Exception:
            pass
        return None

    def _rename_attachments(self, source, dir_path, old_name, new_name, moved_path):
        """主视频重命名后，同步重命名同名字幕/海报等附件"""
        from app.main import webdav_propfind, webdav_move
        url, user, pwd = self._creds(source)
        try:
            base_old, _ = os.path.splitext(old_name)
            items, _ = webdav_propfind(url, user, pwd, dir_path, depth=1)
            for item in items:
                if item["is_dir"]:
                    continue
                fname = item["name"]
                fpath = dir_path + "/" + fname
                if fpath == moved_path:
                    continue
                if fname == old_name:
                    continue
                if (fname.startswith(base_old)
                        and fname[len(base_old):len(base_old) + 1]
                        in ('.', '_', '-', ' ')):
                    suffix = fname[len(base_old):]
                    new_attach = os.path.splitext(new_name)[0] + suffix
                    webdav_move(url, user, pwd, fpath, dir_path + "/" + new_attach)
                    time.sleep(0.3)
        except Exception as e:
            print(f"【附件重命名失败】{old_name}: {e}")

    def execute_archive_plan(self, source, plans, ctx):
        """WebDAV 归档：逐个执行（无批量 API）"""
        from app.main import webdav_propfind

        log_entry = ctx["log_entry"]
        cancel_evt = ctx["cancel_evt"]
        failed_dir = ctx["failed_dir"]
        move_to_failed = ctx["move_to_failed"]
        admin_user = ctx["admin_user"]
        state = ctx["state"]

        BATCH_SIZE = 20
        BATCH_COOLDOWN = 10
        consecutive_failures = 0
        MAX_CONSECUTIVE_FAILURES = 4
        processed = 0

        for idx, p in enumerate(plans):
            if cancel_evt and cancel_evt.is_set():
                log_entry["cancelled"] = True
                break

            state["move_current"] = idx + 1
            state["current_file_old"] = p["old_name"]
            state["current_file_new"] = p["new_name"]

            # 目标存在性检查
            target_exists = False
            try:
                items, err = webdav_propfind(
                    source["url"], source["username"], source["password"],
                    p["target_full_path"], depth=0, cancel_event=cancel_evt
                )
                if not err and items:
                    target_exists = True
            except Exception:
                pass

            if target_exists:
                log_entry["skipped"] = log_entry.get("skipped", 0) + 1
                log_entry.setdefault("skipped_files", []).append({
                    "name": p["old_name"],
                    "target": p["target_full_path"],
                    "reason": "目标文件已存在"
                })
                move_to_failed(source, p["old_path"], p["old_name"], failed_dir, cancel_event=cancel_evt)
                processed += 1
                if cancel_evt and cancel_evt.wait(0.3):
                    log_entry["cancelled"] = True
                    break
                continue

            ok, code, err_text = self.move(source, p["old_path"], p["target_full_path"])
            if ok:
                log_entry["success"] += 1
                log_entry["success_files"].append({
                    "old": p["old_name"],
                    "new": p["new_name"],
                    "path": p["target_full_path"]
                })
                admin_user["today_used"] = admin_user.get("today_used", 0) + 1
                consecutive_failures = 0
                self._rename_attachments(
                    source, os.path.dirname(p["old_path"]),
                    p["old_name"], p["new_name"], p["target_full_path"]
                )
            else:
                if code == 499:
                    log_entry["cancelled"] = True
                    break
                log_entry["failed"] += 1
                log_entry["failed_files"].append({
                    "name": p["old_name"],
                    "reason": f"HTTP {code}: {str(err_text)[:80]}"
                })
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    log_entry["error"] = f"连续 {consecutive_failures} 个文件失败，任务中止"
                    break

            processed += 1
            if processed % BATCH_SIZE == 0:
                if cancel_evt and cancel_evt.wait(BATCH_COOLDOWN):
                    log_entry["cancelled"] = True
                    break
            else:
                if cancel_evt and cancel_evt.wait(0.3):
                    log_entry["cancelled"] = True
                    break

        return {
            "success": log_entry.get("success", 0),
            "failed": log_entry.get("failed", 0),
            "skipped": log_entry.get("skipped", 0),
        }