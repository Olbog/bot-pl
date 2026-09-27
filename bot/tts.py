"""Озвучка: edge-tts (mp3) → ffmpeg → ogg/opus для голосового в Telegram."""
import asyncio
import tempfile
from pathlib import Path


async def synthesize(text: str, voice: str, rate: str) -> bytes:
    import edge_tts  # импорт здесь, чтобы тесты не зависели от пакета

    with tempfile.TemporaryDirectory() as tmp:
        mp3 = Path(tmp) / "a.mp3"
        ogg = Path(tmp) / "a.ogg"
        await edge_tts.Communicate(text, voice, rate=rate).save(str(mp3))
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
            "-c:a", "libopus", "-b:a", "32k", "-ac", "1", str(ogg),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg: {err.decode(errors='replace')[:300]}")
        return ogg.read_bytes()
