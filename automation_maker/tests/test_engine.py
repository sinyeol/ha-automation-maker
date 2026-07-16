"""RuleEngine (SPEC-V2 §4) 결정적 테스트.

now_fn/loop 주입 + 짧은 실시간 타이머(0.05s)로 결정적으로 검증한다:
(a) 모션 on 이벤트 → 발화
(b) state_held: for 경과 시 발화 / 중간 off 면 미발화
(c) 조건 불충족 → skipped_condition 로그
(d) 액션 오류 3회 → auto_disabled
(e) 시간대 경계 재평가 → segment 트리거 발화
(f) pending timer 저장/복원 (만료 경과 + 조건 참이면 즉시 발화)
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from backend.engine.engine import RuleEngine
from backend.engine.runlog import RunLog
from backend.engine.rule_store import RuleStore
from backend.engine.state_cache import StateCache
from backend.engine.storage import JsonStore
from backend.engine.variables import GlobalVars

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 테스트 더블
# ---------------------------------------------------------------------------
_INVENTORY = {
    "areas": [{"area_id": "living_room", "name": "거실"},
              {"area_id": "master_bedroom", "name": "안방"}],
    "entities": [
        {"entity_id": "binary_sensor.living_room_motion", "domain": "binary_sensor",
         "name": "거실 모션", "area_id": "living_room", "device_class": "motion", "state": "off"},
        {"entity_id": "binary_sensor.master_bedroom_motion", "domain": "binary_sensor",
         "name": "안방 모션", "area_id": "master_bedroom", "device_class": "motion", "state": "off"},
        {"entity_id": "light.living_room_main", "domain": "light",
         "name": "거실 메인등", "area_id": "living_room", "state": "off"},
    ],
    "zones": [],
}


class FakeSource:
    """on_event/on_resync 를 직접 구동하는 모의 이벤트 소스(상시 연결)."""

    def __init__(self, initial=None):
        self.initial = initial or []
        self._on_event = None
        self._on_resync = None
        self._on_connect = None
        self._on_disconnect = None

    async def start(self, on_event, on_resync, on_connect=None, on_disconnect=None):
        self._on_event = on_event
        self._on_resync = on_resync
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        on_resync(self.initial)
        if on_connect is not None:
            on_connect()

    async def stop(self):
        self._on_event = None
        if self._on_disconnect is not None:
            self._on_disconnect()

    def inject(self, entity_id, new_state, old_state="off"):
        old = {"entity_id": entity_id, "state": old_state, "attributes": {}}
        new = {"entity_id": entity_id, "state": new_state, "attributes": {}}
        self._on_event(entity_id, old, new)

    def drop(self):
        """단절 통지(연결 끊김)."""
        if self._on_disconnect is not None:
            self._on_disconnect()

    def reconnect(self, states=None):
        """재연결 통지 + 선택적 resync 스냅샷 재적용."""
        if self._on_connect is not None:
            self._on_connect()
        if states is not None and self._on_resync is not None:
            self._on_resync(states)


class DeferredSource:
    """HA 처럼 start() 가 즉시 resync 하지 않고, 나중에 resync() 로 스냅샷을 주는 소스.

    HAEventSource 는 start() 가 백그라운드 태스크만 만들고 즉시 반환하므로
    복원 시점에 StateCache 가 비어 있는 상황을 재현한다(fix 1/8).
    """

    def __init__(self):
        self._on_event = None
        self._on_resync = None
        self._on_connect = None
        self._on_disconnect = None

    async def start(self, on_event, on_resync, on_connect=None, on_disconnect=None):
        self._on_event = on_event
        self._on_resync = on_resync
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        # 의도적으로 resync/connect 를 지연(백그라운드 연결 모사)

    async def stop(self):
        if self._on_disconnect is not None:
            self._on_disconnect()

    def resync(self, states):
        if self._on_connect is not None:
            self._on_connect()
        self._on_resync(states)

    def inject(self, entity_id, new_state, old_state="off"):
        old = {"entity_id": entity_id, "state": old_state, "attributes": {}}
        new = {"entity_id": entity_id, "state": new_state, "attributes": {}}
        self._on_event(entity_id, old, new)


class RecordingHA:
    """call_service 호출을 기록. fail=True 면 매 호출 예외를 던진다."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, dict(data or {})))
        if self.fail:
            raise RuntimeError("서비스 호출 실패")


