"""ModeState + 엔진 모드 처리 (SPEC-V3 §1) 테스트.

- ModeState: set/get/snapshot/persist/v2 마이그레이션/sync_settings.
- RuleEngine.set_mode: mode 트리거 pubsub 발화, side-effect 서비스 호출,
  mode 조건 평가, 수동 토글, 재귀 깊이 제한.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.engine.modes import ModeState
from backend.engine.storage import JsonStore


# ---------------------------------------------------------------------------
# ModeState 단위 테스트 (동기)
# ---------------------------------------------------------------------------
def _store(tmp_path, data=None):
    return JsonStore(tmp_path / "modes_state.json", {} if data is None else data)


def test_mode_get_default_off(tmp_path):
    ms = ModeState({"modes": {"슬립 모드": {"initial": "off"}}}, _store(tmp_path))
    assert ms.get("슬립 모드") == "off"
    assert ms.get("정의안된모드") == "off"       # 미정의 모드는 off
    assert ms.names() == ["슬립 모드"]


def test_mode_initial_on(tmp_path):
    ms = ModeState({"modes": {"외출 모드": {"initial": "on"}}}, _store(tmp_path))
    assert ms.get("외출 모드") == "on"
    assert ms.snapshot() == {"외출 모드": "on"}


def test_mode_set_returns_changed(tmp_path):
    ms = ModeState({"modes": {"슬립 모드": {"initial": "off"}}}, _store(tmp_path))
    assert ms.set("슬립 모드", True) is True     # off→on 변경
    assert ms.get("슬립 모드") == "on"
    assert ms.set("슬립 모드", True) is False    # 동일 상태 → 변경 아님
    assert ms.set("슬립 모드", False) is True    # on→off 변경
    assert ms.get("슬립 모드") == "off"


def test_mode_persist_via_store(tmp_path):
    store = _store(tmp_path)
    ms = ModeState({"modes": {"슬립 모드": {"initial": "off"}}}, store)
    ms.set("슬립 모드", True)
    ms.save()
    # store.data 는 런타임 상태를 그대로 참조한다(재시작 간 유지).
    assert store.data.get("슬립 모드") == "on"


def test_mode_persist_reload(tmp_path):
    # 저장된 상태로 새 ModeState 를 만들면 상태가 복원된다.
    prior = _store(tmp_path, {"슬립 모드": "on"})
    ms = ModeState({"modes": {"슬립 모드": {"initial": "off"}}}, prior)
    assert ms.get("슬립 모드") == "on"          # initial(off) 이 아니라 저장값 우선


def test_mode_v2_migration(tmp_path):
    """v2 형식({action,target,data})은 on_action 으로 승격, off_action=null, initial=off."""
    settings = {"modes": {"슬립 모드": {"action": "scene.turn_on",
                                       "target": {"entity_id": ["scene.sleep"]}}}}
    ms = ModeState(settings, _store(tmp_path))
    d = ms.definition("슬립 모드")
    assert d["initial"] == "off"
    assert d["off_action"] is None
    assert d["on_action"] == {"action": "scene.turn_on",
                              "target": {"entity_id": ["scene.sleep"]}}
    # side_effect 헬퍼도 on/off 를 구분한다.
    assert ms.side_effect("슬립 모드", "on") == d["on_action"]
    assert ms.side_effect("슬립 모드", "off") is None
    # sync_settings 가 settings.modes 를 제자리 정규화(마이그레이션)한다.
    assert settings["modes"]["슬립 모드"]["on_action"]["action"] == "scene.turn_on"


def test_mode_set_ignores_undefined_mode(tmp_path):
    """정의(settings.modes)에 없는 모드로 set 하면 무시(False)되어 유령 상태가 안 생긴다.

    (SPEC-V3 §1.2 방어: 미정의/삭제 모드로 set(True) 하면 유령 상태가 modes_state.json 에
    영속되던 결함의 회귀 테스트.)
    """
    store = _store(tmp_path)
    ms = ModeState({"modes": {"슬립 모드": {"initial": "off"}}}, store)
    assert ms.set("유령 모드", True) is False       # 미정의 → 무시
    assert "유령 모드" not in ms.snapshot()          # 상태 미생성
    assert ms.get("유령 모드") == "off"
    ms.save()
    assert "유령 모드" not in store.data             # 영속 파일에도 없음


def test_mode_undefined_set_then_redefined_uses_initial(tmp_path):
    """미정의 모드로 set(True) 뒤 그 이름이 initial=off 로 (재)정의돼도 유령 'on' 이 되살아나지 않는다.

    버그 재현: set 이 유령 상태를 영속 → 재생성 시 sync_settings 가 initial 을 무시하고 'on' 유지.
    """
    store = _store(tmp_path)
    ms = ModeState({"modes": {"슬립 모드": {"initial": "off"}}}, store)
    ms.set("외출 모드", True)                        # 아직 미정의 → 무시돼야 함
    ms.save()
    # "외출 모드" 가 initial off 로 정의된 채 재생성(디스크 상태 승계)
    store2 = JsonStore(tmp_path / "modes_state.json", dict(store.data))
    ms2 = ModeState({"modes": {"외출 모드": {"initial": "off"}}}, store2)
    assert ms2.get("외출 모드") == "off"             # 유령 'on' 이 아니라 initial off


def test_mode_deleted_then_readded_resets_to_initial(tmp_path):
    """정의된 모드를 on 으로 둔 뒤 삭제→재추가하면 initial 로 리셋된다(잔존 상태 제거)."""
    ms = ModeState({"modes": {"외출 모드": {"initial": "off"}}}, _store(tmp_path))
    assert ms.set("외출 모드", True) is True
    assert ms.get("외출 모드") == "on"
    ms.sync_settings({"modes": {}})                 # 삭제 → 잔존 상태 제거
    assert "외출 모드" not in ms.snapshot()
    ms.sync_settings({"modes": {"외출 모드": {"initial": "off"}}})  # 재추가 → initial
    assert ms.get("외출 모드") == "off"


def test_mode_sync_settings_add_remove(tmp_path):
    ms = ModeState({"modes": {"슬립 모드": {"initial": "off"}}}, _store(tmp_path))
    ms.set("슬립 모드", True)
    # 새 모드 추가 + 기존 모드 제거
    ms.sync_settings({"modes": {"외출 모드": {"initial": "on"}}})
    assert "슬립 모드" not in ms.snapshot()      # 삭제된 모드 제거
    assert ms.get("외출 모드") == "on"           # 새 모드는 초기값으로 추가
    assert ms.names() == ["외출 모드"]


# ---------------------------------------------------------------------------
# 엔진 set_mode: side-effect + mode 트리거 pubsub
# ---------------------------------------------------------------------------
_MODE_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {}, "near_home": {"zone_state": "home"}, "aliases": [],
    "modes": {
        "슬립 모드": {
            "initial": "off",
            "on_action": {"action": "scene.turn_on",
                          "target": {"entity_id": ["scene.sleep_mode"]}},
            "off_action": None,
        },
    },
}


def _mode_trigger_rule(to="on"):
    return {"sentence": "슬립모드가 켜지면 거실 조명을 꺼줘", "model": {
        "triggers": [{"type": "mode", "mode": "슬립 모드", "to": to}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_off",
                     "target": {"entity_id": ["light.living_room_main"]}}]}}


@pytest.mark.asyncio
async def test_set_mode_runs_side_effect(make_v3_engine):
    engine, rs, rl, ha, src, ms = await make_v3_engine(settings=_MODE_SETTINGS)
    changed = engine.set_mode("슬립 모드", True, "manual")
    assert changed is True
    await asyncio.sleep(0.03)
    # on_action(scene.turn_on) 이 호출됐다.
    assert ("scene", "turn_on", {"entity_id": ["scene.sleep_mode"]}) in ha.calls
    assert ms.get("슬립 모드") == "on"
    await engine.stop()


@pytest.mark.asyncio
async def test_set_mode_no_change_no_side_effect(make_v3_engine):
    engine, rs, rl, ha, src, ms = await make_v3_engine(settings=_MODE_SETTINGS)
    engine.set_mode("슬립 모드", True, "manual")
    await asyncio.sleep(0.02)
    ha.calls.clear()
    # 이미 on → 다시 on 은 변경 아님 → side-effect 없음
    assert engine.set_mode("슬립 모드", True, "manual") is False
    await asyncio.sleep(0.02)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_set_mode_fires_mode_trigger(make_v3_engine):
    engine, rs, rl, ha, src, ms = await make_v3_engine(settings=_MODE_SETTINGS)
    saved = rs.upsert(_mode_trigger_rule(to="on"))
    engine.reload_rule(saved["id"])

    engine.set_mode("슬립 모드", True, "manual")
    await asyncio.sleep(0.05)
    # mode 트리거 규칙(light.turn_off)이 발화했다.
    assert ("light", "turn_off", {"entity_id": ["light.living_room_main"]}) in ha.calls
    assert [e["result"] for e in rl.entries()] == ["fired"]
    await engine.stop()


@pytest.mark.asyncio
async def test_mode_trigger_edge_only(make_v3_engine):
    """mode 트리거는 실제 전이일 때만 발화(재설정은 무발화)."""
    engine, rs, rl, ha, src, ms = await make_v3_engine(settings=_MODE_SETTINGS)
    saved = rs.upsert(_mode_trigger_rule(to="on"))
    engine.reload_rule(saved["id"])

    engine.set_mode("슬립 모드", True, "manual")
    await asyncio.sleep(0.05)
    ha.calls.clear()
    # 동일 상태 재설정 → 전이 없음 → 트리거 무발화
    engine.set_mode("슬립 모드", True, "manual")
    await asyncio.sleep(0.05)
    assert ha.calls == []
    await engine.stop()


@pytest.mark.asyncio
async def test_mode_trigger_to_off(make_v3_engine):
    engine, rs, rl, ha, src, ms = await make_v3_engine(settings=_MODE_SETTINGS)
    saved = rs.upsert(_mode_trigger_rule(to="off"))
    engine.reload_rule(saved["id"])
    off_call = ("light", "turn_off", {"entity_id": ["light.living_room_main"]})
    # 먼저 on 으로: 트리거는 to==off 라 무발화(단, on_action scene 부수효과는 실행됨).
    engine.set_mode("슬립 모드", True, "manual")
    await asyncio.sleep(0.03)
    assert off_call not in ha.calls          # to==off 트리거는 on 전이에 발화 안 함
    ha.calls.clear()
    # off 로 전이 → 발화
    engine.set_mode("슬립 모드", False, "manual")
    await asyncio.sleep(0.05)
    assert off_call in ha.calls
    await engine.stop()


# ---------------------------------------------------------------------------
# mode 조건 평가
# ---------------------------------------------------------------------------
def _motion_rule_with_mode_cond(state="on"):
    return {"sentence": "슬립모드일 때 움직이면 조명을 켜줘", "model": {
        "triggers": [{"type": "state",
                      "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
        "condition_mode": "and",
        "conditions": [{"type": "mode", "mode": "슬립 모드", "state": state}],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.living_room_main"]}}]}}


@pytest.mark.asyncio
async def test_mode_condition_gates_firing(make_v3_engine):
    engine, rs, rl, ha, src, ms = await make_v3_engine(
        settings=_MODE_SETTINGS,
        seed=[{"entity_id": "binary_sensor.living_room_motion", "state": "off",
               "attributes": {}}])
    saved = rs.upsert(_motion_rule_with_mode_cond(state="on"))
    engine.reload_rule(saved["id"])

    # 모드 off 상태 → 조건 불충족 → skipped
    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await asyncio.sleep(0.04)
    assert ha.calls == []
    assert [e["result"] for e in rl.entries()] == ["skipped_condition"]

    # 모드 on 으로 → 조건 충족 → 발화
    engine.set_mode("슬립 모드", True, "manual")
    await asyncio.sleep(0.03)
    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await asyncio.sleep(0.04)
    assert ("light", "turn_on", {"entity_id": ["light.living_room_main"]}) in ha.calls
    await engine.stop()


# ---------------------------------------------------------------------------
# status + 수동 토글
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_status_includes_modes(make_v3_engine):
    engine, rs, rl, ha, src, ms = await make_v3_engine(settings=_MODE_SETTINGS)
    st = engine.status()
    assert st["modes"] == {"슬립 모드": "off"}
    engine.set_mode("슬립 모드", True, "manual")
    assert engine.status()["modes"] == {"슬립 모드": "on"}
    await engine.stop()


@pytest.mark.asyncio
async def test_set_mode_unknown_engine_without_modestate(v2_data_dir):
    """mode_state 없는 엔진은 set_mode 가 조용히 False(모드 비활성)."""
    from backend.engine.engine import RuleEngine
    from backend.engine.rule_store import RuleStore
    from backend.engine.runlog import RunLog
    from backend.engine.state_cache import StateCache
    from backend.engine.variables import GlobalVars

    loop = asyncio.get_running_loop()
    rs = RuleStore(JsonStore(v2_data_dir / "rules.json", [], loop=loop))
    rl = RunLog(JsonStore(v2_data_dir / "runlog.json", [], loop=loop))
    engine = RuleEngine(rs, StateCache(), GlobalVars({}), None,
                        lambda: {"entities": []}, rl, loop=loop)
    assert engine.set_mode("슬립 모드", True) is False
    assert engine.status()["modes"] == {}
