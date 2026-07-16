"""한국어 파서 골든 테스트 (SPEC-V2 §6, §10).

- 사용자 예시 5문장의 기대 model 을 하드코딩 대조(골든).
- 변형 20문장 이상(단위 이형태·조사 생략·어순)에 대한 구조 단정.
- 실패 케이스(빈 문장, 무의미 문장 → unmatched).

인벤토리는 통합(integration) 이 mock_data 에 추가할 예정인 v2 엔티티를 포함한 자족적
테스트 인벤토리를 사용한다(SPEC-V2 §10: bathroom_motion / bathroom_fan / master_bedroom_motion /
living_room_ac / person.wife / scene.sleep_mode 등). 통합 후에는 실제 mock 인벤토리로
동일 문장이 동작해야 한다.
"""
from __future__ import annotations

import pytest

from backend.nl.gazetteer import Gazetteer
from backend.nl.parser import parse


# ---------------------------------------------------------------------------
# 테스트 인벤토리 (bootstrap entity 형태)
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
    _e("light.living_room_mood", "거실 무드등", "living_room", "거실"),
    _e("light.master_bedroom", "안방 조명", "master_bedroom", "안방"),
    _e("light.bathroom", "욕실 조명", "bathroom", "욕실"),
    _e("light.entrance", "현관 조명", "entrance", "현관"),
    _e("fan.bathroom_fan", "욕실 환풍기", "bathroom", "욕실"),
    _e("binary_sensor.bathroom_motion", "욕실 모션", "bathroom", "욕실", dc="motion"),
    _e("binary_sensor.living_room_motion", "거실 모션", "living_room", "거실", dc="motion"),
    _e("binary_sensor.master_bedroom_motion", "안방 모션", "master_bedroom", "안방", dc="motion"),
    _e("binary_sensor.entrance_door", "현관문", "entrance", "현관", dc="door"),
    _e("cover.living_room_curtain", "거실 커튼", "living_room", "거실"),
    _e("climate.living_room_ac", "거실 에어컨", "living_room", "거실"),
    _e("sensor.living_room_temperature", "거실 온도", "living_room", "거실",
       dc="temperature", state="23.4"),
    _e("person.user", "나"),
    _e("person.wife", "와이프"),
    _e("scene.sleep_mode", "슬립 모드"),
]

_INVENTORY = {"areas": _AREAS, "entities": _ENTITIES,
              "zones": [{"entity_id": "zone.home", "name": "집"}]}

_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user", "와이프": "person.wife"},
    "modes": {"슬립 모드": {"action": "scene.turn_on",
                         "target": {"entity_id": ["scene.sleep_mode"]}}},
    "near_home": {"zone_state": "home"},
    "aliases": [],
}

_DUR = lambda h=0, m=0, s=0: {"hours": h, "minutes": m, "seconds": s}


@pytest.fixture(scope="module")
def gz():
    return Gazetteer.build(_INVENTORY, _SETTINGS)


def _parse(gz, sentence, pins=None):
    return parse(sentence, gz, _SETTINGS, pins)


# ===========================================================================
# 골든 5문장 — 기대 model 전체 대조
# ===========================================================================
def test_golden_1_bathroom_fan_off(gz):
    r = _parse(gz, "화장실은 5분 동안 움직임이 없으면 환풍기와 조명을 꺼줘")
    assert r["ok"] is True
    m = r["model"]
    assert m["triggers"] == [{
        "type": "state_held", "entity_id": "binary_sensor.bathroom_motion",
        "to": "off", "for": _DUR(m=5)}]
    assert m["conditions"] == []
    assert m["actions"] == [
        {"type": "service", "action": "fan.turn_off",
         "target": {"entity_id": ["fan.bathroom_fan"]}},
        {"type": "service", "action": "light.turn_off",
         "target": {"entity_id": ["light.bathroom"]}},
    ]
    assert r["area_id"] == "bathroom"
    assert r["category"] == "fan"


