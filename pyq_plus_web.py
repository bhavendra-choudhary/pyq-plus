import os
import sqlite3
import time
import hashlib
import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn

app = FastAPI(title="PYQ Plus - Official RPSC Archive")

DB_PATH = "rpsc_archive.db"
STATIC_DIR = Path("static")
CROPS_DIR = STATIC_DIR / "crops"
CROPS_DIR.mkdir(parents=True, exist_ok=True)

SYNC_SECRET_KEY = "pyq_plus_master_sync_2026"

# ==============================================================================
# ⚠️ IMPORTANT (POINT #8): PASTE YOUR GOOGLE SEARCH CONSOLE META TAG BELOW
# Agar aapne pichhli bar pura <meta name="google-site-verification" ...> tag lagaya tha,
# to use niche GOOGLE_SITE_VERIFICATION_META ke andar hubahu rakhein:
# ==============================================================================
GOOGLE_SITE_VERIFICATION_META = """
<meta name="google-site-verification" content="8CbfnMfVSEcw2j_aTP-LYzcwLpil5c9nbbkTag8FEm0" />
"""

app.mount("/static", StaticFiles(directory="static"), name="static")


# --- SAFE DATABASE ENGINE (WAL MODE) ---
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.row_factory = sqlite3.Row
    return conn


def db_read(query, params=(), fetchone=False):
    for attempt in range(10):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(query, params)
            data = cur.fetchone() if fetchone else cur.fetchall()
            conn.close()
            return data
        except sqlite3.OperationalError:
            time.sleep(0.05)
    return None if fetchone else []


def db_write(query, params=()):
    for attempt in range(10):
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute(query, params)
            conn.commit()
            last_id = cur.lastrowid
            conn.close()
            return last_id
        except sqlite3.OperationalError:
            time.sleep(0.05)
    return None


def init_all_tables():
    db_write("""
    CREATE TABLE IF NOT EXISTS examinations (
        exam_id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_name TEXT NOT NULL,
        conducting_year TEXT DEFAULT '2011',
        UNIQUE(exam_name, conducting_year)
    );""")
    db_write("""
    CREATE TABLE IF NOT EXISTS question_papers (
        paper_id INTEGER PRIMARY KEY AUTOINCREMENT,
        exam_id INTEGER NOT NULL,
        pdf_file_name TEXT NOT NULL UNIQUE,
        exam_date TEXT NOT NULL,
        exam_shift TEXT NOT NULL,
        total_marks REAL DEFAULT 0.0,
        total_questions INTEGER DEFAULT 50,
        marks_per_question REAL DEFAULT 0.0,
        is_completed INTEGER DEFAULT 0,
        claimed_by TEXT DEFAULT NULL,
        claimed_at TEXT DEFAULT NULL
    );""")
    db_write("""
    CREATE TABLE IF NOT EXISTS subjects (
        subject_id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_name TEXT NOT NULL UNIQUE
    );""")
    db_write("""
    CREATE TABLE IF NOT EXISTS topics (
        topic_id INTEGER PRIMARY KEY AUTOINCREMENT,
        subject_id INTEGER NOT NULL,
        topic_name TEXT NOT NULL,
        UNIQUE(subject_id, topic_name)
    );""")
    db_write("""
    CREATE TABLE IF NOT EXISTS questions (
        question_id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_id INTEGER NOT NULL,
        topic_id INTEGER DEFAULT NULL,
        question_number INTEGER NOT NULL,
        marks REAL DEFAULT 0.0,
        crop_image_path TEXT NOT NULL,
        ai_status TEXT DEFAULT 'PENDING',
        UNIQUE(paper_id, question_number)
    );""")
    db_write("""
    CREATE TABLE IF NOT EXISTS pyq_users (
        user_id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT NOT NULL,
        identifier TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL
    );""")
    db_write("""
    CREATE TABLE IF NOT EXISTS pyq_starred (
        star_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        question_id INTEGER NOT NULL,
        starred_at TEXT NOT NULL,
        UNIQUE(user_id, question_id)
    );""")
    db_write("""
    CREATE TABLE IF NOT EXISTS pyq_analytics (
        visitor_id TEXT PRIMARY KEY,
        visit_date TEXT NOT NULL,
        total_seconds INTEGER DEFAULT 10,
        page_views INTEGER DEFAULT 1,
        last_seen TEXT NOT NULL
    );""")


init_all_tables()


# --- REQUEST MODELS ---
class AuthRequest(BaseModel):
    full_name: str = ""
    identifier: str
    password: str


class StarRequest(BaseModel):
    user_id: int
    question_id: int


class HeartbeatRequest(BaseModel):
    visitor_id: str
    add_seconds: int = 10
    is_new_view: bool = False


class OneClickSyncItem(BaseModel):
    secret_key: str
    exam_name: str
    conducting_year: str
    pdf_file_name: str
    exam_date: str
    exam_shift: str
    subject_name: str
    topic_name: str
    question_number: int
    image_filename: str
    image_base64: str


# --- PUBLIC API ENDPOINTS ---
@app.get("/api/init")
def api_init():
    subjects = db_read("""
        SELECT DISTINCT s.subject_id, s.subject_name
        FROM subjects s
        JOIN topics t ON s.subject_id = t.subject_id
        JOIN questions q ON t.topic_id = q.topic_id
        WHERE q.ai_status = 'COMPLETED'
        ORDER BY s.subject_name ASC
    """)
    stats = db_read("""
        SELECT 
            (SELECT COUNT(DISTINCT subject_id) FROM subjects) as total_subs,
            (SELECT COUNT(DISTINCT topic_id) FROM topics) as total_tops,
            (SELECT COUNT(*) FROM questions WHERE ai_status = 'COMPLETED') as total_qs
    """, fetchone=True)
    return {
        "subjects": [dict(s) for s in subjects],
        "stats": dict(stats) if stats else {"total_subs": 0, "total_tops": 0, "total_qs": 0}
    }


@app.get("/api/topics/{subject_id}")
def api_topics(subject_id: int):
    topics = db_read("""
        SELECT DISTINCT t.topic_id, t.topic_name
        FROM topics t
        JOIN questions q ON t.topic_id = q.topic_id
        WHERE t.subject_id = ? AND q.ai_status = 'COMPLETED'
        ORDER BY t.topic_name ASC
    """, (subject_id,))
    return {"topics": [dict(t) for t in topics]}


@app.get("/api/questions/{topic_id}")
def api_questions(topic_id: int, user_id: int = 0):
    rows = db_read("""
        SELECT q.question_id, q.question_number, q.crop_image_path,
               e.exam_name, qp.exam_date, qp.exam_shift
        FROM questions q
        JOIN question_papers qp ON q.paper_id = qp.paper_id
        JOIN examinations e ON qp.exam_id = e.exam_id
        WHERE q.topic_id = ? AND q.ai_status = 'COMPLETED'
        ORDER BY qp.exam_date DESC, CAST(q.question_number AS INTEGER) ASC
    """, (topic_id,))

    starred_ids = set()
    if user_id > 0:
        st_rows = db_read("SELECT question_id FROM pyq_starred WHERE user_id = ?", (user_id,))
        starred_ids = {r["question_id"] for r in st_rows}

    result = []
    for r in rows:
        item = dict(r)
        item["crop_image_path"] = "/" + str(item["crop_image_path"]).replace("\\", "/").lstrip("/")
        item["is_starred"] = item["question_id"] in starred_ids
        result.append(item)

    return {"questions": result}


@app.post("/api/auth")
def api_auth(req: AuthRequest):
    ident = req.identifier.strip().lower()
    pwd_hash = hashlib.sha256(req.password.encode()).hexdigest()

    existing = db_read("SELECT * FROM pyq_users WHERE identifier = ?", (ident,), fetchone=True)
    if existing:
        if existing["password_hash"] != pwd_hash:
            return JSONResponse({"ok": False, "msg": "Password galat hai! Dobara koshish karein."}, status_code=400)
        return {"ok": True, "user": {"user_id": existing["user_id"], "full_name": existing["full_name"], "identifier": ident}}
    else:
        name = req.full_name.strip() or ident.split("@")[0]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uid = db_write(
            "INSERT INTO pyq_users (full_name, identifier, password_hash, created_at) VALUES (?, ?, ?, ?)",
            (name, ident, pwd_hash, now_str)
        )
        return {"ok": True, "user": {"user_id": uid, "full_name": name, "identifier": ident}}


@app.post("/api/star")
def api_toggle_star(req: StarRequest):
    existing = db_read(
        "SELECT star_id FROM pyq_starred WHERE user_id = ? AND question_id = ?",
        (req.user_id, req.question_id),
        fetchone=True
    )
    if existing:
        db_write("DELETE FROM pyq_starred WHERE star_id = ?", (existing["star_id"],))
        return {"ok": True, "starred": False}
    else:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db_write(
            "INSERT OR IGNORE INTO pyq_starred (user_id, question_id, starred_at) VALUES (?, ?, ?)",
            (req.user_id, req.question_id, now_str)
        )
        return {"ok": True, "starred": True}


@app.get("/api/starred/{user_id}")
def api_get_starred(user_id: int):
    rows = db_read("""
        SELECT q.question_id, q.question_number, q.crop_image_path,
               e.exam_name, qp.exam_date, qp.exam_shift,
               s.subject_name, t.topic_name
        FROM pyq_starred ps
        JOIN questions q ON ps.question_id = q.question_id
        JOIN question_papers qp ON q.paper_id = qp.paper_id
        JOIN examinations e ON qp.exam_id = e.exam_id
        LEFT JOIN topics t ON q.topic_id = t.topic_id
        LEFT JOIN subjects s ON t.subject_id = s.subject_id
        WHERE ps.user_id = ?
        ORDER BY ps.starred_at DESC
    """, (user_id,))
    result = []
    for r in rows:
        item = dict(r)
        item["crop_image_path"] = "/" + str(item["crop_image_path"]).replace("\\", "/").lstrip("/")
        item["is_starred"] = True
        result.append(item)
    return {"questions": result}


@app.post("/api/heartbeat")
def api_heartbeat(req: HeartbeatRequest):
    today = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    existing = db_read("SELECT * FROM pyq_analytics WHERE visitor_id = ?", (req.visitor_id,), fetchone=True)
    if existing:
        new_sec = existing["total_seconds"] + req.add_seconds
        new_views = existing["page_views"] + (1 if req.is_new_view else 0)
        db_write(
            "UPDATE pyq_analytics SET total_seconds = ?, page_views = ?, last_seen = ? WHERE visitor_id = ?",
            (new_sec, new_views, now_str, req.visitor_id)
        )
    else:
        db_write(
            "INSERT INTO pyq_analytics (visitor_id, visit_date, total_seconds, page_views, last_seen) VALUES (?, ?, ?, 1, ?)",
            (req.visitor_id, today, req.add_seconds, now_str)
        )
    return {"ok": True}


