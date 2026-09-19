# /opt/mediafix/app/rename_tasks.py
# 重命名任务持久化模块（独立文件，避免污染 main.py）

import os, json, time, threading, uuid
from fastapi import Request, HTTPException
from pydantic import BaseModel

TASKS_FILE = "/data/rename_tasks.json"
WEBDAV_FILE = "/data/webdav_list.json"
MAX_TASKS = 100

# 内存中的取消事件（进程重启后会丢失，但任务状态已经持久化）
_cancel_events = {}


def register_rename_routes(app):
    """注册重命名任务相关路由，需要由 main.py 调用"""
    from app.main import (
        webdav_move, webdav_propfind,
        load_json, save_json, get_beijing_time,
    )

    # ============ 服务启动时：一次性清理残留 running 状态 ============
    def _initial_cleanup():
        try:
            tasks = load_json(TASKS_FILE, [])
            if not isinstance(tasks, list):
                return
            changed = False
            n = 0
            for t in tasks:
                if t.get("status") == "running":
                    t["status"] = "interrupted"
                    t["error"] = "服务重启，任务中断"
                    t["finished_at"] = time.time()
                    t["current_file"] = ""
                    changed = True
                    n += 1
            if changed:
                save_json(TASKS_FILE, tasks)
                print(f"【重命名任务】启动清理：标记 {n} 个残留任务为中断")
        except Exception as e:
            print(f"【重命名任务】启动清理失败: {e}")

    _initial_cleanup()

    # ============ 内部工具 ============

    def _read_tasks():
        """单纯读文件，不做任何副作用"""
        tasks = load_json(TASKS_FILE, [])
        if not isinstance(tasks, list):
            tasks = []
        return tasks

    def _write_tasks(tasks):
        if len(tasks) > MAX_TASKS:
            tasks = tasks[-MAX_TASKS:]
        save_json(TASKS_FILE, tasks)

    def _get_task(task_id):
        tasks = _read_tasks()
        return next((t for t in tasks if t["task_id"] == task_id), None)

    def _update(task_id, updater):
        tasks = _read_tasks()
        for t in tasks:
            if t["task_id"] == task_id:
                updater(t)
                break
        _write_tasks(tasks)

    def _rename_attachments(source, dir_path, old_name, new_name, moved_path):
        """主视频重命名后，同步重命名同名字幕/海报等附件"""
        try:
            base_old, _ = os.path.splitext(old_name)
            from app.source_dispatch import dispatch_propfind, dispatch_move
            items, _ = dispatch_propfind(source, dir_path, depth=1)
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
                    dispatch_move(source, fpath, dir_path + "/" + new_attach)
                    time.sleep(0.3)
        except Exception as e:
            print(f"【附件重命名失败】{old_name}: {e}")

    def _run_task(task_id, src_cfg):
        """后台线程执行的实体（123 源走批量 API）"""
        cancel_evt = _cancel_events.get(task_id)
        if cancel_evt is None:
            cancel_evt = threading.Event()
            _cancel_events[task_id] = cancel_evt

        task = _get_task(task_id)
        if not task:
            return

        source = src_cfg["source"]
        is_pan123 = (source.get("type") == "pan123")
        files = task["files"]

        # 判断是否取消
        def is_cancelled():
            return cancel_evt.is_set()

        # 更新函数
        def _mark_cancelled():
            def _c(t):
                t["status"] = "cancelled"
                t["current_file"] = ""
                t["finished_at"] = time.time()
            _update(task_id, _c)

        # 收尾
        def _finish():
            def _fin(t):
                if t.get("status") == "running":
                    t["status"] = "done"
                t["current_file"] = ""
                t["finished_at"] = time.time()
                t["success"] = sum(1 for x in t["files"] if x["status"] == "success")
                t["failed"] = sum(1 for x in t["files"] if x["status"] == "fail")
            _update(task_id, _fin)
            _cancel_events.pop(task_id, None)

        # ==================== 123 批量路径 ====================
        if is_pan123:
            from app.pan123 import (
                pan123_batch_resolve_files, pan123_batch_rename,
                pan123_list_dir,
            )
            aid = source["id"]
            uid = source.get("user_id", "admin")

            # 1. 更新任务为 running，重置所有文件状态
            def _reset(t):
                t["status"] = "running"
                t["current_index"] = 0
                t["current_file"] = "批量处理中..."
                t["started_at"] = time.time()
                t["finished_at"] = None
                t["error"] = ""
                for f in t["files"]:
                    f["status"] = "pending"
                    f["result_path"] = ""
                    f["error"] = ""
            _update(task_id, _reset)

            if is_cancelled():
                _mark_cancelled()
                _finish()
                return

            # 2. 批量解析 fileId
            paths = [f["path"] for f in files]
            resolved = pan123_batch_resolve_files(uid, aid, paths)

            # 3. 分主/失败
            main_items = []
            for idx, f in enumerate(files):
                info = resolved.get(f["path"])
                if not info:
                    def _fail(t, i=idx):
                        t["files"][i]["status"] = "fail"
                        t["files"][i]["error"] = "文件不存在"
                    _update(task_id, _fail)
                    continue
                main_items.append({
                    "idx": idx,
                    "file_id": info["file_id"],
                    "old_path": f["path"],
                    "old_name": f["name"],
                    "new_name": f["standard_name"],
                    "dir_path": os.path.dirname(f["path"]).strip("/") or "/",
                    "base_old": os.path.splitext(f["name"])[0],
                })

            if not main_items or is_cancelled():
                if is_cancelled():
                    _mark_cancelled()
                else:
                    _finish()
                return

            # 4. 批量改名（主视频）
            pairs = [(int(it["file_id"]), it["new_name"]) for it in main_items]
            r = pan123_batch_rename(uid, aid, pairs)

            # 更新成功/失败状态
            success_pairs = {}  # new_name → idx
            for it in main_items:
                success_pairs[it["new_name"]] = it["idx"]
            # 用错误列表判断哪些失败
            failed_new_names = set()
            for e in r.get("errors", []):
                n = e.get("new_name", "")
                if n:
                    failed_new_names.add(n)

            for it in main_items:
                def _ok(t, i=it["idx"], nn=it["new_name"], op=it["dir_path"]):
                    if nn in failed_new_names:
                        t["files"][i]["status"] = "fail"
                        t["files"][i]["error"] = "批量改名失败"
                    else:
                        t["files"][i]["status"] = "success"
                        t["files"][i]["result_path"] = "/" + op + "/" + nn
                _update(task_id, _ok)

            # 5. 批量改字幕（按目录缓存）
            dir_cache = {}
            for it in main_items:
                d = it["dir_path"]
                if d not in dir_cache:
                    try:
                        dir_cache[d] = pan123_list_dir(uid, aid, d)
                    except Exception:
                        dir_cache[d] = []

            attach_pairs = []
            for it in main_items:
                new_base = os.path.splitext(it["new_name"])[0]
                for item in dir_cache.get(it["dir_path"], []):
                    if item.get("is_dir"):
                        continue
                    n = item.get("name", "")
                    if n == it["old_name"]:
                        continue
                    if not n.startswith(it["base_old"]):
                        continue
                    suf = n[len(it["base_old"]):len(it["base_old"])+1]
                    if suf not in ('.', '_', '-', ' '):
                        continue
                    suffix = n[len(it["base_old"]):]
                    new_attach_name = new_base + suffix
                    attach_pairs.append((int(item["file_id"]), new_attach_name))

            if attach_pairs and not is_cancelled():
                try:
                    pan123_batch_rename(uid, aid, attach_pairs)
                except Exception as e:
                    print("附件批量改名失败: " + str(e))

            # 6. 同步移动：如果任务是"跨目录移动"，还要对 success 的文件调批量移动
            # （当前 rename_tasks 是"同目录改名"，不涉及移动）
            # 但如果有"移动到目标目录"的需求，需要按 dst_dir 分组批量移动

            _finish()
            return

        # ==================== WebDAV 原逻辑 ====================
        base_url = src_cfg.get("url", "")
        username = src_cfg.get("username", "")
        password = src_cfg.get("password", "")

        for idx, f in enumerate(files):
            if is_cancelled():
                _mark_cancelled()
                return

            def _set_cur(t, i=idx, n=f["name"]):
                t["current_index"] = i
                t["current_file"] = n
                t["files"][i]["status"] = "processing"
            _update(task_id, _set_cur)

            old_path = f["path"]
            new_name = f["standard_name"]
            dir_path = os.path.dirname(old_path)
            target_path = dir_path + "/" + new_name

            try:
                from app.source_dispatch import dispatch_move
                ok, code, err_text = dispatch_move(source, old_path, target_path)
                if ok:
                    def _ok(t, i=idx, tp=target_path):
                        t["files"][i]["status"] = "success"
                        t["files"][i]["result_path"] = tp
                    _update(task_id, _ok)
                    _rename_attachments(source, dir_path, f["name"], new_name, target_path)
                else:
                    def _fail(t, i=idx, c=code, e=err_text):
                        t["files"][i]["status"] = "fail"
                        t["files"][i]["error"] = "HTTP " + str(c) + ": " + str(e)[:80]
                    _update(task_id, _fail)
            except Exception as e:
                def _err(t, i=idx, msg=str(e)):
                    t["files"][i]["status"] = "fail"
                    t["files"][i]["error"] = msg
                _update(task_id, _err)

            time.sleep(0.3)

        _finish()


    # ============ 数据模型 ============

    class RenameCreateItem(BaseModel):
        name: str
        path: str
        standard_name: str

    class RenameCreateRequest(BaseModel):
        source_id: str
        files: list[RenameCreateItem]

    # ============ 路由 ============

    @app.post("/api/rename_tasks/create")
    def create_rename_task(req: RenameCreateRequest, request: Request):
        uid = request.state.user["id"]
        from app.unified_source import resolve_source
        src = resolve_source(uid, req.source_id, WEBDAV_FILE)
        if not src:
            raise HTTPException(status_code=404, detail="源不存在或已失效")

        task_id = str(uuid.uuid4())
        now = time.time()
        files = [{
            "name": it.name,
            "path": it.path,
            "standard_name": it.standard_name,
            "status": "pending",
            "result_path": "",
            "error": "",
        } for it in req.files]

        task = {
            "task_id": task_id,
            "user_id": uid,
            "created_at": now,
            "time_str": get_beijing_time(),
            "source_id": req.source_id,
            "source_name": src["name"],
            "status": "pending",
            "total": len(files),
            "success": 0,
            "failed": 0,
            "current_index": 0,
            "current_file": "",
            "files": files,
            "started_at": None,
            "finished_at": None,
            "error": "",
        }

        tasks = _read_tasks()
        tasks.append(task)
        _write_tasks(tasks)
        return {"status": "ok", "task_id": task_id}

    @app.post("/api/rename_tasks/{task_id}/run")
    def run_rename_task(task_id: str, request: Request):
        uid = request.state.user["id"]
        task = _get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task["status"] == "running":
            raise HTTPException(status_code=400, detail="任务正在运行中")

        from app.unified_source import resolve_source
        src = resolve_source(uid, task["source_id"], WEBDAV_FILE)
        if not src:
            raise HTTPException(status_code=404, detail="源网盘不存在或已失效")

        def _start(t):
            t["status"] = "running"
            t["current_index"] = 0
            t["current_file"] = ""
            t["started_at"] = time.time()
            t["finished_at"] = None
            t["error"] = ""
            for f in t["files"]:
                f["status"] = "pending"
                f["result_path"] = ""
                f["error"] = ""
        _update(task_id, _start)

        _cancel_events[task_id] = threading.Event()

        threading.Thread(
            target=_run_task,
            args=(task_id, {
                "source": src,
            }),
            daemon=True,
        ).start()

        return {"status": "ok", "message": "任务已在后台启动"}

    @app.post("/api/rename_tasks/{task_id}/cancel")
    def cancel_rename_task(task_id: str, request: Request):
        ev = _cancel_events.get(task_id)
        if ev:
            ev.set()
            return {"status": "ok", "message": "中断信号已发送"}
        return {"status": "ok", "message": "该任务不在运行中"}

    @app.get("/api/rename_tasks")
    def list_rename_tasks(request: Request):
        uid = request.state.user["id"]
        tasks = _read_tasks()
        mine = [t for t in tasks if t.get("user_id") == uid]
        summary = [{
            "task_id": t["task_id"],
            "time_str": t.get("time_str", ""),
            "source_name": t.get("source_name", ""),
            "status": t.get("status", ""),
            "total": t.get("total", 0),
            "success": t.get("success", 0),
            "failed": t.get("failed", 0),
            "current_index": t.get("current_index", 0),
            "current_file": t.get("current_file", ""),
        } for t in mine]
        return {"tasks": list(reversed(summary))}

    @app.get("/api/rename_tasks/{task_id}")
    def get_rename_task(task_id: str, request: Request):
        task = _get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.get("user_id") != request.state.user["id"] and request.state.user["role"] != "admin":
            raise HTTPException(status_code=403, detail="无权限")
        return task

    @app.delete("/api/rename_tasks/{task_id}")
    def delete_rename_task(task_id: str, request: Request):
        tasks = _read_tasks()
        tasks = [t for t in tasks if t["task_id"] != task_id]
        _write_tasks(tasks)
        return {"status": "ok"}

    print("【重命名任务模块】路由已注册")