import os
import sqlite3
import time
import hashlib
import base64
import json
from datetime import datetime
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
            (SELECT COUNT(*) FROM pyq_starred) as total_stars
        FROM pyq_analytics
    """, fetchone=True)

    daily = db_read("""
        SELECT visit_date, COUNT(visitor_id) as visitors, ROUND(AVG(total_seconds)/60.0, 1) as avg_minutes
        FROM pyq_analytics
        GROUP BY visit_date
        ORDER BY visit_date ASC
        LIMIT 30
    """)
    return {
        "summary": dict(summary) if summary else {},
        "daily": [dict(d) for d in daily]
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

# Allow AI Search Assistants (Google AI, ChatGPT, Perplexity) to recommend PYQ Plus
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
    """Builds Server-Side JSON-LD Schema + Crawlable Catalog so Google & AI recommend PYQ Plus."""
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
                    "RAS Previous Year Questions",
                    "RPSC Headmaster & PTI Question Bank"
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
            },
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": "RPSC ke sabhi purane papers (PYQ) Subject-wise aur Topic-wise kahan milenge?",
                        "acceptedAnswer": {
                            "@type": "Answer",
                            "text": f"PYQ Plus ({base_url}) par 2011 se 2026 tak ke sabhi RPSC Official Question Papers ({exam_names}) ke {total_q}+ questions Subject-wise ({sub_names}) aur Topic-wise bilkul free uplabdh hain."
                        }
                    }
                ]
            }
        ]
    }

    seo_html = f"""
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


