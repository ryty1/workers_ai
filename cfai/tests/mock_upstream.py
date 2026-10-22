#!/usr/bin/env python3
"""模拟 OpenAI 兼容上游，用于验证 cfai 的轮询/故障转移/流式转发。

用法: python mock_upstream.py <port> <mode>
mode: ok | 429 | 500 | auth | flaky（前 2 次 500，之后 ok）
GET /__stats 返回 {"hits": N}
"""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_B64_IMG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="

PORT = int(sys.argv[1])
MODE = sys.argv[2]
HITS = {"n": 0, "ua": ""}
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_raw(self, data: bytes, ctype: str, code: int = 200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/__ua"):
            raw = json.dumps({"ua": HITS["ua"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/__stats"):
            raw = json.dumps({"hits": HITS["n"], "mode": MODE}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.path.startswith("/v1/models"):
            raw = json.dumps({"object": "list", "data": [{"id": "@cf/meta/llama-3.1-8b-instruct"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            payload = {}          # multipart 之类解析不了，留给下面的分支按原始字节处理
        with LOCK:
            HITS["n"] += 1
            hits = HITS["n"]
            HITS["ua"] = self.headers.get("User-Agent") or ""

        def fail(code, msg):
            raw = json.dumps({"error": {"message": msg, "code": code}}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if code == 429:
                self.send_header("Retry-After", "1")
            self.end_headers()
            self.wfile.write(raw)

        if MODE == "429":
            return fail(429, "rate limited")
        if MODE == "quota":
            return fail(429, "AiError: you have used up your daily free allocation "
                              "of 10,000 neurons. Please upgrade to Workers Paid.")
        if MODE == "500":
            return fail(500, "boom")
        if MODE == "auth":
            return fail(401, "bad token")
        if MODE == "flaky" and hits <= 2:
            return fail(500, "flaky boom")

        model = payload.get("model", "?")
        text = f"pong-from-{PORT}"

        if self.path.startswith("/ai/run/"):
            mid = self.path.rsplit("/", 1)[-1]
            ctype = (self.headers.get("Content-Type") or "")
            ok = {"success": True, "errors": [], "messages": []}
            if "multipart/form-data" in ctype:
                if b'name="audio"' in body:                 # 语音转写
                    return self._send({"result": {"text": "mock transcript"}, **ok})
                return self._send({"result": {"image": _B64_IMG}, **ok})
            txt = payload.get("text")
            if isinstance(txt, list):                       # 向量
                return self._send({"result": {"data": [[0.1, 0.2, 0.3] for _ in txt],
                                               "shape": [len(txt), 3]}, **ok})
            if isinstance(txt, str):                        # 语音合成 → 二进制
                return self._send_raw(b"ID3\x03\x00\x00\x00mock-mp3", "audio/mpeg")
            if payload.get("prompt") and "image" in payload:   # 视觉问答
                return self._send({"result": {"description": "mock vision answer"}, **ok})
            if payload.get("prompt"):                       # 文生图
                return self._send({"result": {"image": _B64_IMG}, **ok})
            answers = {qid: {"type": "noul", "noul": 0.9}
                       for qid in (payload.get("questions") or {})}
            raw = json.dumps({
                "result": {"model": mid, "answers": answers,
                           "usage": {"input_tokens": 11, "output_tokens": 0}},
                "success": True, "errors": [], "messages": [],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in [text[:6], text[6:]]:
                chunk = {"id": "x", "object": "chat.completion.chunk", "model": model,
                         "choices": [{"index": 0, "delta": {"content": piece}}]}
                data = ("data: " + json.dumps(chunk) + "\n\n").encode()
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()
            done = b"data: [DONE]\n\n"
            self.wfile.write(b"%x\r\n" % len(done) + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        raw = json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    srv.daemon_threads = True
    srv.serve_forever()
