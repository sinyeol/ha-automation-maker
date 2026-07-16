"""수동 단어 매핑 (SPEC-V3 §3) 테스트.

- tokenize: 공백 분절 + 조사 분리(core) + 문자 오프셋.
- suggest_roles: 파서 힌트로 역할 초안.
- build_model_from_tokens: 사용자 예시 토큰이 골든 B 와 동등한 subrules 2개를 만드는지,
  boundary 분리, duration held/delay, 유효성 오류.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.engine.rule_model import validate_rule_model
from backend.nl.gazetteer import Gazetteer
from backend.nl.manual import (build_model_from_tokens, suggest_roles, tokenize)


_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user"}, "near_home": {"zone_state": "home"},
    "aliases": [],
    "modes": {"슬립 모드": {"initial": "off", "on_action": None, "off_action": None}},
}


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
def inv():
    return _build_inventory()


@pytest.fixture(scope="module")
def gz(inv):
    return Gazetteer.build(inv, _SETTINGS)


# ---------------------------------------------------------------------------
# tokenize
# ---------------------------------------------------------------------------
def test_tokenize_offsets_and_core():
    toks = tokenize("거실조명은 켜줘")
    assert [t["text"] for t in toks] == ["거실조명은", "켜줘"]
    assert [t["index"] for t in toks] == [0, 1]
    # 조사(은) 분리 → core
    assert toks[0]["core"] == "거실조명"
    # 문자 오프셋(공백 기준)
    assert toks[0]["start"] == 0 and toks[0]["end"] == 5
    assert toks[1]["start"] == 6 and toks[1]["end"] == 8


def test_tokenize_empty():
    assert tokenize("") == []
    assert tokenize("   ") == []


# ---------------------------------------------------------------------------
# suggest_roles (파서 힌트 초안)
# ---------------------------------------------------------------------------
def test_suggest_roles_basic_hints(gz):
    toks = tokenize("새벽에 3분 10% 켜줘 없으면")
    sug = {s["index"]: s for s in suggest_roles(toks, gz, _SETTINGS)}
    assert sug[0]["role"] == "segment" and sug[0]["ref"] == "dawn"
    assert sug[1]["role"] == "duration" and sug[1]["value"] == 180
    assert sug[2]["role"] == "value" and sug[2]["value"] == 10
    assert sug[3]["role"] == "action_verb" and sug[3]["verb"] == "on"
    assert sug[4]["role"] == "event_state" and sug[4]["state"] == "off"


def test_suggest_roles_mode(gz):
    toks = tokenize("슬립모드이고")
    sug = suggest_roles(toks, gz, _SETTINGS)
    assert sug[0]["role"] == "mode_ref"
    assert sug[0]["ref"] == "슬립 모드"
    assert sug[0]["state"] == "on"


# ---------------------------------------------------------------------------
# build_model_from_tokens — 골든 B 재현(2 서브룰 + 컨텍스트 상속)
# ---------------------------------------------------------------------------
# 골든 B 문장 토큰:
# 0 새벽에 1 슬립모드이고 2 거실 3 모션이 4 작동하면 5 거실조명을 6 10% 7 켜주고
# 8 3분 9 동안 10 모션이 11 없으면 12 꺼줘
_GOLDEN_B_ASSIGN = [
    {"index": 0, "role": "segment", "ref": "dawn"},
    {"index": 1, "role": "mode_ref", "ref": "슬립 모드", "state": "on"},
    {"index": 2, "role": "ignore"},
    {"index": 3, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
    {"index": 4, "role": "event_state", "state": "on"},
    {"index": 5, "role": "action_target", "ref": "light.living_room_main"},
    {"index": 6, "role": "value", "kind": "brightness", "value": 10},
    {"index": 7, "role": "action_verb", "verb": "on", "boundary": True},
    {"index": 8, "role": "duration", "value": 180},
    {"index": 9, "role": "ignore"},
    {"index": 10, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
    {"index": 11, "role": "event_state", "state": "off"},
    {"index": 12, "role": "action_verb", "verb": "off"},
]


def test_build_golden_b_two_subrules(inv):
    res = build_model_from_tokens(_GOLDEN_B_ASSIGN, inv, _SETTINGS)
    assert res["ok"] is True
    assert res["errors"] == []
    subs = res["model"]["subrules"]
    assert len(subs) == 2


def test_build_golden_b_subrule1(inv):
    subs = build_model_from_tokens(_GOLDEN_B_ASSIGN, inv, _SETTINGS)["model"]["subrules"]
    s1 = subs[0]
    assert s1["triggers"] == [{
        "type": "state", "entity_id": "binary_sensor.living_room_motion", "to": "on"}]
    assert {"type": "time_segment", "segments": ["dawn"]} in s1["conditions"]
    assert {"type": "mode", "mode": "슬립 모드", "state": "on"} in s1["conditions"]
    act = s1["actions"][0]
    assert act["action"] == "light.turn_on"
    assert act["target"]["entity_id"] == ["light.living_room_main"]
    assert act["data"]["brightness_pct"] == 10


def test_build_golden_b_subrule2_inheritance(inv):
    subs = build_model_from_tokens(_GOLDEN_B_ASSIGN, inv, _SETTINGS)["model"]["subrules"]
    s2 = subs[1]
    # duration(3분)이 트리거존에 있으므로 state_held 로 승격
    t = s2["triggers"][0]
    assert t == {"type": "state_held", "entity_id": "binary_sensor.living_room_motion",
                 "to": "off", "for": {"hours": 0, "minutes": 3, "seconds": 0}}
    # 액션 대상 미지정 → 앞 서브룰의 거실 메인등 상속
    act = s2["actions"][0]
    assert act["action"] == "light.turn_off"
    assert act["target"]["entity_id"] == ["light.living_room_main"]


# ---------------------------------------------------------------------------
# boundary 분리
# ---------------------------------------------------------------------------
def test_boundary_role_splits_subrules(inv):
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "event_state", "state": "on"},
        {"index": 2, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 3, "role": "action_verb", "verb": "on"},
        {"index": 4, "role": "boundary"},        # 전용 boundary 토큰
        {"index": 5, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 6, "role": "event_state", "state": "off"},
        {"index": 7, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 8, "role": "action_verb", "verb": "off"},
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is True
    assert len(res["model"]["subrules"]) == 2


def test_single_pair_flattened(inv):
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "event_state", "state": "on"},
        {"index": 2, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 3, "role": "action_verb", "verb": "on"},
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is True
    # 단일 쌍 → 최상위 평탄화(하위호환)
    assert "subrules" not in res["model"]
    assert res["model"]["triggers"][0]["entity_id"] == "binary_sensor.living_room_motion"
    assert res["model"]["actions"][0]["action"] == "light.turn_on"


# ---------------------------------------------------------------------------
# duration: 액션존이면 delay
# ---------------------------------------------------------------------------
def test_duration_in_action_zone_becomes_delay(inv):
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "event_state", "state": "on"},
        {"index": 2, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 3, "role": "action_verb", "verb": "on"},
        {"index": 4, "role": "duration", "value": 60},     # 액션 뒤 → delay
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    actions = res["model"]["actions"]
    assert {"type": "delay", "duration": {"hours": 0, "minutes": 1, "seconds": 0}} in actions


# ---------------------------------------------------------------------------
# 유효성 오류
# ---------------------------------------------------------------------------
def test_error_missing_action(inv):
    # 트리거만 있고 동작 없음
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "event_state", "state": "on"},
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is False
    assert any("동작" in e for e in res["errors"])


def test_error_missing_trigger(inv):
    # 동작만 있고 트리거 없음
    assign = [
        {"index": 0, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 1, "role": "action_verb", "verb": "on"},
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is False
    assert any("트리거" in e or "조건" in e for e in res["errors"])


def test_error_empty_assignments(inv):
    res = build_model_from_tokens([], inv, _SETTINGS)
    assert res["ok"] is False
    assert res["errors"]


# ---------------------------------------------------------------------------
# [Fix1] build 의 ok 가 실제 저장 게이트(validate_rule_model)와 일치해야 한다.
#   기존엔 트리거/액션 '존재'만 검사해 저장 불가 모델에도 ok=True 가 나왔다.
# ---------------------------------------------------------------------------
def test_build_ok_matches_save_gate_numeric_missing_value(inv):
    """numeric 역할에 값이 비면 저장 시 400 → build ok=False 여야 한다(회귀)."""
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "numeric", "cmp": "above"},      # value 없음
        {"index": 2, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 3, "role": "action_verb", "verb": "on"},
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is False
    assert any("이상 또는 이하" in e for e in res["errors"])
    # ok=False 는 실제 저장 게이트 실패와 정확히 일치한다.
    assert validate_rule_model(res["model"], inv, set(_SETTINGS["modes"]))


def test_build_ok_matches_save_gate_mode_ref_unselected(inv):
    """mode_ref 의 모드가 미선택(ref=None)이면 저장 시 400 → build ok=False(회귀)."""
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "event_state", "state": "on"},
        {"index": 2, "role": "mode_ref", "ref": None, "state": "on"},   # 모드 미선택
        {"index": 3, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 4, "role": "action_verb", "verb": "on"},
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is False
    assert any("모드 이름" in e for e in res["errors"])


def test_build_ok_matches_save_gate_set_mode_unselected(inv):
    """set_mode 의 모드가 미선택(ref=None)이면 저장 시 400 → build ok=False(회귀)."""
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "event_state", "state": "on"},
        {"index": 2, "role": "action_verb", "verb": "set_mode", "ref": None, "state": "on"},
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is False
    assert any("모드 이름" in e for e in res["errors"])


def test_build_ok_true_still_passes_save_gate(inv):
    """반대 방향: build 가 ok=True 라면 저장 게이트도 통과해야 한다(불변식)."""
    res = build_model_from_tokens(_GOLDEN_B_ASSIGN, inv, _SETTINGS)
    assert res["ok"] is True
    assert validate_rule_model(res["model"], inv, set(_SETTINGS["modes"])) == []


# ---------------------------------------------------------------------------
# [Fix2] 상태 미지정 엔티티 토큰은 조용히 사라지지 않고 경고 + ok=False.
# ---------------------------------------------------------------------------
def test_build_unmapped_entity_between_triggers_warns(inv):
    """앞 트리거 엔티티에 상태를 안 붙이고 다음 엔티티로 넘어가면 경고하고 버린다(회귀)."""
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.entrance_door"},  # 상태 미지정
        {"index": 1, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 2, "role": "event_state", "state": "on"},
        {"index": 3, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 4, "role": "action_verb", "verb": "on"},
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is False
    assert any("무시했어요" in w for w in res["warnings"])
    # 소비되지 않은 현관문은 어떤 트리거/조건에도 들어가지 않는다.
    trigs = res["model"]["triggers"]
    assert all(t.get("entity_id") != "binary_sensor.entrance_door" for t in trigs)


def test_build_trailing_unmapped_entity_warns(inv):
    """맨 끝의 상태 미지정 엔티티(플러시로 소멸)도 경고 대상이다(회귀)."""
    assign = [
        {"index": 0, "role": "trigger_entity", "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "event_state", "state": "on"},
        {"index": 2, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 3, "role": "action_verb", "verb": "on"},
        {"index": 4, "role": "condition_entity", "ref": "binary_sensor.entrance_door"},  # 상태 미지정, 끝
    ]
    res = build_model_from_tokens(assign, inv, _SETTINGS)
    assert res["ok"] is False
    assert any("무시했어요" in w for w in res["warnings"])


def test_build_fully_mapped_entities_no_drop_warning(inv):
    """정상 매핑(모든 엔티티에 상태 지정)이면 '무시' 경고가 없어야 한다(과경고 방지)."""
    res = build_model_from_tokens(_GOLDEN_B_ASSIGN, inv, _SETTINGS)
    assert not any("무시했어요" in w for w in res["warnings"])
