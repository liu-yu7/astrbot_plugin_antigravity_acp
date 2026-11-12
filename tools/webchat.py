#!/usr/bin/env python3
"""Minimal AstrBot WebChat client: log in, send one message, print the reply stream.

    webchat.py --password <dashboard password> "your message" [--session ID] [--raw]

It talks to the dashboard's own chat API, so the message travels through the
real AstrBot pipeline (commands, agent runner, provider).
"""

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
import uuid

ap = argparse.ArgumentParser()
ap.add_argument("message")
ap.add_argument("--base", default="http://127.0.0.1:6185")
ap.add_argument("--username", default="astrbot")
ap.add_argument("--password", required=True)
ap.add_argument("--session", default="")
ap.add_argument("--timeout", type=float, default=240)
ap.add_argument("--raw", action="store_true", help="print every SSE event as-is")
ap.add_argument("--no-stream", action="store_true")
args = ap.parse_args()


def call(path, data=None, token=None, timeout=30):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(args.base + path, data=body, headers=headers, method="POST" if data is not None else "GET")
    return urllib.request.urlopen(request, timeout=timeout)


def login():
    for password in (args.password, hashlib.md5(args.password.encode()).hexdigest()):
        try:
            with call("/api/auth/login", {"username": args.username, "password": password}) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = {"status": "error", "message": exc.read().decode("utf-8", "replace")[:200]}
        token = (payload.get("data") or {}).get("token") if isinstance(payload.get("data"), dict) else None
        if token:
            return token
        last = payload
    print("login failed:", json.dumps(last, ensure_ascii=False)[:300], file=sys.stderr)
    sys.exit(2)


token = login()
session = args.session or str(uuid.uuid4())
print(f"[session {session}]")
started = time.time()
response = call(
    "/api/chat/send",
    {"message": args.message, "session_id": session, "enable_streaming": not args.no_stream},
    token=token,
    timeout=args.timeout,
)
text = []
reasoning = []
for raw_line in response:
    line = raw_line.decode("utf-8", "replace").rstrip("\n")
    if not line.startswith("data:"):
        continue
    payload = line[5:].strip()
    if args.raw:
        print(payload[:600])
        continue
    try:
        event = json.loads(payload)
    except ValueError:
        continue
    kind = event.get("type")
    data = event.get("data")
    chain_type = event.get("chain_type")
    if kind == "plain" and isinstance(data, str):
        if chain_type == "reasoning":
            reasoning.append(data)
        elif chain_type in ("tool_call", "tool_call_result"):
            print(f"[{chain_type}] {data[:300]}")
        else:
            text.append(data)
    elif kind in ("end", "complete"):
        break
    elif kind not in ("message_saved", "session_id", "run_started", None) or not isinstance(data, str):
        print(f"[{kind}] {json.dumps(data, ensure_ascii=False)[:300]}")
if not args.raw:
    if reasoning:
        print("[reasoning] " + "".join(reasoning)[:500])
    print("".join(text))
print(f"[{time.time() - started:.1f}s]")
