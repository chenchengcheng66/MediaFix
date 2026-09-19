# /opt/mediafix/app/pan123.py
# 123 云盘 OpenAPI 客户端（多账号版）
# - 通过社区 OAuth 中转服务（api.oplist.org）授权
# - 支持同一用户添加多个 123 账号
# - 独立限流器（默认 120 请求/分钟，不走 WebDAV 的 55/分钟）

import base64
import json
import os
import threading
import time
import uuid
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import requests

requests.packages.urllib3.disable_warnings()

OPEN_API_BASE = "https://open-api.123pan.com"
OAUTH_BROKER_URL = "https://api.oplist.org"
OAUTH_DRIVER = "123cloud"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 123XiaoZhuShou/1.0"
)

ACCOUNTS_FILE = "/data/pan123_accounts.json"
PAN123_MAX_REQ_PER_MIN = 120

_FILE_LOCK = threading.Lock()
_TOKEN_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_RATE_HISTORY = {}


import unicodedata


def _clean_filename(s: str) -> str:
    """清洗文件名：NFC 规范化 + 去零宽字符 / BOM / NBSP"""
    if not s:
        return ""
    s = str(s)
    s = unicodedata.normalize("NFC", s)
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff", "\u00a0", "\u3000"):
        s = s.replace(ch, "")
    return s.strip()


class Pan123Error(RuntimeError):
    def __init__(self, message: str, code: int = 0):
        super().__init__(message)
        self.code = code


# ==================== 账号存储 ====================
def _load_accounts(user_id: str = "admin") -> list:
    if not os.path.exists(ACCOUNTS_FILE):
        return []
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(user_id, [])
    except Exception:
        return []


def _save_accounts(user_id: str, accounts: list):
    with _FILE_LOCK:
        data = {}
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[user_id] = accounts
        try:
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"【pan123】保存账号失败: {e}")


def _get_account(user_id: str, account_id: str) -> Optional[dict]:
    for a in _load_accounts(user_id):
        if a.get("id") == account_id:
            return a
    return None


def _update_account(user_id: str, account_id: str, updater):
    accounts = _load_accounts(user_id)
    for a in accounts:
        if a.get("id") == account_id:
            updater(a)
            break
    _save_accounts(user_id, accounts)


# ==================== 独立限流器 ====================
def _rate_limit(account_id: str):
    """每个 123 账号独立限流 120 请求/分钟"""
    while True:
        with _RATE_LOCK:
            now = time.time()
            history = _RATE_HISTORY.get(account_id, [])
            history = [t for t in history if now - t < 60]
            _RATE_HISTORY[account_id] = history
            if len(history) < PAN123_MAX_REQ_PER_MIN:
                history.append(now)
                _RATE_HISTORY[account_id] = history
                return True
            oldest = history[0]
            wait_sec = 60 - (now - oldest) + 0.3
        time.sleep(wait_sec)


# ==================== OAuth 中转服务 ====================
def _broker_headers() -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "user-agent": USER_AGENT,
        "referer": f"{OAUTH_BROKER_URL}/",
    }


def pan123_get_auth_url() -> dict:
    """获取 123 官方授权页地址 + redirect_uri"""
    try:
        r = requests.get(
            f"{OAUTH_BROKER_URL}/{OAUTH_DRIVER}/requests",
            params={"server_use": "true", "driver_txt": f"{OAUTH_DRIVER}_oa"},
            headers=_broker_headers(),
            timeout=25,
        )
    except Exception as e:
        raise Pan123Error(f"连接授权中转服务失败: {e}")

    data = r.json()
    auth_url = str(data.get("text", ""))
    if r.status_code >= 400 or not auth_url.startswith("http"):
        msg = data.get("text") or data.get("message") or "未知错误"
        raise Pan123Error(f"获取授权地址失败: {msg}")

    parsed = urlparse(auth_url)
    query = parse_qs(parsed.query)
    redirect_uri = (query.get("redirect_uri") or [""])[0]
    if not redirect_uri:
        raise Pan123Error("授权地址缺少 redirect_uri")

    return {"authorizeUrl": auth_url, "redirectUri": redirect_uri}


