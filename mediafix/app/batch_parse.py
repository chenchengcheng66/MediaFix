# /opt/mediafix/app/batch_parse.py
# 批量 AI 解析模块 —— 支持目录上下文，适配 GLM-5.3-Flash 强制思考模式

import os, json, time, requests
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

_CACHE_FILE = "/data/ai_cache.json"
_API_CONFIGS_FILE = "/data/api_configs.json"
_SECRETS_FILE = "/data/secrets.env"

PROVIDERS = {
    "deepseek": {"base_url": "https://api.deepseek.com"},
    "aliyun": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
    "moonshot": {"base_url": "https://api.moonshot.cn/v1"},
    "zhipu": {"base_url": "https://open.bigmodel.cn/api/paas/v4"},
    "volcengine": {"base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    "custom": {"base_url": ""},
}


def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_secrets():
    if not os.path.exists(_SECRETS_FILE):
        return {}
    d = {}
    with open(_SECRETS_FILE) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                d[k] = v
    return d


def _sanitize_title(t):
    return (t.replace("/", "／").replace("\\", "＼").replace(":", "：")
            .replace("*", "＊").replace("?", "？").replace('"', "＂")
            .replace("<", "＜").replace(">", "＞").replace("|", "｜"))


def _year_suffix(year):
    """年份后缀：有年份返回 ' (YYYY)'，否则空字符串"""
    if not year or year == "未知年份":
        return ""
    return f" ({year})"


def _make_std_name(original, r):
    ext = os.path.splitext(original)[1]
    title = r.get("title", "未知影视")
    year = r.get("year", "未知年份")
    mt = r.get("media_type", "movie")
    if mt == "movie":
        return title + _year_suffix(year) + ext
    s = str(r.get("season") or 1).zfill(2)
    e = str(r.get("episode") or 1).zfill(2)
    return title + _year_suffix(year) + " - S" + s + "E" + e + ext


class BatchParseItem(BaseModel):
    name: str
    path: str = ""


class BatchParseRequest(BaseModel):
    source_id: str = ""
    files: list[BatchParseItem]
    batch_size: int = 15


PROMPT = """解析影视文件名，输出 JSON 数组。
输入格式：序号. 文件名  【所在目录：xxx】
输出格式：{"name":"原文件名","is_media":true|false,"non_media_reason":"","media_type":"movie|tv|anime","title":"中文名","year":"年份","season":数字|null,"episode":数字|null}
规则：
1. is_media 判断是否真正的影视资源：
   - true 的情况：含影视信息(剧名/年份/季集)、纯 S01E01 格式
   - false 的情况：
     * 文件名含网址(www./http/.com)
     * 广告词(广告/加群/公众号/关注/推广)
     * 资源站水印、预告片、试看片段
     * 非影视内容(软件/游戏/文档)
     * 动漫的 OP(片头)/ED(片尾)/PV(宣传片)/SP(特典)/NC(无字幕版)/CM(广告)/Menu(菜单) 作为独立词出现时
2. non_media_reason：当 is_media 为 false 时填写简短原因(如"片头OP""广告宣传")，否则为空字符串
3. title：无论 is_media 是什么 title 都必须有值；若非影视资源，title 可填原文件名主体
4. media_type：电影=movie，剧集=tv，动漫=anime
5. title 优先从文件名提取中文名；纯 S01E01 格式从"所在目录"推断剧名
6. year 提取不到填"未知年份"
7. S01E01 拆为 season/episode，电影填 null
8. 数组长度必须等于输入行数，name 字段必须与输入完全一致
9. 只输出 JSON"""


def _ai_call(names, provider, api_key, model, base_url, contexts=None):
    lines = []
    for i, n in enumerate(names):
        ctx = contexts[i] if (contexts and i < len(contexts)) else ""
        if ctx:
            lines.append(f"{i+1}. {n}  【所在目录：{ctx}】")
        else:
            lines.append(f"{i+1}. {n}")
    user_content = "文件名列表：\n" + "\n".join(lines)
    
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
    }
    
    # 适配 GLM-5.3-Flash 强制思考模式
    if provider == "zhipu":
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "low"
    
    try:
        r = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + api_key},
            json=payload,
            timeout=(10, 180),
        )
        if r.status_code != 200:
            return {}, "HTTP " + str(r.status_code) + ": " + r.text[:200]
        resp = r.json()
        if "error" in resp:
            return {}, "API错误: " + str(resp["error"].get("message", ""))
        content = resp["choices"][0]["message"]["content"]
        content = content.replace("```json", "").replace("```", "").strip()
        i1 = content.find("[")
        i2 = content.rfind("]")
        if i1 < 0 or i2 <= i1:
            return {}, "AI 未返回数组: " + content[:200]
        arr = json.loads(content[i1:i2 + 1])
        out = {}
        for it in arr:
            if not isinstance(it, dict):
                continue
            name = it.get("name", "")
            if not name:
                continue

            _raw = it.get("is_media", True)
            if _raw is False:
                is_media = False
            elif _raw is True or _raw is None:
                is_media = True
            else:
                is_media = str(_raw).strip().lower() not in ("false", "0", "no", "none", "null", "")
            it["is_media"] = is_media

            t = str(it.get("title", "")).strip()
            if not t:
                if is_media:
                    continue
                t = name.rsplit(".", 1)[0] if "." in name else name
                it["title"] = t

            it["title"] = _sanitize_title(t)
            if not it.get("year"):
                it["year"] = "未知年份"
            if not it.get("media_type"):
                it["media_type"] = "movie"
            out[name] = it
        return out, None
    except Exception as e:
        return {}, "异常: " + str(e)


