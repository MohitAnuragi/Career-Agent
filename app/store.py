"""User state: profile, long-term preferences, feedback, chat memory and LLM usage caps."""
from __future__ import annotations

from datetime import date
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.ingest.cities import canonical_list
from app.models import CandidateProfile, Job, Preferences
from app.search import JOB_COLS


class RateLimited(Exception):
    pass


def create_user(conn) -> str:
    row = conn.execute("INSERT INTO users DEFAULT VALUES RETURNING id").fetchone()
    conn.commit()
    return str(row["id"])


def user_exists(conn, user_id: str) -> bool:
    try:
        UUID(user_id)
    except ValueError:
        return False
    return conn.execute("SELECT 1 FROM users WHERE id = %s", [user_id]).fetchone() is not None


def save_profile(conn, user_id: str, profile: CandidateProfile, vector) -> None:
    conn.execute(
        "UPDATE users SET profile = %s, profile_embedding = %s WHERE id = %s",
        [Jsonb(profile.model_dump()), vector, user_id],
    )
    conn.commit()


def get_profile(conn, user_id: str) -> tuple[CandidateProfile | None, object]:
    row = conn.execute("SELECT profile, profile_embedding FROM users WHERE id = %s", [user_id]).fetchone()
    if not row or not row["profile"]:
        return None, None
    return CandidateProfile(**row["profile"]), row["profile_embedding"]


def get_preferences(conn, user_id: str) -> Preferences:
    row = conn.execute("SELECT preferences FROM users WHERE id = %s", [user_id]).fetchone()
    return Preferences(**(row["preferences"] if row else {}))


def update_preferences(conn, user_id: str, changes: dict) -> Preferences:
    """Merge semantics: list fields are unioned (case-insensitive), scalars overwritten, None ignored."""
    current = get_preferences(conn, user_id).model_dump()
    for key, value in changes.items():
        if key not in current or value is None:
            continue
        if isinstance(current[key], list):
            merged = list(current[key])
            for v in value if isinstance(value, list) else [value]:
                if str(v).lower() not in {str(x).lower() for x in merged}:
                    merged.append(v)
            current[key] = merged
        else:
            current[key] = value
    current["locations"] = canonical_list(current["locations"])  # "gurgaon" and "Gurugram" are one city
    prefs = Preferences(**current)
    conn.execute("UPDATE users SET preferences = %s WHERE id = %s", [Jsonb(prefs.model_dump()), user_id])
    conn.commit()
    return prefs


def set_preferences(conn, user_id: str, prefs: Preferences) -> Preferences:
    prefs = prefs.model_copy(update={"locations": canonical_list(prefs.locations)})
    conn.execute("UPDATE users SET preferences = %s WHERE id = %s", [Jsonb(prefs.model_dump()), user_id])
    conn.commit()
    return prefs


def record_feedback(conn, user_id: str, job_id: str, action: str) -> bool:
    if conn.execute("SELECT 1 FROM jobs WHERE id = %s", [job_id]).fetchone() is None:
        return False
    conn.execute(
        """INSERT INTO feedback (user_id, job_id, action) VALUES (%s, %s, %s)
           ON CONFLICT (user_id, job_id) DO UPDATE SET action = EXCLUDED.action, created_at = now()""",
        [user_id, job_id, action],
    )
    conn.commit()
    return True


def clear_feedback(conn, user_id: str, job_id: str) -> None:
    conn.execute("DELETE FROM feedback WHERE user_id = %s AND job_id = %s", [user_id, job_id])
    conn.commit()


def feedback_ids(conn, user_id: str, action: str) -> list[str]:
    rows = conn.execute(
        "SELECT job_id FROM feedback WHERE user_id = %s AND action = %s ORDER BY created_at DESC",
        [user_id, action],
    ).fetchall()
    return [r["job_id"] for r in rows]


def saved_jobs(conn, user_id: str) -> list[Job]:
    rows = conn.execute(
        f"""SELECT {', '.join('j.' + c.strip() for c in JOB_COLS.split(','))}
            FROM feedback f JOIN jobs j ON j.id = f.job_id
            WHERE f.user_id = %s AND f.action = 'save' ORDER BY f.created_at DESC""",
        [user_id],
    ).fetchall()
    return [Job(**r) for r in rows]


def get_job(conn, job_id: str) -> Job | None:
    row = conn.execute(f"SELECT {JOB_COLS} FROM jobs WHERE id = %s", [job_id]).fetchone()
    return Job(**row) if row else None


# ---------------------------------------------------------------- chat memory
def recent_messages(conn, user_id: str, limit: int = 12) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE user_id = %s ORDER BY id DESC LIMIT %s", [user_id, limit]
    ).fetchall()
    return list(reversed(rows))


def append_messages(conn, user_id: str, pairs: list[tuple[str, str]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO messages (user_id, role, content) VALUES (%s, %s, %s)",
            [(user_id, role, content) for role, content in pairs],
        )
    conn.commit()


def clear_messages(conn, user_id: str) -> None:
    conn.execute("DELETE FROM messages WHERE user_id = %s", [user_id])
    conn.commit()


# ---------------------------------------------------------------- cost control
def charge_llm_call(conn, user_id: str, limit: int, calls: int = 1, global_limit: int | None = None) -> int:
    """Atomically count LLM calls per user and across all users per day; raises RateLimited past either cap."""
    if global_limit is None:
        global_limit = get_settings().global_daily_llm_limit
    today = date.today()
    row = conn.execute(
        """INSERT INTO llm_usage (user_id, day, calls) VALUES (%s, %s, %s)
           ON CONFLICT (user_id, day) DO UPDATE SET calls = llm_usage.calls + EXCLUDED.calls
           RETURNING calls""",
        [user_id, today, calls],
    ).fetchone()
    total = conn.execute(
        """INSERT INTO llm_usage_global (day, calls) VALUES (%s, %s)
           ON CONFLICT (day) DO UPDATE SET calls = llm_usage_global.calls + EXCLUDED.calls
           RETURNING calls""",
        [today, calls],
    ).fetchone()
    conn.commit()
    if row["calls"] > limit:
        raise RateLimited(f"Daily AI limit of {limit} requests reached. It resets tomorrow.")
    if global_limit and total["calls"] > global_limit:
        raise RateLimited("This demo has used today's shared AI budget. Search still works; AI features return tomorrow.")
    return row["calls"]