# --- MAIN 3D AESTHETIC WEBSITE HTML ---
MAIN_HTML = """<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PYQ Plus — RPSC PYQ Topic-Wise & Subject-Wise Official Archive (2011–2026)</title>
    <meta name="description" content="PYQ Plus: Rajasthan RPSC Official Previous Year Question Papers (2011-2026) Subject-Wise & Topic-Wise Verified Question Bank. RAS, 1st Grade, 2nd Grade, Headmaster, Rajasthan GK, Maths, Science, Reasoning PYQs.">
    <meta name="keywords" content="PYQ Plus, RPSC PYQ, RPSC Previous Year Questions Topic Wise, RPSC Old Question Papers, Rajasthan GK PYQ Topic Wise, RPSC 2nd Grade PYQ, RPSC 1st Grade PYQ, RPSC Archive">
    <meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
    <meta property="og:title" content="PYQ Plus — Rajasthan's #1 RPSC Topic-Wise Question Bank">
    <meta property="og:description" content="2011 se 2026 tak ke sabhi RPSC Official Papers ka Subject-Wise aur Topic-Wise Digital Sankalan.">
    <meta property="og:type" content="website">
    <!-- __SEO_INJECTION__ -->
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r134/three.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700;800&family=Noto+Sans+Devanagari:wght@400;600;700;800&display=swap" rel="stylesheet">
    <style>
        html { scroll-behavior: smooth; }
        body {
            font-family: 'Plus Jakarta Sans', 'Noto Sans Devanagari', sans-serif;
            background-color: #060913;
            color: #f8fafc;
            overflow-x: hidden;
        }
        #bg-canvas {
            position: fixed;
            top: 0; left: 0; width: 100vw; height: 100vh;
            z-index: 0; pointer-events: none;
        }
        .glass {
            background: rgba(15, 23, 42, 0.74);
            backdrop-filter: blur(18px);
            -webkit-backdrop-filter: blur(18px);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        .glass-card {
            background: linear-gradient(145deg, rgba(30, 41, 59, 0.78), rgba(15, 23, 42, 0.9));
            backdrop-filter: blur(14px);
            border: 1px solid rgba(99, 102, 241, 0.24);
            transition: all 0.32s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .glass-card:hover {
            transform: translateY(-5px) scale(1.015);
            border-color: rgba(99, 102, 241, 0.7);
            box-shadow: 0 14px 34px -10px rgba(99, 102, 241, 0.4);
        }
        .intro-overlay {
            position: fixed; inset: 0; z-index: 50;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            background: radial-gradient(circle at center, rgba(15, 23, 42, 0.4) 0%, #060913 100%);
            transition: opacity 0.8s ease, transform 0.8s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .intro-hidden {
            opacity: 0; transform: scale(1.12); pointer-events: none;
        }

        @keyframes neonPulse {
            0%, 100% {
                box-shadow: 0 0 15px rgba(245, 158, 11, 0.55), 0 0 30px rgba(236, 72, 153, 0.35);
                transform: translateY(0px) scale(1);
            }
            50% {
                box-shadow: 0 0 25px rgba(245, 158, 11, 0.9), 0 0 45px rgba(236, 72, 153, 0.65);
                transform: translateY(-2px) scale(1.04);
            }
        }
        @keyframes shimmerSweep {
            0% { transform: translateX(-150%) skewX(-20deg); }
            100% { transform: translateX(250%) skewX(-20deg); }
        }
        .coming-soon-badge {
            position: relative; overflow: hidden;
            background: linear-gradient(135deg, #f59e0b 0%, #ec4899 50%, #6366f1 100%);
            padding: 2px; border-radius: 9999px;
            animation: neonPulse 2.6s infinite ease-in-out;
            cursor: pointer;
        }
        .coming-soon-inner {
            background: rgba(10, 14, 26, 0.92);
            backdrop-filter: blur(12px);
            border-radius: 9999px;
            padding: 7px 18px;
            display: inline-flex; align-items: center; gap: 8px;
        }
        .coming-soon-badge::after {
            content: ""; position: absolute; top: 0; left: 0; width: 45%; height: 100%;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.45), transparent);
            animation: shimmerSweep 2.8s infinite;
        }
        .coming-soon-text {
            background: linear-gradient(90deg, #fde68a, #f472b6, #a5b4fc);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 800; letter-spacing: 0.4px;
        }

        @keyframes spinOuter {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        @keyframes spinInner {
            0% { transform: rotate(360deg); }
            100% { transform: rotate(0deg); }
        }
        @keyframes pulseGlow {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.65; transform: scale(0.96); }
        }
        .wait-ring-outer {
            width: 76px; height: 76px;
            border-radius: 50%;
            border: 4px solid rgba(99, 102, 241, 0.15);
            border-top-color: #818cf8;
            border-right-color: #c084fc;
            animation: spinOuter 0.9s cubic-bezier(0.5, 0.1, 0.5, 0.9) infinite;
            box-shadow: 0 0 25px rgba(99, 102, 241, 0.4);
        }
        .wait-ring-inner {
            position: absolute;
            width: 52px; height: 52px;
            border-radius: 50%;
            border: 4px solid rgba(245, 158, 11, 0.15);
            border-bottom-color: #f59e0b;
            border-left-color: #ec4899;
            animation: spinInner 0.75s linear infinite;
        }
        .wait-text-glow {
            background: linear-gradient(90deg, #ffffff, #a5b4fc, #f472b6, #ffffff);
            background-size: 200% auto;
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            animation: pulseGlow 1.5s infinite ease-in-out;
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

    <canvas id="bg-canvas"></canvas>

    <!-- 3D Entry Splash Animation -->
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

            <h1 class="text-6xl md:text-7xl font-extrabold tracking-tight bg-gradient-to-r from-white via-indigo-200 to-indigo-400 bg-clip-text text-transparent mb-4">
                PYQ Plus
            </h1>
            <p class="text-slate-300 text-lg md:text-xl mb-8">
                RPSC Official Previous Year Questions ka Subject-Wise aur Topic-Wise Digital Sankalan
            </p>
            <button onclick="enterPortal()" class="px-8 py-4 rounded-2xl bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white font-bold text-lg shadow-lg shadow-indigo-600/40 transition transform hover:scale-105 active:scale-95">
                🚀 Explore PYQ Plus Archive
            </button>
        </div>
    </div>

    <!-- AESTHETIC "WAIT..." LOADING MODAL OVERLAY -->
    <div id="wait-loader" class="fixed inset-0 z-50 hidden items-center justify-center bg-slate-950/75 backdrop-blur-md transition-opacity duration-300">
        <div class="glass rounded-3xl px-10 py-8 border border-indigo-500/40 shadow-2xl flex flex-col items-center text-center max-w-xs w-full mx-4">
            <div class="relative flex items-center justify-center mb-5">
                <div class="wait-ring-outer"></div>
                <div class="wait-ring-inner"></div>
                <span class="absolute text-lg">⚡</span>
            </div>
            <div class="text-2xl font-extrabold tracking-wider wait-text-glow flex items-center gap-1">
                <span>Wait</span>
                <span class="inline-flex gap-1 ml-1">
                    <span class="w-2 h-2 rounded-full bg-indigo-400 animate-bounce" style="animation-delay: 0ms;"></span>
                    <span class="w-2 h-2 rounded-full bg-pink-400 animate-bounce" style="animation-delay: 150ms;"></span>
                    <span class="w-2 h-2 rounded-full bg-amber-400 animate-bounce" style="animation-delay: 300ms;"></span>
                </span>
            </div>
            <p id="wait-subtext" class="text-xs text-slate-300 mt-2 font-semibold">
                Sawaal load ho rahe hain...
            </p>
        </div>
    </div>

    <div class="relative z-10 max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pb-20">
        
        <header class="glass sticky top-4 z-30 rounded-2xl px-5 py-3.5 mt-4 mb-8 flex flex-wrap items-center justify-between gap-4 shadow-xl">
            <div class="flex items-center gap-4 flex-wrap">
                <div onclick="showHome()" class="flex items-center gap-3 cursor-pointer">
                    <div class="w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center font-extrabold text-xl shadow-md">
                        P+
                    </div>
                    <div>
                        <span class="text-2xl font-extrabold tracking-tight text-white">PYQ <span class="text-indigo-400">Plus</span></span>
                    </div>
                </div>

                <div onclick="openComingSoonModal()" class="coming-soon-badge" title="Click karke upcoming updates dekhein">
                    <div class="coming-soon-inner">
                        <span class="relative flex h-2.5 w-2.5">
                            <span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-75"></span>
                            <span class="relative inline-flex rounded-full h-2.5 w-2.5 bg-amber-400"></span>
                        </span>
                        <span class="text-base">✨</span>
                        <span class="coming-soon-text text-xs sm:text-sm">Coming Soon All</span>
                    </div>
                </div>
            </div>

            <div class="flex items-center gap-2.5 flex-wrap">
                <button onclick="showHome()" class="px-4 py-2 rounded-xl text-sm font-semibold bg-slate-800/80 hover:bg-slate-700 text-slate-200 transition">
                    📚 Subjects
                </button>
                <button onclick="openStarredTab()" class="px-4 py-2 rounded-xl text-sm font-semibold bg-amber-500/20 hover:bg-amber-500/30 text-amber-300 border border-amber-500/30 transition flex items-center gap-1.5">
                    ⭐ Saved Questions
                </button>
                <button id="auth-btn" onclick="openLoginModal()" class="px-4 py-2 rounded-xl text-sm font-bold bg-indigo-600 hover:bg-indigo-500 text-white transition shadow-md">
                    👤 Login
                </button>
            </div>
        </header>

        <section id="hero-banner" class="glass rounded-3xl p-8 mb-10 text-center border border-indigo-500/20 relative overflow-hidden">
            <div class="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-amber-500/15 border border-amber-400/30 text-amber-300 text-xs sm:text-sm font-bold mb-4">
                🔥 2011 Papers Live • Aage ke Sabhi Years Lagatar Add Ho Rahe Hain!
            </div>
            <h2 class="text-3xl md:text-4xl font-extrabold text-white mb-2">
                🏛️ RPSC Topic-Wise Question Bank
            </h2>
            <p class="text-slate-400 max-w-2xl mx-auto mb-6">
                Kisi bhi Subject card par click karein aur uske andar ke Topic ko chunein. Important sawalon ko ⭐ Star lagakar apne personal folder me save karein.
            </p>
            <div class="grid grid-cols-1 sm:grid-cols-3 gap-4 max-w-3xl mx-auto">
                <div class="glass-card rounded-2xl p-4">
                    <div class="text-slate-400 text-xs uppercase font-bold">Kul Subjects</div>
                    <div id="stat-subs" class="text-3xl font-extrabold text-indigo-400 mt-1">-</div>
                </div>
                <div class="glass-card rounded-2xl p-4">
                    <div class="text-slate-400 text-xs uppercase font-bold">Verified Topics</div>
                    <div id="stat-tops" class="text-3xl font-extrabold text-emerald-400 mt-1">-</div>
                </div>
                <div class="glass-card rounded-2xl p-4">
                    <div class="text-slate-400 text-xs uppercase font-bold">Verified Questions</div>
                    <div id="stat-qs" class="text-3xl font-extrabold text-amber-400 mt-1">-</div>
                </div>
            </div>
        </section>

        <section id="subjects-section">
            <div class="flex items-center justify-between mb-5">
                <h3 class="text-xl font-bold text-white flex items-center gap-2">
                    <span>1️⃣</span> Subject Chunein
                </h3>
            </div>
            <div id="subjects-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5"></div>
        </section>

        <section id="topics-section" class="hidden mt-8">
            <div class="glass rounded-3xl p-6 border border-indigo-500/30">
                <div class="flex flex-wrap items-center justify-between gap-4 mb-5">
                    <div>
                        <span class="text-xs uppercase tracking-wider text-indigo-400 font-bold">Chuna Gaya Subject</span>
                        <h3 id="active-subject-title" class="text-2xl font-extrabold text-white mt-0.5"></h3>
                    </div>
                    <button onclick="showHome()" class="text-sm text-slate-300 hover:text-white px-3 py-1.5 rounded-lg bg-slate-800">
                        ← Subject Badlein
                    </button>
                </div>
                <p class="text-sm text-slate-400 mb-4">2️⃣ Niche diye gaye kisi bhi Topic par click karein:</p>
                <div id="topics-grid" class="flex flex-wrap gap-3"></div>
            </div>
        </section>

        <section id="questions-section" class="hidden mt-8">
            <div class="glass rounded-3xl p-6 mb-6 border-l-4 border-l-indigo-500 flex flex-wrap items-center justify-between gap-4">
                <div>
                    <div id="q-header-sub" class="text-xs font-bold uppercase tracking-wider text-indigo-400"></div>
                    <div class="flex items-center gap-3 mt-1 flex-wrap">
                        <h2 id="q-header-top" class="text-2xl md:text-3xl font-extrabold text-white"></h2>
                        <span id="q-header-count" class="text-xs font-bold px-3 py-1 rounded-full bg-indigo-500/20 text-indigo-300 border border-indigo-500/30"></span>
                    </div>
                </div>
            </div>

            <div id="batch-tabs" class="flex flex-wrap gap-2.5 mb-6"></div>
            <div id="questions-window-wrapper" class="relative"></div>
            <div id="batch-tabs-bottom" class="flex flex-wrap gap-2.5 mt-8 justify-center"></div>
        </section>

    </div>

    <!-- COMING SOON MODAL -->
    <div id="coming-soon-modal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/75 backdrop-blur-md p-4">
        <div class="glass rounded-3xl max-w-md w-full p-8 border border-amber-500/40 shadow-2xl relative text-center">
            <button onclick="closeComingSoonModal()" class="absolute top-5 right-5 text-slate-400 hover:text-white text-xl">✕</button>
            <div class="w-16 h-16 mx-auto rounded-2xl bg-gradient-to-br from-amber-500/30 to-pink-500/30 border border-amber-400/50 flex items-center justify-center text-3xl mb-4 shadow-lg">
                🚀
            </div>
            <div class="inline-block px-3 py-1 rounded-full bg-amber-500/20 text-amber-300 text-xs font-bold mb-2">
                ✨ Coming Soon All
            </div>
            <h3 class="text-2xl font-extrabold text-white mb-2">Naye Papers & Years Lagatar Add Ho Rahe Hain!</h3>
            <p class="text-sm text-slate-300 leading-relaxed mb-6">
                Abhi <b>2011</b> ke sabhi RPSC papers Subject-Wise aur Topic-Wise live kar diye gaye hain. 
                <b>2012 se 2026</b> tak ke baaki sabhi RPSC question papers step-by-step jaldi hi isi portal par add ho jayenge!
            </p>
            <div class="grid grid-cols-2 gap-3 text-left mb-6">
                <div class="p-3 rounded-xl bg-emerald-500/15 border border-emerald-500/30">
                    <div class="text-xs text-emerald-300 font-bold">✅ 2011 Archive</div>
                    <div class="text-sm font-extrabold text-white mt-0.5">100% Live Now</div>
                </div>
                <div class="p-3 rounded-xl bg-amber-500/15 border border-amber-500/30">
                    <div class="text-xs text-amber-300 font-bold">⏳ 2012 – 2026</div>
                    <div class="text-sm font-extrabold text-white mt-0.5">Coming Soon...</div>
                </div>
            </div>
            <button onclick="closeComingSoonModal()" class="w-full py-3 rounded-xl bg-gradient-to-r from-amber-500 to-pink-600 hover:from-amber-400 hover:to-pink-500 text-white font-bold shadow-lg transition">
                👍 Samajh Gaya, Padhai Shuru Karein!
            </button>
        </div>
    </div>

    <!-- LOGIN MODAL -->
    <div id="login-modal" class="fixed inset-0 z-50 hidden items-center justify-center bg-black/70 backdrop-blur-md p-4">
        <div class="glass rounded-3xl max-w-md w-full p-8 border border-indigo-500/40 shadow-2xl relative">
            <button onclick="closeLoginModal()" class="absolute top-5 right-5 text-slate-400 hover:text-white text-xl">✕</button>
            <div class="w-12 h-12 rounded-2xl bg-indigo-600/30 border border-indigo-400/40 flex items-center justify-center text-2xl mb-4">
                ⭐
            </div>
            <h3 class="text-2xl font-extrabold text-white mb-1">PYQ Plus Account</h3>
            <p class="text-sm text-slate-400 mb-6">
                Apne mahatvapurna sawalon ko ⭐ Star karke save rakhne ke liye login ya naya account banayein.
            </p>
            <div class="space-y-4">
                <div>
                    <label class="text-xs font-semibold text-slate-300">Aapka Naam</label>
                    <input id="auth-name" type="text" placeholder="Jaise: Bhuvi" class="w-full mt-1 px-4 py-3 rounded-xl bg-slate-900/90 border border-slate-700 text-white focus:outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-300">Mobile Number ya Email</label>
                    <input id="auth-id" type="text" placeholder="Mobile no. ya Email" class="w-full mt-1 px-4 py-3 rounded-xl bg-slate-900/90 border border-slate-700 text-white focus:outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-300">Password</label>
                    <input id="auth-pass" type="password" placeholder="••••••••" class="w-full mt-1 px-4 py-3 rounded-xl bg-slate-900/90 border border-slate-700 text-white focus:outline-none focus:border-indigo-500">
                </div>
                <div id="auth-err" class="text-xs text-rose-400 hidden"></div>
                <button onclick="submitAuth()" class="w-full py-3.5 rounded-xl bg-gradient-to-r from-indigo-600 to-violet-600 hover:from-indigo-500 hover:to-violet-500 text-white font-bold shadow-lg transition">
                    ✨ Continue & Save Questions
                </button>
            </div>
        </div>
    </div>

    <script>
        const canvas = document.getElementById('bg-canvas');
        const scene = new THREE.Scene();
        const camera = new THREE.PerspectiveCamera(65, window.innerWidth / window.innerHeight, 0.1, 1000);
        const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
        renderer.setSize(window.innerWidth, window.innerHeight);
        renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

        const particlesGeometry = new THREE.BufferGeometry();
        const count = 900;
        const positions = new Float32Array(count * 3);
        for (let i = 0; i < count * 3; i++) {
            positions[i] = (Math.random() - 0.5) * 28;
        }
        particlesGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
        const particlesMaterial = new THREE.PointsMaterial({
            size: 0.055, color: 0x818cf8, transparent: true, opacity: 0.75
        });
        const particlesMesh = new THREE.Points(particlesGeometry, particlesMaterial);
        scene.add(particlesMesh);

        const knotGeo = new THREE.TorusKnotGeometry(3.2, 0.8, 90, 14);
        const knotMat = new THREE.MeshBasicMaterial({ color: 0x4f46e5, wireframe: true, transparent: true, opacity: 0.12 });
        const knotMesh = new THREE.Mesh(knotGeo, knotMat);
        scene.add(knotMesh);

        camera.position.z = 9;
        let mouseX = 0, mouseY = 0;
        window.addEventListener('mousemove', (e) => {
            mouseX = (e.clientX / window.innerWidth - 0.5) * 1.5;
            mouseY = (e.clientY / window.innerHeight - 0.5) * 1.5;
        });

        function animate3D() {
            requestAnimationFrame(animate3D);
            particlesMesh.rotation.y += 0.0012;
            particlesMesh.rotation.x += 0.0005;
            knotMesh.rotation.y += 0.003;
            knotMesh.rotation.x += 0.002;
            camera.position.x += (mouseX - camera.position.x) * 0.04;
            camera.position.y += (-mouseY - camera.position.y) * 0.04;
            renderer.render(scene, camera);
        }
        animate3D();

        window.addEventListener('resize', () => {
            camera.aspect = window.innerWidth / window.innerHeight;
            camera.updateProjectionMatrix();
            renderer.setSize(window.innerWidth, window.innerHeight);
        });

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
                card.className = 'glass-card rounded-2xl p-6 cursor-pointer flex items-center justify-between';
                card.onclick = () => selectSubject(s.subject_id, s.subject_name);
                card.innerHTML = `
                    <div class="flex items-center gap-4">
                        <div class="w-12 h-12 rounded-2xl bg-indigo-500/20 border border-indigo-400/30 flex items-center justify-center text-2xl">
                            📘
                        </div>
                        <div>
                            <h4 class="text-lg font-bold text-white">${s.subject_name}</h4>
                            <span class="text-xs text-indigo-300">Topics dekhein →</span>
                        </div>
                    </div>
                `;
                grid.appendChild(card);
            });
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
                pill.className = 'px-5 py-3 rounded-2xl glass-card text-left font-semibold text-slate-100 hover:text-white transition';
                pill.innerText = '🎯 ' + t.topic_name;
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
                card.className = 'glass rounded-3xl p-6 border border-slate-800 shadow-lg question-card-anim';
                card.innerHTML = `
                    <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
                        <div class="flex flex-wrap items-center gap-2 text-xs sm:text-sm font-semibold">
                            <span class="px-3.5 py-1.5 rounded-xl bg-blue-500/15 text-blue-300 border border-blue-500/30">
                                🏛️ <b>Exam:</b> ${q.exam_name}
                            </span>
                            <span class="px-3.5 py-1.5 rounded-xl bg-emerald-500/15 text-emerald-300 border border-emerald-500/30">
                                📅 <b>Date:</b> ${q.exam_date}
                            </span>
                            <span class="px-3.5 py-1.5 rounded-xl bg-amber-500/15 text-amber-300 border border-amber-500/30">
                                ⏰ <b>Shift:</b> ${q.exam_shift}
                            </span>
                        </div>
                        <button id="star-btn-${q.question_id}" onclick="handleStarClick(${q.question_id})"
                            class="px-4 py-2 rounded-xl text-sm font-bold transition flex items-center gap-1.5 ${
                                q.is_starred
                                    ? 'bg-amber-500 text-slate-950 shadow-md shadow-amber-500/30'
                                    : 'bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700'
                            }">
                            <span>${q.is_starred ? '⭐ Saved' : '☆ Star'}</span>
                        </button>
                    </div>
                    <div class="bg-white rounded-2xl p-3 overflow-hidden shadow-inner">
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
                nextBanner.className = 'glass rounded-2xl p-5 text-center border border-indigo-500/30 flex flex-wrap items-center justify-between gap-4';
                nextBanner.innerHTML = `
                    <span class="text-sm text-slate-300 font-semibold">✅ Is tab ke sawal samapt hue. Agle tab ke sawal pehle se tayyar hain!</span>
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
                wrapper.innerHTML = `<div class="glass rounded-2xl p-8 text-center text-slate-400">Abhi koi question nahi hai.</div>`;
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
                            ? 'bg-indigo-600 text-white shadow-lg shadow-indigo-600/30 ring-2 ring-indigo-400/50'
                            : 'glass text-slate-300 hover:text-white hover:border-indigo-400/40'
                    }`;
                    btn.innerHTML = `
                        <span>📄 Sawaal ${start} se ${end}</span>
                        ${isPreloadedNext ? '<span class="w-2 h-2 rounded-full bg-emerald-400" title="Instant Ready"></span>' : ''}
                    `;
                    btn.onclick = () => switchBatchTab(i);
                    return btn;
                };

                topBar.appendChild(createBtn());
                bottomBar.appendChild(createBtn());
            }
        }

        function handleStarClick(qId) {
            if (!currentUser) {
                pendingStarQuestionId = qId;
                openLoginModal();
                return;
            }
            toggleStarAPI(qId);
        }

        async function toggleStarAPI(qId) {
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
                    btn.className = `px-4 py-2 rounded-xl text-sm font-bold transition flex items-center gap-1.5 ${
                        data.starred
                            ? 'bg-amber-500 text-slate-950 shadow-md shadow-amber-500/30'
                            : 'bg-slate-800 hover:bg-slate-700 text-slate-200 border border-slate-700'
                    }`;
                    btn.innerHTML = `<span>${data.starred ? '⭐ Saved' : '☆ Star'}</span>`;
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
                toggleStarAPI(pendingStarQuestionId);
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


# --- PRIVATE FOUNDER ANALYTICS DASHBOARD (/admin) ---
ADMIN_HTML = """<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <title>Dashboard</title>
</head>
...
</html>"""