#!/usr/bin/env python3
"""Read-only mirror of a local, long-running download operation.

The heavy work (downloads, validation, verification) runs on a machine at
home; this app only receives periodic status snapshots from it and shows them.
If snapshots stop arriving, the dashboard says so loudly — that IS the signal
the owner asked for ("what happens if my computer crashes").

Auth model:
  - POST /api/push   requires  Authorization: Bearer $PUSH_TOKEN  (the agent)
  - GET  /           requires  ?token=$VIEW_TOKEN once; a cookie remembers it
No database: the latest snapshot lives in memory and in /tmp (ephemeral is
fine — the agent re-pushes every couple of minutes).
"""
import json
import os
import time
from collections import deque
from pathlib import Path

from flask import (Flask, jsonify, make_response, redirect, render_template,
                   request)

app = Flask(__name__)

PUSH_TOKEN = os.environ.get("PUSH_TOKEN", "")
VIEW_TOKEN = os.environ.get("VIEW_TOKEN", "")
TMP = Path("/tmp/bookscan_ops_latest.json")

LATEST = {}
PUSH_TIMES = deque(maxlen=500)

if TMP.exists():
    try:
        LATEST = json.loads(TMP.read_text())
    except Exception:
        LATEST = {}


def _viewer_ok():
    if not VIEW_TOKEN:
        return False
    if request.cookies.get("vt") == VIEW_TOKEN:
        return True
    return request.args.get("token") == VIEW_TOKEN


@app.post("/api/push")
def push():
    auth = request.headers.get("Authorization", "")
    if not PUSH_TOKEN or auth != f"Bearer {PUSH_TOKEN}":
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "bad body"}), 400
    global LATEST
    data["received_at"] = time.time()
    LATEST = data
    PUSH_TIMES.append(time.time())
    try:
        TMP.write_text(json.dumps(data))
    except Exception:
        pass
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    if not _viewer_ok():
        return jsonify({"error": "unauthorized"}), 401
    out = dict(LATEST)
    out["server_now"] = time.time()
    return jsonify(out)


@app.get("/")
def index():
    if not _viewer_ok():
        return ("<html><body style='background:#0d1117;color:#8b949e;"
                "font-family:system-ui;display:flex;align-items:center;"
                "justify-content:center;height:100vh'>"
                "<div>This page needs its access link.</div></body></html>",
                401)
    resp = make_response(render_template("index.html"))
    # The token came in the URL; move it into a cookie and clean the URL.
    if request.args.get("token"):
        resp = make_response(redirect("/"))
        resp.set_cookie("vt", VIEW_TOKEN, max_age=180 * 24 * 3600,
                        httponly=True, secure=True, samesite="Lax")
    return resp


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "have_snapshot": bool(LATEST)})
