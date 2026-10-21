import sqlite3
import base64
import urllib.request
import json
from pathlib import Path

# Live Server URL (Jab domain ya cloud IP mil jaye, use yahan likhein)
LIVE_SERVER_URL = "http://localhost:8000"  # Example: "https://pyqplus.in"
SYNC_SECRET_KEY = "pyq_plus_master_sync_2026"
DB_PATH = "rpsc_archive.db"


def get_live_synced_questions():
    url = f"{LIVE_SERVER_URL.rstrip('/')}/api/sync/existing-keys?secret_key={SYNC_SECRET_KEY}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data.get("synced", {})


def push_question_to_live(payload):
    url = f"{LIVE_SERVER_URL.rstrip('/')}/api/sync/push-question"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_one_click_sync():
    print("==========================================================")
    print(f"🚀 PYQ Plus — 1-Click Auto Updater ({LIVE_SERVER_URL})")
    print("==========================================================")

    try:
        live_synced = get_live_synced_questions()
    except Exception as e:
        print(f"❌ Live Server se connect nahi ho paya: {e}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    local_rows = cur.execute("""
        SELECT q.question_number, q.crop_image_path,
               qp.pdf_file_name, qp.exam_date, qp.exam_shift,
               e.exam_name, e.conducting_year,
               s.subject_name, t.topic_name
        FROM questions q
        JOIN question_papers qp ON q.paper_id = qp.paper_id
        JOIN examinations e ON qp.exam_id = e.exam_id
        JOIN topics t ON q.topic_id = t.topic_id
        JOIN subjects s ON t.subject_id = s.subject_id
        WHERE q.ai_status = 'COMPLETED'
        ORDER BY q.question_id ASC
    """).fetchall()
    conn.close()

    to_push = []
    for r in local_rows:
        key = f"{r['pdf_file_name']}::{r['question_number']}"
        val = f"{r['subject_name']}::{r['topic_name']}"
        if live_synced.get(key) != val:
            to_push.append(r)

    if not to_push:
        print("✅ Live Website pehle se 100% Up-to-Date hai! Koi naya sawal baki nahi hai.")
        return

    print(f"📦 Kul {len(to_push)} naye/updated Questions (Subjects & Topics sahit) mile! Upload shuru...")

    success_count = 0
    for idx, r in enumerate(to_push, 1):
        img_path = Path(r["crop_image_path"])
        if not img_path.exists():
            continue

        with open(img_path, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")

        payload = {
            "secret_key": SYNC_SECRET_KEY,
            "exam_name": r["exam_name"],
            "conducting_year": str(r["conducting_year"]),
            "pdf_file_name": r["pdf_file_name"],
            "exam_date": r["exam_date"],
            "exam_shift": r["exam_shift"],
            "subject_name": r["subject_name"],
            "topic_name": r["topic_name"],
            "question_number": int(r["question_number"]),
            "image_filename": img_path.name,
            "image_base64": b64_img
        }

        try:
            res = push_question_to_live(payload)
            if res.get("ok"):
                success_count += 1
                print(f"✅ [{idx}/{len(to_push)}] Synced -> [{r['subject_name']}] -> [{r['topic_name']}] (Q.#{r['question_number']})")
        except Exception as e:
            print(f"⚠️ Failed Q.#{r['question_number']}: {e}")

    print("==========================================================")
    print(f"🎉 1-Click Update Complete! {success_count} Questions Live Website par add ho gaye!")
    print("==========================================================")


if __name__ == "__main__":
    run_one_click_sync()