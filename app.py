import os
import csv
import json
import time
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from functools import wraps
from io import StringIO, TextIOWrapper

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash, g
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ─── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR   = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
DB_FILE    = os.path.join(DATA_DIR, "db.json")
LOCK_FILE  = os.path.join(DATA_DIR, ".write.lock")
os.makedirs(DATA_DIR, exist_ok=True)

# ─── Thread lock for in-process safety ────────────────────────────────────────
_mem_lock = threading.Lock()

# ─── Config ───────────────────────────────────────────────────────────────────
USERS = {
    os.environ.get("USER1_NAME", "admin"):    os.environ.get("USER1_PASS", "admin123"),
    os.environ.get("USER2_NAME", "reviewer"): os.environ.get("USER2_PASS", "review123"),
}
SESSION_LIFETIME = timedelta(hours=12)
CONFLICT_WINDOW  = 30   # seconds — if candidate was reviewed within this window, warn

# ─── DB helpers ───────────────────────────────────────────────────────────────

def _empty_db():
    return {"candidates": {}, "meta": {"last_import": None, "row_count": 0}}

def load_db():
    if not os.path.exists(DB_FILE):
        return _empty_db()
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _empty_db()

def save_db(db):
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)   # atomic on POSIX

def get_db():
    """Return the db, cached on g for this request."""
    if not hasattr(g, "_db"):
        g._db = load_db()
    return g._db

# ─── CSV parsing ──────────────────────────────────────────────────────────────

def _candidate_key(row):
    """Stable, unique key for a form row (phone + name hash)."""
    raw = (row.get("Full Name", "") + row.get("Contact Number", "")).strip().lower()
    return hashlib.md5(raw.encode()).hexdigest()

