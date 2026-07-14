"""build_automation / validate_model 골든 테스트.

기대값은 SPEC §2.3 규칙으로 직접 계산해 하드코딩한다(신문법: triggers/[trigger:...],
actions/[action:...]). build_automation 은 모델 전체를 검증하므로, 개별 노드 테스트는
검증을 통과하는 최소 모델(FILLER 트리거/액션 포함)에 노드를 끼워 넣어 확인한다.
"""
from __future__ import annotations

import pytest

from backend.automation_builder import (
    ValidationError, build_automation, summarize, to_yaml, validate_model,
)
from tests.conftest import dur, make_model


# ---------------------------------------------------------------------------
# 개별 노드 빌드 헬퍼
# ---------------------------------------------------------------------------
def built_trigger(t: dict) -> dict:
    return build_automation(make_model(triggers=[t]))["triggers"][0]


def built_conditions(c, mode: str = "and") -> list:
    conds = c if isinstance(c, list) else [c]
    return build_automation(make_model(conditions=conds, condition_mode=mode))["conditions"]


def built_action(a: dict) -> dict:
    return build_automation(make_model(actions=[a]))["actions"][0]


# ===========================================================================
# 트리거 8종 골든
# ===========================================================================
def test_trigger_state():
    t = {"type": "state", "entity_id": "light.x", "from": "off", "to": "on",
         "for": dur(minutes=5)}
    assert built_trigger(t) == {
        "trigger": "state", "entity_id": "light.x", "from": "off", "to": "on",
        "for": "00:05:00",
    }


def test_trigger_state_omits_empty_from_to_and_zero_for():
    t = {"type": "state", "entity_id": "light.x", "from": "", "to": "",
         "for": dur()}
    assert built_trigger(t) == {"trigger": "state", "entity_id": "light.x"}


def test_trigger_numeric_state():
    t = {"type": "numeric_state", "entity_id": "sensor.x", "above": 10, "below": 30,
         "for": dur(hours=1)}
    assert built_trigger(t) == {
        "trigger": "numeric_state", "entity_id": "sensor.x",
        "above": 10, "below": 30, "for": "01:00:00",
    }


def test_trigger_numeric_state_above_zero_kept():
    # above=0 은 None 이 아니므로 유지되어야 한다.
    t = {"type": "numeric_state", "entity_id": "sensor.x", "above": 0}
    assert built_trigger(t) == {
        "trigger": "numeric_state", "entity_id": "sensor.x", "above": 0,
    }


def test_trigger_time():
    assert built_trigger({"type": "time", "at": "07:00"}) == {
        "trigger": "time", "at": "07:00"}


def test_trigger_time_hms():
    assert built_trigger({"type": "time", "at": "23:30:15"}) == {
        "trigger": "time", "at": "23:30:15"}


def test_trigger_time_pattern():
    t = {"type": "time_pattern", "minutes": "/5"}
    assert built_trigger(t) == {"trigger": "time_pattern", "minutes": "/5"}


def test_trigger_sun():
    t = {"type": "sun", "event": "sunset", "offset": "-00:45:00"}
    assert built_trigger(t) == {
        "trigger": "sun", "event": "sunset", "offset": "-00:45:00"}


def test_trigger_zone():
    t = {"type": "zone", "entity_id": "person.user", "zone": "zone.home",
         "event": "enter"}
    assert built_trigger(t) == {
        "trigger": "zone", "entity_id": "person.user", "zone": "zone.home",
        "event": "enter",
    }


def test_trigger_template():
    t = {"type": "template", "value_template": "{{ is_state('x','on') }}",
         "for": dur(seconds=30)}
    assert built_trigger(t) == {
        "trigger": "template", "value_template": "{{ is_state('x','on') }}",
        "for": "00:00:30",
    }


def test_trigger_homeassistant():
    assert built_trigger({"type": "homeassistant", "event": "start"}) == {
        "trigger": "homeassistant", "event": "start"}


# ===========================================================================
# Duration 변환 규칙
# ===========================================================================
def test_duration_padding():
    t = {"type": "state", "entity_id": "light.x", "for": dur(1, 2, 3)}
    assert built_trigger(t)["for"] == "01:02:03"


def test_duration_all_zero_omitted():
    t = {"type": "state", "entity_id": "light.x", "for": dur()}
    assert "for" not in built_trigger(t)


def test_duration_delay_value():
    a = {"type": "delay", "duration": dur(hours=0, minutes=10, seconds=0)}
    assert built_action(a) == {"delay": "00:10:00"}


