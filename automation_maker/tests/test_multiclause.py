"""다중 조건-액션 문법 (SPEC-V3 §2) 테스트.

- 골든 B 가 subrules 2개로 정확히 분해되는지(컨텍스트 상속 포함).
- 단일 쌍 문장은 최상위 4필드로 평탄화(하위호환).
- 엔진이 서브룰을 독립적으로 발화하는지.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.nl.gazetteer import Gazetteer
from backend.nl.parser import parse


_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user"}, "near_home": {"zone_state": "home"},
    "aliases": [],
    "modes": {"슬립 모드": {"initial": "off",
                          "on_action": {"action": "scene.turn_on",
                                        "target": {"entity_id": ["scene.sleep_mode"]}},
                          "off_action": None}},
}

_GOLDEN_B = ("새벽에 슬립모드이고 거실 모션이 작동하면 거실조명을 10% 켜주고 "
             "3분 동안 모션이 없으면 꺼줘")
_GOLDEN_A = "슬립모드가 켜지면 집의 모든 조명을 꺼줘"


def _build_inventory():
    from backend.mock_data import MockHAClient
    from backend.ha_client import merge_inventory
    from backend.app import extract_zones

    async def _go():
        ha = MockHAClient()
        await ha.start()
        reg = await ha.fetch_registries()
        states = await ha.get_states()
        inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
        return {"areas": inv["areas"], "entities": inv["entities"],
                "zones": extract_zones(states)}

    return asyncio.run(_go())


@pytest.fixture(scope="module")
def gz():
    return Gazetteer.build(_build_inventory(), _SETTINGS)


# ---------------------------------------------------------------------------
# 골든 B: 2개의 서브룰 + 컨텍스트 상속
# ---------------------------------------------------------------------------
def test_golden_b_two_subrules(gz):
    r = parse(_GOLDEN_B, gz, _SETTINGS)
    assert r["ok"] is True
    subrules = r["model"].get("subrules")
    assert isinstance(subrules, list) and len(subrules) == 2


def test_golden_b_subrule1(gz):
    sub = parse(_GOLDEN_B, gz, _SETTINGS)["model"]["subrules"][0]
    assert sub["triggers"] == [{
        "type": "state", "entity_id": "binary_sensor.living_room_motion", "to": "on"}]
    # 조건: 시간대(새벽) + 모드(슬립 모드 on)
    assert {"type": "time_segment", "segments": ["dawn"]} in sub["conditions"]
    assert {"type": "mode", "mode": "슬립 모드", "state": "on"} in sub["conditions"]
    # 액션: 거실 메인등 10% 켜기
    act = sub["actions"][0]
    assert act["action"] == "light.turn_on"
    assert act["target"]["entity_id"] == ["light.living_room_main"]
    assert act["data"]["brightness_pct"] == 10


def test_golden_b_subrule2_context_inheritance(gz):
    sub = parse(_GOLDEN_B, gz, _SETTINGS)["model"]["subrules"][1]
    # 트리거: state_held(모션 off, 3분)
    t = sub["triggers"][0]
    assert t["type"] == "state_held"
    assert t["entity_id"] == "binary_sensor.living_room_motion"
    assert t["to"] == "off"
    assert t["for"] == {"hours": 0, "minutes": 3, "seconds": 0}
    # 컨텍스트 상속: 대상이 생략됐지만 앞 서브룰의 거실 메인등을 상속
    act = sub["actions"][0]
    assert act["action"] == "light.turn_off"
    assert act["target"]["entity_id"] == ["light.living_room_main"]


# ---------------------------------------------------------------------------
# 골든 A: 단일 쌍 → 최상위 평탄화(하위호환)
# ---------------------------------------------------------------------------
def test_golden_a_single_flattened(gz):
    r = parse(_GOLDEN_A, gz, _SETTINGS)
    assert r["ok"] is True
    model = r["model"]
    # 단일 쌍은 subrules 없이 최상위 필드로 평탄화한다.
    assert "subrules" not in model
    assert model["triggers"] == [{"type": "mode", "mode": "슬립 모드", "to": "on"}]
    act = model["actions"][0]
    assert act["action"] == "light.turn_off"
    # "집의 모든 조명" → 인벤토리 전체 light.*
    assert len(act["target"]["entity_id"]) >= 5
    assert all(e.startswith("light.") for e in act["target"]["entity_id"])


def test_simple_single_pair_backward_compat(gz):
    r = parse("거실에 움직임이 감지되면 거실 조명을 켜줘", gz, _SETTINGS)
    assert r["ok"] is True
    assert "subrules" not in r["model"]     # 단일 쌍은 평탄화
    assert r["model"]["triggers"][0]["entity_id"] == "binary_sensor.living_room_motion"


# ---------------------------------------------------------------------------
# 엔진: 서브룰 독립 발화
# ---------------------------------------------------------------------------
def _two_subrule_rule():
    """서브룰1: 모션 on→켜기, 서브룰2: 모션 off→끄기(상속 대상)."""
    return {"sentence": "다중 규칙", "model": {"alias": "", "mode": "single", "subrules": [
        {"triggers": [{"type": "state",
                       "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
         "condition_mode": "and", "conditions": [],
         "actions": [{"type": "service", "action": "light.turn_on",
                      "target": {"entity_id": ["light.living_room_main"]}}]},
        {"triggers": [{"type": "state",
                       "entity_id": "binary_sensor.living_room_motion", "to": "off"}],
         "condition_mode": "and", "conditions": [],
         "actions": [{"type": "service", "action": "light.turn_off",
                      "target": {"entity_id": ["light.living_room_main"]}}]},
    ]}}


@pytest.mark.asyncio
async def test_engine_fires_subrules_independently(make_v3_engine):
    engine, rs, rl, ha, src, ms = await make_v3_engine(
        seed=[{"entity_id": "binary_sensor.living_room_motion", "state": "off",
               "attributes": {}}])
    saved = rs.upsert(_two_subrule_rule())
    engine.reload_rule(saved["id"])

    # 서브룰1 트리거: 모션 on → 켜기
    src.inject("binary_sensor.living_room_motion", "on", old_state="off")
    await asyncio.sleep(0.04)
    assert ("light", "turn_on", {"entity_id": ["light.living_room_main"]}) in ha.calls

    # 서브룰2 트리거: 모션 off → 끄기
    src.inject("binary_sensor.living_room_motion", "off", old_state="on")
    await asyncio.sleep(0.04)
    assert ("light", "turn_off", {"entity_id": ["light.living_room_main"]}) in ha.calls
    # 두 서브룰이 각각 fired 로 기록
    assert [e["result"] for e in rl.entries()] == ["fired", "fired"]
    await engine.stop()


# ---------------------------------------------------------------------------
# 적대적 검증 회귀(§2.1): 컨텍스트 상속·경계 판정 확정 결함 3건
# ---------------------------------------------------------------------------
def test_defect1_condition_sensor_reuses_trigger_not_action_area(gz):
    """[major] 생략된 뒤 서브룰 조건('모션이 없으면')은 앞 서브룰의 트리거 센서를 재참조해야
    한다. 액션 대상 방(안방)으로 오염되면 안 됨(§2.1 컨텍스트 상속).

    재현: "거실 모션이 감지되면 안방 조명을 켜주고 3분 동안 모션이 없으면 꺼줘".
    수정 전엔 서브룰2 트리거가 state_held(binary_sensor.master_bedroom_motion)였다.
    """
    r = parse("거실 모션이 감지되면 안방 조명을 켜주고 3분 동안 모션이 없으면 꺼줘",
              gz, _SETTINGS)
    subs = r["model"]["subrules"]
    assert len(subs) == 2
    # 서브룰1 트리거: 앞에서 언급된 '거실 모션' 센서 재참조 — '안방 모션'이 아니어야 함
    t = subs[1]["triggers"][0]
    assert t["type"] == "state_held"
    assert t["entity_id"] == "binary_sensor.living_room_motion"
    assert t["entity_id"] != "binary_sensor.master_bedroom_motion"
    assert t["to"] == "off"
    assert t["for"] == {"hours": 0, "minutes": 3, "seconds": 0}
    # 액션 대상 상속(안방 조명)은 그대로 — 트리거 센서 상속과 분리
    assert subs[0]["actions"][0]["target"]["entity_id"] == ["light.master_bedroom"]
    assert subs[1]["actions"][0]["action"] == "light.turn_off"
    assert subs[1]["actions"][0]["target"]["entity_id"] == ["light.master_bedroom"]


def test_defect2_explicit_unmatched_target_not_silently_inherited(gz):
    """[minor] 명시된 대상이 어휘에 없으면(무드등) 앞 서브룰 대상(욕실 조명)으로 조용히
    상속하지 말고 unresolved 로 내려 ok=False (§결함2).

    재현: "욕실에 움직임이 감지되면 욕실 조명을 켜주고 밤이 되면 무드등을 켜줘".
    수정 전엔 서브룰2 액션이 light.turn_on(light.bathroom) 로 조용히 상속되고 ok=True였다.
    """
    r = parse("욕실에 움직임이 감지되면 욕실 조명을 켜주고 밤이 되면 무드등을 켜줘",
              gz, _SETTINGS)
    subs = r["model"]["subrules"]
    assert len(subs) == 2
    # 서브룰0 은 정상적으로 욕실 조명
    assert subs[0]["actions"][0]["target"]["entity_id"] == ["light.bathroom"]
    # 서브룰1 은 앞 대상(light.bathroom)을 상속하지 않아야 함
    inherited = [a for a in subs[1]["actions"]
                 if a.get("target", {}).get("entity_id") == ["light.bathroom"]]
    assert inherited == []
    # '무드등' 은 미해석으로 내려가고 전체 ok=False
    assert r["ok"] is False
    assert any("무드등" in u for u in r["unmatched"])
    assert any(c["status"] == "unresolved" for c in r["chips"])


def test_defect3_condition_subject_verb_not_treated_as_action_boundary(gz):
    """[major] 다음 서브룰 조건의 주어+동사('환풍기가 가동하고')를 앞 서브룰 액션 경계로
    오인해 스푸리어스 액션(fan.turn_on)을 만들면 안 됨(§결함3).

    재현: "거실에 움직임이 감지되면 에어컨을 켜주고 환풍기가 가동하고 사람이 없으면 에어컨을 꺼줘".
    수정 전엔 서브룰1 액션에 fan.turn_on(fan.bathroom_fan)이 스푸리어스로 컴파일됐다.
    """
    r = parse("거실에 움직임이 감지되면 에어컨을 켜주고 환풍기가 가동하고 사람이 없으면 에어컨을 꺼줘",
              gz, _SETTINGS)
    subs = r["model"]["subrules"]
    assert len(subs) == 2
    # 서브룰0 액션은 에어컨 켜기 하나뿐 — '환풍기가 가동하고'가 흡수되면 안 됨
    sr0_actions = subs[0]["actions"]
    assert len(sr0_actions) == 1
    assert sr0_actions[0]["action"] == "climate.turn_on"
    assert sr0_actions[0]["target"]["entity_id"] == ["climate.living_room_ac"]
    # 어떤 서브룰에도 스푸리어스 fan.turn_on 이 없어야 함
    all_actions = [a for sr in subs for a in sr["actions"]]
    assert not any(a.get("action") == "fan.turn_on" for a in all_actions)
