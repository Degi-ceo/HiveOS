from datetime import datetime, timezone
from hive.autonomy.time_window import check_window

def test_empty_window_denies():
    assert not check_window('', 'Europe/Warsaw')[0]

def test_window_allows_and_denies_by_local_time():
    assert check_window('09:00-17:00', 'Europe/Warsaw', now=datetime(2026, 1, 1, 10, tzinfo=timezone.utc))[0]
    assert not check_window('09:00-17:00', 'Europe/Warsaw', now=datetime(2026, 1, 1, 20, tzinfo=timezone.utc))[0]

def test_overnight_window_is_supported():
    assert check_window('22:00-02:00', 'Europe/Warsaw', now=datetime(2026, 1, 1, 23, tzinfo=timezone.utc))[0]

def test_invalid_zone_denies():
    assert not check_window('09:00-17:00', 'Invalid/Zone')[0]