def _seed(*entity_states):
    return [{"entity_id": eid, "state": st, "attributes": {}} for eid, st in entity_states]


async def _build(data_dir, *, fail=False, now_fn=None, rule_store=None, runlog=None):
    loop = asyncio.get_running_loop()
    rs = rule_store or RuleStore(JsonStore(data_dir / "rules.json", [], loop=loop))
    rl = runlog or RunLog(JsonStore(data_dir / "runlog.json", [], loop=loop))
    cache = StateCache()
    gvars = GlobalVars({}, now_fn=now_fn)
    ha = RecordingHA(fail=fail)
    engine = RuleEngine(rs, cache, gvars, ha, lambda: _INVENTORY, rl, now_fn=now_fn, loop=loop)
    return engine, rs, rl, ha


def _motion_rule(entity_id="binary_sensor.living_room_motion"):
    return {"sentence": "모션 발화", "model": {
        "triggers": [{"type": "state", "entity_id": entity_id, "to": "on"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.living_room_main"]}}]}}


def _held_rule(seconds=0.05, entity_id="binary_sensor.living_room_motion"):
    return {"sentence": "정지 후 발화", "model": {
        "triggers": [{"type": "state_held", "entity_id": entity_id, "to": "on",
                      "for": {"hours": 0, "minutes": 0, "seconds": seconds}}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.living_room_main"]}}]}}


def _delay_rule(delay_seconds=0.3, entity_id="binary_sensor.living_room_motion"):
    return {"sentence": "지연 후 소등", "model": {
        "triggers": [{"type": "state", "entity_id": entity_id, "to": "on"}],
        "condition_mode": "and", "conditions": [],
        "actions": [
            {"type": "service", "action": "light.turn_on",
             "target": {"entity_id": ["light.living_room_main"]}},
            {"type": "delay", "duration": {"hours": 0, "minutes": 0, "seconds": delay_seconds}},
            {"type": "service", "action": "light.turn_off",
             "target": {"entity_id": ["light.living_room_main"]}},
        ]}}


def _snap(entity_id, state, *, changed_ago=0.0, updated_ago=0.0):
    """resync 스냅샷 1건 — last_changed/last_updated 를 (지금 - N초) 로 지정."""
    now = datetime.now(timezone.utc)
    lc = (now - timedelta(seconds=changed_ago)).isoformat()
    lu = (now - timedelta(seconds=updated_ago)).isoformat()
    return [{"entity_id": entity_id, "state": state, "attributes": {},
             "last_changed": lc, "last_updated": lu}]


# ---------------------------------------------------------------------------
# (a) 모션 on → 발화
# ---------------------------------------------------------------------------
async def test_motion_on_fires(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_motion_rule())
    engine.reload_rule(saved["id"])

    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await asyncio.sleep(0.05)

    assert ha.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    assert [e["result"] for e in rl.entries()] == ["fired"]
    await engine.stop()


async def test_no_edge_no_fire(v2_data_dir):
    # old==new (상태값 동일) 이벤트는 트리거 평가를 건너뛴다
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "on")))
    await engine.start(src)
    saved = rs.upsert(_motion_rule())
    engine.reload_rule(saved["id"])

    src.inject("binary_sensor.living_room_motion", "on", old_state="on")
    await asyncio.sleep(0.03)

    assert ha.calls == []
    await engine.stop()


# ---------------------------------------------------------------------------
# (b) state_held
# ---------------------------------------------------------------------------
async def test_held_fires_after_duration(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_held_rule(seconds=0.05))
    engine.reload_rule(saved["id"])

    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    assert engine.status()["active_timers"] == 1  # for 타이머 무장
    await asyncio.sleep(0.12)

    assert ha.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    assert [e["result"] for e in rl.entries()] == ["fired"]
    await engine.stop()


async def test_held_broken_before_duration_no_fire(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_held_rule(seconds=0.1))
    engine.reload_rule(saved["id"])

    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await asyncio.sleep(0.03)
    src.inject("binary_sensor.living_room_motion", "off", old_state="on")  # 도중 깨짐 → 타이머 취소
    assert engine.status()["active_timers"] == 0
    await asyncio.sleep(0.12)

    assert ha.calls == []
    assert all(e["result"] != "fired" for e in rl.entries())
    await engine.stop()


# ---------------------------------------------------------------------------
# (c) 조건 불충족 → skipped_condition
# ---------------------------------------------------------------------------
async def test_condition_not_met_skips(v2_data_dir):
    # 트리거는 발생하지만 time_segment 조건이 현재 시간대와 불일치
    now_night = lambda: datetime(2026, 7, 16, 23, 0, 0)
    engine, rs, rl, ha = await _build(v2_data_dir, now_fn=now_night)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    rule = _motion_rule()
    rule["model"]["conditions"] = [{"type": "time_segment", "segments": ["morning"]}]
    saved = rs.upsert(rule)
    engine.reload_rule(saved["id"])

    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await asyncio.sleep(0.05)

    assert ha.calls == []
    results = [(e["result"], e["detail"]) for e in rl.entries()]
    assert ("skipped_condition", "조건 불충족") in results
    await engine.stop()


# ---------------------------------------------------------------------------
# (d) 오류 3회 → auto_disabled
# ---------------------------------------------------------------------------
async def test_three_errors_auto_disable(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir, fail=True)
    src = FakeSource([])
    await engine.start(src)
    saved = rs.upsert(_motion_rule())
    engine.reload_rule(saved["id"])

    # fire_rule 은 쿨다운/조건을 우회하므로 오류를 3회 누적시킬 수 있다
    for _ in range(3):
        await engine.fire_rule(saved, "run")

    meta = rs.get(saved["id"])["meta"]
    assert meta["auto_disabled"] is True
    assert meta["last_error"]
    assert [e["result"] for e in rl.entries()] == ["error", "error", "error"]
    # auto_disabled 규칙은 라우팅에서 제거됨
    src2 = FakeSource([])
    assert saved["id"] not in engine._index.get("binary_sensor.living_room_motion", set())
    await engine.stop()


# ---------------------------------------------------------------------------
# (e) 시간대 경계 재평가 → segment 트리거 발화
# ---------------------------------------------------------------------------
async def test_segment_boundary_fires(v2_data_dir):
    now_night = lambda: datetime(2026, 7, 16, 21, 0, 30)  # night 진입 직후
    engine, rs, rl, ha = await _build(v2_data_dir, now_fn=now_night)
    src = FakeSource([])
    await engine.start(src)
    rule = {"sentence": "밤 되면 소등", "model": {
        "triggers": [{"type": "segment", "to": "night"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_off",
                     "target": {"entity_id": ["light.living_room_main"]}}]}}
    saved = rs.upsert(rule)
    engine.reload_rule(saved["id"])

    engine._on_boundary()  # 경계 도달 콜백(벽시계상 night)
    await asyncio.sleep(0.03)

    assert ha.calls == [("light", "turn_off", {"entity_id": ["light.living_room_main"]})]
    assert [e["result"] for e in rl.entries()] == ["fired"]
    await engine.stop()


async def test_segment_boundary_wrong_segment_no_fire(v2_data_dir):
    now_day = lambda: datetime(2026, 7, 16, 9, 0, 30)  # day
    engine, rs, rl, ha = await _build(v2_data_dir, now_fn=now_day)
    src = FakeSource([])
    await engine.start(src)
    rule = {"sentence": "밤 되면 소등", "model": {
        "triggers": [{"type": "segment", "to": "night"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_off",
                     "target": {"entity_id": ["light.living_room_main"]}}]}}
    saved = rs.upsert(rule)
    engine.reload_rule(saved["id"])

    engine._on_boundary()
    await asyncio.sleep(0.03)

    assert ha.calls == []
    await engine.stop()


# ---------------------------------------------------------------------------
# (f) pending timer 저장/복원
# ---------------------------------------------------------------------------
async def test_pending_timer_save_and_restore(v2_data_dir):
    loop = asyncio.get_running_loop()
    base = datetime(2026, 7, 16, 12, 0, 0)
    # 규칙 저장소는 재기동 간 디스크로 유지되므로 두 엔진이 공유(재기동 모사)
    shared_rules = RuleStore(JsonStore(v2_data_dir / "rules.json", [], loop=loop))

    # --- 1차 엔진: 긴 for(100s) 무장 후 stop → pending_timers.json 저장 ---
    engine1, _, _, _ = await _build(v2_data_dir, now_fn=lambda: base, rule_store=shared_rules)
    src1 = FakeSource(_seed(("binary_sensor.living_room_motion", "on")))
    await engine1.start(src1)
    saved = shared_rules.upsert(_held_rule(seconds=100))
    engine1.reload_rule(saved["id"])
    src1.inject("binary_sensor.living_room_motion", "on", old_state="on")
    # on→on 은 edge 아님 → held 재평가 유도를 위해 off→on 주입
    src1.inject("binary_sensor.living_room_motion", "on", old_state="off")
    assert engine1.status()["active_timers"] == 1
    await engine1.stop()

    pend = json.loads((v2_data_dir / "pending_timers.json").read_text())
    assert len(pend) == 1
    assert pend[0]["rule_id"] == saved["id"]

    # --- 2차 엔진: 시계를 만료 이후로 전진 → 복원 시 즉시 발화(조건 여전히 on) ---
    now2 = lambda: base + timedelta(seconds=200)
    runlog2 = RunLog(JsonStore(v2_data_dir / "runlog.json", [], loop=loop))
    engine2, _, _, ha2 = await _build(v2_data_dir, now_fn=now2, rule_store=shared_rules,
                                      runlog=runlog2)
    src2 = FakeSource(_seed(("binary_sensor.living_room_motion", "on")))
    await engine2.start(src2)
    await asyncio.sleep(0.05)

    assert ha2.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    assert [e["result"] for e in runlog2.entries()] == ["fired"]
    await engine2.stop()


# ---------------------------------------------------------------------------
# status 스냅샷
# ---------------------------------------------------------------------------
async def test_status_snapshot(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir, now_fn=lambda: datetime(2026, 7, 16, 22, 0))
    src = FakeSource([])
    await engine.start(src)
    saved = rs.upsert(_motion_rule())
    engine.reload_rule(saved["id"])

    st = engine.status()
    assert st["connected"] is True
    assert st["rules"] == 1
    assert st["vars"]["segment"] == "night"
    await engine.stop()


# ===========================================================================
# 적대적 검증으로 확정된 결함 회귀 테스트 (fix 1~8)
# ===========================================================================

# --- fix 1: 만료 pending 복원이 첫 resync 이후에 발화 -----------------------
async def test_pending_expired_fires_after_first_resync(v2_data_dir):
    """HA 형(start 후 지연 resync)에서 만료 pending 이 유실되지 않고 첫 resync 후 발화."""
    loop = asyncio.get_running_loop()
    base = datetime(2026, 7, 16, 12, 0, 0)
    shared_rules = RuleStore(JsonStore(v2_data_dir / "rules.json", [], loop=loop))

    # 1차 엔진: 긴 for(100s) 무장 후 stop → pending 저장
    engine1, _, _, _ = await _build(v2_data_dir, now_fn=lambda: base, rule_store=shared_rules)
    src1 = FakeSource(_seed(("binary_sensor.living_room_motion", "on")))
    await engine1.start(src1)
    saved = shared_rules.upsert(_held_rule(seconds=100))
    engine1.reload_rule(saved["id"])
    src1.inject("binary_sensor.living_room_motion", "on", old_state="off")
    assert engine1.status()["active_timers"] == 1
    await engine1.stop()

    # 2차 엔진: DeferredSource — start 직후엔 캐시가 비어 있다(HA 재현)
    now2 = lambda: base + timedelta(seconds=200)  # 만료 경과
    runlog2 = RunLog(JsonStore(v2_data_dir / "runlog.json", [], loop=loop))
    engine2, _, _, ha2 = await _build(v2_data_dir, now_fn=now2, rule_store=shared_rules,
                                      runlog=runlog2)
    src2 = DeferredSource()
    await engine2.start(src2)
    # 아직 resync 전 → 발화하지 않고 보관 중
    assert ha2.calls == []
    assert engine2._pending_expired == [(saved["id"], 0)]
    # 첫 resync 도착(캐시 채워짐) → 조건(상태==on) 재확인 후 발화
    src2.resync(_seed(("binary_sensor.living_room_motion", "on")))
    await asyncio.sleep(0.05)
    assert ha2.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    assert [e["result"] for e in runlog2.entries()] == ["fired"]
    await engine2.stop()


async def test_pending_expired_no_fire_if_condition_broken(v2_data_dir):
    """만료 pending 이라도 첫 resync 스냅샷에서 상태가 to 가 아니면 발화하지 않는다."""
    loop = asyncio.get_running_loop()
    base = datetime(2026, 7, 16, 12, 0, 0)
    shared_rules = RuleStore(JsonStore(v2_data_dir / "rules.json", [], loop=loop))

    engine1, _, _, _ = await _build(v2_data_dir, now_fn=lambda: base, rule_store=shared_rules)
    src1 = FakeSource(_seed(("binary_sensor.living_room_motion", "on")))
    await engine1.start(src1)
    saved = shared_rules.upsert(_held_rule(seconds=100))
    engine1.reload_rule(saved["id"])
    src1.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await engine1.stop()

    now2 = lambda: base + timedelta(seconds=200)
    runlog2 = RunLog(JsonStore(v2_data_dir / "runlog.json", [], loop=loop))
    engine2, _, _, ha2 = await _build(v2_data_dir, now_fn=now2, rule_store=shared_rules,
                                      runlog=runlog2)
    src2 = DeferredSource()
    await engine2.start(src2)
    # resync 스냅샷: 모션이 off → 조건 깨짐 → 발화 안 함
    src2.resync(_seed(("binary_sensor.living_room_motion", "off")))
    await asyncio.sleep(0.05)
    assert ha2.calls == []
    await engine2.stop()


# --- fix 2: 재연결 resync 시 유지 연속성 검증(토글로 깨진 유지 → 재장전) ------
async def test_reconnect_toggle_rearms_and_prevents_early_fire(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    loop = asyncio.get_running_loop()
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_held_rule(seconds=0.2))
    engine.reload_rule(saved["id"])
    key = (saved["id"], 0)

    # 유지 시작 → 0.2s 타이머 무장
    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    assert key in engine._for_timers
    await asyncio.sleep(0.12)  # 원래 만료(0.2s)의 절반 이상 경과

    # 재연결: 단절 중 토글로 유지가 '방금' 재시작된 스냅샷(last_changed 신선)
    engine._on_resync(_snap("binary_sensor.living_room_motion", "on", changed_ago=0.0))
    # 재장전되어 잔여시간이 원래 임박(≈0.08)이 아니라 ≈0.2 로 늘어남
    remaining = engine._for_timers[key].when() - loop.time()
    assert remaining > 0.15

    # 원래 만료 시점을 지나도(누적 ≈0.24) 재장전 덕분에 아직 발화 안 함(오발화 없음)
    await asyncio.sleep(0.12)
    assert ha.calls == []
    # 새 만료까지 대기 → 정상 발화
    await asyncio.sleep(0.15)
    assert ha.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    await engine.stop()


async def test_reconnect_completed_hold_does_not_fire(v2_data_dir):
    """단절 중 유지가 완료된 것으로 보이면(재연결 발화 금지) 타이머를 취소하고 발화하지 않는다."""
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_held_rule(seconds=0.2))
    engine.reload_rule(saved["id"])
    key = (saved["id"], 0)

    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    assert key in engine._for_timers
    # 재연결: 상태는 on 이지만 last_changed 가 5초 전 → 유지가 이미 완료된 상태
    engine._on_resync(_snap("binary_sensor.living_room_motion", "on", changed_ago=5.0))
    assert key not in engine._for_timers  # 취소됨(자동 발화 금지)
    await asyncio.sleep(0.3)
    assert ha.calls == []
    await engine.stop()


# --- fix 3: resync/기동 시 이미 to 인 held 트리거의 타이머 장전 --------------
async def test_resync_arms_held_when_already_to(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_held_rule(seconds=0.2))
    engine.reload_rule(saved["id"])
    key = (saved["id"], 0)
    assert key not in engine._for_timers  # 아직 이벤트 없음 → 타이머 없음

    # resync: 모션이 이미 on, 방금 진입 → 잔여시간(≈for)으로 arm
    engine._on_resync(_snap("binary_sensor.living_room_motion", "on", changed_ago=0.0))
    assert key in engine._for_timers
    await asyncio.sleep(0.3)
    assert ha.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    await engine.stop()


async def test_resync_no_arm_when_for_already_exceeded(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_held_rule(seconds=0.2))
    engine.reload_rule(saved["id"])
    key = (saved["id"], 0)

    # resync: 모션 on 이지만 5초 전부터 유지(=for 0.2s 초과) → arm 안 함, 발화 안 함
    engine._on_resync(_snap("binary_sensor.living_room_motion", "on",
                            changed_ago=5.0, updated_ago=0.0))
    assert key not in engine._for_timers
    await asyncio.sleep(0.1)
    assert ha.calls == []
    await engine.stop()


# --- fix 4: 진행 중 실행이 규칙 삭제/수정 시 취소된다 ------------------------
async def test_delay_action_cancelled_on_rule_removed(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_delay_rule(delay_seconds=0.3))
    engine.reload_rule(saved["id"])

    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await asyncio.sleep(0.05)  # 첫 액션(turn_on) 실행 후 delay 진행 중
    assert ha.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]

    # 규칙 삭제 → api_v2 delete 흐름과 동일하게 reload_rule → 진행 중 실행 취소
    rs.delete(saved["id"])
    engine.reload_rule(saved["id"])
    await asyncio.sleep(0.4)  # delay 가 끝났을 시간
    # 남은 turn_off 는 실행되지 않는다
    assert ha.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    # 취소된 실행은 fired 로그를 남기지 않는다
    assert all(e["result"] != "fired" for e in rl.entries())
    await engine.stop()


async def test_delay_action_cancelled_on_rule_toggle_off(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))
    await engine.start(src)
    saved = rs.upsert(_delay_rule(delay_seconds=0.3))
    engine.reload_rule(saved["id"])

    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await asyncio.sleep(0.05)
    assert ha.calls[-1] == ("light", "turn_on", {"entity_id": ["light.living_room_main"]})

    rs.set_enabled(saved["id"], False)  # 비활성 → reload 시 unindex + 실행 취소
    engine.reload_rule(saved["id"])
    await asyncio.sleep(0.4)
    assert ha.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    await engine.stop()


# --- fix 5: 세그먼트 전환이 없으면 재발화하지 않는다 -------------------------
async def test_boundary_no_refire_when_segment_unchanged(v2_data_dir):
    now_night = lambda: datetime(2026, 7, 16, 21, 0, 30)  # night
    engine, rs, rl, ha = await _build(v2_data_dir, now_fn=now_night)
    src = FakeSource([])
    await engine.start(src)
    rule = {"sentence": "밤 되면 소등", "model": {
        "triggers": [{"type": "segment", "to": "night"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_off",
                     "target": {"entity_id": ["light.living_room_main"]}}]}}
    saved = rs.upsert(rule)
    engine.reload_rule(saved["id"])

    engine._on_boundary()  # night 진입 → 1회 발화
    await asyncio.sleep(0.03)
    engine._last_fired.clear()  # 쿨다운 제거해 '전환 검사'(fix 5)만 격리 검증
    engine._on_boundary()  # 같은 세그먼트 재호출 → 전환 없음 → 재발화 금지
    await asyncio.sleep(0.03)

    assert ha.calls == [("light", "turn_off", {"entity_id": ["light.living_room_main"]})]
    await engine.stop()


# --- fix 6: 경계에서 time_segment 조건 재평가 -------------------------------
async def test_boundary_reevaluates_time_segment_condition(v2_data_dir):
    now_night = lambda: datetime(2026, 7, 16, 21, 0, 30)  # night
    engine, rs, rl, ha = await _build(v2_data_dir, now_fn=now_night)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "on")))  # 이미 모션 on
    await engine.start(src)
    rule = _motion_rule()
    rule["model"]["conditions"] = [{"type": "time_segment", "segments": ["night"]}]
    saved = rs.upsert(rule)
    engine.reload_rule(saved["id"])

    # 트리거(모션)는 현재 성립 중. night 진입 순간 조건이 참이 되어 발화해야 한다.
    engine._on_boundary()
    await asyncio.sleep(0.05)
    assert ha.calls == [("light", "turn_on", {"entity_id": ["light.living_room_main"]})]
    await engine.stop()


