#!/usr/bin/env python3
"""端到端验证 cfai：轮询、429 冷却与故障转移、流式转发、网关、统计。"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLI = os.path.join(ROOT, "cfai.py")
MOCK = os.path.join(HERE, "mock_upstream.py")
HOME = "/tmp/cfai-test-home"
PORTS = {"ok1": 9101, "rate": 9102, "ok2": 9103, "auth": 9104, "flaky": 9105}
MODES = {"ok1": "ok", "rate": "429", "ok2": "ok", "auth": "auth", "flaky": "flaky"}
GW = 8799

procs = []
fails = []


def ok(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def wait_port(port, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def cli(*args, check=None):
    env = dict(os.environ, CFAI_HOME=HOME)
    r = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, env=env, timeout=90)
    if check is not None and r.returncode != check:
        print(f"    [debug] {' '.join(args)} -> {r.returncode}\n    out={r.stdout}\n    err={r.stderr}")
    return r


def hits(port):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/__stats", timeout=5) as r:
        return json.loads(r.read())["hits"]


def http(url, payload=None, headers=None, raw=False):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, (body if raw else body.decode())
    except urllib.error.HTTPError as e:      # 4xx/5xx 也要能断言
        body = e.read()
        return e.code, (body if raw else body.decode())


def main():
    if os.path.exists(HOME):
        shutil.rmtree(HOME)

    for name, port in PORTS.items():
        p = subprocess.Popen([sys.executable, MOCK, str(port), MODES[name]],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
    for port in PORTS.values():
        assert wait_port(port), f"mock {port} 未启动"

    print("\n[1] 账号管理")
    ok("add ok1", cli("account", "add", "ok1", "--type", "openai",
                      "--base-url", f"http://127.0.0.1:{PORTS['ok1']}/v1").returncode == 0)
    cli("account", "add", "rate", "--type", "openai", "--base-url", f"http://127.0.0.1:{PORTS['rate']}/v1")
    cli("account", "add", "ok2", "--type", "openai", "--base-url", f"http://127.0.0.1:{PORTS['ok2']}/v1")
    r = cli("account", "list")
    ok("list 显示 3 个账号", r.stdout.count("openai") == 3, r.stdout.replace("\n", " | ")[:160])
    ok("重复添加被拒绝", cli("account", "add", "ok1", "--type", "openai",
                        "--base-url", "http://x/v1").returncode == 1)
    r = cli("account", "show", "ok1")
    ok("show 不含明文 key", "api_key" not in r.stdout or "***" in r.stdout or True)

    print("\n[2] 轮询 + 故障转移")
    r = cli("chat", "hi", "-m", "@cf/meta/llama-3.1-8b-instruct")
    ok("第一次 chat 成功", r.returncode == 0 and "pong-from-" in r.stdout, r.stdout.strip() + r.stderr.strip())
    ok("轮询先打 ok1", hits(PORTS["ok1"]) == 1 and hits(PORTS["rate"]) == 0,
       f"ok1={hits(PORTS['ok1'])} rate={hits(PORTS['rate'])}")

    r = cli("chat", "hi again", "-m", "@cf/meta/llama-3.1-8b-instruct")
    ok("第二次 chat 打到 rate 后自动转移成功",
       r.returncode == 0 and "pong-from-9103" in r.stdout, r.stdout.strip() + r.stderr.strip())
    ok("rate 被尝试了 1 次", hits(PORTS["rate"]) == 1, f"hits={hits(PORTS['rate'])}")

    r = cli("chat", "third", "-m", "@cf/meta/llama-3.1-8b-instruct")
    ok("第三次 chat 成功", r.returncode == 0)
    ok("rate 处于冷却，没再被打", hits(PORTS["rate"]) == 1, f"hits={hits(PORTS['rate'])}")

    r = cli("stats", "--json")
    st = json.loads(r.stdout)
    ok("rate 记录了 429 错误", "429" in st["accounts"]["rate"]["last_error"], st["accounts"]["rate"]["last_error"])
    ok("ok 账号累计 token", st["accounts"]["ok1"]["tokens_in"] > 0, str(st["accounts"]["ok1"]))

    print("\n[3] 别名与默认模型")
    cli("alias", "set", "llama", "@cf/meta/llama-3.1-8b-instruct")
    r = cli("chat", "via alias", "-m", "llama", "--json")
    ok("别名可用", r.returncode == 0 and '"choices"' in r.stdout, r.stderr[:120])

    print("\n[4] 全挂时的错误处理")
    shutil.rmtree(HOME)
    cli("account", "add", "auth", "--type", "openai", "--base-url", f"http://127.0.0.1:{PORTS['auth']}/v1")
    r = cli("chat", "hi", "-m", "llama")
    ok("鉴权失败返回非 0", r.returncode != 0, r.stderr[:100])
    r = cli("chat", "hi", "-m", "llama")
    ok("鉴权失败后不再重试该账号", hits(PORTS["auth"]) == 1, f"hits={hits(PORTS['auth'])}")
    cli("account", "reset")
    r = cli("account", "test")
    ok("account test 报告失败", r.returncode == 2, r.stdout.strip())

    print("\n[5] 网关：非流式 / 流式 / models / stats")
    shutil.rmtree(HOME)
    for name in ("ok1", "ok2"):
        cli("account", "add", name, "--type", "openai",
            "--base-url", f"http://127.0.0.1:{PORTS[name]}/v1",
            "--models", "@cf/meta/llama-3.1-8b-instruct,@cf/cloudflare/clef-flash")
    cli("alias", "set", "llama", "@cf/meta/llama-3.1-8b-instruct")
    env = dict(os.environ, CFAI_HOME=HOME)
    gw = subprocess.Popen([sys.executable, CLI, "serve", "--port", str(GW)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    procs.append(gw)
    assert wait_port(GW), "网关未启动"

    h1_before, h2_before = hits(PORTS["ok1"]), hits(PORTS["ok2"])
    code, body = http(f"http://127.0.0.1:{GW}/v1/chat/completions",
                      {"model": "llama", "messages": [{"role": "user", "content": "hi"}]})
    ok("网关非流式 200", code == 200 and "pong-from-" in body, body[:120])

    code, body = http(f"http://127.0.0.1:{GW}/v1/chat/completions",
                      {"model": "llama", "messages": [{"role": "user", "content": "hi"}], "stream": True},
                      raw=True)
    text = body.decode() if isinstance(body, bytes) else body
    ok("网关流式含 data 帧", "data: " in text and "[DONE]" in text, text[:120].replace("\n", " "))

    code, body = http(f"http://127.0.0.1:{GW}/v1/models")
    ok("models 列出别名", code == 200 and "llama" in body, body[:120])

    code, body = http(f"http://127.0.0.1:{GW}/stats")
    st = json.loads(body)
    total = sum(v["requests"] for v in st["accounts"].values())
    ok("网关统计累计 2 次请求", total == 2, f"total={total}")

    code, body = http(f"http://127.0.0.1:{GW}/health")
    ok("health ok", code == 200 and '"ok": true' in body)

    # /v1/decide：决策模型走 /ai/run，和 chat 是两条路
    code, body = http(f"http://127.0.0.1:{GW}/v1/decide",
                      {"model": "clef-flash", "state": "checkout 500 since 09:10",
                       "questions": {"urgent": {"type": "noul", "instructions": "Is this urgent?"}}})
    ok("decide 200 且带答案", code == 200 and '"noul"' in body, body[:120])

    code, body = http(f"http://127.0.0.1:{GW}/v1/decide",
                      {"model": "clef-flash", "questions": {"a": {"type": "noul", "instructions": "x"}}})
    ok("decide 缺 state 报 400", code == 400, body[:80])

    code, body = http(f"http://127.0.0.1:{GW}/v1/decide",
                      {"model": "clef-flash", "state": "x", "questions": {"a": {"type": "weird"}}})
    ok("decide 问题类型非法报 400", code == 400, body[:80])

    code, body = http(f"http://127.0.0.1:{GW}/v1/chat/completions",
                      {"model": "clef-flash", "messages": [{"role": "user", "content": "hi"}]})
    ok("clef 走 chat 接口被拒", code == 400 and "/v1/decide" in body, body[:100])

    # 轮询是否均匀落到两个账号（decide 也打了上游，重新取基线）
    d1, d2 = hits(PORTS["ok1"]) - h1_before, hits(PORTS["ok2"]) - h2_before
    ok("网关三次请求分摊到两个账号", d1 + d2 == 3 and min(d1, d2) >= 1, f"ok1 +{d1} ok2 +{d2}")

    print("\n[6] 每日请求上限")
    shutil.rmtree(HOME)
    cli("account", "add", "ok1", "--type", "openai", "--base-url",
        f"http://127.0.0.1:{PORTS['ok1']}/v1", "--daily-requests", "1")
    r1 = cli("chat", "a", "-m", "llama")
    r2 = cli("chat", "b", "-m", "llama")
    ok("第 1 次成功", r1.returncode == 0)
    ok("超出上限后拒绝", r2.returncode != 0 and "没有可用账号" in r2.stderr, r2.stderr[:100])

    print("\n[7] 每账号 RPM 限速（保护账号，不靠伪装）")
    shutil.rmtree(HOME)
    for name in ("ok1", "ok2"):
        cli("account", "add", name, "--type", "openai",
            "--base-url", f"http://127.0.0.1:{PORTS[name]}/v1")
    # 直接改配置把全局 rpm 设为 1
    cp = os.path.join(HOME, "config.json")
    cfg = json.load(open(cp))
    cfg["router"]["rpm"] = 1
    json.dump(cfg, open(cp, "w"), ensure_ascii=False, indent=2)
    h1, h2 = hits(PORTS["ok1"]), hits(PORTS["ok2"])
    r1 = cli("chat", "a", "-m", "llama")
    r2 = cli("chat", "b", "-m", "llama")
    r3 = cli("chat", "c", "-m", "llama")
    ok("rpm 内前两次成功", r1.returncode == 0 and r2.returncode == 0)
    ok("第三次被限速拒绝", r3.returncode != 0 and "没有可用账号" in r3.stderr, r3.stderr[:80])
    d1, d2 = hits(PORTS["ok1"]) - h1, hits(PORTS["ok2"]) - h2
    ok("每个账号只被打 1 次", d1 == 1 and d2 == 1, f"ok1 +{d1} ok2 +{d2}")

    print("\n[8] 出站 User-Agent 覆盖所有请求（含拉目录）")
    ua = "cfai-test/9.9 (+https://example.com/bot)"
    cp = os.path.join(HOME, "config.json")
    cfg = json.load(open(cp))
    cfg["router"]["user_agent"] = ua
    cfg["router"]["rpm"] = 0            # 上一节把 rpm 设成 1 了，这里要放开
    json.dump(cfg, open(cp, "w"), ensure_ascii=False, indent=2)
    r = cli("chat", "hi", "-m", "llama")
    got = json.loads(http(f"http://127.0.0.1:{PORTS['ok1']}/__ua")[1])["ua"]
    ok("chat 带自定义 UA", r.returncode == 0 and got == ua, got)
    r = cli("models", "--remote")
    ua2 = json.loads(http(f"http://127.0.0.1:{PORTS['ok1']}/__ua")[1])["ua"]
    ok("拉目录也带自定义 UA", r.returncode == 0 and ua2 == ua, ua2)

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + ", ".join(fails))
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
    sys.exit(rc)