def _broker_refresh(refresh_token: str) -> dict:
    try:
        r = requests.get(
            f"{OAUTH_BROKER_URL}/{OAUTH_DRIVER}/renewapi",
            params={"refresh_ui": refresh_token.strip()},
            headers=_broker_headers(),
            timeout=25,
        )
    except Exception as e:
        raise Pan123Error(f"刷新 token 请求失败: {e}")

    try:
        data = r.json()
    except Exception:
        raise Pan123Error(f"刷新 token 返回非 JSON: {r.text[:200]}")

    access_token = str(data.get("access_token", ""))
    if r.status_code >= 400 or not access_token:
        msg = str(data.get("text") or data.get("message") or "")
        low = msg.lower()
        if any(k in low for k in ["invalid", "expired", "revoked", "失效", "过期"]):
            raise Pan123Error("授权已失效，请重新授权")
        raise Pan123Error(msg or "刷新 token 失败")

    return {
        "accessToken": access_token,
        "refreshToken": str(data.get("refresh_token", "") or refresh_token),
        "expiresIn": int(data.get("expires_in", 7200)),
    }


# ==================== Token 管理 ====================
def _get_valid_token(user_id: str, account_id: str) -> str:
    account = _get_account(user_id, account_id)
    if not account:
        raise Pan123Error("账号不存在")

    access = account.get("accessToken", "")
    expires = float(account.get("expiresAt", 0))
    if access and expires > time.time() + 60:
        return access

    refresh = account.get("refreshToken", "")
    if not refresh:
        raise Pan123Error("该账号未授权，请先完成授权")

    with _TOKEN_LOCK:
        account = _get_account(user_id, account_id)
        access = account.get("accessToken", "")
        expires = float(account.get("expiresAt", 0))
        if access and expires > time.time() + 60:
            return access

        result = _broker_refresh(refresh)
        def upd(a):
            a["accessToken"] = result["accessToken"]
            a["refreshToken"] = result["refreshToken"]
            a["expiresAt"] = time.time() + result.get("expiresIn", 7200) - 120
        _update_account(user_id, account_id, upd)
        return result["accessToken"]


# ==================== 通用请求 ====================
def _request(user_id: str, account_id: str, method: str, path: str,
             params: dict = None, body: dict = None) -> dict:
    last_err = ""
    for attempt in range(3):
        _rate_limit(account_id)
        token = _get_valid_token(user_id, account_id)
        headers = {
            "Authorization": f"Bearer {token}",
            "platform": "open_platform",
        }
        if method.upper() == "POST":
            headers["Content-Type"] = "application/json"

        try:
            r = requests.request(
                method.upper(),
                OPEN_API_BASE + path,
                params=params,
                json=body,
                headers=headers,
                timeout=30,
            )
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise Pan123Error(f"请求失败: {last_err}")

        try:
            data = r.json()
        except Exception:
            raise Pan123Error(f"返回非 JSON: {r.text[:200]}")

        code = data.get("code")

        if r.status_code == 401 or code in (401, "401"):
            if attempt == 0:
                def upd(a):
                    a["accessToken"] = ""
                    a["expiresAt"] = 0
                _update_account(user_id, account_id, upd)
                continue

        msg = str(data.get("message", ""))
        if code in (429, "429", -1) or "频繁" in msg or "限流" in msg:
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue

        if r.status_code < 400 and code in (0, 200, "0", None):
            return data

        raise Pan123Error(
            msg or f"123 API 错误 {r.status_code}",
            code=int(code) if code else r.status_code,
        )

    raise Pan123Error(f"重试失败: {last_err}")


# ==================== 账号 CRUD ====================
def pan123_list_accounts(user_id: str = "admin") -> list:
    """列出账号（脱敏，不返回 access_token 全文）"""
    accounts = _load_accounts(user_id)
    out = []
    for a in accounts:
        out.append({
            "id": a.get("id"),
            "name": a.get("name", ""),
            "enabled": a.get("enabled", True),
            "createdAt": a.get("createdAt", 0),
            "lastUserInfo": a.get("lastUserInfo", {}),
            "lastCheckedAt": a.get("lastCheckedAt", 0),
        })
    return out


