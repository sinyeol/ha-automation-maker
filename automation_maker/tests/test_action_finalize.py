"""APP-PORT-PLAN S6 (액션 마감) 결정적 테스트.

  - 검증기(rule_model): light.turn_on data 범위(brightness_pct/step·rgb·kelvin·transition),
    절대·상대 밝기 동시 금지, notify.notify message 필수, homeassistant 도메인 허용(§3).
  - 도메인 안전 적대: homeassistant 편입이 기존 도메인 불일치 방어(fan.turn_on↔light 거부)를
    약화시키지 않는다(게이트4).
  - builder(automation_builder/ha_map): homeassistant.toggle/turn_on/off·notify.notify 왕복.
  - MockHAClient(§2.7): light rgb/kelvin/step 반영·notify 수신 로그·homeassistant.toggle 반전.
"""
from __future__ import annotations

import asyncio

import pytest

from datetime import datetime

from backend.automation_builder import (KNOWN_SERVICES, _REQUIRED_SERVICE_DATA,
                                        build_automation, validate_model)
from backend.engine.engine import RuleEngine
from backend.engine.ha_map import subrule_to_automation
from backend.engine.rule_model import _ALLOWED_ACTION_DOMAINS, validate_rule_model
from backend.engine.runlog import RunLog
from backend.engine.rule_store import RuleStore
from backend.engine.state_cache import StateCache
from backend.engine.storage import JsonStore
from backend.engine.variables import GlobalVars
from backend.ha_client import merge_inventory
from backend.mock_data import MockHAClient

_INV = {"entities": [{"entity_id": "light.a"}, {"entity_id": "fan.b"},
                     {"entity_id": "cover.c"}]}


def _model(action, data=None, target=None):
    a = {"type": "service", "action": action}
    if data is not None:
        a["data"] = data
    if target is not None:
        a["target"] = {"entity_id": target}
    return {"subrules": [{"triggers": [{"type": "daily", "at": "07:00"}],
                          "conditions": [], "actions": [a]}]}


def _msgs(model):
    return [e["message"] for e in validate_rule_model(model, _INV)]


# ===========================================================================
# 검증기 — homeassistant 도메인(§3.2·§3.4)
# ===========================================================================
def test_homeassistant_domain_whitelisted():
    assert "homeassistant" in _ALLOWED_ACTION_DOMAINS
    assert KNOWN_SERVICES.get("homeassistant") == ["turn_on", "turn_off", "toggle"]


def test_homeassistant_toggle_mixed_domain_ok():
    """혼합 도메인 대상 homeassistant.toggle 은 도메인 무관 서비스라 통과."""
    assert _msgs(_model("homeassistant.toggle", target=["light.a", "fan.b"])) == []
    assert _msgs(_model("homeassistant.turn_off", target=["light.a", "cover.c"])) == []


def test_domain_mismatch_still_rejected_adversarial():
    """게이트4: homeassistant 추가가 기존 도메인 안전을 약화시키지 않는다.
    도메인 특정 서비스(fan.turn_on)를 다른 도메인 엔티티(light)에 걸면 여전히 거부."""
    errs = _msgs(_model("fan.turn_on", target=["light.a"]))
    assert any("맞지 않아요" in m for m in errs)
    # light.turn_on 을 fan 엔티티에 거는 반대 방향도 거부.
    errs2 = _msgs(_model("light.turn_on", target=["fan.b"]))
    assert any("맞지 않아요" in m for m in errs2)


# ===========================================================================
# 검증기 — light.turn_on data 범위(§3.1)
# ===========================================================================
def test_light_data_ok():
    assert _msgs(_model("light.turn_on",
                        {"brightness_pct": 30, "rgb_color": [255, 0, 0]},
                        ["light.a"])) == []
    assert _msgs(_model("light.turn_on",
                        {"color_temp_kelvin": 2700, "transition": 5},
                        ["light.a"])) == []
    assert _msgs(_model("light.turn_on",
                        {"brightness_step_pct": -20}, ["light.a"])) == []
    # 경계값 허용.
    for data in ({"brightness_pct": 1}, {"brightness_pct": 100},
                 {"color_temp_kelvin": 2000}, {"color_temp_kelvin": 6500},
                 {"brightness_step_pct": -100}, {"brightness_step_pct": 100},
                 {"rgb_color": [0, 0, 0]}, {"rgb_color": [255, 255, 255]},
                 {"transition": 0}, {"transition": 2.5}):
        assert _msgs(_model("light.turn_on", data, ["light.a"])) == [], data


