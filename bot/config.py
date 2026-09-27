"""Настройки из переменных окружения (.env)."""
import os
from dataclasses import dataclass, field


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


@dataclass(frozen=True)
class Config:
    telegram_token: str
    worker_url: str
    proxy_token: str
    allowed_ids: set[int] = field(default_factory=set)
    models: tuple[str, ...] = ()
    show_model: bool = True
    thinking_level: str = "low"
    level: str = "A1–A2"
    tts_voice: str = "pl-PL-ZofiaNeural"
    tts_rate: str = "-10%"
    history_limit: int = 20
    db_path: str = "/app/data/bot.db"


def load() -> Config:
    return Config(
        telegram_token=os.environ["TELEGRAM_TOKEN"],
        worker_url=os.environ["WORKER_URL"].rstrip("/"),
        proxy_token=os.environ["PROXY_TOKEN"],
        allowed_ids=_ids(os.environ.get("ALLOWED_USER_IDS", "")),
        models=tuple(m for m in os.environ.get("GEMINI_MODELS", "").replace(" ", "").split(",") if m),
        show_model=os.environ.get("SHOW_MODEL", "1").strip().lower() not in ("0", "false", "no", ""),
        thinking_level=os.environ.get("GEMINI_THINKING_LEVEL", "low"),
        level=os.environ.get("LEVEL", "A1–A2"),
        tts_voice=os.environ.get("TTS_VOICE", "pl-PL-ZofiaNeural"),
        tts_rate=os.environ.get("TTS_RATE", "-10%"),
        history_limit=int(os.environ.get("HISTORY_LIMIT", "20")),
        db_path=os.environ.get("DB_PATH", "/app/data/bot.db"),
    )
