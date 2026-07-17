"""Phase 3B — L2 템플릿 매처(backend.nl.pattern_match.TemplateMatcher) 골든 테스트.

검증 목표(APP-REINFORCEMENT-PLAN §2):
  - covered 골드 문형에 **novel(예시에 없는) 문장**을 슬롯 바인딩으로 매핑한다(slot_fill).
  - 미커버/무의미 문장은 None(오탐 0) — L1 not ok 게이트 앞단에서 매처가 흡수하지 않는다.
  - 극성 게이트: ACTON↔ACTOFF 교차 매칭 차단(func 심볼 멀티셋 불일치).
  - 인덱스는 covered/partial 만(§2.6 게이트2: gap 제외) — gap 템플릿은 절대 반환하지 않는다.

인벤토리는 test_nl_parser.py 와 동일한 자족 부트스트랩 인벤토리를 사용한다.
"""
from __future__ import annotations

import pytest

from backend.engine.rule_model import validate_rule_model
from backend.nl.gazetteer import Gazetteer
from backend.nl.pattern_match import (TemplateMatcher, load_pattern_library)


# ---------------------------------------------------------------------------
# 테스트 인벤토리
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
    _e("light.entrance", "현관 조명", "entrance", "현관"),
    _e("fan.bathroom_fan", "욕실 환풍기", "bathroom", "욕실"),
    _e("binary_sensor.bathroom_motion", "욕실 모션", "bathroom", "욕실", dc="motion"),
    _e("binary_sensor.living_room_motion", "거실 모션", "living_room", "거실", dc="motion"),
    _e("binary_sensor.master_bedroom_motion", "안방 모션", "master_bedroom", "안방", dc="motion"),
    _e("cover.living_room_curtain", "거실 커튼", "living_room", "거실"),
    _e("climate.living_room_ac", "거실 에어컨", "living_room", "거실"),
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


@pytest.fixture(scope="module")
def gz():
    return Gazetteer.build(_INVENTORY, _SETTINGS)


@pytest.fixture(scope="module")
def library():
    lib = load_pattern_library()
    assert lib, "pattern_library.yaml 가 비어 있으면 매처 테스트가 의미 없다"
    return lib


@pytest.fixture
def matcher(library, gz):
    return TemplateMatcher(library, gz, _INVENTORY)


def _flatten_subrules(model: dict) -> list:
    """flat 모델과 subrules 모델을 공통 [(triggers, actions)] 리스트로 정규화."""
    subs = model.get("subrules")
    if isinstance(subs, list) and subs:
        return subs
    return [model]


# ===========================================================================
# slot_fill — novel 문장을 covered 골드 문형에 매핑
# ===========================================================================
def test_slot_fill_maps_novel_sentence(matcher):
    """예시 목록에 없는 (안방 모션 → 욕실 환풍기) 조합이 target_fan_on 으로 흡수된다."""
    sentence = "안방 모션이 감지되면 욕실 환풍기를 켜"  # 3개 예시 중 어디에도 없는 조합
    r = matcher.match(sentence)
    assert r is not None
    assert r["matched_id"] == "target_fan_on"
    assert r["mode"] == "slot_fill"
    assert r["score"] == 1.0

    sub = _flatten_subrules(r["model"])[0]
    assert sub["triggers"][0]["entity_id"] == "binary_sensor.master_bedroom_motion"
    assert sub["triggers"][0]["to"] == "on"
    act = sub["actions"][0]
    assert act["action"] == "fan.turn_on"
    assert act["target"]["entity_id"] == ["fan.bathroom_fan"]


def test_slot_fill_model_passes_rule_validation(matcher):
    """매처가 만든 model 은 실제 규칙 검증(validate_rule_model)을 통과한다(저장 가능)."""
    r = matcher.match("안방 모션이 감지되면 욕실 환풍기를 켜")
    assert r is not None
    assert validate_rule_model(r["model"], _INVENTORY, set()) == []