@pytest.mark.parametrize("data,frag", [
    ({"brightness_pct": 0}, "brightness_pct"),
    ({"brightness_pct": 101}, "brightness_pct"),
    ({"brightness_pct": 50.0}, "brightness_pct"),   # float 거부
    ({"brightness_step_pct": 0}, "brightness_step_pct"),
    ({"brightness_step_pct": 101}, "brightness_step_pct"),
    ({"brightness_step_pct": -101}, "brightness_step_pct"),
    ({"rgb_color": [256, 0, 0]}, "rgb_color"),
    ({"rgb_color": [-1, 0, 0]}, "rgb_color"),
    ({"rgb_color": [0, 0]}, "rgb_color"),            # 길이 오류
    ({"color_temp_kelvin": 1999}, "color_temp_kelvin"),
    ({"color_temp_kelvin": 6501}, "color_temp_kelvin"),
    ({"transition": -1}, "transition"),
])
def test_light_data_bad_ranges(data, frag):
    errs = validate_rule_model(_model("light.turn_on", data, ["light.a"]), _INV)
    assert any(frag in e["path"] for e in errs), (data, errs)


def test_light_brightness_abs_and_rel_mutually_exclusive():
    """§3.1: brightness_pct 와 brightness_step_pct 동시 지정 금지."""
    errs = _msgs(_model("light.turn_on",
                        {"brightness_pct": 50, "brightness_step_pct": 20},
                        ["light.a"]))
    assert any("동시에" in m for m in errs)


# ===========================================================================
# 검증기 — notify.notify message 필수(§3.3)
# ===========================================================================
def test_notify_requires_message():
    assert _msgs(_model("notify.notify", {"message": "욕실 누수"})) == []
    # message 누락·빈문자·공백 → 거부.
    assert any("메시지" in m for m in _msgs(_model("notify.notify", {"target": "mobile"})))
    assert any("메시지" in m for m in _msgs(_model("notify.notify", {"message": ""})))
    assert any("메시지" in m for m in _msgs(_model("notify.notify", {"message": "   "})))
    # 비문자열 message 도 거부.
    assert any("메시지" in m for m in _msgs(_model("notify.notify", {"message": 123})))


def test_notify_required_data_registered():
    assert _REQUIRED_SERVICE_DATA.get("notify.notify") == (
        "message", "알림 메시지를 입력해 주세요.")


# ===========================================================================
# builder 왕복(§2.6) — build_automation / ha_map
# ===========================================================================
def _wrap(actions):
    return {"alias": "t", "mode": "single",
            "triggers": [{"type": "time", "at": "07:00"}],
            "conditions": [], "actions": actions}


def test_build_automation_homeassistant_and_notify():
    model = _wrap([
        {"type": "service", "action": "homeassistant.toggle",
         "target": {"entity_id": ["light.a", "fan.b"]}},
        {"type": "service", "action": "notify.notify",
         "data": {"message": "hi", "target": "mobile"}}])
    assert validate_model(model) == []
    cfg = build_automation(model)
    assert cfg["actions"][0] == {"action": "homeassistant.toggle",
                                 "target": {"entity_id": ["light.a", "fan.b"]}}
    assert cfg["actions"][1] == {"action": "notify.notify",
                                 "data": {"message": "hi", "target": "mobile"}}


def test_build_automation_notify_missing_message_rejected():
    model = _wrap([{"type": "service", "action": "notify.notify",
                    "data": {"target": "mobile"}}])
    assert any("메시지" in e["message"] for e in validate_model(model))


def test_ha_map_passthrough_toggle_notify():
    out = subrule_to_automation({
        "triggers": [{"type": "sun", "event": "sunset"}], "conditions": [],
        "actions": [
            {"type": "service", "action": "homeassistant.toggle",
             "target": {"entity_id": ["light.a", "fan.b"]}},
            {"type": "service", "action": "notify.notify", "data": {"message": "hi"}}]})
    assert out["warnings"] == []
    assert [a["action"] for a in out["actions"]] == [
        "homeassistant.toggle", "notify.notify"]


# ===========================================================================
# MockHAClient 폐루프(§2.7)
# ===========================================================================
def _run(coro):
    return asyncio.run(coro)


def test_mock_light_reflects_color_kelvin_transition():
    ha = MockHAClient()
    _run(ha.call_service("light", "turn_on",
                         {"entity_id": "light.master_bedroom",
                          "rgb_color": [0, 0, 255], "transition": 3}))
    attrs = ha._states["light.master_bedroom"]["attributes"]
    assert ha._states["light.master_bedroom"]["state"] == "on"
    assert attrs["rgb_color"] == [0, 0, 255]
    assert attrs["transition"] == 3
    _run(ha.call_service("light", "turn_on",
                         {"entity_id": "light.entrance", "color_temp_kelvin": 2700}))
    assert ha._states["light.entrance"]["attributes"]["color_temp_kelvin"] == 2700


