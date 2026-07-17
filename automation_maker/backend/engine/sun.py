"""SunProvider — 일출/일몰 시각 계산 (APP-PORT-PLAN §2.1, SPEC-SCHEMA-90 §2.1).

NOAA 근사식(순수 파이썬, 의존성 0)으로 지정 좌표의 오늘 sunrise/sunset 을 로컬 naive
datetime 으로 계산한다. offset(초)을 적용한 다음 이벤트 시각(엔진 스케줄용)과 이벤트
시각 쌍(evaluator sun_window 용)을 제공한다.

- 좌표: ``settings["location"] = {"latitude", "longitude"}``. 부재/형식오류 시 **서울
  37.5665/126.9780** 로 폴백(결정성 보장).
- 일 단위 캐시 ``{(iso_date, lat, lon, tz): events}``. ``invalidate()`` 로 비운다(설정
  위치 변경 시 api_v2 가 호출).
- 극지 무일출/무일몰(|cos H| > 1)은 07:00/18:00 로컬 폴백 + log.warning(한국 좌표에선
  미발생, 결정성 보장용).
- 타임존: 시스템 로컬 오프셋(``datetime.now().astimezone()``)을 UTC→로컬 변환에 쓴다.
  테스트 결정성을 위해 ``tz_offset_hours`` 로 주입할 수 있다(주입 시 시스템 미조회).

정확도: sunrise/sunset 은 ±수분 근사(런타임 발화 시각 용도로 충분). 파서/채점은 sun 노드의
event/offset(표면형에서 산출)만 비교하므로 이 계산의 오차는 정확도 지표에 영향을 주지 않는다.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("automation_maker.engine.sun")

# settings.location 부재 시 기본 좌표(서울).
_SEOUL_LAT = 37.5665
_SEOUL_LON = 126.9780

# 대기 굴절 + 태양 원반 반영 표준 천정각(도).
_ZENITH_DEG = 90.833

_EVENTS = ("sunrise", "sunset")


class SunProvider:
    def __init__(self, settings_ref: dict | None = None, now_fn=None,
                 tz_offset_hours: float | None = None):
        self._settings = settings_ref if isinstance(settings_ref, dict) else {}
        self._now_fn = now_fn or (lambda: datetime.now())
        self._tz_override = tz_offset_hours
        self._cache: dict[tuple, dict] = {}

    # ------------------------------------------------------------------ 좌표/tz
    def _latlon(self) -> tuple[float, float]:
        loc = self._settings.get("location") if isinstance(self._settings, dict) else None
        if isinstance(loc, dict):
            try:
                lat = float(loc.get("latitude"))
                lon = float(loc.get("longitude"))
                return lat, lon
            except (TypeError, ValueError):
                pass
        return _SEOUL_LAT, _SEOUL_LON

    def _tz_hours(self) -> float:
        if self._tz_override is not None:
            return float(self._tz_override)
        try:
            off = datetime.now(timezone.utc).astimezone().utcoffset()
            return off.total_seconds() / 3600.0 if off is not None else 0.0
        except Exception:  # pragma: no cover - 방어
            return 0.0

    # ------------------------------------------------------------------ 계산
    def events(self, d: date) -> dict:
        """date d 의 {"sunrise": datetime, "sunset": datetime} (로컬 naive). 일 단위 캐시."""
        lat, lon = self._latlon()
        tz = self._tz_hours()
        key = (d.isoformat(), lat, lon, tz)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        ev = self._compute(d, lat, lon, tz)
        self._cache[key] = ev
        return ev

    @staticmethod
    def _compute(d: date, lat: float, lon: float, tz: float) -> dict:
        n = d.timetuple().tm_yday
        gamma = 2.0 * math.pi / 365.0 * (n - 1 + 0.5)
        eqtime = 229.18 * (
            0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
            - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma))
        decl = (0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
                - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
                - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma))
        lat_r = math.radians(lat)
        # 로컬 표준시 기준 정오(분): 720 − 4·경도(동경+) − eqtime + tz·60.
        snoon = 720.0 - 4.0 * lon - eqtime + tz * 60.0
        midnight = datetime(d.year, d.month, d.day)
        try:
            cos_ha = ((math.cos(math.radians(_ZENITH_DEG))
                       - math.sin(lat_r) * math.sin(decl))
                      / (math.cos(lat_r) * math.cos(decl)))
        except ZeroDivisionError:  # pragma: no cover - 극점 방어
            cos_ha = 2.0
        if cos_ha > 1.0 or cos_ha < -1.0:
            # 극야/백야: 결정적 폴백(07:00/18:00 로컬). 한국 좌표에선 미발생.
            log.warning("극지 무일출/무일몰(cos_ha=%.3f) — 07:00/18:00 폴백", cos_ha)
            return {"sunrise": midnight + timedelta(hours=7),
                    "sunset": midnight + timedelta(hours=18)}
        ha_deg = math.degrees(math.acos(cos_ha))
        sunrise_min = snoon - 4.0 * ha_deg
        sunset_min = snoon + 4.0 * ha_deg
        return {"sunrise": midnight + timedelta(minutes=sunrise_min),
                "sunset": midnight + timedelta(minutes=sunset_min)}

    # ------------------------------------------------------------------ 엔진 API
    def next_event(self, event: str, offset_sec: int, now: datetime) -> datetime:
        """now 이후 첫 (event + offset) 로컬 datetime. event ∈ {sunrise, sunset}."""
        if event not in _EVENTS:
            event = "sunset"
        off = timedelta(seconds=int(offset_sec or 0))
        base = now.date()
        for add in (0, 1, 2):
            ev = self.events(base + timedelta(days=add))
            when = ev.get(event)
            if when is None:
                continue
            when = when + off
            if when > now:
                return when
        # 도달 불가(방어): 내일 이벤트.
        ev = self.events(base + timedelta(days=1))
        return ev[event] + off

    def invalidate(self) -> None:
        """캐시를 비운다(설정 위치 변경 시)."""
        self._cache.clear()