async def test_boundary_time_segment_not_fired_when_trigger_not_true(v2_data_dir):
    now_night = lambda: datetime(2026, 7, 16, 21, 0, 30)
    engine, rs, rl, ha = await _build(v2_data_dir, now_fn=now_night)
    src = FakeSource(_seed(("binary_sensor.living_room_motion", "off")))  # 모션 off
    await engine.start(src)
    rule = _motion_rule()
    rule["model"]["conditions"] = [{"type": "time_segment", "segments": ["night"]}]
    saved = rs.upsert(rule)
    engine.reload_rule(saved["id"])

    engine._on_boundary()  # 트리거가 현재 성립 중이 아니므로 발화 안 함
    await asyncio.sleep(0.05)
    assert ha.calls == []
    await engine.stop()


# --- fix 7: 경계 타이머 재스케줄 공개 메서드 -------------------------------
async def test_reschedule_boundary_replaces_timer(v2_data_dir):
    engine, rs, rl, ha = await _build(
        v2_data_dir, now_fn=lambda: datetime(2026, 7, 16, 10, 0, 0))
    src = FakeSource([])
    await engine.start(src)
    first = engine._boundary_timer
    assert first is not None

    engine.reschedule_boundary()
    second = engine._boundary_timer
    assert second is not None and second is not first  # 새 타이머로 교체
    assert first.cancelled()                            # 이전 타이머는 취소됨
    await engine.stop()


# --- fix 8: connected 가 연결 통지로만 갱신된다 ----------------------------
async def test_not_connected_until_resync_with_deferred_source(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = DeferredSource()
    await engine.start(src)
    assert engine.status()["connected"] is False  # start 직후엔 아직 아님(HA 형)
    src.resync([])                                 # 연결/스냅샷 도착
    assert engine.status()["connected"] is True
    await engine.stop()


async def test_connected_toggles_on_disconnect(v2_data_dir):
    engine, rs, rl, ha = await _build(v2_data_dir)
    src = FakeSource([])
    await engine.start(src)
    assert engine.status()["connected"] is True  # on_connect/resync 로 연결됨
    src.drop()                                     # 단절 통지
    assert engine.status()["connected"] is False
    src.reconnect([])                              # 재연결 통지
    assert engine.status()["connected"] is True
    await engine.stop()
    assert engine.status()["connected"] is False
