import os, json, time, uuid, requests, urllib.parse, hashlib, hmac, re, threading
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

requests.packages.urllib3.disable_warnings()
app = FastAPI()

DATA_DIR = "/data"
os.makedirs(DATA_DIR, exist_ok=True)

SECRETS_FILE = os.path.join(DATA_DIR, "secrets.env")
USERS_FILE = os.path.join(DATA_DIR, "users.json")
INVITES_FILE = os.path.join(DATA_DIR, "invites.json")
WEBDAV_FILE = os.path.join(DATA_DIR, "webdav_list.json")
API_CONFIGS_FILE = os.path.join(DATA_DIR, "api_configs.json")
CACHE_FILE = os.path.join(DATA_DIR, "ai_cache.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
STATS_FILE = os.path.join(DATA_DIR, "stats.json")
AUTO_CONFIG_FILE = os.path.join(DATA_DIR, "auto_config.json")
SERIES_INDEX_FILE = os.path.join(DATA_DIR, "series_index.json")
AUTO_LOG_FILE = os.path.join(DATA_DIR, "auto_log.json")

VIDEO_EXTS = ('.mp4', '.mkv', '.avi', '.mov', '.flv', '.ts', '.strm', '.iso')

PROVIDERS = {
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "models": ["deepseek-v4-flash", "deepseek-v4-pro"]},
    "aliyun": {"name": "阿里云百炼 (通义千问)", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "models": ["qwen3.8-max", "qwen3.7-max", "qwen3.7-plus", "qwen3.6-flash", "qwen-max", "qwen-plus", "qwen-flash", "qwen-turbo"]},
    "moonshot": {"name": "月之暗面 (Kimi)", "base_url": "https://api.moonshot.cn/v1", "models": ["kimi-k3", "kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6"]},
    "zhipu": {"name": "智谱AI (GLM)", "base_url": "https://open.bigmodel.cn/api/paas/v4", "models": ["glm-5.3-flash", "glm-5-turbo", "glm-5", "glm-4.7", "glm-4.6", "glm-4.5-air"]},
    "volcengine": {"name": "火山引擎 (豆包)", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "models": ["doubao-seed-2.1-pro", "doubao-pro-32k", "doubao-lite-32k"]},
    "custom": {"name": "自定义 (OpenAI 兼容)", "base_url": "", "models": ["自定义模型"]}
}

# ============ 非影视资源检测 ============
MIN_VIDEO_SIZE = 20 * 1024 * 1024  # 20MB，小于此大小的视频视为广告/sample

NON_MEDIA_KEYWORDS = [
    # 网址
    'www.', 'http://', 'https://', '.com', '.net', '.org', '.cc', '.tv', '.me',
    # 广告 / 推广
    '广告', '宣传', '推广', '加群', '公众号', '微信号', 'telegram',
    '关注', '收藏本站', '永久域名', '备用网址', '最新地址',
    # 资源站水印（可继续加）
    '不太灵', 'bt磁力', '磁力链', '种子下载', '电影天堂', '阳光电影',
    '飘花', '新视觉', '韩剧TV', '美剧天堂', '低端影视',
    # 预告 / 试看
    '预告', '试看', '抢先版', 'sample', 'preview', 'trailer',
]

# OP/ED/PV/SP/NC/CM 等日本动画术语（作为独立词匹配，避免误伤）
_NON_MEDIA_PATTERN = re.compile(
    r'(?:^|[\s._\-])(op|ed|pv|sp|nc|cm|menu|ncop|nced)(?:\d*)?(?:[\s._\-]|$)',
    re.IGNORECASE
)


def check_non_media(filename, file_size=0):
    """检查文件是否为非影视资源。返回命中的原因或 None"""
    # 1. 体积过滤
    if file_size and file_size < MIN_VIDEO_SIZE:
        return f"文件过小（{file_size / 1024 / 1024:.1f}MB）"
    if not filename:
        return None
    # 2. 关键词过滤
    lower = filename.lower()
    for kw in NON_MEDIA_KEYWORDS:
        if kw in lower:
            return f"关键词命中: {kw}"
    # 3. OP/ED/PV 等术语过滤（带边界）
    m = _NON_MEDIA_PATTERN.search(filename)
    if m:
        return f"动漫片头尾/特典: {m.group(1).upper()}"
    return None


GENERIC_FOLDER_NAMES = {
    '影视', '电影', '剧集', '电视剧', '美剧', '英剧', '日剧', '韩剧', '国剧', '港剧', '台剧',
    '动漫', '动画', '国漫', '日番', '美漫', '综艺', '纪录片', '未分类', '其他', '整理', '待处理',
    '新建文件夹', '下载', '片库', '影视库', '媒体', '媒体库', '电视剧库', '电影库', '收藏',
    '欧美', '欧美电影', '欧美剧', '亚洲', '亚洲电影', '亚洲剧', '国产', '国产剧', '大陆剧',
    '高清', '蓝光', '4k', '4K', '1080p', '1080P', '720p', '720P', '原盘', 'remux', 'REMUX',
    '第一季', '第二季', '第三季', '第四季', '第五季', '第六季', '第七季', '第八季', '第九季', '第十季',
    'season 1', 'season 2', 'season 3', 'season 4', 'season 5'
}

LOCK = threading.Lock()
AUTO_TASK_LOCK = threading.Lock()
AUTO_CONFIG_LOCK = threading.Lock()
AUTO_SCHEDULE_EVENT = threading.Event()
AUTO_TASK_RUNNING = {}
AUTO_TASK_CANCEL = {}
AUTO_TASK_STATE = {}
AUTO_TASK_SEMAPHORE = threading.Semaphore(2)
LOGIN_ATTEMPTS = {}
LOGIN_LOCK = threading.Lock()



def _year_suffix(year):
    """年份后缀：有年份返回 ' (YYYY)'，否则空字符串"""
    if not year or year == "未知年份":
        return ""
    return f" ({year})"


# ================= WebDAV 全局限流器 =================
WEBDAV_RATE_LOCK = threading.Lock()
WEBDAV_REQUEST_HISTORY = {}
WEBDAV_MAX_REQ_PER_MIN = 55

def webdav_rate_limit(base_url, cancel_event=None):
    while True:
        with WEBDAV_RATE_LOCK:
            now = time.time()
            history = WEBDAV_REQUEST_HISTORY.get(base_url, [])
            history = [t for t in history if now - t < 60]
            WEBDAV_REQUEST_HISTORY[base_url] = history
            if len(history) < WEBDAV_MAX_REQ_PER_MIN:
                history.append(now)
                WEBDAV_REQUEST_HISTORY[base_url] = history
                return True
            oldest = history[0]
            wait_sec = 60 - (now - oldest) + 0.3
        print(f"【限流】网盘请求超限（{WEBDAV_MAX_REQ_PER_MIN}/分钟），等待 {wait_sec:.1f} 秒...")
        if cancel_event:
            if cancel_event.wait(wait_sec): return False
        else:
            time.sleep(wait_sec)

DEFAULT_TASK = {
    "task_id": "", "name": "自动整理任务", "enabled": False, "mode": "interval",
    "interval_hours": 1, "daily_time": "03:00", "max_files": 300,
    "source_id": "", "source_path": "/待处理", "target_id": "", "target_path": "/成品影视库"
}

def load_json(file, default):
    with LOCK:
        if os.path.exists(file):
            try:
                with open(file, 'r', encoding='utf-8') as f: return json.load(f)
            except Exception: return default
        return default

def save_json(file, data):
    with LOCK:
        try:
            with open(file, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e: print(f"【保存失败】{file}: {e}")

def get_secrets():
    if not os.path.exists(SECRETS_FILE): return {}
    secrets = {}
    with open(SECRETS_FILE, 'r') as f:
        for line in f:
            if '=' in line:
                k, v = line.strip().split('=', 1); secrets[k] = v
    return secrets

def save_secrets(secrets):
    with LOCK:
        with open(SECRETS_FILE, 'w') as f:
            for k, v in secrets.items(): f.write(f"{k}={v}\n")

def get_beijing_time(): return (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')
def get_beijing_date(): return (datetime.utcnow() + timedelta(hours=8)).strftime('%Y-%m-%d')

def hash_password(password, salt=None):
    if salt is None: salt = uuid.uuid4().hex
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000).hex()
    return f"{salt}${pwd_hash}"

def verify_password(password, stored_hash):
    try:
        salt, pwd_hash = stored_hash.split('$')
        return hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 100000).hex() == pwd_hash
    except: return False

def generate_token(user_id, secret_key):
    expire = int(time.time()) + 7 * 24 * 3600
    data = f"{user_id}|{expire}"
    sig = hmac.new(secret_key.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}|{sig}"

def verify_token(token, secret_key):
    try:
        user_id, expire, sig = token.split('|')
        if int(expire) < time.time(): return None
        data = f"{user_id}|{expire}"
        expected_sig = hmac.new(secret_key.encode(), data.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected_sig): return user_id
    except: pass
    return None

def safe_quote_path(path):
    return "/".join([quote(p, safe='') for p in path.split("/") if p])

def clean_cache(cache):
    if len(cache) > 10000:
        keys = list(cache.keys())
        for k in keys[:2000]: del cache[k]
    return cache

def mask_key(key):
    if not key: return ""
    if len(key) <= 8: return "****"
    return key[:4] + "*" * (len(key) - 8) + key[-4:]

def check_login_rate(ip):
    with LOGIN_LOCK:
        now = time.time()
        attempts = LOGIN_ATTEMPTS.get(ip, [])
        attempts = [t for t in attempts if now - t < 60]
        LOGIN_ATTEMPTS[ip] = attempts
        if len(attempts) >= 5: return False
        attempts.append(now)
        return True

def trim_history(history_list, max_batches=200):
    if len(history_list) > max_batches: return history_list[-max_batches:]
    return history_list

def load_auto_config():
    with AUTO_CONFIG_LOCK:
        configs = load_json(AUTO_CONFIG_FILE, [])
    if isinstance(configs, dict):
        old_task = configs
        old_task["task_id"] = str(uuid.uuid4())
        old_task["name"] = old_task.get("name", "自动整理任务")
        configs = [old_task]
        save_auto_config(configs)
    return configs if isinstance(configs, list) else []

def save_auto_config(configs):
    with AUTO_CONFIG_LOCK:
        save_json(AUTO_CONFIG_FILE, configs)

def load_series_index(): return load_json(SERIES_INDEX_FILE, {})
def save_series_index(index): save_json(SERIES_INDEX_FILE, index)
def load_auto_log(): return load_json(AUTO_LOG_FILE, [])

def save_auto_log(logs):
    if len(logs) > 500: logs = logs[-500:]
    save_json(AUTO_LOG_FILE, logs)

def make_series_key(title, year):
    clean_title = re.sub(r'[\s.\-_—:：,，!！?？()（）\[\]【】]+', '', title)
    # 如果 title 里本身含年份，去掉它，避免 key 不一致
    if year and year != "未知年份":
        clean_title = clean_title.replace(str(year), '')
    return f"{clean_title}|{year}"

