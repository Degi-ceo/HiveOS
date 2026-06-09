"""
voice.py — Hive voice surface (Phase 10, "Jarvis feel").

Pipeline:  wake word ("hej hive") -> record -> STT -> gateway /chat -> TTS.
Backends (all local/cheap, per research): openWakeWord, faster-whisper, Piper.
Heavy audio deps are lazy-imported so the rest of HiveOS runs without them.
"""
from __future__ import annotations
import os
import logging
import httpx

log = logging.getLogger("hiveos.voice")
GATEWAY = f"http://localhost:{os.getenv('HIVE_PORT','8088')}"
SECRET = os.getenv("HIVE_SECRET", "change_me")
WAKE_WORD = os.getenv("HIVE_WAKE_WORD", "hive")


class STT:
    def __init__(self, model: str = "base") -> None:
        from faster_whisper import WhisperModel
        self.m = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, wav_path: str) -> str:
        segs, _ = self.m.transcribe(wav_path)
        return " ".join(s.text for s in segs).strip()


class TTS:
    def __init__(self, voice: str = "en_US-amy-medium") -> None:
        self.voice = voice

    def speak(self, text: str) -> None:
        import subprocess
        subprocess.run(
            f'echo "{text}" | piper --model {self.voice} --output-raw '
            f'| aplay -r 22050 -f S16_LE', shell=True, check=False)


async def ask_hive(text: str) -> str:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{GATEWAY}/chat", headers={"x-hive-token": SECRET},
                         json={"session_id": "voice", "message": text})
        return r.json().get("reply", "")


def record_until_silence(path: str = "/tmp/hive_in.wav") -> str:
    import subprocess
    subprocess.run(f"arecord -d 5 -r 16000 -f S16_LE {path}", shell=True, check=False)
    return path


async def loop() -> None:
    stt, tts = STT(), TTS()
    log.info("Voice ready. Say '%s ...'", WAKE_WORD)
    while True:
        heard = stt.transcribe(record_until_silence()).lower()
        if WAKE_WORD not in heard:
            continue
        prompt = heard.split(WAKE_WORD, 1)[-1].strip()
        if prompt:
            tts.speak(await ask_hive(prompt))


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(loop())