def pan123_add_account(user_id: str, name: str,
                       access_token: str, refresh_token: str,
                       expires_in: int = 7200) -> dict:
    """添加账号，返回新账号 ID"""
    if not access_token or not refresh_token:
        raise Pan123Error("access_token 和 refresh_token 不能为空")

    aid = "p123-" + uuid.uuid4().hex[:12]
    account = {
        "id": aid,
        "name": (name or "123 云盘").strip()[:50],
        "accessToken": access_token.strip(),
        "refreshToken": refresh_token.strip(),
        "expiresAt": time.time() + int(expires_in) - 120,
        "enabled": True,
        "createdAt": time.time(),
        "lastUserInfo": {},
        "lastCheckedAt": 0,
    }

    # 立即验证一次，拿用户信息
    accounts = _load_accounts(user_id)
    accounts.append(account)
    _save_accounts(user_id, accounts)

    try:
        info = _request(user_id, aid, "GET", "/api/v1/user/info").get("data", {})
        def upd(a):
            a["lastUserInfo"] = {
                "nickname": info.get("nickname", ""),
                "spaceUsed": info.get("spaceUsed", 0),
                "spacePermanent": info.get("spacePermanent", 0),
            }
            a["lastCheckedAt"] = time.time()
        _update_account(user_id, aid, upd)
        return {"id": aid, "info": info}
    except Pan123Error:
        # 验证失败 → 删除刚添加的
        accounts = [a for a in _load_accounts(user_id) if a.get("id") != aid]
        _save_accounts(user_id, accounts)
        raise


def pan123_rename_account(user_id: str, account_id: str, new_name: str):
    if not new_name or len(new_name) > 50:
        raise Pan123Error("名称长度必须在 1-50 之间")
    def upd(a):
        a["name"] = new_name.strip()
    _update_account(user_id, account_id, upd)


def pan123_delete_account(user_id: str, account_id: str):
    accounts = [a for a in _load_accounts(user_id) if a.get("id") != account_id]
    _save_accounts(user_id, accounts)
    # 清掉限流历史
    _RATE_HISTORY.pop(account_id, None)


def pan123_account_status(user_id: str, account_id: str) -> dict:
    """检查单个账号的授权状态"""
    account = _get_account(user_id, account_id)
    if not account:
        return {"authorized": False, "reason": "not_found"}
    try:
        info = _request(user_id, account_id, "GET", "/api/v1/user/info").get("data", {})
        def upd(a):
            a["lastUserInfo"] = {
                "nickname": info.get("nickname", ""),
                "spaceUsed": info.get("spaceUsed", 0),
                "spacePermanent": info.get("spacePermanent", 0),
            }
            a["lastCheckedAt"] = time.time()
        _update_account(user_id, account_id, upd)
        return {"authorized": True, "user": info, "name": account.get("name", "")}
    except Pan123Error as e:
        return {"authorized": False, "expired": True, "message": str(e)}


# ==================== 业务接口（按账号分派）====================
def pan123_list_files(user_id: str, account_id: str, parent_id: str = "0") -> List[dict]:
    """列目录（自动翻页，去重）"""
    out = []
    seen = set()
    last_id = 0
    for _ in range(200):
        data = _request(user_id, account_id, "GET", "/api/v2/file/list", params={
            "parentFileId": str(parent_id or "0"),
            "limit": "100",
            "lastFileId": str(last_id),
        })
        body = data.get("data", {})
        batch = body.get("fileList", [])
        for it in batch:
            fid = it.get("fileId")
            if fid in seen:
                continue
            seen.add(fid)
            # 过滤回收站中的文件（trashed = 1）
            if int(it.get("trashed") or 0) != 0:
                continue
            # 清洗文件名（NFC 规范化 + 去零宽字符）
            if "filename" in it:
                it["filename"] = _clean_filename(it["filename"])
            elif "fileName" in it:
                it["fileName"] = _clean_filename(it["fileName"])
            out.append(it)
        nxt = body.get("lastFileId", 0)
        if not batch or nxt == 0 or nxt == last_id:
            break
        last_id = nxt
    return out


def pan123_create_folder(user_id: str, account_id: str, parent_id: str, name: str) -> str:
    data = _request(user_id, account_id, "POST", "/upload/v1/file/mkdir", body={
        "name": name,
        "parentID": int(parent_id or "0"),
    })
    return str(data.get("data", {}).get("dirID", ""))


def pan123_move_files(user_id: str, account_id: str, file_ids: list, target_folder_id: str):
    _request(user_id, account_id, "POST", "/api/v1/file/move", body={
        "fileIDs": [int(i) for i in file_ids],
        "toParentFileID": int(target_folder_id or "0"),
    })