def extract_folder_context(source_path, current_path):
    if not source_path or not current_path: return ""
    rel_path = current_path.replace(source_path, '').strip('/')
    if not rel_path: return ""
    parts = [p for p in rel_path.split('/') if p]
    meaningful = []
    for p in parts:
        p_lower = p.lower()
        if p_lower in GENERIC_FOLDER_NAMES: continue
        if p.isdigit(): continue
        if re.match(r'^(season|s)\s*\d+$', p_lower): continue
        if re.match(r'^(ep|episode|e)\s*\d+$', p_lower): continue
        meaningful.append(p)
    return "/".join(meaningful[-2:]) if meaningful else ""

def get_ai_throttle_config():
    secrets = get_secrets()
    active_api_id = secrets.get("ACTIVE_API_ID", "")
    configs = load_json(API_CONFIGS_FILE, [])
    config = next((c for c in configs if c["id"] == active_api_id), None)
    if not config:
        return {"provider": "unknown", "sleep_between": 1.0, "retry_wait_base": 5, "max_retries": 3}
    provider = config.get("provider", "")
    if provider == "zhipu":
        return {"provider": "zhipu", "sleep_between": 1.0, "retry_wait_base": 15, "max_retries": 4}
    elif provider == "aliyun":
        return {"provider": "aliyun", "sleep_between": 2.0, "retry_wait_base": 8, "max_retries": 3}
    else:
        return {"provider": provider, "sleep_between": 1.0, "retry_wait_base": 5, "max_retries": 3}

# ================= WebDAV 工具 =================
def webdav_mkcol(base_url, username, password, path, cancel_event=None):
    if not webdav_rate_limit(base_url, cancel_event): return False, "用户中断"
    auth = (username, password)
    url = base_url.rstrip('/') + '/' + safe_quote_path(path.lstrip('/'))
    try:
        res = requests.request("MKCOL", url, auth=auth, timeout=30, verify=False)
        if res.status_code < 400 or res.status_code == 405: return True, res.status_code
        return False, res.status_code
    except Exception as e: return False, str(e)

def dispatch_mkdir(source, full_path, cancel_event=None):
    parts = [p for p in full_path.strip('/').split('/') if p]
    current = ""
    for part in parts:
        current = current + "/" + part if current else "/" + part
        webdav_mkcol(base_url, username, password, current, cancel_event)

def dispatch_check_exists(source, path, cancel_event=None):
    if not webdav_rate_limit(base_url, cancel_event): return False
    auth = (username, password)
    url = base_url.rstrip('/') + '/' + safe_quote_path(path.lstrip('/'))
    headers = {"Depth": "0", "Content-Type": "application/xml"}
    xml_body = '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>'
    try:
        res = requests.request("PROPFIND", url, auth=auth, headers=headers, data=xml_body, timeout=15, verify=False)
        return res.status_code in (207, 200)
    except Exception: return False

def webdav_propfind(base_url, username, password, path, depth=1, retries=2, cancel_event=None):
    auth = (username, password)
    url = base_url.rstrip('/') + '/' + safe_quote_path(path.lstrip('/'))
    headers = {"Depth": str(depth), "Content-Type": "application/xml"}
    xml_body = '<d:propfind xmlns:d="DAV:"><d:prop><d:displayname/><d:resourcetype/></d:prop></d:propfind>'
    RETRYABLE = (500, 502, 503, 504, 429)
    last_err = ""
    for attempt in range(retries + 1):
        if not webdav_rate_limit(base_url, cancel_event):
            return [], "用户中断"
        try:
            res = requests.request("PROPFIND", url, auth=auth, headers=headers, data=xml_body, timeout=30, verify=False)
            if res.status_code in (207, 200):
                root = ET.fromstring(res.content); items = []
                for resp in root.findall('.//{DAV:}response'):
                    href_node = resp.find('{DAV:}href')
                    if href_node is None or not href_node.text: continue
                    href = href_node.text; is_dir = href.endswith('/')
                    res_type = resp.find('.//{DAV:}resourcetype')
                    if res_type is not None and res_type.find('{DAV:}collection') is not None: is_dir = True
                    decoded_href = urllib.parse.unquote(href); name = decoded_href.rstrip('/').split('/')[-1]
                    if not name: continue
                    items.append({"name": name, "href": href, "is_dir": is_dir})
                return items, None
            if res.status_code in RETRYABLE and attempt < retries:
                wait_sec = max(60, 2 ** attempt * 15) if res.status_code in (429, 503) else 2 ** attempt
                if cancel_event and cancel_event.wait(wait_sec): return [], "用户中断"
                last_err = f"HTTP {res.status_code}"
                continue
            return [], f"HTTP {res.status_code}"
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                wait_sec = 2 ** attempt
                if cancel_event and cancel_event.wait(wait_sec): return [], "用户中断"
                continue
    return [], f"重试失败: {last_err}"

def webdav_move(base_url, username, password, src_path, dst_path, retries=4, cancel_event=None):
    auth = (username, password)
    src_url = base_url.rstrip('/') + '/' + safe_quote_path(src_path.lstrip('/'))
    dst_url = base_url.rstrip('/') + '/' + safe_quote_path(dst_path.lstrip('/'))
    headers = {"Destination": dst_url, "Overwrite": "F"}
    RETRYABLE_CODES = (500, 502, 503, 504, 429)
    last_err = ""
    for attempt in range(retries + 1):
        if not webdav_rate_limit(base_url, cancel_event):
            return False, 499, "用户中断"
        try:
            res = requests.request("MOVE", src_url, auth=auth, headers=headers, timeout=90, verify=False)
            if res.status_code in (201, 204): return True, res.status_code, res.text
            if res.status_code in RETRYABLE_CODES and attempt < retries:
                if res.status_code in (429, 503):
                    wait_sec = min(60 + 30 * attempt, 180)
                else:
                    wait_sec = min(3 ** (attempt + 1), 30)
                if cancel_event and cancel_event.wait(wait_sec): return False, 499, "用户中断"
                last_err = f"HTTP {res.status_code}"
                continue
            return False, res.status_code, res.text
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                wait_sec = min(3 ** (attempt + 1), 30)
                if cancel_event and cancel_event.wait(wait_sec): return False, 499, "用户中断"
                continue
    return False, 500, f"重试{retries}次后仍失败: {last_err}"

def dispatch_delete(source, path, cancel_event=None):
    if not webdav_rate_limit(base_url, cancel_event): return False, "用户中断"
    auth = (username, password)
    url = base_url.rstrip('/') + '/' + safe_quote_path(path.lstrip('/'))
    try:
        res = requests.request("DELETE", url, auth=auth, timeout=30, verify=False)
        return res.status_code in (200, 204), res.status_code
    except Exception as e: return False, str(e)