def _parse_row(row, row_list=None):
    """
    Parse a CSV row into a candidate dict.

    Google Forms exports duplicate column headers for each section (e.g. every
    role section has its own "GitHub Repository URL" and "Google Drive Link for
    Demo Video").  Python's csv.DictReader collapses duplicate keys and only
    retains the LAST value, which means FullStack / VR link columns get
    overwritten by the empty columns from later sections.

    To fix this we accept the raw list of cell values (row_list) and map each
    role's columns by their positional index in the CSV header layout so we
    always read the correct cell regardless of duplicate header names.

    Expected column layout (0-based):
      0  Timestamp
      1  Full Name
      2  Contact Number
      3  College / University Name
      4  Role Applying For (Required)
      --- Full Stack / AI Developer section ---
      5  Assignment Submitted  (FS)
      6  Brief Description of Submission
      7  Tech Stack Used
      8  Key Features Implemented
      9  Challenges Faced & Solutions  (FS)
      10 GitHub Repository URL  (FS)
      11 Google Drive Link for Demo Video  (FS)
      12 Google Drive Link for Screenshots / Assets
      13 Deployment Link
      --- VR & Clinical App Developer section ---
      14 Brief Description of VR Experience
      15 Tools / Engines Used
      16 Features / Interactions Implemented
      17   Architecture & Code Structure
      18   Performance & Optimization Considerations
      19 Challenges Faced & Solutions  (VR)
      20 GitHub Repository URL  (VR)
      21 Google Drive Link for Demo Video  (VR)
      22 Google Drive Link for Screenshots / Demo Media
      23 APK / Build Download Link (Optional)
      --- Mechanical / Industrial Design section ---
      24 Assignment Submitted  (Design)
      25 Brief Description of Design Approach
      26 Software / Tools Used
      27 Key Design Decisions
      28 Manufacturing / Material Considerations
      29 Challenges Faced & Solutions  (Design)
      30 Google Drive Link for CAD / Design Files
      31 Google Drive Link for Renders / Drawings
      32 Google Drive Link for Explanation Video or Presentation
      33 Simulation Report Link
      34 Prototype Photos Link
      --- Smart Glasses & IoT section ---
      35   Brief Description of Your Submission
      36   Technologies / Frameworks Used
      37 Protocol Parser, BLE Communication & Packet Handling Approach
      38 Simulator / Visualization Features Implemented
      39   Testing & Validation Approach
      40   Challenges Faced & Solutions  (IoT)
      41   Public GitHub Repository URL
      42   Google Drive Link for Demo Video & Screenshots
      43 Simulator Build / Hosted Demo Link
      --- Reflection section ---
      44 What did you learn from this assignment?
      45 References / Tutorials / Tools Used
      46 If AI tools were used, explain how ...
      47 Declaration
    """

    def _cell(idx):
        """Get cell value by column index; fall back to empty string."""
        if row_list and idx < len(row_list):
            return (row_list[idx] or "").strip()
        return ""

    role = row.get("Role Applying For (Required)", "").strip()

    # ── Common fields (all roles) ─────────────────────────────────────────────
    parsed = {
        "key":        _candidate_key(row),
        "name":       row.get("Full Name", "").strip(),
        "college":    row.get("College / University Name", "").strip(),
        "phone":      row.get("Contact Number", "").strip(),
        "role":       role,
        "timestamp":  row.get("Timestamp", "").strip(),
        "learning":   _cell(44),
        "references": _cell(45),
        "ai_usage":   _cell(46),
        "declaration":_cell(47),
        # role-specific fields default to empty
        "assignment": "",
        "description": "",
        "tech_stack": "",
        "features": "",
        "challenges": "",
        "github": "",
        "demo_video": "",
        "screenshots": "",
        "deployment": "",
        # VR / Clinical App Developer fields
        "vr_description": "",
        "vr_tools": "",
        "vr_features": "",
        "vr_architecture": "",
        "vr_performance": "",
        "vr_apk": "",
        # Mechanical / Industrial Design fields
        "design_assignment": "",
        "design_description": "",
        "design_tools": "",
        "design_decisions": "",
        "design_manufacturing": "",
        "design_challenges": "",
        "design_cad_files": "",
        "design_renders": "",
        "design_video": "",
        "design_simulation": "",
        "design_prototype": "",
        # Smart Glasses / IoT fields
        "iot_description": "",
        "iot_tech": "",
        "iot_protocol": "",
        "iot_simulator": "",
        "iot_testing": "",
        "iot_challenges": "",
        "iot_github": "",
        "iot_demo": "",
        "iot_build": "",
    }

    # ── Full Stack / AI Developer ─────────────────────────────────────────────
    if "Full Stack" in role or "AI Developer" in role:
        parsed.update({
            "assignment":  _cell(5),
            "description": _cell(6),
            "tech_stack":  _cell(7),
            "features":    _cell(8),
            "challenges":  _cell(9),
            "github":      _cell(10),
            "demo_video":  _cell(11),
            "screenshots": _cell(12),
            "deployment":  _cell(13),
        })

    # ── VR & Clinical App Developer ───────────────────────────────────────────
    elif "VR" in role or "Clinical" in role:
        parsed.update({
            "vr_description":  _cell(14),
            "vr_tools":        _cell(15),
            "vr_features":     _cell(16),
            "vr_architecture": _cell(17),
            "vr_performance":  _cell(18),
            "vr_apk":          _cell(23),
            "github":          _cell(20),
            "demo_video":      _cell(21),
            "screenshots":     _cell(22),
            # map to generic fields so search and AI review work
            "description":     _cell(14),
            "tech_stack":      _cell(15),
            "features":        _cell(16),
            "challenges":      _cell(19),
        })

    # ── Mechanical / Industrial Design ────────────────────────────────────────
    elif "Mechanical" in role or "Industrial" in role or "Design" in role:
        parsed.update({
            "design_assignment":    _cell(24),
            "design_description":   _cell(25),
            "design_tools":         _cell(26),
            "design_decisions":     _cell(27),
            "design_manufacturing": _cell(28),
            "design_challenges":    _cell(29),
            "design_cad_files":     _cell(30),
            "design_renders":       _cell(31),
            "design_video":         _cell(32),
            "design_simulation":    _cell(33),
            "design_prototype":     _cell(34),
            # map to generic fields
            "assignment":   _cell(24),
            "description":  _cell(25),
            "tech_stack":   _cell(26),
            "features":     _cell(27),
            "challenges":   _cell(29),
        })

    # ── Smart Glasses & IoT Application Developer ─────────────────────────────
    elif "IoT" in role or "Smart Glasses" in role or "Glasses" in role:
        parsed.update({
            "iot_description": _cell(35),
            "iot_tech":        _cell(36),
            "iot_protocol":    _cell(37),
            "iot_simulator":   _cell(38),
            "iot_testing":     _cell(39),
            "iot_challenges":  _cell(40),
            "iot_github":      _cell(41),
            "iot_demo":        _cell(42),
            "iot_build":       _cell(43),
            # map to generic fields
            "description":  _cell(35),
            "tech_stack":   _cell(36),
            "features":     _cell(38),
            "challenges":   _cell(40),
            "github":       _cell(41),
            "demo_video":   _cell(42),
            "deployment":   _cell(43),
        })

    return parsed

