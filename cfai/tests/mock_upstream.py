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

PORT = int(sys.argv[1])
MODE = sys.argv[2]
HITS = {"n": 0, "ua": ""}
LOCK = threading.Lock()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

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
        payload = json.loads(self.rfile.read(n) or b"{}")
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