# ================= AI 解析 =================
def ai_parse_internal(filename, folder_context="", source_id=""):
    cache_key = f"{source_id}::::{filename}" if source_id else filename
    cache = load_json(CACHE_FILE, {})
    if cache_key in cache:
        cached = cache[cache_key]
        if isinstance(cached, dict) and cached.get("title"):
            return {"success": True, "result": cached, "cached": True}
        else:
            cache.pop(cache_key, None)
            save_json(CACHE_FILE, cache)

    secrets = get_secrets(); active_api_id = secrets.get("ACTIVE_API_ID", "")
    configs = load_json(API_CONFIGS_FILE, [])
    config = next((c for c in configs if c["id"] == active_api_id), None)
    if not config: return {"success": False, "reason": "未配置 API", "cached": False}
    provider = config["provider"]; api_key = config["api_key"]; model_name = config["model_name"]
    base_url = PROVIDERS.get(provider, {}).get("base_url", "")
    if provider == "custom": base_url = config.get("custom_base_url", "")

    if provider == "zhipu" and (not model_name or model_name in ["", "default"]):
        model_name = "glm-4.7-flash"

    throttle = get_ai_throttle_config()

    prompt = """解析影视文件名，输出 JSON。
格式：{"is_media":true|false,"non_media_reason":"","media_type":"movie|tv|anime","title":"中文名","year":"年份","season":数字|null,"episode":数字|null}
规则：is_media 判断是否真正的影视资源；true 的情况：含影视信息(剧名/年份/季集)、纯 S01E01 格式；false 的情况：含网址(www./http/.com)/广告词(广告/加群/公众号)/资源站水印/预告片/试看片段/非影视内容/动漫的 OP(片头)/ED(片尾)/PV(宣传片)/SP(特典)/NC(无字幕版)/CM(广告)/Menu(菜单) 作为独立词出现；non_media_reason 在 is_media=false 时填简短原因(否则空)；无论 is_media 是什么 title 都必须有值(非影视可填文件名主体)；media_type 电影=movie,剧集=tv,动漫=anime；title 优先中文，无中文用文件名主体；year 无则"未知年份"；S01E01 拆为 season/episode；只输出 JSON。"""

    user_content = f"文件名：{filename}"
    if folder_context:
        user_content += f"\n所在文件夹：{folder_context}"

    ZHIPU_RETRYABLE_CODES = ("1302", "1305", "1313")
    max_retries = throttle["max_retries"]

    def sanitize(title_str):
        return (str(title_str).strip()
                .replace("/", "／").replace("\\", "＼").replace(":", "：")
                .replace("*", "＊").replace("?", "？").replace('"', "＂")
                .replace("<", "＜").replace(">", "＞").replace("|", "｜"))

    for attempt in range(max_retries):
        try:
            payload = {"model": model_name, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": user_content}], "temperature": 0.1}
            if provider != "aliyun": payload["response_format"] = {"type": "json_object"}
            # 适配 GLM-5.3-Flash 强制思考模式
            if provider == "zhipu":
                payload["thinking"] = {"type": "enabled"}
                payload["reasoning_effort"] = "low"
            r = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=(10, 120))

            if r.status_code == 200:
                resp_json = r.json()
                if "error" in resp_json:
                    err_code = str(resp_json["error"].get("code", ""))
                    if err_code in ZHIPU_RETRYABLE_CODES:
                        wait_sec = min(throttle["retry_wait_base"] * (attempt + 1), 90)
                        print(f"【智谱重试】错误码 {err_code}，等待 {wait_sec} 秒...")
                        time.sleep(wait_sec)
                        continue
                    return {"success": False, "reason": f"API错误: {resp_json['error'].get('message', '未知')}", "cached": False}

                content = resp_json["choices"][0]["message"]["content"]
                content = content.replace("```json", "").replace("```", "").strip()
                json_start = content.find("{")
                json_end = content.rfind("}")
                if json_start < 0 or json_end <= json_start:
                    print(f"【AI 格式异常】未找到 JSON 对象，原始内容: {content[:200]}")
                    time.sleep(2)
                    continue
                content = content[json_start:json_end+1]

                try:
                    result = json.loads(content)
                except json.JSONDecodeError:
                    print(f"【AI 解析】JSON提取失败: {content[:200]}")
                    time.sleep(2)
                    continue

                if not isinstance(result, dict) or not result.get("title") or not str(result.get("title")).strip():
                    print(f"【AI 安检拦截】返回的不是有效对象: {str(result)[:200]}")
                    time.sleep(2)
                    continue

                result["title"] = sanitize(result["title"])
                if not result.get("year"): result["year"] = "未知年份"
                if not result.get("media_type"): result["media_type"] = "movie"

                cache = clean_cache(cache); cache[cache_key] = result; save_json(CACHE_FILE, cache)
                return {"success": True, "result": result, "cached": False}
            else:
                if "response_format" in payload and r.status_code in (400, 422):
                    del payload["response_format"]
                    r2 = requests.post(f"{base_url.rstrip('/')}/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=payload, timeout=(10, 120))
                    if r2.status_code == 200:
                        resp_json = r2.json()
                        if "error" not in resp_json:
                            content = resp_json["choices"][0]["message"]["content"]
                            content = content.replace("```json", "").replace("```", "").strip()
                            json_start = content.find("{"); json_end = content.rfind("}")
                            if json_start >= 0 and json_end > json_start:
                                try:
                                    result = json.loads(content[json_start:json_end+1])
                                    if isinstance(result, dict) and result.get("title") and str(result.get("title")).strip():
                                        result["title"] = sanitize(result["title"])
                                        if not result.get("year"): result["year"] = "未知年份"
                                        if not result.get("media_type"): result["media_type"] = "movie"
                                        cache = clean_cache(cache); cache[cache_key] = result; save_json(CACHE_FILE, cache)
                                        return {"success": True, "result": result, "cached": False}
                                except: pass

                if r.status_code == 429 and attempt < max_retries - 1:
                    wait_sec = min(throttle["retry_wait_base"] * (attempt + 1), 90)
                    time.sleep(wait_sec)
                    continue
                print(f"【AI HTTP错误】{provider} 返回 {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"【AI 解析异常】{filename}: {e}")
        time.sleep(throttle["retry_wait_base"] / 3)
    return {"success": False, "reason": "AI 解析失败", "cached": False}

# ================= 自动任务核心 =================
def ensure_target_folder(source, folder_path, cancel_event=None):
    """统一分派：创建目标目录"""
    from app.source_dispatch import dispatch_check_exists, dispatch_mkdir
    if not dispatch_check_exists(source, folder_path, cancel_event=cancel_event):
        dispatch_mkdir(source, folder_path, cancel_event=cancel_event)
def cleanup_empty_dirs_from_moved(source, visited_dirs, source_root, candidate_parents, cancel_event=None):
    """优化版清理：只检查"本次 MOVE 涉及"的目录及其祖先，从深到浅处理。
    每个候选目录仍会 PROPFIND 二次确认，确保不误删。
    """
    from app.source_dispatch import dispatch_propfind, dispatch_delete
    source_root_clean = source_root.rstrip('/')
    cleaned_list = []

    # 收集候选目录及其祖先链（一直到 source_root 之前）
    all_check = set()
    for p in candidate_parents:
        cur = p.rstrip('/')
        while cur and cur != '/' and cur != source_root_clean and not cur.startswith(source_root_clean + '/'):
            break
        cur = p.rstrip('/')
        while cur and cur != '/' and cur != source_root_clean:
            all_check.add(cur)
            cur = cur.rsplit('/', 1)[0] or '/'

    # 按深度从深到浅排序
    sorted_dirs = sorted(all_check, key=lambda p: p.count('/'), reverse=True)

    for dir_path in sorted_dirs:
        if cancel_event and cancel_event.is_set():
            break
        dir_clean = dir_path.rstrip('/')
        if not dir_clean or dir_clean == '/' or dir_clean == source_root_clean:
            continue

        items, err = dispatch_propfind(source, dir_path, depth=1, cancel_event=cancel_event)
        if err:
            continue

        dir_name = dir_path.rstrip('/').split('/')[-1]
        non_self = [i for i in items if i['name'] != dir_name]
        has_video = any((not i['is_dir']) and i['name'].lower().endswith(VIDEO_EXTS) for i in non_self)
        has_subdir = any(i['is_dir'] for i in non_self)
        should_delete = False
        if not non_self:
            should_delete = True
        elif not has_video and not has_subdir:
            for it in non_self:
                fpath = dir_path.rstrip('/') + '/' + it['name']
                dispatch_delete(source, fpath, cancel_event=cancel_event)
                if cancel_event:
                    if cancel_event.wait(0.3): break
                else:
                    time.sleep(0.3)
            should_delete = True
        if should_delete:
            ok, _ = dispatch_delete(source, dir_path, cancel_event=cancel_event)
            if ok:
                cleaned_list.append(dir_path)
        if cancel_event:
            if cancel_event.wait(0.3):
                break
        else:
            time.sleep(0.3)

    return len(cleaned_list), cleaned_list


def cleanup_empty_dirs_from_visited(source, visited_dirs, source_root, cancel_event=None):
    from app.source_dispatch import dispatch_propfind, dispatch_delete
    sorted_dirs = sorted(visited_dirs, key=lambda p: p.count('/'), reverse=True)
    source_root_clean = source_root.rstrip('/')
    cleaned_list = []
    for dir_path in sorted_dirs:
        if cancel_event and cancel_event.is_set(): break
        dir_clean = dir_path.rstrip('/')
        if not dir_clean or dir_clean == '/' or dir_clean == source_root_clean: continue
        items, err = dispatch_propfind(source, dir_path, depth=1, cancel_event=cancel_event)
        if err: continue
        dir_name = dir_path.rstrip('/').split('/')[-1]
        non_self = [i for i in items if i['name'] != dir_name]
        has_video = any((not i['is_dir']) and i['name'].lower().endswith(VIDEO_EXTS) for i in non_self)
        has_subdir = any(i['is_dir'] for i in non_self)
        should_delete = False
        if not non_self:
            should_delete = True
        elif not has_video and not has_subdir:
            for it in non_self:
                fpath = dir_path.rstrip('/') + '/' + it['name']
                dispatch_delete(source, fpath, cancel_event=cancel_event)
                if cancel_event:
                    if cancel_event.wait(0.3): break
                else:
                    time.sleep(0.3)
            should_delete = True
        if should_delete:
            ok, _ = dispatch_delete(source, dir_path, cancel_event=cancel_event)
            if ok: cleaned_list.append(dir_path)
            if cancel_event:
                if cancel_event.wait(0.3): break
            else:
                time.sleep(0.3)
    return len(cleaned_list), cleaned_list

def _finalize_log(log_entry):
    logs = load_auto_log(); logs.append(log_entry); save_auto_log(logs)

def move_to_failed(source, file_path, file_name, failed_dir, cancel_event=None):
    """移动到 failed/，返回 True/False"""
    try:
        from app.source_dispatch import dispatch_move
        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        new_name = f"{timestamp}_{file_name}"
        target = failed_dir.rstrip('/') + "/" + new_name
        ok, code, err = dispatch_move(source, file_path, target, cancel_event=cancel_event)
        if ok:
            return True
        else:
            print(f"【移入failed失败】{file_name}: HTTP {code} - {str(err)[:100]}")
            return False
    except Exception as e:
        print(f"【移入failed异常】{file_name}: {e}")
        return False

def run_auto_task(task_id, force=False):
    from app.source_dispatch import (
        dispatch_propfind, dispatch_move, dispatch_delete,
        dispatch_check_exists, dispatch_mkdir,
    )
    task_configs = load_auto_config()
    task = next((t for t in task_configs if t["task_id"] == task_id), None)
    if not task:
        return {"error": "任务不存在", "time_str": get_beijing_time(), "total": 0, "success": 0, "failed": 0, "failed_files": []}

    if task_id not in AUTO_TASK_CANCEL:
        AUTO_TASK_CANCEL[task_id] = threading.Event()
    else:
        AUTO_TASK_CANCEL[task_id].clear()

    with AUTO_TASK_LOCK:
        if AUTO_TASK_RUNNING.get(task_id, False):
            return {"error": "任务正在运行中", "time_str": get_beijing_time(), "total": 0, "success": 0, "failed": 0, "failed_files": []}
        AUTO_TASK_RUNNING[task_id] = True

    AUTO_TASK_STATE[task_id] = {
        "running": True, "started_at": time.time(), "cancel_requested": False, "last_result": None,
        "phase": "scan", "current_file": "准备扫描...", "source_type": "",
        "scan_dirs_visited": 0, "scan_files_found": 0,
        "parse_current_batch": 0, "parse_total_batches": 0, "parse_done": 0, "parse_cached": 0,
        "move_current": 0, "move_total": 0, "move_success": 0, "move_failed": 0,
        "current_file_old": "", "current_file_new": "", "cleaned_count": 0,
    }

    cancel_evt = AUTO_TASK_CANCEL[task_id]
    log_entry = {
        "task_id": task_id, "time": time.time(), "time_str": get_beijing_time(),
        "total": 0, "success": 0, "failed": 0,
        "failed_files": [], "success_files": [], "cleaned_dirs": [],
        "error": None, "cancelled": False, "ai_batches": 0, "ai_cached": 0,
        "non_media_files": [], "source_type": "",
    }
    AUTO_TASK_SEMAPHORE.acquire()
    try:
        users = load_json(USERS_FILE, {})
        admin_user = users.get("admin", {})
        today = get_beijing_date()
        if admin_user.get("last_quota_date") != today:
            admin_user["today_used"] = 0
            admin_user["last_quota_date"] = today
            users["admin"] = admin_user
            save_json(USERS_FILE, users)
        quota_limit = 999999 if admin_user.get("role") == "admin" else (3000 if admin_user.get("role") == "member" else 100)
        remain_quota = quota_limit - admin_user.get("today_used", 0)
        if remain_quota <= 0:
            log_entry["error"] = "今日额度已耗尽"
            _finalize_log(log_entry)
            return log_entry

        source_id = task.get("source_id")
        source_path = task.get("source_path", "/待处理").rstrip('/') or "/"
        target_path = task.get("target_path", "/成品影视库").rstrip('/') or "/"
        max_files = min(int(task.get("max_files", 300)), 500, remain_quota)

        # ========== 源解析（统一接口） ==========
        from app.unified_source import resolve_source
        source = resolve_source("admin", source_id, WEBDAV_FILE)
        if not source:
            log_entry["error"] = "源不存在或已失效"
            _finalize_log(log_entry)
            return log_entry
        _stype = source.get("type", "webdav")
        AUTO_TASK_STATE[task_id]["source_type"] = _stype
        log_entry["source_type"] = _stype

        scan_start = time.time()
        scan_deadline = 7200
        SKIP_DIR_NAMES = ('@eaDir', '#recycle', '.Trash', '$RECYCLE.BIN', 'System Volume Information', 'failed', 'lost+found', '.DS_Store', '_trash')
        scan_stack = [source_path]
        visited = set()
        skipped_count = 0
        scan_truncated_reason = None
        scan_order_log = []

        # ========== 阶段 1：扫描 ==========
        collected = []
        while scan_stack:
            if cancel_evt.is_set():
                log_entry["cancelled"] = True
                scan_truncated_reason = "用户中断"
                break
            if time.time() - scan_start > scan_deadline:
                scan_truncated_reason = f"总执行超时"
                break
            if len(collected) >= max_files:
                scan_truncated_reason = f"已达单次处理上限（{max_files}个）"
                break

            current_path = scan_stack.pop()
            if current_path in visited:
                continue
            visited.add(current_path)
            scan_order_log.append(current_path)

            AUTO_TASK_STATE[task_id]["scan_dirs_visited"] = len(visited)
            AUTO_TASK_STATE[task_id]["scan_files_found"] = len(collected)
            AUTO_TASK_STATE[task_id]["current_file"] = current_path

            items, err = dispatch_propfind(source, current_path, depth=1, cancel_event=cancel_evt)
            if err:
                continue

            sub_dirs = []
            for item in items:
                if item["is_dir"]:
                    if any(skip in item["name"] for skip in SKIP_DIR_NAMES):
                        skipped_count += 1
                        continue
                    sub_dirs.append(item["name"])
                else:
                    if not item["name"].lower().endswith(VIDEO_EXTS):
                        continue
                    folder_context = extract_folder_context(source_path, current_path)
                    _non_media_reason = check_non_media(item["name"], item.get("size", 0))
                    collected.append({
                        "name": item["name"],
                        "path": current_path.rstrip('/') + "/" + item["name"],
                        "folder_context": folder_context,
                        "non_media": bool(_non_media_reason),
                        "non_media_reason": _non_media_reason or "",
                        "size": item.get("size", 0),
                    })
                    AUTO_TASK_STATE[task_id]["scan_files_found"] = len(collected)
                    if len(collected) >= max_files:
                        break

            sub_dirs.sort(key=lambda s: s.lower())
            for d in reversed(sub_dirs):
                scan_stack.append(current_path.rstrip('/') + "/" + d)

        scan_elapsed = round(time.time() - scan_start, 1)
        print(f"【自动任务】扫描完成，共发现 {len(collected)} 个视频")

        # ========== 阶段 2：批量 AI 解析 ==========
        AUTO_TASK_STATE[task_id]["phase"] = "parse"
        ai_results = {}
        to_parse = []
        cache = load_json(CACHE_FILE, {})

        for f in collected:
            if f.get("non_media"):
                continue
            key = f"{source_id}::::{f['name']}" if source_id else f['name']
            cached = cache.get(key)
            if isinstance(cached, dict) and cached.get("title"):
                std_name = _make_std_name(f['name'], cached)
                ai_results[f['name']] = {"result": cached, "std_name": std_name, "cached": True, "error": ""}
                log_entry["ai_cached"] += 1
                AUTO_TASK_STATE[task_id]["parse_done"] += 1
                AUTO_TASK_STATE[task_id]["parse_cached"] = log_entry["ai_cached"]
            else:
                to_parse.append(f)

        print(f"【自动任务】缓存命中 {log_entry['ai_cached']} 个，需 AI 解析 {len(to_parse)} 个")

        if to_parse and not cancel_evt.is_set():
            try:
                from app.batch_parse import _ai_call as _batch_ai_call
            except Exception as e:
                print(f"【自动任务】批量模块导入失败: {e}")

            AI_BATCH_SIZE = 15
            batches = [to_parse[i:i+AI_BATCH_SIZE] for i in range(0, len(to_parse), AI_BATCH_SIZE)]
            AUTO_TASK_STATE[task_id]["parse_total_batches"] = len(batches)

            secrets = get_secrets()
            active_api_id = secrets.get("ACTIVE_API_ID", "")
            configs = load_json(API_CONFIGS_FILE, [])
            cfg = next((c for c in configs if c["id"] == active_api_id), None)

            for bi, batch in enumerate(batches):
                if cancel_evt.is_set():
                    log_entry["cancelled"] = True
                    break
                if time.time() - scan_start > scan_deadline:
                    scan_truncated_reason = "总执行超时"
                    break

                AUTO_TASK_STATE[task_id]["parse_current_batch"] = bi + 1
                AUTO_TASK_STATE[task_id]["current_file"] = f"第 {bi+1}/{len(batches)} 批 · {batch[0]['name']}"

                if not cfg:
                    for bf in batch:
                        ai_results[bf['name']] = {"result": None, "std_name": "", "cached": False, "error": "未配置 API"}
                    continue

                provider = cfg["provider"]
                api_key = cfg["api_key"]
                model = cfg.get("model_name") or ""
                base_url_ai = PROVIDERS.get(provider, {}).get("base_url", "")
                if provider == "custom":
                    base_url_ai = cfg.get("custom_base_url", "")
                if provider == "zhipu" and (not model or model == "default"):
                    model = "glm-4.7-flash"

                names = [bf['name'] for bf in batch]
                contexts = [bf.get('folder_context', '') for bf in batch]
                rmap, err = _batch_ai_call(names, provider, api_key, model, base_url_ai, contexts)
                log_entry["ai_batches"] += 1

                if err:
                    print(f"【自动任务】第 {bi+1} 批失败: {err}")
                    for bf in batch:
                        ai_results[bf['name']] = {"result": None, "std_name": "", "cached": False, "error": err}
                else:
                    for bf in batch:
                        r = rmap.get(bf['name'])
                        if not r:
                            stem = os.path.splitext(bf['name'])[0]
                            for k, v in rmap.items():
                                if os.path.splitext(k)[0] == stem:
                                    r = v
                                    break
                        if r:
                            cache_key = f"{source_id}::::{bf['name']}" if source_id else bf['name']
                            cache[cache_key] = r
                            std_name = _make_std_name(bf['name'], r)
                            ai_results[bf['name']] = {"result": r, "std_name": std_name, "cached": False, "error": ""}
                        else:
                            ai_results[bf['name']] = {"result": None, "std_name": "", "cached": False, "error": "AI 漏项"}

                AUTO_TASK_STATE[task_id]["parse_done"] = log_entry["ai_cached"] + sum(1 for x in ai_results.values() if not x.get("cached"))
                cache = clean_cache(cache)
                save_json(CACHE_FILE, cache)

                if bi < len(batches) - 1:
                    throttle = get_ai_throttle_config()
                    if cancel_evt.wait(throttle.get("sleep_between", 1.0)):
                        log_entry["cancelled"] = True
                        break

# ========== 阶段 3：MOVE ==========
        AUTO_TASK_STATE[task_id]["phase"] = "move"
        AUTO_TASK_STATE[task_id]["move_total"] = len(collected)

        failed_dir = source_path.rstrip('/') + "/failed"
        ensure_target_folder(source, failed_dir, cancel_event=cancel_evt)

        # ---------- 1. 本地计算所有文件的归档计划（不调 API）----------
        index = load_series_index()
        plans = []
        non_media_list = []
        _is_batch_mode = (_stype == "pan123")
        if _is_batch_mode:
            AUTO_TASK_STATE[task_id]["current_file"] = f"⚡ 批量归档中（{len(collected)} 个）..."

        # _trash 目录（按需创建，避免无谓的 API 调用）
        _trash_dir = source_path.rstrip('/') + "/_trash"
        _trash_created = False

        def _ensure_trash():
            nonlocal _trash_created
            if not _trash_created:
                ensure_target_folder(source, _trash_dir, cancel_event=cancel_evt)
                _trash_created = True

        for idx, f in enumerate(collected):
            if cancel_evt.is_set():
                log_entry["cancelled"] = True
                break

            # 情况 A：体积/关键词命中
            if f.get("non_media"):
                non_media_list.append({
                    "name": f['name'],
                    "reason": f.get("non_media_reason", "非影视资源"),
                })
                try:
                    _ensure_trash()
                    move_to_failed(source, f['path'], f['name'], _trash_dir, cancel_event=cancel_evt)
                except Exception as e:
                    print(f"【非影视移动失败】{f['name']}: {e}")
                    log_entry["failed"] += 1
                    log_entry["failed_files"].append({
                        "name": f['name'],
                        "reason": f"非影视资源移动失败: {str(e)[:80]}"
                    })
                continue

            parse_info = ai_results.get(f['name'])
            if not parse_info or not parse_info.get("result"):
                reason = (parse_info or {}).get("error", "AI 解析失败")
                log_entry["failed"] += 1
                log_entry["failed_files"].append({"name": f['name'], "reason": reason})
                continue

            # 情况 B：AI 判断为非影视
            _ai_result = parse_info["result"]
            if _ai_result.get("is_media") is False:
                non_media_list.append({
                    "name": f['name'],
                    "reason": f"AI 判断: {_ai_result.get('non_media_reason') or '非影视资源'}",
                })
                try:
                    _ensure_trash()
                    move_to_failed(source, f['path'], f['name'], _trash_dir, cancel_event=cancel_evt)
                except Exception as e:
                    print(f"【非影视移动失败】{f['name']}: {e}")
                    log_entry["failed"] += 1
                    log_entry["failed_files"].append({
                        "name": f['name'],
                        "reason": f"非影视资源移动失败: {str(e)[:80]}"
                    })
                continue

            result = parse_info["result"]
            title = result.get("title", "未知影视")
            year = result.get("year", "未知年份")
            media_type = result.get("media_type", "movie")
            season = result.get("season") or 1
            episode = result.get("episode") or 1
            ext = os.path.splitext(f['name'])[1]
            series_key = make_series_key(title, year)

            if media_type == "movie":
                category = "电影"
                work_folder = f"{target_path.rstrip('/')}/{category}/{title}{_year_suffix(year)}"
                target_file = f"{title}{_year_suffix(year)}{ext}"
            elif media_type == "anime":
                category = "动漫"
                work_folder = f"{target_path.rstrip('/')}/{category}/{title}{_year_suffix(year)}"
                target_file = f"{title}{_year_suffix(year)} - S{str(season).zfill(2)}E{str(episode).zfill(2)}{ext}"
            else:
                category = "剧集"
                work_folder = f"{target_path.rstrip('/')}/{category}/{title}{_year_suffix(year)}"
                target_file = f"{title}{_year_suffix(year)} - S{str(season).zfill(2)}E{str(episode).zfill(2)}{ext}"

            if series_key in index:
                work_folder = index[series_key]["root_path"]

            if media_type == "movie":
                target_folder = work_folder
            else:
                target_folder = work_folder + f"/Season {str(season).zfill(2)}"

            if series_key not in index:
                index[series_key] = {
                    "type": media_type, "title": title, "year": year,
                    "root_path": work_folder,
                    "created_at": time.time(), "updated_at": time.time()
                }
            else:
                index[series_key]["updated_at"] = time.time()

            target_full_path = target_folder.rstrip('/') + "/" + target_file
            plans.append({
                "old_path": f['path'],
                "old_name": f['name'],
                "new_name": target_file,
                "target_folder": target_folder,
                "target_full_path": target_full_path,
            })

        save_series_index(index)
        print(f"【归档】本地计划完成，待归档 {len(plans)} 个")

        # ---------- 2. 分派给驱动执行 ----------
        from app.drivers.registry import get_driver

        try:
            driver = get_driver(source.get("type", "webdav"))
        except ValueError as e:
            log_entry["error"] = f"未知源类型: {source.get('type')}"
            _finalize_log(log_entry)
            return log_entry

        ctx = {
            "log_entry": log_entry,
            "cancel_evt": cancel_evt,
            "failed_dir": failed_dir,
            "move_to_failed": move_to_failed,
            "admin_user": admin_user,
            "state": AUTO_TASK_STATE[task_id],
        }

        try:
            result = driver.execute_archive_plan(source, plans, ctx)
            print(f"【归档】驱动返回: {result}")
        except Exception as e:
            print(f"【归档】驱动异常: {e}")
            log_entry["error"] = f"归档异常: {str(e)}"

        log_entry["non_media_files"] = non_media_list
        AUTO_TASK_STATE[task_id]["move_success"] = log_entry["success"]
        AUTO_TASK_STATE[task_id]["move_failed"] = log_entry["failed"]


        # ========== 阶段 4：清理 ==========

        # 兼容旧代码：统计已处理文件数
        processed_count = (log_entry.get("success", 0)
                          + log_entry.get("failed", 0)
                          + log_entry.get("skipped", 0)
                          + len(log_entry.get("non_media_files", [])))
        AUTO_TASK_STATE[task_id]["phase"] = "cleanup"
        AUTO_TASK_STATE[task_id]["current_file_old"] = ""
        AUTO_TASK_STATE[task_id]["current_file_new"] = ""

        cleaned_count = 0
        cleaned_list = []
        if not log_entry["cancelled"]:
            if not collected:
                # 本次没扫到视频，用全量清理（处理"只剩空目录"场景）
                cleaned_count, cleaned_list = cleanup_empty_dirs_from_visited(source, visited, source_path, cancel_evt)
            else:
                # 有视频整理，用快速局部清理
                candidate_parents = set()
                for c in collected:
                    parent = c["path"].rsplit('/', 1)[0] or '/'
                    candidate_parents.add(parent)
                cleaned_count, cleaned_list = cleanup_empty_dirs_from_moved(source, visited, source_path, candidate_parents, cancel_evt)

        log_entry["cleaned_empty_dirs"] = cleaned_count
        log_entry["cleaned_dirs"] = cleaned_list
        scan_elapsed = round(time.time() - scan_start, 1)
        log_entry["scan_elapsed_sec"] = scan_elapsed
        log_entry["scan_dirs_visited"] = len(visited)
        log_entry["scan_dirs_skipped"] = skipped_count
        log_entry["scan_order_tail"] = scan_order_log[-50:]
        if scan_truncated_reason and not log_entry["error"]:
            if "已达单次处理上限" in (scan_truncated_reason or ""):
                log_entry["info"] = f"✅ 已达本次处理上限（{max_files} 个），剩余文件将在下次任务继续处理"
            else:
                log_entry["error"] = f"扫描提前停止（{scan_truncated_reason}），已处理 {processed_count} 个文件"

        AUTO_TASK_STATE[task_id]["phase"] = "done"
        AUTO_TASK_STATE[task_id]["current_file"] = ""

        if processed_count == 0 and not log_entry["error"]:
            log_entry["info"] = f"✅ 已全部整理（耗时 {scan_elapsed} 秒，访问 {len(visited)} 个目录）"

        users["admin"] = admin_user
        save_json(USERS_FILE, users)
        stats = load_json(STATS_FILE, {})
        admin_stats = stats.get("admin", {"total_renamed": 0})
        admin_stats["total_renamed"] += log_entry["success"]
        stats["admin"] = admin_stats
        save_json(STATS_FILE, stats)

        print(f"【任务完成】扫描 {len(collected)}，AI 批次 {log_entry['ai_batches']}，缓存命中 {log_entry['ai_cached']}，成功 {log_entry['success']}，失败 {log_entry['failed']}")
        _finalize_log(log_entry)
        return log_entry
    except Exception as e:
        log_entry["error"] = f"任务异常: {str(e)}"
        _finalize_log(log_entry)
        return log_entry
    finally:
        try:
            AUTO_TASK_SEMAPHORE.release()
        except Exception:
            pass
        AUTO_TASK_RUNNING[task_id] = False
        if task_id in AUTO_TASK_STATE:
            AUTO_TASK_STATE[task_id]["running"] = False
            AUTO_TASK_STATE[task_id]["current_file"] = ""
            AUTO_TASK_STATE[task_id]["last_result"] = log_entry



# ================= 后台调度线程 =================
def schedule_worker():
    last_run_time = {}; last_run_date = {}
    while not AUTO_SCHEDULE_EVENT.is_set():
        try:
            configs = load_auto_config()
            now = datetime.utcnow() + timedelta(hours=8)
            current_hour_str = now.strftime('%H:%M'); current_date = now.strftime('%Y-%m-%d')

            for task in configs:
                task_id = task.get("task_id")
                if not task_id or not task.get("enabled"): continue
                if AUTO_TASK_RUNNING.get(task_id, False): continue

                should_run = False
                if task.get("mode") == "interval":
                    interval_sec = max(360, int(float(task.get("interval_hours", 1)) * 3600))
                    if time.time() - last_run_time.get(task_id, 0) >= interval_sec: should_run = True
                elif task.get("mode") == "daily":
                    if current_hour_str == task.get("daily_time", "03:00") and last_run_date.get(task_id) != current_date: should_run = True

                if should_run:
                    print(f"【自动任务】{task_id} - {get_beijing_time()} 开始执行")
                    def run_task(tid, cdate):
                        try: run_auto_task(tid, force=False)
                        except Exception as e: print(f"【自动任务异常】{tid}: {e}")
                        finally:
                            # 方案B：任务结束后才更新计时器，实现"严格间隔"
                            last_run_time[tid] = time.time()
                            last_run_date[tid] = cdate
                    # 临时把 last_run_time 设为未来，防止任务启动瞬间被重复触发
                    last_run_time[task_id] = time.time() + 86400 * 30
                    threading.Thread(target=run_task, args=(task_id, current_date), daemon=True).start()
        except Exception as e: print(f"【调度线程异常】{e}")
        AUTO_SCHEDULE_EVENT.wait(30)

def init_system():
    users = load_json(USERS_FILE, {})
    if "admin" not in users:
        init_pwd = os.environ.get("MEDIAFIX_INIT_PWD") or "chenchengcheng666"
        admin_username = os.environ.get("MEDIAFIX_ADMIN_USER") or "chenchengcheng666"
        print(f"【初始化】管理员账号已创建，用户名: {admin_username}，初始密码: {init_pwd}")
        users["admin"] = {"id": "admin", "username": admin_username, "nickname": "管理员", "avatar": "", "password_hash": hash_password(init_pwd), "role": "admin", "status": "active", "today_used": 0, "last_quota_date": get_beijing_date(), "created_at": time.time()}
        save_json(USERS_FILE, users)
    else:
        updated = False
        for uid, u in users.items():
            if "nickname" not in u: u["nickname"] = u.get("username", "用户"); updated = True
            if "avatar" not in u: u["avatar"] = ""; updated = True
        if updated: save_json(USERS_FILE, users)

    secrets = get_secrets()
    if not secrets.get("SECRET_KEY"): secrets["SECRET_KEY"] = uuid.uuid4().hex; save_secrets(secrets)
    if not os.path.exists(API_CONFIGS_FILE): save_json(API_CONFIGS_FILE, [])
    if not secrets.get("ACTIVE_API_ID"): secrets["ACTIVE_API_ID"] = ""; save_secrets(secrets)
    if not os.path.exists(AUTO_CONFIG_FILE): save_auto_config([])
    if not os.path.exists(SERIES_INDEX_FILE): save_series_index({})
    if not os.path.exists(AUTO_LOG_FILE): save_auto_log([])

init_system()
_scheduler_thread = threading.Thread(target=schedule_worker, daemon=True)
_scheduler_thread.start()

# ================= 认证中间件 =================
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path not in ["/api/login", "/api/register", "/api/debug_parse", "/api/health"]:
        token = request.headers.get("Authorization")
        if not token or not token.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"detail": "未登录或登录已过期"})
        secrets = get_secrets(); user_id = verify_token(token[7:], secrets.get("SECRET_KEY", ""))
        if not user_id:
            return JSONResponse(status_code=401, content={"detail": "未登录或登录已过期"})
        users = load_json(USERS_FILE, {}); user = users.get(user_id)
        if not user or user.get("status") == "disabled":
            return JSONResponse(status_code=401, content={"detail": "账号已被禁用或不存在"})
        request.state.user = user
    return await call_next(request)