# ===========================================================================
# 조건 (기본 6 + trigger + 논리 3 = 10종)
# ===========================================================================
def test_condition_state():
    c = {"type": "state", "entity_id": "binary_sensor.x", "state": "on",
         "for": dur(minutes=5)}
    assert built_conditions(c) == [{
        "condition": "state", "entity_id": "binary_sensor.x", "state": "on",
        "for": "00:05:00",
    }]


def test_condition_numeric_state():
    c = {"type": "numeric_state", "entity_id": "sensor.x", "above": 20}
    assert built_conditions(c) == [{
        "condition": "numeric_state", "entity_id": "sensor.x", "above": 20}]


def test_condition_time():
    c = {"type": "time", "after": "22:00:00", "before": "06:00:00",
         "weekday": ["mon", "tue"]}
    assert built_conditions(c) == [{
        "condition": "time", "after": "22:00:00", "before": "06:00:00",
        "weekday": ["mon", "tue"],
    }]


def test_condition_sun():
    c = {"type": "sun", "after": "sunset", "before": "sunrise",
         "after_offset": "-00:30:00"}
    assert built_conditions(c) == [{
        "condition": "sun", "after": "sunset", "before": "sunrise",
        "after_offset": "-00:30:00",
    }]


def test_condition_zone():
    c = {"type": "zone", "entity_id": "person.user", "zone": "zone.home"}
    assert built_conditions(c) == [{
        "condition": "zone", "entity_id": "person.user", "zone": "zone.home"}]


def test_condition_template():
    c = {"type": "template", "value_template": "{{ true }}"}
    assert built_conditions(c) == [{
        "condition": "template", "value_template": "{{ true }}"}]


def test_condition_trigger():
    c = {"type": "trigger", "id": "0"}
    assert built_conditions(c) == [{"condition": "trigger", "id": "0"}]


def test_condition_and():
    sub_a = {"type": "state", "entity_id": "light.x", "state": "on"}
    sub_b = {"type": "numeric_state", "entity_id": "sensor.y", "below": 5}
    c = {"type": "and", "conditions": [sub_a, sub_b]}
    assert built_conditions(c) == [{
        "condition": "and", "conditions": [
            {"condition": "state", "entity_id": "light.x", "state": "on"},
            {"condition": "numeric_state", "entity_id": "sensor.y", "below": 5},
        ],
    }]


def test_condition_or():
    sub = {"type": "state", "entity_id": "light.x", "state": "on"}
    c = {"type": "or", "conditions": [sub, dict(sub)]}
    out = built_conditions(c)
    assert out[0]["condition"] == "or"
    assert len(out[0]["conditions"]) == 2


def test_condition_not():
    sub = {"type": "state", "entity_id": "light.x", "state": "on"}
    c = {"type": "not", "conditions": [sub]}
    assert built_conditions(c) == [{
        "condition": "not", "conditions": [
            {"condition": "state", "entity_id": "light.x", "state": "on"}]}]


# ---------------------------------------------------------------------------
# condition_mode == "or" 래핑 규칙
# ---------------------------------------------------------------------------
def test_condition_mode_or_wraps_when_two_or_more():
    c1 = {"type": "state", "entity_id": "light.x", "state": "on"}
    c2 = {"type": "state", "entity_id": "light.y", "state": "off"}
    out = built_conditions([c1, c2], mode="or")
    assert out == [{
        "condition": "or", "conditions": [
            {"condition": "state", "entity_id": "light.x", "state": "on"},
            {"condition": "state", "entity_id": "light.y", "state": "off"},
        ],
    }]


def test_condition_mode_and_stays_flat():
    c1 = {"type": "state", "entity_id": "light.x", "state": "on"}
    c2 = {"type": "state", "entity_id": "light.y", "state": "off"}
    out = built_conditions([c1, c2], mode="and")
    assert len(out) == 2
    assert all(x["condition"] == "state" for x in out)


def test_condition_mode_or_single_not_wrapped():
    c1 = {"type": "state", "entity_id": "light.x", "state": "on"}
    out = built_conditions([c1], mode="or")
    assert out == [{"condition": "state", "entity_id": "light.x", "state": "on"}]