def import_csv(fileobj):
    """
    Parse CSV and merge into DB.
    Returns (new_count, updated_count, skipped_count, role_breakdown).
    Existing reviews are PRESERVED; only candidate data fields are updated.
    """
    with _mem_lock:
        db = load_db()
        candidates = db["candidates"]

        text = fileobj.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig")

        # We need both the dict (for non-duplicate fields like name/phone) and
        # the raw row list for positional access to duplicate-header columns
        # (e.g. "GitHub Repository URL" appears in both the FS and VR sections).
        # csv.DictReader keeps only the LAST value for duplicate keys, which
        # causes FS/VR github & video links to come back empty. Positional
        # access via the raw reader always returns the correct cell.
        raw_reader = csv.reader(StringIO(text))
        next(raw_reader)   # skip header row

        dict_reader = csv.DictReader(StringIO(text))
        new_count = updated_count = skipped_count = 0
        role_counts = {}

        for row, row_list in zip(dict_reader, raw_reader):
            parsed = _parse_row(row, row_list)
            key = parsed["key"]
            role = parsed["role"]
            role_counts[role] = role_counts.get(role, 0) + 1

            if key not in candidates:
                candidates[key] = {**parsed, "reviews": {}, "note": "", "ai_review": ""}
                new_count += 1
            else:
                # Update candidate data but preserve reviews
                existing = candidates[key]
                for field in ("name","college","phone","role","timestamp","assignment",
                              "description","tech_stack","features","challenges",
                              "github","demo_video","screenshots","deployment",
                              "learning","references","ai_usage","declaration",
                              "vr_description","vr_tools","vr_features","vr_architecture","vr_performance","vr_apk",
                              "design_assignment","design_description","design_tools","design_decisions",
                              "design_manufacturing","design_challenges","design_cad_files","design_renders",
                              "design_video","design_simulation","design_prototype",
                              "iot_description","iot_tech","iot_protocol","iot_simulator","iot_testing",
                              "iot_challenges","iot_github","iot_demo","iot_build"):
                    existing[field] = parsed[field]
                updated_count += 1

        db["meta"]["last_import"] = datetime.utcnow().isoformat()
        db["meta"]["row_count"] = len(candidates)
        save_db(db)
        return new_count, updated_count, skipped_count, role_counts

# ─── Auth ─────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login", next=request.url))
        # refresh session lifetime
        session.permanent = True
        app.permanent_session_lifetime = SESSION_LIFETIME
        return f(*args, **kwargs)
    return decorated

# ─── Routes: Auth ─────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if USERS.get(username) == password:
            session.permanent = True
            app.permanent_session_lifetime = SESSION_LIFETIME
            session["username"] = username
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid credentials."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ─── Routes: Main ─────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    db = get_db()
    candidates = list(db["candidates"].values())
    roles = sorted(set(c["role"] for c in candidates if c["role"]))
    statuses = ["unreviewed", "shortlist", "maybe", "reject"]
    return render_template("index.html",
        candidates=candidates,
        roles=roles,
        statuses=statuses,
        username=session["username"],
        meta=db["meta"],
        all_users=list(USERS.keys()),
        gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
    )

