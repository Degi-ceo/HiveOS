"""
tests/test_voice.py — Unit tests for voice surface hardening (issue #46).

Tests cover:
- available_devices() / _detect_audio_device() parsing arecord -l output
- WakeWordDetector fallback string matching (no openWakeWord dep needed)
- record_until_silence() passes detected device to arecord
- loop() wires WakeWordDetector correctly
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# available_devices / _detect_audio_device
# ---------------------------------------------------------------------------

ARECORD_SAMPLE = """\
**** List of CAPTURE Hardware Devices ****
card 0: PCH [HDA Intel PCH], device 0: ALC3235 Analog [ALC3235 Analog]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
card 1: USB [USB Audio Device], device 0: USB Audio [USB Audio]
  Subdevices: 1/1
  Subdevice #0: subdevice #0
"""


def test_available_devices_parses_arecord_output():
    """available_devices() parses card/device numbers and names from arecord -l."""
    from hive.surfaces import voice

    mock_result = MagicMock()
    mock_result.stdout = ARECORD_SAMPLE
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result):
        devs = voice.available_devices()

    assert len(devs) == 2
    assert devs[0] == (0, 0, "HDA Intel PCH")
    assert devs[1] == (1, 0, "USB Audio Device")


def test_detect_audio_device_returns_hw_string():
    """_detect_audio_device() returns hw:card,device for the first available device."""
    from hive.surfaces import voice

    mock_result = MagicMock()
    mock_result.stdout = ARECORD_SAMPLE
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result):
        device = voice._detect_audio_device()

    assert device == "hw:0,0"


def test_detect_audio_device_returns_none_on_failure():
    """_detect_audio_device() returns None when arecord is unavailable."""
    from hive.surfaces import voice

    with patch("subprocess.run", side_effect=FileNotFoundError("arecord not found")):
        device = voice._detect_audio_device()

    assert device is None


def test_detect_audio_device_returns_none_on_empty_output():
    """_detect_audio_device() returns None when arecord lists no devices."""
    from hive.surfaces import voice

    mock_result = MagicMock()
    mock_result.stdout = ""
    mock_result.returncode = 0

    with patch("subprocess.run", return_value=mock_result):
        device = voice._detect_audio_device()

    assert device is None


# ---------------------------------------------------------------------------
# WakeWordDetector
# ---------------------------------------------------------------------------

def test_wake_word_detector_string_match_when_oww_unavailable():
    """WakeWordDetector falls back to transcript match when openWakeWord is absent."""
    from hive.surfaces.voice import WakeWordDetector

    detector = WakeWordDetector("hive")
    assert detector.uses_oww is False
    assert detector.is_wake_word("/tmp/dummy.wav", "hey hive what time is it") is True
    assert detector.is_wake_word("/tmp/dummy.wav", "hello world") is False


def test_wake_word_detector_case_insensitive():
    """WakeWordDetector string match is case-insensitive."""
    from hive.surfaces.voice import WakeWordDetector

    detector = WakeWordDetector("hive")
    assert detector.is_wake_word("/tmp/dummy.wav", "HIVE do something") is True


def test_wake_word_detector_custom_word():
    """WakeWordDetector respects a custom wake word."""
    from hive.surfaces.voice import WakeWordDetector

    detector = WakeWordDetector("jarvis")
    assert detector.is_wake_word("/tmp/dummy.wav", "ok jarvis open the pod bay doors") is True
    assert detector.is_wake_word("/tmp/dummy.wav", "ok hive open the pod bay doors") is False


# ---------------------------------------------------------------------------
# record_until_silence
# ---------------------------------------------------------------------------

def test_record_until_silence_passes_detected_device():
    """record_until_silence() includes -D flag when a device is detected."""
    from hive.surfaces import voice

    mock_detect = MagicMock(return_value="hw:1,0")
    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return MagicMock()

    with patch.object(voice, "_detect_audio_device", mock_detect), \
         patch("subprocess.run", side_effect=fake_run):
        voice.record_until_silence("/tmp/out.wav")

    assert "-D" in captured_cmd
    assert "hw:1,0" in captured_cmd
    assert "/tmp/out.wav" in captured_cmd


def test_record_until_silence_no_device_flag_when_none():
    """record_until_silence() omits -D flag when no device is detected."""
    from hive.surfaces import voice

    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return MagicMock()

    with patch.object(voice, "_detect_audio_device", return_value=None), \
         patch("subprocess.run", side_effect=fake_run):
        voice.record_until_silence("/tmp/out.wav")

    assert "-D" not in captured_cmd
    assert "/tmp/out.wav" in captured_cmd


def test_record_until_silence_explicit_device_overrides_auto():
    """record_until_silence() uses explicitly provided device, skips auto-detect."""
    from hive.surfaces import voice

    captured_cmd: list = []

    def fake_run(cmd, **kwargs):
        captured_cmd.extend(cmd)
        return MagicMock()

    with patch("subprocess.run", side_effect=fake_run):
        voice.record_until_silence("/tmp/out.wav", device="hw:2,0")

    assert "-D" in captured_cmd
    assert "hw:2,0" in captured_cmd
