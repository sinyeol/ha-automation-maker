"""S9 (APP-PORT-PLAN §4) — L2 게이트 재설계 · 학습 런타임 템플릿 · 구조 동형 · delexicalize v2.

검증 목표(절대 게이트):
  - 절대 미덮음: ok & conf≥0.6 인 L1 결과는 매처를 호출조차 하지 않는다.
  - not-ok 창: learned 우선 → 매처(검증 통과 필수). 금지문은 어느 창에서도 흡수 금지.
  - shadow 창(ok & conf<0.6): 구조 동형 스킵 · 서브룰수 동일 · 검증 통과일 때만 채택, L1 보존.
  - 학습 런타임 템플릿(§4.5): 저장 model 을 어순이 다른 문장이 스트림 LCS 로 재사용(엔티티 재검증).
  - 극성 소프트닝(§4.3): ACTON↔ACTOFF·TRIGON↔TRIGOFF 는 하드 차단 유지.
"""
from __future__ import annotations

import copy

import pytest

from backend.nl import pattern_match as pm
from backend.nl.gazetteer import Gazetteer
from backend.nl.pattern_match import (TemplateMatcher, canonical_model, l2_gate,
                                       load_pattern_library, struct_equal,
                                       subrule_count)


# ---------------------------------------------------------------------------
# 자족 인벤토리(test_pattern_match 와 동형)
# ---------------------------------------------------------------------------
def _e(eid, name, area_id=None, area_name=None, dc=None, state="off"):
    return {"entity_id": eid, "domain": eid.split(".", 1)[0], "name": name,
            "area_id": area_id, "area_name": area_name, "device_id": None,
            "device_name": None, "device_class": dc, "state": state,
            "unit": None, "attributes": {}}


_AREAS = [{"area_id": a, "name": n, "icon": ""} for a, n in [
    ("living_room", "거실"), ("master_bedroom", "안방"), ("bathroom", "욕실"),
    ("entrance", "현관"), ("kitchen", "주방")]]

_ENTITIES = [
    _e("light.living_room_main", "거실 메인등", "living_room", "거실"),
    _e("light.master_bedroom", "안방 조명", "master_bedroom", "안방"),
    _e("light.bathroom", "욕실 조명", "bathroom", "욕실"),
    _e("fan.bathroom_fan", "욕실 환풍기", "bathroom", "욕실"),
    _e("binary_sensor.bathroom_motion", "욕실 모션", "bathroom", "욕실", dc="motion"),
    _e("binary_sensor.living_room_motion", "거실 모션", "living_room", "거실", dc="motion"),
    _e("binary_sensor.master_bedroom_motion", "안방 모션", "master_bedroom", "안방", dc="motion"),
    _e("person.user", "나"),
    _e("scene.sleep_mode", "슬립 모드"),
]

_INVENTORY = {"areas": _AREAS, "entities": _ENTITIES,
              "zones": [{"entity_id": "zone.home", "name": "집"}]}

_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user"},
    "modes": {"슬립 모드": {"action": "scene.turn_on",
                         "target": {"entity_id": ["scene.sleep_mode"]}}},
    "near_home": {"zone_state": "home"},
    "aliases": [],
}

# 저장 가능한 구체 model(mock 실존 엔티티).
_FAN_MODEL = {"subrules": [{
    "triggers": [{"type": "state", "entity_id": "binary_sensor.bathroom_motion", "to": "on"}],
    "conditions": [],
    "actions": [{"type": "service", "action": "fan.turn_on",
                 "target": {"entity_id": ["fan.bathroom_fan"]}}]}]}


@pytest.fixture(scope="module")
def gz():
    return Gazetteer.build(_INVENTORY, _SETTINGS)


@pytest.fixture(scope="module")
def library():
    return load_pattern_library()


