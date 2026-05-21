# CandidateDesk

A private, multi-user candidate review platform for your hiring team. Built with Flask, deployed on Railway with persistent volume storage.

---

## Features

- **Persistent storage** — all candidates and reviews live on a Railway volume, survive redeployments
- **CSV import** — upload Google Forms CSV exports; new rows are added, existing candidates are updated while reviews are preserved
- **Multi-user / concurrent** — two (or more) reviewers can be logged in simultaneously and review in parallel
- **Conflict protection** — if Reviewer B tags a candidate within 30 seconds of Reviewer A doing so, B is warned and must explicitly confirm before overriding
- **Live polling** — both reviewers see each other's decisions in real time (5-second poll)
- **Per-user decisions** — each reviewer's status is tracked separately; the sidebar shows an aggregated dot
- **Shared notes** — a shared text field per candidate visible to all reviewers
- **AI Review** — one-click Claude AI assessment per candidate (cached to volume, so it doesn't need regenerating)
- **Keyboard shortcuts** — `↑/↓` or `j/k` to navigate, `1` shortlist, `2` maybe, `3` reject

---

## Deploy to Railway

### 1. Push to GitHub

```bash
git init
git add .
git commit -m "initial"
gh repo create candidate-desk --private --push
```

### 2. Create Railway project

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. Select your repo

### 3. Add a Volume

1. In your Railway service → **Volumes** tab → **Add Volume**
2. Mount path: `/app/data`
3. This is where `db.json` lives — it persists across every deploy

### 4. Set Environment Variables

In Railway → your service → **Variables**, add:

| Variable | Description | Example |
|---|---|---|
| `SECRET_KEY` | Random string for Flask sessions | `openssl rand -hex 32` output |
| `DATA_DIR` | Must match volume mount path | `/app/data` |
| `USER1_NAME` | First reviewer's username | `alice` |
| `USER1_PASS` | First reviewer's password | `strongpassword1` |
| `USER2_NAME` | Second reviewer's username | `bob` |
| `USER2_PASS` | Second reviewer's password | `strongpassword2` |

> **Important:** Set `SECRET_KEY` to a long random string or sessions won't persist across restarts.

### 5. Deploy

Railway auto-deploys on every push. First deploy will start the app immediately.

---

## Usage

### Importing candidates

1. Export your Google Form responses as CSV (Responses tab → Download CSV)
2. Go to your app URL → **Import CSV** (top right)
3. Upload the CSV — new candidates are added, existing ones updated, all reviews preserved
4. Repeat any time new responses come in

### Reviewing

- **Filter** by status (Shortlist / Maybe / Reject / Unreviewed) or by role
- **Search** by name, college, or tech stack keywords
- Click a candidate to open their detail panel
- Click **Shortlist / Maybe / Reject** buttons (or press `1` / `2` / `3`)
- Clicking the same button again clears the decision
- Both reviewers' decisions show as colored badges per candidate

### Conflict handling

If you try to change a decision that your colleague made in the last 30 seconds, you'll see a warning modal. You can cancel or **Override anyway** to force your decision.

---

## Local development

```bash
pip install -r requirements.txt
export SECRET_KEY=dev-secret
export USER1_NAME=admin
export USER1_PASS=admin123
export USER2_NAME=reviewer
export USER2_PASS=review123
python app.py
```

Visit `http://localhost:5000`

---

## Architecture notes

- **Storage:** Single `db.json` file, written atomically (temp file + `os.replace`) to prevent corruption
- **Concurrency:** Python `threading.Lock` guards all writes; `os.replace` is atomic on Linux/macOS
- **Sessions:** Flask server-side sessions with 12-hour lifetime
- **Polling:** Frontend polls `/api/reviews` every 5 seconds for live updates between concurrent reviewers
- **gunicorn:** 2 workers are fine since writes are fast; the in-process lock handles same-worker concurrency; cross-worker safety is provided by atomic file writes