def pan123_rename_file(user_id: str, account_id: str, file_id: int, filename: str):
    _request(user_id, account_id, "POST", "/api/v1/file/rename", body={
        "renameList": [f"{int(file_id)}|{filename}"],
    })


def pan123_trash_files(user_id: str, account_id: str, file_ids: list):
    _request(user_id, account_id, "POST", "/api/v1/file/trash", body={
        "fileIDs": [int(i) for i in file_ids],
    })


def pan123_get_download_url(user_id: str, account_id: str, file_id: int) -> str:
    data = _request(user_id, account_id, "GET", "/api/v1/file/download_info", params={
        "fileId": str(int(file_id)),
    })
    body = data.get("data", {})
    url = body.get("downloadUrl") or body.get("url") or ""
    if not url:
        raise Pan123Error("123 未返回下载直链")
    return url
    
    

# ==================== 路径 ↔ fileId 映射层 ====================
# 123 OpenAPI 用 fileId 操作，但任务配置用路径。
# 这一层负责：路径 → fileId（带缓存），以及所有按路径的文件操作。

_PATH_CACHE = {}
_PATH_CACHE_TTL = 300
_PATH_LOCK = threading.Lock()


def _cache_get(account_id: str, path: str):
    with _PATH_LOCK:
        entry = _PATH_CACHE.get(account_id)
        if not entry:
            return None
        if time.time() - entry["ts"] > _PATH_CACHE_TTL:
            del _PATH_CACHE[account_id]
            return None
        return entry["paths"].get(path)


def _cache_set(account_id: str, path: str, file_id: str):
    with _PATH_LOCK:
        entry = _PATH_CACHE.get(account_id)
        if not entry or time.time() - entry["ts"] > _PATH_CACHE_TTL:
            entry = {"paths": {}, "ts": time.time()}
            _PATH_CACHE[account_id] = entry
        entry["paths"][path] = file_id


def _cache_clear(account_id: str, path_prefix: str = None):
    with _PATH_LOCK:
        entry = _PATH_CACHE.get(account_id)
        if not entry:
            return
        if path_prefix is None:
            del _PATH_CACHE[account_id]
            return
        to_del = [p for p in entry["paths"]
                  if p == path_prefix or p.startswith(path_prefix + "/")]
        for p in to_del:
            del entry["paths"][p]


def pan123_resolve_path(user_id: str, account_id: str, path: str,
                        create_if_missing: bool = False) -> str:
    """路径 → fileId（带缓存）"""
    path = path.strip("/")
    if not path:
        return "0"

    parts = [p for p in path.split("/") if p]
    current_path = ""
    current_id = "0"

    for part in parts:
        current_path = current_path + "/" + part
        cached = _cache_get(account_id, current_path)
        if cached is not None:
            current_id = cached
            continue

        items = pan123_list_files(user_id, account_id, current_id)
        found = None
        part_clean = _clean_filename(part)
        for it in items:
            cand = _clean_filename(it.get("filename") or "")
            if cand == part_clean and int(it.get("type") or 0) == 1:
                found = it
                break

        if found:
            current_id = str(found["fileId"])
            _cache_set(account_id, current_path, current_id)
        elif create_if_missing:
            new_id = pan123_create_folder(user_id, account_id, current_id, part)
            if not new_id:
                raise Pan123Error(f"创建目录失败: {current_path}")
            _cache_set(account_id, current_path, new_id)
            current_id = new_id
        else:
            raise Pan123Error(f"目录不存在: {current_path}")

    return current_id


def pan123_list_dir(user_id: str, account_id: str, path: str) -> list:
    """按路径列目录，返回统一格式 [{name, is_dir, file_id, size}]"""
    folder_id = pan123_resolve_path(user_id, account_id, path)
    items = pan123_list_files(user_id, account_id, folder_id)
    out = []
    for it in items:
        out.append({
            "name": it.get("filename", ""),
            "is_dir": it.get("type") == 1,
            "file_id": str(it.get("fileId", "")),
            "size": it.get("size", 0),
        })
    return out


def pan123_check_exists(user_id: str, account_id: str, path: str) -> bool:
    """检查路径是否存在"""
    try:
        pan123_resolve_path(user_id, account_id, path, create_if_missing=False)
        return True
    except Pan123Error:
        return False


def pan123_create_folder_by_path(user_id: str, account_id: str, path: str) -> str:
    """递归创建目录（已存在则直接返回 fileId）"""
    return pan123_resolve_path(user_id, account_id, path, create_if_missing=True)