def test_golden_2_arrival_two_persons(gz):
    r = _parse(gz, "나와 와이프가 집에 도착하면 현관 조명을 켜줘")
    assert r["ok"] is True
    m = r["model"]
    # 마지막 사람(와이프)이 트리거, 앞 사람(나)은 상태 조건 (SPEC §6.2)
    assert m["triggers"] == [{
        "type": "zone", "entity_id": "person.wife", "zone": "zone.home", "event": "enter"}]
    assert m["conditions"] == [{
        "type": "state", "entity_id": "person.user", "state": "home"}]
    assert m["actions"] == [{
        "type": "service", "action": "light.turn_on",
        "target": {"entity_id": ["light.entrance"]}}]
    assert r["area_id"] == "entrance"
    assert r["category"] == "lighting"


def test_golden_3_numeric_ac_on(gz):
    r = _parse(gz, "여름에 거실 온도가 28도 이상이면 에어컨을 켜줘")
    assert r["ok"] is True
    m = r["model"]
    assert m["triggers"] == [{
        "type": "numeric_state", "entity_id": "sensor.living_room_temperature",
        "above": 28.0}]
    # '여름에' → season 조건 (SPEC §3 season 노드, 결함 6 수정). 이전에는 계절 표현이
    # 무시돼 conditions == [] 였으나, 이제 계절 조건이 생성된다.
    assert m["conditions"] == [{"type": "season", "seasons": ["summer"]}]
    assert m["actions"] == [{
        "type": "service", "action": "climate.turn_on",
        "target": {"entity_id": ["climate.living_room_ac"]}}]
    assert r["area_id"] == "living_room"
    assert r["category"] == "climate"


def test_golden_4_held_scene(gz):
    r = _parse(gz, "안방은 30분 동안 움직임이 없으면 슬립 모드로 바꿔")
    assert r["ok"] is True
    m = r["model"]
    assert m["triggers"] == [{
        "type": "state_held", "entity_id": "binary_sensor.master_bedroom_motion",
        "to": "off", "for": _DUR(m=30)}]
    assert m["conditions"] == []
    assert m["actions"] == [{
        "type": "service", "action": "scene.turn_on",
        "target": {"entity_id": ["scene.sleep_mode"]}}]
    assert r["area_id"] == "master_bedroom"


def test_golden_5_segment_condition_brightness(gz):
    r = _parse(gz, "거실 조명은 새벽시간에 거실에 움직임이 있으면 10%로 켜줘")
    assert r["ok"] is True
    m = r["model"]
    assert m["triggers"] == [{
        "type": "state", "entity_id": "binary_sensor.living_room_motion", "to": "on"}]
    assert m["conditions"] == [{"type": "time_segment", "segments": ["dawn"]}]
    assert m["actions"] == [{
        "type": "service", "action": "light.turn_on",
        "target": {"entity_id": ["light.living_room_main"]},
        "data": {"brightness_pct": 10}}]
    assert r["area_id"] == "living_room"
    assert r["category"] == "lighting"


# ===========================================================================
# ParseResult 규약(§6.2): chips / confidence / status
# ===========================================================================
def test_chips_slot_keys_and_status(gz):
    r = _parse(gz, "화장실은 5분 동안 움직임이 없으면 환풍기와 조명을 꺼줘")
    slots = {c["slot_key"] for c in r["chips"]}
    assert "triggers[0].entity_id" in slots
    assert "actions[0].target" in slots
    assert "actions[1].target" in slots
    # 모든 칩이 후보를 하나 이상 가지면 status 는 confirmed/uncertain (unresolved 아님)
    assert all(c["status"] in ("confirmed", "uncertain") for c in r["chips"])
    assert 0.0 < r["confidence"] <= 1.0