# ===========================================================================
# 액션 (service·delay·wait_template·wait_for_trigger·condition·choose·if·
#        repeat(count)·repeat(while)·parallel·stop = 11종+)
# ===========================================================================
def test_action_service_full():
    a = {"type": "service", "action": "light.turn_on",
         "target": {"entity_id": ["light.x"], "area_id": [], "device_id": None},
         "data": {"brightness_pct": 50}}
    assert built_action(a) == {
        "action": "light.turn_on",
        "target": {"entity_id": ["light.x"]},
        "data": {"brightness_pct": 50},
    }


def test_action_service_minimal():
    a = {"type": "service", "action": "light.toggle"}
    assert built_action(a) == {"action": "light.toggle"}


def test_action_delay():
    a = {"type": "delay", "duration": dur(minutes=10)}
    assert built_action(a) == {"delay": "00:10:00"}


def test_action_wait_template():
    a = {"type": "wait_template", "wait_template": "{{ is_state('x','on') }}",
         "timeout": dur(minutes=1), "continue_on_timeout": True}
    assert built_action(a) == {
        "wait_template": "{{ is_state('x','on') }}",
        "timeout": "00:01:00",
        "continue_on_timeout": True,
    }


def test_action_wait_for_trigger():
    a = {"type": "wait_for_trigger",
         "triggers": [{"type": "state", "entity_id": "light.x", "to": "on"}]}
    assert built_action(a) == {
        "wait_for_trigger": [{"trigger": "state", "entity_id": "light.x", "to": "on"}]}


def test_action_condition_gate():
    a = {"type": "condition",
         "condition": {"type": "state", "entity_id": "light.x", "state": "on"}}
    assert built_action(a) == {
        "condition": "state", "entity_id": "light.x", "state": "on"}


def test_action_choose():
    a = {"type": "choose",
         "options": [{
             "conditions": [{"type": "state", "entity_id": "light.x", "state": "on"}],
             "sequence": [{"type": "service", "action": "light.turn_off"}],
         }],
         "default": [{"type": "service", "action": "light.turn_on"}]}
    assert built_action(a) == {
        "choose": [{
            "conditions": [{"condition": "state", "entity_id": "light.x", "state": "on"}],
            "sequence": [{"action": "light.turn_off"}],
        }],
        "default": [{"action": "light.turn_on"}],
    }


def test_action_if():
    a = {"type": "if",
         "if": [{"type": "state", "entity_id": "light.x", "state": "on"}],
         "then": [{"type": "service", "action": "light.turn_off"}],
         "else": [{"type": "service", "action": "light.turn_on"}]}
    assert built_action(a) == {
        "if": [{"condition": "state", "entity_id": "light.x", "state": "on"}],
        "then": [{"action": "light.turn_off"}],
        "else": [{"action": "light.turn_on"}],
    }


def test_action_repeat_count():
    a = {"type": "repeat", "kind": "count", "count": 3,
         "sequence": [{"type": "service", "action": "light.toggle"}]}
    assert built_action(a) == {
        "repeat": {"count": 3, "sequence": [{"action": "light.toggle"}]}}


def test_action_repeat_while():
    a = {"type": "repeat", "kind": "while",
         "conditions": [{"type": "state", "entity_id": "light.x", "state": "on"}],
         "sequence": [{"type": "delay", "duration": dur(minutes=1)}]}
    assert built_action(a) == {
        "repeat": {
            "while": [{"condition": "state", "entity_id": "light.x", "state": "on"}],
            "sequence": [{"delay": "00:01:00"}],
        },
    }


def test_action_parallel():
    a = {"type": "parallel", "branches": [
        [{"type": "service", "action": "light.turn_on"}],
        [{"type": "service", "action": "switch.turn_off"}],
    ]}
    assert built_action(a) == {
        "parallel": [
            {"sequence": [{"action": "light.turn_on"}]},
            {"sequence": [{"action": "switch.turn_off"}]},
        ],
    }


def test_action_stop():
    assert built_action({"type": "stop", "message": "완료"}) == {"stop": "완료"}


# ===========================================================================
# 최상위 config: 키 순서 / mode / max
# ===========================================================================
def test_config_key_order_single():
    cfg = build_automation(make_model())
    assert list(cfg.keys()) == [
        "alias", "description", "mode", "triggers", "conditions", "actions"]
    assert cfg["mode"] == "single"
    assert "max" not in cfg


def test_config_alias_stripped():
    cfg = build_automation(make_model(alias="  저녁 조명  "))
    assert cfg["alias"] == "저녁 조명"


def test_config_mode_queued_includes_max_default():
    cfg = build_automation(make_model(mode="queued"))
    assert list(cfg.keys()) == [
        "alias", "description", "mode", "max", "triggers", "conditions", "actions"]
    assert cfg["max"] == 10


