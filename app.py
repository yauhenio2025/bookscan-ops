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
import base64
import json
import os
import re
import time
from collections import deque
from pathlib import Path

from flask import (Flask, jsonify, make_response, redirect, render_template,
                   request, send_file)

app = Flask(__name__)

PUSH_TOKEN = os.environ.get("PUSH_TOKEN", "")
VIEW_TOKEN = os.environ.get("VIEW_TOKEN", "")
TMP = Path("/tmp/bookscan_ops_latest.json")
THUMBS = Path("/tmp/bookscan_thumbs")
MANIFEST_F = Path("/tmp/bookscan_review_manifest.json")
VERDICTS_F = Path("/tmp/bookscan_verdicts.json")
THUMBS.mkdir(parents=True, exist_ok=True)

LATEST = {}
MANIFEST = []          # review rows, in owner-priority order
VERDICTS = {}          # video_id -> {verdict, note, at, applied}
PUSH_TIMES = deque(maxlen=500)

PLACEHOLDER = ("<svg xmlns='http://www.w3.org/2000/svg' width='560' height='315'>"
               "<rect width='100%' height='100%' fill='#2c2c2b'/>"
               "<text x='50%' y='50%' fill='#8b8a80' font-family='sans-serif' "
               "font-size='15' text-anchor='middle'>frame not synced yet</text></svg>")


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


LATEST = _load(TMP, {})
MANIFEST = _load(MANIFEST_F, {}).get("rows", [])
VERDICTS = _load(VERDICTS_F, {})


def _viewer_ok():
    if not VIEW_TOKEN:
        return False
    if request.cookies.get("vt") == VIEW_TOKEN:
        return True
    return request.args.get("token") == VIEW_TOKEN


def _pusher_ok():
    return PUSH_TOKEN and request.headers.get("Authorization") == f"Bearer {PUSH_TOKEN}"


# ------------------------------------------------------------ agent endpoints

@app.post("/api/review-manifest")
def review_manifest():
    if not _pusher_ok():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return jsonify({"error": "bad body"}), 400
    global MANIFEST
    MANIFEST = data["rows"]
    try:
        MANIFEST_F.write_text(json.dumps(data))
    except Exception:
        pass
    return jsonify({"ok": True, "rows": len(MANIFEST)})


@app.get("/api/thumbs/have")
def thumbs_have():
    if not _pusher_ok():
        return jsonify({"error": "unauthorized"}), 401
    if request.args.get("mode") == "count":
        return jsonify({"count": sum(1 for _ in THUMBS.glob("*.jpg"))})
    return jsonify({"names": sorted(p.name for p in THUMBS.glob("*.jpg"))})


@app.post("/api/thumbs")
def thumbs_upload():
    if not _pusher_ok():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    n = 0
    for item in data.get("items", []):
        name = str(item.get("name", ""))
        # Uploaded names become filesystem paths; keep them boring.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.jpg", name):
            continue
        try:
            (THUMBS / name).write_bytes(base64.b64decode(item.get("b64", "")))
            n += 1
        except Exception:
            continue
    return jsonify({"ok": True, "written": n})


@app.get("/api/verdicts")
def verdicts_pull():
    if not _pusher_ok():
        return jsonify({"error": "unauthorized"}), 401
    return jsonify({"verdicts": VERDICTS})


@app.post("/api/verdicts/ack")
def verdicts_ack():
    if not _pusher_ok():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    for vid, applied in (data.get("applied") or {}).items():
        if vid in VERDICTS:
            VERDICTS[vid]["applied"] = applied
    try:
        VERDICTS_F.write_text(json.dumps(VERDICTS))
    except Exception:
        pass
    return jsonify({"ok": True})


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


# ------------------------------------------------------------ review (owner)

@app.get("/review")
def review_page():
    if not _viewer_ok():
        return redirect("/")
    resp = make_response(render_template("review.html"))
    if request.args.get("token"):
        resp = make_response(redirect("/review"))
        resp.set_cookie("vt", VIEW_TOKEN, max_age=180 * 24 * 3600,
                        httponly=True, secure=True, samesite="Lax")
    return resp


