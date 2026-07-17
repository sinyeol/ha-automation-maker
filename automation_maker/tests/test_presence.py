"""APP-PORT-PLAN S5 (프레즌스 축) 결정적 테스트.

  - 검증기: presence_agg 트리거(first/last/any/all)·조건(none/any/all) 정상·경계·오류(§3).
  - ha_map: presence 트리거/조건 → HA v1 방언 왕복(§2.6·§5).
  - evaluator: 집 인원 레벨 none/any/all 순수 평가(§2.5).
  - 엔진: 집 인원 에지(first/last/any/all)·for 타이머(arm/취소/발화)·
          재시작(pending) 복원·재연결(resync) 복원·재시작/재연결 자동 발화 0(§2.4 게이트4).
  - 파서/postpass 통합: presence_agg 방출 + savable 승급(#23).

엔진 테스트는 now_fn 고정 + FakeSource.inject 로 결정적이며, for 타이머는 짧은 실시간
지속시간(0.05~0.3s)으로 발화/취소/복원 불변식을 고정한다.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

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
from backend.engine.variables import GlobalVars

_PRESENCE_INV = {"entities": [
    {"entity_id": "person.user", "domain": "person"},
    {"entity_id": "person.wife", "domain": "person"},
]}
_TWO = ["person.user", "person.wife"]


# ===========================================================================
# 검증기 (rule_model) — §3
# ===========================================================================
def _model(triggers, conditions=()):
    return {"subrules": [{"triggers": list(triggers), "conditions": list(conditions),
                          "actions": [{"type": "service", "action": "light.turn_on"}]}]}


_INV = {"entities": [{"entity_id": "person.user"}, {"entity_id": "person.wife"}]}


def test_validate_presence_trigger_quants_ok():
    for q in ("first", "last", "any", "all"):
        assert validate_rule_model(_model([{"type": "presence_agg", "quant": q}]), _INV) == [], q


def test_validate_presence_trigger_bad_quant():
    # 조건 전용 quant(none)를 트리거에 쓰면 거부.
    errs = validate_rule_model(_model([{"type": "presence_agg", "quant": "none"}]), _INV)
    assert any(".quant" in e["path"] for e in errs)


def test_validate_presence_persons_existence_and_prefix():
    # 실존 person.* → 통과.
    assert validate_rule_model(
        _model([{"type": "presence_agg", "quant": "any", "persons": _TWO}]), _INV) == []
    # 비실존 person.
    errs = validate_rule_model(
        _model([{"type": "presence_agg", "quant": "any",
                 "persons": ["person.ghost"]}]), _INV)
    assert any(".persons" in e["path"] for e in errs)
    # person. 접두 아님.
    errs2 = validate_rule_model(
        _model([{"type": "presence_agg", "quant": "any",
                 "persons": ["light.kitchen"]}]), _INV)
    assert any(".persons" in e["path"] for e in errs2)
    # 빈 목록.
    errs3 = validate_rule_model(
        _model([{"type": "presence_agg", "quant": "any", "persons": []}]), _INV)
    assert any(".persons" in e["path"] for e in errs3)


def test_validate_presence_for_only_last_all():
    # last/all + for → 통과.
    for q in ("last", "all"):
        assert validate_rule_model(
            _model([{"type": "presence_agg", "quant": q,
                     "for": {"minutes": 10}}]), _INV) == [], q
    # first/any + for → 오류.
    for q in ("first", "any"):
        errs = validate_rule_model(
            _model([{"type": "presence_agg", "quant": q,
                     "for": {"minutes": 10}}]), _INV)
        assert any(".for" in e["path"] for e in errs), q


def test_validate_presence_condition_quants():
    for q in ("none", "any", "all"):
        m = _model([{"type": "daily", "at": "07:00"}],
                   [{"type": "presence_agg", "quant": q}])
        assert validate_rule_model(m, _INV) == [], q
    # 트리거 전용 quant(first)를 조건에 → 거부.
    m2 = _model([{"type": "daily", "at": "07:00"}],
                [{"type": "presence_agg", "quant": "first"}])
    assert any(".quant" in e["path"] for e in validate_rule_model(m2, _INV))
    # 조건 + for → 거부(조건엔 for 불허).
    m3 = _model([{"type": "daily", "at": "07:00"}],
                [{"type": "presence_agg", "quant": "all", "for": {"minutes": 5}}])
    assert any(".for" in e["path"] for e in validate_rule_model(m3, _INV))


def test_validate_presence_persons_omitted_ok():
    """persons 생략 → 전체 person.* 로 통과(검증에서 필수 아님)."""
    assert validate_rule_model(_model([{"type": "presence_agg", "quant": "first"}]), _INV) == []


# ===========================================================================
# ha_map (§2.6·§5) — v2 → HA v1 방언 왕복
# ===========================================================================
def test_ha_map_presence_first_last_zone_home():
    out = subrule_to_automation(
        {"triggers": [{"type": "presence_agg", "quant": "first"}],
         "conditions": [], "actions": []})
    assert out["triggers"] == [{"type": "numeric_state", "entity_id": "zone.home", "above": 0}]
    assert _build_trigger(out["triggers"][0]) == {
        "trigger": "numeric_state", "entity_id": "zone.home", "above": 0}
    out2 = subrule_to_automation(
        {"triggers": [{"type": "presence_agg", "quant": "last",
                       "for": {"minutes": 5}}], "conditions": [], "actions": []})
    assert out2["triggers"] == [{"type": "numeric_state", "entity_id": "zone.home",
                                 "below": 1, "for": {"minutes": 5}}]
    assert out["warnings"] == [] and out2["warnings"] == []


def test_ha_map_presence_any_all_person_state():
    # any → person 별 state to home.
    out = subrule_to_automation(
        {"triggers": [{"type": "presence_agg", "quant": "any", "persons": _TWO}],
         "conditions": [], "actions": []})
    assert out["triggers"] == [
        {"type": "state", "entity_id": "person.user", "to": "home"},
        {"type": "state", "entity_id": "person.wife", "to": "home"}]
    assert out["conditions"] == []
    # all → person 별 state 트리거 + 전원 home 조건 병기.
    out2 = subrule_to_automation(
        {"triggers": [{"type": "presence_agg", "quant": "all", "persons": _TWO}],
         "conditions": [], "actions": []})
    assert out2["triggers"] == [
        {"type": "state", "entity_id": "person.user", "to": "home"},
        {"type": "state", "entity_id": "person.wife", "to": "home"}]
    assert out2["conditions"] == [
        {"type": "state", "entity_id": "person.user", "state": "home"},
        {"type": "state", "entity_id": "person.wife", "state": "home"}]


def test_ha_map_presence_any_all_persons_from_inventory():
    """persons 생략 → 인벤토리 person.* 로 확장."""
    out = subrule_to_automation(
        {"triggers": [{"type": "presence_agg", "quant": "any"}],
         "conditions": [], "actions": []}, inventory=_PRESENCE_INV)
    assert [t["entity_id"] for t in out["triggers"]] == _TWO


def test_ha_map_presence_condition_none_any_all():
    none_out = subrule_to_automation(
        {"triggers": [], "conditions": [{"type": "presence_agg", "quant": "none"}],
         "actions": []})
    assert none_out["conditions"] == [
        {"type": "numeric_state", "entity_id": "zone.home", "below": 1}]
    any_out = subrule_to_automation(
        {"triggers": [], "conditions": [{"type": "presence_agg", "quant": "any"}],
         "actions": []})
    assert any_out["conditions"] == [
        {"type": "numeric_state", "entity_id": "zone.home", "above": 0}]
    all_out = subrule_to_automation(
        {"triggers": [], "conditions": [{"type": "presence_agg", "quant": "all",
                                         "persons": _TWO}], "actions": []})
    assert all_out["conditions"] == [{"type": "and", "conditions": [
        {"type": "state", "entity_id": "person.user", "state": "home"},
        {"type": "state", "entity_id": "person.wife", "state": "home"}]}]
    # v1 빌더가 소비 가능(and/state 왕복).
    assert _build_condition(all_out["conditions"][0]) == {"condition": "and", "conditions": [
        {"condition": "state", "entity_id": "person.user", "state": "home"},
        {"condition": "state", "entity_id": "person.wife", "state": "home"}]}


# ===========================================================================
# evaluator 집 인원 레벨 (§2.5) — 순수 단위
# ===========================================================================
class _FakeCache:
    def __init__(self, states):
        self._s = {k: {"state": v} for k, v in states.items()}

    def get(self, eid):
        return self._s.get(eid)


def _pctx(states, inventory=None):
    return EvalContext(cache=_FakeCache(states), gvars=None,
                       now_fn=lambda: datetime(2026, 7, 16, 12, 0),
                       inventory_fn=lambda: (inventory or _PRESENCE_INV))


def _pc(quant, persons=None):
    node = {"type": "presence_agg", "quant": quant}
    if persons is not None:
        node["persons"] = persons
    return node


def test_eval_presence_level_none():
    ctx = _pctx({"person.user": "not_home", "person.wife": "not_home"})
    assert evaluate_condition(_pc("none", _TWO), ctx) is True
    assert evaluate_condition(_pc("any", _TWO), ctx) is False
    assert evaluate_condition(_pc("all", _TWO), ctx) is False


def test_eval_presence_level_any():
    ctx = _pctx({"person.user": "home", "person.wife": "not_home"})
    assert evaluate_condition(_pc("none", _TWO), ctx) is False
    assert evaluate_condition(_pc("any", _TWO), ctx) is True
    assert evaluate_condition(_pc("all", _TWO), ctx) is False


def test_eval_presence_level_all():
    ctx = _pctx({"person.user": "home", "person.wife": "home"})
    assert evaluate_condition(_pc("all", _TWO), ctx) is True
    assert evaluate_condition(_pc("any", _TWO), ctx) is True
    assert evaluate_condition(_pc("none", _TWO), ctx) is False


def test_eval_presence_persons_omitted_uses_inventory():
    ctx = _pctx({"person.user": "home", "person.wife": "home"})
    assert evaluate_condition(_pc("all"), ctx) is True  # persons 생략 → 인벤토리 2명


def test_eval_presence_no_persons_false():
    """persons 못 구함(빈 인벤토리) → vacuous truth 방지로 False."""
    ctx = _pctx({}, inventory={"entities": []})
    for q in ("none", "any", "all"):
        assert evaluate_condition(_pc(q), ctx) is False, q


# ===========================================================================
# 엔진 집 인원 에지 (§2.4) — 결정적
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

    def inject(self, entity_id, new_state, old_state):
        old = {"entity_id": entity_id, "state": old_state, "attributes": {}}
        new = {"entity_id": entity_id, "state": new_state, "attributes": {}}
        self._on_event(entity_id, old, new)

    def resync(self, states):
        if self._on_connect:
            self._on_connect()
        self._on_resync(states)


class _RecordingHA:
    def __init__(self):
        self.calls = []

    async def call_service(self, domain, service, data):
        self.calls.append((domain, service, dict(data or {})))


def _seed(*pairs):
    return [{"entity_id": eid, "state": st, "attributes": {}} for eid, st in pairs]


def _snap(states, changed_ago=0.0):
    now = datetime.now(timezone.utc)
    lc = (now - timedelta(seconds=changed_ago)).isoformat()
    return [{"entity_id": eid, "state": st, "attributes": {},
             "last_changed": lc, "last_updated": lc} for eid, st in states.items()]


def _presence_rule(quant, persons=_TWO, for_dur=None):
    node = {"type": "presence_agg", "quant": quant}
    if persons is not None:
        node["persons"] = persons
    if for_dur is not None:
        node["for"] = for_dur
    return {"sentence": f"presence {quant}", "model": {
        "triggers": [node], "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.living_room_main"]}}]}}


async def _build_presence(data_dir, now_fn=None, rule_store=None, runlog=None,
                          inventory=None):
    loop = asyncio.get_running_loop()
    nf = now_fn or (lambda: datetime(2026, 7, 16, 12, 0, 0))
    rs = rule_store or RuleStore(JsonStore(data_dir / "rules.json", [], loop=loop))
    rl = runlog or RunLog(JsonStore(data_dir / "runlog.json", [], loop=loop))
    gvars = GlobalVars({}, now_fn=nf)
    ha = _RecordingHA()
    inv = inventory if inventory is not None else _PRESENCE_INV
    engine = RuleEngine(rs, StateCache(), gvars, ha, lambda: inv, rl,
                        now_fn=nf, loop=loop)
    return engine, rs, rl, ha


_FIRE = ("light", "turn_on", {"entity_id": ["light.living_room_main"]})


async def _start_rule(engine, rs, rule, seed):
    src = _FakeSource(_seed(*seed))
    await engine.start(src)
    saved = rs.upsert(dict(rule))
    engine.reload_rule(saved["id"])
    return src, saved


@pytest.mark.asyncio
async def test_presence_first_edge_fires(v2_data_dir):
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(engine, rs, _presence_rule("first"),
                               [("person.user", "not_home"), ("person.wife", "not_home")])
    src.inject("person.wife", "home", old_state="not_home")  # 0→1
    await asyncio.sleep(0.05)
    assert ha.calls == [_FIRE]
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_first_no_fire_on_second_arrival(v2_data_dir):
    """first 는 0→≥1 만. 1→2(이미 한 명 있음)엔 발화하지 않는다."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(engine, rs, _presence_rule("first"),
                               [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.wife", "home", old_state="not_home")  # 1→2
    await asyncio.sleep(0.05)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_any_fires_on_each_increase(v2_data_dir):
    """any 는 count 증가(1→2 포함)면 발화한다(first 와의 구분)."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(engine, rs, _presence_rule("any"),
                               [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.wife", "home", old_state="not_home")  # 1→2
    await asyncio.sleep(0.05)
    assert ha.calls == [_FIRE]
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_last_edge_fires(v2_data_dir):
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(engine, rs, _presence_rule("last"),
                               [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.user", "not_home", old_state="home")  # 1→0
    await asyncio.sleep(0.05)
    assert ha.calls == [_FIRE]
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_last_no_fire_when_someone_home(v2_data_dir):
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(engine, rs, _presence_rule("last"),
                               [("person.user", "home"), ("person.wife", "home")])
    src.inject("person.user", "not_home", old_state="home")  # 2→1 (아직 1명 집)
    await asyncio.sleep(0.05)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_all_edge_fires(v2_data_dir):
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(engine, rs, _presence_rule("all"),
                               [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.wife", "home", old_state="not_home")  # 1→2 == len
    await asyncio.sleep(0.05)
    assert ha.calls == [_FIRE]
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_all_no_fire_until_everyone(v2_data_dir):
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(engine, rs, _presence_rule("all"),
                               [("person.user", "not_home"), ("person.wife", "not_home")])
    src.inject("person.wife", "home", old_state="not_home")  # 0→1 (아직 전원 아님)
    await asyncio.sleep(0.05)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_persons_omitted_uses_inventory(v2_data_dir):
    """persons 생략 → 인벤토리 person.* 로 인덱싱·에지 판정."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(engine, rs, _presence_rule("first", persons=None),
                               [("person.user", "not_home"), ("person.wife", "not_home")])
    src.inject("person.user", "home", old_state="not_home")
    await asyncio.sleep(0.05)
    assert ha.calls == [_FIRE]
    await engine.stop()