def test_pins_override_makes_confirmed(gz):
    # 에어컨 액션 슬롯을 핀으로 확정하면 후보 계산을 건너뛰고 confirmed 로 유지(§6.2)
    pins = {"actions[0].target": "climate.living_room_ac"}
    r = _parse(gz, "여름에 거실 온도가 28도 이상이면 에어컨을 켜줘", pins=pins)
    chip = next(c for c in r["chips"] if c["slot_key"] == "actions[0].target")
    assert chip["status"] == "confirmed"
    assert chip["chosen"] == "climate.living_room_ac"


# ===========================================================================
# 변형 20+ — 단위 이형태 / 조사 생략 / 어순 / 부정
# ===========================================================================
def _trig_types(m):
    return [t["type"] for t in m["triggers"]]


def _act_actions(m):
    return [a.get("action") for a in m["actions"]]


def test_var_no_space_duration(gz):
    m = _parse(gz, "화장실은 5분동안 움직임이 없으면 환풍기와 조명을 꺼줘")["model"]
    assert m["triggers"][0]["type"] == "state_held"
    assert m["triggers"][0]["entity_id"] == "binary_sensor.bathroom_motion"
    assert m["triggers"][0]["for"] == _DUR(m=5)


def test_var_seconds_unit_converts(gz):
    # 300초 → 5분
    r = _parse(gz, "욕실에 300초 동안 움직임이 없으면 환풍기와 조명을 꺼줘")
    m = r["model"]
    assert m["triggers"][0]["entity_id"] == "binary_sensor.bathroom_motion"
    assert m["triggers"][0]["for"] == _DUR(m=5)
    assert m["actions"][0] == {"type": "service", "action": "fan.turn_off",
                               "target": {"entity_id": ["fan.bathroom_fan"]}}
    assert m["actions"][1]["action"] == "light.turn_off"


def test_var_state_held_p2_form(gz):
    # "없는 상태로 5분 지나면" (P2 패턴) + '랑' 병렬
    m = _parse(gz, "화장실은 움직임이 없는 상태로 5분 지나면 환풍기랑 조명을 꺼줘")["model"]
    assert m["triggers"][0]["type"] == "state_held"
    assert m["triggers"][0]["for"] == _DUR(m=5)
    assert m["actions"][0]["action"] == "fan.turn_off"


def test_var_hour_unit(gz):
    m = _parse(gz, "화장실은 1시간 동안 움직임이 없으면 환풍기를 꺼줘")["model"]
    assert m["triggers"][0]["for"] == _DUR(h=1)


def test_var_arrival_single_no_condition(gz):
    m = _parse(gz, "와이프가 집에 도착하면 현관 조명 켜줘")["model"]
    assert m["triggers"] == [{"type": "zone", "entity_id": "person.wife",
                              "zone": "zone.home", "event": "enter"}]
    assert m["conditions"] == []
    assert m["actions"][0]["target"] == {"entity_id": ["light.entrance"]}


def test_var_numeric_above_neom(gz):
    m = _parse(gz, "거실 온도가 28도 넘으면 에어컨 켜줘")["model"]
    assert m["triggers"][0] == {"type": "numeric_state",
                                "entity_id": "sensor.living_room_temperature",
                                "above": 28.0}


def test_var_numeric_particle_omitted(gz):
    m = _parse(gz, "거실 온도 28도 이상이면 에어컨을 틀어줘")["model"]
    assert m["triggers"][0]["above"] == 28.0
    assert _act_actions(m) == ["climate.turn_on"]


def test_var_numeric_below(gz):
    m = _parse(gz, "거실 온도가 18도 이하면 보일러를 켜줘")["model"]
    assert m["triggers"][0]["below"] == 18.0


def test_var_motion_trigger_no_brightness(gz):
    m = _parse(gz, "거실에 움직임이 감지되면 거실 조명을 켜줘")["model"]
    assert m["triggers"][0] == {"type": "state",
                                "entity_id": "binary_sensor.living_room_motion", "to": "on"}
    assert m["actions"][0] == {"type": "service", "action": "light.turn_on",
                               "target": {"entity_id": ["light.living_room_main"]}}