def pan123_find_file_id(user_id: str, account_id: str, path: str):
    """按路径找文件（不是目录）的 fileId。找不到返回 None"""
    path = path.strip("/")
    if not path:
        return None
    parts = path.split("/")
    file_name = parts[-1]
    dir_path = "/".join(parts[:-1])

    dir_id = pan123_resolve_path(user_id, account_id, dir_path)
    items = pan123_list_files(user_id, account_id, dir_id)
    for it in items:
        if it.get("filename") == file_name:
            return str(it.get("fileId", "")), it
    return None


def pan123_move_by_path(user_id: str, account_id: str,
                        src_path: str, dst_dir_path: str,
                        new_name: str = None) -> dict:
    """按路径移动/重命名文件。
    - 同目录 → 只改名，不调 move_files（123 API 不允许移动到当前目录）
    - 跨目录 → 改名 + 移动
    """
    src_path_clean = src_path.strip("/")
    dst_dir_clean = dst_dir_path.strip("/")

    found = pan123_find_file_id(user_id, account_id, src_path_clean)
    if not found:
        raise Pan123Error(f"源文件不存在: {src_path}")
    src_file_id, src_info = found

    src_name = src_info.get("filename") or src_info.get("fileName") or ""
    src_parent_id = str(src_info.get("parentFileId", ""))

    # 目标目录 fileId
    if not dst_dir_clean:
        dst_dir_id = "0"
    else:
        dst_dir_id = pan123_resolve_path(user_id, account_id, dst_dir_clean,
                                         create_if_missing=True)

    is_same_dir = (src_parent_id == str(dst_dir_id))

    # 改名
    final_name = new_name or src_name
    if new_name and new_name != src_name:
        pan123_rename_file(user_id, account_id, int(src_file_id), new_name)

    # 跨目录才移动
    if not is_same_dir:
        pan123_move_files(user_id, account_id, [src_file_id], dst_dir_id)

    # 清缓存
    src_dir = "/".join(src_path_clean.split("/")[:-1])
    _cache_clear(account_id, src_dir)
    if not is_same_dir:
        _cache_clear(account_id, dst_dir_clean)

    return {"file_id": src_file_id, "name": final_name, "moved": not is_same_dir}


def pan123_delete_by_path(user_id: str, account_id: str, path: str):
    """按路径删除文件（移到回收站）"""
    found = pan123_find_file_id(user_id, account_id, path)
    if not found:
        return False
    file_id, _ = found
    pan123_trash_files(user_id, account_id, [file_id])
    return True


def pan123_scan_videos(user_id: str, account_id: str, root_path: str,
                       video_exts: tuple, max_depth: int = 8,
                       cancel_check=None) -> list:
    """递归扫描视频文件（BFS，带深度限制）。
    返回 [{name, path, file_id, size}]
    """
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

        try:
            items = pan123_list_dir(user_id, account_id, cur_path)
        except Pan123Error as e:
            print(f"【pan123扫描】{cur_path} 失败: {e}")
            continue

        for it in items:
            full = (cur_path + "/" + it["name"]).replace("//", "/")
            if it["is_dir"]:
                stack.append((full, depth + 1))
            else:
                if it["name"].lower().endswith(video_exts):
                    out.append({
                        "name": it["name"],
                        "path": full,
                        "file_id": it["file_id"],
                        "size": it["size"],
                    })
    return out

# ==================== 批量操作（v2 优化版） ====================
_BATCH_LIMIT = 100
_BATCH_RETRY = 2


def _split_batches(items: list, size: int = _BATCH_LIMIT):
    """把列表切分成每批 size 个"""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def pan123_batch_rename(user_id: str, account_id: str, items: list) -> dict:
    """批量改名。

    items: [(file_id: int, new_name: str), ...]
    返回：{"success": N, "failed": N, "errors": [...]}
    """
    success = 0
    failed = 0
    errors = []

    for batch_idx, batch in enumerate(_split_batches(items)):
        rename_list = [f"{int(fid)}|{name}" for fid, name in batch]

        for attempt in range(_BATCH_RETRY + 1):
            try:
                _request(user_id, account_id, "POST", "/api/v1/file/rename", body={
                    "renameList": rename_list,
                })
                success += len(batch)
                break
            except Pan123Error as e:
                if attempt < _BATCH_RETRY:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                # 整批最终失败 → 逐个重试
                print(f"【批量改名失败】第 {batch_idx + 1} 批，逐个重试: {e}")
                for fid, name in batch:
                    try:
                        pan123_rename_file(user_id, account_id, int(fid), name)
                        success += 1
                    except Pan123Error as e2:
                        failed += 1
                        errors.append({"file_id": fid, "new_name": name, "reason": str(e2)})
            except Exception as e:
                if attempt < _BATCH_RETRY:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                failed += len(batch)
                for fid, name in batch:
                    errors.append({"file_id": fid, "new_name": name, "reason": str(e)})

    return {"success": success, "failed": failed, "errors": errors}