@app.get("/api/admin-analytics")
def api_admin_analytics():
    summary = db_read("""
        SELECT
            COUNT(visitor_id) as total_visitors,
            COALESCE(SUM(total_seconds), 0) as total_sec,
            COALESCE(AVG(total_seconds), 0) as avg_sec,
            COALESCE(SUM(page_views), 0) as total_views,
            (SELECT COUNT(*) FROM pyq_users) as registered_users,
            (SELECT COUNT(*) FROM pyq_starred) as total_stars,
            (SELECT COUNT(*) FROM question_papers) as total_papers,
            (SELECT COUNT(*) FROM question_papers WHERE is_completed = 1) as completed_papers,
            (SELECT COUNT(*) FROM question_papers WHERE is_completed = 0) as incomplete_papers,
            (SELECT COUNT(*) FROM questions) as total_questions,
            (SELECT COUNT(*) FROM questions WHERE ai_status = 'COMPLETED') as completed_questions,
            (SELECT COUNT(*) FROM questions WHERE ai_status = 'PENDING') as pending_questions,
            (SELECT COUNT(*) FROM questions WHERE ai_status LIKE 'REJECTED%') as rejected_questions
        FROM pyq_analytics
    """, fetchone=True)

    first_visit_start = (datetime.now().date() - timedelta(days=29)).isoformat()
    first_visit_end = datetime.now().date().isoformat()
    daily_first_visits = db_read("""
        SELECT visit_date, COUNT(visitor_id) as first_visits
        FROM pyq_analytics
        WHERE visit_date >= ? AND visit_date <= ?
        GROUP BY visit_date
        ORDER BY visit_date ASC
    """, (first_visit_start, first_visit_end))

    first_visits_by_date = {
        row["visit_date"]: row["first_visits"] for row in daily_first_visits
    }
    today = datetime.now().date()
    first_visits = [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "count": first_visits_by_date.get(
                (today - timedelta(days=offset)).isoformat(), 0
            ),
        }
        for offset in range(29, -1, -1)
    ]

    subject_rows = db_read("""
        SELECT s.subject_name, COUNT(q.question_id) as completed_questions
        FROM questions q
        JOIN topics t ON q.topic_id = t.topic_id
        JOIN subjects s ON t.subject_id = s.subject_id
        WHERE q.ai_status = 'COMPLETED'
        GROUP BY s.subject_id, s.subject_name
        ORDER BY completed_questions DESC, s.subject_name ASC
        LIMIT 10
    """)

    paper_rows = db_read("""
        SELECT
            e.exam_name,
            e.conducting_year,
            qp.pdf_file_name,
            qp.exam_date,
            qp.exam_shift,
            qp.is_completed,
            COUNT(q.question_id) as detected_questions,
            COALESCE(SUM(CASE WHEN q.ai_status = 'COMPLETED' THEN 1 ELSE 0 END), 0)
                as classified_questions,
            COALESCE(SUM(CASE WHEN q.ai_status = 'PENDING' THEN 1 ELSE 0 END), 0)
                as pending_questions,
            COALESCE(SUM(CASE WHEN q.ai_status LIKE 'REJECTED%' THEN 1 ELSE 0 END), 0)
                as rejected_questions
        FROM question_papers qp
        JOIN examinations e ON e.exam_id = qp.exam_id
        LEFT JOIN questions q ON q.paper_id = qp.paper_id
        GROUP BY qp.paper_id
        ORDER BY qp.is_completed ASC, detected_questions DESC, e.exam_name ASC
        LIMIT 12
    """)

    status_rows = db_read("""
        SELECT ai_status, COUNT(*) as count
        FROM questions
        GROUP BY ai_status
        ORDER BY ai_status
    """)

    return {
        "summary": dict(summary) if summary else {},
        "first_visits": first_visits,
        "subjects": [dict(row) for row in subject_rows],
        "papers": [dict(row) for row in paper_rows],
        "question_statuses": [dict(row) for row in status_rows],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


# --- 1-CLICK AUTO SYNC RECEIVER ENDPOINTS ---
@app.get("/api/sync/existing-keys")
def api_sync_existing_keys(secret_key: str = ""):
    if secret_key != SYNC_SECRET_KEY:
        return JSONResponse({"ok": False, "msg": "Unauthorized"}, status_code=403)
    rows = db_read("""
        SELECT qp.pdf_file_name, q.question_number, s.subject_name, t.topic_name
        FROM questions q
        JOIN question_papers qp ON q.paper_id = qp.paper_id
        JOIN topics t ON q.topic_id = t.topic_id
        JOIN subjects s ON t.subject_id = s.subject_id
        WHERE q.ai_status = 'COMPLETED'
    """)
    synced_map = {
        f"{r['pdf_file_name']}::{r['question_number']}": f"{r['subject_name']}::{r['topic_name']}"
        for r in rows
    }
    return {"ok": True, "synced": synced_map}


@app.post("/api/sync/push-question")
def api_sync_push_question(item: OneClickSyncItem):
    if item.secret_key != SYNC_SECRET_KEY:
        return JSONResponse({"ok": False, "msg": "Invalid Secret Key"}, status_code=403)

    img_bytes = base64.b64decode(item.image_base64)
    rel_img_path = f"static/crops/{item.image_filename}"
    full_img_path = CROPS_DIR / item.image_filename
    with open(full_img_path, "wb") as f:
        f.write(img_bytes)

    exam_clean = item.exam_name.strip()
    year_clean = item.conducting_year.strip() or "2011"
    e_row = db_read("SELECT exam_id FROM examinations WHERE exam_name = ?", (exam_clean,), fetchone=True)
    if not e_row:
        db_write("INSERT OR IGNORE INTO examinations (exam_name, conducting_year) VALUES (?, ?)", (exam_clean, year_clean))
        e_row = db_read("SELECT exam_id FROM examinations WHERE exam_name = ?", (exam_clean,), fetchone=True)
    exam_id = e_row["exam_id"]

    p_row = db_read("SELECT paper_id FROM question_papers WHERE pdf_file_name = ?", (item.pdf_file_name,), fetchone=True)
    if not p_row:
        db_write("""
            INSERT INTO question_papers (exam_id, pdf_file_name, exam_date, exam_shift, is_completed)
            VALUES (?, ?, ?, ?, 1)
        """, (exam_id, item.pdf_file_name, item.exam_date, item.exam_shift))
        p_row = db_read("SELECT paper_id FROM question_papers WHERE pdf_file_name = ?", (item.pdf_file_name,), fetchone=True)
    else:
        db_write("""
            UPDATE question_papers SET exam_id = ?, exam_date = ?, exam_shift = ? WHERE paper_id = ?
        """, (exam_id, item.exam_date, item.exam_shift, p_row["paper_id"]))
    paper_id = p_row["paper_id"]

    sub_clean = item.subject_name.strip()
    top_clean = item.topic_name.strip()
    s_row = db_read("SELECT subject_id FROM subjects WHERE subject_name = ?", (sub_clean,), fetchone=True)
    if not s_row:
        db_write("INSERT OR IGNORE INTO subjects (subject_name) VALUES (?)", (sub_clean,))
        s_row = db_read("SELECT subject_id FROM subjects WHERE subject_name = ?", (sub_clean,), fetchone=True)
    subject_id = s_row["subject_id"]

    t_row = db_read("SELECT topic_id FROM topics WHERE subject_id = ? AND topic_name = ?", (subject_id, top_clean), fetchone=True)
    if not t_row:
        db_write("INSERT OR IGNORE INTO topics (subject_id, topic_name) VALUES (?, ?)", (subject_id, top_clean))
        t_row = db_read("SELECT topic_id FROM topics WHERE subject_id = ? AND topic_name = ?", (subject_id, top_clean), fetchone=True)
    topic_id = t_row["topic_id"]

    db_write("""
        INSERT INTO questions (paper_id, topic_id, question_number, marks, crop_image_path, ai_status)
        VALUES (?, ?, ?, 0.0, ?, 'COMPLETED')
        ON CONFLICT(paper_id, question_number) DO UPDATE SET
            topic_id = excluded.topic_id,
            crop_image_path = excluded.crop_image_path,
            ai_status = 'COMPLETED'
    """, (paper_id, topic_id, item.question_number, rel_img_path))

    return {"ok": True}


# --- GOOGLE #1 SEO ENGINE: ROBOTS.TXT & DYNAMIC SITEMAP.XML ---
@app.get("/robots.txt")
def serve_robots(request: Request):
    base_url = str(request.base_url).rstrip("/")
    content = f"""User-agent: *
Allow: /
Disallow: /admin

User-agent: Google-Extended
Allow: /
User-agent: GPTBot
Allow: /
User-agent: OAI-SearchBot
Allow: /

Sitemap: {base_url}/sitemap.xml
"""
    return Response(content=content, media_type="text/plain")


@app.get("/sitemap.xml")
def serve_sitemap(request: Request):
    base_url = str(request.base_url).rstrip("/")
    today = datetime.now().strftime("%Y-%m-%d")

    topics = db_read("""
        SELECT DISTINCT t.topic_id, s.subject_id
        FROM topics t
        JOIN subjects s ON t.subject_id = s.subject_id
        JOIN questions q ON t.topic_id = q.topic_id
        WHERE q.ai_status = 'COMPLETED'
    """)

    urls = [
        f"""  <url>
    <loc>{base_url}/</loc>
    <lastmod>{today}</lastmod>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>"""
    ]

    for t in topics:
        urls.append(f"""  <url>
    <loc>{base_url}/?subject={t['subject_id']}&amp;topic={t['topic_id']}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>""")

    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
</urlset>"""
    return Response(content=xml_content, media_type="application/xml")


def build_seo_injection(base_url: str) -> str:
    subjects = db_read("SELECT subject_name FROM subjects ORDER BY subject_name ASC")
    topics = db_read("SELECT topic_name FROM topics ORDER BY topic_name ASC LIMIT 120")
    exams = db_read("SELECT DISTINCT exam_name FROM examinations ORDER BY exam_name ASC LIMIT 60")
    stats = db_read("SELECT COUNT(*) as cnt FROM questions WHERE ai_status = 'COMPLETED'", fetchone=True)
    total_q = stats["cnt"] if stats else 0

    sub_names = ", ".join([s["subject_name"] for s in subjects])
    top_names = ", ".join([t["topic_name"] for t in topics])
    exam_names = ", ".join([e["exam_name"] for e in exams])

    schema_obj = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "EducationalOrganization",
                "name": "PYQ Plus",
                "alternateName": ["RPSC PYQ Plus", "PYQ Plus Official RPSC Archive"],
                "url": base_url,
                "description": f"Rajasthan RPSC Official Previous Year Question Papers (2011-2026) Subject-Wise & Topic-Wise Verified Digital Archive with {total_q}+ Official Questions.",
                "knowsAbout": [
                    "RPSC Previous Year Question Papers",
                    "RPSC Topic Wise PYQ",
                    "Rajasthan GK Previous Year Questions",
                    "RPSC 1st Grade School Lecturer PYQ",
                    "RPSC 2nd Grade Senior Teacher PYQ",
                    "RAS Previous Year Questions"
                ] + [s["subject_name"] for s in subjects]
            },
            {
                "@type": "Dataset",
                "name": "RPSC Official Previous Year Questions Topic-Wise Archive (2011–2026)",
                "description": f"Complete verified collection of {total_q}+ RPSC examination questions categorized by Subject ({sub_names}) and Micro-Topics.",
                "url": base_url,
                "keywords": [
                    "RPSC PYQ", "PYQ Plus", "RPSC Previous Year Questions Topic Wise",
                    "RPSC Old Papers Hindi", "Rajasthan GK Topic Wise PYQ"
                ],
                "creator": {"@type": "Organization", "name": "PYQ Plus"},
                "isAccessibleForFree": True
            }
        ]
    }

    seo_html = f"""
    {GOOGLE_SITE_VERIFICATION_META}
    <script type="application/ld+json">{json.dumps(schema_obj, ensure_ascii=False)}</script>
    <noscript>
        <div>
            <h1>PYQ Plus — RPSC Official Topic-Wise Previous Year Questions (2011-2026)</h1>
            <p>Subjects: {sub_names}</p>
            <p>Topics: {top_names}</p>
            <p>RPSC Official Exams Covered: {exam_names}</p>
        </div>
    </noscript>
    """
    return seo_html


# --- MAIN BRIGHT PROFESSIONAL 3D EDTECH INTERFACE (WITH OPTIONAL DARK MODE) ---
MAIN_HTML = """<!DOCTYPE html>
<html lang="hi" class="light">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PYQ Plus — RPSC PYQ Topic-Wise & Subject-Wise Official Archive (2011–2026)</title>
    <meta name="description" content="PYQ Plus: Rajasthan RPSC Official Previous Year Question Papers (2011-2026) Subject-Wise & Topic-Wise Verified Question Bank.">
    <meta name="keywords" content="PYQ Plus, RPSC PYQ, RPSC Previous Year Questions Topic Wise, RPSC Old Question Papers, Rajasthan GK PYQ Topic Wise">
    <meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
    <!-- __SEO_INJECTION__ -->
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    fontFamily: {
                        sans: ['Plus Jakarta Sans', 'Noto Sans Devanagari', 'sans-serif'],
                    }
                }
            }
        }
    </script>
    <!-- 3D Tilt Physics & Micro-Confetti CDN Libraries (No Local Install Needed) -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/vanilla-tilt/1.8.1/vanilla-tilt.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.9.2/dist/confetti.browser.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Noto+Sans+Devanagari:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        html { scroll-behavior: smooth; }
        body {
            font-family: 'Plus Jakarta Sans', 'Noto Sans Devanagari', sans-serif;
            background-color: #f8fafc;
            background-image: 
                radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.08) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.07) 0px, transparent 50%),
                radial-gradient(at 50% 100%, rgba(16, 185, 129, 0.06) 0px, transparent 50%);
            color: #0f172a;
            overflow-x: hidden;
            transition: background-color 0.35s ease, color 0.35s ease;
        }
        html.dark body {
            background-color: #0b1120;
            background-image: 
                radial-gradient(at 0% 0%, rgba(99, 102, 241, 0.15) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(236, 72, 153, 0.12) 0px, transparent 50%);
            color: #f8fafc;
        }

        /* 3D ELEVATED PROFESSIONAL SURFACES */
        .pro-surface {
            background: rgba(255, 255, 255, 0.88);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(226, 232, 240, 0.95);
            box-shadow: 0 10px 30px -10px rgba(15, 23, 42, 0.06), 0 4px 6px -2px rgba(15, 23, 42, 0.03);
        }
        html.dark .pro-surface {
            background: rgba(15, 23, 42, 0.82);
            border: 1px solid rgba(51, 65, 85, 0.8);
            box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.45);
        }

        /* 3D INTERACTIVE SUBJECT & STAT CARDS */
        .card-3d {
            background: linear-gradient(145deg, #ffffff, #f8fafc);
            border: 1px solid rgba(99, 102, 241, 0.16);
            box-shadow: 
                0 12px 24px -8px rgba(79, 70, 229, 0.08),
                0 2px 6px -1px rgba(15, 23, 42, 0.04),
                inset 0 1px 0 0 rgba(255, 255, 255, 0.9);
            transform-style: preserve-3d;
            transition: border-color 0.25s ease, box-shadow 0.25s ease;
        }
        .card-3d:hover {
            border-color: rgba(79, 70, 229, 0.55);
            box-shadow: 
                0 20px 38px -12px rgba(79, 70, 229, 0.2),
                0 6px 14px -3px rgba(79, 70, 229, 0.08);
        }
        html.dark .card-3d {
            background: linear-gradient(145deg, #1e293b, #0f172a);
            border: 1px solid rgba(99, 102, 241, 0.25);
            box-shadow: 0 12px 28px -8px rgba(0, 0, 0, 0.5);
        }
        html.dark .card-3d:hover {
            border-color: rgba(129, 140, 248, 0.7);
            box-shadow: 0 20px 38px -10px rgba(99, 102, 241, 0.35);
        }
        .pop-depth {
            transform: translateZ(24px);
        }

        /* EYE-CATCHING "COMING SOON ALL" BADGE */
        @keyframes neonPulse {
            0%, 100% {
                box-shadow: 0 4px 18px rgba(245, 158, 11, 0.35), 0 0 0 0 rgba(236, 72, 153, 0.3);
                transform: translateY(0px);
            }
            50% {
                box-shadow: 0 6px 24px rgba(236, 72, 153, 0.5), 0 0 0 4px rgba(245, 158, 11, 0.15);
                transform: translateY(-1.5px);
            }
        }
        @keyframes shimmerSweep {
            0% { transform: translateX(-150%) skewX(-20deg); }
            100% { transform: translateX(250%) skewX(-20deg); }
        }
        .coming-soon-badge {
            position: relative; overflow: hidden;
            background: linear-gradient(135deg, #f59e0b 0%, #ec4899 50%, #4f46e5 100%);
            padding: 2px; border-radius: 9999px;
            animation: neonPulse 2.6s infinite ease-in-out;
            cursor: pointer;
        }
        .coming-soon-inner {
            background: #ffffff;
            border-radius: 9999px;
            padding: 6px 16px;
            display: inline-flex; align-items: center; gap: 8px;
        }
        html.dark .coming-soon-inner {
            background: #0f172a;
        }
        .coming-soon-badge::after {
            content: ""; position: absolute; top: 0; left: 0; width: 45%; height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.7), transparent);
            animation: shimmerSweep 2.8s infinite;
        }
        .coming-soon-text {
            background: linear-gradient(90deg, #d97706, #db2777, #4f46e5);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 800; letter-spacing: 0.3px;
        }
        html.dark .coming-soon-text {
            background: linear-gradient(90deg, #fde68a, #f472b6, #a5b4fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        /* 3D ENTRY SPLASH OVERLAY (CLEAN BRIGHT 3D) */
        .intro-overlay {
            position: fixed; inset: 0; z-index: 50;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            background: radial-gradient(circle at center, #ffffff 0%, #eef2ff 100%);
            transition: opacity 0.7s ease, transform 0.7s cubic-bezier(0.16, 1, 0.3, 1);
        }
        html.dark .intro-overlay {
            background: radial-gradient(circle at center, #1e293b 0%, #090d16 100%);
        }
        .intro-hidden {
            opacity: 0; transform: scale(1.08); pointer-events: none;
        }

        /* "WAIT..." LOADING SPINNER */
        @keyframes spinOuter { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        @keyframes spinInner { 0% { transform: rotate(360deg); } 100% { transform: rotate(0deg); } }
        .wait-ring-outer {
            width: 72px; height: 72px; border-radius: 50%;
            border: 4px solid rgba(79, 70, 229, 0.15);
            border-top-color: #4f46e5; border-right-color: #ec4899;
            animation: spinOuter 0.85s cubic-bezier(0.5, 0.1, 0.5, 0.9) infinite;
        }
        .wait-ring-inner {
            position: absolute; width: 48px; height: 48px; border-radius: 50%;
            border: 4px solid rgba(245, 158, 11, 0.18);
            border-bottom-color: #f59e0b; border-left-color: #10b981;
            animation: spinInner 0.7s linear infinite;
        }

        /* POINT #6: EXAM NAME CLICK -> SLIDE-UP DATE & SHIFT REVEAL */
        .exam-meta-drawer {
            max-height: 0;
            opacity: 0;
            overflow: hidden;
            transform: translateY(8px) scale(0.97);
            transition: all 0.32s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        .exam-meta-drawer.open {
            max-height: 80px;
            opacity: 1;
            transform: translateY(0) scale(1);
            margin-bottom: 10px;
        }

        /* POINT #7: PURE ICON STAR ANIMATION (NO TEXT) */
        @keyframes starPop {
            0% { transform: scale(1) rotate(0deg); }
            35% { transform: scale(1.42) rotate(-18deg); }
            70% { transform: scale(0.9) rotate(12deg); }
            100% { transform: scale(1) rotate(0deg); }
        }
        .star-icon-btn {
            width: 44px; height: 44px;
            border-radius: 14px;
            display: inline-flex; align-items: center; justify-content: center;
            transition: all 0.25s cubic-bezier(0.34, 1.56, 0.64, 1);
            position: relative;
        }
        .star-icon-btn:hover {
            transform: translateY(-2px) scale(1.08);
        }
        .star-icon-btn.active-star {
            background: linear-gradient(135deg, #fef3c7, #fde68a);
            border: 1px solid #f59e0b;
            box-shadow: 0 6px 18px -4px rgba(245, 158, 11, 0.45);
        }
        html.dark .star-icon-btn.active-star {
            background: linear-gradient(135deg, rgba(245, 158, 11, 0.25), rgba(217, 119, 6, 0.35));
            border: 1px solid #fbbf24;
        }
        .star-pop-anim {
            animation: starPop 0.55s cubic-bezier(0.34, 1.56, 0.64, 1);
        }

        @keyframes cardFadeUp {
            from { opacity: 0; transform: translateY(14px); }
            to { opacity: 1; transform: translateY(0); }
        }
        .question-card-anim {
            animation: cardFadeUp 0.32s cubic-bezier(0.16, 1, 0.3, 1) forwards;
        }
    </style>
</head>
<body class="min-h-screen relative">

    <!-- 3D Entry Splash Screen -->
    <div id="intro-screen" class="intro-overlay">
        <div class="text-center px-6 max-w-2xl">
            <div onclick="openComingSoonModal()" class="coming-soon-badge inline-block mb-6">
                <div class="coming-soon-inner">
                    <span class="relative flex h-3 w-3">
                        <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
                        <span class="relative inline-flex rounded-full h-3 w-3 bg-amber-500"></span>
                    </span>
                    <span class="text-lg">🚀</span>
                    <span class="coming-soon-text text-sm sm:text-base">Coming Soon All</span>
                </div>
            </div>

            <div class="w-20 h-20 mx-auto mb-5 rounded-3xl bg-gradient-to-br from-indigo-600 via-violet-600 to-fuchsia-600 flex items-center justify-center text-white font-extrabold text-4xl shadow-2xl shadow-indigo-500/30">
                P+
            </div>

            <h1 class="text-5xl md:text-7xl font-extrabold tracking-tight bg-gradient-to-r from-indigo-700 via-violet-600 to-fuchsia-600 dark:from-white dark:via-indigo-200 dark:to-indigo-400 bg-clip-text text-transparent mb-4">
                PYQ Plus
            </h1>
            <p class="text-slate-600 dark:text-slate-300 text-lg md:text-xl mb-8 font-medium">
                Rajasthan RPSC Official Previous Year Questions ka Subject-Wise aur Topic-Wise Smart 3D Sankalan
            </p>
            <button onclick="enterPortal()" class="px-9 py-4 rounded-2xl bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white font-extrabold text-lg shadow-xl shadow-indigo-600/30 transition transform hover:scale-105 active:scale-95">
                🚀 Start Smart Preparation
            </button>
        </div>
    </div>

    <!-- AESTHETIC "WAIT..." LOADING MODAL OVERLAY -->
    <div id="wait-loader" class="fixed inset-0 z-50 hidden items-center justify-center bg-slate-900/40 dark:bg-slate-950/75 backdrop-blur-md transition-opacity duration-300">
        <div class="pro-surface rounded-3xl px-10 py-8 shadow-2xl flex flex-col items-center text-center max-w-xs w-full mx-4">
            <div class="relative flex items-center justify-center mb-4">
                <div class="wait-ring-outer"></div>
                <div class="wait-ring-inner"></div>
                <span class="absolute text-lg">⚡</span>
            </div>
            <div class="text-2xl font-extrabold tracking-wider text-indigo-600 dark:text-indigo-400 flex items-center gap-1">
                <span>Wait</span>
                <span class="inline-flex gap-1 ml-1">
                    <span class="w-2 h-2 rounded-full bg-indigo-500 animate-bounce" style="animation-delay: 0ms;"></span>
                    <span class="w-2 h-2 rounded-full bg-pink-500 animate-bounce" style="animation-delay: 150ms;"></span>
                    <span class="w-2 h-2 rounded-full bg-amber-500 animate-bounce" style="animation-delay: 300ms;"></span>
                </span>
            </div>
            <p id="wait-subtext" class="text-xs text-slate-500 dark:text-slate-300 mt-2 font-semibold">
                Sawaal load ho rahe hain...
            </p>
        </div>
    </div>

    <div class="relative z-10 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pb-20">
        
        <!-- POINT #1: TOP NAVBAR FIXED AT THE TOP OF THE PAGE (DOES NOT FOLLOW SCROLL DOWN) -->
        <header class="pro-surface relative rounded-3xl px-5 py-4 mt-5 mb-8 flex flex-wrap items-center justify-between gap-4">
            <div class="flex items-center gap-4 flex-wrap">
                <div onclick="showHome()" class="flex items-center gap-3 cursor-pointer group">
                    <div class="w-11 h-11 rounded-2xl bg-gradient-to-br from-indigo-600 to-violet-600 flex items-center justify-center font-extrabold text-xl text-white shadow-lg shadow-indigo-500/25 group-hover:scale-105 transition">
                        P+
                    </div>
                    <div>
                        <span class="text-2xl font-extrabold tracking-tight text-slate-900 dark:text-white">PYQ <span class="text-indigo-600 dark:text-indigo-400">Plus</span></span>
                        <span class="block text-[11px] font-bold text-slate-400 dark:text-slate-400 -mt-1">RPSC Official Archive</span>
                    </div>
                </div>

                <div onclick="openComingSoonModal()" class="coming-soon-badge" title="Click karke upcoming updates dekhein">
                    <div class="coming-soon-inner">
                        <span class="relative flex h-2.5 w-2.5">
                            <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
                            <span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-500"></span>
                        </span>
                        <span class="text-base">✨</span>
                        <span class="coming-soon-text text-xs sm:text-sm">Coming Soon All</span>
                    </div>
                </div>
            </div>

            <div class="flex items-center gap-2.5 flex-wrap">
                <button onclick="showHome()" class="px-4 py-2.5 rounded-xl text-sm font-bold bg-slate-100 hover:bg-indigo-50 text-slate-700 hover:text-indigo-600 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-slate-700 transition">
                    📚 Subjects
                </button>
                <button onclick="openStarredTab()" class="px-4 py-2.5 rounded-xl text-sm font-bold bg-amber-50 hover:bg-amber-100 text-amber-700 border border-amber-200 dark:bg-amber-500/15 dark:text-amber-300 dark:border-amber-500/30 transition flex items-center gap-1.5">
                    ⭐ Saved
                </button>
                <!-- POINT #2: SEPARATE OPTIONAL DARK MODE TOGGLE BUTTON -->
                <button id="theme-toggle-btn" onclick="toggleThemeMode()" class="w-10 h-10 rounded-xl bg-slate-100 hover:bg-slate-200 dark:bg-slate-800 dark:hover:bg-slate-700 text-slate-700 dark:text-amber-300 flex items-center justify-center text-lg transition" title="Dark / Light Mode Badlein">
                    🌙
                </button>
                <button id="auth-btn" onclick="openLoginModal()" class="px-4 py-2.5 rounded-xl text-sm font-bold bg-indigo-600 hover:bg-indigo-500 text-white transition shadow-md shadow-indigo-600/20">
                    👤 Login
                </button>
            </div>
        </header>

        <!-- Hero Stats Banner with 3D Depth Cards -->
        <section id="hero-banner" class="pro-surface rounded-3xl p-8 mb-10 text-center relative overflow-hidden">
            <div class="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-indigo-50 border border-indigo-200 text-indigo-700 dark:bg-amber-500/15 dark:border-amber-400/30 dark:text-amber-300 text-xs sm:text-sm font-bold mb-4">
                🔥 2011 Official Papers Live • Aage ke Sabhi Years Step-by-Step Add Ho Rahe Hain!
            </div>
            <h2 class="text-3xl md:text-4xl font-extrabold text-slate-900 dark:text-white mb-2 tracking-tight">
                🏛️ RPSC Topic-Wise Verified Question Bank
            </h2>
            <p class="text-slate-500 dark:text-slate-400 max-w-2xl mx-auto mb-7 text-sm sm:text-base font-medium">
                Apना Subject chunein aur Topic-wise purane sawalon ki practice karein. Kisi bhi sawal ke Exam naam par click karke uski Date & Shift dekhein.
            </p>
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-5 max-w-3xl mx-auto">
                <div class="card-3d rounded-2xl p-5" data-tilt data-tilt-max="8" data-tilt-speed="400" data-tilt-glare data-tilt-max-glare="0.15">
                    <div class="pop-depth">
                        <div class="text-slate-400 dark:text-slate-400 text-xs uppercase font-extrabold tracking-wider">Kul Subjects</div>
                        <div id="stat-subs" class="text-3xl font-extrabold text-indigo-600 dark:text-indigo-400 mt-1">-</div>
                    </div>
                </div>
                <div class="card-3d rounded-2xl p-5" data-tilt data-tilt-max="8" data-tilt-speed="400" data-tilt-glare data-tilt-max-glare="0.15">
                    <div class="pop-depth">
                        <div class="text-slate-400 dark:text-slate-400 text-xs uppercase font-extrabold tracking-wider">Verified Topics</div>
                        <div id="stat-tops" class="text-3xl font-extrabold text-emerald-600 dark:text-emerald-400 mt-1">-</div>
                    </div>
                </div>
                <div class="card-3d rounded-2xl p-5" data-tilt data-tilt-max="8" data-tilt-speed="400" data-tilt-glare data-tilt-max-glare="0.15">
                    <div class="pop-depth">
                        <div class="text-slate-400 dark:text-slate-400 text-xs uppercase font-extrabold tracking-wider">Official Questions</div>
                        <div id="stat-qs" class="text-3xl font-extrabold text-amber-600 dark:text-amber-400 mt-1">-</div>
                    </div>
                </div>
            </div>
        </section>

        <!-- STEP 1: 3D SUBJECTS GRID -->
        <section id="subjects-section">
            <div class="flex items-center justify-between mb-5">
                <h3 class="text-xl font-extrabold text-slate-900 dark:text-white flex items-center gap-2">
                    <span class="w-7 h-7 rounded-lg bg-indigo-600 text-white text-sm inline-flex items-center justify-center">1</span>
                    Subject Chunein
                </h3>
            </div>
            <div id="subjects-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5"></div>
        </section>

        <!-- STEP 2: TOPICS DRAWER -->
        <section id="topics-section" class="hidden mt-8">
            <div class="pro-surface rounded-3xl p-7">
                <div class="flex flex-wrap items-center justify-between gap-4 mb-5 pb-4 border-b border-slate-200 dark:border-slate-800">
                    <div>
                        <span class="text-xs uppercase tracking-wider text-indigo-600 dark:text-indigo-400 font-extrabold">Chuna Gaya Subject</span>
                        <h3 id="active-subject-title" class="text-2xl font-extrabold text-slate-900 dark:text-white mt-0.5"></h3>
                    </div>
                    <button onclick="showHome()" class="text-sm font-bold text-slate-600 hover:text-indigo-600 dark:text-slate-300 dark:hover:text-white px-4 py-2 rounded-xl bg-slate-100 hover:bg-indigo-50 dark:bg-slate-800 transition">
                        ← Subject Badlein
                    </button>
                </div>
                <p class="text-sm text-slate-500 dark:text-slate-400 font-semibold mb-4">2️⃣ Ab niche diye gaye kisi bhi Topic par click karein:</p>
                <div id="topics-grid" class="flex flex-wrap gap-3"></div>
            </div>
        </section>

        <!-- STEP 3: QUESTIONS VIEW WITH 50-50 TABS -->
        <section id="questions-section" class="hidden mt-8">
            <div class="pro-surface rounded-3xl p-6 mb-6 border-l-4 border-l-indigo-600 flex flex-wrap items-center justify-between gap-4">
                <div>
                    <div id="q-header-sub" class="text-xs font-extrabold uppercase tracking-wider text-indigo-600 dark:text-indigo-400"></div>
                    <div class="flex items-center gap-3 mt-1 flex-wrap">
                        <h2 id="q-header-top" class="text-2xl md:text-3xl font-extrabold text-slate-900 dark:text-white"></h2>
                        <span id="q-header-count" class="text-xs font-extrabold px-3.5 py-1 rounded-full bg-indigo-50 text-indigo-700 border border-indigo-200 dark:bg-indigo-500/20 dark:text-indigo-300 dark:border-indigo-500/30"></span>
                    </div>
                </div>
                <button onclick="showHome()" class="text-xs sm:text-sm font-bold px-4 py-2 rounded-xl bg-slate-100 hover:bg-slate-200 text-slate-700 dark:bg-slate-800 dark:text-slate-200 transition">
                    📚 Sabhi Subjects
                </button>
            </div>

            <div id="batch-tabs" class="flex flex-wrap gap-2.5 mb-6"></div>
            <div id="questions-window-wrapper" class="relative"></div>
            <div id="batch-tabs-bottom" class="flex flex-wrap gap-2.5 mt-8 justify-center"></div>
        </section>

    </div>

    <!-- COMING SOON MODAL -->
    <div id="coming-soon-modal" class="fixed inset-0 z-50 hidden items-center justify-center bg-slate-900/60 backdrop-blur-md p-4">
        <div class="pro-surface rounded-3xl max-w-md w-full p-8 shadow-2xl relative text-center">
            <button onclick="closeComingSoonModal()" class="absolute top-5 right-5 text-slate-400 hover:text-slate-700 dark:hover:text-white text-xl">✕</button>
            <div class="w-16 h-16 mx-auto rounded-2xl bg-gradient-to-br from-amber-500/20 to-pink-500/20 border border-amber-400/50 flex items-center justify-center text-3xl mb-4 shadow-md">
                🚀
            </div>
            <div class="inline-block px-3 py-1 rounded-full bg-amber-100 text-amber-800 dark:bg-amber-500/20 dark:text-amber-300 text-xs font-extrabold mb-2">
                ✨ Coming Soon All
            </div>
            <h3 class="text-2xl font-extrabold text-slate-900 dark:text-white mb-2">Naye Papers & Years Lagatar Add Ho Rahe Hain!</h3>
            <p class="text-sm text-slate-600 dark:text-slate-300 leading-relaxed mb-6">
                Abhi <b>2011</b> ke sabhi RPSC papers Subject-Wise aur Topic-Wise live kar diye gaye hain. 
                <b>2012 se 2026</b> tak ke baaki sabhi RPSC question papers step-by-step jaldi hi isi portal par add ho jayenge!
            </p>
            <div class="grid grid-cols-2 gap-3 text-left mb-6">
                <div class="p-3 rounded-xl bg-emerald-50 border border-emerald-200 dark:bg-emerald-500/15 dark:border-emerald-500/30">
                    <div class="text-xs text-emerald-700 dark:text-emerald-300 font-bold">✅ 2011 Archive</div>
                    <div class="text-sm font-extrabold text-slate-900 dark:text-white mt-0.5">100% Live Now</div>
                </div>
                <div class="p-3 rounded-xl bg-amber-50 border border-amber-200 dark:bg-amber-500/15 dark:border-amber-500/30">
                    <div class="text-xs text-amber-700 dark:text-amber-300 font-bold">⏳ 2012 – 2026</div>
                    <div class="text-sm font-extrabold text-slate-900 dark:text-white mt-0.5">Coming Soon...</div>
                </div>
            </div>
            <button onclick="closeComingSoonModal()" class="w-full py-3.5 rounded-xl bg-gradient-to-r from-amber-500 to-pink-600 hover:from-amber-400 hover:to-pink-500 text-white font-bold shadow-lg transition">
                👍 Samajh Gaya, Padhai Shuru Karein!
            </button>
        </div>
    </div>

    <!-- LOGIN MODAL -->
    <div id="login-modal" class="fixed inset-0 z-50 hidden items-center justify-center bg-slate-900/60 backdrop-blur-md p-4">
        <div class="pro-surface rounded-3xl max-w-md w-full p-8 shadow-2xl relative">
            <button onclick="closeLoginModal()" class="absolute top-5 right-5 text-slate-400 hover:text-slate-700 dark:hover:text-white text-xl">✕</button>
            <div class="w-12 h-12 rounded-2xl bg-indigo-50 border border-indigo-200 dark:bg-indigo-600/30 dark:border-indigo-400/40 flex items-center justify-center text-2xl mb-4">
                ⭐
            </div>
            <h3 class="text-2xl font-extrabold text-slate-900 dark:text-white mb-1">PYQ Plus Account</h3>
            <p class="text-sm text-slate-500 dark:text-slate-400 mb-6">
                Apne mahatvapurna sawalon ko ⭐ Star karke save rakhne ke liye login ya naya account banayein.
            </p>
            <div class="space-y-4">
                <div>
                    <label class="text-xs font-bold text-slate-700 dark:text-slate-300">Aapka Naam</label>
                    <input id="auth-name" type="text" placeholder="Jaise: Bhuvi" class="w-full mt-1 px-4 py-3 rounded-xl bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 text-slate-900 dark:text-white focus:outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-700 dark:text-slate-300">Mobile Number ya Email</label>
                    <input id="auth-id" type="text" placeholder="Mobile no. ya Email" class="w-full mt-1 px-4 py-3 rounded-xl bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 text-slate-900 dark:text-white focus:outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="text-xs font-bold text-slate-700 dark:text-slate-300">Password</label>
                    <input id="auth-pass" type="password" placeholder="••••••••" class="w-full mt-1 px-4 py-3 rounded-xl bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 text-slate-900 dark:text-white focus:outline-none focus:border-indigo-500">
                </div>
                <div id="auth-err" class="text-xs text-rose-600 dark:text-rose-400 font-semibold hidden"></div>
                <button onclick="submitAuth()" class="w-full py-3.5 rounded-xl bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white font-bold shadow-lg transition">
                    ✨ Continue & Save Questions
                </button>
            </div>
        </div>
    </div>

    <script>
        // --- 1. THEME MODE (DEFAULT BRIGHT LIGHT MODE + OPTIONAL DARK MODE) ---
        function applyTheme(mode) {
            const root = document.documentElement;
            const btn = document.getElementById('theme-toggle-btn');
            if (mode === 'dark') {
                root.classList.add('dark');
                root.classList.remove('light');
                if (btn) btn.innerText = '☀️';
            } else {
                root.classList.remove('dark');
                root.classList.add('light');
                if (btn) btn.innerText = '🌙';
            }
        }
        const savedTheme = localStorage.getItem('pyq_theme') || 'light';
        applyTheme(savedTheme);

        function toggleThemeMode() {
            const isDark = document.documentElement.classList.contains('dark');
            const nextMode = isDark ? 'light' : 'dark';
            localStorage.setItem('pyq_theme', nextMode);
            applyTheme(nextMode);
        }

        function init3DTilt() {
            if (window.VanillaTilt) {
                VanillaTilt.init(document.querySelectorAll("[data-tilt]"), {
                    max: 8,
                    speed: 400,
                    glare: true,
                    "max-glare": 0.14
                });
            }
        }

        function enterPortal() {
            document.getElementById('intro-screen').classList.add('intro-hidden');
            sessionStorage.setItem('pyq_entered', '1');
        }
        if (sessionStorage.getItem('pyq_entered') === '1') {
            document.getElementById('intro-screen').classList.add('intro-hidden');
        }

        function openComingSoonModal() {
            document.getElementById('coming-soon-modal').classList.remove('hidden');
            document.getElementById('coming-soon-modal').classList.add('flex');
        }
        function closeComingSoonModal() {
            document.getElementById('coming-soon-modal').classList.add('hidden');
            document.getElementById('coming-soon-modal').classList.remove('flex');
        }

        // --- 2. SILENT VISITOR TRACKER ---
        let visitorId = localStorage.getItem('pyq_visitor_id');
        if (!visitorId) {
            visitorId = 'v_' + Math.random().toString(36).substring(2, 12) + '_' + Date.now();
            localStorage.setItem('pyq_visitor_id', visitorId);
        }
        function sendHeartbeat(isNewView = false) {
            if (document.visibilityState === 'visible') {
                fetch('/api/heartbeat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ visitor_id: visitorId, add_seconds: isNewView ? 2 : 10, is_new_view: isNewView })
                }).catch(() => {});
            }
        }
        sendHeartbeat(true);
        setInterval(() => sendHeartbeat(false), 10000);

        // --- 3. STATE MANAGEMENT ---
        let currentUser = JSON.parse(localStorage.getItem('pyq_user') || 'null');
        let pendingStarQuestionId = null;
        let currentSubjectName = '';
        let currentTopicName = '';
        let currentQuestions = [];
        let activeBatchIndex = 0;
        const BATCH_SIZE = 50;
        let renderedPanes = {};

        function showWaitLoader(show, message = 'Sawaal load ho rahe hain...') {
            const el = document.getElementById('wait-loader');
            const sub = document.getElementById('wait-subtext');
            if (sub) sub.innerText = message;
            if (show) {
                el.classList.remove('hidden');
                el.classList.add('flex');
            } else {
                el.classList.add('hidden');
                el.classList.remove('flex');
            }
        }

        function preloadImagesList(qList, maxWaitMs = 550) {
            return new Promise((resolve) => {
                if (!qList || qList.length === 0) return resolve();
                let settled = false;
                const done = () => {
                    if (!settled) { settled = true; resolve(); }
                };
                setTimeout(done, maxWaitMs);
                const firstFew = qList.slice(0, 6);
                let loadedCount = 0;
                firstFew.forEach((q) => {
                    const img = new Image();
                    img.onload = img.onerror = () => {
                        loadedCount++;
                        if (loadedCount >= firstFew.length) done();
                    };
                    img.src = q.crop_image_path;
                });
            });
        }

        function updateAuthUI() {
            const btn = document.getElementById('auth-btn');
            if (currentUser) {
                btn.innerText = '👤 ' + currentUser.full_name;
                btn.onclick = () => {
                    if (confirm('Kya aap logout karna chahte hain?')) {
                        localStorage.removeItem('pyq_user');
                        currentUser = null;
                        updateAuthUI();
                    }
                };
            } else {
                btn.innerText = '👤 Login';
                btn.onclick = () => openLoginModal();
            }
        }
        updateAuthUI();

        // --- 4. LOAD SUBJECTS & 3D CARDS ---
        async function loadInitData() {
            const res = await fetch('/api/init');
            const data = await res.json();
            document.getElementById('stat-subs').innerText = data.stats.total_subs;
            document.getElementById('stat-tops').innerText = data.stats.total_tops;
            document.getElementById('stat-qs').innerText = data.stats.total_qs;

            const grid = document.getElementById('subjects-grid');
            grid.innerHTML = '';
            data.subjects.forEach((s) => {
                const card = document.createElement('div');
                card.className = 'card-3d rounded-3xl p-6 cursor-pointer flex items-center justify-between group';
                card.setAttribute('data-tilt', '');
                card.onclick = () => selectSubject(s.subject_id, s.subject_name);
                card.innerHTML = `
                    <div class="flex items-center gap-4 pop-depth">
                        <div class="w-13 h-13 p-3.5 rounded-2xl bg-indigo-50 border border-indigo-100 dark:bg-indigo-500/20 dark:border-indigo-400/30 flex items-center justify-center text-2xl shadow-sm group-hover:scale-110 transition">
                            📘
                        </div>
                        <div>
                            <h4 class="text-lg font-extrabold text-slate-900 dark:text-white group-hover:text-indigo-600 dark:group-hover:text-indigo-400 transition">${s.subject_name}</h4>
                            <span class="text-xs font-bold text-indigo-600 dark:text-indigo-400">Topics dekhein →</span>
                        </div>
                    </div>
                `;
                grid.appendChild(card);
            });
            init3DTilt();
        }
        loadInitData();

        function showHome() {
            document.getElementById('subjects-section').classList.remove('hidden');
            document.getElementById('topics-section').classList.add('hidden');
            document.getElementById('questions-section').classList.add('hidden');
        }

        async function selectSubject(subId, subName) {
            showWaitLoader(true, 'Topics load ho rahe hain...');
            currentSubjectName = subName;
            document.getElementById('active-subject-title').innerText = subName;

            const res = await fetch(`/api/topics/${subId}`);
            const data = await res.json();

            const tGrid = document.getElementById('topics-grid');
            tGrid.innerHTML = '';
            data.topics.forEach(t => {
                const pill = document.createElement('button');
                pill.className = 'px-5 py-3.5 rounded-2xl card-3d text-left font-bold text-slate-800 dark:text-slate-100 hover:text-indigo-600 dark:hover:text-indigo-400 transition flex items-center gap-2';
                pill.innerHTML = `<span class="text-indigo-600 dark:text-indigo-400">🎯</span> <span>${t.topic_name}</span>`;
                pill.onclick = () => selectTopic(t.topic_id, t.topic_name);
                tGrid.appendChild(pill);
            });

            document.getElementById('subjects-section').classList.add('hidden');
            document.getElementById('topics-section').classList.remove('hidden');
            document.getElementById('questions-section').classList.add('hidden');
            showWaitLoader(false);
        }

        async function selectTopic(topId, topName) {
            showWaitLoader(true, 'Sawaal tayyar ho rahe hain...');
            currentTopicName = topName;
            const uid = currentUser ? currentUser.user_id : 0;

            const res = await fetch(`/api/questions/${topId}?user_id=${uid}`);
            const data = await res.json();
            currentQuestions = data.questions;
            activeBatchIndex = 0;

            document.getElementById('q-header-sub').innerText = '📚 Subject: ' + currentSubjectName;
            document.getElementById('q-header-top').innerText = '🎯 Topic: ' + currentTopicName;
            document.getElementById('q-header-count').innerText = `Kul ${currentQuestions.length} Questions`;

            resetWindowAndActivateBatch(0);

            const firstBatchSlice = currentQuestions.slice(0, BATCH_SIZE);
            await preloadImagesList(firstBatchSlice, 550);

            showWaitLoader(false);
            document.getElementById('questions-section').classList.remove('hidden');
            document.getElementById('questions-section').scrollIntoView({ behavior: 'smooth' });
        }

        // POINT #6: TOGGLE DATE & SHIFT DRAWER WHEN OFFICIAL EXAM NAME IS CLICKED
        function toggleExamDetails(qId) {
            const drawer = document.getElementById(`exam-drawer-${qId}`);
            if (drawer) {
                drawer.classList.toggle('open');
            }
        }

        // POINT #7: SVG STAR ICON GENERATOR (NO TEXT BESIDE STAR)
        function getStarSVG(isStarred) {
            if (isStarred) {
                return `<svg class="w-6 h-6 text-amber-500 fill-amber-400 drop-shadow-sm" viewBox="0 0 24 24" stroke="currentColor" stroke-width="1.8">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" />
                </svg>`;
            } else {
                return `<svg class="w-6 h-6 text-slate-400 hover:text-amber-500 transition-colors" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
                    <path stroke-linecap="round" stroke-linejoin="round" d="M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" />
                </svg>`;
            }
        }

        function buildBatchDOMPane(batchIdx) {
            const wrapper = document.getElementById('questions-window-wrapper');
            if (renderedPanes[batchIdx]) return renderedPanes[batchIdx];

            const pane = document.createElement('div');
            pane.id = `batch-pane-${batchIdx}`;
            pane.className = 'space-y-6 hidden';

            const slice = currentQuestions.slice(batchIdx * BATCH_SIZE, (batchIdx + 1) * BATCH_SIZE);
            slice.forEach((q) => {
                const preImg = new Image();
                preImg.src = q.crop_image_path;

                const seoAlt = `RPSC ${q.exam_name} (${q.exam_date}) - ${currentSubjectName} - ${currentTopicName} Question ${q.question_number} | PYQ Plus`;

                const card = document.createElement('div');
                card.className = 'pro-surface rounded-3xl p-5 sm:p-6 question-card-anim';
                card.innerHTML = `
                    <!-- POINT #6: HIDDEN DATE & SHIFT DRAWER (SLIDES UP WHEN EXAM NAME IS CLICKED) -->
                    <div id="exam-drawer-${q.question_id}" class="exam-meta-drawer">
                        <div class="inline-flex flex-wrap items-center gap-2 px-3.5 py-2 rounded-2xl bg-slate-100 dark:bg-slate-800/90 border border-slate-200 dark:border-slate-700 text-xs font-bold">
                            <span class="px-2.5 py-1 rounded-lg bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border border-emerald-500/25">
                                📅 Date: ${q.exam_date}
                            </span>
                            <span class="px-2.5 py-1 rounded-lg bg-amber-500/15 text-amber-700 dark:text-amber-300 border border-amber-500/25">
                                ⏰ Shift: ${q.exam_shift}
                            </span>
                        </div>
                    </div>

                    <!-- TOP BAR: ONLY OFFICIAL EXAM NAME + PURE STAR ICON ANIMATION -->
                    <div class="flex items-center justify-between gap-3 mb-4">
                        <button onclick="toggleExamDetails(${q.question_id})" 
                            class="group text-left px-4 py-2 rounded-2xl bg-indigo-50/90 hover:bg-indigo-100 dark:bg-indigo-500/15 dark:hover:bg-indigo-500/25 text-indigo-700 dark:text-indigo-300 border border-indigo-200/80 dark:border-indigo-500/30 transition flex items-center gap-2 shadow-sm"
                            title="Date aur Shift dekhne ke liye click karein">
                            <span class="text-base">🏛️</span>
                            <span class="text-xs sm:text-sm font-extrabold tracking-tight">${q.exam_name}</span>
                            <span class="text-[10px] px-1.5 py-0.5 rounded-md bg-white/80 dark:bg-slate-800 text-slate-500 dark:text-slate-400 font-bold group-hover:text-indigo-600 transition">▾ Info</span>
                        </button>

                        <!-- POINT #7: ONLY STAR ICON & ANIMATION (NO TEXT) -->
                        <button id="star-btn-${q.question_id}" onclick="handleStarClick(${q.question_id}, event)"
                            aria-label="Save Question"
                            class="star-icon-btn ${
                                q.is_starred
                                    ? 'active-star'
                                    : 'bg-slate-100 hover:bg-amber-50/80 dark:bg-slate-800 border border-slate-200 dark:border-slate-700'
                            }">
                            ${getStarSVG(q.is_starred)}
                        </button>
                    </div>

                    <div class="bg-white rounded-2xl p-3 sm:p-4 overflow-hidden border border-slate-200/80 shadow-sm">
                        <img src="${q.crop_image_path}" alt="${seoAlt}" class="w-full h-auto object-contain mx-auto" decoding="async">
                    </div>
                `;
                pane.appendChild(card);
            });

            const totalBatches = Math.ceil(currentQuestions.length / BATCH_SIZE);
            if (batchIdx + 1 < totalBatches) {
                const nextStart = (batchIdx + 1) * BATCH_SIZE + 1;
                const nextEnd = Math.min((batchIdx + 2) * BATCH_SIZE, currentQuestions.length);
                const nextBanner = document.createElement('div');
                nextBanner.className = 'pro-surface rounded-2xl p-5 text-center flex flex-wrap items-center justify-between gap-4';
                nextBanner.innerHTML = `
                    <span class="text-sm text-slate-600 dark:text-slate-300 font-bold">✅ Is tab ke sawal poore hue. Agle tab ke sawal pehle se ready hain!</span>
                    <button onclick="switchBatchTab(${batchIdx + 1})" class="px-5 py-2.5 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-sm shadow-lg transition">
                        Agla Tab: Sawaal ${nextStart} se ${nextEnd} →
                    </button>
                `;
                pane.appendChild(nextBanner);
            }

            wrapper.appendChild(pane);
            renderedPanes[batchIdx] = pane;
            return pane;
        }

        function syncTwoTabWindow(activeIdx) {
            const totalBatches = Math.ceil(currentQuestions.length / BATCH_SIZE);
            const allowedIndices = new Set([activeIdx]);
            if (activeIdx + 1 < totalBatches) {
                allowedIndices.add(activeIdx + 1);
            }

            Object.keys(renderedPanes).forEach((key) => {
                const idx = parseInt(key);
                if (!allowedIndices.has(idx)) {
                    if (renderedPanes[idx] && renderedPanes[idx].parentNode) {
                        renderedPanes[idx].parentNode.removeChild(renderedPanes[idx]);
                    }
                    delete renderedPanes[idx];
                }
            });

            buildBatchDOMPane(activeIdx);
            Object.keys(renderedPanes).forEach((key) => {
                const idx = parseInt(key);
                if (idx === activeIdx) {
                    renderedPanes[idx].classList.remove('hidden');
                } else {
                    renderedPanes[idx].classList.add('hidden');
                }
            });

            if (activeIdx + 1 < totalBatches && !renderedPanes[activeIdx + 1]) {
                setTimeout(() => {
                    if (activeBatchIndex === activeIdx) {
                        buildBatchDOMPane(activeIdx + 1);
                        renderTabButtons();
                    }
                }, 80);
            }
        }

        function resetWindowAndActivateBatch(startIdx = 0) {
            const wrapper = document.getElementById('questions-window-wrapper');
            wrapper.innerHTML = '';
            renderedPanes = {};
            activeBatchIndex = startIdx;

            if (currentQuestions.length === 0) {
                document.getElementById('batch-tabs').innerHTML = '';
                document.getElementById('batch-tabs-bottom').innerHTML = '';
                wrapper.innerHTML = `<div class="pro-surface rounded-2xl p-8 text-center text-slate-500 font-semibold">Abhi koi question nahi hai.</div>`;
                return;
            }

            syncTwoTabWindow(activeBatchIndex);
            renderTabButtons();
        }

        async function switchBatchTab(targetIdx) {
            if (targetIdx === activeBatchIndex) return;

            const isAlreadyPreloaded = Boolean(renderedPanes[targetIdx]);
            if (!isAlreadyPreloaded) {
                showWaitLoader(true, 'Tab load ho raha hai...');
                const slice = currentQuestions.slice(targetIdx * BATCH_SIZE, (targetIdx + 1) * BATCH_SIZE);
                await preloadImagesList(slice, 400);
            }

            activeBatchIndex = targetIdx;
            syncTwoTabWindow(activeBatchIndex);
            renderTabButtons();
            showWaitLoader(false);
            document.getElementById('questions-section').scrollIntoView({ behavior: 'smooth' });
        }

        function renderTabButtons() {
            const topBar = document.getElementById('batch-tabs');
            const bottomBar = document.getElementById('batch-tabs-bottom');
            topBar.innerHTML = '';
            bottomBar.innerHTML = '';

            const totalBatches = Math.ceil(currentQuestions.length / BATCH_SIZE);
            if (totalBatches <= 1) return;

            for (let i = 0; i < totalBatches; i++) {
                const start = i * BATCH_SIZE + 1;
                const end = Math.min((i + 1) * BATCH_SIZE, currentQuestions.length);
                const isActive = (i === activeBatchIndex);
                const isPreloadedNext = (i === activeBatchIndex + 1 && Boolean(renderedPanes[i]));

                const createBtn = () => {
                    const btn = document.createElement('button');
                    btn.className = `px-4 py-2.5 rounded-xl text-sm font-bold transition flex items-center gap-2 ${
                        isActive
                            ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/25 ring-2 ring-indigo-400/40'
                            : 'pro-surface text-slate-700 dark:text-slate-300 hover:text-indigo-600'
                    }`;
                    btn.innerHTML = `
                        <span>📄 Sawaal ${start} se ${end}</span>
                        ${isPreloadedNext ? '<span class="w-2 h-2 rounded-full bg-emerald-500" title="Instant Ready"></span>' : ''}
                    `;
                    btn.onclick = () => switchBatchTab(i);
                    return btn;
                };

                topBar.appendChild(createBtn());
                bottomBar.appendChild(createBtn());
            }
        }

        // --- 5. PURE STAR ICON & CONFETTI BURST LOGIC ---
        function handleStarClick(qId, evt) {
            if (!currentUser) {
                pendingStarQuestionId = qId;
                openLoginModal();
                return;
            }
            toggleStarAPI(qId, evt);
        }

        async function toggleStarAPI(qId, evt) {
            const res = await fetch('/api/star', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_id: currentUser.user_id, question_id: qId })
            });
            const data = await res.json();
            if (data.ok) {
                const qObj = currentQuestions.find(item => item.question_id === qId);
                if (qObj) qObj.is_starred = data.starred;
                const btn = document.getElementById(`star-btn-${qId}`);
                if (btn) {
                    btn.className = `star-icon-btn star-pop-anim ${
                        data.starred
                            ? 'active-star'
                            : 'bg-slate-100 hover:bg-amber-50/80 dark:bg-slate-800 border border-slate-200 dark:border-slate-700'
                    }`;
                    btn.innerHTML = getStarSVG(data.starred);

                    // Golden Sparkle Burst when starred
                    if (data.starred && window.confetti && evt) {
                        const rect = btn.getBoundingClientRect();
                        confetti({
                            particleCount: 18,
                            spread: 55,
                            startVelocity: 18,
                            scalar: 0.7,
                            colors: ['#f59e0b', '#fbbf24', '#fef08a'],
                            origin: {
                                x: (rect.left + rect.width / 2) / window.innerWidth,
                                y: (rect.top + rect.height / 2) / window.innerHeight
                            }
                        });
                    }
                }
            }
        }

        function openLoginModal() {
            document.getElementById('login-modal').classList.remove('hidden');
            document.getElementById('login-modal').classList.add('flex');
        }

        function closeLoginModal() {
            document.getElementById('login-modal').classList.add('hidden');
            document.getElementById('login-modal').classList.remove('flex');
        }

        async function submitAuth() {
            const full_name = document.getElementById('auth-name').value;
            const identifier = document.getElementById('auth-id').value;
            const password = document.getElementById('auth-pass').value;
            const errEl = document.getElementById('auth-err');

            if (!identifier || !password) {
                errEl.innerText = 'Kripya Mobile/Email aur Password bharein.';
                errEl.classList.remove('hidden');
                return;
            }
            const res = await fetch('/api/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ full_name, identifier, password })
            });
            const data = await res.json();
            if (!data.ok) {
                errEl.innerText = data.msg || 'Error';
                errEl.classList.remove('hidden');
                return;
            }
            currentUser = data.user;
            localStorage.setItem('pyq_user', JSON.stringify(currentUser));
            updateAuthUI();
            closeLoginModal();

            if (pendingStarQuestionId) {
                toggleStarAPI(pendingStarQuestionId, null);
                pendingStarQuestionId = null;
            }
        }

        async function openStarredTab() {
            if (!currentUser) {
                openLoginModal();
                return;
            }
            showWaitLoader(true, 'Saved sawal load ho rahe hain...');
            const res = await fetch(`/api/starred/${currentUser.user_id}`);
            const data = await res.json();
            currentQuestions = data.questions;
            activeBatchIndex = 0;

            document.getElementById('subjects-section').classList.add('hidden');
            document.getElementById('topics-section').classList.add('hidden');

            document.getElementById('q-header-sub').innerText = '👤 ' + currentUser.full_name + ' ke Personal Notes';
            document.getElementById('q-header-top').innerText = '⭐ Saved Important Questions';
            document.getElementById('q-header-count').innerText = `Kul ${currentQuestions.length} Saved`;

            resetWindowAndActivateBatch(0);
            const firstSlice = currentQuestions.slice(0, BATCH_SIZE);
            await preloadImagesList(firstSlice, 500);

            showWaitLoader(false);
            document.getElementById('questions-section').classList.remove('hidden');
            document.getElementById('questions-section').scrollIntoView({ behavior: 'smooth' });
        }
    </script>
</body>
</html>
"""


# --- FOUNDER ANALYTICS DASHBOARD (/admin) ---
ADMIN_HTML = """<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#0b1220">
    <title>PYQ Plus — Admin Analytics</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { background: #0b1220; }
        .panel { background: linear-gradient(145deg, #121d30, #101827); }
        .chart-wrap { position: relative; height: 280px; }
        @media (max-width: 640px) { .chart-wrap { height: 230px; } }
    </style>
</head>
<body class="min-h-screen text-slate-100 font-sans">
    <main class="max-w-7xl mx-auto px-4 py-7 md:px-8 md:py-10">
        <header class="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between mb-8">
            <div>
                <p class="text-indigo-300 text-sm font-bold tracking-wide">PYQ PLUS · ADMIN</p>
                <h1 class="text-3xl md:text-4xl font-black mt-1">Site performance</h1>
                <p class="text-slate-400 mt-2">Content quality, paper coverage aur student engagement ka data-based overview.</p>
            </div>
            <div class="flex items-center gap-3">
                <span id="updated-at" class="text-xs text-slate-500">Data load ho raha hai…</span>
                <button id="refresh-button" type="button" class="rounded-xl border border-slate-700 px-4 py-2 text-sm font-semibold hover:bg-slate-800">Refresh</button>
                <a href="/" class="rounded-xl bg-indigo-500 px-4 py-2 text-sm font-bold hover:bg-indigo-400">Main site</a>
            </div>
        </header>

        <div id="error-banner" class="hidden mb-6 rounded-xl border border-rose-800 bg-rose-950/60 px-4 py-3 text-sm text-rose-200" role="alert"></div>

        <section aria-labelledby="content-title">
            <div class="mb-4">
                <h2 id="content-title" class="text-xl font-bold">Archive health</h2>
                <p class="text-sm text-slate-400 mt-1">Counts database ke current records se aate hain; “completed paper” ka matlab full paper scan status hai.</p>
            </div>
            <div class="grid grid-cols-2 xl:grid-cols-4 gap-3 md:gap-5">
                <article class="panel rounded-2xl border border-slate-800 p-4 md:p-5">
                    <p class="text-xs font-bold uppercase tracking-wide text-slate-400">Completed questions</p>
                    <p id="metric-completed-questions" class="mt-2 text-3xl font-black text-emerald-300">—</p>
                    <p class="mt-2 text-xs text-slate-500">AI classification status = COMPLETED</p>
                </article>
                <article class="panel rounded-2xl border border-slate-800 p-4 md:p-5">
                    <p class="text-xs font-bold uppercase tracking-wide text-slate-400">Scanned papers</p>
                    <p id="metric-completed-papers" class="mt-2 text-3xl font-black text-indigo-300">—</p>
                    <p id="metric-paper-coverage" class="mt-2 text-xs text-slate-500">Full-scan papers / all papers</p>
                </article>
                <article class="panel rounded-2xl border border-slate-800 p-4 md:p-5">
                    <p class="text-xs font-bold uppercase tracking-wide text-slate-400">Awaiting classification</p>
                    <p id="metric-pending" class="mt-2 text-3xl font-black text-amber-300">—</p>
                    <p class="mt-2 text-xs text-slate-500">Detected crops with PENDING status</p>
                </article>
                <article class="panel rounded-2xl border border-slate-800 p-4 md:p-5">
                    <p class="text-xs font-bold uppercase tracking-wide text-slate-400">Rejected questions</p>
                    <p id="metric-rejected" class="mt-2 text-3xl font-black text-rose-300">—</p>
                    <p class="mt-2 text-xs text-slate-500">All REJECTED_* statuses</p>
                </article>
            </div>
        </section>

        <section class="mt-8 grid grid-cols-1 xl:grid-cols-3 gap-5" aria-label="Archive charts">
            <article class="panel rounded-2xl border border-slate-800 p-5">
                <h3 class="font-bold">Question workflow</h3>
                <p class="text-xs text-slate-400 mt-1">Har detected question ka current processing status.</p>
                <div class="chart-wrap mt-4"><canvas id="question-status-chart" aria-label="Question status chart"></canvas></div>
            </article>
            <article class="panel rounded-2xl border border-slate-800 p-5">
                <h3 class="font-bold">Paper scan coverage</h3>
                <p class="text-xs text-slate-400 mt-1">Database mein marked full-scan papers vs remaining.</p>
                <div class="chart-wrap mt-4"><canvas id="paper-status-chart" aria-label="Paper scan coverage chart"></canvas></div>
            </article>
            <article class="panel rounded-2xl border border-slate-800 p-5">
                <h3 class="font-bold">Most classified subjects</h3>
                <p class="text-xs text-slate-400 mt-1">Completed questions ke hisaab se top subjects.</p>
                <div class="chart-wrap mt-4"><canvas id="subject-chart" aria-label="Top subjects chart"></canvas></div>
            </article>
        </section>

        <section class="mt-8 grid grid-cols-1 lg:grid-cols-2 gap-5" aria-label="Audience analytics">
            <article class="panel rounded-2xl border border-slate-800 p-5">
                <h2 class="font-bold">Audience & engagement</h2>
                <p class="text-xs text-slate-400 mt-1">Visitor ID first observed each day; this is not daily returning-user count.</p>
                <div class="grid grid-cols-2 gap-3 mt-4">
                    <div class="rounded-xl bg-slate-900/70 p-4">
                        <p class="text-xs text-slate-400">Tracked visitor IDs</p>
                        <p id="metric-visitors" class="mt-1 text-2xl font-bold">—</p>
                    </div>
                    <div class="rounded-xl bg-slate-900/70 p-4">
                        <p class="text-xs text-slate-400">Registered accounts</p>
                        <p id="metric-users" class="mt-1 text-2xl font-bold">—</p>
                    </div>
                    <div class="rounded-xl bg-slate-900/70 p-4">
                        <p class="text-xs text-slate-400">Recorded page views</p>
                        <p id="metric-page-views" class="mt-1 text-2xl font-bold">—</p>
                    </div>
                    <div class="rounded-xl bg-slate-900/70 p-4">
                        <p class="text-xs text-slate-400">Avg. tracked time / visitor</p>
                        <p id="metric-time" class="mt-1 text-2xl font-bold">—</p>
                    </div>
                </div>
                <div class="chart-wrap mt-5"><canvas id="first-visits-chart" aria-label="First observed visitors over 30 days"></canvas></div>
                <p class="text-xs leading-relaxed text-slate-500 mt-3">Time heartbeat se estimate hota hai; browser/device limits ke karan yeh exact active attention time nahi hai. Starred questions: <strong id="metric-stars" class="text-slate-300">—</strong></p>
            </article>
            <article class="panel rounded-2xl border border-slate-800 p-5">
                <h2 class="font-bold">Paper worklist</h2>
                <p class="text-xs text-slate-400 mt-1">Detected, classified, pending aur rejected question counts per paper.</p>
                <div class="overflow-x-auto mt-4">
                    <table class="w-full text-left text-sm">
                        <thead class="text-xs uppercase text-slate-500 border-b border-slate-800">
                            <tr><th class="py-3 pr-3">Paper</th><th class="py-3 px-2">Scan</th><th class="py-3 px-2">Done</th><th class="py-3 px-2">Pending</th><th class="py-3 pl-2">Rejected</th></tr>
                        </thead>
                        <tbody id="paper-table" class="divide-y divide-slate-800/80"></tbody>
                    </table>
                </div>
            </article>
        </section>

        <section class="mt-8 panel rounded-2xl border border-slate-800 p-5">
            <h2 class="font-bold">Metrics ko kaise padhein</h2>
            <dl class="grid sm:grid-cols-2 xl:grid-cols-3 gap-x-8 gap-y-4 mt-4 text-sm">
                <div><dt class="font-semibold text-emerald-200">Completed questions</dt><dd class="text-slate-400 mt-1">Woh question rows jo AI classification ke baad COMPLETED hain. Yeh total scanned pixels/crops ka measure nahi.</dd></div>
                <div><dt class="font-semibold text-indigo-200">Scanned papers / coverage</dt><dd class="text-slate-400 mt-1">`is_completed=1` papers / total papers. Is flag ka matlab scan pass complete; yeh har question ke accepted hone ki guarantee nahi.</dd></div>
                <div><dt class="font-semibold text-amber-200">Awaiting classification</dt><dd class="text-slate-400 mt-1">PENDING question rows. Zero ka matlab classifier queue mein abhi koi pending row nahi.</dd></div>
                <div><dt class="font-semibold text-rose-200">Rejected questions</dt><dd class="text-slate-400 mt-1">REJECTED_* status wali rows. Inhe accepted/completed count mein include nahi kiya jata.</dd></div>
                <div><dt class="font-semibold text-sky-200">First-observed visitor IDs</dt><dd class="text-slate-400 mt-1">Daily heartbeat table mein pehli baar record hue visitor IDs. Purane IDs ke repeat visits rozana alag se record nahi hote.</dd></div>
                <div><dt class="font-semibold text-violet-200">Top subjects</dt><dd class="text-slate-400 mt-1">COMPLETED questions ki current taxonomy grouping; subject-wise accuracy ya exam-weightage ka score nahi.</dd></div>
            </dl>
        </section>
        <footer class="text-center text-xs text-slate-600 mt-8">Dashboard aggregates only; personal visitor identifiers are not displayed. Auto-refresh har 60 seconds.</footer>
    </main>

    <script>
        const numberFormat = new Intl.NumberFormat('en-IN');
        const charts = {};
        const chartColors = ['#34d399', '#fbbf24', '#fb7185', '#64748b', '#60a5fa', '#c084fc'];
        Chart.defaults.color = '#94a3b8';
        Chart.defaults.font.family = 'Inter, ui-sans-serif, system-ui, sans-serif';

        function setText(id, value) {
            document.getElementById(id).textContent = value;
        }

        function makeChart(id, config) {
            if (charts[id]) {
                charts[id].data = config.data;
                charts[id].options = config.options;
                charts[id].update();
            } else {
                charts[id] = new Chart(document.getElementById(id), config);
            }
        }

        function renderPaperRows(rows) {
            const body = document.getElementById('paper-table');
            body.replaceChildren();
            for (const paper of rows) {
                const tr = document.createElement('tr');
                const title = document.createElement('td');
                title.className = 'py-3 pr-3 min-w-52';
                title.textContent = `${paper.exam_name} · ${paper.conducting_year}`;
                const scan = document.createElement('td');
                scan.className = 'py-3 px-2';
                scan.textContent = paper.is_completed ? 'Complete' : 'In progress';
                scan.classList.add(paper.is_completed ? 'text-emerald-300' : 'text-amber-300');
                tr.append(title, scan);
                for (const value of [paper.classified_questions, paper.pending_questions, paper.rejected_questions]) {
                    const td = document.createElement('td');
                    td.className = 'py-3 px-2 tabular-nums';
                    td.textContent = numberFormat.format(value);
                    tr.appendChild(td);
                }
                body.appendChild(tr);
            }
            if (!rows.length) {
                const tr = document.createElement('tr');
                const td = document.createElement('td');
                td.colSpan = 5;
                td.className = 'py-5 text-center text-slate-500';
                td.textContent = 'Abhi paper records nahi hain.';
                tr.appendChild(td);
                body.appendChild(tr);
            }
        }

        function renderAnalytics(data) {
            const s = data.summary;
            setText('metric-completed-questions', numberFormat.format(s.completed_questions));
            setText('metric-completed-papers', `${numberFormat.format(s.completed_papers)} / ${numberFormat.format(s.total_papers)}`);
            const coverage = s.total_papers ? (100 * s.completed_papers / s.total_papers).toFixed(1) : '0.0';
            setText('metric-paper-coverage', `${coverage}% full-scan flag`);
            setText('metric-pending', numberFormat.format(s.pending_questions));
            setText('metric-rejected', numberFormat.format(s.rejected_questions));
            setText('metric-visitors', numberFormat.format(s.total_visitors));
            setText('metric-users', numberFormat.format(s.registered_users));
            setText('metric-page-views', numberFormat.format(s.total_views));
            setText('metric-time', `${(s.avg_sec / 60).toFixed(1)} min`);
            setText('metric-stars', numberFormat.format(s.total_stars));
            setText('updated-at', `Updated ${new Date(data.updated_at).toLocaleTimeString()}`);

            const statusLabels = data.question_statuses.map(row => row.ai_status);
            const statusCounts = data.question_statuses.map(row => row.count);
            makeChart('question-status-chart', {
                type: 'doughnut',
                data: { labels: statusLabels, datasets: [{ data: statusCounts, backgroundColor: chartColors, borderColor: '#121d30', borderWidth: 3 }] },
                options: { maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } } }
            });
            makeChart('paper-status-chart', {
                type: 'doughnut',
                data: { labels: ['Full scan marked', 'Not yet full scan'], datasets: [{ data: [s.completed_papers, s.incomplete_papers], backgroundColor: ['#818cf8', '#334155'], borderColor: '#121d30', borderWidth: 3 }] },
                options: { maintainAspectRatio: false, plugins: { legend: { position: 'bottom' } } }
            });
            makeChart('subject-chart', {
                type: 'bar',
                data: { labels: data.subjects.map(row => row.subject_name), datasets: [{ label: 'Completed questions', data: data.subjects.map(row => row.completed_questions), backgroundColor: '#38bdf8', borderRadius: 6 }] },
                options: { indexAxis: 'y', maintainAspectRatio: false, scales: { x: { beginAtZero: true, grid: { color: '#263246' } }, y: { grid: { display: false } } }, plugins: { legend: { display: false } } }
            });
            makeChart('first-visits-chart', {
                type: 'line',
                data: { labels: data.first_visits.map(row => row.date), datasets: [{ label: 'First-observed visitor IDs', data: data.first_visits.map(row => row.count), borderColor: '#a78bfa', backgroundColor: 'rgba(167,139,250,.14)', pointRadius: 2, tension: .25, fill: true }] },
                options: { maintainAspectRatio: false, scales: { y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: '#263246' } }, x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } } } }
            });
            renderPaperRows(data.papers);
        }

        async function loadAnalytics() {
            const banner = document.getElementById('error-banner');
            const button = document.getElementById('refresh-button');
            button.disabled = true;
            banner.classList.add('hidden');
            try {
                const response = await fetch('/api/admin-analytics', { cache: 'no-store' });
                if (!response.ok) throw new Error(`Analytics request failed (${response.status})`);
                const data = await response.json();
                if (!data.summary || !Array.isArray(data.question_statuses) || !Array.isArray(data.first_visits)) {
                    throw new Error('Analytics response format unexpected hai.');
                }
                renderAnalytics(data);
            } catch (error) {
                banner.textContent = `Dashboard data load nahi hua: ${error.message}`;
                banner.classList.remove('hidden');
            } finally {
                button.disabled = false;
            }
        }
        document.getElementById('refresh-button').addEventListener('click', loadAnalytics);
        loadAnalytics();
        window.setInterval(loadAnalytics, 60000);
    </script>
</body>
</html>
"""


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_home(request: Request):
    base_url = str(request.base_url).rstrip("/")
    seo_block = build_seo_injection(base_url)
    return MAIN_HTML.replace("<!-- __SEO_INJECTION__ -->", seo_block)


@app.api_route("/admin", methods=["GET", "HEAD"], response_class=HTMLResponse)
def serve_admin():
    return ADMIN_HTML


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"🚀 PYQ Plus Live Server Starting on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)