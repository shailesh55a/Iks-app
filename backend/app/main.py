from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator, model_validator
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfgen import canvas

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
load_dotenv(BASE.parent.parent / ".env", override=False)
DB_PATH = BASE / "iksphere.db"
SEED_PATH = BASE / "seed.json"

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "").strip()
FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "").strip()
TEACHER_EMAILS = {x.strip().lower() for x in os.getenv("TEACHER_EMAILS", "").split(",") if x.strip()}
CORS_ORIGINS = [x.strip() for x in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000") .split(",") if x.strip()]
APP_VERSION = "5.0.0-mobile"

COURSE_SCHOLARS = {
    "course_ganita": ["sch_baudhayana", "sch_brahmagupta", "sch_madhava"],
    "course_khagola": ["sch_aryabhata", "sch_brahmagupta"],
    "course_ayurveda": ["sch_sushruta", "sch_charaka"],
    "course_darshana": ["sch_kanada"],
    "course_vyakarana": ["sch_panini"],
    "course_rasayana": [],
    "course_sthapatya": [],
    "course_krishi": [],
    "course_jala": [],
}

SANSKRIT_GUIDE = {
    "vowels": [
        ["अ", "a", "short a"], ["आ", "ā", "long aa"], ["इ", "i", "short i"], ["ई", "ī", "long ee"],
        ["उ", "u", "short u"], ["ऊ", "ū", "long oo"], ["ऋ", "ṛ", "vocalic r"], ["ए", "e", "long e"],
        ["ऐ", "ai", "as in aisle"], ["ओ", "o", "long o"], ["औ", "au", "as in loud"], ["अं", "aṃ", "anusvāra"],
        ["अः", "aḥ", "visarga"],
    ],
    "consonants": [
        ["क", "ka", "ka"], ["ख", "kha", "aspirated kha"], ["ग", "ga", "ga"], ["घ", "gha", "aspirated gha"],
        ["च", "ca", "cha-like c"], ["ज", "ja", "ja"], ["ट", "ṭa", "retroflex t"], ["ड", "ḍa", "retroflex d"],
        ["त", "ta", "dental t"], ["द", "da", "dental d"], ["प", "pa", "pa"], ["ब", "ba", "ba"],
        ["म", "ma", "ma"], ["य", "ya", "ya"], ["र", "ra", "ra"], ["ल", "la", "la"],
        ["व", "va", "va/wa"], ["श", "śa", "palatal sh"], ["ष", "ṣa", "retroflex sh"], ["स", "sa", "sa"], ["ह", "ha", "ha"],
    ],
    "terms": [
        ["ज्ञान", "jñāna", "knowledge"], ["विद्या", "vidyā", "learning / knowledge"], ["गणित", "gaṇita", "mathematics"],
        ["खगोल", "khagola", "astronomy / celestial sphere"], ["शून्य", "śūnya", "zero / void"], ["सूत्र", "sūtra", "compact rule / aphorism"],
        ["आचार्य", "ācārya", "teacher / preceptor"], ["श्लोक", "śloka", "verse"], ["तर्क", "tarka", "reasoning"], ["विज्ञान", "vijñāna", "discernment / specialized knowledge"],
    ],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_key() -> str:
    return date.today().isoformat()


def uid(prefix: str = "id") -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def postgres_sql(sql: str) -> Optional[str]:
    sql = re.sub(
        r"ALTER TABLE (\w+) ADD COLUMN (?!IF NOT EXISTS)",
        r"ALTER TABLE \1 ADD COLUMN IF NOT EXISTS ",
        sql,
        flags=re.IGNORECASE,
    )
    if re.match(r"\s*PRAGMA\b", sql, flags=re.IGNORECASE):
        return None
    ignored_insert = re.match(r"\s*INSERT\s+OR\s+IGNORE\s+INTO\b", sql, flags=re.IGNORECASE)
    if ignored_insert:
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, count=1, flags=re.IGNORECASE)
        sql = sql.rstrip().removesuffix(";") + " ON CONFLICT DO NOTHING"
    return sql.replace("?", "%s")


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql: str, params=()):
        sql = postgres_sql(sql)
        if sql is None:
            return None
        return self.connection.execute(sql, params)

    def executescript(self, script: str):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def db():
    if SUPABASE_DB_URL:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("SUPABASE_DB_URL is configured but the psycopg package is missing") from exc
        return PostgresConnection(psycopg.connect(SUPABASE_DB_URL, row_factory=dict_row))
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def rows(sql: str, params=()):
    conn = db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def row(sql: str, params=()):
    conn = db()
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def hydrate_json(obj: dict, fields: list[str]) -> dict:
    for field in fields:
        raw = obj.pop(field, None)
        if raw is not None:
            try:
                obj[field.removesuffix("_json")] = json.loads(raw or "[]")
            except json.JSONDecodeError:
                obj[field.removesuffix("_json")] = []
    return obj