class _Stub:
    """정해진 결과를 돌려주는 매처 대역(호출 문장 기록)."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def match(self, sentence):
        self.calls.append(sentence)
        return self.result


def _valid(_m):
    return []           # 항상 통과


def _invalid(_m):
    return [{"path": "x", "message": "bad"}]


# ===========================================================================
# 절대 미덮음 · not-ok 창 · 금지문 가드
# ===========================================================================
def test_gate_never_overrides_ok_highconf():
    l1 = {"triggers": [{"type": "state", "entity_id": "a", "to": "on"}],
          "conditions": [], "actions": []}
    r = {"ok": True, "confidence": 0.8, "model": copy.deepcopy(l1)}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "x", "score": 0.9})
    info = l2_gate(r, "문장", matcher=st, validate=_valid)
    assert info["used"] is None
    assert st.calls == []                    # 매처 호출조차 안 함
    assert r["model"] == l1                   # 결과 불변


def test_gate_notok_adopts_matcher():
    r = {"ok": False, "confidence": 0.0,
         "model": {"triggers": [], "conditions": [], "actions": []}}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "target_fan_on",
                "score": 0.9})
    info = l2_gate(r, "욕실 환풍기 켜", matcher=st, validate=_valid)
    assert info["used"] == "pattern"
    assert info["matched_id"] == "target_fan_on"
    assert r["ok"] is True
    assert r["model"]["subrules"][0]["actions"][0]["action"] == "fan.turn_on"


def test_gate_notok_rejects_invalid_matcher():
    r = {"ok": False, "confidence": 0.0, "model": {}}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "x", "score": 0.9})
    info = l2_gate(r, "욕실 환풍기 켜", matcher=st, validate=_invalid)
    assert info["used"] is None
    assert r["ok"] is False                   # 검증 실패 → 미채택


def test_gate_learned_preferred_over_matcher():
    r = {"ok": False, "confidence": 0.0, "model": {}}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "x", "score": 0.9})
    info = l2_gate(r, "문장", matcher=st, validate=_valid,
                   learned_lookup=lambda: copy.deepcopy(_FAN_MODEL))
    assert info["used"] == "learned"
    assert st.calls == []                     # learned 채택 시 매처 미호출


@pytest.mark.parametrize("sentence", ["가스밸브 열지 마", "문 잠그지 말아줘",
                                      "불 켜지 말라고"])
def test_gate_prohibition_never_absorbed(sentence):
    r = {"ok": False, "confidence": 0.0, "model": {}}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "x", "score": 0.9})
    info = l2_gate(r, sentence, matcher=st, validate=_valid)
    assert info["used"] is None
    assert st.calls == []                     # 금지문은 매처 호출조차 안 함
    assert r["ok"] is False


# ===========================================================================
# shadow 창(ok & conf<0.6)
# ===========================================================================
def _sub(entity, action=None, target=None):
    s = {"triggers": [{"type": "state", "entity_id": entity, "to": "on"}],
         "conditions": [], "actions": []}
    if action:
        s["actions"] = [{"type": "service", "action": action,
                         "target": {"entity_id": [target]}}]
    return s


def test_gate_shadow_skips_when_struct_equal():
    l1 = {"subrules": [copy.deepcopy(_FAN_MODEL["subrules"][0])]}
    r = {"ok": True, "confidence": 0.3, "model": l1}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "x", "score": 0.9})
    info = l2_gate(r, "문장", matcher=st, validate=_valid)
    assert info["shadow_tried"] is True
    assert info["shadow_adopted"] is False    # 동형 → 스킵
    assert info["used"] is None


def test_gate_shadow_adopts_when_different_valid_same_count():
    l1 = {"subrules": [_sub("binary_sensor.living_room_motion")]}   # 1 서브룰(액션 없음)
    r = {"ok": True, "confidence": 0.3, "model": copy.deepcopy(l1)}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "target_fan_on",
                "score": 0.9})
    info = l2_gate(r, "문장", matcher=st, validate=_valid)
    assert info["shadow_tried"] is True
    assert info["shadow_adopted"] is True
    assert info["used"] == "pattern-shadow"
    assert r["ok"] is True
    assert r["l1_model"] == l1                 # L1 보존(런로그·회귀 분석용)


def test_gate_shadow_skips_on_subrule_count_mismatch():
    l1 = {"subrules": [_sub("binary_sensor.living_room_motion"),
                       _sub("binary_sensor.bathroom_motion")]}       # 2 서브룰
    r = {"ok": True, "confidence": 0.3, "model": copy.deepcopy(l1)}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "x",   # 1 서브룰
                "score": 0.9})
    info = l2_gate(r, "문장", matcher=st, validate=_valid)
    assert info["shadow_adopted"] is False


def test_gate_shadow_can_be_disabled():
    r = {"ok": True, "confidence": 0.3, "model": {"subrules": [_sub("x")]}}
    st = _Stub({"model": copy.deepcopy(_FAN_MODEL), "matched_id": "x", "score": 0.9})
    info = l2_gate(r, "문장", matcher=st, validate=_valid, enable_shadow=False)
    assert info["shadow_tried"] is False
    assert st.calls == []


# ===========================================================================
# 구조 동형(struct_equal / canonical_model / subrule_count)
# ===========================================================================
def test_struct_equal_flat_vs_subrules_wrapper():
    flat = {"triggers": [{"type": "state", "entity_id": "x", "to": "on"}],
            "conditions": [], "actions": []}
    wrapped = {"subrules": [{"triggers": [{"type": "state", "entity_id": "x", "to": "on"}],
                             "conditions": [], "actions": []}]}
    assert struct_equal(flat, wrapped)
    assert canonical_model(flat) == canonical_model(wrapped)


def test_struct_equal_detects_entity_difference():
    a = {"triggers": [{"type": "state", "entity_id": "x", "to": "on"}],
         "conditions": [], "actions": []}
    b = {"triggers": [{"type": "state", "entity_id": "y", "to": "on"}],
         "conditions": [], "actions": []}
    assert not struct_equal(a, b)


def test_subrule_count():
    assert subrule_count({"subrules": [{}, {}]}) == 2
    assert subrule_count({"triggers": [], "conditions": [], "actions": []}) == 1


# ===========================================================================
# 학습 런타임 템플릿(§4.5 CLI 증류 수용체)
# ===========================================================================
def test_runtime_template_absorbs_reordered_variant(gz):
    matcher = TemplateMatcher([], gz, _INVENTORY)       # 빈 라이브러리
    assert matcher.match("욕실 모션 감지되면 욕실 환풍기 켜") is None
    matcher.add_runtime_templates([{
        "id": "L1", "normalized": "욕실 모션이 감지되면 욕실 환풍기를 켜",
        "model": copy.deepcopy(_FAN_MODEL),
        "entities": ["binary_sensor.bathroom_motion", "fan.bathroom_fan"]}])
    r = matcher.match("욕실 모션 감지되면 욕실 환풍기 켜")   # 조사 생략 변형
    assert r is not None
    assert r["matched_id"].startswith("learned:")
    assert r["model"]["subrules"][0]["actions"][0]["action"] == "fan.turn_on"


def test_runtime_template_entity_existence_recheck(gz):
    matcher = TemplateMatcher([], gz, _INVENTORY)
    stale = {"subrules": [{
        "triggers": [{"type": "state", "entity_id": "binary_sensor.bathroom_motion", "to": "on"}],
        "conditions": [],
        "actions": [{"type": "service", "action": "fan.turn_on",
                     "target": {"entity_id": ["fan.removed"]}}]}]}
    matcher.add_runtime_templates([{
        "id": "L1", "normalized": "욕실 모션이 감지되면 욕실 환풍기를 켜",
        "model": stale, "entities": ["fan.removed"]}])   # 엔티티 소멸
    assert matcher.match("욕실 모션이 감지되면 욕실 환풍기를 켜") is None


def test_runtime_templates_replaced_on_readd(gz):
    matcher = TemplateMatcher([], gz, _INVENTORY)
    matcher.add_runtime_templates([{
        "id": "L1", "normalized": "욕실 모션이 감지되면 욕실 환풍기를 켜",
        "model": copy.deepcopy(_FAN_MODEL),
        "entities": ["binary_sensor.bathroom_motion", "fan.bathroom_fan"]}])
    matcher.add_runtime_templates([])                    # 삭제 반영
    assert matcher.match("욕실 모션 감지되면 욕실 환풍기 켜") is None


# ===========================================================================
# delexicalize v2 심볼(§4.3) + 극성 소프트닝 하드 차단 유지
# ===========================================================================
def test_delexicalize_v2_event_synonyms(gz):
    matcher = TemplateMatcher([], gz, _INVENTORY)
    s1, _ = matcher.delexicalize("욕실 인기척이 느껴지면 욕실 조명 켜")
    assert "EVTON" in s1                       # 느껴지 → EVTON
    s2, _ = matcher.delexicalize("욕실 움직임이 잡히면 욕실 조명 켜")
    assert "EVTON" in s2                       # 잡히 → EVTON


def test_delexicalize_v2_intransitive_polarity(gz):
    matcher = TemplateMatcher([], gz, _INVENTORY)
    on, _ = matcher.delexicalize("거실 조명이 켜져 있으면 안방 조명 켜")
    off, _ = matcher.delexicalize("거실 조명이 꺼져 있으면 안방 조명 켜")
    assert "TRIGON" in on                      # 켜져 → TRIGON
    assert "TRIGOFF" in off                    # 꺼져 → TRIGOFF


def test_polarity_conflict_hard_block():
    assert TemplateMatcher._polarity_conflict(("ACTON",), ("ACTOFF",)) is True
    assert TemplateMatcher._polarity_conflict(("TRIGON",), ("TRIGOFF",)) is True
    # 극성 충돌이 아니면(부가 심볼 차이) 하드 차단 아님 → 소프트 패널티 대상.
    assert TemplateMatcher._polarity_conflict(("ACTON", "COND"), ("ACTON",)) is False


def test_unrecognized_rate_metric(gz):
    matcher = TemplateMatcher([], gz, _INVENTORY)
    stat = matcher.unrecognized_rate("욕실 모션이 감지되면 욕실 환풍기를 켜")
    assert stat["total"] > 0
    assert 0.0 <= stat["rate"] <= 1.0


# ===========================================================================
# 절 단위 매칭(§4.4) — 2절 문장을 subrules 로 조립
# ===========================================================================
def test_multiclause_assembles_subrules(gz, library):
    matcher = TemplateMatcher(library, gz, _INVENTORY)
    # 각 절이 mode_trig_on/off 템플릿에 매칭되는 2규칙 문장 → subrules 조립.
    r = matcher.match("슬립 모드가 켜지면 거실 메인등을 켜고 슬립 모드가 꺼지면 거실 메인등을 꺼")
    assert r is not None
    assert r["mode"] == "multiclause"
    assert subrule_count(r["model"]) == 2
    acts = [sub["actions"][0]["action"] for sub in r["model"]["subrules"]]
    assert "light.turn_on" in acts and "light.turn_off" in acts


def test_multiclause_single_clause_returns_none(gz, library):
    """단일 절(pivot<2)은 multiclause 경로를 타지 않는다(정상 단일 매칭 or None)."""
    matcher = TemplateMatcher(library, gz, _INVENTORY)
    r = matcher._match_multiclause("욕실 모션이 감지되면 욕실 환풍기를 켜")
    assert r is None
