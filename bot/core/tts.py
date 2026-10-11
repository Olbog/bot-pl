"""Озвучка: edge-tts (mp3) → ffmpeg → ogg/opus для голосового в Telegram.

Сервис Microsoft иногда случайно отказывает при подключении (403 / 503 / нет аудио) — тогда сразу
повторяем: ещё RETRY_DELAYS попыток с паузами. Ошибка ffmpeg не повторяется — она не от сервиса."""
import asyncio
import logging
import tempfile
from pathlib import Path

log = logging.getLogger("bot-pl")
RETRY_DELAYS = (2, 5, 10)   # секунд перед 2-й, 3-й и 4-й попыткой


async def synthesize(text: str, voice: str, rate: str, sleep=None) -> bytes:
    import edge_tts  # импорт здесь, чтобы тесты не зависели от пакета

    sleep = sleep or asyncio.sleep
    with tempfile.TemporaryDirectory() as tmp:
        mp3 = Path(tmp) / "a.mp3"
        ogg = Path(tmp) / "a.ogg"
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                await edge_tts.Communicate(text, voice, rate=rate).save(str(mp3))
                break
            except Exception as e:
                if attempt == len(RETRY_DELAYS):
                    raise
                delay = RETRY_DELAYS[attempt]
                log.warning("Озвучка: сбой сервиса (%s: %s), попытка %s, повтор через %s с",
                            type(e).__name__, str(e)[:120], attempt + 1, delay)
                mp3.unlink(missing_ok=True)
                await sleep(delay)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
            "-c:a", "libopus", "-b:a", "32k", "-ac", "1", str(ogg),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg: {err.decode(errors='replace')[:300]}")
        return ogg.read_bytes()
