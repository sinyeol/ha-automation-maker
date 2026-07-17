"""APP-PORT-PLAN S4 (주기 축 = time_pattern) 결정적 테스트.

  - _next_pattern_time: /N 다음 배수 시각 계산(분/시/초·롤오버·경계 직전/직후).
  - 검증기: time_pattern 트리거 정확히 1필드·정수 N≥1·분/초≤59·시≤23(§3).
  - ha_map: time_pattern 정수 N → HA `/N` 방언 왕복(§2.6).
  - postpass: N분/시간마다 → time_pattern 트리거 방출·상태/수치 트리거 강등·repeat 게이트(#22).
  - 엔진: time_pattern 타이머 스케줄(지연 계산)·발화 후 다음 배수 재장전·_unindex 취소·
          재연결(resync)/재시작(_compile_all) 시 자동 발화 0(§2.3 게이트4).

엔진 테스트는 now_fn 고정으로 결정적이며, 발화는 _on_pattern 직접 호출로 검증한다
(벽시계 대기 없이 스케줄 재장전·취소·무발화 불변식을 고정).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from backend.automation_builder import _build_trigger
from backend.engine.engine import RuleEngine
from backend.engine.ha_map import subrule_to_automation
from backend.engine.rule_model import validate_rule_model
from backend.engine.runlog import RunLog
from backend.engine.rule_store import RuleStore
from backend.engine.state_cache import StateCache
from backend.engine.storage import JsonStore
from backend.engine.variables import GlobalVars
from backend.nl import postpass


# ===========================================================================
# _next_pattern_time (§2.3) — 순수 배수 계산
# ===========================================================================
_f = RuleEngine._next_pattern_time


def test_next_pattern_minutes_basic():
    # /30 → 매시 :00·:30. 10:15:30 → 10:30:00.
    assert _f(datetime(2026, 7, 16, 10, 15, 30), "minutes", 30) == datetime(2026, 7, 16, 10, 30, 0)
    # 경계 위(10:30:00)에서는 다음 슬롯(11:00:00) — 엄격히 이후(이중발화 방지).
    assert _f(datetime(2026, 7, 16, 10, 30, 0), "minutes", 30) == datetime(2026, 7, 16, 11, 0, 0)
    # /45: 10:15 → 10:45.
    assert _f(datetime(2026, 7, 16, 10, 15, 0), "minutes", 45) == datetime(2026, 7, 16, 10, 45, 0)
    # /45 롤오버: 10:50 → 11:00(0%45==0), 45 를 더한 :95 가 아니라 필드 매치.
    assert _f(datetime(2026, 7, 16, 10, 50, 0), "minutes", 45) == datetime(2026, 7, 16, 11, 0, 0)


def test_next_pattern_hours_and_seconds():
    # /2 시간: 10:15:30 → 12:00:00(분·초 0).
    assert _f(datetime(2026, 7, 16, 10, 15, 30), "hours", 2) == datetime(2026, 7, 16, 12, 0, 0)
    # 자정 롤오버: 23:30 → 익일 00:00.
    assert _f(datetime(2026, 7, 16, 23, 30, 0), "hours", 2) == datetime(2026, 7, 17, 0, 0, 0)
    # /15 초: 10:00:50 → 10:01:00(롤오버, 0%15==0).
    assert _f(datetime(2026, 7, 16, 10, 0, 50), "seconds", 15) == datetime(2026, 7, 16, 10, 1, 0)
    # 마이크로초는 무시하고 다음 배수 초.
    assert _f(datetime(2026, 7, 16, 10, 0, 5, 500000), "seconds", 20) == datetime(2026, 7, 16, 10, 0, 20)


def test_next_pattern_strictly_after_on_boundary():
    # 정확히 배수 시각(초 0)에서 계산하면 다음 배수로(같은 시각 반환 금지).
    assert _f(datetime(2026, 7, 16, 10, 0, 0), "seconds", 15) == datetime(2026, 7, 16, 10, 0, 15)


# ===========================================================================
# 검증기 (rule_model) — §3
# ===========================================================================
def _model(triggers):
    return {"subrules": [{"triggers": list(triggers), "conditions": [],
                          "actions": [{"type": "service", "action": "light.turn_on"}]}]}


_INV = {"entities": []}  # valid_ids 비움 → 노드 필드 검증만


def test_validate_time_pattern_ok():
    for node in ({"type": "time_pattern", "minutes": 30},
                 {"type": "time_pattern", "hours": 2},
                 {"type": "time_pattern", "seconds": 45},
                 {"type": "time_pattern", "minutes": 59},  # 분 경계
                 {"type": "time_pattern", "hours": 23},     # 시 경계
                 {"type": "time_pattern", "seconds": 1}):   # 하한
        assert validate_rule_model(_model([node]), _INV) == [], node


def test_validate_time_pattern_exactly_one_field():
    # 0개 또는 2개 이상 → 거부.
    errs = validate_rule_model(_model([{"type": "time_pattern"}]), _INV)
    assert errs
    errs2 = validate_rule_model(
        _model([{"type": "time_pattern", "minutes": 30, "hours": 2}]), _INV)
    assert errs2


def test_validate_time_pattern_bad_value():
    # 정수 아님(str/float/bool)·0·음수 → 거부.
    for bad in ("30", 5.0, True, 0, -1):
        errs = validate_rule_model(
            _model([{"type": "time_pattern", "minutes": bad}]), _INV)
        assert any(".minutes" in e["path"] for e in errs), bad


def test_validate_time_pattern_range_limits():
    # 분/초 ≤59, 시 ≤23 초과 → 거부.
    assert any(".minutes" in e["path"] for e in validate_rule_model(
        _model([{"type": "time_pattern", "minutes": 60}]), _INV))
    assert any(".seconds" in e["path"] for e in validate_rule_model(
        _model([{"type": "time_pattern", "seconds": 60}]), _INV))
    assert any(".hours" in e["path"] for e in validate_rule_model(
        _model([{"type": "time_pattern", "hours": 24}]), _INV))


def test_validate_time_pattern_not_unsupported():
    """time_pattern 이 '지원 안 함'으로 거부되지 않는다(엔진이 안다)."""
    errs = validate_rule_model(_model([{"type": "time_pattern", "minutes": 30}]), _INV)
    assert not any("지원하지 않" in e["message"] for e in errs)


# ===========================================================================
# ha_map (§2.6) — v2 정수 N → HA `/N` 방언 왕복
# ===========================================================================
def test_ha_map_time_pattern_int_to_slash():
    out = subrule_to_automation(
        {"triggers": [{"type": "time_pattern", "minutes": 30}],
         "conditions": [], "actions": []})
    tr = out["triggers"][0]
    assert tr == {"type": "time_pattern", "minutes": "/30"}
    # v1 빌더가 소비 가능(HA 자동화 방언).
    assert _build_trigger(tr) == {"trigger": "time_pattern", "minutes": "/30"}
    assert out["warnings"] == []


def test_ha_map_time_pattern_hours_and_idempotent():
    out = subrule_to_automation(
        {"triggers": [{"type": "time_pattern", "hours": 2}], "conditions": [], "actions": []})
    assert out["triggers"][0] == {"type": "time_pattern", "hours": "/2"}
    # 이미 "/N" 문자열이면 그대로(멱등).
    out2 = subrule_to_automation(
        {"triggers": [{"type": "time_pattern", "seconds": "/15"}],
         "conditions": [], "actions": []})
    assert out2["triggers"][0] == {"type": "time_pattern", "seconds": "/15"}


# ===========================================================================
# postpass (#22) — N분/시간마다 → time_pattern
# ===========================================================================
def test_detect_time_pattern_units():
    assert postpass._detect_time_pattern("45분마다 티비 꺼줘") == ("minutes", 45)
    assert postpass._detect_time_pattern("두 시간마다 방송") is None  # 수사 미정규화분은 감지 안 함
    assert postpass._detect_time_pattern("2시간마다 방송") == ("hours", 2)
    assert postpass._detect_time_pattern("30분 간격으로 확인") == ("minutes", 30)
    assert postpass._detect_time_pattern("20분 걸러 한 번씩") == ("minutes", 20)
    assert postpass._detect_time_pattern("한 시간에 한 번") is None  # '한'은 숫자 아님
    assert postpass._detect_time_pattern("1시간에 한 번") == ("hours", 1)
    # 'N시 M분마다'(벽시계+마다) 는 daily 지 주기가 아니다.
    assert postpass._detect_time_pattern("7시 30분마다") is None


def test_augment_time_pattern_demotes_state_to_condition():
    # 상태 트리거 + '10분마다' → time_pattern 트리거 + 상태 조건.
    sub = {"triggers": [{"type": "state", "entity_id": "binary_sensor.door", "to": "on"}],
           "conditions": [], "actions": [{"type": "service", "action": "notify.notify"}]}
    result = {"ok": False, "confidence": 0.0}
    postpass._augment_time_pattern("10분마다 현관문 열려 있으면 알려줘", sub, result)
    assert sub["triggers"] == [{"type": "time_pattern", "minutes": 10}]
    assert {"type": "state", "entity_id": "binary_sensor.door", "state": "on"} in sub["conditions"]
    assert result["ok"] is True  # _mark_savable 승급


def test_augment_time_pattern_noop_without_pattern():
    sub = {"triggers": [{"type": "state", "entity_id": "binary_sensor.door", "to": "on"}],
           "conditions": [], "actions": [{"type": "service", "action": "light.turn_on"}]}
    postpass._augment_time_pattern("현관문 열리면 불 켜줘", sub, None)
    assert sub["triggers"] == [{"type": "state", "entity_id": "binary_sensor.door", "to": "on"}]


def test_apply_repeat_gate_keeps_repeat_not_pattern():
    """repeat 케이던스('1초 간격으로 세 번')는 time_pattern 으로 바뀌지 않는다(is_repeat 게이트)."""
    sub = {"triggers": [{"type": "state", "entity_id": "binary_sensor.door", "to": "on"}],
           "conditions": [], "actions": [{"type": "service", "action": "light.turn_on"}]}
    result = {"ok": True, "confidence": 0.9,
              "model": {"subrules": [sub]}}
    postpass.apply(result, "작은방 창문 열리면 조명 세 번 깜빡여줘",
                   "작은방 창문 열리면 조명 세 번 깜빡여줘", gz=None, settings={})
    # 상태 트리거 유지(time_pattern 미방출).
    assert not any(t.get("type") == "time_pattern" for t in sub["triggers"])


# ===========================================================================
# 엔진 time_pattern 스케줄 (§2.3) — 결정적
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


_PATTERN_RULE = {"sentence": "45분마다 거실 티비 꺼줘", "model": {
    "triggers": [{"type": "time_pattern", "minutes": 45}],
    "condition_mode": "and", "conditions": [],
    "actions": [{"type": "service", "action": "media_player.turn_off",
                 "target": {"entity_id": ["media_player.living_room_tv"]}}]}}


async def _build(data_dir, now):
    loop = asyncio.get_running_loop()
    rs = RuleStore(JsonStore(data_dir / "rules.json", [], loop=loop))
    rl = RunLog(JsonStore(data_dir / "runlog.json", [], loop=loop))
    gvars = GlobalVars({}, now_fn=lambda: now)
    ha = _RecordingHA()
    engine = RuleEngine(rs, StateCache(), gvars, ha, lambda: {"entities": []}, rl,
                        now_fn=lambda: now, loop=loop)
    return engine, rs, rl, ha


@pytest.mark.asyncio
async def test_engine_pattern_schedules_timer_with_delay(v2_data_dir):
    now = datetime(2026, 7, 16, 10, 15, 0)   # /45 → 다음 10:45:00 = 1800초.
    engine, rs, rl, ha = await _build(v2_data_dir, now)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_PATTERN_RULE))
    engine.reload_rule(saved["id"])

    key = (saved["id"], 0)
    assert key in engine._daily_timers          # 타이머 무장(수명주기 dict 재사용)
    delay = engine._daily_timers[key].when() - engine._loop.time()
    assert 1798.0 < delay < 1802.0              # (10:45 - 10:15) = 1800초
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_pattern_fires_and_rearms_next_multiple(v2_data_dir):
    # /45 는 minute%45==0(=:00·:45) HA 동형. 10:45 발화 → 다음 배수는 11:00(엄격히 이후).
    now = datetime(2026, 7, 16, 10, 45, 0)
    engine, rs, rl, ha = await _build(v2_data_dir, now)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_PATTERN_RULE))
    engine.reload_rule(saved["id"])

    engine._on_pattern(saved["id"], 0)          # 타이머 만료 시뮬레이션(결정적)
    await asyncio.sleep(0.05)                    # launch 태스크 실행 대기

    assert ha.calls == [("media_player", "turn_off",
                         {"entity_id": ["media_player.living_room_tv"]})]
    assert [e["result"] for e in rl.entries()] == ["fired"]
    key = (saved["id"], 0)
    assert key in engine._daily_timers          # 다음 배수(11:00)로 재장전
    delay = engine._daily_timers[key].when() - engine._loop.time()
    assert 898.0 < delay < 902.0                # 10:45 → 11:00 = 900초(:45 다음은 :00)
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_pattern_unindex_cancels_timer(v2_data_dir):
    now = datetime(2026, 7, 16, 10, 15, 0)
    engine, rs, rl, ha = await _build(v2_data_dir, now)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_PATTERN_RULE))
    engine.reload_rule(saved["id"])
    key = (saved["id"], 0)
    handle = engine._daily_timers[key]

    engine._unindex_rule(saved["id"])
    assert key not in engine._daily_timers
    assert handle.cancelled()
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_pattern_resync_does_not_fire(v2_data_dir):
    """재연결(resync)은 재계산만 — time_pattern 자동 발화 0(게이트4)."""
    now = datetime(2026, 7, 16, 10, 15, 0)
    engine, rs, rl, ha = await _build(v2_data_dir, now)
    src = _FakeSource()
    await engine.start(src)
    saved = rs.upsert(dict(_PATTERN_RULE))
    engine.reload_rule(saved["id"])

    src.resync([])                              # 재연결 통지
    await asyncio.sleep(0.05)

    assert ha.calls == []                        # 무발화
    assert (saved["id"], 0) in engine._daily_timers  # 타이머는 유지
    await engine.stop()


@pytest.mark.asyncio
async def test_engine_pattern_restart_recomputes_no_fire(v2_data_dir):
    """재시작(_compile_all)은 타이머 재계산만 — 자동 발화 0(게이트4)."""
    now = datetime(2026, 7, 16, 10, 15, 0)
    engine, rs, rl, ha = await _build(v2_data_dir, now)
    await engine.start(_FakeSource())
    saved = rs.upsert(dict(_PATTERN_RULE))
    engine.reload_rule(saved["id"])
    first = engine._daily_timers[(saved["id"], 0)]

    engine._compile_all()                        # 재시작 모사(재컴파일)
    await asyncio.sleep(0.05)

    assert ha.calls == []                        # 무발화
    second = engine._daily_timers[(saved["id"], 0)]
    assert second is not first                   # 재계산으로 새 타이머
    assert first.cancelled()
    await engine.stop()
