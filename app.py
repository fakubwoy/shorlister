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

def _parse_row(row):
    role = row.get("Role Applying For (Required)", "").strip()

    # ── Common fields (all roles) ─────────────────────────────────────────────
    parsed = {
        "key":        _candidate_key(row),
        "name":       row.get("Full Name", "").strip(),
        "college":    row.get("College / University Name", "").strip(),
        "phone":      row.get("Contact Number", "").strip(),
        "role":       role,
        "timestamp":  row.get("Timestamp", "").strip(),
        "learning":   row.get("What did you learn from this assignment?", "").strip(),
        "references": row.get("References / Tutorials / Tools Used", "").strip(),
        "ai_usage":   row.get("If AI tools were used, explain how (e.g., code generation, design inspiration, analysis).", "").strip(),
        "declaration":row.get("Declaration", "").strip(),
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
            "assignment":  row.get("Assignment Submitted", "").strip(),
            "description": row.get("Brief Description of Submission", "").strip(),
            "tech_stack":  row.get("Tech Stack Used", "").strip(),
            "features":    row.get("Key Features Implemented", "").strip(),
            "challenges":  row.get("Challenges Faced & Solutions", "").strip(),
            "github":      row.get("GitHub Repository URL", "").strip(),
            "demo_video":  row.get("Google Drive Link for Demo Video", "").strip(),
            "screenshots": row.get("Google Drive Link for Screenshots / Assets", "").strip(),
            "deployment":  row.get("Deployment Link", "").strip(),
        })

    # ── VR & Clinical App Developer ───────────────────────────────────────────
    elif "VR" in role or "Clinical" in role:
        parsed.update({
            "github":          row.get("GitHub Repository URL", "").strip(),
            "demo_video":      row.get("Google Drive Link for Demo Video", "").strip(),
            "screenshots":     row.get("Google Drive Link for Screenshots / Demo Media ", "").strip(),
            "vr_description":  row.get("Brief Description of VR Experience", "").strip(),
            "vr_tools":        row.get("Tools / Engines Used", "").strip(),
            "vr_features":     row.get("Features / Interactions Implemented", "").strip(),
            "vr_architecture": row.get("  Architecture & Code Structure  ", "").strip(),
            "vr_performance":  row.get("  Performance & Optimization Considerations  ", "").strip(),
            "vr_apk":          row.get("APK / Build Download Link (Optional)", "").strip(),
            # map to generic fields so search and AI review work
            "description":     row.get("Brief Description of VR Experience", "").strip(),
            "tech_stack":      row.get("Tools / Engines Used", "").strip(),
            "features":        row.get("Features / Interactions Implemented", "").strip(),
            "challenges":      row.get("Challenges Faced & Solutions", "").strip(),
        })

    # ── Mechanical / Industrial Design ────────────────────────────────────────
    elif "Mechanical" in role or "Industrial" in role or "Design" in role:
        parsed.update({
            "design_assignment":    row.get("Assignment Submitted", "").strip(),
            "design_description":   row.get("Brief Description of Design Approach", "").strip(),
            "design_tools":         row.get("Software / Tools Used", "").strip(),
            "design_decisions":     row.get("Key Design Decisions", "").strip(),
            "design_manufacturing": row.get("Manufacturing / Material Considerations", "").strip(),
            "design_challenges":    row.get("Challenges Faced & Solutions", "").strip(),
            "design_cad_files":     row.get("Google Drive Link for CAD / Design Files", "").strip(),
            "design_renders":       row.get("Google Drive Link for Renders / Drawings", "").strip(),
            "design_video":         row.get("Google Drive Link for Explanation Video or Presentation", "").strip(),
            "design_simulation":    row.get("Simulation Report Link", "").strip(),
            "design_prototype":     row.get("Prototype Photos Link", "").strip(),
            # map to generic fields
            "assignment":   row.get("Assignment Submitted", "").strip(),
            "description":  row.get("Brief Description of Design Approach", "").strip(),
            "tech_stack":   row.get("Software / Tools Used", "").strip(),
            "features":     row.get("Key Design Decisions", "").strip(),
            "challenges":   row.get("Challenges Faced & Solutions", "").strip(),
        })

    # ── Smart Glasses & IoT Application Developer ─────────────────────────────
    elif "IoT" in role or "Smart Glasses" in role or "Glasses" in role:
        parsed.update({
            "iot_description": row.get("  Brief Description of Your Submission  ", "").strip(),
            "iot_tech":        row.get("  Technologies / Frameworks Used  ", "").strip(),
            "iot_protocol":    row.get("Protocol Parser, BLE Communication & Packet Handling Approach ", "").strip(),
            "iot_simulator":   row.get("Simulator / Visualization Features Implemented ", "").strip(),
            "iot_testing":     row.get("  Testing & Validation Approach  ", "").strip(),
            "iot_challenges":  row.get("  Challenges Faced & Solutions  ", "").strip(),
            "iot_github":      row.get("  Public GitHub Repository URL  ", "").strip(),
            "iot_demo":        row.get("  Google Drive Link for Demo Video & Screenshots  ", "").strip(),
            "iot_build":       row.get("Simulator Build / Hosted Demo Link ", "").strip(),
            # map to generic fields
            "description":  row.get("  Brief Description of Your Submission  ", "").strip(),
            "tech_stack":   row.get("  Technologies / Frameworks Used  ", "").strip(),
            "features":     row.get("Simulator / Visualization Features Implemented ", "").strip(),
            "challenges":   row.get("  Challenges Faced & Solutions  ", "").strip(),
            "github":       row.get("  Public GitHub Repository URL  ", "").strip(),
            "demo_video":   row.get("  Google Drive Link for Demo Video & Screenshots  ", "").strip(),
            "deployment":   row.get("Simulator Build / Hosted Demo Link ", "").strip(),
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

        reader = csv.DictReader(StringIO(text))
        new_count = updated_count = skipped_count = 0
        role_counts = {}

        for row in reader:
            parsed = _parse_row(row)
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