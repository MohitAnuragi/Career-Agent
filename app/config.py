from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://career:career@localhost:5432/career_agent"
    db_pool_max: int = 10  # keep small (e.g. 3) on serverless hosts, where every instance opens its own pool

    llm_provider: str = "gemini"  # "gemini" | "fake"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    judge_model: str = ""  # evals only; use a different/stronger model than gemini_model to avoid self-grading bias
    eval_database_url: str = ""  # defaults to <database_url with db name career_agent_eval>

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    fastembed_cache: str = ".cache/fastembed"

    daily_llm_limit: int = 40  # per user
    global_daily_llm_limit: int = 800  # all users combined; protects the free-tier quota on a public URL
    admin_token: str = ""

    # Retrieval knobs
    candidate_pool: int = 50  # per retriever, before fusion
    rrf_k: int = 60
    rerank_top_n: int = 15

    # Freshness and ingest housekeeping
    fresh_days_default: int = 14  # search/feed show jobs posted in the last N days unless asked otherwise
    ingest_max_age_days: int = 30  # older postings are not stored, and stored ones expire past this age
    prune_after_days: int = 60  # inactive jobs unseen this long are deleted (unless a user saved/dismissed them)
    board_max_failures: int = 7  # consecutive failed runs before a board is disabled


@lru_cache
def get_settings() -> Settings:
    return Settings()