def ensure_db() -> None:
    seed = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    conn = db()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, role TEXT NOT NULL,
            firebase_uid TEXT UNIQUE, provider TEXT DEFAULT 'local', is_guest INTEGER DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS courses(
            id TEXT PRIMARY KEY, title TEXT NOT NULL, sanskrit TEXT, category TEXT, icon TEXT,
            level TEXT, duration TEXT, description TEXT, era TEXT, highlights_json TEXT DEFAULT '[]',
            texts_json TEXT DEFAULT '[]', created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS lessons(
            id TEXT PRIMARY KEY, course_id TEXT NOT NULL, title TEXT, sanskrit TEXT, overview TEXT,
            content TEXT, takeaways_json TEXT DEFAULT '[]', practical TEXT, minutes INTEGER, difficulty TEXT,
            position INTEGER, iast TEXT DEFAULT '', english_meaning TEXT DEFAULT '', meter TEXT DEFAULT '',
            historical_context TEXT DEFAULT '', modern_parallel TEXT DEFAULT '', source TEXT DEFAULT '',
            concepts_json TEXT DEFAULT '[]', checkpoint_json TEXT DEFAULT '{}', word_by_word_json TEXT DEFAULT '[]',
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS paths(
            id TEXT PRIMARY KEY, title TEXT NOT NULL, level TEXT, weeks INTEGER, description TEXT,
            created_by TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS path_courses(
            path_id TEXT, course_id TEXT, position INTEGER, PRIMARY KEY(path_id, course_id),
            FOREIGN KEY(path_id) REFERENCES paths(id) ON DELETE CASCADE, FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS scholars(
            id TEXT PRIMARY KEY, name TEXT, sanskrit TEXT, era TEXT, domain TEXT, work TEXT,
            contribution TEXT, location TEXT DEFAULT 'Indian subcontinent', quote TEXT DEFAULT '', quote_note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS progress(
            user_id TEXT, lesson_id TEXT, completed INTEGER DEFAULT 0, updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, lesson_id), FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS quizzes(
            id TEXT PRIMARY KEY, course_id TEXT, title TEXT NOT NULL, description TEXT,
            created_by TEXT, created_at TEXT NOT NULL, FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS quiz_questions(
            id TEXT PRIMARY KEY, quiz_id TEXT, question TEXT, options_json TEXT, answer_index INTEGER, explanation TEXT,
            FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS attempts(
            id TEXT PRIMARY KEY, user_id TEXT, quiz_id TEXT, score INTEGER, total INTEGER, percent REAL, created_at TEXT NOT NULL,
            FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS bookmarks(
            user_id TEXT, lesson_id TEXT, created_at TEXT NOT NULL, PRIMARY KEY(user_id, lesson_id),
            FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS notes(
            id TEXT PRIMARY KEY, user_id TEXT, lesson_id TEXT, content TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(lesson_id) REFERENCES lessons(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS certificates(
            id TEXT PRIMARY KEY, user_id TEXT, course_id TEXT, certificate_no TEXT UNIQUE, issued_at TEXT NOT NULL,
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS badges(id TEXT PRIMARY KEY, name TEXT, icon TEXT, description TEXT, condition_text TEXT);
        CREATE TABLE IF NOT EXISTS user_badges(user_id TEXT, badge_id TEXT, awarded_at TEXT NOT NULL, PRIMARY KEY(user_id, badge_id));
        CREATE TABLE IF NOT EXISTS study_days(user_id TEXT, day_key TEXT, created_at TEXT NOT NULL, PRIMARY KEY(user_id, day_key));
        CREATE TABLE IF NOT EXISTS flashcards(
            id TEXT PRIMARY KEY, course_id TEXT, category TEXT, front TEXT, back TEXT, parallel TEXT,
            position INTEGER DEFAULT 0, FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS flashcard_reviews(
            user_id TEXT NOT NULL, card_id TEXT NOT NULL, repetitions INTEGER NOT NULL DEFAULT 0,
            interval_days REAL NOT NULL DEFAULT 0, due_at TEXT NOT NULL, last_reviewed_at TEXT NOT NULL,
            PRIMARY KEY(user_id, card_id),
            FOREIGN KEY(card_id) REFERENCES flashcards(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS classrooms(
            id TEXT PRIMARY KEY, teacher_id TEXT NOT NULL, name TEXT NOT NULL,
            join_code TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS classroom_members(
            classroom_id TEXT NOT NULL, user_id TEXT NOT NULL, joined_at TEXT NOT NULL,
            PRIMARY KEY(classroom_id,user_id),
            FOREIGN KEY(classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS classroom_assignments(
            id TEXT PRIMARY KEY, classroom_id TEXT NOT NULL, course_id TEXT, quiz_id TEXT,
            created_at TEXT NOT NULL,
            CHECK ((course_id IS NOT NULL AND quiz_id IS NULL) OR (course_id IS NULL AND quiz_id IS NOT NULL)),
            FOREIGN KEY(classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE,
            FOREIGN KEY(course_id) REFERENCES courses(id) ON DELETE CASCADE,
            FOREIGN KEY(quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS daily_wisdom(
            id TEXT PRIMARY KEY, sanskrit TEXT, iast TEXT, translation TEXT, source TEXT, url TEXT, word_by_word_json TEXT
        );
        CREATE TABLE IF NOT EXISTS chat_messages(
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """
    )
    # Migrate an older database only for IKS data. Retired syllabus tables are intentionally not used.
    for table, col, ddl in [
        ("users", "firebase_uid", "TEXT"), ("users", "provider", "TEXT DEFAULT 'local'"), ("users", "is_guest", "INTEGER DEFAULT 0"),
        ("courses", "texts_json", "TEXT DEFAULT '[]'"), ("lessons", "iast", "TEXT DEFAULT ''"),
        ("lessons", "english_meaning", "TEXT DEFAULT ''"), ("lessons", "meter", "TEXT DEFAULT ''"),
        ("lessons", "historical_context", "TEXT DEFAULT ''"), ("lessons", "modern_parallel", "TEXT DEFAULT ''"),
        ("lessons", "source", "TEXT DEFAULT ''"), ("lessons", "concepts_json", "TEXT DEFAULT '[]'"),
        ("lessons", "checkpoint_json", "TEXT DEFAULT '{}'"), ("lessons", "word_by_word_json", "TEXT DEFAULT '[]'"),
        ("scholars", "location", "TEXT DEFAULT 'Indian subcontinent'"), ("scholars", "quote", "TEXT DEFAULT ''"),
        ("scholars", "quote_note", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass

    now = now_iso()
    for c in seed["courses"]:
        conn.execute(
            "INSERT INTO courses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (
                c["id"], c["title"], c.get("sanskrit", ""), c.get("category", "General"), c.get("icon", "📘"),
                c.get("level", "Beginner"), c.get("duration", "2 weeks"), c.get("description", ""), c.get("era", ""),
                json.dumps(c.get("highlights", []), ensure_ascii=False), json.dumps(c.get("texts", []), ensure_ascii=False), "seed", now,
            ),
        )
        # Keep authored seed content stable; teacher-created courses are untouched.
        conn.execute(
            "UPDATE courses SET title=?,sanskrit=?,category=?,icon=?,level=?,duration=?,description=?,era=?,highlights_json=?,texts_json=? WHERE id=? AND created_by='seed'",
            (c["title"], c.get("sanskrit", ""), c.get("category", "General"), c.get("icon", "📘"), c.get("level", "Beginner"), c.get("duration", "2 weeks"), c.get("description", ""), c.get("era", ""), json.dumps(c.get("highlights", []), ensure_ascii=False), json.dumps(c.get("texts", []), ensure_ascii=False), c["id"]),
        )
        for i, l in enumerate(c.get("lessons", [])):
            conn.execute(
                """INSERT INTO lessons(id,course_id,title,sanskrit,overview,content,takeaways_json,practical,minutes,difficulty,position,iast,english_meaning,meter,historical_context,modern_parallel,source,concepts_json,checkpoint_json,word_by_word_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                course_id=excluded.course_id,title=excluded.title,sanskrit=excluded.sanskrit,overview=excluded.overview,
                content=excluded.content,takeaways_json=excluded.takeaways_json,practical=excluded.practical,
                minutes=excluded.minutes,difficulty=excluded.difficulty,position=excluded.position,iast=excluded.iast,
                english_meaning=excluded.english_meaning,meter=excluded.meter,historical_context=excluded.historical_context,
                modern_parallel=excluded.modern_parallel,source=excluded.source,concepts_json=excluded.concepts_json,
                checkpoint_json=excluded.checkpoint_json,word_by_word_json=excluded.word_by_word_json""",
                (
                    l["id"], c["id"], l["title"], l.get("sanskrit", ""), l.get("overview", ""), l.get("content", ""),
                    json.dumps(l.get("takeaways", []), ensure_ascii=False), l.get("practical", ""), int(l.get("minutes", 10)),
                    l.get("difficulty", "Beginner"), i, l.get("iast", l.get("sanskrit", "")), l.get("english_meaning", l.get("overview", "")),
                    l.get("meter", "Textual / instructional passage"), l.get("historical_context", ""), l.get("modern_parallel", ""),
                    l.get("source", ""), json.dumps(l.get("concepts", []), ensure_ascii=False), json.dumps(l.get("checkpoint", {}), ensure_ascii=False),
                    json.dumps(l.get("word_by_word", []), ensure_ascii=False),
                ),
            )

    for s in seed.get("scholars", []):
        conn.execute("""INSERT INTO scholars VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,sanskrit=excluded.sanskrit,era=excluded.era,domain=excluded.domain,work=excluded.work,
            contribution=excluded.contribution,location=excluded.location,quote=excluded.quote,quote_note=excluded.quote_note""", (
            s["id"], s["name"], s.get("sanskrit", ""), s.get("era", ""), s.get("domain", ""), s.get("work", ""),
            s.get("contribution", ""), s.get("location", "Indian subcontinent"), s.get("quote", ""), s.get("quote_note", ""),
        ))
    for p in seed.get("paths", []):
        conn.execute("INSERT INTO paths VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING", (p["id"], p["title"], p.get("level", "Beginner"), int(p.get("weeks", 4)), f"{p['title']} guided curriculum", "seed", now))
        conn.execute("UPDATE paths SET title=?,level=?,weeks=?,description=? WHERE id=? AND created_by='seed'", (p["title"], p.get("level", "Beginner"), int(p.get("weeks", 4)), f"{p['title']} guided curriculum", p["id"]))
        conn.execute("DELETE FROM path_courses WHERE path_id=?", (p["id"],))
        for i, cid in enumerate(p.get("course_ids", [])):
            conn.execute("INSERT INTO path_courses VALUES (?,?,?) ON CONFLICT(path_id,course_id) DO NOTHING", (p["id"], cid, i))
    for b in seed.get("badges", []):
        conn.execute("""INSERT INTO badges VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,icon=excluded.icon,description=excluded.description,condition_text=excluded.condition_text""", (b["id"], b["name"], b["icon"], b["description"], b["condition"]))
    for w in seed.get("daily_wisdom", []):
        conn.execute("""INSERT INTO daily_wisdom VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            sanskrit=excluded.sanskrit,iast=excluded.iast,translation=excluded.translation,source=excluded.source,
            url=excluded.url,word_by_word_json=excluded.word_by_word_json""", (w["id"], w["sanskrit"], w["iast"], w["translation"], w["source"], w.get("url", ""), json.dumps(w.get("word_by_word", []), ensure_ascii=False)))
    for i, f in enumerate(seed.get("flashcards", [])):
        conn.execute("""INSERT INTO flashcards VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            course_id=excluded.course_id,category=excluded.category,front=excluded.front,back=excluded.back,
            parallel=excluded.parallel,position=excluded.position""", (f["id"], f["course_id"], f.get("category", "General"), f["front"], f["back"], f.get("parallel", ""), i))

    # Demo local accounts are useful when Firebase is not configured.
    conn.execute("INSERT OR IGNORE INTO users(id,name,email,role,provider,is_guest,created_at) VALUES (?,?,?,?,?,?,?)", ("demo-student", "Aarav Sharma", "student@iksphere.local", "learner", "local", 0, now))
    conn.execute("INSERT OR IGNORE INTO users(id,name,email,role,provider,is_guest,created_at) VALUES (?,?,?,?,?,?,?)", ("demo-teacher", "Ananya Deshmukh", "teacher@iksphere.local", "teacher", "local", 0, now))

    if not conn.execute("SELECT 1 FROM quizzes LIMIT 1").fetchone():
        qid = "quiz_ganita_foundations"
        conn.execute("INSERT INTO quizzes VALUES (?,?,?,?,?,?)", (qid, "course_ganita", "Ganita Foundations", "Geometry and number concepts checkpoint.", "seed", now))
        questions = [
            ("q1", "Which text is associated with the geometric construction known as the Baudhayana Sulba Sutra?", ["Baudhayana Sulba Sutra", "Charaka Samhita", "Surya Siddhanta", "Ashtadhyayi"], 0, "The Baudhayana Sulba Sutra is a Śulbasūtra text concerned with geometric constructions for ritual spaces."),
            ("q2", "Which mathematician is associated with rules for arithmetic involving zero in the Brahmasphutasiddhanta?", ["Brahmagupta", "Panini", "Sushruta", "Kanada"], 0, "Brahmagupta's Brahmasphutasiddhanta includes arithmetic rules for zero and signed quantities."),
            ("q3", "What is the best historical way to describe the Kerala school's infinite-series work?", ["A mathematical approximation tradition", "A modern computer language", "A metal alloy", "A surgical manual"], 0, "The Kerala mathematical-astronomical tradition developed series methods for trigonometric quantities and π."),
        ]
        for q in questions:
            conn.execute("INSERT INTO quiz_questions VALUES (?,?,?,?,?,?)", (q[0], qid, q[1], json.dumps(q[2], ensure_ascii=False), q[3], q[4]))
    conn.commit()
    conn.close()


@asynccontextmanager
async def lifespan(_app):
    ensure_db()
    yield


app = FastAPI(title="IKSphere API", version=APP_VERSION, description="Mobile-first Indian Knowledge Systems learning API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS or ["*"], allow_credentials=False, allow_methods=["*"], allow_headers=["*"])


class LoginIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(min_length=5, max_length=200)
    role: str = "learner"

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str):
        value = value.strip().lower()
        if "@" not in value:
            raise ValueError("Enter a valid email address")
        return value


class GuestIn(BaseModel):
    name: str = Field(default="Guest Learner", min_length=2, max_length=100)


class FirebaseAuthIn(BaseModel):
    id_token: str = Field(min_length=20)
    name: str = Field(min_length=2, max_length=100)
    email: str = Field(default="", max_length=200)
    provider: str = Field(default="firebase")


class ProgressIn(BaseModel):
    completed: bool = True


class BookmarkIn(BaseModel):
    lesson_id: str = Field(min_length=1)


class NoteIn(BaseModel):
    lesson_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=5000)


class QuizAnswerIn(BaseModel):
    answers: list[int] = Field(min_length=1, max_length=100)


class FlashcardReviewIn(BaseModel):
    rating: Literal["again", "hard", "good", "easy"]


class AskIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    context: str = Field(default="", max_length=7000)
    history: list[dict] = Field(default_factory=list, max_length=20)


class LessonIn(BaseModel):
    id: Optional[str] = None
    title: str = Field(min_length=1, max_length=200)
    sanskrit: str = ""
    iast: str = ""
    overview: str = ""
    content: str = ""
    english_meaning: str = ""
    meter: str = ""
    historical_context: str = ""
    modern_parallel: str = ""
    source: str = ""
    takeaways: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    word_by_word: list[str] = Field(default_factory=list)
    practical: str = ""
    minutes: int = Field(default=10, ge=1, le=600)
    difficulty: str = "Beginner"
    checkpoint: dict = Field(default_factory=dict)


class CourseIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    texts: list[str] = Field(default_factory=list)
    sanskrit: str = ""
    category: str = "General"
    icon: str = "📘"
    level: str = "Beginner"
    duration: str = "2 weeks"
    description: str = ""
    era: str = "Classical / historical study"
    highlights: list[str] = Field(default_factory=list)
    lessons: list[LessonIn] = Field(default_factory=list, max_length=40)


class QuizQuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    options: list[str] = Field(min_length=2, max_length=6)
    answer_index: int = 0
    explanation: str = ""

    @field_validator("options")
    @classmethod
    def valid_options(cls, value):
        cleaned = [str(x).strip() for x in value]
        if any(not x for x in cleaned):
            raise ValueError("Quiz options cannot be empty")
        return cleaned


class QuizIn(BaseModel):
    course_id: str = Field(min_length=1)
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    questions: list[QuizQuestionIn] = Field(min_length=1, max_length=50)


class PathIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    level: str = "Beginner"
    weeks: int = Field(default=4, ge=1, le=52)
    course_ids: list[str] = Field(min_length=1, max_length=20)


class ClassroomIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)


class ClassroomJoinIn(BaseModel):
    join_code: str = Field(min_length=6, max_length=20)


class ClassroomAssignmentIn(BaseModel):
    course_id: Optional[str] = None
    quiz_id: Optional[str] = None

    @model_validator(mode="after")
    def require_one_assignment_target(self):
        if (self.course_id is None) == (self.quiz_id is None):
            raise ValueError("Choose exactly one course or quiz to assign")
        return self


def decode_jwt_payload(token: str) -> dict:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            raise ValueError
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(401, "Invalid Firebase token") from exc


def verify_firebase_token(token: str) -> dict:
    if not FIREBASE_PROJECT_ID:
        if SUPABASE_DB_URL:
            raise HTTPException(503, "Set FIREBASE_PROJECT_ID before using the hosted database")
        payload = decode_jwt_payload(token)
        uid_claim = payload.get("user_id") or payload.get("sub")
        if not uid_claim:
            raise HTTPException(401, "Firebase token does not contain a user id")
        # Local configuration intentionally supports development without server credentials.
        payload["uid"] = uid_claim
        payload["_verified"] = False
        return payload
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
        payload = google_id_token.verify_firebase_token(token, google_requests.Request(), audience=FIREBASE_PROJECT_ID)
        payload["uid"] = payload.get("uid") or payload.get("user_id") or payload.get("sub")
        payload["_verified"] = True
        return payload
    except Exception as exc:
        raise HTTPException(401, f"Firebase token verification failed: {exc}") from exc


def current_user(user_id: Optional[str] = None) -> dict:
    user = row("SELECT * FROM users WHERE id=?", (user_id or "demo-student",))
    if not user:
        raise HTTPException(404, "User not found")
    return user


def require_teacher(user_id: str) -> dict:
    user = current_user(user_id)
    if user["role"] != "teacher":
        raise HTTPException(403, "Teacher role required")
    return user


def auth_row_for_user(user_id: str, authorization: Optional[str]) -> None:
    # Demo mode is unauthenticated only with local SQLite and no Firebase project.
    if not authorization:
        if FIREBASE_PROJECT_ID or SUPABASE_DB_URL:
            raise HTTPException(401, "Firebase authentication is required")
        return
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Use a Bearer Firebase ID token")
    token = authorization.split(" ", 1)[1].strip()
    payload = verify_firebase_token(token)
    firebase_uid = payload.get("uid") or payload.get("sub")
    if firebase_uid:
        existing = row("SELECT id FROM users WHERE firebase_uid=?", (firebase_uid,))
        if not existing:
            raise HTTPException(403, "Authenticated Firebase account is not registered with this API")
        if existing["id"] != user_id:
            raise HTTPException(403, "Authenticated user does not match requested account")


def mark_study_day(user_id: str) -> None:
    conn = db(); conn.execute("INSERT OR IGNORE INTO study_days VALUES (?,?,?)", (user_id, today_key(), now_iso())); conn.commit(); conn.close()


def streak_payload(user_id: str) -> dict:
    days = {r["day_key"] for r in rows("SELECT day_key FROM study_days WHERE user_id=? ORDER BY day_key DESC LIMIT 90", (user_id,))}
    current = 0; cursor = date.today()
    while cursor.isoformat() in days:
        current += 1; cursor -= timedelta(days=1)
    last7 = [(date.today() - timedelta(days=i)).isoformat() in days for i in range(6, -1, -1)]
    return {"current": current, "last7": last7, "studied_days": len(days)}


def learner_xp(user_id: str) -> int:
    lesson_xp = int(row("SELECT COUNT(*) c FROM progress WHERE user_id=? AND completed=1", (user_id,))["c"]) * 20
    quiz_xp = int(row("SELECT COALESCE(SUM(score),0) s FROM attempts WHERE user_id=?", (user_id,))["s"])
    return lesson_xp + quiz_xp


def rank_for_xp(xp: int) -> dict:
    if xp >= 500: return {"key": "acharya", "name": "Acharya", "subtitle": "Master", "min": 500}
    if xp >= 250: return {"key": "upadhyaya", "name": "Upadhyaya", "subtitle": "Scholar", "min": 250}
    if xp >= 100: return {"key": "vidyarthi", "name": "Vidyarthi", "subtitle": "Student", "min": 100}
    return {"key": "jijnasu", "name": "Jijnasu", "subtitle": "Seeker", "min": 0}


def award_badge(user_id: str, badge_id: str) -> None:
    if not row("SELECT id FROM badges WHERE id=?", (badge_id,)): return
    conn = db(); conn.execute("INSERT OR IGNORE INTO user_badges VALUES (?,?,?)", (user_id, badge_id, now_iso())); conn.commit(); conn.close()


def course_completion(user_id: str, course_id: str) -> tuple[int, int]:
    total = int(row("SELECT COUNT(*) c FROM lessons WHERE course_id=?", (course_id,))["c"])
    completed = int(row("SELECT COUNT(*) c FROM progress p JOIN lessons l ON l.id=p.lesson_id WHERE p.user_id=? AND p.completed=1 AND l.course_id=?", (user_id, course_id))["c"])
    return total, completed


def maybe_issue_certificate(user_id: str, course_id: str) -> None:
    total, completed = course_completion(user_id, course_id)
    if total and total == completed and not row("SELECT 1 FROM certificates WHERE user_id=? AND course_id=?", (user_id, course_id)):
        conn = db(); conn.execute("INSERT INTO certificates VALUES (?,?,?,?,?)", (uid("cert"), user_id, course_id, f"IKS-{datetime.now().year}-{secrets.token_hex(3).upper()}", now_iso())); conn.commit(); conn.close()


def refresh_achievement_badges(user_id: str) -> None:
    completed = int(row("SELECT COUNT(*) c FROM progress WHERE user_id=? AND completed=1", (user_id,))["c"])
    if completed >= 1: award_badge(user_id, "badge_first_step")
    if completed >= 5: award_badge(user_id, "badge_scholar")
    if streak_payload(user_id)["current"] >= 7: award_badge(user_id, "badge_consistent")
    attempts = [float(x["percent"]) for x in rows("SELECT percent FROM attempts WHERE user_id=?", (user_id,))]
    if sum(1 for x in attempts if x >= 90) >= 3: award_badge(user_id, "badge_quiz_champion")
    mapping = {"badge_math_pioneer": "course_ganita", "badge_stars": "course_khagola", "badge_ayurveda": "course_ayurveda", "badge_metallurgy": "course_rasayana"}
    for badge_id, course_id in mapping.items():
        total, done = course_completion(user_id, course_id)
        if total and total == done: award_badge(user_id, badge_id)


def hydrate_course(c: dict, user_id: str) -> dict:
    hydrate_json(c, ["highlights_json", "texts_json"])
    c["lessons"] = []
    for lesson in rows("SELECT * FROM lessons WHERE course_id=? ORDER BY position", (c["id"],)):
        hydrate_json(lesson, ["takeaways_json", "concepts_json", "checkpoint_json", "word_by_word_json"])
        progress_row = row("SELECT completed FROM progress WHERE user_id=? AND lesson_id=?", (user_id, lesson["id"]))
        lesson["completed"] = bool(progress_row["completed"]) if progress_row else False
        lesson["bookmarked"] = bool(row("SELECT 1 FROM bookmarks WHERE user_id=? AND lesson_id=?", (user_id, lesson["id"])) )
        c["lessons"].append(lesson)
    scholars = []
    for sid in COURSE_SCHOLARS.get(c["id"], []):
        s = row("SELECT id,name,sanskrit,era,domain,work FROM scholars WHERE id=?", (sid,))
        if s: scholars.append(s)
    c["scholars"] = scholars
    total = len(c["lessons"]); done = sum(1 for l in c["lessons"] if l["completed"])
    c["progress_pct"] = round(done / total * 100, 1) if total else 0
    return c


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "IKSphere API", "version": APP_VERSION, "time": now_iso(), "groq_configured": bool(GROQ_API_KEY), "firebase_configured": bool(FIREBASE_PROJECT_ID), "supabase_configured": bool(SUPABASE_DB_URL)}


@app.get("/api/config")
def config():
    return {"version": APP_VERSION, "groq_configured": bool(GROQ_API_KEY), "groq_model": GROQ_MODEL, "firebase_server_verification": bool(FIREBASE_PROJECT_ID), "teacher_email_count": len(TEACHER_EMAILS), "supabase_configured": bool(SUPABASE_DB_URL), "database": "supabase" if SUPABASE_DB_URL else "sqlite"}


@app.post("/api/auth/guest")
def guest_login(data: GuestIn):
    now = now_iso(); user_id = uid("guest"); email = f"{user_id}@guest.iksphere.local"
    conn = db(); conn.execute("INSERT INTO users(id,name,email,role,provider,is_guest,created_at) VALUES (?,?,?,?,?,?,?)", (user_id, data.name.strip(), email, "learner", "anonymous", 1, now)); conn.commit(); conn.close(); return current_user(user_id)


@app.post("/api/auth/firebase")
def firebase_login(data: FirebaseAuthIn):
    payload = verify_firebase_token(data.id_token)
    firebase_uid = payload.get("uid") or payload.get("sub")
    if not firebase_uid: raise HTTPException(401, "Firebase user id missing")
    email = (data.email or payload.get("email") or "").strip().lower()
    name = (data.name or payload.get("name") or (email.split("@")[0] if email else "Learner")).strip()[:100]
    provider = data.provider or "firebase"
    is_guest = 1 if provider == "anonymous" else 0
    existing = row("SELECT * FROM users WHERE firebase_uid=?", (firebase_uid,))
    if existing:
        role = existing["role"]
        if email in TEACHER_EMAILS: role = "teacher"
        conn = db(); conn.execute("UPDATE users SET name=?,email=?,role=?,provider=?,is_guest=? WHERE id=?", (name, email, role, provider, is_guest, existing["id"])); conn.commit(); conn.close(); return current_user(existing["id"])
    role = "teacher" if email in TEACHER_EMAILS else "learner"
    user = {"id": uid("user"), "name": name, "email": email, "role": role, "firebase_uid": firebase_uid, "provider": provider, "is_guest": is_guest, "created_at": now_iso()}
    conn = db(); conn.execute("INSERT INTO users(id,name,email,role,firebase_uid,provider,is_guest,created_at) VALUES (?,?,?,?,?,?,?,?)", tuple(user.values())); conn.commit(); conn.close(); return user


@app.post("/api/auth/login")
def local_login(data: LoginIn):
    if FIREBASE_PROJECT_ID or SUPABASE_DB_URL:
        raise HTTPException(403, "Local demo login is disabled when hosted authentication or storage is configured")
    email = data.email.strip().lower(); existing = row("SELECT * FROM users WHERE email=?", (email,))
    if existing: return existing
    role = data.role if data.role in ("learner", "teacher") else "learner"
    user = {"id": uid("user"), "name": data.name.strip(), "email": email, "role": role, "firebase_uid": None, "provider": "local", "is_guest": 0, "created_at": now_iso()}
    conn = db(); conn.execute("INSERT INTO users(id,name,email,role,firebase_uid,provider,is_guest,created_at) VALUES (?,?,?,?,?,?,?,?)", tuple(user.values())); conn.commit(); conn.close(); return user


@app.get("/api/users/{user_id}/dashboard")
def dashboard(user_id: str, authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); user = current_user(user_id)
    total = int(row("SELECT COUNT(*) c FROM lessons")["c"]); completed = int(row("SELECT COUNT(*) c FROM progress WHERE user_id=? AND completed=1", (user_id,))["c"])
    avg = float(row("SELECT COALESCE(AVG(percent),0) a FROM attempts WHERE user_id=?", (user_id,))["a"] or 0); bookmarks = int(row("SELECT COUNT(*) c FROM bookmarks WHERE user_id=?", (user_id,))["c"])
    notes = int(row("SELECT COUNT(*) c FROM notes WHERE user_id=?", (user_id,))["c"]); certs = int(row("SELECT COUNT(*) c FROM certificates WHERE user_id=?", (user_id,))["c"]); badges = int(row("SELECT COUNT(*) c FROM user_badges WHERE user_id=?", (user_id,))["c"])
    recent = rows("SELECT l.id lesson_id,l.title,c.title course_title,p.updated_at FROM progress p JOIN lessons l ON l.id=p.lesson_id JOIN courses c ON c.id=l.course_id WHERE p.user_id=? AND p.completed=1 ORDER BY p.updated_at DESC LIMIT 5", (user_id,))
    xp = learner_xp(user_id)
    return {"user": user, "stats": {"total_lessons": total, "completed_lessons": completed, "completion_pct": round(completed / total * 100, 1) if total else 0, "avg_quiz": round(avg, 1), "bookmarks": bookmarks, "notes": notes, "certificates": certs, "badges": badges, "quiz_attempts": int(row("SELECT COUNT(*) c FROM attempts WHERE user_id=?", (user_id,))["c"]), "xp": xp, "rank": rank_for_xp(xp)}, "recent": recent, "streak": streak_payload(user_id)}


@app.get("/api/home/{user_id}")
def home(user_id: str, authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    wisdom = rows("SELECT * FROM daily_wisdom ORDER BY id")
    if not wisdom: raise HTTPException(503, "Daily wisdom data is unavailable")
    w = wisdom[date.today().toordinal() % len(wisdom)]; hydrate_json(w, ["word_by_word_json"])
    scholars = rows("SELECT * FROM scholars ORDER BY name"); scholar = scholars[date.today().toordinal() % len(scholars)] if scholars else None
    challenge = rows("SELECT id,course_id,title,description FROM quizzes ORDER BY created_at ASC LIMIT 1")
    featured = rows("SELECT id,title,category,icon FROM courses ORDER BY title LIMIT 4")
    return {"wisdom": w, "scholar_of_day": scholar, "daily_quiz": challenge[0] if challenge else None, "featured_courses": featured}


@app.get("/api/search")
def search(q: str = "", user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id); query = q.strip().lower()
    if not query: return {"courses": [], "lessons": [], "scholars": []}
    like = f"%{query}%"
    return {
        "courses": rows("SELECT id,title,category,description,era,icon FROM courses WHERE lower(title||' '||category||' '||description||' '||era) LIKE ? ORDER BY title", (like,)),
        "lessons": rows("SELECT l.id,l.title,l.course_id,c.title course_title,l.overview FROM lessons l JOIN courses c ON c.id=l.course_id WHERE lower(l.title||' '||l.overview||' '||l.content||' '||l.concepts_json) LIKE ? ORDER BY l.position LIMIT 40", (like,)),
        "scholars": rows("SELECT id,name,domain,work,contribution FROM scholars WHERE lower(name||' '||domain||' '||work||' '||contribution) LIKE ? ORDER BY name", (like,)),
    }


@app.get("/api/courses")
def courses(user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    return [hydrate_course(c, user_id) for c in rows("SELECT * FROM courses ORDER BY title")]


@app.get("/api/courses/{course_id}")
def course(course_id: str, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    c = row("SELECT * FROM courses WHERE id=?", (course_id,))
    if not c: raise HTTPException(404, "Course not found")
    return hydrate_course(c, user_id)


@app.post("/api/courses")
def create_course(data: CourseIn, user_id: str = "demo-teacher", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); require_teacher(user_id); now = now_iso(); cid = uid("course"); conn = db()
    try:
        conn.execute("BEGIN")
        conn.execute("INSERT INTO courses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (cid, data.title.strip(), data.sanskrit, data.category, data.icon[:8], data.level, data.duration, data.description[:1200], data.era, json.dumps(data.highlights, ensure_ascii=False), json.dumps(data.texts, ensure_ascii=False), user_id, now))
        for i, l in enumerate(data.lessons):
            lid = l.id or uid("lesson")
            conn.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (lid, cid, l.title.strip(), l.sanskrit, l.overview, l.content, json.dumps(l.takeaways, ensure_ascii=False), l.practical, l.minutes, l.difficulty, i, l.iast, l.english_meaning, l.meter, l.historical_context, l.modern_parallel, l.source, json.dumps(l.concepts, ensure_ascii=False), json.dumps(l.checkpoint, ensure_ascii=False), json.dumps(l.word_by_word, ensure_ascii=False)))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()
    return {"id": cid, "message": "Course created"}


@app.put("/api/courses/{course_id}")
def update_course(course_id: str, data: CourseIn, user_id: str = "demo-teacher", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); require_teacher(user_id)
    existing = row("SELECT * FROM courses WHERE id=?", (course_id,))
    if not existing: raise HTTPException(404, "Course not found")
    if existing["created_by"] != user_id and existing["created_by"] != "seed": raise HTTPException(403, "You can only edit your own courses")
    conn = db()
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE courses SET title=?,sanskrit=?,category=?,icon=?,level=?,duration=?,description=?,era=?,highlights_json=?,texts_json=? WHERE id=?", (data.title.strip(), data.sanskrit, data.category, data.icon[:8], data.level, data.duration, data.description[:1200], data.era, json.dumps(data.highlights, ensure_ascii=False), json.dumps(data.texts, ensure_ascii=False), course_id))
        keep = set()
        for i, l in enumerate(data.lessons):
            lid = l.id or uid("lesson"); keep.add(lid)
            conn.execute("""INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                course_id=excluded.course_id,title=excluded.title,sanskrit=excluded.sanskrit,overview=excluded.overview,
                content=excluded.content,takeaways_json=excluded.takeaways_json,practical=excluded.practical,
                minutes=excluded.minutes,difficulty=excluded.difficulty,position=excluded.position,iast=excluded.iast,
                english_meaning=excluded.english_meaning,meter=excluded.meter,historical_context=excluded.historical_context,
                modern_parallel=excluded.modern_parallel,source=excluded.source,concepts_json=excluded.concepts_json,
                checkpoint_json=excluded.checkpoint_json,word_by_word_json=excluded.word_by_word_json""",
                (lid, course_id, l.title.strip(), l.sanskrit, l.overview, l.content, json.dumps(l.takeaways, ensure_ascii=False), l.practical, l.minutes, l.difficulty, i, l.iast, l.english_meaning, l.meter, l.historical_context, l.modern_parallel, l.source, json.dumps(l.concepts, ensure_ascii=False), json.dumps(l.checkpoint, ensure_ascii=False), json.dumps(l.word_by_word, ensure_ascii=False)))
        old = rows("SELECT id FROM lessons WHERE course_id=?", (course_id,))
        for l in old:
            if l["id"] not in keep:
                conn.execute("DELETE FROM lessons WHERE id=?", (l["id"],))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    return {"id": course_id, "message": "Course updated"}


@app.get("/api/paths")
def paths(user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    result = []
    for p in rows("SELECT * FROM paths ORDER BY weeks,title"):
        p["courses"] = []
        for pc in rows("SELECT c.id,c.title,c.icon,c.level,c.duration FROM path_courses pc JOIN courses c ON c.id=pc.course_id WHERE pc.path_id=? ORDER BY pc.position", (p["id"],)):
            total, done = course_completion(user_id, pc["id"]); pc["lesson_count"] = total; pc["progress_pct"] = round(done / total * 100, 1) if total else 0; p["courses"].append(pc)
        lesson_total = sum(c["lesson_count"] for c in p["courses"]); done_total = sum(round(c["lesson_count"] * (c["progress_pct"] / 100)) for c in p["courses"])
        p["progress_pct"] = round(done_total / lesson_total * 100, 1) if lesson_total else 0
        result.append(p)
    return result


@app.post("/api/paths")
def create_path(data: PathIn, user_id: str = "demo-teacher", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); require_teacher(user_id); now = now_iso(); pid = uid("path"); conn = db()
    try:
        conn.execute("BEGIN")
        for cid in data.course_ids:
            if not conn.execute("SELECT 1 FROM courses WHERE id=?", (cid,)).fetchone(): raise HTTPException(404, f"Course not found: {cid}")
        conn.execute("INSERT INTO paths VALUES (?,?,?,?,?,?,?)", (pid, data.title.strip(), data.level, data.weeks, f"{data.title.strip()} guided curriculum", user_id, now))
        for i, cid in enumerate(dict.fromkeys(data.course_ids)):
            conn.execute("INSERT INTO path_courses VALUES (?,?,?)", (pid, cid, i))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    return {"id": pid, "message": "Learning path created"}


@app.get("/api/classrooms")
def classrooms(user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); user = current_user(user_id)
    if user["role"] == "teacher":
        return rows(
            """SELECT c.*,COUNT(m.user_id) student_count FROM classrooms c
            LEFT JOIN classroom_members m ON m.classroom_id=c.id
            WHERE c.teacher_id=? GROUP BY c.id ORDER BY c.created_at DESC""",
            (user_id,),
        )
    return rows(
        """SELECT c.id,c.name,c.teacher_id,c.created_at,u.name teacher_name,COUNT(a.id) assignment_count
        FROM classroom_members m JOIN classrooms c ON c.id=m.classroom_id
        JOIN users u ON u.id=c.teacher_id LEFT JOIN classroom_assignments a ON a.classroom_id=c.id
        WHERE m.user_id=? GROUP BY c.id,u.name ORDER BY c.created_at DESC""",
        (user_id,),
    )


@app.post("/api/classrooms")
def create_classroom(data: ClassroomIn, user_id: str = "demo-teacher", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); require_teacher(user_id)
    classroom_id = uid("class")
    join_code = secrets.token_hex(4).upper()
    created_at = now_iso()
    conn = db()
    try:
        conn.execute(
            "INSERT INTO classrooms(id,teacher_id,name,join_code,created_at) VALUES (?,?,?,?,?)",
            (classroom_id, user_id, data.name.strip(), join_code, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": classroom_id, "name": data.name.strip(), "join_code": join_code, "student_count": 0, "created_at": created_at}


@app.post("/api/classrooms/join")
def join_classroom(data: ClassroomJoinIn, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization)
    user = current_user(user_id)
    if user["role"] != "learner":
        raise HTTPException(403, "Only student accounts can join a classroom")
    classroom = row("SELECT id,name,teacher_id FROM classrooms WHERE join_code=?", (data.join_code.strip().upper(),))
    if not classroom:
        raise HTTPException(404, "Classroom join code not found")
    conn = db()
    try:
        conn.execute(
            "INSERT INTO classroom_members(classroom_id,user_id,joined_at) VALUES (?,?,?) ON CONFLICT(classroom_id,user_id) DO NOTHING",
            (classroom["id"], user_id, now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": classroom["id"], "name": classroom["name"], "message": "Joined classroom"}


@app.get("/api/classrooms/{classroom_id}/assignments")
def classroom_assignments(classroom_id: str, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    classroom = row("SELECT id,teacher_id FROM classrooms WHERE id=?", (classroom_id,))
    if not classroom:
        raise HTTPException(404, "Classroom not found")
    if classroom["teacher_id"] != user_id and not row(
        "SELECT 1 FROM classroom_members WHERE classroom_id=? AND user_id=?", (classroom_id, user_id)
    ):
        raise HTTPException(403, "Join this classroom to view its assignments")
    return rows(
        """SELECT a.id,a.created_at,a.course_id,c.title course_title,a.quiz_id,q.title quiz_title
        FROM classroom_assignments a
        LEFT JOIN courses c ON c.id=a.course_id
        LEFT JOIN quizzes q ON q.id=a.quiz_id
        WHERE a.classroom_id=? ORDER BY a.created_at DESC""",
        (classroom_id,),
    )


@app.post("/api/classrooms/{classroom_id}/assignments")
def assign_classroom_content(
    classroom_id: str,
    data: ClassroomAssignmentIn,
    user_id: str = "demo-teacher",
    authorization: Optional[str] = Header(default=None),
):
    auth_row_for_user(user_id, authorization); require_teacher(user_id)
    if not row("SELECT id FROM classrooms WHERE id=? AND teacher_id=?", (classroom_id, user_id)):
        raise HTTPException(404, "Classroom not found")
    if data.course_id and not row("SELECT id FROM courses WHERE id=?", (data.course_id,)):
        raise HTTPException(404, "Course not found")
    if data.quiz_id and not row("SELECT id FROM quizzes WHERE id=?", (data.quiz_id,)):
        raise HTTPException(404, "Quiz not found")
    assignment_id = uid("assignment")
    created_at = now_iso()
    conn = db()
    try:
        conn.execute(
            "INSERT INTO classroom_assignments(id,classroom_id,course_id,quiz_id,created_at) VALUES (?,?,?,?,?)",
            (assignment_id, classroom_id, data.course_id, data.quiz_id, created_at),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": assignment_id, "message": "Content assigned to classroom"}


@app.get("/api/scholars")
def scholars():
    return rows("SELECT * FROM scholars ORDER BY name")


@app.get("/api/lessons/{lesson_id}")
def lesson(lesson_id: str, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id); l = row("SELECT l.*,c.title course_title,c.category course_category FROM lessons l JOIN courses c ON c.id=l.course_id WHERE l.id=?", (lesson_id,))
    if not l: raise HTTPException(404, "Lesson not found")
    hydrate_json(l, ["takeaways_json", "concepts_json", "checkpoint_json", "word_by_word_json"])
    l["completed"] = bool(row("SELECT completed FROM progress WHERE user_id=? AND lesson_id=?", (user_id, lesson_id)))
    l["bookmarked"] = bool(row("SELECT 1 FROM bookmarks WHERE user_id=? AND lesson_id=?", (user_id, lesson_id)))
    return l


@app.post("/api/lessons/{lesson_id}/progress")
def progress_lesson(lesson_id: str, data: ProgressIn, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id); l = row("SELECT * FROM lessons WHERE id=?", (lesson_id,))
    if not l: raise HTTPException(404, "Lesson not found")
    conn = db(); conn.execute("INSERT INTO progress VALUES (?,?,?,?) ON CONFLICT(user_id,lesson_id) DO UPDATE SET completed=excluded.completed,updated_at=excluded.updated_at", (user_id, lesson_id, int(data.completed), now_iso())); conn.commit(); conn.close()
    if data.completed:
        mark_study_day(user_id); maybe_issue_certificate(user_id, l["course_id"]); refresh_achievement_badges(user_id)
    return {"completed": data.completed, "xp": learner_xp(user_id)}


@app.get("/api/bookmarks/{user_id}")
def bookmarks(user_id: str, authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    return rows("SELECT b.lesson_id,l.title,c.title course_title,b.created_at FROM bookmarks b JOIN lessons l ON l.id=b.lesson_id JOIN courses c ON c.id=l.course_id WHERE b.user_id=? ORDER BY b.created_at DESC", (user_id,))


@app.post("/api/bookmarks")
def toggle_bookmark(data: BookmarkIn, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    if not row("SELECT id FROM lessons WHERE id=?", (data.lesson_id,)): raise HTTPException(404, "Lesson not found")
    exists = row("SELECT 1 FROM bookmarks WHERE user_id=? AND lesson_id=?", (user_id, data.lesson_id)); conn = db()
    if exists: conn.execute("DELETE FROM bookmarks WHERE user_id=? AND lesson_id=?", (user_id, data.lesson_id)); result = False
    else: conn.execute("INSERT INTO bookmarks VALUES (?,?,?)", (user_id, data.lesson_id, now_iso())); result = True
    conn.commit(); conn.close(); mark_study_day(user_id)
    return {"bookmarked": result}


@app.get("/api/notes/{user_id}")
def notes(user_id: str, authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    return rows("SELECT n.id,n.lesson_id,n.content,n.created_at,n.updated_at,l.title lesson_title,c.title course_title FROM notes n JOIN lessons l ON l.id=n.lesson_id JOIN courses c ON c.id=l.course_id WHERE n.user_id=? ORDER BY n.updated_at DESC", (user_id,))


@app.post("/api/notes")
def create_note(data: NoteIn, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    if not row("SELECT id FROM lessons WHERE id=?", (data.lesson_id,)): raise HTTPException(404, "Lesson not found")
    now = now_iso(); nid = uid("note"); conn = db(); conn.execute("INSERT INTO notes VALUES (?,?,?,?,?,?)", (nid, user_id, data.lesson_id, data.content.strip(), now, now)); conn.commit(); conn.close(); mark_study_day(user_id); return {"id": nid, "message": "Note saved"}


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    conn = db(); deleted = conn.execute("DELETE FROM notes WHERE id=? AND user_id=?", (note_id, user_id)).rowcount; conn.commit(); conn.close()
    if not deleted: raise HTTPException(404, "Note not found")
    return {"message": "Note deleted"}


@app.get("/api/quizzes")
def quizzes(user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    result = []
    for q in rows("SELECT q.id,q.course_id,q.title,q.description,q.created_by,q.created_at,COUNT(qq.id) question_count FROM quizzes q LEFT JOIN quiz_questions qq ON qq.quiz_id=q.id GROUP BY q.id ORDER BY q.created_at"):
        last = row("SELECT percent,created_at FROM attempts WHERE user_id=? AND quiz_id=? ORDER BY created_at DESC LIMIT 1", (user_id, q["id"]))
        q["last_percent"] = last["percent"] if last else None; result.append(q)
    return result


@app.post("/api/quizzes")
def create_quiz(data: QuizIn, user_id: str = "demo-teacher", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); require_teacher(user_id)
    if not row("SELECT 1 FROM courses WHERE id=?", (data.course_id,)): raise HTTPException(404, "Course not found")
    if any(q.answer_index < 0 or q.answer_index >= len(q.options) for q in data.questions): raise HTTPException(400, "Correct answer index is invalid")
    qid = uid("quiz"); now = now_iso(); conn = db()
    try:
        conn.execute("BEGIN"); conn.execute("INSERT INTO quizzes VALUES (?,?,?,?,?,?)", (qid, data.course_id, data.title.strip(), data.description[:1000], user_id, now))
        for q in data.questions: conn.execute("INSERT INTO quiz_questions VALUES (?,?,?,?,?,?)", (uid("question"), qid, q.question.strip(), json.dumps(q.options, ensure_ascii=False), q.answer_index, q.explanation.strip()))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    return {"id": qid, "message": "Quiz published"}


@app.get("/api/quizzes/{quiz_id}")
def quiz(quiz_id: str, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    q = row("SELECT * FROM quizzes WHERE id=?", (quiz_id,))
    if not q: raise HTTPException(404, "Quiz not found")
    q["questions"] = []
    for question in rows("SELECT id,question,options_json FROM quiz_questions WHERE quiz_id=? ORDER BY id", (quiz_id,)):
        opts = json.loads(question.pop("options_json") or "[]"); question["options"] = opts; q["questions"].append(question)
    return q


@app.post("/api/quizzes/{quiz_id}/attempt")
def quiz_attempt(quiz_id: str, data: QuizAnswerIn, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id); qs = rows("SELECT * FROM quiz_questions WHERE quiz_id=? ORDER BY id", (quiz_id,))
    if not qs: raise HTTPException(404, "Quiz has no questions")
    if len(data.answers) != len(qs): raise HTTPException(400, f"Answer all {len(qs)} questions before submitting")
    review = []; score = 0
    for idx, answer in enumerate(data.answers):
        opts = json.loads(qs[idx]["options_json"] or "[]")
        if answer < 0 or answer >= len(opts): raise HTTPException(400, "Invalid answer option")
        correct = answer == qs[idx]["answer_index"]; score += int(correct)
        review.append({"question_id": qs[idx]["id"], "question": qs[idx]["question"], "selected_index": answer, "correct_index": qs[idx]["answer_index"], "correct": correct, "explanation": qs[idx]["explanation"], "correct_answer": opts[qs[idx]["answer_index"]]})
    total = len(qs); pct = round(score / total * 100, 1); aid = uid("attempt"); conn = db(); conn.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?)", (aid, user_id, quiz_id, score, total, pct, now_iso())); conn.commit(); conn.close(); mark_study_day(user_id); refresh_achievement_badges(user_id)
    return {"attempt_id": aid, "score": score, "total": total, "percent": pct, "review": review, "xp": learner_xp(user_id)}


@app.get("/api/flashcards")
def flashcards(course_id: Optional[str] = None):
    if course_id: return rows("SELECT * FROM flashcards WHERE course_id=? ORDER BY position", (course_id,))
    return rows("SELECT * FROM flashcards ORDER BY position")


@app.get("/api/flashcards/review")
def flashcard_review_queue(user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    now = now_iso()
    cards = rows(
        """SELECT f.*,r.repetitions,r.interval_days,r.due_at,r.last_reviewed_at,
        CASE WHEN r.card_id IS NULL OR r.due_at<=? THEN 1 ELSE 0 END due
        FROM flashcards f LEFT JOIN flashcard_reviews r ON r.card_id=f.id AND r.user_id=?
        ORDER BY CASE WHEN r.card_id IS NULL OR r.due_at<=? THEN 0 ELSE 1 END,f.position""",
        (now, user_id, now),
    )
    return {
        "cards": cards,
        "due_count": sum(card["due"] for card in cards),
        "total_count": len(cards),
    }


@app.post("/api/flashcards/{card_id}/review")
def review_flashcard(
    card_id: str,
    data: FlashcardReviewIn,
    user_id: str = "demo-student",
    authorization: Optional[str] = Header(default=None),
):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    if not row("SELECT id FROM flashcards WHERE id=?", (card_id,)):
        raise HTTPException(404, "Flashcard not found")

    previous = row(
        "SELECT repetitions,interval_days FROM flashcard_reviews WHERE user_id=? AND card_id=?",
        (user_id, card_id),
    )
    repetitions = int(previous["repetitions"]) if previous else 0
    interval_days = float(previous["interval_days"]) if previous else 0
    if data.rating == "again":
        repetitions = 0
        interval_days = 1 / 144
    elif data.rating == "hard":
        repetitions += 1
        interval_days = max(1, round(interval_days * 1.2)) if interval_days else 1
    elif data.rating == "good":
        interval_days = 1 if repetitions == 0 else 3 if repetitions == 1 else max(1, round(interval_days * 2.5))
        repetitions += 1
    else:
        interval_days = 4 if repetitions == 0 else max(4, round(interval_days * 3.2))
        repetitions += 1
    interval_days = min(interval_days, 365)

    reviewed_at = datetime.now(timezone.utc)
    due_at = reviewed_at + timedelta(days=interval_days)
    conn = db()
    try:
        conn.execute(
            """INSERT INTO flashcard_reviews(user_id,card_id,repetitions,interval_days,due_at,last_reviewed_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id,card_id) DO UPDATE SET repetitions=excluded.repetitions,
            interval_days=excluded.interval_days,due_at=excluded.due_at,last_reviewed_at=excluded.last_reviewed_at""",
            (user_id, card_id, repetitions, interval_days, due_at.isoformat(), reviewed_at.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    mark_study_day(user_id)
    return {
        "card_id": card_id,
        "rating": data.rating,
        "repetitions": repetitions,
        "interval_days": interval_days,
        "due_at": due_at.isoformat(),
    }


@app.get("/api/analytics/{user_id}")
def analytics(user_id: str, authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    cats = rows("SELECT c.category,COUNT(DISTINCT l.id) total,SUM(CASE WHEN p.completed=1 THEN 1 ELSE 0 END) completed,COALESCE(SUM(CASE WHEN p.completed=1 THEN l.minutes ELSE 0 END),0) minutes_completed,COALESCE(SUM(l.minutes),0) minutes_total FROM courses c JOIN lessons l ON l.course_id=c.id LEFT JOIN progress p ON p.lesson_id=l.id AND p.user_id=? GROUP BY c.category ORDER BY c.category", (user_id,))
    q = dict(row("SELECT COUNT(*) attempts,COALESCE(AVG(percent),0) avg_score,COALESCE(MAX(percent),0) best FROM attempts WHERE user_id=?", (user_id,)))
    xp = learner_xp(user_id)
    return {"by_category": cats, "quiz": q, "streak": streak_payload(user_id), "xp": xp, "rank": rank_for_xp(xp)}


@app.get("/api/achievements/{user_id}")
def achievements(user_id: str, authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id); refresh_achievement_badges(user_id)
    badges_data = rows("SELECT b.*,ub.awarded_at FROM badges b LEFT JOIN user_badges ub ON ub.badge_id=b.id AND ub.user_id=? ORDER BY (ub.awarded_at IS NULL),ub.awarded_at DESC,b.name", (user_id,))
    certs = rows("SELECT c.id,c.certificate_no,c.issued_at,co.title,co.icon FROM certificates c JOIN courses co ON co.id=c.course_id WHERE c.user_id=? ORDER BY c.issued_at DESC", (user_id,))
    xp = learner_xp(user_id); return {"badges": badges_data, "certificates": certs, "xp": xp, "rank": rank_for_xp(xp)}


@app.get("/api/certificates/{certificate_id}/pdf")
def certificate_pdf(certificate_id: str, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    c = row("SELECT cert.*,u.name user_name,co.title course_title FROM certificates cert JOIN users u ON u.id=cert.user_id JOIN courses co ON co.id=cert.course_id WHERE cert.id=?", (certificate_id,))
    if not c: raise HTTPException(404, "Certificate not found")
    if c["user_id"] != user_id: raise HTTPException(403, "You do not have access to this certificate")
    buff = BytesIO(); pdf = canvas.Canvas(buff, pagesize=landscape(A4)); w, h = landscape(A4); pdf.setTitle(f"IKSphere Certificate - {c['certificate_no']}"); pdf.setLineWidth(5); pdf.rect(30, 30, w - 60, h - 60)
    pdf.setFont("Helvetica-Bold", 30); pdf.drawCentredString(w/2, h-115, "IKSphere"); pdf.setFont("Helvetica", 14); pdf.drawCentredString(w/2, h-145, "Certificate of Learning")
    pdf.setFont("Helvetica-Bold", 25); pdf.drawCentredString(w/2, h-205, c["user_name"]); pdf.setFont("Helvetica", 15); pdf.drawCentredString(w/2, h-235, "has successfully completed")
    pdf.setFont("Helvetica-Bold", 20); pdf.drawCentredString(w/2, h-275, c["course_title"]); pdf.setFont("Helvetica", 11); pdf.drawCentredString(w/2, h-325, f"Certificate No: {c['certificate_no']} • Issued: {c['issued_at'][:10]}")
    pdf.setFont("Helvetica-Oblique", 10); pdf.drawCentredString(w/2, 74, "IKSphere • Indian Knowledge Systems learning platform"); pdf.save()
    return Response(buff.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={c['certificate_no']}.pdf"})


def offline_answer(message: str) -> str:
    q = message.lower()
    answers = {
        "zero": "Śūnya (zero) is best studied as part of the historical development of positional arithmetic. Brahmagupta's 7th-century work gives explicit arithmetic rules involving zero; the broader history spans multiple traditions and stages.",
        "shunya": "Śūnya (zero) is best studied as part of the historical development of positional arithmetic. Brahmagupta's 7th-century work gives explicit arithmetic rules involving zero; the broader history spans multiple traditions and stages.",
        "panini": "Pāṇini's Aṣṭādhyāyī is a compact rule system for Sanskrit grammar. It is useful to compare its formal structure with computational grammars, but it should not be treated as literally identical to a modern programming language.",
        "aryabhata": "Āryabhaṭa's Aryabhaṭīya uses mathematical astronomy and a moving-boat analogy to explain apparent stellar motion relative to an observer. The useful modern comparison is reference-frame reasoning.",
        "wootz": "Wootz refers to a South Asian crucible-steel tradition. Modern materials science can analyze composition, processing and microstructure, but historical production methods varied and should be described from the evidence available for a particular site or text.",
        "tridosha": "Tridoṣa is a classical Ayurvedic framework using vāta, pitta and kapha. In IKSphere it is presented as a historical medical model and should be kept distinct from modern biomedical physiology.",
        "sanskrit": "Use the Sanskrit Guide for vowels, consonants and IAST. Long vowels are marked with macrons: ā ī ū. Retroflex sounds use dots: ṭ ḍ ṇ ṣ. Anusvāra is ṃ and visarga is ḥ.",
        "certificate": "IKSphere issues a certificate when all lessons in a course are completed. The record includes a unique certificate number and a PDF download.",
    }
    for key, answer in answers.items():
        if key in q: return answer
    return "I can help with IKS topics, scholars, Sanskrit reading, courses, quizzes, learning paths, history, and modern comparisons. For live, model-powered answers, add your GROQ_API_KEY; without it, I will stay within the built-in curated knowledge base."


def retrieve_context(message: str) -> str:
    words = [w for w in "".join(ch.lower() if ch.isalnum() or ch.isspace() else " " for ch in message).split() if len(w) >= 4][:12]
    if not words: return ""
    clauses = " OR ".join(["lower(title||' '||overview||' '||content||' '||concepts_json) LIKE ?"] * len(words))
    params = tuple(f"%{w}%" for w in words)
    matches = rows(f"SELECT id,title,source,historical_context,modern_parallel FROM lessons WHERE {clauses} ORDER BY position LIMIT 6", params)
    lines = []
    for m in matches:
        lines.append(f"Lesson: {m['title']}\nSource: {m.get('source','')}\nHistorical context: {m.get('historical_context','')}\nModern parallel: {m.get('modern_parallel','')}")
    return "\n\n".join(lines)


@app.get("/api/ai/history/{user_id}")
def ai_history(user_id: str, authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id); return rows("SELECT id,role,content,created_at FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT 40", (user_id,))[::-1]


@app.delete("/api/ai/history/{user_id}")
def ai_clear(user_id: str, authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id); conn = db(); conn.execute("DELETE FROM chat_messages WHERE user_id=?", (user_id,)); conn.commit(); conn.close(); return {"message": "Conversation cleared"}


@app.get("/api/sanskrit-guide")
def sanskrit_guide():
    return SANSKRIT_GUIDE


@app.post("/api/ai/ask")
async def ai_ask(data: AskIn, user_id: str = "demo-student", authorization: Optional[str] = Header(default=None)):
    auth_row_for_user(user_id, authorization); current_user(user_id)
    message = data.message.strip(); retrieved = retrieve_context(message)
    history = [m for m in data.history if m.get("role") in {"user", "assistant"} and isinstance(m.get("content"), str)][-12:]
    stored = rows("SELECT role,content FROM chat_messages WHERE user_id=? ORDER BY created_at DESC LIMIT 12", (user_id,))[::-1]
    if stored: history = stored
    system = (
        "You are Samvāda, the study mentor inside IKSphere. Answer the user's question helpfully, including general academic questions, while prioritizing Indian Knowledge Systems when relevant. "
        "Be historically careful. Separate primary-text evidence, scholarly reconstruction, traditional interpretation and modern analogy. Never invent a quote, date, attribution, experiment, or citation. "
        "When evidence is uncertain, say so. Do not present traditional medicine as a substitute for medical care. Use clear language and explain Sanskrit terms briefly. "
        "The user may be learning on a phone, so prefer structured answers with short headings and bullets.\n\n"
        f"Curated IKS context:\n{retrieved or 'No directly matching lesson context.'}\n\nUser-provided context:\n{data.context.strip() or 'None'}"
    )
    messages = [{"role": "system", "content": system}, *history, {"role": "user", "content": message}]
    if GROQ_API_KEY:
        payload = {"model": GROQ_MODEL, "messages": messages, "temperature": 0.25, "max_completion_tokens": 1400}
        try:
            async with httpx.AsyncClient(timeout=35) as client:
                r = await client.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}, json=payload)
                r.raise_for_status(); body = r.json(); answer = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()
                if answer:
                    conn = db(); conn.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?)", (uid("chat"), user_id, "user", message, now_iso())); conn.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?)", (uid("chat"), user_id, "assistant", answer, now_iso())); conn.commit(); conn.close(); mark_study_day(user_id)
                    return {"answer": answer, "offline": False, "model": GROQ_MODEL, "sources": ["IKSphere curated lessons", "Groq model response"], "retrieved": bool(retrieved)}
        except Exception as exc:
            offline = offline_answer(message)
            return {"answer": offline + f"\n\n(Groq could not answer this request right now: {type(exc).__name__}.)", "offline": True, "model": GROQ_MODEL, "sources": ["IKSphere curated knowledge"]}
    offline = offline_answer(message)
    conn = db(); conn.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?)", (uid("chat"), user_id, "user", message, now_iso())); conn.execute("INSERT INTO chat_messages VALUES (?,?,?,?,?)", (uid("chat"), user_id, "assistant", offline, now_iso())); conn.commit(); conn.close(); mark_study_day(user_id)
    return {"answer": offline, "offline": True, "model": None, "sources": ["IKSphere curated knowledge"]}


ensure_db()