# ================= 递归扫描 =================
def recursive_scan(source, base_path, current_depth, max_depth, req_name):
    from app.source_dispatch import dispatch_propfind
    if current_depth > max_depth: return
    items, err = dispatch_propfind(source, base_path, depth=1)
    if err: return
    for item in items:
        if req_name and item['name'] == req_name: continue
        if item['is_dir']:
            yield from recursive_scan(source, base_path + "/" + item['name'], current_depth + 1, max_depth, req_name)
        else:
            if item['name'].lower().endswith(VIDEO_EXTS):
                yield {"name": item['name'], "path": base_path + "/" + item['name']}

# ================= 数据模型 =================
class LoginRequest(BaseModel): username: str; password: str
class RegisterRequest(BaseModel): username: str; password: str; invite_code: str
class APIConfigRequest(BaseModel): id: str = None; name: str; provider: str; api_key: str; model_name: str; custom_base_url: str = ""
class WebDAVSource(BaseModel): id: str = None; name: str; url: str; username: str; password: str
class WebDAVTestRequest(BaseModel): url: str; username: str; password: str
class BrowseRequest(BaseModel): source_id: str; path: str
class ScanRequest(BaseModel): source_id: str; path: str
class AIRequest(BaseModel):
    filename: str
    folder_context: str = ""
    source_id: str = ""