def test_mock_light_brightness_step_clamped():
    ha = MockHAClient()
    # 주방 조명: brightness 255(=100%). -30% → 70% → round(70*255/100)=178.
    _run(ha.call_service("light", "turn_on",
                         {"entity_id": "light.kitchen", "brightness_step_pct": -30}))
    assert ha._states["light.kitchen"]["attributes"]["brightness"] == 178
    # +50% 는 100% 상한 클램프 → 255.
    _run(ha.call_service("light", "turn_on",
                         {"entity_id": "light.kitchen", "brightness_step_pct": 50}))
    assert ha._states["light.kitchen"]["attributes"]["brightness"] == 255


def test_mock_notify_logged():
    ha = MockHAClient()
    _run(ha.call_service("notify", "notify",
                         {"message": "욕실 누수 발생", "title": "알림", "target": "mobile"}))
    assert len(ha.notifications) == 1
    n = ha.notifications[0]
    assert n["message"] == "욕실 누수 발생"
    assert n["target"] == "mobile"
    # notify 는 상태 훅을 발생시키지 않는다(대상 엔티티 없음).


def test_mock_homeassistant_toggle_flips_by_entity_domain():
    ha = MockHAClient()
    ha._states["fan.bathroom_fan"]["state"] = "off"
    ha._states["light.bathroom"]["state"] = "off"
    _run(ha.call_service("homeassistant", "toggle",
                         {"entity_id": ["fan.bathroom_fan", "light.bathroom"]}))
    assert ha._states["fan.bathroom_fan"]["state"] == "on"
    assert ha._states["light.bathroom"]["state"] == "on"
    # cover/lock 은 도메인별 반전(on/off 아님).
    ha._states["cover.living_room_curtain"]["state"] = "open"
    _run(ha.call_service("homeassistant", "toggle",
                         {"entity_id": ["cover.living_room_curtain", "lock.entrance_door"]}))
    assert ha._states["cover.living_room_curtain"]["state"] == "closed"
    assert ha._states["lock.entrance_door"]["state"] == "unlocked"


# ===========================================================================
# DEV 폐루프 E2E — 트리거 발화 → 엔진이 notify 실행 → MockHAClient 알림 로그(§5.6)
# ===========================================================================
class _FakeSource:
    def __init__(self, initial=None):
        self.initial = initial or []
        self._on_event = None

    async def start(self, on_event, on_resync, on_connect=None, on_disconnect=None):
        self._on_event = on_event
        on_resync(self.initial)
        if on_connect:
            on_connect()

    async def stop(self):
        pass

    def inject(self, entity_id, new_state, old_state):
        old = {"entity_id": entity_id, "state": old_state, "attributes": {}}
        new = {"entity_id": entity_id, "state": new_state, "attributes": {}}
        self._on_event(entity_id, old, new)


@pytest.mark.asyncio
async def test_dev_e2e_notify_closed_loop(v2_data_dir):
    """DEV 폐루프: 누수 감지(state 트리거) → 엔진이 notify.notify 실행 →
    MockHAClient.notifications 에 메시지 적재 + runlog 'fired'."""
    loop = asyncio.get_running_loop()
    ha = MockHAClient()
    reg = await ha.fetch_registries()
    states = await ha.get_states()
    inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
    now = datetime(2026, 7, 16, 12, 0, 0)
    rs = RuleStore(JsonStore(v2_data_dir / "rules.json", [], loop=loop))
    rl = RunLog(JsonStore(v2_data_dir / "runlog.json", [], loop=loop))
    gvars = GlobalVars({}, now_fn=lambda: now)
    engine = RuleEngine(rs, StateCache(), gvars, ha, lambda: inv, rl,
                        now_fn=lambda: now, loop=loop)
    rule = {"sentence": "누수 알림", "model": {
        "triggers": [{"type": "state", "entity_id": "binary_sensor.bathroom_moisture",
                      "to": "on"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "notify.notify",
                     "data": {"message": "욕실 누수 발생", "target": "mobile"}}]}}
    src = _FakeSource([{"entity_id": "binary_sensor.bathroom_moisture", "state": "off",
                        "attributes": {}}])
    await engine.start(src)
    saved = rs.upsert(dict(rule))
    engine.reload_rule(saved["id"])

    src.inject("binary_sensor.bathroom_moisture", "on", old_state="off")
    await asyncio.sleep(0.05)

    assert [n["message"] for n in ha.notifications] == ["욕실 누수 발생"]
    assert ha.notifications[0]["target"] == "mobile"
    assert "fired" in [e["result"] for e in rl.entries()]
    await engine.stop()