def test_var_segment_condition_with_percent_variant(gz):
    # 새벽 시간대 조건 + '10퍼센트' 이형태
    m = _parse(gz, "새벽에 거실에서 움직임이 있으면 거실 조명을 10퍼센트로 켜줘")["model"]
    assert {"type": "time_segment", "segments": ["dawn"]} in m["conditions"]
    assert m["actions"][0]["data"] == {"brightness_pct": 10}


def test_var_bedroom_motion_and_light(gz):
    m = _parse(gz, "안방에 움직임이 감지되면 안방 조명을 켜줘")["model"]
    assert m["triggers"][0]["entity_id"] == "binary_sensor.master_bedroom_motion"
    assert m["actions"][0]["target"] == {"entity_id": ["light.master_bedroom"]}


def test_var_segment_trigger_night(gz):
    m = _parse(gz, "밤이 되면 거실 조명을 꺼줘")["model"]
    assert m["triggers"] == [{"type": "segment", "to": "night"}]
    assert m["actions"][0]["action"] == "light.turn_off"


def test_var_segment_trigger_evening(gz):
    m = _parse(gz, "저녁이 되면 거실 조명을 켜줘")["model"]
    assert m["triggers"] == [{"type": "segment", "to": "evening"}]


def test_var_all_lights_scope(gz):
    m = _parse(gz, "밤이 되면 모든 조명을 꺼줘")["model"]
    assert m["triggers"] == [{"type": "segment", "to": "night"}]
    assert m["actions"][0]["action"] == "light.turn_off"
    ids = m["actions"][0]["target"]["entity_id"]
    assert set(ids) >= {"light.living_room_main", "light.bathroom", "light.entrance"}
    assert all(i.startswith("light.") for i in ids)


def test_var_door_open_trigger(gz):
    m = _parse(gz, "현관문이 열리면 현관 조명을 켜줘")["model"]
    assert m["triggers"][0] == {"type": "state",
                                "entity_id": "binary_sensor.entrance_door", "to": "on"}


def test_var_motion_negative_condition_state(gz):
    # 지속시간 없는 '움직임이 없으면' → state to off (held 아님)
    m = _parse(gz, "거실에 움직임이 없으면 거실 조명을 꺼줘")["model"]
    assert m["triggers"][0] == {"type": "state",
                                "entity_id": "binary_sensor.living_room_motion", "to": "off"}
    assert m["actions"][0]["action"] == "light.turn_off"


def test_var_daytime_segment_condition(gz):
    m = _parse(gz, "낮에 거실에 움직임이 있으면 거실 조명을 켜줘")["model"]
    assert {"type": "time_segment", "segments": ["day"]} in m["conditions"]
    assert m["triggers"][0]["type"] == "state"


def test_var_summary_present(gz):
    r = _parse(gz, "거실에 움직임이 감지되면 거실 조명을 켜줘")
    assert isinstance(r["summary"], str) and r["summary"].strip()


# ===========================================================================
# 실패 / 미해석 케이스
# ===========================================================================
@pytest.mark.parametrize("sentence", ["", "   ", "\n\t"])
def test_fail_empty_sentence(gz, sentence):
    r = _parse(gz, sentence)
    assert r["ok"] is False
    assert r["model"]["triggers"] == []
    assert r["model"]["actions"] == []


@pytest.mark.parametrize("sentence", ["안녕하세요 오늘 날씨 어때", "ㅁㄴㅇㄹ 아무거나"])
def test_fail_nonsense_sentence(gz, sentence):
    r = _parse(gz, sentence)
    assert r["ok"] is False
    assert r["unmatched"]  # 이해 못 한 구간이 기록됨


def test_fail_arrival_unmatched_person(gz):
    # '내가' 는 person 표면형('나')과 다르게 매칭되지 않아 도착 절이 미해석
    r = _parse(gz, "내가 집에 도착하면 거실 조명을 켜줘")
    assert r["ok"] is False
    assert r["model"]["triggers"] == []
    assert r["unmatched"]