def test_slot_fill_scope_expands_domain(matcher):
    """target_scope_all: '다' 스코프가 도메인 전체(light) 로 확장된다."""
    r = matcher.match("거실 모션이 감지되면 다 안방 조명을 꺼주세요")
    assert r is not None
    assert r["matched_id"] == "target_scope_all"
    sub = _flatten_subrules(r["model"])[0]
    act = sub["actions"][0]
    assert act["action"] == "light.turn_off"
    ids = act["target"]["entity_id"]
    # scope.expand → 인벤토리의 모든 light 도메인 엔티티
    assert set(ids) == {e["entity_id"] for e in _ENTITIES if e["domain"] == "light"}


# ===========================================================================
# 도메인 인식 — light/fan/climate 골드를 도메인으로 구분(오도메인 흡수 차단)
# ===========================================================================
@pytest.mark.parametrize("sentence, want_id, want_action, want_target", [
    ("안방 모션이 감지되면 거실 메인등을 켜",
     "target_basic_on", "light.turn_on", "light.living_room_main"),
    ("안방 모션이 감지되면 욕실 환풍기를 켜",
     "target_fan_on", "fan.turn_on", "fan.bathroom_fan"),
    ("거실 모션이 감지되면 거실 에어컨을 켜",
     "target_climate_on", "climate.turn_on", "climate.living_room_ac"),
])
def test_device_domain_routing(matcher, sentence, want_id, want_action, want_target):
    """[MOT,EVTON,D,ACTON] 동일 스트림이라도 기기 도메인으로 골드를 갈라 채택한다.

    회귀: 예전에는 covered 정렬 첫 항목(target_fan_on)이 도메인과 무관하게 선택돼, light
    문장이 fan.turn_on(on light) 으로 흡수됐다(조용히 잘못된 자동화).
    """
    r = matcher.match(sentence)
    assert r is not None, sentence
    assert r["matched_id"] == want_id
    act = _flatten_subrules(r["model"])[0]["actions"][0]
    assert act["action"] == want_action
    assert act["target"]["entity_id"] == [want_target]
    # 안전망: 도메인 일치 model 은 검증 통과.
    assert validate_rule_model(r["model"], _INVENTORY, set()) == []


