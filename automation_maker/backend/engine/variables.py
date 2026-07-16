"""전역 변수 — Fibaro $TimeOfDay 개념의 내장판 (§1)."""
from __future__ import annotations

from datetime import datetime, timedelta

SEGMENTS = ["dawn", "morning", "day", "evening", "night"]  # 새벽/아침/낮/저녁/밤
SEGMENT_LABELS = {"dawn": "새벽", "morning": "아침", "day": "낮", "evening": "저녁", "night": "밤"}

DEFAULT_SEGMENTS = {
    "dawn": "00:00", "morning": "06:00", "day": "09:00", "evening": "17:00", "night": "21:00",
}

_SEASON_BY_MONTH = {
    3: "spring", 4: "spring", 5: "spring",
    6: "summer", 7: "summer", 8: "summer",
    9: "autumn", 10: "autumn", 11: "autumn",
    12: "winter", 1: "winter", 2: "winter",
}

_WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]


def _parse_hhmm(value: str) -> int:
    """"HH:MM" → 자정 기준 분. 형식 오류 시 0."""
    try:
        h, m = str(value).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


class GlobalVars:
    def __init__(self, settings: dict, now_fn=None):
        self._settings = settings or {}
        self._now = now_fn or (lambda: datetime.now())
        try:
            import holidays  # noqa: PLC0415
            self._holidays = holidays.country_holidays("KR")
        except Exception:
            # holidays 미설치/실패 시 weekday/weekend만으로 동작(크래시 금지)
            self._holidays = None

    def _boundaries(self) -> list[tuple[str, int]]:
        segs = self._settings.get("segments") or DEFAULT_SEGMENTS
        return [(key, _parse_hhmm(segs.get(key, DEFAULT_SEGMENTS[key]))) for key in SEGMENTS]

    def segment(self) -> str:
        now = self._now()
        cur = now.hour * 60 + now.minute + now.second / 60.0
        bounds = sorted(self._boundaries(), key=lambda kv: kv[1])
        active = bounds[-1][0]  # 첫 경계 이전 시각은 자정 걸침(마지막 세그먼트)
        for key, minute in bounds:
            if cur >= minute:
                active = key
        return active

    def season(self) -> str:
        return _SEASON_BY_MONTH[self._now().month]

    def day_type(self) -> str:
        now = self._now()
        day = now.date()
        if self._holidays is not None:
            try:
                if day in self._holidays:
                    return "holiday"
            except Exception:
                pass
        return "weekend" if now.weekday() >= 5 else "weekday"

    def is_in_segments(self, segs: list[str]) -> bool:
        return self.segment() in (segs or [])

    def next_boundary(self) -> datetime:
        now = self._now()
        cur = now.hour * 60 + now.minute + now.second / 60.0
        minutes = sorted({m for _, m in self._boundaries()})
        for m in minutes:
            if m > cur:
                h, mi = divmod(m, 60)
                return now.replace(hour=h, minute=mi, second=0, microsecond=0)
        h, mi = divmod(minutes[0], 60)
        return (now + timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0)

    def snapshot(self) -> dict:
        now = self._now()
        seg = self.segment()
        return {
            "segment": seg,
            "segment_label": SEGMENT_LABELS[seg],
            "season": self.season(),
            "day_type": self.day_type(),
            "date": now.date().isoformat(),
            "weekday": _WEEKDAY_KO[now.weekday()],
        }