def test_fail_no_trigger_command_only(gz):
    # 트리거 없이 액션만 있으면 ok 아님
    r = _parse(gz, "거실 조명을 50프로로 켜줘")
    assert r["ok"] is False
    assert r["model"]["triggers"] == []


# ===========================================================================
# v2.0 파서 결함 회귀 테스트 (확정 결함 1~10, 직접 재현으로 확인됨)
# ===========================================================================
def test_regr_1_cover_verb_direction(gz):
    """결함1: 커튼 '내려/닫아/쳐'=close, '올려/열어/걷어'=open (반전 금지)."""
    for verb in ("내려줘", "닫아줘", "쳐줘"):
        m = _parse(gz, f"거실에 움직임이 있으면 거실 커튼을 {verb}")["model"]
        assert m["actions"][0] == {
            "type": "service", "action": "cover.close_cover",
            "target": {"entity_id": ["cover.living_room_curtain"]}}, verb
    for verb in ("올려줘", "열어줘", "걷어줘"):
        m = _parse(gz, f"거실에 움직임이 있으면 거실 커튼을 {verb}")["model"]
        assert m["actions"][0]["action"] == "cover.open_cover", verb


def test_regr_1_light_dim_unsupported(gz):
    """결함1 주석: 조명 '내려줘'(밝기 낮추기)는 v2.0 미지원 → 미해석."""
    r = _parse(gz, "밤이 되면 거실 조명을 내려줘")
    assert r["model"]["actions"] == []
    assert r["unmatched"]


def test_regr_2_person_partial_match_rejected(gz):
    """결함2: '누나'의 '나'가 person.user로 오매칭되지 않는다."""
    r = _parse(gz, "누나가 집에 도착하면 거실 조명을 켜줘")
    assert r["ok"] is False
    assert r["model"]["triggers"] == []
    assert r["unmatched"]


def test_regr_2_person_with_particle_still_matches(gz):
    """결함2: 뒤가 조사인 '나와/와이프가'는 계속 매칭돼야 한다."""
    m = _parse(gz, "나와 와이프가 집에 도착하면 현관 조명을 켜줘")["model"]
    trig_ids = {t.get("entity_id") for t in m["triggers"]}
    cond_ids = {c.get("entity_id") for c in m["conditions"]}
    assert "person.wife" in trig_ids
    assert "person.user" in cond_ids


def test_regr_3_midnight_bam_12(gz):
    """결함3: '밤 12시'=00:00, '낮/오후 12시'=12:00, '새벽 12시'=00:00."""
    from backend.nl.normalize import find_clock
    assert find_clock("밤 12시")["hhmm"] == "00:00"
    assert find_clock("새벽 12시")["hhmm"] == "00:00"
    assert find_clock("낮 12시")["hhmm"] == "12:00"
    assert find_clock("오후 12시")["hhmm"] == "12:00"
    # '밤 12시 이후' → after 00:00:00 조건
    m = _parse(gz, "밤 12시 이후에 거실에 움직임이 있으면 거실 조명을 켜줘")["model"]
    assert {"type": "time", "after": "00:00:00"} in m["conditions"]


def test_regr_4_action_zone_other_not_group_held(gz):
    """결함4: 액션 존의 '다른 조명'이 트리거를 group_held로 뒤바꾸지 않는다."""
    m = _parse(gz, "거실은 5분 동안 움직임이 없으면 다른 조명을 꺼줘")["model"]
    # 트리거는 거실 모션의 state_held (group_held 아님)
    assert m["triggers"] == [{
        "type": "state_held", "entity_id": "binary_sensor.living_room_motion",
        "to": "off", "for": _DUR(m=5)}]
    # 액션은 거실을 제외한 나머지 조명들
    assert m["actions"][0]["action"] == "light.turn_off"
    ids = set(m["actions"][0]["target"]["entity_id"])
    assert ids == {"light.master_bedroom", "light.bathroom", "light.entrance"}
    assert "light.living_room_main" not in ids