def pan123_batch_move(user_id: str, account_id: str,
                      file_ids: list, target_folder_id: str) -> dict:
    """批量移动（同一目标目录）。

    file_ids: [file_id, ...]
    target_folder_id: 目标目录 fileId
    返回：{"success": N, "failed": N, "errors": [...]}
    """
    success = 0
    failed = 0
    errors = []

    for batch_idx, batch in enumerate(_split_batches(file_ids)):
        for attempt in range(_BATCH_RETRY + 1):
            try:
                _request(user_id, account_id, "POST", "/api/v1/file/move", body={
                    "fileIDs": [int(fid) for fid in batch],
                    "toParentFileID": int(target_folder_id or "0"),
                })
                success += len(batch)
                break
            except Pan123Error as e:
                if attempt < _BATCH_RETRY:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                # 整批失败 → 逐个重试
                print(f"【批量移动失败】第 {batch_idx + 1} 批，逐个重试: {e}")
                for fid in batch:
                    try:
                        pan123_move_files(user_id, account_id, [fid], target_folder_id)
                        success += 1
                    except Pan123Error as e2:
                        failed += 1
                        errors.append({"file_id": fid, "reason": str(e2)})
            except Exception as e:
                if attempt < _BATCH_RETRY:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                failed += len(batch)
                for fid in batch:
                    errors.append({"file_id": fid, "reason": str(e)})

    return {"success": success, "failed": failed, "errors": errors}


def pan123_batch_resolve_files(user_id: str, account_id: str,
                               paths: list) -> dict:
    """批量把路径解析成 fileId（按目录分组，每目录一次 list_files）。

    paths: ["/待处理/剧名/S01E01.mkv", ...]
    返回：{path: {"file_id": xxx, "size": xxx, "filename": xxx}, ...}
         （解析失败的 path 不在返回里）
    """
    from collections import defaultdict

    result = {}
    by_dir = defaultdict(list)
    for p in paths:
        p_clean = p.strip("/")
        parts = p_clean.split("/")
        if not parts or not parts[-1]:
            continue
        file_name = parts[-1]
        dir_path = "/".join(parts[:-1])
        by_dir[dir_path].append((p, file_name))

    for dir_path, items in by_dir.items():
        try:
            dir_id = pan123_resolve_path(user_id, account_id, dir_path) if dir_path else "0"
            dir_items = pan123_list_files(user_id, account_id, dir_id)
            name_to_info = {}
            for it in dir_items:
                n = it.get("filename") or it.get("fileName") or ""
                if n:
                    name_to_info[n] = {
                        "file_id": str(it.get("fileId", "")),
                        "size": int(it.get("size", 0) or 0),
                        "filename": n,
                    }
            for p, file_name in items:
                if file_name in name_to_info:
                    result[p] = name_to_info[file_name]
        except Exception as e:
            print(f"【批量解析失败】{dir_path}: {e}")

    return result


def pan123_find_files_in_dir(user_id: str, account_id: str,
                              dir_path: str, filenames: list) -> dict:
    """在某目录下一次 list_files，返回 {filename: info} 映射。

    用于批量查找视频和同名字幕。
    """
    try:
        dir_id = pan123_resolve_path(user_id, account_id, dir_path) if dir_path else "0"
        dir_items = pan123_list_files(user_id, account_id, dir_id)
        want = set(filenames)
        found = {}
        for it in dir_items:
            n = it.get("filename") or it.get("fileName") or ""
            if n in want:
                found[n] = {
                    "file_id": str(it.get("fileId", "")),
                    "size": int(it.get("size", 0) or 0),
                    "filename": n,
                }
        return found
    except Exception as e:
        print(f"【批量查找失败】{dir_path}: {e}")
        return {}