def test_config_mode_parallel_max_override():
    cfg = build_automation(make_model(mode="parallel", max=5))
    assert cfg["max"] == 5


def test_config_mode_restart_no_max():
    cfg = build_automation(make_model(mode="restart"))
    assert "max" not in cfg
    assert cfg["mode"] == "restart"


# ===========================================================================
# to_yaml
# ===========================================================================
def test_to_yaml_unicode_and_order():
    cfg = build_automation(make_model(alias="거실 저녁등"))
    y = to_yaml(cfg)
    # sort_keys=False → alias 가 첫 줄, allow_unicode=True → 한글 그대로
    assert y.startswith("alias:")
    assert "거실 저녁등" in y
    assert "\\uac70" not in y  # 이스케이프되지 않음


# ===========================================================================
# 검증 오류 케이스 (10+)
# ===========================================================================
def _paths(errors):
    return {e["path"] for e in errors}


def test_validate_ok_empty_list():
    assert validate_model(make_model()) == []


def test_error_empty_alias():
    errs = validate_model(make_model(alias="   "))
    assert "alias" in _paths(errs)


def test_error_state_trigger_missing_entity():
    errs = validate_model(make_model(triggers=[{"type": "state"}]))
    assert "triggers[0].entity_id" in _paths(errs)


def test_error_numeric_state_no_above_below():
    errs = validate_model(make_model(
        triggers=[{"type": "numeric_state", "entity_id": "sensor.x"}]))
    assert "triggers[0]" in _paths(errs)


def test_error_time_bad_format():
    errs = validate_model(make_model(triggers=[{"type": "time", "at": "25:99"}]))
    assert "triggers[0].at" in _paths(errs)


def test_error_choose_empty_options():
    errs = validate_model(make_model(
        actions=[{"type": "choose", "options": []}]))
    assert "actions[0].options" in _paths(errs)


def test_error_repeat_count_below_one():
    errs = validate_model(make_model(actions=[{
        "type": "repeat", "kind": "count", "count": 0,
        "sequence": [{"type": "service", "action": "light.toggle"}]}]))
    assert "actions[0].count" in _paths(errs)


def test_error_unknown_trigger_type():
    errs = validate_model(make_model(triggers=[{"type": "bogus"}]))
    assert "triggers[0].type" in _paths(errs)


def test_error_empty_triggers():
    errs = validate_model(make_model(triggers=[]))
    assert "triggers" in _paths(errs)


def test_error_empty_actions():
    errs = validate_model(make_model(actions=[]))
    assert "actions" in _paths(errs)


def test_error_zone_missing_fields():
    errs = validate_model(make_model(
        triggers=[{"type": "zone", "entity_id": "person.user"}]))
    paths = _paths(errs)
    assert "triggers[0].zone" in paths
    assert "triggers[0].event" in paths


def test_error_unknown_action_type():
    errs = validate_model(make_model(actions=[{"type": "bogus"}]))
    assert "actions[0].type" in _paths(errs)


def test_error_unknown_condition_type():
    errs = validate_model(make_model(conditions=[{"type": "bogus"}]))
    assert "conditions[0].type" in _paths(errs)


def test_error_delay_missing_duration():
    errs = validate_model(make_model(actions=[{"type": "delay", "duration": dur()}]))
    assert "actions[0].duration" in _paths(errs)


# ---------------------------------------------------------------------------
# build_automation 은 유효하지 않으면 ValidationError 를 던진다
# ---------------------------------------------------------------------------
def test_build_raises_validation_error():
    with pytest.raises(ValidationError) as ei:
        build_automation(make_model(alias=""))
    assert ei.value.errors
    assert any(e["path"] == "alias" for e in ei.value.errors)


# ===========================================================================
# 강화된 검증: sun/time 조건 필수 필드, offset/time_pattern 형식,
#              trigger 조건 id 범위, service 필수 data (리뷰 확정 결함)
# ===========================================================================
def test_error_sun_condition_requires_after_or_before():
    # after/before 둘 다 없으면 오류 (item 5)
    errs = validate_model(make_model(conditions=[{"type": "sun"}]))
    assert "conditions[0]" in _paths(errs)


def test_sun_condition_ok_with_after_only():
    errs = validate_model(make_model(conditions=[{"type": "sun", "after": "sunset"}]))
    assert errs == []


