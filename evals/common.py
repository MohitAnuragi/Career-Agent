from __future__ import annotations

import json
import re
from pathlib import Path

from app.config import get_settings

ROOT = Path(__file__).parent
DATA = ROOT / "data"
RESULTS = ROOT / "results"
SNAPSHOT = DATA / "snapshot.jsonl.gz"
QRELS = DATA / "qrels.json"


def eval_db_url() -> str:
    s = get_settings()
    return s.eval_database_url or re.sub(r"/[^/?]+(\?|$)", r"/career_agent_eval\1", s.database_url)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def judge_llm():
    from app.llm.gemini import GeminiLLM

    s = get_settings()
    return GeminiLLM(api_key=s.gemini_api_key, model=s.judge_model or s.gemini_model)