def test_validate_rejects_cross_domain_target():
    """Finding: validate_rule_model 이 service↔대상 엔티티 도메인 불일치를 잡는다
    (매처/LLM/학습 모든 저장 경로의 안전망). 예: fan.turn_on 을 light 엔티티에."""
    def _mdl(action, target):
        return {"subrules": [{
            "triggers": [{"type": "state",
                          "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
            "conditions": [],
            "actions": [{"type": "service", "action": action,
                         "target": {"entity_id": target}}]}]}
    # 도메인 불일치 → 오류.
    assert validate_rule_model(
        _mdl("fan.turn_on", ["light.living_room_main"]), _INVENTORY, set())
    # 도메인 일치 → 통과.
    assert validate_rule_model(
        _mdl("fan.turn_on", ["fan.bathroom_fan"]), _INVENTORY, set()) == []


def test_validate_rejects_null_and_empty_target():
    """Finding: 미해석 slot 이 흘려보낸 [None]/[] 대상을 거부한다."""
    def _mdl(target):
        return {"subrules": [{
            "triggers": [{"type": "state",
                          "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
            "conditions": [],
            "actions": [{"type": "service", "action": "light.turn_on",
                         "target": {"entity_id": target}}]}]}
    assert validate_rule_model(_mdl([None]), _INVENTORY, set())
    assert validate_rule_model(_mdl([]), _INVENTORY, set())
    assert validate_rule_model(_mdl(["light.living_room_main"]), _INVENTORY, set()) == []


# ===========================================================================
# 오탐 0 — 미커버/무의미 문장은 None
# ===========================================================================
@pytest.mark.parametrize("sentence", [
    "안녕하세요 오늘 날씨 어때요",
    "ㅁㄴㅇㄹ 테스트 문장",
    "그냥 아무 의미 없는 말",
    "",
])
def test_no_false_positive(matcher, sentence):
    assert matcher.match(sentence) is None


def test_empty_stream_returns_none(matcher):
    # gazetteer 로 아무 내용/기능 토큰도 잡히지 않으면 매칭 시도 자체를 하지 않는다.
    assert matcher.match("... ??? ...") is None


# ===========================================================================
# 극성 게이트 — ACTON↔ACTOFF 교차 매칭 차단
# ===========================================================================
def test_polarity_gate_blocks_off_against_on_template(matcher):
    """켜기 템플릿(target_fan_on)만 있고 끄기 커버가 없으면, 끄기 문장은 매칭되지 않는다.

    같은 (MOT EVTON D) 골격이라도 액션 극성(ACTOFF vs ACTON)이 다르면 struct_replace
    가 교차 매칭하지 못한다(func 심볼 멀티셋 불일치)."""
    on = matcher.match("욕실 모션이 감지되면 욕실 환풍기를 켜")
    off = matcher.match("욕실 모션이 감지되면 욕실 환풍기를 꺼")
    assert on is not None and on["matched_id"] == "target_fan_on"
    assert off is None


def test_delexicalize_polarity_symbols(matcher):
    """delexicalize 스트림의 극성 심볼이 켜기/끄기에서 갈린다(게이트의 근거)."""
    on_stream, _ = matcher.delexicalize("욕실 모션이 감지되면 욕실 환풍기를 켜")
    off_stream, _ = matcher.delexicalize("욕실 모션이 감지되면 욕실 환풍기를 꺼")
    assert on_stream[-1] == "ACTON"
    assert off_stream[-1] == "ACTOFF"
    assert (TemplateMatcher._func_multiset(on_stream)
            != TemplateMatcher._func_multiset(off_stream))


# ===========================================================================
# 인덱스 게이트 — covered/partial 만, gap 제외
# ===========================================================================
def test_index_only_covered_or_partial(matcher):
    statuses = {r["status"] for r in matcher._index}
    assert statuses <= {"covered", "partial"}


def test_gap_template_excluded_from_index(gz):
    """status=gap 템플릿은 인덱스에서 제외된다(§2.6 게이트2)."""
    covered = {
        "id": "t_cov", "status": "covered",
        "template": "{motion}{가} 감지되면 {device_fan}{을} <on>",
        "gold": {"subrules": [{"triggers": [{"type": "state",
                 "entity_id": "{motion.entity}", "to": "on"}], "conditions": [],
                 "actions": [{"type": "service", "action": "fan.turn_on",
                 "target": {"entity_id": ["{device_fan.entity}"]}}]}]},
    }
    gap = dict(covered, id="t_gap", status="gap")
    tm = TemplateMatcher([covered, gap], gz, _INVENTORY)
    ids = {r["id"] for r in tm._index}
    assert "t_cov" in ids
    assert "t_gap" not in ids


def test_only_gap_library_matches_nothing(gz):
    """라이브러리가 gap 템플릿뿐이면 인덱스가 비어 어떤 문장도 매칭되지 않는다."""
    gap = {
        "id": "t_gap", "status": "gap",
        "template": "{motion}{가} 감지되면 {device_fan}{을} <on>",
        "gold": {"subrules": [{"triggers": [{"type": "state",
                 "entity_id": "{motion.entity}", "to": "on"}], "conditions": [],
                 "actions": [{"type": "service", "action": "fan.turn_on",
                 "target": {"entity_id": ["{device_fan.entity}"]}}]}]},
    }
    tm = TemplateMatcher([gap], gz, _INVENTORY)
    assert tm._index == []
    assert tm.match("욕실 모션이 감지되면 욕실 환풍기를 켜") is None