def test_error_time_condition_requires_after_before_or_weekday():
    # after/before/weekday 모두 비어 있으면 오류 (item 6)
    errs = validate_model(make_model(conditions=[{"type": "time"}]))
    assert "conditions[0]" in _paths(errs)


def test_error_time_condition_empty_weekday_still_requires_something():
    errs = validate_model(make_model(conditions=[{"type": "time", "weekday": []}]))
    assert "conditions[0]" in _paths(errs)


def test_time_condition_ok_with_weekday_only():
    errs = validate_model(make_model(conditions=[{"type": "time", "weekday": ["mon"]}]))
    assert errs == []


def test_error_sun_trigger_bad_offset():
    # 트리거 offset 형식 검증 (item 7)
    errs = validate_model(make_model(
        triggers=[{"type": "sun", "event": "sunset", "offset": "abc"}]))
    assert "triggers[0].offset" in _paths(errs)


def test_sun_trigger_good_offset_ok():
    errs = validate_model(make_model(
        triggers=[{"type": "sun", "event": "sunset", "offset": "-00:45:00"}]))
    assert errs == []


def test_error_sun_condition_bad_after_offset():
    # 조건 after_offset 형식 검증 (item 7)
    errs = validate_model(make_model(conditions=[{
        "type": "sun", "after": "sunset", "after_offset": "12345"}]))
    assert "conditions[0].after_offset" in _paths(errs)


def test_error_time_pattern_bad_format():
    # time_pattern 값 형식 검증 (item 8)
    errs = validate_model(make_model(
        triggers=[{"type": "time_pattern", "minutes": "5분"}]))
    assert "triggers[0].minutes" in _paths(errs)


def test_time_pattern_slash_and_plain_ok():
    errs = validate_model(make_model(
        triggers=[{"type": "time_pattern", "minutes": "/5", "seconds": "0"}]))
    assert errs == []


def test_error_trigger_condition_id_out_of_range():
    # 트리거 1개(기본 FILLER)뿐인데 id "5" 참조 → 범위 밖 (item 9)
    errs = validate_model(make_model(conditions=[{"type": "trigger", "id": "5"}]))
    assert "conditions[0].id" in _paths(errs)


def test_error_trigger_condition_id_non_numeric():
    errs = validate_model(make_model(conditions=[{"type": "trigger", "id": "abc"}]))
    assert "conditions[0].id" in _paths(errs)


def test_trigger_condition_id_in_range_ok():
    m = make_model(
        triggers=[{"type": "time", "at": "07:00"},
                  {"type": "state", "entity_id": "light.x", "to": "on"}],
        conditions=[{"type": "trigger", "id": "1"}])
    assert validate_model(m) == []


def test_error_climate_set_temperature_requires_temperature():
    # 필수 data 표 (item 10)
    errs = validate_model(make_model(
        actions=[{"type": "service", "action": "climate.set_temperature"}]))
    assert "actions[0].data.temperature" in _paths(errs)


def test_climate_set_temperature_ok_with_data():
    errs = validate_model(make_model(
        actions=[{"type": "service", "action": "climate.set_temperature",
                  "data": {"temperature": 22}}]))
    assert errs == []


def test_error_media_volume_set_requires_volume_level():
    errs = validate_model(make_model(
        actions=[{"type": "service", "action": "media_player.volume_set",
                  "data": {}}]))
    assert "actions[0].data.volume_level" in _paths(errs)


def test_error_fan_percentage_and_humidifier_humidity():
    e1 = validate_model(make_model(
        actions=[{"type": "service", "action": "fan.set_percentage"}]))
    assert "actions[0].data.percentage" in _paths(e1)
    e2 = validate_model(make_model(
        actions=[{"type": "service", "action": "humidifier.set_humidity"}]))
    assert "actions[0].data.humidity" in _paths(e2)


def test_required_data_zero_value_is_valid():
    # 0 은 유효한 값(닫힘/음소거)이므로 통과해야 한다 (None 만 거부)
    errs = validate_model(make_model(
        actions=[{"type": "service", "action": "cover.set_cover_position",
                  "data": {"position": 0}}]))
    assert errs == []


def test_summarize_numeric_between_phrase():
    # numeric_state above+below → "사이가 되면" (item 11: 기존 의미가 반대였음)
    m = make_model(triggers=[{"type": "numeric_state", "entity_id": "sensor.x",
                              "above": 10, "below": 30}])
    s = summarize(m, {"sensor.x": "온도"})
    assert "10~30 사이가 되면" in s
    assert "벗어나" not in s
