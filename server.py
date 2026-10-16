#!/usr/bin/env python3
"""
PrepPulse AI — Flask backend for Render.
Serves the frontend and provides translation, transcription, health, and SQLite APIs.
"""
import json
import os
import sqlite3
from pathlib import Path

import requests
from flask import Flask, g, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_TRANSLATION_BYTES = 64 * 1024
DB_PATH = Path(os.environ.get("DATABASE_PATH", str(BASE_DIR / "preppulse.db")))

GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
DEEPGRAM_KEY = os.environ.get("DEEPGRAM_API_KEY", "").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_AUDIO_BYTES


def extract_transcript(data):
    try:
        transcript = data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ValueError("Deepgram returned no transcription text.")
    if not transcript:
        raise ValueError("Deepgram returned an empty transcription.")
    return transcript


def extract_translation(data):
    try:
        parts = data["candidates"][0]["content"]["parts"]
        translation = "".join(part.get("text", "") for part in parts).strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ValueError("Gemini returned no translation text.")
    if not translation:
        raise ValueError("Gemini returned an empty translation.")
    return translation


def get_db():
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(str(DB_PATH))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS interview_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS interview_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER,
                question TEXT NOT NULL DEFAULT '',
                answer TEXT NOT NULL,
                translated_answer TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES interview_sessions(id) ON DELETE CASCADE
            )
        """)
        db.commit()


@app.get("/")
@app.get("/index")
@app.get("/home")
def home():
    filename = "innohack.html" if (BASE_DIR / "innohack.html").is_file() else "index.html"
    if not (BASE_DIR / filename).is_file():
        return (
            "<h1>PrepPulse AI backend is running</h1>"
            "<p>Place innohack.html or index.html beside server.py to serve the frontend.</p>",
            200,
            {"Content-Type": "text/html; charset=utf-8"},
        )
    return send_from_directory(BASE_DIR, filename)


@app.get("/api/status")
@app.get("/api/health")
def health():
    return jsonify({
        "status": "online",
        "app": "PrepPulse AI — Mock Interview Suite",
        "mode": "live" if (GEMINI_KEY or DEEPGRAM_KEY) else "sandbox",
        "providers": {
            "gemini_ai": bool(GEMINI_KEY),
            "gemini_translation": bool(GEMINI_KEY),
            "deepgram_stt": bool(DEEPGRAM_KEY),
            "supabase_auth": bool(SUPABASE_URL),
        },
        "transcriptionConfigured": bool(DEEPGRAM_KEY),
        "translationConfigured": bool(GEMINI_KEY),
        "databaseConfigured": True,
    })


@app.post("/api/transcribe")
def transcribe():
    if not DEEPGRAM_KEY:
        return jsonify(
            error="Speech transcription is not configured. Add DEEPGRAM_API_KEY in the server environment and restart it."
        ), 503

    content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
        return jsonify(error="Content-Type must be a supported audio media type."), 415

    audio_data = request.get_data(cache=False)
    if not audio_data:
        return jsonify(error="Audio request body is required."), 400
    if len(audio_data) > MAX_AUDIO_BYTES:
        return jsonify(error="Audio exceeds the allowed size."), 413

    try:
        response = requests.post(
            "https://api.deepgram.com/v1/listen?model=nova-2&smart_format=true&detect_language=true",
            data=audio_data,
            headers={
                "Authorization": f"Token {DEEPGRAM_KEY}",
                "Content-Type": request.headers.get("Content-Type", "audio/webm"),
            },
            timeout=45,
        )
        if not response.ok:
            app.logger.warning("Deepgram provider returned HTTP %s", response.status_code)
            status_code = response.status_code if response.status_code in (400, 401, 403, 429) else 502
            return jsonify(
                error=f"Deepgram transcription failed (provider HTTP {response.status_code})."
            ), status_code
        transcript = extract_transcript(response.json())
        return jsonify(transcript=transcript, provider="deepgram-nova-2")
    except requests.RequestException:
        app.logger.exception("Deepgram connection failed")
        return jsonify(
            error="Could not connect to Deepgram. Check the API key and server network access."
        ), 502
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return jsonify(
            error="Deepgram returned an invalid or empty transcription response."
        ), 502


@app.post("/api/translate")
def translate():
    if not GEMINI_KEY:
        return jsonify(
            error="Answer translation is not configured. Add GEMINI_API_KEY in the server environment and restart it."
        ), 503
    if not request.is_json:
        return jsonify(error="Content-Type must be application/json."), 415

    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return jsonify(error="Provide the answer as a string in the 'text' field."), 400

    text = data["text"].strip()
    if len(text) < 2:
        return jsonify(error="Answer text must contain at least 2 characters."), 400
    if len(text.encode("utf-8")) > MAX_TRANSLATION_BYTES:
        return jsonify(error="Answer text exceeds the translation size limit."), 413

    prompt = (
        "Translate the candidate's interview answer into natural English. "
        "If it is already in English, return it unchanged. Preserve meaning, facts, numbers, "
        "names, and first-person perspective. Do not add, omit, summarize, or evaluate. "
        "Return only the translated answer, without labels or quotation marks.\n\n"
        "Candidate answer:\n" + text
    )

    try:
        response = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            params={"key": GEMINI_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0},
            },
            timeout=45,
        )
        if not response.ok:
            app.logger.warning("Gemini provider returned HTTP %s", response.status_code)
            status_code = response.status_code if response.status_code in (400, 401, 403, 429) else 502
            return jsonify(
                error=f"Answer translation failed (provider HTTP {response.status_code})."
            ), status_code
        translated = extract_translation(response.json())
        return jsonify(translatedText=translated, provider=GEMINI_MODEL)
    except requests.RequestException:
        app.logger.exception("Gemini connection failed")
        return jsonify(
            error="Could not connect to Gemini. Check the API key and server network access."
        ), 502
    except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return jsonify(
            error="Gemini returned an invalid or empty translation response."
        ), 502


@app.post("/api/sessions")
def create_session():
    data = request.get_json(silent=True) or {}
    candidate_name = str(data.get("candidate_name", ""))[:200]
    role = str(data.get("role", ""))[:200]
    db = get_db()
    cursor = db.execute(
        "INSERT INTO interview_sessions(candidate_name, role) VALUES (?, ?)",
        (candidate_name, role),
    )
    db.commit()
    return jsonify(id=cursor.lastrowid, candidate_name=candidate_name, role=role), 201


@app.get("/api/sessions")
def list_sessions():
    rows = get_db().execute(
        "SELECT id, candidate_name, role, created_at FROM interview_sessions ORDER BY id DESC LIMIT 100"
    ).fetchall()
    return jsonify(sessions=[dict(row) for row in rows])


@app.post("/api/answers")
def save_answer():
    data = request.get_json(silent=True) or {}
    answer = data.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return jsonify(error="answer is required."), 400

    session_id = data.get("session_id")
    question = str(data.get("question", ""))[:4000]
    translated_answer = data.get("translated_answer")
    if translated_answer is not None and not isinstance(translated_answer, str):
        return jsonify(error="translated_answer must be a string."), 400

    try:
        cursor = get_db().execute(
            """INSERT INTO interview_answers
               (session_id, question, answer, translated_answer)
               VALUES (?, ?, ?, ?)""",
            (
                session_id,
                question,
                answer[:20000],
                translated_answer[:20000] if translated_answer else None,
            ),
        )
        get_db().commit()
    except sqlite3.IntegrityError:
        return jsonify(error="session_id must refer to an existing interview session or be null."), 400
    return jsonify(id=cursor.lastrowid, saved=True), 201


@app.get("/api/sessions/<int:session_id>/answers")
def list_answers(session_id):
    rows = get_db().execute(
        """SELECT id, session_id, question, answer, translated_answer, created_at
           FROM interview_answers WHERE session_id = ? ORDER BY id""",
        (session_id,),
    ).fetchall()
    return jsonify(answers=[dict(row) for row in rows])


@app.get("/<path:filename>")
def static_files(filename):
    # Do not serve environment files, hidden files, or backend/database files.
    normalized = filename.replace("\\", "/")
    if any(part.startswith(".") for part in normalized.split("/")):
        return jsonify(error="Not found"), 404
    if normalized in {"server.py", "requirements.txt", "render.yaml", "preppulse.db"}:
        return jsonify(error="Not found"), 404
    target = BASE_DIR / normalized
    if target.is_file():
        return send_from_directory(BASE_DIR, normalized)
    return jsonify(error="Not found"), 404


# Initialize SQLite tables when Gunicorn imports this module.
with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