@app.get("/api/review")
def review_api():
    if not _viewer_ok():
        return jsonify({"error": "unauthorized"}), 401
    flt = request.args.get("flt", "all")
    qs = (request.args.get("q") or "").lower().strip()
    offset = max(0, int(request.args.get("offset", 0)))
    limit = min(200, int(request.args.get("limit", 60)))

    def keep(r):
        v = VERDICTS.get(r["video_id"], {}).get("verdict") or r.get("verdict")
        if flt == "downloaded" and r.get("route") != "download_and_skip":
            return False
        if flt == "pending" and r.get("route") not in ("download_pending",
                                                       "blocked_quota"):
            return False
        if flt == "flagged" and not r.get("dl_category"):
            return False
        if flt == "scan" and r.get("route") != "scan_ourselves":
            return False
        if flt.startswith("scan_") and r.get("scan_kind") != flt[5:]:
            return False
        if flt == "unreviewed" and v:
            return False
        if flt == "wrong" and v != "wrong":
            return False
        if flt == "notisbn" and r.get("isbn_anchored"):
            return False
        if flt == "long" and not (r.get("minutes") and r["minutes"] >= 20):
            return False
        if flt == "kept" and not r.get("kept_unverified"):
            return False
        if flt == "weak" and not (r.get("route") == "download_and_skip"
                                  and r.get("containment") is not None
                                  and r["containment"] < 0.30):
            return False
        if flt == "cluster" and not r.get("cluster"):
            return False
        if flt == "llmretired" and not (r.get("route") == "download_and_skip"
                                        and not r.get("isbn_anchored")):
            return False
        if flt == "iaborrow" and r.get("route") != "ia_borrow_available":
            return False
        if flt == "iadirect" and r.get("ia_access") != "direct":
            return False
        if qs and qs not in " ".join(str(r.get(k) or "").lower() for k in
                                     ("title", "author", "isbn", "video_id")):
            return False
        return True

    rows = [r for r in MANIFEST if keep(r)]
    out = []
    for r in rows[offset:offset + limit]:
        rr = dict(r)
        v = VERDICTS.get(r["video_id"])
        if v:
            rr["verdict"] = v["verdict"]
            rr["verdict_applied"] = v.get("applied")
        out.append(rr)
    reviewed = sum(1 for r in MANIFEST
                   if VERDICTS.get(r["video_id"]) or r.get("verdict"))
    return jsonify({"total": len(rows), "all_rows": len(MANIFEST),
                    "reviewed": reviewed, "offset": offset, "rows": out,
                    "thumbs_synced": sum(1 for _ in THUMBS.glob("*.jpg"))})


@app.post("/api/verdict")
def verdict_post():
    if not _viewer_ok():
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    vid = str(data.get("video_id", ""))
    verdict = data.get("verdict")
    if not vid or verdict not in ("correct", "wrong", "unsure",
                                  "notabook", "both", None):
        return jsonify({"error": "bad verdict"}), 400
    if verdict is None:
        VERDICTS.pop(vid, None)
    else:
        VERDICTS[vid] = {"verdict": verdict,
                         "note": str(data.get("note") or "")[:300],
                         "at": time.time(), "applied": None}
    try:
        VERDICTS_F.write_text(json.dumps(VERDICTS))
    except Exception:
        pass
    return jsonify({"ok": True})


ROUTE_LABEL = {"download_and_skip": "have text (downloaded)",
               "download_pending": "download queued",
               "blocked_quota": "download queued",
               "scan_ourselves": "scanning from video",
               "ia_borrow_available": "borrow on archive.org",
               "not_book_media_rip": "not a book (lecture/rip)",
               "not_a_book": "not a book",
               "human_review": "needs review",
               "retry_service": "lookup retry"}


@app.get("/books")
def books_page():
    if not _viewer_ok():
        return redirect("/")
    resp = make_response(render_template("books.html"))
    if request.args.get("token"):
        resp = make_response(redirect("/books"))
        resp.set_cookie("vt", VIEW_TOKEN, max_age=180 * 24 * 3600,
                        httponly=True, secure=True, samesite="Lax")
    return resp


@app.get("/api/books")
def books_api():
    """The whole library as rows — the card view answers 'is this right?',
    this answers 'what do we have?'."""
    if not _viewer_ok():
        return jsonify({"error": "unauthorized"}), 401
    rows = [{"title": r.get("title"), "author": r.get("author"),
             "year": r.get("year"), "isbn": r.get("isbn"),
             "isbn_ok": r.get("isbn_anchored"),
             "status": ROUTE_LABEL.get(r.get("route"), r.get("route") or "?"),
             "route": r.get("route"),
             "match": r.get("containment"), "minutes": r.get("minutes"),
             "verdict": (VERDICTS.get(r["video_id"], {}) or {}).get("verdict")
                        or r.get("verdict"),
             "video_id": r.get("video_id"),
             "play": r.get("play"), "file": r.get("dlfile")}
            for r in MANIFEST]
    return jsonify({"rows": rows, "total": len(rows)})


@app.get("/books.csv")
def books_csv():
    if not _viewer_ok():
        return "", 401
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["title", "author", "year", "isbn", "isbn_verified", "status",
                "text_overlap", "video_minutes", "owner_verdict", "video_id"])
    for r in MANIFEST:
        w.writerow([r.get("title") or "", r.get("author") or "",
                    r.get("year") or "", r.get("isbn") or "",
                    "yes" if r.get("isbn_anchored") else "no",
                    ROUTE_LABEL.get(r.get("route"), r.get("route") or ""),
                    r.get("containment") or "", r.get("minutes") or "",
                    (VERDICTS.get(r["video_id"], {}) or {}).get("verdict") or "",
                    r.get("video_id") or ""])
    return buf.getvalue(), 200, {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": 'attachment; filename="bookscan-library.csv"'}


@app.get("/thumb/<name>")
def thumb(name):
    if not _viewer_ok():
        return "", 401
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}\.jpg", name):
        return "", 404
    p = THUMBS / name
    if p.exists():
        return send_file(p, mimetype="image/jpeg", max_age=86400)
    return PLACEHOLDER, 200, {"Content-Type": "image/svg+xml",
                              "Cache-Control": "no-store"}


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "have_snapshot": bool(LATEST),
                    "manifest_rows": len(MANIFEST),
                    "thumbs": sum(1 for _ in THUMBS.glob("*.jpg"))})