def test_regr_5_locative_room_preserved(gz):
    """결함5: 처소격 '안방에'가 지속 트리거의 방으로 흡수된다(거실 오선택 금지)."""
    m = _parse(gz, "안방에 30분 동안 움직임이 없으면 안방 조명을 꺼줘")["model"]
    assert m["triggers"][0] == {
        "type": "state_held", "entity_id": "binary_sensor.master_bedroom_motion",
        "to": "off", "for": _DUR(m=30)}
    assert m["actions"][0]["target"] == {"entity_id": ["light.master_bedroom"]}


def test_regr_6_day_type_and_season(gz):
    """결함6: 주말→day_type, 계절→season 조건 생성. '주말 아침'은 둘 다."""
    m = _parse(gz, "주말 아침에 거실에 움직임이 있으면 거실 조명을 켜줘")["model"]
    assert {"type": "day_type", "types": ["weekend"]} in m["conditions"]
    assert {"type": "time_segment", "segments": ["morning"]} in m["conditions"]
    m2 = _parse(gz, "겨울에 거실에 움직임이 있으면 거실 조명을 켜줘")["model"]
    assert {"type": "season", "seasons": ["winter"]} in m2["conditions"]


def test_regr_7_delay_after_minutes(gz):
    """결함7: 액션 존 'N분 뒤에' → 해당 액션 앞에 delay 삽입."""
    m = _parse(gz, "현관문이 열리면 10분 뒤에 현관 조명을 꺼줘")["model"]
    assert m["actions"] == [
        {"type": "delay", "duration": _DUR(m=10)},
        {"type": "service", "action": "light.turn_off",
         "target": {"entity_id": ["light.entrance"]}},
    ]


def test_regr_8_daily_trigger(gz):
    """결함8: '매일 … H시' 및 'H시가 되면' → daily 트리거. 'H시 이후'는 조건 유지."""
    m = _parse(gz, "매일 밤 9시에 거실 조명을 꺼줘")["model"]
    assert m["triggers"] == [{"type": "daily", "at": "21:00"}]
    assert m["actions"][0]["action"] == "light.turn_off"
    m2 = _parse(gz, "밤 9시가 되면 거실 조명을 꺼줘")["model"]
    assert m2["triggers"] == [{"type": "daily", "at": "21:00"}]
    # '밤 9시 이후에'는 여전히 time 조건 (daily 아님)
    m3 = _parse(gz, "밤 9시 이후에 거실에 움직임이 있으면 거실 조명을 켜줘")["model"]
    assert not any(t["type"] == "daily" for t in m3["triggers"])
    assert {"type": "time", "after": "21:00:00"} in m3["conditions"]


def test_regr_9_chips_have_real_spans(gz):
    """결함9: 모든 해석 가능한 칩이 원문 내 실제 span([0,0] 아님)을 가진다."""
    r = _parse(gz, "거실 조명은 새벽시간에 거실에 움직임이 있으면 10%로 켜줘")
    sentence = "거실 조명은 새벽시간에 거실에 움직임이 있으면 10%로 켜줘"
    assert len(r["chips"]) >= 3
    for c in r["chips"]:
        assert c["span"] != [0, 0], c
        s, e = c["span"]
        assert sentence[s:e] == c["text"]


def test_regr_10_summary_dedup_and_josa(gz):
    """결함10: 대상 이름에 모션 의미가 있으면 중복 서술 제거 + 받침 기반 조사 단일화."""
    s = _parse(gz, "거실에 움직임이 감지되면 거실 조명을 켜줘")["summary"]
    assert "모션이 감지되면" in s          # '모션이(가) 움직임이 감지되면' 아님
    assert "움직임이 감지되면" not in s     # 중복 서술 제거
    assert "이(가)" not in s               # 받침 판정으로 단일화
    assert "을(를)" not in s