def register_batch_routes(app):

    @app.post("/api/parse_batch_stream")
    def parse_batch_stream(req: BatchParseRequest, request: Request):
        source_id = req.source_id or ""
        files = req.files
        bs = max(5, min(30, req.batch_size or 15))

        def gen():
            if not files:
                yield "data: " + json.dumps({"done": True}) + "\n\n"
                return

            cache = _load_json(_CACHE_FILE, {})
            secrets = _get_secrets()
            active = secrets.get("ACTIVE_API_ID", "")
            configs = _load_json(_API_CONFIGS_FILE, [])
            cfg = next((c for c in configs if c["id"] == active), None)
            if not cfg:
                yield "data: " + json.dumps({"done": True, "error": "未配置 API"}, ensure_ascii=False) + "\n\n"
                return

            provider = cfg["provider"]
            api_key = cfg["api_key"]
            model = cfg.get("model_name") or ""
            base_url = PROVIDERS.get(provider, {}).get("base_url", "")
            if provider == "custom":
                base_url = cfg.get("custom_base_url", "")
            if provider == "zhipu" and (not model or model == "default"):
                model = "glm-5.3-flash"

            pending = []
            total = len(files)
            for i, f in enumerate(files):
                key = source_id + "::::" + f.name if source_id else f.name
                c = cache.get(key)
                if isinstance(c, dict) and c.get("title"):
                    std = _make_std_name(f.name, c)
                    msg = {"index": i, "name": f.name, "success": True,
                           "result": c, "standard_name": std, "cached": True}
                    yield "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n"
                else:
                    pending.append((i, f, key))

            batches = [pending[k:k + bs] for k in range(0, len(pending), bs)]
            for bi, batch in enumerate(batches):
                names = [x[1].name for x in batch]
                contexts = []
                for x in batch:
                    p = x[1].path or ""
                    d = os.path.dirname(p) if p else ""
                    parts = [seg for seg in d.strip('/').split('/') if seg]
                    contexts.append("/".join(parts[-2:]) if parts else "")

                rmap, err = _ai_call(names, provider, api_key, model, base_url, contexts)
                if err:
                    for i, f, key in batch:
                        msg = {"index": i, "name": f.name, "success": False, "reason": err}
                        yield "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n"
                    continue
                for i, f, key in batch:
                    r = rmap.get(f.name)
                    if not r:
                        stem = os.path.splitext(f.name)[0]
                        for k, v in rmap.items():
                            if os.path.splitext(k)[0] == stem:
                                r = v
                                break
                    if r:
                        cache[key] = r
                        std = _make_std_name(f.name, r)
                        msg = {"index": i, "name": f.name, "success": True,
                               "result": r, "standard_name": std, "cached": False}
                        yield "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n"
                    else:
                        msg = {"index": i, "name": f.name, "success": False, "reason": "AI 漏项"}
                        yield "data: " + json.dumps(msg, ensure_ascii=False) + "\n\n"

                if len(cache) > 10000:
                    for k in list(cache.keys())[:2000]:
                        cache.pop(k, None)
                _save_json(_CACHE_FILE, cache)
                if bi < len(batches) - 1:
                    time.sleep(1.0)

            yield "data: " + json.dumps({"done": True, "total": total}) + "\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")