@app.route("/candidate/<key>")
@login_required
def candidate_detail(key):
    db = get_db()
    c = db["candidates"].get(key)
    if not c:
        return jsonify({"error": "Not found"}), 404
    return jsonify(c)

# ─── Routes: Review ───────────────────────────────────────────────────────────

@app.route("/review/<key>", methods=["POST"])
@login_required
def set_review(key):
    """
    Body: { status: str, force: bool }
    Returns: { ok, conflict, conflict_by, conflict_at, conflict_status }
    """
    data    = request.get_json(force=True)
    status  = data.get("status", "").strip()
    force   = bool(data.get("force", False))
    note    = data.get("note", None)
    user    = session["username"]
    now_ts  = time.time()

    valid_statuses = {"shortlist", "maybe", "reject", "unreviewed"}
    if status not in valid_statuses:
        return jsonify({"ok": False, "error": "Invalid status"}), 400

    with _mem_lock:
        db = load_db()
        c = db["candidates"].get(key)
        if not c:
            return jsonify({"ok": False, "error": "Not found"}), 404

        reviews = c.setdefault("reviews", {})

        # Check for recent review by ANOTHER user
        if not force:
            for reviewer, rev in reviews.items():
                if reviewer == user:
                    continue
                age = now_ts - rev.get("ts", 0)
                if age < CONFLICT_WINDOW and rev.get("status") != "unreviewed":
                    return jsonify({
                        "ok": False,
                        "conflict": True,
                        "conflict_by": reviewer,
                        "conflict_at": rev.get("at", ""),
                        "conflict_status": rev.get("status", ""),
                        "their_status": rev.get("status", ""),
                    })

        reviews[user] = {
            "status": status,
            "ts": now_ts,
            "at": datetime.utcnow().strftime("%H:%M:%S UTC"),
        }
        if note is not None:
            c["note"] = note

        save_db(db)

    return jsonify({"ok": True, "reviews": reviews})

@app.route("/note/<key>", methods=["POST"])
@login_required
def set_note(key):
    data = request.get_json(force=True)
    note = data.get("note", "")
    with _mem_lock:
        db = load_db()
        c = db["candidates"].get(key)
        if not c:
            return jsonify({"ok": False}), 404
        c["note"] = note
        save_db(db)
    return jsonify({"ok": True})

@app.route("/ai_review/<key>", methods=["POST"])
@login_required
def save_ai_review(key):
    data = request.get_json(force=True)
    text = data.get("text", "")
    with _mem_lock:
        db = load_db()
        c = db["candidates"].get(key)
        if not c:
            return jsonify({"ok": False}), 404
        c["ai_review"] = text
        save_db(db)
    return jsonify({"ok": True})

# ─── Routes: Upload ───────────────────────────────────────────────────────────

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    result = None
    if request.method == "POST":
        f = request.files.get("csv_file")
        if not f or not f.filename.endswith(".csv"):
            flash("Please upload a .csv file.", "error")
            return redirect(url_for("upload"))
        new_c, upd_c, skip_c, roles = import_csv(f)
        result = {"new": new_c, "updated": upd_c, "skipped": skip_c, "roles": roles}
    return render_template("upload.html", result=result, username=session["username"])

# ─── Routes: API (polling for live updates) ───────────────────────────────────

@app.route("/api/reviews")
@login_required
def api_reviews():
    """Return all reviews for lightweight polling."""
    db = load_db()
    out = {}
    for key, c in db["candidates"].items():
        out[key] = {"reviews": c.get("reviews", {}), "note": c.get("note", "")}
    return jsonify(out)

@app.route("/api/stats")
@login_required
def api_stats():
    db = load_db()
    counts = {"shortlist": 0, "maybe": 0, "reject": 0, "unreviewed": 0}
    for c in db["candidates"].values():
        reviews = c.get("reviews", {})
        # Aggregate: majority vote, else unreviewed
        if not reviews:
            counts["unreviewed"] += 1
        else:
            votes = [r["status"] for r in reviews.values()]
            dominant = max(set(votes), key=votes.count)
            counts[dominant] = counts.get(dominant, 0) + 1
    return jsonify(counts)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)