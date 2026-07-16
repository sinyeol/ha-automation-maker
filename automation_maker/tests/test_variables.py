"""GlobalVars (SPEC-V2 §1) 단위 테스트 — 시계 주입으로 결정적.

segment/season/day_type/next_boundary 를 now_fn 주입으로 검증한다.
공휴일(holidays) 관련 단정은 라이브러리가 있을 때만 수행한다(skip 마커).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from backend.engine.variables import GlobalVars

try:
    import holidays as _holidays  # noqa: F401
    _HAS_HOLIDAYS = True
except Exception:  # pragma: no cover
    _HAS_HOLIDAYS = False


def _gv(dt: datetime, settings: dict | None = None) -> GlobalVars:
    return GlobalVars(settings or {}, now_fn=lambda: dt)


# ---------------------------------------------------------------------------
# segment: 기본 경계 dawn 00:00 / morning 06:00 / day 09:00 / evening 17:00 / night 21:00
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("hh,mm,expected", [
    (0, 0, "dawn"),
    (3, 30, "dawn"),
    (5, 59, "dawn"),
    (6, 0, "morning"),
    (8, 59, "morning"),
    (9, 0, "day"),
    (16, 59, "day"),
    (17, 0, "evening"),
    (20, 59, "evening"),
    (21, 0, "night"),
    (23, 59, "night"),
])
def test_segment_default_boundaries(hh, mm, expected):
    assert _gv(datetime(2026, 7, 16, hh, mm)).segment() == expected


def test_segment_custom_boundaries():
    # morning 을 05:00 으로 앞당기면 05:30 은 morning
    settings = {"segments": {"dawn": "00:00", "morning": "05:00", "day": "09:00",
                             "evening": "17:00", "night": "21:00"}}
    assert _gv(datetime(2026, 7, 16, 5, 30), settings).segment() == "morning"
    assert _gv(datetime(2026, 7, 16, 4, 59), settings).segment() == "dawn"


def test_is_in_segments():
    gv = _gv(datetime(2026, 7, 16, 22, 0))  # night
    assert gv.is_in_segments(["night"]) is True
    assert gv.is_in_segments(["dawn", "morning"]) is False
    assert gv.is_in_segments([]) is False


# ---------------------------------------------------------------------------
# season: 3-5 봄 / 6-8 여름 / 9-11 가을 / 12-2 겨울
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("month,expected", [
    (3, "spring"), (4, "spring"), (5, "spring"),
    (6, "summer"), (7, "summer"), (8, "summer"),
    (9, "autumn"), (10, "autumn"), (11, "autumn"),
    (12, "winter"), (1, "winter"), (2, "winter"),
])
def test_season(month, expected):
    assert _gv(datetime(2026, month, 15, 12, 0)).season() == expected


# ---------------------------------------------------------------------------
# day_type: weekday / weekend / holiday
# ---------------------------------------------------------------------------
def test_day_type_weekday():
    # 2026-07-16 은 목요일
    assert _gv(datetime(2026, 7, 16, 12, 0)).day_type() == "weekday"


def test_day_type_weekend():
    # 2026-07-18 은 토요일, 07-19 는 일요일
    assert _gv(datetime(2026, 7, 18, 12, 0)).day_type() == "weekend"
    assert _gv(datetime(2026, 7, 19, 12, 0)).day_type() == "weekend"


@pytest.mark.skipif(not _HAS_HOLIDAYS, reason="holidays 라이브러리 미설치")
def test_day_type_holiday():
    # 신정(2026-01-01)은 목요일이지만 공휴일 → holiday 가 우선
    assert _gv(datetime(2026, 1, 1, 12, 0)).day_type() == "holiday"


def test_day_type_no_holidays_lib_degrades(monkeypatch):
    # holidays 로드 실패해도 크래시 없이 weekday/weekend 로 동작
    gv = _gv(datetime(2026, 1, 1, 12, 0))
    gv._holidays = None  # 라이브러리 미설치 상황 모사
    assert gv.day_type() in ("weekday", "weekend")


# ---------------------------------------------------------------------------
# next_boundary: 다음 경계 시각(경계 재평가 스케줄용)
# ---------------------------------------------------------------------------
def test_next_boundary_within_day():
    # 07:00(morning) → 다음 경계 09:00(day)
    nb = _gv(datetime(2026, 7, 16, 7, 0)).next_boundary()
    assert (nb.hour, nb.minute) == (9, 0)
    assert nb.date() == datetime(2026, 7, 16).date()


def test_next_boundary_wraps_to_next_day():
    # 22:00(night) → 다음 경계는 익일 00:00(dawn)
    nb = _gv(datetime(2026, 7, 16, 22, 0)).next_boundary()
    assert (nb.hour, nb.minute) == (0, 0)
    assert nb.date() == datetime(2026, 7, 17).date()


def test_next_boundary_exactly_on_boundary_advances():
    # 정확히 09:00 이면 다음은 17:00 (현재 경계는 지난 것으로 취급)
    nb = _gv(datetime(2026, 7, 16, 9, 0)).next_boundary()
    assert (nb.hour, nb.minute) == (17, 0)


# ---------------------------------------------------------------------------
# snapshot: API 노출용
# ---------------------------------------------------------------------------
def test_snapshot_shape():
    snap = _gv(datetime(2026, 7, 16, 22, 0)).snapshot()
    assert snap["segment"] == "night"
    assert snap["segment_label"] == "밤"
    assert snap["season"] == "summer"
    assert snap["day_type"] == "weekday"
    assert snap["date"] == "2026-07-16"
    assert snap["weekday"] == "목"