# ===========================================================================
# for 타이머 (§2.4) — arm / 취소 / 발화
# ===========================================================================
@pytest.mark.asyncio
async def test_presence_last_for_fires_after_duration(v2_data_dir):
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 0.05}),
        [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.user", "not_home", old_state="home")  # 무인 → 타이머 무장
    assert engine.status()["active_timers"] == 1
    await asyncio.sleep(0.12)
    assert ha.calls == [_FIRE]
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_last_for_cancelled_on_return(v2_data_dir):
    """무인 유지 중 귀가 → 타이머 취소, 발화 없음."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 0.2}),
        [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.user", "not_home", old_state="home")  # 무인 → arm
    assert engine.status()["active_timers"] == 1
    src.inject("person.user", "home", old_state="not_home")  # 중도 귀가 → 취소
    assert engine.status()["active_timers"] == 0
    await asyncio.sleep(0.3)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_all_for_fires_and_cancel(v2_data_dir):
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(
        engine, rs, _presence_rule("all", for_dur={"seconds": 0.2}),
        [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.wife", "home", old_state="not_home")  # 전원 재실 → arm
    assert engine.status()["active_timers"] == 1
    src.inject("person.wife", "not_home", old_state="home")  # 한 명 외출 → 취소
    assert engine.status()["active_timers"] == 0
    await asyncio.sleep(0.3)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_last_for_hold_not_reset_by_noise(v2_data_dir):
    """무인 유지 중 이미 not_home 인 사람의 다른 상태 변화가 타이머를 리셋하지 않는다."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, _ = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 0.15}),
        [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.user", "not_home", old_state="home")  # 무인 → arm
    handle1 = engine._for_timers[(list(engine._rules)[0], 0)]
    await asyncio.sleep(0.05)
    # 이미 집 밖인 wife 가 not_home→work 로 바뀌어도 여전히 무인(count 0) — 재설정 금지.
    src.inject("person.wife", "work", old_state="not_home")
    handle2 = engine._for_timers[(list(engine._rules)[0], 0)]
    assert handle2 is handle1  # 동일 타이머(리셋 안 됨)
    await asyncio.sleep(0.15)
    assert ha.calls == [_FIRE]
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_last_for_no_refire_on_away_transition(v2_data_dir):
    """결함2: 무인 유지 발화 후 '연속 유지 구간'에서 외출자 상태 전이(not_home→work,
    홈카운트 0 불변)만으로는 재무장·재발화하지 않는다(발화 래치). 붕괴(귀가) 후 재유지
    시에만 다시 발화한다. 재현: repro_presence.py 시나리오.
    """
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, saved = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 0.05}),
        [("person.user", "home"), ("person.wife", "not_home")])
    key = (saved["id"], 0)

    # 마지막 사람 나감 → 무인 → 타이머 무장 → 만료 후 1회 발화, 발화 래치 설정.
    src.inject("person.user", "not_home", old_state="home")
    assert engine.status()["active_timers"] == 1
    await asyncio.sleep(0.12)
    assert ha.calls == [_FIRE]
    assert engine.status()["active_timers"] == 0    # 발화 후 타이머 pop
    assert key in engine._held_fired                # 이 유지 구간 발화 래치

    # 쿨다운(_COOLDOWN=5s)을 비워, '재무장이 있었다면 재발화했을' 조건을 만든다.
    # 그래도 래치 때문에 재무장 자체가 안 일어나 재발화 0 이어야 한다(결함2 핵심).
    engine._last_fired.clear()

    # 외출자 not_home→work (홈카운트 0 불변) → 재무장 금지.
    src.inject("person.wife", "work", old_state="not_home")
    assert engine.status()["active_timers"] == 0    # 재무장 안 됨(결함2 수정 전엔 1)
    src.inject("person.user", "work", old_state="not_home")
    assert engine.status()["active_timers"] == 0
    await asyncio.sleep(0.12)
    assert ha.calls == [_FIRE]                       # 여전히 1회(재발화 없음)

    # 붕괴: 한 명 귀가(count 0→1) → 래치 해제.
    src.inject("person.user", "home", old_state="work")
    assert key not in engine._held_fired
    # 재유지: 다시 무인(count 1→0) → 재무장 → 재발화 가능.
    src.inject("person.user", "not_home", old_state="home")
    assert engine.status()["active_timers"] == 1
    await asyncio.sleep(0.12)
    assert ha.calls == [_FIRE, _FIRE]                # 붕괴 후 재유지로 재발화
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_all_for_latch_set_and_refire_after_collapse(v2_data_dir):
    """결함2(all 축): 전원 재실 유지 발화 시 래치가 설정되고, 붕괴(한 명 외출) 시 해제돼
    재유지(전원 재실) 시 다시 발화한다(붕괴 후 정상 재발화 보존)."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, saved = await _start_rule(
        engine, rs, _presence_rule("all", for_dur={"seconds": 0.05}),
        [("person.user", "home"), ("person.wife", "not_home")])
    key = (saved["id"], 0)
    src.inject("person.wife", "home", old_state="not_home")  # 전원 재실 → arm → 발화
    await asyncio.sleep(0.12)
    assert ha.calls == [_FIRE]
    assert key in engine._held_fired                 # all 축 발화 래치
    assert engine.status()["active_timers"] == 0
    engine._last_fired.clear()                       # 쿨다운 제거(재발화 여부를 래치만으로 판정)

    # 붕괴: 한 명 외출(count n→n-1) → 래치 해제, 재무장 없음.
    src.inject("person.wife", "not_home", old_state="home")
    assert key not in engine._held_fired
    assert engine.status()["active_timers"] == 0
    # 재유지: 다시 전원 재실 → 재무장 → 재발화.
    src.inject("person.wife", "home", old_state="not_home")
    assert engine.status()["active_timers"] == 1
    await asyncio.sleep(0.12)
    assert ha.calls == [_FIRE, _FIRE]
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_reconnect_no_refire_after_fired(v2_data_dir):
    """결함2(재연결 축): 무인 유지 발화 후 재연결(resync)에서 무인이 계속 유지돼도
    _arm_missing_held_timers 가 발화 래치를 존중해 재무장·재발화하지 않는다."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, saved = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 0.05}),
        [("person.user", "home"), ("person.wife", "not_home")])
    key = (saved["id"], 0)
    src.inject("person.user", "not_home", old_state="home")  # 무인 → arm → 발화
    await asyncio.sleep(0.12)
    assert ha.calls == [_FIRE]
    assert key in engine._held_fired
    engine._last_fired.clear()                       # 쿨다운 제거
    # 재연결: 스냅샷도 여전히 무인(유지 지속) → 그래도 이미 발화했으니 재무장 금지.
    engine._on_resync(_snap({"person.user": "not_home", "person.wife": "not_home"},
                            changed_ago=0.0))
    assert key not in engine._for_timers             # 재무장 안 됨(래치 존중)
    await asyncio.sleep(0.12)
    assert ha.calls == [_FIRE]                        # 재발화 없음
    # 재연결 스냅샷에서 유지가 깨지면(귀가) 래치가 해제된다.
    engine._on_resync(_snap({"person.user": "home", "person.wife": "not_home"}))
    assert key not in engine._held_fired
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_for_unindex_cancels_timer(v2_data_dir):
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, saved = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 5}),
        [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.user", "not_home", old_state="home")
    key = (saved["id"], 0)
    handle = engine._for_timers[key]
    engine._unindex_rule(saved["id"])
    assert key not in engine._for_timers
    assert handle.cancelled()
    await engine.stop()


# ===========================================================================
# 재시작(pending) 복원 · 재연결(resync) 복원 · 자동 발화 0 (§2.4 게이트4)
# ===========================================================================
@pytest.mark.asyncio
async def test_presence_restart_pending_fires(v2_data_dir):
    """last+for 타이머 무장 후 재시작 → 만료 경과 + 무인 유지면 첫 resync 후 발화(복원)."""
    loop = asyncio.get_running_loop()
    base = datetime(2026, 7, 16, 12, 0, 0)
    shared_rules = RuleStore(JsonStore(v2_data_dir / "rules.json", [], loop=loop))

    # 1차: for 100s 무장 후 stop → pending 저장
    engine1, _, _, _ = await _build_presence(v2_data_dir, now_fn=lambda: base,
                                             rule_store=shared_rules)
    src1, saved = await _start_rule(
        engine1, shared_rules, _presence_rule("last", for_dur={"seconds": 100}),
        [("person.user", "home"), ("person.wife", "not_home")])
    src1.inject("person.user", "not_home", old_state="home")
    assert engine1.status()["active_timers"] == 1
    await engine1.stop()

    pend = json.loads((v2_data_dir / "pending_timers.json").read_text())
    assert len(pend) == 1 and pend[0]["rule_id"] == saved["id"]

    # 2차: 시계 +200s(만료 경과) → 재시작. 첫 resync 스냅샷 무인 → 발화.
    now2 = lambda: base + timedelta(seconds=200)
    runlog2 = RunLog(JsonStore(v2_data_dir / "runlog.json", [], loop=loop))
    engine2, _, _, ha2 = await _build_presence(v2_data_dir, now_fn=now2,
                                               rule_store=shared_rules, runlog=runlog2)
    src2 = _FakeSource(_seed(("person.user", "not_home"), ("person.wife", "not_home")))
    await engine2.start(src2)
    await asyncio.sleep(0.05)
    assert ha2.calls == [_FIRE]
    assert [e["result"] for e in runlog2.entries()] == ["fired"]
    await engine2.stop()


@pytest.mark.asyncio
async def test_presence_restart_pending_no_fire_if_someone_home(v2_data_dir):
    """만료 pending 이라도 재시작 스냅샷에서 무인이 아니면(귀가) 발화하지 않는다."""
    loop = asyncio.get_running_loop()
    base = datetime(2026, 7, 16, 12, 0, 0)
    shared_rules = RuleStore(JsonStore(v2_data_dir / "rules.json", [], loop=loop))

    engine1, _, _, _ = await _build_presence(v2_data_dir, now_fn=lambda: base,
                                             rule_store=shared_rules)
    src1, saved = await _start_rule(
        engine1, shared_rules, _presence_rule("last", for_dur={"seconds": 100}),
        [("person.user", "home"), ("person.wife", "not_home")])
    src1.inject("person.user", "not_home", old_state="home")
    await engine1.stop()

    now2 = lambda: base + timedelta(seconds=200)
    runlog2 = RunLog(JsonStore(v2_data_dir / "runlog.json", [], loop=loop))
    engine2, _, _, ha2 = await _build_presence(v2_data_dir, now_fn=now2,
                                               rule_store=shared_rules, runlog=runlog2)
    # 재시작 시 누군가 귀가한 스냅샷 → 무인 아님 → 미발화.
    src2 = _FakeSource(_seed(("person.user", "home"), ("person.wife", "not_home")))
    await engine2.start(src2)
    await asyncio.sleep(0.05)
    assert ha2.calls == []
    await engine2.stop()


@pytest.mark.asyncio
async def test_presence_reconnect_arms_when_level_held(v2_data_dir):
    """재연결 resync 스냅샷이 무인(막 진입)이면 잔여시간으로 타이머 장전 → 발화(fix3 복원)."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, saved = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 0.2}),
        [("person.user", "home"), ("person.wife", "home")])
    key = (saved["id"], 0)
    assert key not in engine._for_timers  # 아직 무인 아님

    engine._on_resync(_snap({"person.user": "not_home", "person.wife": "not_home"},
                            changed_ago=0.0))
    assert key in engine._for_timers  # 무인 유지로 장전
    await asyncio.sleep(0.3)
    assert ha.calls == [_FIRE]
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_reconnect_no_arm_when_for_exceeded(v2_data_dir):
    """재연결 시 무인이 이미 for 초과 유지면 장전하지 않고 발화하지 않는다(자동 발화 0)."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, saved = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 0.2}),
        [("person.user", "home"), ("person.wife", "home")])
    key = (saved["id"], 0)
    engine._on_resync(_snap({"person.user": "not_home", "person.wife": "not_home"},
                            changed_ago=5.0))  # for 0.2s 초과
    assert key not in engine._for_timers
    await asyncio.sleep(0.1)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_reconnect_revalidate_cancels_on_return(v2_data_dir):
    """재연결 스냅샷에서 무인이 깨졌으면(귀가) armed 타이머를 취소한다(fix2)."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, saved = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 0.2}),
        [("person.user", "home"), ("person.wife", "not_home")])
    src.inject("person.user", "not_home", old_state="home")  # 무인 → arm
    key = (saved["id"], 0)
    assert key in engine._for_timers
    # 재연결: 스냅샷에 누군가 집 → 유지 깨짐 → 취소
    engine._on_resync(_snap({"person.user": "home", "person.wife": "not_home"}))
    assert key not in engine._for_timers
    await asyncio.sleep(0.3)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_presence_restart_recompute_no_immediate_fire(v2_data_dir):
    """재시작 recompute 는 즉시 자동 발화하지 않는다(게이트4). 무인 유지면 타이머만 장전."""
    engine, rs, rl, ha = await _build_presence(v2_data_dir)
    src, saved = await _start_rule(
        engine, rs, _presence_rule("last", for_dur={"seconds": 100}),
        [("person.user", "not_home"), ("person.wife", "not_home")])
    engine._compile_all()  # 재시작 모사(재컴파일)
    await asyncio.sleep(0.05)
    assert ha.calls == []  # 즉시 발화 없음
    await engine.stop()


# ===========================================================================
# 파서/postpass 통합 (#23) — presence_agg 방출 + savable
# ===========================================================================
def _gz():
    import asyncio as _a
    from backend.mock_data import MockHAClient
    from backend.ha_client import merge_inventory
    from backend.nl.gazetteer import Gazetteer
    ha = MockHAClient()

    async def _f():
        return await ha.fetch_registries(), await ha.get_states()

    reg, states = _a.run(_f())
    inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
    inv["zones"] = [{"entity_id": s["entity_id"], "name": ""}
                    for s in states if s["entity_id"].startswith("zone.")]
    return Gazetteer.build(inv, _PARSE_SETTINGS)


_PARSE_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user", "와이프": "person.wife"},
    "modes": {"슬립 모드": {"action": "scene.turn_on",
                         "target": {"entity_id": ["scene.sleep_mode"]}}},
    "near_home": {"zone_state": "home"},
    "aliases": [],
}


def _prim(model):
    subs = model.get("subrules")
    return subs[0] if isinstance(subs, list) and subs else model


@pytest.fixture(scope="module")
def parse_gz():
    return _gz()


def test_parse_emits_presence_last(parse_gz):
    from backend.nl.parser import parse
    r = parse("집에 아무도 없으면 보일러 꺼줘", parse_gz, _PARSE_SETTINGS, {})
    assert _prim(r["model"])["triggers"] == [{"type": "presence_agg", "quant": "last"}]
    assert r["ok"] is True  # savable 승급


def test_parse_emits_presence_all_with_persons(parse_gz):
    from backend.nl.parser import parse
    r = parse("우리 둘 다 집에 오면 거실 불 켜줘", parse_gz, _PARSE_SETTINGS, {})
    assert _prim(r["model"])["triggers"] == [
        {"type": "presence_agg", "quant": "all", "persons": ["person.user", "person.wife"]}]
    assert r["ok"] is True


def test_parse_emits_presence_any(parse_gz):
    from backend.nl.parser import parse
    r = parse("가족 중 누구든 집에 오면 현관등 켜줘", parse_gz, _PARSE_SETTINGS, {})
    assert _prim(r["model"])["triggers"] == [{"type": "presence_agg", "quant": "any"}]


def test_parse_emits_presence_last_for(parse_gz):
    from backend.nl.parser import parse
    r = parse("10분 넘게 집에 아무도 없으면 난방 꺼줘", parse_gz, _PARSE_SETTINGS, {})
    assert _prim(r["model"])["triggers"] == [
        {"type": "presence_agg", "quant": "last", "for": {"minutes": 10}}]


def test_parse_presence_model_validates(parse_gz):
    """방출된 presence 모델은 검증기를 통과한다(auto_disabled 방지)."""
    from backend.nl.parser import parse
    inv = {"entities": [{"entity_id": "person.user"}, {"entity_id": "person.wife"},
                        {"entity_id": "climate.living_room_boiler"},
                        {"entity_id": "light.living_room_main"}]}
    for sent in ("집에 아무도 없으면 보일러 꺼줘",
                 "우리 둘 다 집에 오면 거실 불 켜줘",
                 "10분 넘게 집에 아무도 없으면 난방 꺼줘"):
        r = parse(sent, parse_gz, _PARSE_SETTINGS, {})
        # 트리거 노드 자체가 검증 통과(액션 대상은 문장별 상이 — 트리거만 확인)
        sub = _prim(r["model"])
        errs = validate_rule_model(
            {"subrules": [{"triggers": sub["triggers"], "conditions": [],
                           "actions": [{"type": "service", "action": "light.turn_on",
                                        "target": {"entity_id": ["light.living_room_main"]}}]}]},
            inv)
        assert errs == [], (sent, errs)
