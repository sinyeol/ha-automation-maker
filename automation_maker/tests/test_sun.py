"""APP-PORT-PLAN S2 (sun 축) 결정적 테스트.

  - SunProvider: 좌표 폴백·이벤트 순서·next_event·극지 폴백·캐시 무효화.
  - 검증기: sun 트리거 / sun_window 조건 정상·경계·오류(§3).
  - ha_map: sun 트리거·sun_window 조건 → HA v1 방언 왕복(§2.6).
  - evaluator: sun_window 자정 걸침·오프셋·provider 부재(§2.5).
  - 엔진: sun 타이머 스케줄(지연 계산)·발화 후 재장전·_unindex 취소·
          재연결(resync)/재시작(_compile_all) 시 자동 발화 0(§2.2 게이트4).

엔진 테스트는 now_fn 고정 + StubSun 으로 결정적이며, 발화는 _on_sun 직접 호출로
검증한다(벽시계 대기 없이 스케줄 재장전·취소·무발화 불변식을 고정).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time as dtime, timedelta

import pytest

from backend.automation_builder import _build_condition, _build_trigger
from backend.engine.engine import RuleEngine
from backend.engine.evaluator import EvalContext, evaluate_condition
from backend.engine.ha_map import subrule_to_automation
from backend.engine.rule_model import validate_rule_model
from backend.engine.runlog import RunLog
from backend.engine.rule_store import RuleStore
from backend.engine.state_cache import StateCache
from backend.engine.storage import JsonStore
from backend.engine.sun import SunProvider
from backend.engine.variables import GlobalVars


# ===========================================================================
# SunProvider
# ===========================================================================
def test_sun_events_order_and_date():
    sp = SunProvider({"location": {"latitude": 37.5665, "longitude": 126.9780}},
                     tz_offset_hours=9)
    ev = sp.events(date(2026, 7, 16))
    assert ev["sunrise"].date() == date(2026, 7, 16)
    assert ev["sunset"].date() == date(2026, 7, 16)
    assert ev["sunrise"] < ev["sunset"]
    # 서울 한여름: 일출 05시대, 일몰 19~20시대(±수분 근사).
    assert dtime(5, 0) <= ev["sunrise"].time() <= dtime(6, 0)
    assert dtime(19, 0) <= ev["sunset"].time() <= dtime(20, 30)


def test_sun_location_fallback_to_seoul():
    """location 부재/형식오류 → 서울 좌표로 폴백(결정성)."""
    a = SunProvider({}, tz_offset_hours=9).events(date(2026, 7, 16))
    b = SunProvider({"location": {"latitude": "x", "longitude": None}},
                    tz_offset_hours=9).events(date(2026, 7, 16))
    seoul = SunProvider({"location": {"latitude": 37.5665, "longitude": 126.9780}},
                        tz_offset_hours=9).events(date(2026, 7, 16))
    assert a == b == seoul


def test_sun_next_event_after_now():
    sp = SunProvider({"location": {"latitude": 37.5665, "longitude": 126.9780}},
                     tz_offset_hours=9)
    ev = sp.events(date(2026, 7, 16))
    # 정오엔 오늘 일몰이 다음, 일출은 내일.
    noon = datetime(2026, 7, 16, 12, 0, 0)
    assert sp.next_event("sunset", 0, noon) == ev["sunset"]
    assert sp.next_event("sunrise", 0, noon).date() == date(2026, 7, 17)
    # 오프셋(초) 적용.
    assert sp.next_event("sunset", -3600, noon) == ev["sunset"] - timedelta(hours=1)
    # 오늘 일몰이 지난 뒤엔 내일 일몰.
    after = ev["sunset"] + timedelta(minutes=1)
    assert sp.next_event("sunset", 0, after).date() == date(2026, 7, 17)


def test_sun_polar_fallback():
    """극지(무일몰) → 07:00/18:00 폴백(크래시·비결정 금지)."""
    sp = SunProvider({"location": {"latitude": 78.0, "longitude": 15.0}},
                     tz_offset_hours=1)
    ev = sp.events(date(2026, 6, 21))
    assert ev["sunrise"].time() == dtime(7, 0)
    assert ev["sunset"].time() == dtime(18, 0)


def test_sun_invalidate_clears_cache():
    sp = SunProvider({"location": {"latitude": 37.5665, "longitude": 126.9780}},
                     tz_offset_hours=9)
    a = sp.events(date(2026, 7, 16))
    assert sp.events(date(2026, 7, 16)) is a  # 캐시 히트(동일 객체 반환)
    sp.invalidate()
    assert sp._cache == {}                     # 캐시 비워짐
    b = sp.events(date(2026, 7, 16))
    assert b == a and b is not a               # 재계산(동등하나 새 객체)


# ===========================================================================
# 검증기 (rule_model)
# ===========================================================================
def _model(triggers, conditions=()):
    return {"subrules": [{"triggers": list(triggers), "conditions": list(conditions),
                          "actions": [{"type": "service", "action": "light.turn_on"}]}]}


_INV = {"entities": []}  # valid_ids 비움 → 실존 검사 스킵(노드 필드 검증만)


def test_validate_sun_trigger_ok():
    assert validate_rule_model(_model([{"type": "sun", "event": "sunset"}]), _INV) == []
    assert validate_rule_model(
        _model([{"type": "sun", "event": "sunrise", "offset": -2700}]), _INV) == []
    # 경계: ±12시간 정확히 43200초는 허용.
    assert validate_rule_model(
        _model([{"type": "sun", "event": "sunset", "offset": 43200}]), _INV) == []


def test_validate_sun_trigger_bad_event():
    errs = validate_rule_model(_model([{"type": "sun", "event": "noon"}]), _INV)
    assert any(".event" in e["path"] for e in errs)


def test_validate_sun_trigger_bad_offset():
    # float/str/bool 은 정수 아님 → 거부.
    for bad in (12.5, "3600", True):
        errs = validate_rule_model(
            _model([{"type": "sun", "event": "sunset", "offset": bad}]), _INV)
        assert any(".offset" in e["path"] for e in errs), bad
    # 범위 초과(43201초 = 12시간 1초).
    errs = validate_rule_model(
        _model([{"type": "sun", "event": "sunset", "offset": 43201}]), _INV)
    assert any(".offset" in e["path"] for e in errs)


def test_validate_sun_window_condition_ok():
    m = _model([{"type": "daily", "at": "07:00"}],
               [{"type": "sun_window", "after": "sunset", "before": "sunrise"}])
    assert validate_rule_model(m, _INV) == []
    m2 = _model([{"type": "daily", "at": "07:00"}],
                [{"type": "sun_window", "after": "sunset", "before": "sunrise",
                  "after_offset": -1800, "before_offset": 600}])
    assert validate_rule_model(m2, _INV) == []


def test_validate_sun_window_bad_fields():
    # after/before 필수·값 검증.
    m = _model([{"type": "daily", "at": "07:00"}],
               [{"type": "sun_window", "after": "midnight", "before": "sunrise"}])
    errs = validate_rule_model(m, _INV)
    assert any(".after" in e["path"] for e in errs)
    # 오프셋 범위 초과.
    m2 = _model([{"type": "daily", "at": "07:00"}],
                [{"type": "sun_window", "after": "sunset", "before": "sunrise",
                  "after_offset": 99999}])
    errs2 = validate_rule_model(m2, _INV)
    assert any(".after_offset" in e["path"] for e in errs2)


def test_validate_sun_no_longer_unsupported():
    """_UNSUPPORTED 에서 sun 제거 확인 — sun 트리거가 '지원 안 함'으로 거부되지 않는다."""
    errs = validate_rule_model(_model([{"type": "sun", "event": "sunset"}]), _INV)
    assert not any("지원하지 않" in e["message"] for e in errs)


# ===========================================================================
# ha_map (§2.6) — v2 → HA v1 방언 왕복
# ===========================================================================
def test_ha_map_sun_trigger_offset_to_hhmmss():
    out = subrule_to_automation(
        {"triggers": [{"type": "sun", "event": "sunset", "offset": -2700}],
         "conditions": [], "actions": []})
    tr = out["triggers"][0]
    assert tr == {"type": "sun", "event": "sunset", "offset": "-00:45:00"}
    # 빌더가 소비 가능(v1 자동화 방언).
    assert _build_trigger(tr) == {"trigger": "sun", "event": "sunset",
                                  "offset": "-00:45:00"}
    assert out["warnings"] == []


def test_ha_map_sun_trigger_zero_offset_omitted():
    out = subrule_to_automation(
        {"triggers": [{"type": "sun", "event": "sunrise", "offset": 0}],
         "conditions": [], "actions": []})
    assert out["triggers"][0] == {"type": "sun", "event": "sunrise"}
    assert _build_trigger(out["triggers"][0]) == {"trigger": "sun", "event": "sunrise"}


def test_ha_map_sun_window_to_sun_condition():
    out = subrule_to_automation(
        {"triggers": [], "conditions": [
            {"type": "sun_window", "after": "sunset", "before": "sunrise",
             "after_offset": 3600}], "actions": []})
    cond = out["conditions"][0]
    assert cond == {"type": "sun", "after": "sunset", "before": "sunrise",
                    "after_offset": "+01:00:00"}
    assert _build_condition(cond) == {"condition": "sun", "after": "sunset",
                                      "before": "sunrise", "after_offset": "+01:00:00"}


def test_ha_map_unmappable_warns():
    out = subrule_to_automation(
        {"triggers": [{"type": "segment", "to": "night"}], "conditions": [], "actions": []})
    assert out["triggers"] == []
    assert out["warnings"]


# ===========================================================================
# evaluator sun_window (§2.5) — 순수 단위
# ===========================================================================
class FixedSun:
    def __init__(self, sunrise=dtime(6, 0), sunset=dtime(18, 0)):
        self._sr, self._ss = sunrise, sunset

    def events(self, d):
        return {"sunrise": datetime.combine(d, self._sr),
                "sunset": datetime.combine(d, self._ss)}


def _ctx(now, sun=None):
    return EvalContext(cache=None, gvars=None, now_fn=lambda: now,
                       inventory_fn=lambda: {}, sun=sun)


def _sw(after="sunset", before="sunrise", **extra):
    return {"type": "sun_window", "after": after, "before": before, **extra}


def test_sun_window_midnight_crossing_inside():
    sun = FixedSun()  # 일몰 18:00, 일출 06:00
    # 밤 23시: 창(18:00~다음날 06:00) 안.
    assert evaluate_condition(_sw(), _ctx(datetime(2026, 7, 16, 23, 0), sun)) is True
    # 새벽 03시: 창 안(자정 넘김).
    assert evaluate_condition(_sw(), _ctx(datetime(2026, 7, 16, 3, 0), sun)) is True


def test_sun_window_midnight_crossing_outside():
    sun = FixedSun()
    # 정오 12시: 낮 → 창 밖.
    assert evaluate_condition(_sw(), _ctx(datetime(2026, 7, 16, 12, 0), sun)) is False


def test_sun_window_day_window_non_crossing():
    sun = FixedSun()
    # after=일출, before=일몰 → 낮 창(06:00~18:00), 자정 안 걸침.
    day = _sw(after="sunrise", before="sunset")
    assert evaluate_condition(day, _ctx(datetime(2026, 7, 16, 12, 0), sun)) is True
    assert evaluate_condition(day, _ctx(datetime(2026, 7, 16, 5, 0), sun)) is False


def test_sun_window_offset_shifts_boundary():
    sun = FixedSun()
    # 일몰+1시간(19:00)~일출 창. 18:30 은 아직 창 밖.
    w = _sw(after_offset=3600)
    assert evaluate_condition(w, _ctx(datetime(2026, 7, 16, 18, 30), sun)) is False
    assert evaluate_condition(w, _ctx(datetime(2026, 7, 16, 19, 30), sun)) is True


def test_sun_window_no_provider_false():
    """provider 미주입 → False(크래시 금지)."""
    assert evaluate_condition(_sw(), _ctx(datetime(2026, 7, 16, 23, 0), sun=None)) is False


# ===========================================================================
# 엔진 sun 스케줄 (§2.2) — 결정적
# ===========================================================================
class _FakeSource:
    def __init__(self, initial=None):
        self.initial = initial or []
        self._on_event = self._on_resync = self._on_connect = self._on_disconnect = None

    async def start(self, on_event, on_resync, on_connect=None, on_disconnect=None):
        self._on_event, self._on_resync = on_event, on_resync
        self._on_connect, self._on_disconnect = on_connect, on_disconnect
        on_resync(self.initial)
        if on_connect:
            on_connect()

    async def stop(self):
        if self._on_disconnect:
            self._on_disconnect()

    def resync(self, states):
        if self._on_connect:
            self._on_connect()
        self._on_resync(states)


class _RecordingHA:
    def __init__(self):
        self.calls = []

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, dict(data or {})))


class _StubSun:
    """결정적 sun provider: next_event 반환을 호출 순서대로 제어(소진 시 마지막 값 유지)."""

    def __init__(self, whens):
        self._whens = list(whens)
        self.calls = []

    def next_event(self, event, offset_sec, now):
        self.calls.append((event, offset_sec, now))
        return self._whens[min(len(self.calls) - 1, len(self._whens) - 1)]

    def events(self, d):
        return {"sunrise": datetime(d.year, d.month, d.day, 6, 0),
                "sunset": datetime(d.year, d.month, d.day, 18, 0)}


_SUN_RULE = {"sentence": "해 지면 소등", "model": {
    "triggers": [{"type": "sun", "event": "sunset"}],
    "condition_mode": "and", "conditions": [],
    "actions": [{"type": "service", "action": "light.turn_off",
                 "target": {"entity_id": ["light.living_room_main"]}}]}}


async def _build_sun(data_dir, now, sun):
    loop = asyncio.get_running_loop()
    rs = RuleStore(JsonStore(data_dir / "rules.json", [], loop=loop))
    rl = RunLog(JsonStore(data_dir / "runlog.json", [], loop=loop))
    gvars = GlobalVars({}, now_fn=lambda: now)
    ha = _RecordingHA()
    engine = RuleEngine(rs, StateCache(), gvars, ha, lambda: {"entities": []}, rl,
                        now_fn=lambda: now, loop=loop, sun_provider=sun)
    return engine, rs, rl, ha


@pytest.mark.asyncio
async def test_engine_sun_schedules_timer_with_delay(v2_data_dir):
    now = datetime(2026, 7, 16, 12, 0, 0)
    sun = _StubSun([now + timedelta(seconds=120)])
    engine, rs, rl, ha = await _build_sun(v2_data_dir, now, sun)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_SUN_RULE))
    engine.reload_rule(saved["id"])

    key = (saved["id"], 0)
    assert key in engine._daily_timers          # 타이머 무장
    handle = engine._daily_timers[key]
    delay = handle.when() - engine._loop.time()
    assert 118.0 < delay < 121.0                # (when - now) = 120초
    assert sun.calls and sun.calls[0][0] == "sunset"
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_sun_fires_and_rearms(v2_data_dir):
    now = datetime(2026, 7, 16, 12, 0, 0)
    # 1차 arm=오늘 일몰, 재장전=내일(발화 후 다음 이벤트).
    sun = _StubSun([now + timedelta(seconds=60),
                    now + timedelta(days=1, seconds=60)])
    engine, rs, rl, ha = await _build_sun(v2_data_dir, now, sun)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_SUN_RULE))
    engine.reload_rule(saved["id"])

    engine._on_sun(saved["id"], 0)              # 타이머 만료 시뮬레이션(결정적)
    await asyncio.sleep(0.05)                    # launch 태스크 실행 대기

    assert ha.calls == [("light", "turn_off", {"entity_id": ["light.living_room_main"]})]
    assert [e["result"] for e in rl.entries()] == ["fired"]
    assert (saved["id"], 0) in engine._daily_timers   # 익일 재장전됨
    assert len(sun.calls) == 2                          # 최초 + 재장전
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_sun_unindex_cancels_timer(v2_data_dir):
    now = datetime(2026, 7, 16, 12, 0, 0)
    sun = _StubSun([now + timedelta(seconds=3600)])
    engine, rs, rl, ha = await _build_sun(v2_data_dir, now, sun)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_SUN_RULE))
    engine.reload_rule(saved["id"])
    key = (saved["id"], 0)
    handle = engine._daily_timers[key]

    engine._unindex_rule(saved["id"])
    assert key not in engine._daily_timers
    assert handle.cancelled()
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_sun_resync_does_not_fire(v2_data_dir):
    """재연결(resync)은 재계산만 — sun 자동 발화 0(게이트4)."""
    now = datetime(2026, 7, 16, 12, 0, 0)
    sun = _StubSun([now + timedelta(seconds=3600)])
    engine, rs, rl, ha = await _build_sun(v2_data_dir, now, sun)
    src = _FakeSource()
    await engine.start(src)
    saved = rs.upsert(dict(_SUN_RULE))
    engine.reload_rule(saved["id"])

    src.resync([])                              # 재연결 통지
    await asyncio.sleep(0.05)

    assert ha.calls == []                        # 무발화
    assert (saved["id"], 0) in engine._daily_timers  # 타이머는 유지
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_sun_restart_recomputes_no_fire(v2_data_dir):
    """재시작(_compile_all)은 타이머 재계산만 — 자동 발화 0(게이트4)."""
    now = datetime(2026, 7, 16, 12, 0, 0)
    sun = _StubSun([now + timedelta(seconds=3600)])
    engine, rs, rl, ha = await _build_sun(v2_data_dir, now, sun)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_SUN_RULE))
    engine.reload_rule(saved["id"])
    first = engine._daily_timers[(saved["id"], 0)]

    engine._compile_all()                        # 재시작 모사(재컴파일)
    await asyncio.sleep(0.05)

    assert ha.calls == []                        # 무발화
    second = engine._daily_timers[(saved["id"], 0)]
    assert second is not first                   # 재계산으로 새 타이머
    assert first.cancelled()
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_sun_no_provider_dormant_not_disabled(v2_data_dir):
    """sun_provider 미주입 시 sun 규칙은 휴면(무스케줄) — auto_disabled 되지 않는다."""
    now = datetime(2026, 7, 16, 12, 0, 0)
    engine, rs, rl, ha = await _build_sun(v2_data_dir, now, sun=None)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_SUN_RULE))
    engine.reload_rule(saved["id"])

    assert saved["id"] in engine._rules                  # 인덱싱됨(트리거 존재)
    assert (saved["id"], 0) not in engine._daily_timers  # 스케줄 없음
    assert not (rs.get(saved["id"]).get("meta") or {}).get("auto_disabled")
    await engine.stop()
