"""Solar elevation (vision/sun.py) against known values."""

from datetime import datetime, timezone

import pytest

from cyberpunk_computer.vision.sun import solar_elevation


def test_june_solstice_noon_52n():
    # 90 - 52 + 23.44 = 61.44 deg at local solar noon (lon 0 -> ~12:02 UTC)
    e = solar_elevation(datetime(2026, 6, 21, 12, 2, tzinfo=timezone.utc), 52.0, 0.0)
    assert e == pytest.approx(61.4, abs=0.3)


def test_june_solstice_midnight_52n_negative():
    # lower culmination: -(90 - 52 - 23.44) = -14.56 deg
    e = solar_elevation(datetime(2026, 6, 21, 0, 2, tzinfo=timezone.utc), 52.0, 0.0)
    assert e == pytest.approx(-14.6, abs=0.3)


def test_december_solstice_noon_52n():
    # 90 - 52 - 23.44 = 14.56 deg
    e = solar_elevation(datetime(2026, 12, 21, 11, 58, tzinfo=timezone.utc), 52.0, 0.0)
    assert e == pytest.approx(14.6, abs=0.3)


def test_epoch_and_naive_datetime_agree():
    dt = datetime(2026, 10, 19, 13, 22, 14, tzinfo=timezone.utc)
    a = solar_elevation(dt, 54.35, 18.65)
    b = solar_elevation(dt.timestamp(), 54.35, 18.65)
    c = solar_elevation(dt.replace(tzinfo=None), 54.35, 18.65)
    assert a == pytest.approx(b) == pytest.approx(c)
    # the drive home (15:22 CEST, Gdansk): afternoon sun ~16 deg
    assert 10 < a < 22


def test_civil_dusk_gdansk_october():
    # 19 Oct, Gdansk: sunset ~15:50 UTC, civil dusk (-6 deg) ~16:30 UTC
    before = solar_elevation(datetime(2026, 10, 19, 16, 0, tzinfo=timezone.utc), 54.35, 18.65)
    after = solar_elevation(datetime(2026, 10, 19, 17, 0, tzinfo=timezone.utc), 54.35, 18.65)
    assert -6 < before < 0
    assert after < -6
