"""Imported events with a non-positive duration must not vanish from the list.

list_events selects events that overlap the query window with
``dtstart < end AND dtend > start``. An import that stores ``dtend == dtstart``
(a single-day all-day event whose source wrote DTEND equal to DTSTART, treating
it as an inclusive bound) is therefore silently dropped — the event never shows
on the calendar even though it was imported. import_ics now clamps such an end
to a positive span, matching the default used when DTEND is absent.
"""
import asyncio
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("icalendar")

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
import routes.calendar_routes as cr  # noqa: E402
from routes.calendar_routes import _ensure_positive_duration  # noqa: E402

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


@pytest.fixture(autouse=True)
def _bind_temp_db(monkeypatch):
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    monkeypatch.setattr(cr, "SessionLocal", _TS)
    monkeypatch.setattr(cr, "require_user", lambda request: "tester")
    yield


# ---- pure helper -----------------------------------------------------------

def test_all_day_same_date_end_clamped_to_one_day():
    start = datetime(2026, 6, 20)
    assert _ensure_positive_duration(start, start, True) == datetime(2026, 6, 21)


def test_timed_non_positive_end_clamped_to_one_hour():
    start = datetime(2026, 6, 20, 9, 0)
    assert _ensure_positive_duration(start, start, False) == datetime(2026, 6, 20, 10, 0)
    # reversed end (dtend < dtstart) is also normalized
    earlier = datetime(2026, 6, 20, 8, 0)
    assert _ensure_positive_duration(start, earlier, False) == datetime(2026, 6, 20, 10, 0)


def test_positive_duration_end_is_unchanged():
    start = datetime(2026, 6, 20, 9, 0)
    end = datetime(2026, 6, 20, 17, 0)
    assert _ensure_positive_duration(start, end, False) is end


# ---- behavioral: import -> list -------------------------------------------

def _ics(dtstart_date, dtend_date):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:holiday-1\r\nSUMMARY:Public Holiday\r\n"
        f"DTSTART;VALUE=DATE:{dtstart_date}\r\nDTEND;VALUE=DATE:{dtend_date}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()


class _FakeUpload:
    def __init__(self, content, filename="cal.ics"):
        self._content = content
        self.filename = filename

    async def read(self, n=-1):
        return self._content


def _endpoints():
    router = cr.setup_calendar_routes()
    eps = {}
    for route in router.routes:
        if route.path == "/api/calendar/import" and "POST" in route.methods:
            eps["import"] = route.endpoint
        if route.path == "/api/calendar/events" and "GET" in route.methods:
            eps["list"] = route.endpoint
    return eps


def _request():
    return SimpleNamespace(state=SimpleNamespace(current_user="tester"))


def test_single_day_all_day_event_with_same_date_end_appears_in_list():
    eps = _endpoints()
    res = asyncio.run(eps["import"](
        _request(), file=_FakeUpload(_ics("20260620", "20260620")), calendar_name="A",
    ))
    assert res["imported"] == 1

    out = asyncio.run(eps["list"](
        _request(), start="2026-06-20T00:00:00", end="2026-06-23T00:00:00",
    ))
    assert [e["summary"] for e in out["events"]] == ["Public Holiday"]


def test_normal_multi_day_all_day_event_still_appears():
    # Regression: a well-formed exclusive DTEND must keep working.
    eps = _endpoints()
    res = asyncio.run(eps["import"](
        _request(), file=_FakeUpload(_ics("20260710", "20260711")), calendar_name="B",
    ))
    assert res["imported"] == 1

    out = asyncio.run(eps["list"](
        _request(), start="2026-07-10T00:00:00", end="2026-07-12T00:00:00",
    ))
    assert [e["summary"] for e in out["events"]] == ["Public Holiday"]
