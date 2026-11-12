#!/usr/bin/env python3
"""Minimal ACP handshake probe for `agy acp` (read-only diagnostics).

Usage: acp_probe.py [--prompt TEXT] [--model MODEL_ID] [--client NAME]
Spawns `agy acp` in an isolated gemini dir, runs initialize + session/new,
and optionally one prompt, printing every JSON-RPC frame it sees.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

ap = argparse.ArgumentParser()
ap.add_argument("--home", default=os.path.expanduser("~/install-tmp/agy-probe"))
ap.add_argument("--prompt", default="")
ap.add_argument("--model", default="")
ap.add_argument("--effort", default="")
ap.add_argument("--client", default="astrbot")
ap.add_argument("--agent", default="")
ap.add_argument("--system", default="")
ap.add_argument("--proxy", default="http://127.0.0.1:7890")
ap.add_argument("--maxlen", type=int, default=2500)
args = ap.parse_args()

home = args.home
os.makedirs(home + "/gemini", exist_ok=True)
os.makedirs(home + "/ws", exist_ok=True)
env = dict(os.environ)
env.update(
    HTTPS_PROXY=args.proxy,
    HTTP_PROXY=args.proxy,
    DBUS_SESSION_BUS_ADDRESS="unix:path=" + home + "/.no-dbus-session",
)
argv = [
    os.path.expanduser("~/.local/bin/agy"), "acp",
    "--gemini-dir", home + "/gemini",
    "--app-data-dir", "agy",
    "--process-per-session",
    "--disable-telemetry",
    "--log-file", home + "/acp.log",
]
if args.agent:
    argv += ["--agent", args.agent]
p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, env=env, cwd=home + "/ws")
q = queue.Queue()


def pump(stream, tag):
    for line in stream:
        q.put((tag, line.rstrip("\n")))


threading.Thread(target=pump, args=(p.stdout, "OUT"), daemon=True).start()
threading.Thread(target=pump, args=(p.stderr, "ERR"), daemon=True).start()


def send(obj):
    p.stdin.write(json.dumps(obj) + "\n")
    p.stdin.flush()


def wait_for(id_, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            tag, line = q.get(timeout=0.5)
        except queue.Empty:
            if p.poll() is not None:
                print("[process exited]", p.returncode)
                return None
            continue
        if tag == "ERR":
            if "logging before google.Init" not in line:
                print("[stderr]", line[:300])
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print("[non-json stdout]", line[:300])
            continue
        if "method" in msg and "id" in msg:
            # agent -> client request: refuse everything (no fs/terminal/permission grants)
            print("[agent request]", json.dumps(msg, ensure_ascii=False)[: args.maxlen])
            if msg["method"] == "session/request_permission":
                send({"jsonrpc": "2.0", "id": msg["id"], "result": {"outcome": {"outcome": "cancelled"}}})
            else:
                send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "not supported"}})
            continue
        print("[msg]", json.dumps(msg, ensure_ascii=False)[: args.maxlen])
        if msg.get("id") == id_ and "method" not in msg:
            return msg
    print("[timeout waiting for id %s]" % id_)
    return None


t0 = time.time()
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": 1, "clientCapabilities": {}, "clientInfo": {"name": args.client, "version": "0.1.0"}}})
wait_for(1, 90)
print("== initialize took %.1fs" % (time.time() - t0))

t0 = time.time()
params = {"cwd": home + "/ws", "mcpServers": []}
if args.system:
    params["_meta"] = {"antigravity": {"systemPrompt": {"append": args.system}}}
send({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": params})
res = wait_for(2, 120)
print("== session/new took %.1fs" % (time.time() - t0))
sid = (res or {}).get("result", {}).get("sessionId") if res else None

rid = 3
if sid and args.model:
    t0 = time.time()
    send({"jsonrpc": "2.0", "id": rid, "method": "session/set_config_option",
          "params": {"sessionId": sid, "configId": "model", "value": args.model}})
    wait_for(rid, 60)
    print("== set model took %.1fs" % (time.time() - t0))
    rid += 1
if sid and args.effort:
    t0 = time.time()
    send({"jsonrpc": "2.0", "id": rid, "method": "session/set_config_option",
          "params": {"sessionId": sid, "configId": "thought_level", "value": args.effort}})
    wait_for(rid, 60)
    print("== set effort took %.1fs" % (time.time() - t0))
    rid += 1
if sid and args.prompt:
    t0 = time.time()
    send({"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
          "params": {"sessionId": sid, "prompt": [{"type": "text", "text": args.prompt}]}})
    wait_for(rid, 180)
    print("== prompt took %.1fs" % (time.time() - t0))
    rid += 1
if sid:
    send({"jsonrpc": "2.0", "id": rid, "method": "session/close", "params": {"sessionId": sid}})
    wait_for(rid, 20)

p.terminate()
try:
    p.wait(10)
except Exception:
    p.kill()
print("== exit", p.returncode)
