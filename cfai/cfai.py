#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cfai — 多 Cloudflare Workers AI 账号统一调度 CLI + OpenAI 兼容网关。

两种账号类型可以混在同一个池子里：
  cloudflare : account_id + api_token，直连 CF 官方 OpenAI 兼容端点
  openai     : 任意 OpenAI 兼容 base_url + key（比如你自己部署的 Worker / 反代）

所有账号共享一个模型名空间，路由器负责轮询、冷却、故障转移和每日配额。
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import random
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.2.0"

CF_ACCOUNT_CHAT = "https://api.cloudflare.com/client/v4/accounts/{}/ai/v1/chat/completions"
CF_ACCOUNT_MODELS = "https://api.cloudflare.com/client/v4/accounts/{}/ai/v1/models"

DEFAULT_CONFIG = {
    "accounts": [],
    "aliases": {},
    "allowed_models": [],               # 对外暴露/允许调用的模型；空 = 全部放行
    "admin_password": "",               # sha256 哈希；空 = 用 api_key 登录控制台
    "keys": [],                         # 客户端 API Key：[{name,key,enabled,daily_requests,daily_neurons,note}]
    "router": {
        "strategy": "round_robin",      # round_robin | least_used | random
        "max_retries": 3,               # 单次请求最多换几个账号
        "cooldown_seconds": 60,         # 429/5xx 后的初始冷却
        "max_cooldown_seconds": 900,    # 指数退避上限
        "timeout": 120,
        "rpm": 0,                       # 每账号每分钟请求上限，0=不限（保护账号，避免触发限流）
        "default_daily_neurons": 10000, # 账号没单独设额度时按这个算（CF 免费额度 10000/天），0=不限
        "alias_only": True,             # 有别名时只对外暴露别名，原始模型 id 不列出也不接受调用
        "health_check_minutes": 60,     # 后台自动体检间隔（分钟），0=关闭
        "user_agent": "cfai/1.0",       # 明确标识自己，不要伪装浏览器
        "default_model": "",
        "verbose": False,
    },
}

ALLOWED_KEYS = {
    "model", "messages", "max_tokens", "temperature", "top_p", "top_k", "stream",
    "stop", "seed", "frequency_penalty", "presence_penalty", "repetition_penalty",
    "tools", "tool_choice", "response_format",
}

STATE_LOCK = threading.Lock()

# ---- 控制台会话：登录后发一次性 token，12 小时过期（进程内保存，重启即全部失效） ----
SESSION_TTL = float(os.environ.get("CFAI_SESSION_TTL") or 12 * 3600)
SESSIONS = {}                      # token -> 过期时间戳
SESSION_LOCK = threading.Lock()


def new_session() -> str:
    tok = secrets.token_urlsafe(32)
    now = time.time()
    with SESSION_LOCK:
        for t, exp in list(SESSIONS.items()):   # 顺手清掉过期的
            if exp <= now:
                SESSIONS.pop(t, None)
        SESSIONS[tok] = now + SESSION_TTL
    return tok


def session_left(tok: str) -> float:
    if not tok:
        return 0.0
    with SESSION_LOCK:
        exp = SESSIONS.get(tok)
        if exp is None:
            return 0.0
        left = exp - time.time()
        if left <= 0:
            SESSIONS.pop(tok, None)
            return 0.0
        return left


def session_ok(tok: str) -> bool:
    return session_left(tok) > 0


def drop_session(tok: str) -> None:
    with SESSION_LOCK:
        SESSIONS.pop(tok, None)


def drop_all_sessions() -> None:
    with SESSION_LOCK:
        SESSIONS.clear()


# --------------------------------------------------------------------------- 路径

def home_dir() -> str:
    return os.environ.get("CFAI_HOME") or os.path.join(os.path.expanduser("~"), ".config", "cfai")


def config_path() -> str:
    return os.path.join(home_dir(), "config.json")


def state_path() -> str:
    return os.path.join(home_dir(), "state.json")