class RenameFileItem(BaseModel): old_path: str; new_name: str
class RenameRequest(BaseModel): source_id: str; files: list[RenameFileItem]
class ChangePasswordRequest(BaseModel): old_password: str; new_password: str
class UpdateProfileRequest(BaseModel): nickname: str = None; avatar: str = None
class AdminActionRequest(BaseModel): user_id: str; action: str; new_password: str = None
class InviteCreateRequest(BaseModel): role: str = "normal"; expire_hours: int = 168
class AutoTaskConfigRequest(BaseModel):
    task_id: str = ""; name: str = "自动整理任务"; enabled: bool = False; mode: str = "interval"
    interval_hours: float = 1; daily_time: str = "03:00"; max_files: int = 300
    source_id: str = ""; source_path: str = "/待处理"; target_id: str = ""; target_path: str = "/成品影视库"
class TaskActionRequest(BaseModel): task_id: str

# ================= 用户接口 =================
@app.get("/api/health")
def health(): return {"status": "ok"}

@app.post("/api/login")
def login(req: LoginRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not check_login_rate(client_ip):
        raise HTTPException(status_code=429, detail="登录尝试过于频繁，请 1 分钟后再试")
    users = load_json(USERS_FILE, {})
    for uid, user in users.items():
        if user.get("username") == req.username:
            if user.get("status") == "disabled":
                raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")
            if verify_password(req.password, user.get("password_hash")):
                with LOGIN_LOCK:
                    LOGIN_ATTEMPTS.pop(client_ip, None)
                secrets = get_secrets(); token = generate_token(uid, secrets.get("SECRET_KEY", ""))
                return {"status": "ok", "token": token, "role": user.get("role")}
            else:
                raise HTTPException(status_code=400, detail="用户名或密码错误")
    raise HTTPException(status_code=400, detail="用户名或密码错误")

@app.post("/api/register")
def register(req: RegisterRequest):
    if not re.match(r'^[a-zA-Z0-9_\u4e00-\u9fa5]{3,20}$', req.username):
        raise HTTPException(status_code=400, detail="用户名只能包含中英文、数字、下划线，长度 3-20 位")
    invites = load_json(INVITES_FILE, [])
    invite = next((i for i in invites if i["code"] == req.invite_code), None)
    if not invite: raise HTTPException(status_code=400, detail="邀请码不存在")
    if invite.get("status") != "unused": raise HTTPException(status_code=400, detail="邀请码已被使用或已作废")
    if invite.get("expire_at") and invite["expire_at"] < time.time(): raise HTTPException(status_code=400, detail="邀请码已过期")
    users = load_json(USERS_FILE, {})
    if any(u.get("username") == req.username for u in users.values()): raise HTTPException(status_code=400, detail="用户名已存在")
    role = invite.get("role", "normal"); uid = str(uuid.uuid4())
    users[uid] = {"id": uid, "username": req.username, "nickname": req.username, "avatar": "", "password_hash": hash_password(req.password), "role": role, "status": "active", "today_used": 0, "last_quota_date": get_beijing_date(), "created_at": time.time()}
    save_json(USERS_FILE, users)
    invite["status"] = "used"; invite["used_by"] = req.username; invite["used_at"] = time.time()
    save_json(INVITES_FILE, invites)
    return {"status": "ok", "message": "注册成功"}

@app.get("/api/status")
def get_status(request: Request):
    user = request.state.user; stats = load_json(STATS_FILE, {}).get(user["id"], {"total_renamed": 0}); today = get_beijing_date()
    if user.get("last_quota_date") != today:
        users = load_json(USERS_FILE, {})
        if user["id"] in users:
            users[user["id"]]["today_used"] = 0; users[user["id"]]["last_quota_date"] = today
            save_json(USERS_FILE, users); user["today_used"] = 0; user["last_quota_date"] = today
    quota_limit = 999999 if user["role"] == "admin" else (3000 if user["role"] == "member" else 100)
    secrets = get_secrets(); active_api_id = secrets.get("ACTIVE_API_ID", "")
    api_configs = load_json(API_CONFIGS_FILE, [])
    active_api = next((c for c in api_configs if c["id"] == active_api_id), None)
    safe_active_api = None
    if active_api:
        safe_active_api = {"id": active_api.get("id"), "name": active_api.get("name"), "provider": active_api.get("provider"), "model_name": active_api.get("model_name")}
    return {"api_configured": len(api_configs) > 0, "stats": stats, "providers": PROVIDERS, "active_api_id": active_api_id, "active_api": safe_active_api, "user": {"username": user["username"], "nickname": user.get("nickname", user["username"]), "avatar": user.get("avatar", ""), "role": user["role"], "today_used": user.get("today_used", 0), "quota_limit": quota_limit}}

@app.post("/api/user/update_profile")
def update_profile(req: UpdateProfileRequest, request: Request):
    uid = request.state.user["id"]; users = load_json(USERS_FILE, {})
    if uid not in users: raise HTTPException(status_code=404, detail="用户不存在")
    user = users[uid]
    if req.nickname is not None:
        nickname = req.nickname.strip()
        if not nickname or len(nickname) > 20: raise HTTPException(status_code=400, detail="昵称长度必须在 1-20 位之间")
        user["nickname"] = nickname
    if req.avatar is not None:
        if len(req.avatar) > 150000: raise HTTPException(status_code=400, detail="头像图片太大")
        user["avatar"] = req.avatar
    save_json(USERS_FILE, users); return {"status": "ok", "message": "资料更新成功"}

@app.post("/api/user/change_password")
def change_password(req: ChangePasswordRequest, request: Request):
    users = load_json(USERS_FILE, {}); user = users[request.state.user["id"]]
    if not verify_password(req.old_password, user["password_hash"]): raise HTTPException(status_code=400, detail="原密码错误")
    user["password_hash"] = hash_password(req.new_password); save_json(USERS_FILE, users)
    return {"status": "ok", "message": "密码修改成功"}

# ================= API 配置 =================
@app.get("/api/api_configs")
def list_api_configs(request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="仅管理员可管理 API")
    configs = load_json(API_CONFIGS_FILE, []); safe_configs = []
    for c in configs:
        safe_configs.append({"id": c.get("id"), "name": c.get("name"), "provider": c.get("provider"), "model_name": c.get("model_name"), "custom_base_url": c.get("custom_base_url", ""), "api_key_masked": mask_key(c.get("api_key", ""))})
    return safe_configs

@app.post("/api/api_configs/save")
def save_api_config(config: APIConfigRequest, request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="仅管理员可管理 API")
    configs = load_json(API_CONFIGS_FILE, []); is_new = False
    if config.id:
        for c in configs:
            if c["id"] == config.id:
                if config.api_key and "*" in config.api_key: config.api_key = c.get("api_key", config.api_key)
                c.update(config.dict()); break
    else:
        config.id = str(uuid.uuid4()); configs.append(config.dict()); is_new = True
    save_json(API_CONFIGS_FILE, configs); secrets = get_secrets()
    if is_new and not secrets.get("ACTIVE_API_ID"): secrets["ACTIVE_API_ID"] = config.id; save_secrets(secrets)
    return {"status": "ok", "id": config.id}

@app.delete("/api/api_configs/delete/{config_id}")
def delete_api_config(config_id: str, request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="仅管理员可管理 API")
    configs = load_json(API_CONFIGS_FILE, []); configs = [c for c in configs if c["id"] != config_id]
    save_json(API_CONFIGS_FILE, configs)
    secrets = get_secrets()
    if secrets.get("ACTIVE_API_ID") == config_id: secrets["ACTIVE_API_ID"] = ""; save_secrets(secrets)
    return {"status": "ok"}

@app.post("/api/api_configs/activate/{config_id}")
def activate_api_config(config_id: str, request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="仅管理员可操作")
    configs = load_json(API_CONFIGS_FILE, [])
    if not any(c["id"] == config_id for c in configs): raise HTTPException(status_code=404, detail="配置不存在")
    secrets = get_secrets(); secrets["ACTIVE_API_ID"] = config_id; save_secrets(secrets)
    return {"status": "ok"}

@app.get("/api/api_configs/balance/{config_id}")
def get_api_balance(config_id: str, request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    configs = load_json(API_CONFIGS_FILE, []); config = next((c for c in configs if c["id"] == config_id), None)
    if not config: raise HTTPException(status_code=404, detail="配置不存在")
    provider = config["provider"]; key = config["api_key"]
    try:
        if provider == "deepseek":
            r = requests.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {key}"}, timeout=10)
            data = r.json()
            if "balance_infos" in data and data["balance_infos"]:
                total = sum(float(b.get("total_balance", 0)) for b in data["balance_infos"])
                currency = data["balance_infos"][0].get("currency", "CNY")
                return {"balance": total, "currency": currency, "raw": data}
            return data
        elif provider == "moonshot":
            r = requests.get("https://api.moonshot.cn/v1/users/me/balance", headers={"Authorization": f"Bearer {key}"}, timeout=10)
            data = r.json()
            if "data" in data and isinstance(data["data"], dict):
                return {"balance": data["data"].get("available_balance", 0), "currency": "CNY", "raw": data}
            return data
        elif provider == "zhipu":
            try:
                r = requests.get(
                    "https://www.bigmodel.cn/api/biz/account/query-customer-account-report",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    timeout=10
                )
                data = r.json()
                if data.get("code") == 200 and "data" in data:
                    d = data["data"]
                    available = d.get("availableBalance") or d.get("balance") or 0
                    return {"balance": float(available), "currency": "CNY", "raw": data}
            except Exception:
                pass
            return {"error": "无法获取智谱余额"}
        else:
            return {"error": "该供应商暂不支持余额查询"}
    except Exception as e:
        return {"error": str(e)}

# ================= 管理员 =================
class APITestRequest(BaseModel):
    provider: str
    api_key: str
    model_name: str
    custom_base_url: str = ""


@app.post("/api/api_configs/test")
def test_api_config(req: APITestRequest, request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")

    base_url = PROVIDERS.get(req.provider, {}).get("base_url", "")
    if req.provider == "custom":
        base_url = req.custom_base_url
    if not base_url:
        raise HTTPException(status_code=400, detail="缺少 Base URL")
    if not req.api_key:
        raise HTTPException(status_code=400, detail="缺少 API Key")
    if not req.model_name:
        raise HTTPException(status_code=400, detail="缺少模型名")

    real_key = req.api_key
    if '*' in req.api_key:
        configs = load_json(API_CONFIGS_FILE, [])
        for c in configs:
            if c.get("provider") == req.provider and c.get("model_name") == req.model_name:
                real_key = c.get("api_key", req.api_key)
                break

    payload = {
        "model": req.model_name,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "temperature": 0,
    }
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {real_key}"},
            json=payload,
            timeout=(10, 30),
        )
        if r.status_code == 200:
            data = r.json()
            if "error" in data:
                return {"status": "fail", "message": "API 返回错误：" + str(data["error"].get("message", "未知"))}
            return {"status": "ok", "message": "连接成功，API Key 有效"}
        else:
            try:
                err = r.json()
                msg = err.get("error", {}).get("message") or err.get("message") or r.text[:200]
            except Exception:
                msg = r.text[:200]
            return {"status": "fail", "message": "HTTP " + str(r.status_code) + "：" + str(msg)}
    except Exception as e:
        return {"status": "fail", "message": "请求异常：" + str(e)}


@app.get("/api/admin/users")
def admin_get_users(request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    users = load_json(USERS_FILE, {})
    return [{"id": uid, "username": u["username"], "nickname": u.get("nickname", u["username"]), "avatar": u.get("avatar", ""), "role": u["role"], "status": u["status"], "created_at": u.get("created_at")} for uid, u in users.items()]

@app.post("/api/admin/user_action")
def admin_user_action(req: AdminActionRequest, request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    users = load_json(USERS_FILE, {})
    if req.user_id not in users: raise HTTPException(status_code=404, detail="用户不存在")
    if req.user_id == request.state.user["id"] and req.action in ["delete", "downgrade"]:
        raise HTTPException(status_code=400, detail="不能对自己操作")
    user = users[req.user_id]
    if req.action == "disable": user["status"] = "disabled"
    elif req.action == "enable": user["status"] = "active"
    elif req.action == "upgrade": user["role"] = "member"
    elif req.action == "downgrade": user["role"] = "normal"
    elif req.action == "reset_password": user["password_hash"] = hash_password(req.new_password or "123456")
    elif req.action == "delete":
        del users[req.user_id]
        for file in [WEBDAV_FILE, HISTORY_FILE, STATS_FILE]:
            data = load_json(file, {})
            if req.user_id in data: del data[req.user_id]; save_json(file, data)
    save_json(USERS_FILE, users); return {"status": "ok"}

@app.get("/api/admin/invites")
def admin_get_invites(request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    return load_json(INVITES_FILE, [])

@app.post("/api/admin/invite_create")
def admin_create_invite(req: InviteCreateRequest, request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    invites = load_json(INVITES_FILE, []); code = uuid.uuid4().hex[:8].upper()
    invites.append({"code": code, "role": req.role, "status": "unused", "created_at": time.time(), "expire_at": time.time() + req.expire_hours * 3600 if req.expire_hours > 0 else None, "used_by": None})
    save_json(INVITES_FILE, invites); return {"status": "ok", "code": code}

@app.post("/api/admin/invite_revoke/{code}")
def admin_revoke_invite(code: str, request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    invites = load_json(INVITES_FILE, [])
    for i in invites:
        if i["code"] == code: i["status"] = "revoked"; break
    save_json(INVITES_FILE, invites); return {"status": "ok"}

# ================= 系统维护 =================
@app.post("/api/admin/clear_cache")
def clear_ai_cache(request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    save_json(CACHE_FILE, {}); return {"status": "ok", "message": "AI 缓存已清空"}

@app.post("/api/admin/clear_history")
def clear_history(request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    save_json(HISTORY_FILE, {}); save_json(AUTO_LOG_FILE, []); return {"status": "ok", "message": "操作历史已清空"}

@app.get("/api/admin/system_stats")
def system_stats(request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="无权限")
    cache = load_json(CACHE_FILE, {}); history = load_json(HISTORY_FILE, {}); auto_log = load_json(AUTO_LOG_FILE, [])
    users = load_json(USERS_FILE, {}); index = load_json(SERIES_INDEX_FILE, {})
    return {"ai_cache_count": len(cache), "history_users": len(history), "auto_log_count": len(auto_log), "user_count": len(users), "series_index_count": len(index)}

# ================= 自动任务 API =================
@app.get("/api/auto/configs")
def get_auto_configs(request: Request):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    configs = load_auto_config()
    running_state = {k: True for k, v in AUTO_TASK_RUNNING.items() if v}
    return {"configs": configs, "running": running_state}

@app.post("/api/auto/config")
def save_auto_config_endpoint(req: AutoTaskConfigRequest, request: Request):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    name = (req.name or "").strip()
    if not name or len(name) > 30: raise HTTPException(status_code=400, detail="任务名称长度必须在 1-30 之间")
    if not req.source_id: raise HTTPException(status_code=400, detail="请选择网盘")
    src_path = (req.source_path or "").strip()
    tgt_path = (req.target_path or "").strip()
    if not src_path or not tgt_path: raise HTTPException(status_code=400, detail="待处理文件夹和成品库不能为空")
    if not src_path.startswith('/'): src_path = '/' + src_path
    if not tgt_path.startswith('/'): tgt_path = '/' + tgt_path
    max_files = max(50, min(500, int(req.max_files or 300)))
    interval_hours = max(0.1, min(24.0, float(req.interval_hours or 1)))

    task = req.dict()
    task["name"] = name; task["source_path"] = src_path; task["target_path"] = tgt_path
    task["max_files"] = max_files; task["interval_hours"] = interval_hours
    if not task.get("target_id"): task["target_id"] = task["source_id"]

    configs = load_auto_config()
    if not task.get("task_id"):
        task["task_id"] = str(uuid.uuid4()); configs.append(task)
    else:
        found = False
        for i, t in enumerate(configs):
            if t["task_id"] == task["task_id"]: configs[i] = task; found = True; break
        if not found: configs.append(task)
    save_auto_config(configs); return {"status": "ok", "message": "配置已保存", "task_id": task["task_id"]}

@app.post("/api/auto/delete")
def delete_auto_config(req: TaskActionRequest, request: Request):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    if AUTO_TASK_RUNNING.get(req.task_id, False): raise HTTPException(status_code=400, detail="任务正在运行中，请先中断任务再删除")
    configs = load_auto_config(); configs = [c for c in configs if c["task_id"] != req.task_id]
    save_auto_config(configs); return {"status": "ok", "message": "任务已删除"}

@app.post("/api/auto/cancel")
def cancel_auto_task(req: TaskActionRequest, request: Request):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    if not AUTO_TASK_RUNNING.get(req.task_id, False): return {"status": "ok", "message": "当前没有运行中的任务"}
    if req.task_id in AUTO_TASK_CANCEL: AUTO_TASK_CANCEL[req.task_id].set()
    if req.task_id in AUTO_TASK_STATE: AUTO_TASK_STATE[req.task_id]["cancel_requested"] = True
    return {"status": "ok", "message": "中断信号已发送，任务将在当前文件处理完毕后停止"}

@app.post("/api/auto/force_reset")
def force_reset_auto_task(req: TaskActionRequest, request: Request):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    if req.task_id in AUTO_TASK_CANCEL: AUTO_TASK_CANCEL[req.task_id].set()
    AUTO_TASK_RUNNING[req.task_id] = False
    if req.task_id in AUTO_TASK_STATE:
        AUTO_TASK_STATE[req.task_id]["running"] = False; AUTO_TASK_STATE[req.task_id]["cancel_requested"] = True
    configs = load_auto_config(); configs = [c for c in configs if c["task_id"] != req.task_id]
    save_auto_config(configs); return {"status": "ok", "message": "已强制删除任务"}

@app.post("/api/auto/run_now")
def run_auto_now(req: TaskActionRequest, request: Request):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    if AUTO_TASK_RUNNING.get(req.task_id, False): raise HTTPException(status_code=400, detail="任务正在运行中，请稍后再试")
    configs = load_auto_config()
    if not any(c["task_id"] == req.task_id for c in configs): raise HTTPException(status_code=404, detail="任务不存在")
    def bg_task(tid):
        try: run_auto_task(tid, force=True)
        except Exception as e: print(f"【手动任务异常】{tid}: {e}")
    threading.Thread(target=bg_task, args=(req.task_id,), daemon=True).start()
    return {"status": "ok", "message": "任务已在后台启动"}

@app.get("/api/auto/status")
def get_auto_status(request: Request):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    return {"running": {k: True for k, v in AUTO_TASK_RUNNING.items() if v}, "state": AUTO_TASK_STATE}

@app.get("/api/auto/logs")
def get_auto_logs(request: Request, task_id: str = None):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    logs = load_auto_log()
    if task_id: logs = [l for l in logs if l.get("task_id") == task_id]
    return {"logs": logs}

@app.get("/api/auto/index")
def get_auto_index(request: Request):
    if request.state.user["role"] not in ["admin", "member"]: raise HTTPException(status_code=403, detail="自动定时任务为会员专属功能，请开通会员后使用。")
    return {"index": load_series_index()}

# ================= WebDAV =================
@app.get("/api/webdav/list")
def list_webdav(request: Request):
    sources = load_json(WEBDAV_FILE, {}).get(request.state.user["id"], []); safe = []
    for s in sources:
        safe.append({"id": s.get("id"), "name": s.get("name"), "url": s.get("url"), "username": s.get("username"), "password_masked": mask_key(s.get("password", ""))})
    return safe

@app.post("/api/webdav/test")
def test_webdav(req: WebDAVTestRequest, request: Request):
    if request.state.user["role"] != "admin": raise HTTPException(status_code=403, detail="仅管理员可操作")
    try:
        items, err = webdav_propfind(req.url, req.username, req.password, "/", depth=0)
        if err: return {"status": "fail", "message": f"连接失败: {err}"}
        return {"status": "ok", "message": "✅ 连接成功，WebDAV 可正常访问"}
    except Exception as e:
        return {"status": "fail", "message": f"连接失败: {str(e)}"}

@app.post("/api/webdav/save")
def save_webdav(source: WebDAVSource, request: Request):
    uid = request.state.user["id"]; all_sources = load_json(WEBDAV_FILE, {}); sources = all_sources.get(uid, [])
    role = request.state.user["role"]; limit = 999 if role == "admin" else (5 if role == "member" else 2)
    if not source.id and len(sources) >= limit: raise HTTPException(status_code=400, detail=f"当前等级最多只能添加 {limit} 个网盘")
    if source.id:
        for s in sources:
            if s['id'] == source.id:
                s["name"] = source.name; s["url"] = source.url; s["username"] = source.username
                if not source.password or "*" not in source.password: s["password"] = source.password
                break
    else:
        source.id = str(uuid.uuid4()); sources.append(source.dict())
    all_sources[uid] = sources; save_json(WEBDAV_FILE, all_sources)
    return {"status": "ok", "id": source.id}

@app.delete("/api/webdav/delete/{source_id}")
def delete_webdav(source_id: str, request: Request):
    uid = request.state.user["id"]; all_sources = load_json(WEBDAV_FILE, {})
    all_sources[uid] = [s for s in all_sources.get(uid, []) if s['id'] != source_id]
    save_json(WEBDAV_FILE, all_sources); return {"status": "ok"}

def get_webdav(source_id, uid):
    sources = load_json(WEBDAV_FILE, {}).get(uid, [])
    for s in sources:
        if s['id'] == source_id: return s
    raise HTTPException(status_code=404, detail="WebDAV 源不存在")

@app.post("/api/browse")
def browse_dir(req: BrowseRequest, request: Request):
    """统一分派：列目录（支持 WebDAV + 123 等）"""
    uid = request.state.user["id"]
    from app.unified_source import resolve_source
    from app.source_dispatch import dispatch_propfind

    source = resolve_source(uid, req.source_id, WEBDAV_FILE)
    if not source:
        raise HTTPException(status_code=404, detail="源不存在或已失效")

    path = req.path.strip('/')
    req_name = urllib.parse.unquote(req.path).strip('/').split('/')[-1]

    items, err = dispatch_propfind(source, path, depth=1)
    if err:
        raise HTTPException(status_code=400, detail=f"读取失败: {err}")

    dirs, files = [], []
    for item in items:
        if req_name and item['name'] == req_name:
            continue
        full_path = (path + "/" + item['name']).lstrip("/")
        if item['is_dir']:
            dirs.append({"name": item['name'], "path": full_path})
        else:
            if item['name'].lower().endswith(VIDEO_EXTS):
                files.append({"name": item['name'], "path": full_path})
    return {"dirs": dirs, "files": files}


@app.post("/api/scan_stream")
def scan_stream(req: ScanRequest, request: Request):
    user = request.state.user
    from app.unified_source import resolve_source
    source = resolve_source(user["id"], req.source_id, WEBDAV_FILE)
    if not source:
        def err_gen(): yield f"data: {json.dumps({'error': '源不存在或已失效'})}\n\n"
        return StreamingResponse(err_gen(), media_type="text/event-stream")
    path = req.path.strip('/'); req_name = urllib.parse.unquote(req.path).strip('/').split('/')[-1]
    quota_limit = 999999 if user["role"] == "admin" else (3000 if user["role"] == "member" else 100)
    remain_quota = quota_limit - user.get("today_used", 0)
    if remain_quota <= 0:
        def err_gen(): yield f"data: {json.dumps({'error': '您的今日操作额度已用尽'})}\n\n"
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    def event_generator():
        count = 0; truncated = False
        try:
            for file_data in recursive_scan(source, path, 0, 5, req_name):
                if count >= remain_quota: truncated = True; break
                count += 1; yield f"data: {json.dumps(file_data, ensure_ascii=False)}\n\n"; time.sleep(0.1)
            if truncated: yield f"data: {json.dumps({'error': f'已扫描到 {count} 个文件，剩余已被截断'})}\n\n"
            else: yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e: yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")

# ================= AI 解析 & 重命名 =================
@app.post("/api/parse")
def ai_parse(req: AIRequest, request: Request):
    result = ai_parse_internal(req.filename, req.folder_context, req.source_id)
    if result["success"]:
        r = result["result"]
        ext = os.path.splitext(req.filename)[1]
        title = r.get("title", "未知影视")
        year = r.get("year", "未知年份")
        media_type = r.get("media_type", "movie")
        if media_type == "movie":
            standard_name = f"{title}{_year_suffix(year)}{ext}"
        else:
            season = str(r.get("season") or 1).zfill(2)
            episode = str(r.get("episode") or 1).zfill(2)
            standard_name = f"{title}{_year_suffix(year)} - S{season}E{episode}{ext}"
        standard_name = (standard_name.replace("/", "／").replace("\\", "＼").replace(":", "：")
                         .replace("*", "＊").replace("?", "？").replace('"', "＂")
                         .replace("<", "＜").replace(">", "＞").replace("|", "｜"))
        r["standard_name"] = standard_name
        return {"result": r, "cached": result.get("cached", False)}
    raise HTTPException(status_code=500, detail=result.get("reason", "AI 解析失败"))

@app.post("/api/rename_stream")
def rename_files_stream(req: RenameRequest, request: Request):
    CHR = chr(10)
    SSE_END = CHR + CHR

    user = request.state.user; uid = user["id"]
    from app.unified_source import resolve_source
    from app.source_dispatch import dispatch_move, dispatch_propfind
    source = resolve_source(uid, req.source_id, WEBDAV_FILE)
    if not source:
        def err_gen():
            yield "data: " + json.dumps({"error": "源不存在或已失效"}, ensure_ascii=False) + SSE_END
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    users = load_json(USERS_FILE, {}); live_user = users.get(uid, user); today = get_beijing_date()
    if live_user.get("last_quota_date") != today:
        live_user["today_used"] = 0
        live_user["last_quota_date"] = today
    quota_limit = 999999 if live_user["role"] == "admin" else (3000 if live_user["role"] == "member" else 100)
    remain_quota = quota_limit - live_user.get("today_used", 0)
    if remain_quota <= 0:
        def err_gen():
            yield "data: " + json.dumps({"error": "您的今日操作额度已用尽"}, ensure_ascii=False) + SSE_END
        return StreamingResponse(err_gen(), media_type="text/event-stream")

    files_to_process = req.files[:remain_quota]
    is_pan123 = (source.get("type") == "pan123")

    def sse(payload):
        return "data: " + json.dumps(payload, ensure_ascii=False) + SSE_END

    def event_generator():
        history = load_json(HISTORY_FILE, {}); user_history = history.get(uid, [])
        batch = {"time": time.time(), "operations": []}
        success_count = 0; fail_count = 0; skip_count = 0; error_msgs = []

        try:
            if is_pan123:
                from app.pan123 import pan123_batch_resolve_files, pan123_batch_rename, pan123_list_dir
                aid = source["id"]
                to_do = []
                for idx, f in enumerate(files_to_process):
                    old_name = os.path.basename(f.old_path)
                    if old_name == f.new_name:
                        skip_count += 1
                        yield sse({"index": idx, "total": len(files_to_process), "old": old_name, "status": "skip"})
                    else:
                        to_do.append({
                            "idx": idx,
                            "old_path": f.old_path,
                            "old_name": old_name,
                            "new_name": f.new_name,
                            "dir_path": os.path.dirname(f.old_path).strip("/") or "/",
                            "base_old": os.path.splitext(old_name)[0],
                        })

                paths = [it["old_path"] for it in to_do]
                resolved = pan123_batch_resolve_files(uid, aid, paths)

                main_items = []
                for it in to_do:
                    info = resolved.get(it["old_path"])
                    if not info:
                        fail_count += 1
                        error_msgs.append(it["old_name"] + ": 文件不存在")
                        yield sse({"index": it["idx"], "total": len(files_to_process), "old": it["old_name"], "status": "fail", "message": "文件不存在"})
                        continue
                    it["file_id"] = info["file_id"]
                    main_items.append(it)

                if main_items:
                    pairs = [(int(it["file_id"]), it["new_name"]) for it in main_items]
                    r = pan123_batch_rename(uid, aid, pairs)
                    success_count = r["success"]
                    fail_count += r["failed"]
                    for e in r["errors"]:
                        error_msgs.append(str(e.get("new_name", "?")) + ": " + str(e.get("reason", "?")))

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

                if attach_pairs:
                    ra = pan123_batch_rename(uid, aid, attach_pairs)
                    if ra["failed"]:
                        error_msgs.append("附件改名失败 " + str(ra["failed"]) + " 个")

                for it in main_items:
                    batch["operations"].append({
                        "old": it["old_path"],
                        "new": (it["dir_path"] + "/" + it["new_name"]).lstrip("/"),
                    })

                for it in main_items:
                    yield sse({"index": it["idx"], "total": len(files_to_process), "old": it["old_name"], "new": it["new_name"], "status": "success"})

            else:
                for idx, f in enumerate(files_to_process):
                    old_path = f.old_path; new_name = f.new_name
                    dir_path = os.path.dirname(old_path); old_name = os.path.basename(old_path)
                    base_old, _ = os.path.splitext(old_name)
                    if old_name == new_name:
                        skip_count += 1
                        yield sse({"index": idx, "total": len(files_to_process), "old": old_name, "status": "skip"})
                        continue
                    target_path = dir_path + "/" + new_name
                    ok, status_code, err_text = dispatch_move(source, old_path, target_path)
                    time.sleep(0.3)
                    if ok:
                        success_count += 1
                        batch["operations"].append({"old": old_path, "new": target_path})
                        try:
                            items, _ = dispatch_propfind(source, dir_path, depth=1)
                            for item in items:
                                if item['is_dir']:
                                    continue
                                fname = item['name']; fpath = dir_path + "/" + fname
                                if fpath == old_path:
                                    continue
                                if fname == old_name or (fname.startswith(base_old) and fname[len(base_old):len(base_old)+1] in ('.', '_', '-', ' ')):
                                    suffix = fname[len(base_old):]
                                    new_attach_name = os.path.splitext(new_name)[0] + suffix
                                    dispatch_move(source, fpath, dir_path + "/" + new_attach_name)
                                    time.sleep(0.3)
                        except Exception as e:
                            print("附件重命名失败: " + str(e))
                        yield sse({"index": idx, "total": len(files_to_process), "old": old_name, "new": new_name, "status": "success"})
                    else:
                        fail_count += 1
                        error_msgs.append(old_name + ": HTTP " + str(status_code))
                        yield sse({"index": idx, "total": len(files_to_process), "old": old_name, "status": "fail", "message": "HTTP " + str(status_code)})

        except Exception as e:
            yield sse({"error": str(e)})
            return

        user_history.append(batch); user_history = trim_history(user_history)
        history[uid] = user_history; save_json(HISTORY_FILE, history)
        live_user["today_used"] = live_user.get("today_used", 0) + success_count
        users[uid] = live_user; save_json(USERS_FILE, users)
        stats = load_json(STATS_FILE, {}); user_stats = stats.get(uid, {"total_renamed": 0})
        user_stats["total_renamed"] += success_count; stats[uid] = user_stats; save_json(STATS_FILE, stats)

        msg = "重命名完成！成功 " + str(success_count) + " 个，跳过 " + str(skip_count) + " 个，失败 " + str(fail_count) + " 个"
        if len(req.files) > remain_quota:
            msg += "。注意：因额度限制，仅处理了前 " + str(remain_quota) + " 个文件。"
        if error_msgs:
            msg += "。错误详情: " + '; '.join(error_msgs[:5])
        yield sse({"done": True, "message": msg, "success": success_count, "fail": fail_count, "skip": skip_count})

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/api/history")
def get_history(request: Request):
    return load_json(HISTORY_FILE, {}).get(request.state.user["id"], [])

# ================= 调试接口（临时） =================
@app.get("/api/debug_parse")
def debug_parse(filename: str):
    save_json(CACHE_FILE, {})
    result = ai_parse_internal(filename)
    if result.get("success"):
        r = result["result"]
        ext = os.path.splitext(filename)[1]
        title = r.get("title", "未知影视"); year = r.get("year", "未知年份")
        media_type = r.get("media_type", "movie")
        if media_type == "movie":
            standard_name = f"{title}{_year_suffix(year)}{ext}"
        else:
            season = str(r.get("season") or 1).zfill(2)
            episode = str(r.get("episode") or 1).zfill(2)
            standard_name = f"{title}{_year_suffix(year)} - S{season}E{episode}{ext}"
        r["standard_name"] = standard_name
        return {"result": r, "cached": False}
    return {"error": result.get("reason", "AI 解析失败")}

app.mount("/static", StaticFiles(directory="app/static"), name="static")

@app.get("/")
def read_root():
    return FileResponse("app/static/index.html")





# ===== 批量解析路由注册（追加） =====

from app.batch_parse import register_batch_routes

register_batch_routes(app)






# ===== 重命名任务路由注册（追加） =====

from app.rename_tasks import register_rename_routes

register_rename_routes(app)




# 补充：批量解析模块里的标准名生成函数

from app.batch_parse import _make_std_name

from app.batch_parse import _make_std_name


# ================= 123 云盘 OpenAPI（多账号） =================

from app.pan123 import (
    pan123_get_auth_url,
    pan123_list_accounts, pan123_add_account, pan123_rename_account,
    pan123_delete_account, pan123_account_status,
    Pan123Error,
)


class Pan123AddRequest(BaseModel):
    name: str = "123 云盘"
    access_token: str
    refresh_token: str


class Pan123RenameRequest(BaseModel):
    name: str


@app.get("/api/pan123/auth-url")
def pan123_auth_url_endpoint(request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    try:
        return pan123_get_auth_url()
    except Pan123Error as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/pan123/accounts")
def pan123_accounts_list(request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    uid = request.state.user["id"]
    return {"accounts": pan123_list_accounts(uid)}


@app.post("/api/pan123/accounts")
def pan123_accounts_add(req: Pan123AddRequest, request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    uid = request.state.user["id"]
    try:
        result = pan123_add_account(uid, req.name, req.access_token, req.refresh_token)
        return {"status": "ok", "id": result["id"], "user": result["info"]}
    except Pan123Error as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/pan123/accounts/{account_id}/rename")
def pan123_accounts_rename(account_id: str, req: Pan123RenameRequest, request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    uid = request.state.user["id"]
    try:
        pan123_rename_account(uid, account_id, req.name)
        return {"status": "ok"}
    except Pan123Error as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/pan123/accounts/{account_id}")
def pan123_accounts_delete(account_id: str, request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    uid = request.state.user["id"]
    pan123_delete_account(uid, account_id)
    return {"status": "ok"}


@app.get("/api/pan123/accounts/{account_id}/status")
def pan123_accounts_status(account_id: str, request: Request):
    if request.state.user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可操作")
    uid = request.state.user["id"]
    return pan123_account_status(uid, account_id)


# ================= 统一数据源接口 =================

@app.get("/api/sources/list")
def list_all_sources_api(request: Request):
    """返回所有可用数据源（WebDAV + 123 直连）"""
    uid = request.state.user["id"]
    from app.unified_source import list_all_sources
    sources = list_all_sources(uid, WEBDAV_FILE)
    return {"sources": sources}
