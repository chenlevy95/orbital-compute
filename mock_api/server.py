"""Mock Orbital Compute Billing API. Standard library only.

    python mock_api/server.py --as-of 2026-09-20 [--host 127.0.0.1] [--port 8765]

--as-of sets the server's "now": only data published on or before that date is visible.
Each flag can also be set with an environment variable (AS_OF, HOST, PORT), which is how the
container image and the Kubernetes manifest configure it.
Treat this server as a black box that behaves like the real third-party API described
in the README. Do not modify it.
"""

import argparse
import base64
import gzip
import json
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CLIENT_ID = "candidate"
CLIENT_SECRET = "orbital-s3cret"
TOKEN_TTL_SECONDS = 30
MAX_RANGE_DAYS = 31
MAX_PAGE_SIZE = 200
LATENCY_SECONDS = 0.4


def load_visible(as_of: date):
    cutoff = datetime.combine(as_of, datetime.max.time())
    latest = {}
    with gzip.open(Path(__file__).with_name("usage_versions.jsonl.gz"), "rt", encoding="utf-8") as f:
        for line in f:
            v = json.loads(line)
            if datetime.strptime(v["updated_at"], "%Y-%m-%dT%H:%M:%SZ") > cutoff:
                continue
            prev = latest.get(v["record_id"])
            if prev is None or v["updated_at"] > prev["updated_at"]:
                latest[v["record_id"]] = v
    return sorted(latest.values(), key=lambda v: (v["usage_date"], v["record_id"]))


class State:
    def __init__(self, records):
        self.records = records
        self.tokens = {}
        self.counter = 0
        self.lock = threading.Lock()

    def tick(self):
        with self.lock:
            self.counter += 1
            return self.counter


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"{self.address_string()} {fmt % args}")

        def _send(self, status, body, headers=None):
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)

        def _chaos(self):
            n = state.tick()
            if n % 7 == 0:
                self._send(429, {"error": "rate_limited"}, {"Retry-After": "3"})
                return True
            if n % 19 == 0:
                self._send(503, {"error": "upstream_unavailable"})
                return True
            return False

        def do_POST(self):
            if urlparse(self.path).path != "/oauth/token":
                return self._send(404, {"error": "not_found"})
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            try:
                body = json.loads(raw) if raw.strip().startswith("{") else \
                    {k: v[0] for k, v in parse_qs(raw).items()}
            except ValueError:
                return self._send(400, {"error": "invalid_request"})
            if body.get("client_id") != CLIENT_ID or body.get("client_secret") != CLIENT_SECRET:
                return self._send(401, {"error": "invalid_client"})
            token = secrets.token_hex(16)
            state.tokens[token] = time.time() + TOKEN_TTL_SECONDS
            self._send(200, {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL_SECONDS})

        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/v1/usage":
                return self._send(404, {"error": "not_found"})
            auth = self.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ").strip()
            if state.tokens.get(token, 0) < time.time():
                return self._send(401, {"error": "invalid_or_expired_token"})
            time.sleep(LATENCY_SECONDS)
            if self._chaos():
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                start = date.fromisoformat(q["start_date"])
                end = date.fromisoformat(q["end_date"])
                page_size = min(int(q.get("page_size", 100)), MAX_PAGE_SIZE)
                offset = int(base64.urlsafe_b64decode(q["page_token"]).decode()) if q.get("page_token") else 0
            except (KeyError, ValueError):
                return self._send(400, {"error": "start_date and end_date (YYYY-MM-DD) are required"})
            if end < start or (end - start) > timedelta(days=MAX_RANGE_DAYS - 1):
                return self._send(400, {"error": f"date range must be 1..{MAX_RANGE_DAYS} days"})
            rows = [r for r in state.records if start.isoformat() <= r["usage_date"] <= end.isoformat()]
            page = rows[offset:offset + page_size]
            nxt = offset + page_size
            token_out = base64.urlsafe_b64encode(str(nxt).encode()).decode() if nxt < len(rows) else None
            self._send(200, {"data": page, "next_page_token": token_out})

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", type=date.fromisoformat, default=os.environ.get("AS_OF"))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    args = ap.parse_args()
    if args.as_of is None:
        ap.error("--as-of (or the AS_OF environment variable) is required")
    state = State(load_visible(args.as_of))
    print(f"as-of {args.as_of}: {len(state.records)} records visible; listening on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), make_handler(state)).serve_forever()


if __name__ == "__main__":
    main()
