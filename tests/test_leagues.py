"""Season-key arithmetic for the three leagues."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from vgi_nba.leagues import GLEAGUE, NBA, WNBA, eastern_today


def test_season_labels() -> None:
    assert NBA.season_label(2025) == "2025-26"
    assert NBA.season_label(1999) == "1999-00"
    assert GLEAGUE.season_label(2025) == "2025-26"
    assert WNBA.season_label(2025) == "2025"


@pytest.mark.parametrize(
    ("league", "day", "expected"),
    [
        (NBA, date(2026, 11, 10), 2026),
        (NBA, date(2026, 6, 15), 2025),
        (NBA, date(2026, 10, 1), 2026),
        (WNBA, date(2026, 11, 10), 2026),
        (WNBA, date(2026, 3, 1), 2025),
        (GLEAGUE, date(2027, 1, 5), 2026),
    ],
)
def test_current_season(league, day: date, expected: int) -> None:
    assert league.current_season(day) == expected


def test_resolve_season_bounds() -> None:
    assert NBA.resolve_season(2010) == 2010
    assert isinstance(NBA.resolve_season(None), int)
    with pytest.raises(ValueError, match="start with the 1997 season"):
        WNBA.resolve_season(1990)
    with pytest.raises(ValueError, match="in the future"):
        NBA.resolve_season(2090)


def test_eastern_today_crosses_midnight_utc() -> None:
    assert eastern_today(datetime(2026, 11, 10, 3, 0, tzinfo=UTC)) == date(2026, 11, 9)
    assert eastern_today(datetime(2026, 11, 10, 6, 0, tzinfo=UTC)) == date(2026, 11, 10)