def ensure_home() -> None:
    d = home_dir()
    os.makedirs(d, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass


def today() -> str:
    """CF 免费额度按 UTC 零点重置，所以计数日也必须用 UTC，否则会和实际额度错 8 小时。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- 配置

def load_config() -> dict:
    ensure_home()
    p = config_path()
    if not os.path.exists(p):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config(cfg)
        return cfg
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = json.loads(json.dumps(v))
    for k, v in DEFAULT_CONFIG["router"].items():
        cfg["router"].setdefault(k, v)
    cfg.setdefault("accounts", [])
    cfg.setdefault("aliases", {})
    return cfg


def save_config(cfg: dict) -> None:
    ensure_home()
    p = config_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    # 同一进程内（网关控制台改配置）立刻生效，避免等 mtime 轮询出现竞态
    H = globals().get("Handler")
    if H is not None:
        try:
            H.cfg = cfg
            H.cfg_mtime = os.path.getmtime(p)
        except OSError:
            pass


def get_account(cfg: dict, name: str):
    for a in cfg["accounts"]:
        if a["name"] == name:
            return a
    return None


def find_account(cfg: dict, name: str):
    """支持按名字或序号（1 起）查找。"""
    a = get_account(cfg, name)
    if a:
        return a
    if name.isdigit():
        i = int(name) - 1
        if 0 <= i < len(cfg["accounts"]):
            return cfg["accounts"][i]
    return None


# --------------------------------------------------------------------------- 状态

def new_account_state() -> dict:
    return {
        "requests": 0, "ok": 0, "fail": 0,
        "tokens_in": 0, "tokens_out": 0, "neurons": 0.0,
        "last_used": 0, "last_error": "",
        "cooldown_until": 0, "fail_streak": 0,
        "auth_error": False,
        "checked_at": 0, "check_ok": None,
    }


def load_state() -> dict:
    ensure_home()
    p = state_path()
    st = {"date": today(), "rr_cursor": 0, "accounts": {}}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                st = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    if st.get("date") != today():
        st["date"] = today()
        for v in st.get("accounts", {}).values():
            for k in ("requests", "ok", "fail", "tokens_in", "tokens_out", "neurons"):
                v[k] = 0
        for v in st.get("keys", {}).values():
            for k in ("requests", "ok", "fail", "tin", "tout"):
                v[k] = 0
            v["neurons"] = 0.0
    st.setdefault("accounts", {})
    st.setdefault("rr_cursor", 0)
    return st


def save_state(st: dict) -> None:
    ensure_home()
    p = state_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def acc_state(st: dict, name: str) -> dict:
    if name not in st["accounts"]:
        st["accounts"][name] = new_account_state()
    else:
        base = new_account_state()
        base.update(st["accounts"][name])
        st["accounts"][name] = base
    return st["accounts"][name]


# --------------------------------------------------------------------------- 路由

def resolve_model(cfg: dict, acct: dict, model: str) -> str:
    m = cfg.get("aliases", {}).get(model, model)
    m = acct.get("model_map", {}).get(m, m)
    return m


def allowed_models(cfg: dict) -> list:
    return [m for m in (cfg.get("allowed_models") or []) if m]


def hash_pw(pw: str) -> str:
    return hashlib.sha256(("cfai:v1:" + pw).encode("utf-8")).hexdigest()


def new_key_string() -> str:
    return "sk-" + secrets.token_hex(20)


def find_key(cfg: dict, token: str):
    for k in (cfg.get("keys") or []):
        if k.get("enabled", True) and k.get("key") == token:
            return k
    return None


def get_key(cfg: dict, name: str):
    for k in (cfg.get("keys") or []):
        if k.get("name") == name:
            return k
    return None


def key_state(st: dict, name: str) -> dict:
    ks = st.setdefault("keys", {})
    if name not in ks:
        ks[name] = {"requests": 0, "ok": 0, "fail": 0, "neurons": 0.0, "tin": 0, "tout": 0}
    return ks[name]


def record_key_usage(st: dict, name: str, ok: bool, neurons: float = 0.0,
                     tin: int = 0, tout: int = 0) -> None:
    if not name:
        return
    with STATE_LOCK:
        s = key_state(st, name)
        s["requests"] += 1
        s["ok" if ok else "fail"] += 1
        s["neurons"] = round(float(s.get("neurons", 0)) + float(neurons or 0), 4)
        s["tin"] = int(s.get("tin", 0)) + int(tin or 0)
        s["tout"] = int(s.get("tout", 0)) + int(tout or 0)


def key_limit_hit(cfg: dict, st: dict, name: str):
    """返回 (是否超限, 原因)。"""
    if not name:
        return False, ""
    k = get_key(cfg, name)
    if not k:
        return False, ""
    s = key_state(st, name)
    cap = k.get("daily_requests")
    if cap and s.get("requests", 0) >= int(cap):
        return True, f"该 Key 今日请求已达上限（{int(cap)}）"
    ncap = k.get("daily_neurons")
    if ncap and float(s.get("neurons", 0)) >= float(ncap):
        return True, f"该 Key 今日神经元已达上限（{float(ncap):g}）"
    return False, ""


def alias_for(cfg: dict, model_id: str):
    """找出指向这个模型 id 的别名（没有就返回 None）。"""
    for name, target in (cfg.get("aliases") or {}).items():
        if target == model_id:
            return name
    return None


def model_allowed(cfg: dict, model: str) -> bool:
    """空列表 = 不限制；否则只放行列表里的模型（别名也算）。"""
    allow = allowed_models(cfg)
    if not allow:
        return True
    aliases = cfg.get("aliases") or {}
    resolved = aliases.get(model, model)
    for a in allow:
        if model == a or resolved == a or resolved == aliases.get(a, a):
            return True
    return False


def supports(acct: dict, cfg: dict, model: str) -> bool:
    pats = acct.get("models") or []
    if not pats:
        return True
    names = {model, cfg.get("aliases", {}).get(model, model), resolve_model(cfg, acct, model)}
    for p in pats:
        for n in names:
            if fnmatch.fnmatch(n, p) or n == p:
                return True
    return False


def _recent_count(s: dict, window: int = 60) -> int:
    now = time.time()
    rec = [t for t in (s.get("recent") or []) if now - t < window]
    s["recent"] = rec
    return len(rec)


def mark_dispatch(st: dict, name: str) -> None:
    s = acc_state(st, name)
    now = time.time()
    s["recent"] = [t for t in (s.get("recent") or []) if now - t < 120] + [now]


def daily_budget(cfg: dict, acct: dict) -> float:
    """账号的每日神经元额度：优先账号自定义，否则用全局默认（CF 免费 10000/天）。0=不限。"""
    v = acct.get("daily_neurons")
    if v in (None, "", 0, "0"):
        v = cfg["router"].get("default_daily_neurons") or 0
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def order_candidates(cfg: dict, st: dict, model: str) -> list:
    """加锁：网关是多线程的，游标/冷却必须原子更新，否则并发请求会全砸同一个账号。"""
    with STATE_LOCK:
        return _order_candidates(cfg, st, model)


def _order_candidates(cfg: dict, st: dict, model: str) -> list:
    now = time.time()
    ready = []
    for a in cfg["accounts"]:
        if not supports(a, cfg, model):
            continue
        s = acc_state(st, a["name"])
        if s.get("auth_error"):
            continue
        if s.get("cooldown_until", 0) > now:
            continue
        cap = a.get("daily_requests")
        if cap and s.get("requests", 0) >= int(cap):
            continue
        ncap = daily_budget(cfg, a)
        if ncap and float(s.get("neurons", 0)) >= ncap:
            continue
        rpm = a.get("rpm") or cfg["router"].get("rpm") or 0
        if rpm and _recent_count(s) >= int(rpm):
            continue
        if model in (s.get("bad_models") or []):
            continue
        ready.append(a)

    if not ready:
        return []

    strategy = cfg["router"].get("strategy", "round_robin")
    if strategy == "least_used":
        ready.sort(key=lambda a: acc_state(st, a["name"]).get("requests", 0))
    elif strategy == "random":
        random.shuffle(ready)
    else:  # round_robin
        n = len(ready)
        cur = st.get("rr_cursor", 0) % n
        ready = ready[cur:] + ready[:cur]
        st["rr_cursor"] = (cur + 1) % n
    return ready


# --------------------------------------------------------------------------- 上游调用

def classify(code: int) -> str:
    if code in (401, 403):
        return "auth"
    if code == 429:
        return "rate"
    if code == 404:
        return "model"
    if 500 <= code < 600:
        return "server"
    return "client"


def build_request(cfg: dict, acct: dict, payload: dict):
    model = resolve_model(cfg, acct, payload.get("model", ""))
    body = {k: v for k, v in payload.items() if k in ALLOWED_KEYS}
    body["model"] = model

    if acct["type"] == "cloudflare":
        url = CF_ACCOUNT_CHAT.format(acct["account_id"])
        headers = {"Authorization": "Bearer " + acct["token"]}
    else:
        url = acct["base_url"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + acct.get("api_key", "")}
    headers["Content-Type"] = "application/json"
    ua = acct.get("user_agent") or cfg["router"].get("user_agent")
    if ua:
        headers["User-Agent"] = ua
    return url, headers, body


def _usage_tokens(data):
    """兼容 chat (prompt/completion_tokens) 与 clef (input/output_tokens) 两种 usage。"""
    if not isinstance(data, dict):
        return 0, 0
    inner = data.get("result") if isinstance(data.get("result"), dict) else data
    usage = (inner.get("usage") or {}) if isinstance(inner, dict) else {}
    tin = usage.get("prompt_tokens", usage.get("input_tokens")) or 0
    tout = usage.get("completion_tokens", usage.get("output_tokens")) or 0
    return int(tin), int(tout)


def record_success(st: dict, name: str, data, neurons_hint=None) -> None:
    with STATE_LOCK:
        _record_success(st, name, data, neurons_hint)


def _hour_key(ts=None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%dT%H")


def _bump_hourly(st: dict, name: str, ok: bool, neurons: float = 0.0, tin: int = 0, tout: int = 0) -> None:
    h = st.setdefault("hourly", {}).setdefault(name, {})
    b = h.setdefault(_hour_key(), {"ok": 0, "fail": 0, "neurons": 0.0, "tin": 0, "tout": 0})
    if ok:
        b["ok"] += 1
    else:
        b["fail"] += 1
    b["neurons"] = round(float(b.get("neurons", 0)) + neurons, 4)
    b["tin"] = int(b.get("tin", 0)) + tin
    b["tout"] = int(b.get("tout", 0)) + tout
    if len(h) > 120:
        for k in sorted(h)[:-72]:
            h.pop(k, None)


def _record_success(st: dict, name: str, data, neurons_hint=None) -> None:
    s = acc_state(st, name)
    s["requests"] += 1
    s["ok"] += 1
    s["last_used"] = int(time.time())
    s["fail_streak"] = 0
    s["cooldown_until"] = 0
    s["last_error"] = ""
    tin, tout = _usage_tokens(data)
    s["tokens_in"] += tin
    s["tokens_out"] += tout
    inner = data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
    usage = (inner.get("usage") or {}) if isinstance(inner, dict) else {}
    neu = float(neurons_hint) if neurons_hint is not None else float(usage.get("neurons") or 0)
    s["neurons"] = round(float(s.get("neurons", 0)) + neu, 4)
    _bump_hourly(st, name, True, neu, tin, tout)


def _data_neurons(data) -> float:
    inner = data.get("result") if isinstance(data, dict) and isinstance(data.get("result"), dict) else data
    usage = (inner.get("usage") or {}) if isinstance(inner, dict) else {}
    try:
        return float(usage.get("neurons") or 0)
    except (TypeError, ValueError):
        return 0.0


def _neurons_header(resp):
    """CF 每个响应都带 cf-ai-neurons，流式也有——比 body 里的 usage 更可靠。"""
    try:
        v = resp.headers.get("cf-ai-neurons")
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _model_unavailable(msg: str) -> bool:
    m = (msg or "").lower()
    return ("is not available" in m or "not available on" in m
            or "model not found" in m or "no such model" in m)


def _extract_model(msg: str):
    mt = re.search(r"@[a-z0-9._-]+/[a-z0-9._/-]+", msg or "", re.I)
    return mt.group(0) if mt else None


def record_failure(st: dict, cfg: dict, name: str, code: int, msg: str, retry_after=None, model=None) -> None:
    with STATE_LOCK:
        _record_failure(st, cfg, name, code, msg, retry_after, model)


def _record_failure(st: dict, cfg: dict, name: str, code: int, msg: str, retry_after=None, model=None) -> None:
    s = acc_state(st, name)
    s["requests"] += 1
    s["fail"] += 1
    s["last_used"] = int(time.time())
    s["fail_streak"] = int(s.get("fail_streak", 0)) + 1
    s["last_error"] = f"HTTP {code}: {msg}" if code else str(msg)
    _bump_hourly(st, name, False)
    kind = classify(code) if code else "server"
    if kind == "auth" and _model_unavailable(msg):
        # 403/401 但原因是"这个账号没有这个模型"——是模型权限问题，不是凭证失效，
        # 绝不能把账号踢出池子，只记下这个账号缺这个模型。
        kind = "model"
        mid = _extract_model(msg) or model
        if mid:
            bad = set(s.get("bad_models") or [])
            bad.add(mid)
            s["bad_models"] = sorted(bad)
    base = int(cfg["router"].get("cooldown_seconds", 60))
    cap = int(cfg["router"].get("max_cooldown_seconds", 900))
    if kind == "auth":
        s["auth_error"] = True
        s["cooldown_until"] = int(time.time()) + cap
    elif kind == "rate":
        wait = base
        if retry_after:
            try:
                wait = max(wait, int(float(retry_after)))
            except (TypeError, ValueError):
                pass
        s["cooldown_until"] = int(time.time()) + min(wait, cap)
    elif kind in ("server",):
        wait = min(base * (2 ** (s["fail_streak"] - 1)), cap)
        wait = wait * random.uniform(0.8, 1.2)   # 抖动，避免多账号同时重试形成尖峰
        s["cooldown_until"] = int(time.time()) + int(wait)
    elif kind == "model":
        s["cooldown_until"] = 0
    else:
        s["cooldown_until"] = 0


def _err_body(msg: str, code: int) -> dict:
    return {"error": {"message": msg, "type": "cfai_error", "code": code}}


def complete(cfg: dict, st: dict, payload: dict, timeout=None):
    """非流式。返回 (status, body_dict, account_name)。"""
    model = payload.get("model") or cfg["router"].get("default_model", "")
    if not model:
        return 400, _err_body("缺少 model，且 router.default_model 未配置", 400), None
    if not model_allowed(cfg, model):
        return 403, _err_body(f"模型未启用: {model}（在控制台「模型」页勾选后可用）", 403), None
    if cfg["router"].get("alias_only", True):
        al = alias_for(cfg, model)
        if al and al != model:
            return 403, _err_body(f"该模型已设置别名，请使用 `{al}` 调用（原始 id 不对外）", 403), None
    cands = order_candidates(cfg, st, model)
    if not cands:
        return 503, _err_body("没有可用账号（全部在冷却/超配额/鉴权失败）", 503), None

    tries = max(1, int(cfg["router"].get("max_retries", 3)))
    errors = []
    used = 0
    for acct in cands:
        if used >= tries:
            break
        used += 1
        url, headers, body = build_request(cfg, acct, payload)
        mark_dispatch(st, acct["name"])
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout or cfg["router"].get("timeout", 120)) as r:
                nh = _neurons_header(r)
                raw = r.read()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"raw": raw.decode("utf-8", "replace")}
            record_success(st, acct["name"], data, nh)
            return 200, data, acct["name"]
        except urllib.error.HTTPError as e:
            raw = e.read()
            text = raw.decode("utf-8", "replace")
            if classify(e.code) == "client":
                # 4xx 是调用方参数问题（比如拿聊天接口去调决策模型），不该算到账号头上
                return e.code, _err_body(text[:800], e.code), acct["name"]
            record_failure(st, cfg, acct["name"], e.code, text[:300],
                           e.headers.get("Retry-After") if e.headers else None,
                           model=body.get("model"))
            errors.append({"account": acct["name"], "code": e.code, "msg": text[:200]})
            if _model_unavailable(text):
                used -= 1          # 该账号只是缺这个模型，不占用重试预算
        except Exception as e:  # 网络/超时/解析
            record_failure(st, cfg, acct["name"], 0, str(e))
            errors.append({"account": acct["name"], "code": 0, "msg": str(e)[:200]})

    return 502, _err_body("所有账号均失败: " + json.dumps(errors, ensure_ascii=False)[:800], 502), None


def complete_stream(cfg: dict, st: dict, payload: dict, timeout=None):
    """流式。返回 (status, response_obj_or_body, account_name)。"""
    model = payload.get("model") or cfg["router"].get("default_model", "")
    if not model:
        return 400, _err_body("缺少 model", 400), None
    if not model_allowed(cfg, model):
        return 403, _err_body(f"模型未启用: {model}", 403), None
    if cfg["router"].get("alias_only", True):
        al = alias_for(cfg, model)
        if al and al != model:
            return 403, _err_body(f"该模型已设置别名，请使用 `{al}` 调用", 403), None
    cands = order_candidates(cfg, st, model)
    if not cands:
        return 503, _err_body("没有可用账号", 503), None

    tries = max(1, int(cfg["router"].get("max_retries", 3)))
    errors = []
    used = 0
    for acct in cands:
        if used >= tries:
            break
        used += 1
        url, headers, body = build_request(cfg, acct, payload)
        body["stream"] = True
        mark_dispatch(st, acct["name"])
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
            r = urllib.request.urlopen(req, timeout=timeout or cfg["router"].get("timeout", 120))
            record_success(st, acct["name"], {"usage": {}}, _neurons_header(r))
            return 200, r, acct["name"]
        except urllib.error.HTTPError as e:
            raw = e.read()
            text = raw.decode("utf-8", "replace")
            if classify(e.code) == "client":
                # 4xx 是调用方参数问题（比如拿聊天接口去调决策模型），不该算到账号头上
                return e.code, _err_body(text[:800], e.code), acct["name"]
            record_failure(st, cfg, acct["name"], e.code, text[:300],
                           e.headers.get("Retry-After") if e.headers else None,
                           model=body.get("model"))
            errors.append({"account": acct["name"], "code": e.code, "msg": text[:200]})
            if _model_unavailable(text):
                used -= 1          # 该账号只是缺这个模型，不占用重试预算
        except Exception as e:
            record_failure(st, cfg, acct["name"], 0, str(e))
            errors.append({"account": acct["name"], "code": 0, "msg": str(e)[:200]})

    return 502, _err_body("所有账号均失败: " + json.dumps(errors, ensure_ascii=False)[:800], 502), None


# --------------------------------------------------------------------------- 原生 run 端点（clef 等非 chat 模型）

def run_url(acct: dict, model_id: str) -> str:
    """原生 /ai/run/<model>。openai 类型从 base_url 反推：
    .../accounts/<id>/ai/v1 -> .../accounts/<id>/ai/run/<model>"""
    if acct["type"] == "cloudflare":
        return f"https://api.cloudflare.com/client/v4/accounts/{acct['account_id']}/ai/run/{model_id}"
    base = acct["base_url"].rstrip("/")
    if base.endswith("/ai/v1"):
        base = base[: -len("/ai/v1")]
    elif base.endswith("/v1"):
        base = base[: -len("/v1")]
    return f"{base}/ai/run/{model_id}"


def decide_call(cfg: dict, st: dict, model_id: str, body: dict, timeout=None):
    """在账号池里跑一次原生 run 请求，同样的轮询/冷却/故障转移。"""
    if not model_allowed(cfg, model_id):
        return 403, _err_body(f"模型未启用: {model_id}", 403), None
    cands = order_candidates(cfg, st, model_id)
    if not cands:
        return 503, _err_body("没有可用账号（全部在冷却/超配额/鉴权失败）", 503), None
    tries = max(1, int(cfg["router"].get("max_retries", 3)))
    errors = []
    used = 0
    for acct in cands:
        if used >= tries:
            break
        used += 1
        url = run_url(acct, model_id)
        if acct["type"] == "cloudflare":
            headers = {"Authorization": "Bearer " + acct["token"]}
        else:
            headers = {"Authorization": "Bearer " + acct.get("api_key", "")}
        headers["Content-Type"] = "application/json"
        ua = acct.get("user_agent") or cfg["router"].get("user_agent")
        if ua:
            headers["User-Agent"] = ua
        mark_dispatch(st, acct["name"])
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout or cfg["router"].get("timeout", 120)) as r:
                nh = _neurons_header(r)
                raw = r.read()
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("success") is False:
                err = (data.get("errors") or [{}])[0].get("message", "unknown error")
                record_failure(st, cfg, acct["name"], 400, err)
                errors.append({"account": acct["name"], "code": 400, "msg": err[:200]})
                continue
            record_success(st, acct["name"], data, nh)
            return 200, data, acct["name"]
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if classify(e.code) == "client":
                # 4xx 是调用方参数问题（比如拿聊天接口去调决策模型），不该算到账号头上
                return e.code, _err_body(text[:800], e.code), acct["name"]
            record_failure(st, cfg, acct["name"], e.code, text[:300],
                           e.headers.get("Retry-After") if e.headers else None,
                           model=body.get("model"))
            errors.append({"account": acct["name"], "code": e.code, "msg": text[:200]})
            if _model_unavailable(text):
                used -= 1          # 该账号只是缺这个模型，不占用重试预算
        except Exception as e:
            record_failure(st, cfg, acct["name"], 0, str(e))
            errors.append({"account": acct["name"], "code": 0, "msg": str(e)[:200]})
    return 502, _err_body("所有账号均失败: " + json.dumps(errors, ensure_ascii=False)[:800], 502), None


def _data_url(path: str) -> str:
    import base64
    import mimetypes
    mime = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


def _fmt_answer(qid: str, a: dict) -> str:
    t = a.get("type", "")
    if t == "noul":
        p = a.get("noul", 0)
        return f"{qid:<14}noul    {'YES' if p >= 0.5 else 'NO '}  p={p:.3f}"
    if t == "choice":
        probs = a.get("probabilities") or {}
        detail = "  ".join(f"{k}={v:.3f}" for k, v in sorted(probs.items(), key=lambda kv: -kv[1]))
        return f"{qid:<14}choice  {a.get('choice')}  (conf {a.get('confidence', 0):.2f})\n{'':<14}        {detail}"
    if t == "score":
        legend = a.get("legend") or {}
        probs = a.get("probabilities") or {}
        detail = "  ".join(f"{k}={v:.2f}" for k, v in sorted(probs.items(), key=lambda kv: int(kv[0])))
        top = max(probs.items(), key=lambda kv: kv[1])[0] if probs else ""
        return (f"{qid:<14}score   {a.get('score', 0):.2f}  top={legend.get(top, top)}"
                f"  (conf {a.get('confidence', 0):.2f})\n{'':<14}        {detail}")
    return f"{qid:<14}{t}  {json.dumps(a, ensure_ascii=False)[:120]}"


def cmd_decide(args) -> int:
    cfg = load_config()
    st = load_state()
    model = args.model or "clef-flash"
    if cfg["router"].get("alias_only", True) and model.startswith("@"):
        al = alias_for(cfg, model)
        if al:
            print(f"该模型已设置别名，请用 -m {al} 调用", file=sys.stderr)
            return 1
    model_id = model if model.startswith("@") else "@cf/cloudflare/" + model
    selector = model_id.rsplit("/", 1)[-1]

    if args.state_file:
        with open(args.state_file, "r", encoding="utf-8") as f:
            state = f.read()
    elif args.state == "-" or args.state is None:
        if sys.stdin.isatty():
            print("请用 --state / --state-file 提供 state（或管道传入）", file=sys.stderr)
            return 1
        state = sys.stdin.read()
    else:
        state = args.state

    raw_q = args.questions
    if args.questions_file:
        with open(args.questions_file, "r", encoding="utf-8") as f:
            raw_q = f.read()
    if not raw_q:
        print("请用 --questions 或 --questions-file 提供问题定义", file=sys.stderr)
        return 1
    try:
        questions = json.loads(raw_q)
    except json.JSONDecodeError as e:
        print(f"questions 不是合法 JSON: {e}", file=sys.stderr)
        return 1

    body = {"model": selector, "state": state, "questions": questions}
    if args.image:
        body["images"] = [_data_url(p) for p in args.image]

    status, data, acct = decide_call(cfg, st, model_id, body)
    save_state(st)
    if status != 200:
        print(json.dumps(data, ensure_ascii=False), file=sys.stderr)
        return 1
    if cfg["router"].get("verbose"):
        print(f"[cfai] -> {acct}", file=sys.stderr)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    result = data.get("result") if isinstance(data.get("result"), dict) else data
    print(f"model: {result.get('model', selector)}  account: {acct}")
    for qid, ans in (result.get("answers") or {}).items():
        print("  " + _fmt_answer(qid, ans))
    u = result.get("usage") or {}
    if u:
        print(f"usage: {u}")
    return 0


# --------------------------------------------------------------------------- 命令：账号管理

def cmd_account_add(args) -> int:
    cfg = load_config()
    if get_account(cfg, args.name):
        print(f"账号已存在: {args.name}（先 rm 再 add，或用 account rm {args.name}）", file=sys.stderr)
        return 1

    if args.type == "cloudflare":
        if not args.account_id or not args.token:
            print("cloudflare 类型需要 --account-id 和 --token", file=sys.stderr)
            return 1
        acct = {"name": args.name, "type": "cloudflare",
                "account_id": args.account_id, "token": args.token}
    else:
        if not args.base_url:
            print("openai 类型需要 --base-url（例如 https://xxx.workers.dev/v1）", file=sys.stderr)
            return 1
        acct = {"name": args.name, "type": "openai",
                "base_url": args.base_url, "api_key": args.api_key or ""}

    if args.models:
        acct["models"] = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.model_map:
        try:
            acct["model_map"] = json.loads(args.model_map)
        except json.JSONDecodeError:
            print("--model-map 必须是 JSON，例如 '{\"gpt-4o-mini\":\"@cf/meta/llama-3.1-8b-instruct\"}'", file=sys.stderr)
            return 1
    if args.daily_requests:
        acct["daily_requests"] = int(args.daily_requests)
    if args.daily_neurons:
        acct["daily_neurons"] = float(args.daily_neurons)
    if args.note:
        acct["note"] = args.note

    cfg["accounts"].append(acct)
    save_config(cfg)
    print(f"已添加账号 {acct['name']} ({acct['type']})，共 {len(cfg['accounts'])} 个")

    if args.test:
        return cmd_account_test(argparse.Namespace(name=args.name, model=args.model or None, json=False))
    return 0


def cmd_account_list(args) -> int:
    cfg = load_config()
    st = load_state()
    if not cfg["accounts"]:
        print("还没有账号。用 `cfai account add ...` 添加，或 `cfai init` 看示例。")
        return 0
    now = time.time()
    print(f"{'#':<3}{'名称':<18}{'类型':<12}{'状态':<10}{'今日请求':<10}{'今日失败':<10}模型限制")
    for i, a in enumerate(cfg["accounts"], 1):
        s = acc_state(st, a["name"])
        if s.get("auth_error"):
            status = "鉴权失败"
        elif s.get("cooldown_until", 0) > now:
            status = f"冷却{int(s['cooldown_until'] - now)}s"
        else:
            status = "就绪"
        models = ",".join(a.get("models") or []) or "全部"
        if len(models) > 40:
            models = models[:37] + "..."
        print(f"{i:<3}{a['name']:<18}{a['type']:<12}{status:<10}{s['requests']:<10}{s['fail']:<10}{models}")
    return 0


def cmd_account_rm(args) -> int:
    cfg = load_config()
    a = find_account(cfg, args.name)
    if not a:
        print(f"找不到账号: {args.name}", file=sys.stderr)
        return 1
    cfg["accounts"] = [x for x in cfg["accounts"] if x["name"] != a["name"]]
    save_config(cfg)
    st = load_state()
    st["accounts"].pop(a["name"], None)
    save_state(st)
    print(f"已删除账号 {a['name']}")
    return 0


def cmd_account_reset(args) -> int:
    """清除冷却 / 鉴权失败标记。"""
    cfg = load_config()
    st = load_state()
    targets = [find_account(cfg, args.name)] if args.name else cfg["accounts"]
    if not any(targets):
        print("找不到账号", file=sys.stderr)
        return 1
    for a in targets:
        if not a:
            continue
        st["accounts"][a["name"]] = new_account_state()
    save_state(st)
    print("已重置: " + ", ".join(a["name"] for a in targets if a))
    return 0


def _acct_headers(cfg: dict, acct: dict) -> dict:
    if acct["type"] == "cloudflare":
        tok = acct["token"]
    else:
        tok = acct.get("api_key", "")
    h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}
    ua = acct.get("user_agent") or cfg["router"].get("user_agent")
    if ua:
        h["User-Agent"] = ua
    return h


def _post_json(url: str, headers: dict, body: dict, timeout: int = 30):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _probe(cfg: dict, acct: dict, model: str):
    """按模型类型选探测方式：聊天 / clef 决策 / embedding。返回 (ok, err)。"""
    model = (cfg.get("aliases") or {}).get(model, model)
    mid = model.lower()
    cat = (load_catalog().get("models") or {}).get(model) or {}
    task = (cat.get("task") or "").lower()

    if "clef" in mid:
        body = {"model": model.rsplit("/", 1)[-1],
                "state": "connectivity probe",
                "questions": {"ok": {"type": "noul", "instructions": "Is this a probe?"}}}
        data = _post_json(run_url(acct, model), _acct_headers(cfg, acct), body)
        if isinstance(data, dict) and data.get("success") is False:
            return False, str((data.get("errors") or [{}])[0].get("message", ""))[:200]
        return True, ""

    if "embedding" in task or "/bge-" in mid or "embed" in mid:
        data = _post_json(run_url(acct, model), _acct_headers(cfg, acct), {"text": ["ping"]})
        if isinstance(data, dict) and data.get("success") is False:
            return False, str((data.get("errors") or [{}])[0].get("message", ""))[:200]
        return True, ""

    # 默认按聊天模型探测
    payload = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
    url, headers, body = build_request(cfg, acct, payload)
    _post_json(url, headers, body)
    return True, ""


def _health_check_all(cfg: dict = None) -> dict:
    """逐个账号做一次真实探测，结果写进状态——「就绪」才有依据。"""
    cfg = cfg or load_config()
    st = Handler.state or load_state()
    model = _pick_test_model(cfg)
    ok_n = 0
    for a in cfg["accounts"]:
        try:
            ok, ms, used, err = _test_one(cfg, a, model)
        except Exception as e:
            ok, ms, used, err = False, 0, model, str(e)[:200]
        with STATE_LOCK:
            s = acc_state(st, a["name"])
            s["checked_at"] = int(time.time())
            s["check_ok"] = ok
            if ok:
                ok_n += 1
                s["auth_error"] = False
                s["cooldown_until"] = 0
                s["fail_streak"] = 0
                s["last_error"] = ""
            else:
                s["last_error"] = err
                if "401" in err or "403" in err:
                    s["auth_error"] = True
            save_state(st)
    return {"checked": len(cfg["accounts"]), "ok": ok_n, "model": model}


def _pick_test_model(cfg: dict) -> str:
    """测试用的模型：优先白名单第一个（探测方式会按模型类型自动适配）。"""
    allow = allowed_models(cfg)
    if allow:
        return (cfg.get("aliases") or {}).get(allow[0], allow[0])
    dm = cfg["router"].get("default_model") or ""
    dm_res = (cfg.get("aliases") or {}).get(dm, dm)
    return dm_res or "@cf/meta/llama-3.3-70b-instruct-fp8-fast"


def _test_one(cfg, acct, model=None):
    model = model or _pick_test_model(cfg)
    t0 = time.time()
    try:
        ok, err = _probe(cfg, acct, model)
        return ok, int((time.time() - t0) * 1000), model, ("" if ok else err)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        return False, int((time.time() - t0) * 1000), model, f"HTTP {e.code}: {text[:200]}"
    except Exception as e:
        return False, int((time.time() - t0) * 1000), model, str(e)[:200]


def cmd_account_test(args) -> int:
    cfg = load_config()
    st = load_state()
    if args.name:
        a = find_account(cfg, args.name)
        if not a:
            print(f"找不到账号: {args.name}", file=sys.stderr)
            return 1
        targets = [a]
    else:
        targets = cfg["accounts"]
    if not targets:
        print("没有账号可测", file=sys.stderr)
        return 1

    bad = 0
    results = []
    for a in targets:
        ok, ms, model, err = _test_one(cfg, a, args.model)
        results.append({"account": a["name"], "ok": ok, "ms": ms, "model": model, "error": err})
        s = acc_state(st, a["name"])
        if ok:
            s["auth_error"] = False
            s["cooldown_until"] = 0
            s["fail_streak"] = 0
            s["last_error"] = ""
        else:
            bad += 1
            s["last_error"] = err
            if "401" in err or "403" in err:
                s["auth_error"] = True
        if not args.json:
            mark = "OK  " if ok else "FAIL"
            print(f"[{mark}] {a['name']:<18} {ms:>5}ms  {model}" + (f"  <- {err}" if err else ""))
    save_state(st)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if bad == 0 else 2


def cmd_account_show(args) -> int:
    cfg = load_config()
    a = find_account(cfg, args.name)
    if not a:
        print(f"找不到账号: {args.name}", file=sys.stderr)
        return 1
    red = json.loads(json.dumps(a))
    for k in ("token", "api_key"):
        if red.get(k):
            v = red[k]
            red[k] = v[:6] + "..." + v[-4:] if len(v) > 12 else "***"
    print(json.dumps(red, ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- 命令：chat

def build_messages(args):
    msgs = []
    if args.system:
        msgs.append({"role": "system", "content": args.system})
    if args.prompt == "-" or args.prompt is None:
        return msgs
    msgs.append({"role": "user", "content": args.prompt})
    return msgs


def stream_print(resp) -> str:
    out = []
    for line in resp:
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            continue
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            piece = d.get("content") or d.get("reasoning_content") or ""
            if piece:
                sys.stdout.write(piece)
                sys.stdout.flush()
                out.append(piece)
    sys.stdout.write("\n")
    sys.stdout.flush()
    return "".join(out)


def cmd_chat(args) -> int:
    cfg = load_config()
    st = load_state()
    model = args.model or cfg["router"].get("default_model") or ""
    if not model:
        print("请用 -m 指定模型（或设置 router.default_model）。用 `cfai models` 看可用模型。", file=sys.stderr)
        return 1
    if "clef" in model.lower():
        print("clef / clef-flash / clef-omni 是决策模型（System One API），不接受 messages。\n"
              "请改用: cfai decide -m clef-flash --state \"...\" --questions-file q.json", file=sys.stderr)
        return 1

    if args.prompt == "-" or args.prompt is None:
        return cmd_repl(cfg, st, args, model)

    payload = {"model": model, "messages": build_messages(args)}
    if args.max_tokens:
        payload["max_tokens"] = args.max_tokens
    if args.temperature is not None:
        payload["temperature"] = args.temperature

    if args.stream:
        status, resp, acct = complete_stream(cfg, st, payload)
        if status != 200:
            print(json.dumps(resp, ensure_ascii=False), file=sys.stderr)
            save_state(st)
            return 1
        if cfg["router"].get("verbose"):
            print(f"[cfai] -> {acct}", file=sys.stderr)
        try:
            stream_print(resp)
        finally:
            resp.close()
        save_state(st)
        return 0

    status, data, acct = complete(cfg, st, payload)
    save_state(st)
    if status != 200:
        print(json.dumps(data, ensure_ascii=False), file=sys.stderr)
        return 1
    if cfg["router"].get("verbose"):
        print(f"[cfai] -> {acct}", file=sys.stderr)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        try:
            msg = data["choices"][0]["message"]
            text = msg.get("content") or msg.get("reasoning_content") or ""
            print(text if text else json.dumps(data, ensure_ascii=False, indent=2))
        except (KeyError, IndexError, TypeError):
            print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def cmd_repl(cfg, st, args, model) -> int:
    msgs = []
    if args.system:
        msgs.append({"role": "system", "content": args.system})
    print(f"cfai 交互模式 · 模型 {model} · Ctrl-D 退出", file=sys.stderr)
    while True:
        try:
            line = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line.strip():
            continue
        msgs.append({"role": "user", "content": line})
        payload = {"model": model, "messages": msgs, "stream": True}
        if args.max_tokens:
            payload["max_tokens"] = args.max_tokens
        status, resp, acct = complete_stream(cfg, st, payload)
        if status != 200:
            print(json.dumps(resp, ensure_ascii=False), file=sys.stderr)
            msgs.pop()
            save_state(st)
            continue
        if cfg["router"].get("verbose"):
            print(f"[cfai] -> {acct}", file=sys.stderr)
        try:
            text = stream_print(resp)
        finally:
            resp.close()
        msgs.append({"role": "assistant", "content": text})
        save_state(st)


# --------------------------------------------------------------------------- 命令：models / stats / doctor

CF_URL_RE = re.compile(r"https://api\.cloudflare\.com/client/v4/accounts/([0-9a-f]+)/ai/v1")
CHAT_TASKS = ("text generation", "conversation", "summarization")


def catalog_path() -> str:
    return os.path.join(home_dir(), "catalog.json")


def load_catalog() -> dict:
    p = catalog_path()
    if not os.path.exists(p):
        return {"fetched_at": 0, "models": {}}
    try:
        with open(p, "r", encoding="utf-8") as f:
            cat = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"fetched_at": 0, "models": {}}
    cat.setdefault("fetched_at", 0)
    cat.setdefault("models", {})
    return cat


def save_catalog(cat: dict) -> None:
    ensure_home()
    p = catalog_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _remote_models(acct, cfg: dict = None) -> list:
    """返回 [{"id","task","context","price"}]。CF 的 /ai/v1 不支持 GET /models（405），
    改用 catalog 搜索接口 /ai/models/search。"""
    ua = acct.get("user_agent") or ((cfg or {}).get("router") or {}).get("user_agent")
    base = acct.get("base_url", "")
    m = CF_URL_RE.match(base)
    if acct["type"] == "cloudflare" or m:
        acc = acct["account_id"] if acct["type"] == "cloudflare" else m.group(1)
        token = acct["token"] if acct["type"] == "cloudflare" else acct.get("api_key", "")
        url = f"https://api.cloudflare.com/client/v4/accounts/{acc}/ai/models/search?per_page=200"
        hdrs = {"Authorization": "Bearer " + token}
        if ua:
            hdrs["User-Agent"] = ua
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read())
        out = []
        for x in (data.get("result") or []):
            name = x.get("name")
            if not name:
                continue
            props = {p.get("property_id"): p.get("value") for p in (x.get("properties") or [])}
            raw_price = props.get("price")
            pin = pout = pcached = None
            extra = []
            if isinstance(raw_price, list):
                for item in raw_price:
                    if not isinstance(item, dict):
                        continue
                    unit = (item.get("unit") or "").lower()
                    val = item.get("price")
                    if "cached" in unit and "input" in unit:
                        pcached = val
                    elif "input tokens" in unit:
                        pin = val
                    elif "output tokens" in unit:
                        pout = val
                    else:
                        extra.append({"unit": item.get("unit"), "price": val})
            elif isinstance(raw_price, (int, float)):
                pin = raw_price
            out.append({
                "id": name,
                "task": (x.get("task") or {}).get("name", ""),
                "context": props.get("context_window"),
                "price_in": pin,
                "price_out": pout,
                "price_cached": pcached,
                "price_other": extra,
            })
        return out

    url = base.rstrip("/") + "/models"
    hdrs = {"Authorization": "Bearer " + acct.get("api_key", "")}
    if ua:
        hdrs["User-Agent"] = ua
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read())
    out = []
    for x in (data.get("data") or data.get("result") or []):
        mid = x.get("id") or x.get("name")
        if mid:
            out.append({"id": mid, "task": "", "context": None,
                        "price_in": None, "price_out": None, "price_cached": None,
                        "price_other": []})
    return out


def refresh_catalog(cfg: dict, quiet: bool = False) -> dict:
    """从每个账号拉一次模型目录，合并成 catalog.json（记录哪些账号有哪个模型）。"""
    models = {}
    ok = 0
    errs = []
    for a in cfg["accounts"]:
        try:
            items = _remote_models(a, cfg)
            ok += 1
        except Exception as e:
            errs.append(f"[{a['name']}] {str(e)[:100]}")
            continue
        for it in items:
            e = models.setdefault(it["id"], {"task": it["task"], "context": it["context"],
                                             "price_in": it["price_in"], "price_out": it["price_out"],
                                             "price_cached": it["price_cached"],
                                             "price_other": it["price_other"], "accounts": []})
            e["accounts"].append(a["name"])
            if it["task"]:
                e["task"] = it["task"]
            if it["context"] and not e.get("context"):
                e["context"] = it["context"]
            for k in ("price_in", "price_out", "price_cached"):
                if it[k] is not None and e.get(k) is None:
                    e[k] = it[k]
    if not quiet:
        for e in errs:
            print("拉取失败 " + e, file=sys.stderr)
    if models:
        cat = {"fetched_at": int(time.time()), "accounts_ok": ok, "models": models}
        save_catalog(cat)
        return cat
    return load_catalog()


def ensure_catalog_fresh(cfg: dict, ttl_hours: float = 24.0, quiet: bool = True) -> dict:
    cat = load_catalog()
    age = time.time() - float(cat.get("fetched_at") or 0)
    if not cat.get("models") or age > ttl_hours * 3600:
        return refresh_catalog(cfg, quiet=quiet)
    return cat


def _cat_rows(cat: dict, task: str = None) -> list:
    rows = []
    for mid, e in (cat.get("models") or {}).items():
        if task and task.lower() not in (e.get("task") or "").lower():
            continue
        rows.append((mid, e))
    rows.sort(key=lambda r: r[0])
    return rows


def cmd_key(args) -> int:
    cfg = load_config()
    keys = cfg.setdefault("keys", [])
    act = args.action or "list"
    st = load_state()

    if act == "list":
        if not keys:
            print("还没有客户端 Key，用 `cfai key add <名称>` 创建")
            return 0
        print(f"{'名称':<18}{'Key':<26}{'状态':<6}{'今日请求':<10}{'日限额'}")
        for k in keys:
            s = key_state(st, k["name"])
            cap = k.get("daily_requests") or 0
            print(f"{k['name']:<18}{_mask(k.get('key', '')):<26}"
                  f"{'启用' if k.get('enabled', True) else '停用':<6}"
                  f"{s['requests']:<10}{cap if cap else '不限'}")
        return 0

    if act == "add":
        if not args.name:
            print("用法: cfai key add <名称> [--key ...] [--daily-requests N]", file=sys.stderr)
            return 1
        if get_key(cfg, args.name):
            print(f"Key 名称已存在: {args.name}", file=sys.stderr)
            return 1
        token = args.key or new_key_string()
        keys.append({
            "name": args.name, "key": token, "enabled": True,
            "daily_requests": int(args.daily_requests or 0),
            "daily_neurons": float(args.daily_neurons or 0),
            "note": args.note or "",
        })
        save_config(cfg)
        print(f"已创建「{args.name}」：{token}")
        print("客户端 Base URL 用 http://<你的地址>/v1，Key 填上面这串。")
        return 0

    if act == "rm":
        if not args.name:
            print("用法: cfai key rm <名称>", file=sys.stderr)
            return 1
        n = len(keys)
        cfg["keys"] = [k for k in keys if k.get("name") != args.name]
        if len(cfg["keys"]) == n:
            print(f"没有这个 Key: {args.name}", file=sys.stderr)
            return 1
        save_config(cfg)
        print(f"已删除 {args.name}")
        return 0

    if act in ("enable", "disable"):
        k = get_key(cfg, args.name or "")
        if not k:
            print(f"没有这个 Key: {args.name}", file=sys.stderr)
            return 1
        k["enabled"] = (act == "enable")
        save_config(cfg)
        print(f"{args.name} 已{'启用' if k['enabled'] else '停用'}")
        return 0

    return 1


def cmd_models(args) -> int:
    if getattr(args, "allow", None) is not None:
        cfg = load_config()
        raw = (args.allow or "").strip()
        if raw in ("", "none", "-", "all"):
            cfg["allowed_models"] = []
            print("白名单已清空：所有模型都对外可用（不建议）")
        else:
            ids = [x.strip() for x in raw.split(",") if x.strip()]
            cfg["allowed_models"] = ids
            print("白名单已更新（%d 个）：" % len(ids))
            for i in ids:
                print("  " + i)
        save_config(cfg)
        return 0

    cfg = load_config()
    cat = refresh_catalog(cfg) if args.remote else load_catalog()

    if args.remote and not cat.get("models"):
        print("一个账号都没拉到模型列表", file=sys.stderr)
        return 1

    aliases = cfg.get("aliases") or {}
    rows = _cat_rows(cat, args.task)

    if rows:
        when = datetime.fromtimestamp(cat["fetched_at"]).strftime("%Y-%m-%d %H:%M") if cat.get("fetched_at") else "未知"
        print(f"目录更新时间 {when} · {len(rows)} 个模型（--remote 立即刷新）")
        print(f"{'模型':<52}{'任务':<18}{'上下文':<8}{'入$':<9}{'出$':<9}{'账号'}")
        for mid, e in rows:
            ctx = e.get("context") or "-"
            pin = e.get("price_in")
            pout = e.get("price_out")
            pin_s = f"{pin:g}" if pin is not None else "-"
            if pout is not None:
                pout_s = f"{pout:g}"
            elif pin is not None:
                pout_s = "免费"
            else:
                pout_s = "-"
            print(f"{mid:<52}{(e.get('task') or '-'):<18}{str(ctx):<8}{pin_s:<9}{pout_s:<9}{len(e.get('accounts') or [])}")
        print("（单位：美元 / 百万 token；“免费”= 该模型只收输入费）")
        if not args.remote and time.time() - float(cat.get("fetched_at") or 0) > 86400:
            print("（缓存超过 24 小时，建议 cfai models --remote 刷新）", file=sys.stderr)
    else:
        print("还没有模型目录缓存。运行 `cfai models --remote` 从各账号拉一次。")

    if aliases:
        print("\n别名:")
        for k, v in aliases.items():
            print(f"  {k} -> {v}")
    return 0


def cmd_alias(args) -> int:
    cfg = load_config()
    if args.action == "set":
        cfg["aliases"][args.name] = args.target
        save_config(cfg)
        print(f"别名 {args.name} -> {args.target}")
    elif args.action == "rm":
        if cfg["aliases"].pop(args.name, None) is None:
            print("没有这个别名", file=sys.stderr)
            return 1
        save_config(cfg)
        print(f"已删除别名 {args.name}")
    else:
        for k, v in (cfg.get("aliases") or {}).items():
            print(f"{k} -> {v}")
    return 0


def cmd_stats(args) -> int:
    cfg = load_config()
    st = load_state()
    if args.reset:
        for a in cfg["accounts"]:
            st["accounts"][a["name"]] = new_account_state()
        st["hourly"] = {}
        save_state(st)
        print("统计已清零（含 24 小时图）")
        return 0
    if not cfg["accounts"]:
        print("没有账号")
        return 0
    if args.json:
        print(json.dumps({"date": st["date"], "strategy": cfg["router"].get("strategy"),
                          "accounts": {a["name"]: acc_state(st, a["name"]) for a in cfg["accounts"]}},
                         ensure_ascii=False, indent=2))
        return 0
    now = time.time()
    total_req = total_ok = total_fail = 0
    total_neurons = 0.0
    rows = []
    for a in cfg["accounts"]:
        s = acc_state(st, a["name"])
        total_req += s["requests"]; total_ok += s["ok"]; total_fail += s["fail"]
        total_neurons += float(s.get("neurons", 0))
        if s.get("auth_error"):
            status = "鉴权失败"
        elif s.get("cooldown_until", 0) > now:
            status = f"冷却 {int(s['cooldown_until'] - now)}s"
        else:
            status = "就绪"
        last = datetime.fromtimestamp(s["last_used"]).strftime("%H:%M:%S") if s["last_used"] else "-"
        nq = a.get("daily_neurons")
        ntxt = f"{s.get('neurons', 0):.0f}/{int(nq)}" if nq else f"{s.get('neurons', 0):.0f}"
        rows.append((a["name"], status, s["requests"], s["ok"], s["fail"],
                     s["tokens_in"], s["tokens_out"], ntxt, last, s.get("last_error", "")))
    print(f"日期 {st['date']} · 策略 {cfg['router'].get('strategy')} · 共 {len(cfg['accounts'])} 个账号")
    print(f"{'名称':<18}{'状态':<12}{'请求':<7}{'成功':<7}{'失败':<7}{'入tok':<9}{'出tok':<9}{'神经元':<12}最后使用")
    for r in rows:
        print(f"{r[0]:<18}{r[1]:<12}{r[2]:<7}{r[3]:<7}{r[4]:<7}{r[5]:<9}{r[6]:<9}{r[7]:<12}{r[8]}")
        if r[9]:
            print(f"    最近错误: {r[9][:110]}")
    print(f"合计: 请求 {total_req} · 成功 {total_ok} · 失败 {total_fail} · 神经元 {total_neurons:.0f}")
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config()
    print(f"cfai {VERSION} · python {sys.version.split()[0]}")
    print(f"配置目录 {home_dir()} ({'存在' if os.path.isdir(home_dir()) else '不存在'})")
    p = config_path()
    if os.path.exists(p):
        mode = oct(os.stat(p).st_mode & 0o777)
        print(f"配置文件权限 {mode}" + ("  ⚠ 建议 600" if mode != "0o600" else "  ok"))
    print(f"账号数量 {len(cfg['accounts'])}")
    types = {}
    for a in cfg["accounts"]:
        types[a["type"]] = types.get(a["type"], 0) + 1
    for t, n in types.items():
        print(f"  {t}: {n}")
    print("连通性: ", end="")
    try:
        req = urllib.request.Request("https://api.cloudflare.com/client/v4/", method="GET")
        urllib.request.urlopen(req, timeout=10)
        print("api.cloudflare.com 可达")
    except urllib.error.HTTPError as e:
        print(f"api.cloudflare.com 可达 (HTTP {e.code})")
    except Exception as e:
        print(f"不可达: {e}")
    return 0


# --------------------------------------------------------------------------- serve

# --------------------------------------------------------------------------- 管理 API（Web 控制台后端）

def _mask(v: str) -> str:
    if not v:
        return ""
    return "••••" + v[-4:] if len(v) > 8 else "••••"


def _hourly_series(st: dict, hours: int = 24) -> list:
    now = time.time()
    out = []
    for i in range(hours - 1, -1, -1):
        k = _hour_key(now - i * 3600)
        ok = fail = 0
        neu = 0.0
        for _acc, h in (st.get("hourly") or {}).items():
            b = h.get(k)
            if b:
                ok += int(b.get("ok", 0))
                fail += int(b.get("fail", 0))
                neu += float(b.get("neurons", 0))
        out.append({"h": k[-2:] + ":00", "ok": ok, "fail": fail, "neurons": round(neu, 2)})
    return out


def admin_snapshot(cfg: dict, st: dict) -> dict:
    now = time.time()
    accounts = []
    for a in cfg["accounts"]:
        s = acc_state(st, a["name"])
        if s.get("auth_error"):
            status = "auth_error"
        elif s.get("cooldown_until", 0) > now:
            status = "cooling"
        elif not s.get("checked_at"):
            status = "unverified"
        elif s.get("check_ok") is False:
            status = "error"
        else:
            status = "ready"
        budget = daily_budget(cfg, a) or None
        is_default = bool(budget) and not a.get("daily_neurons")
        used = float(s.get("neurons", 0))
        accounts.append({
            "name": a["name"],
            "type": a["type"],
            "status": status,
            "cooldown_left": max(0, int(s.get("cooldown_until", 0) - now)),
            "requests": s.get("requests", 0),
            "ok": s.get("ok", 0),
            "fail": s.get("fail", 0),
            "neurons": round(used, 2),
            "budget": budget,
            "budget_is_default": is_default,
            "remaining": round(max(0.0, float(budget) - used), 2) if budget else None,
            "tokens_in": s.get("tokens_in", 0),
            "tokens_out": s.get("tokens_out", 0),
            "last_used": s.get("last_used", 0),
            "last_error": s.get("last_error", ""),
            "checked_at": s.get("checked_at", 0),
            "check_ok": s.get("check_ok"),
            "bad_models": s.get("bad_models") or [],
            "models": a.get("models") or [],
            "daily_requests": a.get("daily_requests"),
            "rpm": a.get("rpm"),
            "note": a.get("note", ""),
            "user_agent": a.get("user_agent", ""),
            "target": a.get("account_id") or a.get("base_url", ""),
            "key_hint": _mask(a.get("token") or a.get("api_key") or ""),
        })
    cat = load_catalog()
    now_utc = datetime.now(timezone.utc)
    reset_at = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    keys_out = []
    for k in (cfg.get("keys") or []):
        s = key_state(st, k["name"])
        keys_out.append({
            "name": k["name"],
            "key": k.get("key", ""),
            "enabled": k.get("enabled", True),
            "daily_requests": k.get("daily_requests"),
            "daily_neurons": k.get("daily_neurons"),
            "note": k.get("note", ""),
            "requests": s.get("requests", 0),
            "ok": s.get("ok", 0),
            "fail": s.get("fail", 0),
            "neurons": round(float(s.get("neurons", 0)), 2),
            "tin": s.get("tin", 0),
            "tout": s.get("tout", 0),
        })
    return {
        "date": st.get("date"),
        "now": int(now),
        "has_password": bool(cfg.get("admin_password")),
        "keys": keys_out,
        "reset_at": int(reset_at.timestamp()),
        "reset_in": max(0, int(reset_at.timestamp() - now)),
        "router": {k: cfg["router"].get(k) for k in
                   ("strategy", "max_retries", "rpm", "cooldown_seconds",
                    "max_cooldown_seconds", "timeout", "default_model", "user_agent",
                    "default_daily_neurons")},
        "aliases": cfg.get("aliases") or {},
        "accounts": accounts,
        "totals": {
            "accounts": len(accounts),
            "ready": sum(1 for x in accounts if x["status"] == "ready"),
            "requests": sum(x["requests"] for x in accounts),
            "ok": sum(x["ok"] for x in accounts),
            "fail": sum(x["fail"] for x in accounts),
            "neurons": round(sum(x["neurons"] for x in accounts), 2),
            "tokens_in": sum(x["tokens_in"] for x in accounts),
            "tokens_out": sum(x["tokens_out"] for x in accounts),
            "budget": round(sum(x["budget"] for x in accounts if x["budget"]), 2),
            "remaining": round(sum(x["remaining"] for x in accounts if x["remaining"] is not None), 2)
            if any(x["remaining"] is not None for x in accounts) else None,
        },
        "hourly": _hourly_series(st),
        "catalog": {"count": len(cat.get("models") or {}),
                    "fetched_at": cat.get("fetched_at", 0)},
    }


def admin_add_account(cfg: dict, d: dict):
    name = (d.get("name") or "").strip()
    if not name:
        return 400, "名称不能为空"
    if get_account(cfg, name):
        return 409, f"账号 {name} 已存在"
    typ = d.get("type") or "cloudflare"
    if typ == "cloudflare":
        if not (d.get("account_id") or "").strip() or not (d.get("token") or "").strip():
            return 400, "cloudflare 类型需要 account_id 和 token"
        a = {"name": name, "type": "cloudflare",
             "account_id": d["account_id"].strip(), "token": d["token"].strip()}
    else:
        if not (d.get("base_url") or "").strip():
            return 400, "openai 类型需要 base_url"
        a = {"name": name, "type": "openai",
             "base_url": d["base_url"].strip(), "api_key": (d.get("api_key") or "").strip()}
    models = d.get("models")
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    if models:
        a["models"] = models
    for k in ("daily_requests", "daily_neurons", "rpm"):
        if d.get(k) not in (None, "", 0):
            try:
                a[k] = float(d[k]) if k == "daily_neurons" else int(d[k])
            except (TypeError, ValueError):
                return 400, f"{k} 必须是数字"
    if d.get("note"):
        a["note"] = d["note"]
    if (d.get("user_agent") or "").strip():
        a["user_agent"] = d["user_agent"].strip()
    cfg["accounts"].append(a)
    save_config(cfg)
    return 200, a


def admin_update_account(cfg: dict, name: str, d: dict):
    a = get_account(cfg, name)
    if not a:
        return 404, "账号不存在"
    new_name = (d.get("name") or "").strip()
    if new_name and new_name != name:
        if get_account(cfg, new_name):
            return 409, f"账号 {new_name} 已存在"
        a["name"] = new_name
    for k in ("base_url", "account_id", "note", "user_agent"):
        if k in d and isinstance(d[k], str):
            if k == "user_agent" and not d[k].strip():
                a.pop("user_agent", None)
            else:
                a[k] = d[k].strip()
    # 密钥：空字符串表示"不改"
    for k in ("token", "api_key"):
        if d.get(k):
            a[k] = d[k].strip()
    if "models" in d:
        m = d["models"]
        if isinstance(m, str):
            m = [x.strip() for x in m.split(",") if x.strip()]
        if m:
            a["models"] = m
        else:
            a.pop("models", None)
    for k in ("daily_requests", "daily_neurons", "rpm"):
        if k in d:
            v = d[k]
            if v in (None, "", 0, "0"):
                a.pop(k, None)
            else:
                try:
                    a[k] = float(v) if k == "daily_neurons" else int(v)
                except (TypeError, ValueError):
                    return 400, f"{k} 必须是数字"
    save_config(cfg)
    return 200, a


def admin_delete_account(cfg: dict, name: str, st: dict):
    a = get_account(cfg, name)
    if not a:
        return 404, "账号不存在"
    cfg["accounts"] = [x for x in cfg["accounts"] if x["name"] != name]
    save_config(cfg)
    st.get("accounts", {}).pop(name, None)
    st.get("hourly", {}).pop(name, None)
    save_state(st)
    return 200, {"deleted": name}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg = None
    api_key = None
    state = None
    cfg_mtime = 0.0
    _raw_body = b""

    @classmethod
    def _ensure_cfg(cls):
        """配置被控制台或命令行改过后，下一个请求自动重载，不用重启进程。"""
        try:
            m = os.path.getmtime(config_path())
        except OSError:
            return
        if m != cls.cfg_mtime:
            try:
                cls.cfg = load_config()
                cls.cfg_mtime = m
            except Exception:
                pass

    def log_message(self, fmt, *a):
        if Handler.cfg and Handler.cfg["router"].get("verbose"):
            sys.stderr.write("[serve] " + (fmt % a) + "\n")

    # -- 工具
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code: int, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in getattr(self, "_extra_headers", {}).items():
            self.send_header(k, str(v))
        self._extra_headers = {}
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _bearer(self) -> str:
        h = self.headers.get("Authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else ""

    def _auth_ok(self) -> bool:
        if not Handler.api_key:
            return True
        return self._bearer() == Handler.api_key

    def _admin_ok(self) -> bool:
        """控制台鉴权：12 小时会话优先，其次是密码哈希（脚本用），最后退回 api_key。"""
        cfg = Handler.cfg or {}
        pw = cfg.get("admin_password") or ""
        tok = self._bearer() or self.headers.get("X-Admin-Token", "")
        if tok and session_ok(tok):
            return True
        if not pw:
            return self._auth_ok()
        return tok == pw

    def _client_key(self):
        """返回调用方 Key 的名字；未配置任何 Key 时返回 __open__。"""
        cfg = Handler.cfg or {}
        has_keys = bool(cfg.get("keys")) or bool(Handler.api_key)
        if not has_keys:
            return "__open__"
        tok = self._bearer()
        if not tok:
            return None
        if Handler.api_key and tok == Handler.api_key:
            return "__master__"
        k = find_key(cfg, tok)
        return k["name"] if k else None

    def _chunk(self, data: bytes):
        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    # -- 路由
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        Handler._ensure_cfg()
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", ""):
            return self._json(200, {"ok": True, "service": "cfai", "version": VERSION})
        if path in ("/admin", "/ui"):
            return self._html()
        if path.startswith("/admin/api/"):
            return self._admin_get(path)
        if self._client_key() is None:
            return self._json(401, _err_body("invalid api key", 401))
        if path == "/v1/models":
            if "refresh=1" in self.path:
                try:
                    refresh_catalog(Handler.cfg, quiet=True)
                except Exception:
                    pass
            return self._json(200, {"object": "list", "data": self._models()})
        if path == "/stats":
            with STATE_LOCK:
                st = Handler.state or load_state()
                return self._json(200, {"date": st["date"], "accounts": st["accounts"]})
        return self._json(404, _err_body("not found", 404))

    # -- 管理控制台
    def _html(self):
        p = os.path.join(os.path.dirname(os.path.realpath(__file__)), "admin.html")
        if not os.path.exists(p):
            return self._json(500, _err_body("admin.html 缺失", 500))
        with open(p, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(raw)

    def _admin_get(self, path: str):
        if not self._admin_ok():
            return self._json(401, _err_body("invalid api key", 401))
        cfg = load_config()
        with STATE_LOCK:
            st = Handler.state or load_state()
            if path == "/admin/api/snapshot":
                snap = admin_snapshot(cfg, st)
                proto = self.headers.get("X-Forwarded-Proto")
                if not proto:
                    proto = "https" if self.headers.get("CF-Ray") else "http"
                host = self.headers.get("Host") or "127.0.0.1:8787"
                snap["base_url"] = f"{proto}://{host}/v1"
                snap["models_public"] = [m["id"] for m in self._models()]
                return self._json(200, snap)
            if path == "/admin/api/session":
                left = session_left(self._bearer() or self.headers.get("X-Admin-Token", ""))
                if left <= 0:
                    return self._json(401, _err_body("会话已失效", 401))
                return self._json(200, {"ok": True, "expires_in": int(left)})
            if path == "/admin/api/models":
                cat = load_catalog()
                allow = allowed_models(cfg)
                allow_set = set(allow)
                aliases = cfg.get("aliases") or {}
                resolved_allow = {aliases.get(a, a) for a in allow}
                q = ""
                if "?" in self.path:
                    for kv in self.path.split("?", 1)[1].split("&"):
                        if kv.startswith("q="):
                            q = kv[2:].lower()
                rows = [{"id": mid, "task": e.get("task", ""), "context": e.get("context"),
                         "price_in": e.get("price_in"), "price_out": e.get("price_out"),
                         "price_cached": e.get("price_cached"),
                         "allowed": (not allow) or mid in allow_set or mid in resolved_allow,
                         "accounts": len(e.get("accounts") or [])}
                        for mid, e in (cat.get("models") or {}).items()
                        if not q or q in mid.lower() or q in (e.get("task") or "").lower()]
                rows.sort(key=lambda r: r["id"])
                return self._json(200, {"fetched_at": cat.get("fetched_at", 0),
                                        "allowed_models": allow, "models": rows})
        return self._json(404, _err_body("not found", 404))

    def _read_body(self):
        """必须在任何提前返回之前读完 body，否则 keep-alive 连接里残留的 body
        会被当成下一个请求的请求行，导致 501 Unsupported method。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        try:
            self._raw_body = self.rfile.read(n) if n > 0 else b""
        except Exception:
            self._raw_body = b""
        return self._raw_body

    def _admin_body(self):
        try:
            return json.loads(self._raw_body or b"{}")
        except json.JSONDecodeError:
            return None

    def _admin_post(self, path: str):
        from urllib.parse import unquote
        tail = [unquote(p) for p in path.split("/") if p][2:]
        d = self._admin_body()
        if d is None:
            return self._json(400, _err_body("invalid json", 400))

        # 登录不需要已有令牌
        if tail == ["login"]:
            cfg0 = load_config()
            stored = cfg0.get("admin_password") or ""
            pw = (d.get("password") or "").strip()
            ok_login = False
            has_pw = bool(stored)
            if stored:
                ok_login = (hash_pw(pw) == stored)
            elif Handler.api_key:
                ok_login = (pw == Handler.api_key)
            if not ok_login:
                msg = "密码错误" if stored else "密码错误（尚未设置控制台密码，可用 API Key 登录）"
                return self._json(401, _err_body(msg, 401))
            tok = new_session()
            return self._json(200, {
                "ok": True, "token": tok, "has_password": has_pw,
                "expires_in": int(SESSION_TTL),
                "expires_at": int(time.time() + SESSION_TTL),
            })

        if tail == ["logout"]:
            tok = self._bearer() or self.headers.get("X-Admin-Token", "")
            drop_session(tok)
            return self._json(200, {"ok": True})

        if tail == ["session"]:
            tok = self._bearer() or self.headers.get("X-Admin-Token", "")
            left = session_left(tok)
            if left <= 0:
                return self._json(401, _err_body("会话已失效", 401))
            return self._json(200, {"ok": True, "expires_in": int(left)})

        if not self._admin_ok():
            return self._json(401, _err_body("invalid api key", 401))
        cfg = load_config()
        with STATE_LOCK:
            st = Handler.state or load_state()

        if tail == ["accounts"]:
            code, res = admin_add_account(cfg, d)
            if code != 200:
                return self._json(code, _err_body(res, code))
            out = {"ok": True, "account": res}
            if d.get("verify"):
                model = d.get("model") or _pick_test_model(cfg)
                ok, ms, used, err = _test_one(cfg, res, model)
                with STATE_LOCK:
                    s = acc_state(st, res["name"])
                    s["checked_at"] = int(time.time())
                    s["check_ok"] = ok
                    if not ok:
                        s["last_error"] = err
                        if "401" in err or "403" in err:
                            s["auth_error"] = True
                    save_state(st)
                out["verify"] = {"ok": ok, "ms": ms, "model": used, "error": err}
            return self._json(200, out)

        if len(tail) == 3 and tail[0] == "accounts":
            name, action = tail[1], tail[2]
            a = get_account(cfg, name)
            if not a:
                return self._json(404, _err_body("账号不存在", 404))
            if action == "test":
                model = d.get("model") or _pick_test_model(cfg)
                ok, ms, used, err = _test_one(cfg, a, model)
                with STATE_LOCK:
                    s = acc_state(st, name)
                    s["checked_at"] = int(time.time())
                    s["check_ok"] = ok
                    if ok:
                        s["auth_error"] = False
                        s["cooldown_until"] = 0
                        s["fail_streak"] = 0
                        s["last_error"] = ""
                    else:
                        s["last_error"] = err
                        if "401" in err or "403" in err:
                            s["auth_error"] = True
                    save_state(st)
                return self._json(200, {"ok": ok, "ms": ms, "model": used, "error": err})
            if action == "reset":
                # 只清冷却/鉴权/错误标记，不动今天的用量统计
                with STATE_LOCK:
                    s = acc_state(st, name)
                    s["cooldown_until"] = 0
                    s["auth_error"] = False
                    s["fail_streak"] = 0
                    s["last_error"] = ""
                    save_state(st)
                return self._json(200, {"ok": True})
            if action == "calibrate":
                try:
                    v = float(d.get("neurons"))
                except (TypeError, ValueError):
                    return self._json(400, _err_body("neurons 必须是数字", 400))
                with STATE_LOCK:
                    acc_state(st, name)["neurons"] = round(max(0.0, v), 4)
                    save_state(st)
                return self._json(200, {"ok": True, "neurons": v})
            return self._json(404, _err_body("未知操作", 404))

        if tail == ["catalog", "refresh"]:
            cat = refresh_catalog(cfg)
            return self._json(200, {"ok": True, "count": len(cat.get("models") or {}),
                                    "fetched_at": cat.get("fetched_at")})

        if tail == ["models", "allow"]:
            ids = d.get("ids")
            if not isinstance(ids, list):
                return self._json(400, _err_body("ids 必须是数组", 400))
            aliases = cfg.get("aliases") or {}
            # 统一存真实模型 id，别名在这里就解析掉，避免别名被删后留下失效条目
            norm = sorted({aliases.get(str(x).strip(), str(x).strip())
                           for x in ids if str(x).strip()})
            cfg["allowed_models"] = norm
            save_config(cfg)
            return self._json(200, {"ok": True, "allowed_models": cfg["allowed_models"]})

        if tail == ["password"]:
            pw = (d.get("password") or "").strip()
            if len(pw) < 6:
                return self._json(400, _err_body("密码至少 6 位", 400))
            cfg["admin_password"] = hash_pw(pw)
            save_config(cfg)
            drop_all_sessions()          # 改密码 = 所有端下线，必须重新登录
            return self._json(200, {"ok": True, "relogin": True})

        if tail == ["keys"]:
            name = (d.get("name") or "").strip()
            if not name:
                return self._json(400, _err_body("名称不能为空", 400))
            if get_key(cfg, name):
                return self._json(409, _err_body("Key 名称已存在", 409))
            k = {"name": name, "key": (d.get("key") or "").strip() or new_key_string(),
                 "enabled": bool(d.get("enabled", True))}
            for f in ("daily_requests", "daily_neurons"):
                if d.get(f) not in (None, "", 0, "0"):
                    try:
                        k[f] = float(d[f]) if f == "daily_neurons" else int(d[f])
                    except (TypeError, ValueError):
                        return self._json(400, _err_body(f"{f} 必须是数字", 400))
            if d.get("note"):
                k["note"] = d["note"]
            cfg.setdefault("keys", []).append(k)
            save_config(cfg)
            return self._json(200, {"ok": True, "key": k})

        if tail == ["router"]:
            for k in ("strategy", "max_retries", "rpm", "cooldown_seconds",
                      "max_cooldown_seconds", "timeout", "default_model", "user_agent",
                      "default_daily_neurons", "health_check_minutes"):
                if k in d and d[k] not in ("", None):
                    try:
                        if k in ("cooldown_seconds", "max_cooldown_seconds", "timeout",
                                 "default_daily_neurons"):
                            cfg["router"][k] = float(d[k])
                        elif k in ("max_retries", "rpm", "health_check_minutes"):
                            cfg["router"][k] = int(d[k])
                        else:
                            cfg["router"][k] = str(d[k])
                    except (TypeError, ValueError):
                        return self._json(400, _err_body(f"{k} 非法", 400))
            save_config(cfg)
            return self._json(200, {"ok": True, "router": cfg["router"]})

        if tail == ["aliases"]:
            name = (d.get("name") or "").strip()
            target = (d.get("target") or "").strip()
            if not name or not target:
                return self._json(400, _err_body("需要 name 和 target", 400))
            cfg["aliases"][name] = target
            save_config(cfg)
            return self._json(200, {"ok": True, "aliases": cfg["aliases"]})

        return self._json(404, _err_body("not found", 404))

    def do_PATCH(self):
        Handler._ensure_cfg()
        self._read_body()
        from urllib.parse import unquote
        path = self.path.split("?")[0].rstrip("/")
        if not self._admin_ok():
            return self._json(401, _err_body("invalid api key", 401))
        tail = [unquote(p) for p in path.split("/") if p][2:]
        if len(tail) == 2 and tail[0] == "accounts":
            d = self._admin_body()
            if d is None:
                return self._json(400, _err_body("invalid json", 400))
            cfg = load_config()
            old = tail[1]
            code, res = admin_update_account(cfg, old, d)
            if code != 200:
                return self._json(code, _err_body(res, code))
            new = res.get("name") or old
            if new != old:
                with STATE_LOCK:
                    st = Handler.state or load_state()
                    if old in st.get("accounts", {}):
                        st["accounts"][new] = st["accounts"].pop(old)
                    if old in st.get("hourly", {}):
                        st["hourly"][new] = st["hourly"].pop(old)
                    save_state(st)
            return self._json(200, {"ok": True, "account": res})

        if len(tail) == 2 and tail[0] == "keys":
            d = self._admin_body()
            if d is None:
                return self._json(400, _err_body("invalid json", 400))
            cfg = load_config()
            k = get_key(cfg, tail[1])
            if not k:
                return self._json(404, _err_body("Key 不存在", 404))
            new_name = (d.get("name") or "").strip()
            if new_name and new_name != tail[1]:
                if get_key(cfg, new_name):
                    return self._json(409, _err_body(f"Key {new_name} 已存在", 409))
                with STATE_LOCK:
                    st = Handler.state or load_state()
                    ks = st.setdefault("keys", {})
                    if tail[1] in ks:
                        ks[new_name] = ks.pop(tail[1])
                    save_state(st)
                k["name"] = new_name
            if isinstance(d.get("key"), str) and d["key"].strip():
                k["key"] = d["key"].strip()
            if "enabled" in d:
                k["enabled"] = bool(d["enabled"])
            if isinstance(d.get("note"), str):
                k["note"] = d["note"]
            for f in ("daily_requests", "daily_neurons"):
                if f in d:
                    if d[f] in (None, "", 0, "0"):
                        k.pop(f, None)
                    else:
                        try:
                            k[f] = float(d[f]) if f == "daily_neurons" else int(d[f])
                        except (TypeError, ValueError):
                            return self._json(400, _err_body(f"{f} 必须是数字", 400))
            save_config(cfg)
            return self._json(200, {"ok": True, "key": k})
        return self._json(404, _err_body("not found", 404))

    def do_DELETE(self):
        Handler._ensure_cfg()
        self._read_body()
        from urllib.parse import unquote
        path = self.path.split("?")[0].rstrip("/")
        if not self._admin_ok():
            return self._json(401, _err_body("invalid api key", 401))
        tail = [unquote(p) for p in path.split("/") if p][2:]
        cfg = load_config()
        with STATE_LOCK:
            st = Handler.state or load_state()
            if len(tail) == 2 and tail[0] == "accounts":
                code, res = admin_delete_account(cfg, tail[1], st)
                if code != 200:
                    return self._json(code, _err_body(res, code))
                return self._json(200, res)
        if len(tail) == 2 and tail[0] == "aliases":
            if cfg["aliases"].pop(tail[1], None) is None:
                return self._json(404, _err_body("别名不存在", 404))
            save_config(cfg)
            return self._json(200, {"ok": True})
        if len(tail) == 2 and tail[0] == "keys":
            before = len(cfg.get("keys") or [])
            cfg["keys"] = [x for x in (cfg.get("keys") or []) if x.get("name") != tail[1]]
            if len(cfg["keys"]) == before:
                return self._json(404, _err_body("Key 不存在", 404))
            save_config(cfg)
            with STATE_LOCK:
                st.get("keys", {}).pop(tail[1], None)
                save_state(st)
            return self._json(200, {"ok": True})
        return self._json(404, _err_body("not found", 404))

    def _models(self):
        cfg = Handler.cfg
        allow = allowed_models(cfg)
        if allow:
            aliases = cfg.get("aliases") or {}
            cat = (load_catalog().get("models") or {})
            ids = {}
            alias_only = cfg["router"].get("alias_only", True)
            for m in allow:
                mid = aliases.get(m, m)
                if cat and mid not in cat:
                    continue          # 失效条目（别名已删/模型已下架）不暴露
                names = [n for n, t in aliases.items() if t == mid]
                if names and alias_only:
                    for n in names:
                        ids[n] = "cfai-alias"      # 有别名就只露别名，原始 id 不出现
                else:
                    ids[mid] = "cfai"
                    for n in names:
                        ids[n] = "cfai-alias"
            return [{"id": k, "object": "model", "owned_by": v} for k, v in sorted(ids.items())]
        ids = {}
        for k in (cfg.get("aliases") or []):
            ids[k] = "cfai-alias"
        cat = load_catalog()
        for mid, e in (cat.get("models") or {}).items():
            task = (e.get("task") or "").lower()
            if any(h in task for h in CHAT_TASKS):
                ids[mid] = "cloudflare"
        for a in cfg["accounts"]:
            for m in (a.get("models") or []):
                ids.setdefault(m, "cfai")
        return [{"id": k, "object": "model", "owned_by": v} for k, v in sorted(ids.items())]

    def _handle_decide(self, payload, key_name, st):
        """POST /v1/decide —— 决策模型（clef 系列）的 HTTP 入口。"""
        cfg = Handler.cfg
        if not isinstance(payload, dict):
            return self._json(400, _err_body("invalid json body", 400))
        model = str(payload.get("model") or "clef-flash")
        state = payload.get("state")
        questions = payload.get("questions")
        if state in (None, ""):
            return self._json(400, _err_body("state 必填（要判断的上下文，字符串）", 400))
        if not isinstance(questions, dict) or not questions:
            return self._json(400, _err_body("questions 必填，且必须是非空对象", 400))
        for qid, q in questions.items():
            if not isinstance(q, dict) or q.get("type") not in ("noul", "choice", "score"):
                return self._json(400, _err_body(
                    f"questions.{qid}.type 必须是 noul / choice / score", 400))

        if model.startswith("@"):
            if cfg["router"].get("alias_only", True) and alias_for(cfg, model):
                return self._json(403, _err_body("该模型已设置别名，请用别名调用", 403))
            model_id = model
        else:
            model_id = (cfg.get("aliases") or {}).get(model) or ("@cf/cloudflare/" + model)

        body = {"model": model_id.rsplit("/", 1)[-1], "state": state, "questions": questions}
        imgs = payload.get("images")
        if imgs:
            body["images"] = imgs if isinstance(imgs, list) else [imgs]

        status, data, acct = decide_call(cfg, st, model_id, body)
        record_key_usage(st, key_name, status == 200)   # 内部自带锁，不能再套 STATE_LOCK
        with STATE_LOCK:
            save_state(st)
        if status != 200:
            return self._json(status, data)
        if acct:
            self._extra_headers = {"X-Cfai-Account": acct}
        return self._json(200, data)

    def do_POST(self):
        Handler._ensure_cfg()
        self._read_body()
        path = self.path.split("?")[0].rstrip("/")
        if path.startswith("/admin/api/"):
            return self._admin_post(path)
        if path not in ("/v1/chat/completions", "/v1/decide"):
            return self._json(404, _err_body("not found", 404))
        key_name = self._client_key()
        if key_name is None:
            return self._json(401, _err_body("invalid api key", 401))

        try:
            payload = json.loads(self._raw_body or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, _err_body("invalid json body", 400))

        with STATE_LOCK:
            if Handler.state is None or Handler.state.get("date") != today():
                Handler.state = load_state()
            st = Handler.state
            over, why = key_limit_hit(Handler.cfg, st, key_name)
        if over:
            return self._json(429, _err_body(why, 429))

        if path == "/v1/decide":
            return self._handle_decide(payload, key_name, st)

        if not isinstance(payload, dict) or not payload.get("messages"):
            return self._json(400, _err_body("messages is required", 400))
        if "clef" in str(payload.get("model", "")).lower():
            return self._json(400, _err_body(
                "clef/clef-flash/clef-omni 是决策模型（System One API），需要 {state, questions}，"
                "不能走 /v1/chat/completions。请用 /v1/decide。", 400))

        want_stream = bool(payload.get("stream"))

        if want_stream:
            status, resp, acct = complete_stream(Handler.cfg, st, payload)
            if status != 200:
                record_key_usage(st, key_name, False)
                with STATE_LOCK:
                    save_state(st)
                return self._json(status, resp)
            record_key_usage(st, key_name, True, _neurons_header(resp) or 0)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Cfai-Account", acct or "")
            self._cors()
            self.end_headers()
            try:
                for line in resp:
                    if not line:
                        continue
                    self._chunk(line if isinstance(line, bytes) else line.encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
                with STATE_LOCK:
                    save_state(st)
            return

        status, data, acct = complete(Handler.cfg, st, payload)
        if status == 200:
            tin, tout = _usage_tokens(data)
            record_key_usage(st, key_name, True, _data_neurons(data), tin, tout)
        else:
            record_key_usage(st, key_name, False)
        with STATE_LOCK:
            save_state(st)
        self.send_response(status)
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if acct:
            self.send_header("X-Cfai-Account", acct)
        self._cors()
        self.end_headers()
        self.wfile.write(raw)


def cmd_serve(args) -> int:
    cfg = load_config()
    if not cfg["accounts"]:
        print("还没有账号，先 `cfai account add ...`", file=sys.stderr)
        return 1
    Handler.cfg = cfg
    Handler.api_key = (args.api_key or os.environ.get("CFAI_KEY")
                       or os.environ.get("CFAI_API_KEY") or "")
    Handler.state = load_state()

    def _warm():
        try:
            cat = ensure_catalog_fresh(cfg, args.models_ttl)
            print(f"模型目录: {len(cat.get('models') or {})} 个模型（TTL {args.models_ttl}h）", file=sys.stderr)
        except Exception as e:
            print(f"模型目录刷新失败: {str(e)[:120]}", file=sys.stderr)

    threading.Thread(target=_warm, daemon=True).start()

    def _health_loop():
        interval = float(cfg["router"].get("health_check_minutes") or 0)
        if interval <= 0:
            return
        time.sleep(20)                       # 启动后先等一会，别和目录刷新抢
        while True:
            try:
                r = _health_check_all(load_config())
                print(f"体检完成: {r['ok']}/{r['checked']} 正常（模型 {r['model']}）", file=sys.stderr)
            except Exception as e:
                print(f"体检失败: {str(e)[:120]}", file=sys.stderr)
            time.sleep(interval * 60)

    threading.Thread(target=_health_loop, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    scheme = "http"
    if args.tls_cert and args.tls_key:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.tls_cert, args.tls_key)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        scheme = "https"
    print(f"cfai 网关已启动: {scheme}://{args.host}:{args.port}/v1", file=sys.stderr)
    print(f"账号 {len(cfg['accounts'])} 个 · 策略 {cfg['router'].get('strategy')}"
          + (" · 需要 API Key" if Handler.api_key else " · 无鉴权（仅本机建议）"), file=sys.stderr)
    print(f"OpenAI Base URL: {scheme}://{args.host}:{args.port}/v1", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
    finally:
        srv.server_close()
    return 0


# --------------------------------------------------------------------------- init

SAMPLE = {
    # 留空：用 `cfai account add ...` 添加。占位账号会让每条命令都报错，所以不放示例数据。
    "accounts": [],
    "aliases": {},
    "allowed_models": [],
    "keys": [],
    "router": {
        "strategy": "round_robin",
        "max_retries": 3,
        "cooldown_seconds": 60,
        "max_cooldown_seconds": 900,
        "timeout": 120,
        "default_model": "",
        "user_agent": "cfai/1.1",
        "health_check_minutes": 60,
        "verbose": False
    }
}


def cmd_passwd(args) -> int:
    """设置或清除控制台密码。忘了密码就用这个重置。"""
    cfg = load_config()
    pw = args.password
    if pw is None:
        import getpass
        pw = getpass.getpass("新密码（留空=清除密码）: ")
        if pw:
            pw2 = getpass.getpass("再输一次: ")
            if pw != pw2:
                print("两次输入不一致", file=sys.stderr)
                return 1
    if pw and len(pw) < 6:
        print("密码至少 6 位", file=sys.stderr)
        return 1
    cfg["admin_password"] = hash_pw(pw) if pw else ""
    save_config(cfg)
    print("控制台密码已更新" if pw else "控制台密码已清除（改用 API Key 登录）")
    return 0


def cmd_init(args) -> int:
    ensure_home()
    p = config_path()
    if os.path.exists(p) and not args.force:
        print(f"配置已存在: {p}（加 --force 覆盖）", file=sys.stderr)
        return 1
    with open(p, "w", encoding="utf-8") as f:
        json.dump(SAMPLE, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    print(f"已写入示例配置: {p}")
    print("\n下一步 —— 添加一个账号：")
    print("  cfai account add cf-01 --type cloudflare \\")
    print("    --account-id <32位account_id> --token <cfut_...>")
    print("  cfai account add worker-1 --type openai \\")
    print("    --base-url https://xxx.workers.dev/v1 --api-key sk-xxx")
    print("  cfai account test")
    return 0


# --------------------------------------------------------------------------- main

def build_parser():
    p = argparse.ArgumentParser(prog="cfai", description="多 Cloudflare Workers AI 账号统一调度 CLI / OpenAI 兼容网关")
    p.add_argument("-V", "--version", action="version", version=f"cfai {VERSION}")
    sub = p.add_subparsers(dest="cmd")

    ap = sub.add_parser("account", help="账号管理")
    asp = ap.add_subparsers(dest="action")

    a_add = asp.add_parser("add", help="添加账号")
    a_add.add_argument("name")
    a_add.add_argument("--type", choices=["cloudflare", "openai"], default="cloudflare")
    a_add.add_argument("--account-id")
    a_add.add_argument("--token")
    a_add.add_argument("--base-url")
    a_add.add_argument("--api-key")
    a_add.add_argument("--models", help="逗号分隔的模型白名单/通配符，留空=全部")
    a_add.add_argument("--model-map", help="JSON，账号级模型名映射")
    a_add.add_argument("--daily-requests", type=int, help="该账号每日请求上限（软配额）")
    a_add.add_argument("--daily-neurons", type=float, help="该账号每日神经元上限（CF 免费额度约 10000）")
    a_add.add_argument("--note")
    a_add.add_argument("--test", action="store_true", help="添加后立即测试")
    a_add.add_argument("--model", help="--test 用的模型")
    a_add.set_defaults(func=cmd_account_add)

    a_ls = asp.add_parser("list", help="列出账号")
    a_ls.set_defaults(func=cmd_account_list)
    a_ls.add_argument("--json", action="store_true")

    a_rm = asp.add_parser("rm", help="删除账号")
    a_rm.add_argument("name")
    a_rm.set_defaults(func=cmd_account_rm)

    a_t = asp.add_parser("test", help="测试账号连通性")
    a_t.add_argument("name", nargs="?")
    a_t.add_argument("--model")
    a_t.add_argument("--json", action="store_true")
    a_t.set_defaults(func=cmd_account_test)

    a_s = asp.add_parser("show", help="查看账号详情（脱敏）")
    a_s.add_argument("name")
    a_s.set_defaults(func=cmd_account_show)

    a_r = asp.add_parser("reset", help="清除冷却/鉴权失败标记")
    a_r.add_argument("name", nargs="?")
    a_r.set_defaults(func=cmd_account_reset)

    c = sub.add_parser("chat", help="发一次对话（不传 prompt 或传 - 进入交互模式）")
    c.add_argument("prompt", nargs="?")
    c.add_argument("-m", "--model")
    c.add_argument("-s", "--system")
    c.add_argument("--max-tokens", type=int)
    c.add_argument("-t", "--temperature", type=float)
    c.add_argument("--stream", action="store_true")
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_chat)

    s = sub.add_parser("serve", help="启动 OpenAI 兼容网关")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--api-key", help="本地网关鉴权 key（可选）")
    s.add_argument("--models-ttl", type=float, default=24.0,
                   help="模型目录缓存有效期（小时），启动时过期自动刷新")
    s.add_argument("--tls-cert", help="TLS 证书（配合 --tls-key，用 CF Origin Certificate 即可）")
    s.add_argument("--tls-key", help="TLS 私钥")
    s.set_defaults(func=cmd_serve)

    d = sub.add_parser("decide", help="跑决策模型（clef / clef-flash / clef-omni，原生 run 端点）")
    d.add_argument("-m", "--model", default="clef-flash",
                   help="clef-flash | clef-omni | clef | 完整 @cf/... 名")
    d.add_argument("--state", help="要评估的内容（字符串，- 表示从 stdin 读）")
    d.add_argument("--state-file", help="从文件读 state")
    d.add_argument("--questions", help="问题定义 JSON 字符串")
    d.add_argument("--questions-file", help="问题定义 JSON 文件")
    d.add_argument("--image", action="append", help="附加图片（可重复，PNG/JPEG/WebP）")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_decide)

    m = sub.add_parser("models", help="查看模型")
    m.add_argument("--remote", action="store_true", help="从各账号实时拉取")
    m.add_argument("--task", help="按任务类型过滤，如 text generation")
    m.add_argument("--allow", help="设置对外白名单（逗号分隔；none 表示清空）")
    m.set_defaults(func=cmd_models)

    k = sub.add_parser("key", help="客户端 Key 管理")
    k.add_argument("action", choices=["list", "add", "rm", "enable", "disable"],
                   nargs="?", default="list")
    k.add_argument("name", nargs="?", help="Key 名称")
    k.add_argument("--key", help="指定 Key（默认自动生成 sk-+40 位十六进制）")
    k.add_argument("--daily-requests", type=int, default=0, help="每日请求上限，0=不限")
    k.add_argument("--daily-neurons", type=float, default=0, help="每日神经元上限，0=不限")
    k.add_argument("--note", help="备注")
    k.set_defaults(func=cmd_key)

    al = sub.add_parser("alias", help="模型别名")
    al.add_argument("action", choices=["set", "rm", "list"], nargs="?", default="list")
    al.add_argument("name", nargs="?")
    al.add_argument("target", nargs="?")
    al.set_defaults(func=cmd_alias)

    stt = sub.add_parser("stats", help="用量统计")
    stt.add_argument("--reset", action="store_true")
    stt.add_argument("--json", action="store_true")
    stt.set_defaults(func=cmd_stats)

    d = sub.add_parser("doctor", help="环境自检")
    d.set_defaults(func=cmd_doctor)

    i = sub.add_parser("init", help="写入示例配置")
    i.add_argument("--force", action="store_true")
    i.set_defaults(func=cmd_init)

    pw = sub.add_parser("passwd", help="设置/清除控制台密码（忘记密码时用）")
    pw.add_argument("password", nargs="?")
    pw.set_defaults(func=cmd_passwd)

    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
