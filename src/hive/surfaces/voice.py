"""
voice.py — Hive voice surface (Phase 10, "Jarvis feel").

Pipeline: wake word → record → STT → gateway /chat → TTS.
Backends (all local/cheap): openWakeWord, faster-whisper, Piper.
Heavy audio deps are lazy-imported so the rest of HiveOS runs without them.

Hardening (issue #46):
- _detect_audio_device(): probes `arecord -l` for best input device
- WakeWordDetector: uses openWakeWord if installed, falls back to transcript string match
- available_devices(): returns list of (card, device, name) tuples for introspection
- record_until_silence() accepts explicit device; auto-detects when omitted

SYNTHESIS A.2: promoted from scripts/voice.py into the package.
Usage: python -m hive.surfaces.voice  (or hive.surfaces.voice.loop() from code)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from typing import Any

import httpx

log = logging.getLogger("hive.surfaces.voice")

GATEWAY = f"http://localhost:{os.getenv('HIVE_PORT', '8088')}"
SECRET = os.getenv("HIVE_SECRET", "change_me")
WAKE_WORD = os.getenv("HIVE_WAKE_WORD", "hive")


# ---------------------------------------------------------------------------
# Audio device detection
# ---------------------------------------------------------------------------

def available_devices() -> list[tuple[int, int, str]]:
    """Return list of (card_num, device_num, name) for available ALSA capture devices."""
    try:
        result = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=5
        )
        devices: list[tuple[int, int, str]] = []
        for line in result.stdout.splitlines():
            m = re.match(r"card\s+(\d+):\s+\S+\s+\[([^\]]+)\].*device\s+(\d+)", line)
            if m:
                devices.append((int(m.group(1)), int(m.group(3)), m.group(2).strip()))
        return devices
    except Exception:
        return []


def _detect_audio_device() -> str | None:
    """Return best available ALSA capture device string (hw:card,dev) or None."""
    devs = available_devices()
    if not devs:
        return None
    card, device, _ = devs[0]
    return f"hw:{card},{device}"


# ---------------------------------------------------------------------------
# Wake-word detection
# ---------------------------------------------------------------------------

class WakeWordDetector:
    """Detect wake word; uses openWakeWord if available, else transcript string match."""

    def __init__(self, wake_word: str = WAKE_WORD) -> None:
        self._word = wake_word.lower()
        self._oww: Any | None = None
        try:
            from openwakeword.model import Model  # type: ignore[import]
            self._oww = Model(wakeword_models=[wake_word])
            log.info("voice: openWakeWord loaded for '%s'", wake_word)
        except Exception:
            log.debug("voice: openWakeWord unavailable, using transcript fallback")

    @property
    def uses_oww(self) -> bool:
        return self._oww is not None

    def is_wake_word(self, audio_path: str, transcript: str = "") -> bool:
        """Return True if wake word detected in audio or (fallback) in transcript."""
        if self._oww is not None:
            try:
                import numpy as np  # type: ignore[import]
                import soundfile as sf  # type: ignore[import]
                audio, _ = sf.read(audio_path, dtype="int16")
                pred = self._oww.predict(audio)
                if any(v > 0.5 for v in pred.values()):
                    return True
            except Exception as exc:
                log.debug("voice: openWakeWord inference failed: %s", exc)
        return self._word in transcript.lower()


# ---------------------------------------------------------------------------
# STT / TTS
# ---------------------------------------------------------------------------

class STT:
    def __init__(self, model: str = "base") -> None:
        from faster_whisper import WhisperModel  # type: ignore[import]
        self.m = WhisperModel(model, device="cpu", compute_type="int8")

    def transcribe(self, wav_path: str) -> str:
        segs, _ = self.m.transcribe(wav_path)
        return " ".join(s.text for s in segs).strip()


class TTS:
    def __init__(self, voice: str = "en_US-amy-medium") -> None:
        self.voice = voice

    def speak(self, text: str) -> None:
        echo = subprocess.Popen(["echo", text], stdout=subprocess.PIPE)
        piper = subprocess.Popen(
            ["piper", "--model", self.voice, "--output-raw"],
            stdin=echo.stdout,
            stdout=subprocess.PIPE,
        )
        if echo.stdout:
            echo.stdout.close()
        subprocess.run(
            ["aplay", "-r", "22050", "-f", "S16_LE"],
            stdin=piper.stdout,
            check=False,
        )
        if piper.stdout:
            piper.stdout.close()
        piper.wait()


# ---------------------------------------------------------------------------
# Recording / gateway
# ---------------------------------------------------------------------------

async def ask_hive(text: str) -> str:
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"{GATEWAY}/chat",
            headers={"x-hive-token": SECRET},
            json={"session_id": "voice", "message": text},
        )
        return r.json().get("reply", "")


def record_until_silence(
    path: str = "/tmp/hive_in.wav", device: str | None = None
) -> str:
    """Record ~5 s of audio into `path`. Auto-detects ALSA device when not given."""
    if device is None:
        device = _detect_audio_device()
    cmd = ["arecord", "-d", "5", "-r", "16000", "-f", "S16_LE"]
    if device:
        cmd += ["-D", device]
    cmd.append(path)
    subprocess.run(cmd, check=False)
    return path


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def loop(
    *,
    stt_model: str = "base",
    tts_voice: str = "en_US-amy-medium",
    wake_word: str = WAKE_WORD,
    audio_device: str | None = None,
) -> None:
    """Voice loop: record → detect wake word → STT → chat → TTS."""
    stt = STT(stt_model)
    tts = TTS(tts_voice)
    detector = WakeWordDetector(wake_word)
    device = audio_device or _detect_audio_device()
    log.info("Voice ready. Say '%s ...' (device=%s, oww=%s)",
             wake_word, device or "default", detector.uses_oww)
    while True:
        wav = record_until_silence(device=device)
        transcript = stt.transcribe(wav).lower()
        if not detector.is_wake_word(wav, transcript):
            continue
        prompt = transcript.split(wake_word.lower(), 1)[-1].strip()
        if prompt:
            tts.speak(await ask_hive(prompt))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(loop())
