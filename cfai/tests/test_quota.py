#!/usr/bin/env python3
"""额度冻结验证：429 额度用尽 → 冻结 → 跳过 → UTC 零点后自动恢复。

跑法: python3 tests/test_quota.py
"""
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
HOME = "/tmp/cfai-quota-home"
MOCK_PORT = 9111
GW = 8798

fails = []


def ok(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def wait_port(port, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def cli(*args, check=0):
    env = dict(os.environ, CFAI_HOME=HOME)
    r = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True,
                       env=env, timeout=60)
    if check is not None and r.returncode != check:
        print(f"    [debug] {' '.join(args)} -> {r.returncode}\n    {r.stdout}\n    {r.stderr}")
    return r


def http(url, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    shutil.rmtree(HOME, ignore_errors=True)
    os.makedirs(HOME, exist_ok=True)
    procs = []
    try:
        procs.append(subprocess.Popen([sys.executable, MOCK, str(MOCK_PORT), "quota"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        wait_port(MOCK_PORT)
        cli("init")
        cli("account", "add", "quota-1", "--type", "openai",
            "--base-url", f"http://127.0.0.1:{MOCK_PORT}/v1", "--api-key", "sk-test")
        cfg_path = os.path.join(HOME, "config.json")
        cfg = json.load(open(cfg_path))
        cfg["accounts"][0]["daily_neurons"] = 10000
        cfg["allowed_models"] = ["@cf/meta/llama-3.3-70b-instruct-fp8-fast"]
        cfg["router"]["max_retries"] = 1
        json.dump(cfg, open(cfg_path, "w"), ensure_ascii=False, indent=2)

        procs.append(subprocess.Popen([sys.executable, CLI, "serve", "--port", str(GW),
                                       "--api-key", "sk-test"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      env=dict(os.environ, CFAI_HOME=HOME)))
        wait_port(GW)

        body = {"model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
                "messages": [{"role": "user", "content": "hi"}]}
        h = {"Authorization": "Bearer sk-test"}

        code, out = http(f"http://127.0.0.1:{GW}/v1/chat/completions", body, h)
        ok("额度用尽请求失败", code in (429, 502), out[:90])

        st = json.load(open(os.path.join(HOME, "state.json")))
        s = st["accounts"]["quota-1"]
        ok("账号被冻结", s.get("quota_until", 0) > time.time(),
           f"quota_until={s.get('quota_until')}")
        ok("冻结原因是额度而非限流", "daily free allocation" in (s.get("quota_reason") or ""),
           (s.get("quota_reason") or "")[:60])

        snap = json.loads(http(f"http://127.0.0.1:{GW}/admin/api/snapshot", None, h)[1])
        a = snap["accounts"][0]
        ok("状态显示为 quota", a["status"] == "quota", a["status"])
        ok("带恢复倒计时", a["quota_left"] > 0, f"{a['quota_left']}s")

        code, out = http(f"http://127.0.0.1:{GW}/v1/chat/completions", body, h)
        ok("冻结后跳过该账号（无候选 503）", code == 503, out[:90])

        # 手动解冻（等同控制台点「重置」，也等于 UTC 零点自动恢复走的那条路）
        code, out = http(f"http://127.0.0.1:{GW}/admin/api/accounts/quota-1/reset", {}, h)
        ok("重置接口解冻成功", code == 200, out[:60])

        code, out = http(f"http://127.0.0.1:{GW}/v1/chat/completions", body, h)
        ok("重置后自动恢复并重新可用", code in (429, 502) and "daily free" in out, out[:90])

        st = json.load(open(os.path.join(HOME, "state.json")))
        s = st["accounts"]["quota-1"]
        ok("恢复时清空额度计数", float(s.get("neurons", 0)) < 10000,
           f"neurons={s.get('neurons')}")
        ok("再次命中额度后重新冻结", s.get("quota_until", 0) > time.time())

        # 神经元达到上限也直接冻结（纯函数验证，不依赖上游返回什么）
        import importlib.util
        spec = importlib.util.spec_from_file_location("cfai_mod", CLI)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cfg2 = json.load(open(os.path.join(HOME, "config.json")))
        st2 = {"date": mod.today(), "rr_cursor": 0, "accounts": {}}
        s2 = mod.acc_state(st2, "quota-1")
        s2["neurons"] = 10000
        cands = mod.order_candidates(cfg2, st2, "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        ok("神经元达上限即跳过", len(cands) == 0, f"{len(cands)} 个候选")
        ok("并被标记冻结", st2["accounts"]["quota-1"].get("quota_until", 0) > time.time())
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()

    print()
    if fails:
        print(f"FAILED ({len(fails)}): " + ", ".join(fails))
        return 1
    print("ALL QUOTA TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
