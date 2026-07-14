"""merge_inventory 순수 함수 테스트.

목데이터(MockHAClient) 기반 통합 확인 + 소규모 수작업 데이터로 우선순위/상속 규칙 검증.
"""
from __future__ import annotations

import asyncio

from backend.ha_client import merge_inventory
from backend.mock_data import MockHAClient


# ---------------------------------------------------------------------------
# 목데이터 기반: 실제 병합 결과 검증
# ---------------------------------------------------------------------------
def _merged_from_mock():
    ha = MockHAClient()

    async def _go():
        reg = await ha.fetch_registries()
        states = await ha.get_states()
        return merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)

    return asyncio.run(_go())


def _by_id(entities):
    return {e["entity_id"]: e for e in entities}


def test_areas_passthrough():
    inv = _merged_from_mock()
    ids = {a["area_id"] for a in inv["areas"]}
    assert {"living_room", "master_bedroom", "kitchen", "bathroom"} <= ids
    lr = next(a for a in inv["areas"] if a["area_id"] == "living_room")
    assert lr["name"] == "거실"
    assert lr["icon"] == "mdi:sofa"


def test_device_area_inheritance():
    # binary_sensor.living_room_motion 은 entity.area_id 가 없고 device 로 상속
    inv = _merged_from_mock()
    ent = _by_id(inv["entities"])["binary_sensor.living_room_motion"]
    assert ent["area_id"] == "living_room"
    assert ent["area_name"] == "거실"
    # sensor.living_room_temperature 도 device(dev_env_living) 로 상속
    temp = _by_id(inv["entities"])["sensor.living_room_temperature"]
    assert temp["area_id"] == "living_room"


def test_device_class_from_states():
    # device_class 는 레지스트리에 없고 states.attributes 에서 병합되어야 한다
    inv = _merged_from_mock()
    by = _by_id(inv["entities"])
    assert by["binary_sensor.living_room_motion"]["device_class"] == "motion"
    assert by["sensor.living_room_temperature"]["device_class"] == "temperature"
    assert by["sensor.veranda_pm25"]["device_class"] == "pm25"
    # device_class 가 없는 조명은 None
    assert by["light.living_room_main"]["device_class"] is None


def test_unit_and_slim_attributes():
    inv = _merged_from_mock()
    by = _by_id(inv["entities"])
    temp = by["sensor.living_room_temperature"]
    assert temp["unit"] == "°C"
    # light 의 축약 attribute 는 brightness 만
    assert set(by["light.living_room_mood"]["attributes"]) == {"brightness"}
    assert by["light.living_room_mood"]["attributes"]["brightness"] == 120
    # climate 는 temperature/current_temperature
    clim = by["climate.living_room_boiler"]
    assert set(clim["attributes"]) == {"temperature", "current_temperature"}


def test_diagnostic_and_disabled_excluded():
    inv = _merged_from_mock()
    ids = set(_by_id(inv["entities"]))
    assert "sensor.wallpad_signal" not in ids   # entity_category=diagnostic
    assert "light.disabled_spare" not in ids     # disabled_by=user


def test_automation_domain_excluded():
    inv = _merged_from_mock()
    assert not any(e["entity_id"].startswith("automation.") for e in inv["entities"])


def test_unassigned_entities_have_null_area():
    inv = _merged_from_mock()
    by = _by_id(inv["entities"])
    relay = by["switch.unassigned_relay"]
    assert relay["area_id"] is None
    assert relay["area_name"] is None


def test_state_and_friendly_name_present():
    inv = _merged_from_mock()
    by = _by_id(inv["entities"])
    assert by["switch.gas_valve"]["state"] == "on"
    assert by["switch.gas_valve"]["name"] == "가스밸브"


# ---------------------------------------------------------------------------
# 수작업 데이터: 이름 우선순위 / 미배정 / states 없는 엔티티
# ---------------------------------------------------------------------------
def test_name_priority_friendly_over_all():
    areas = [{"area_id": "a1", "name": "방1"}]
    devices = [{"id": "d1", "name": "기기1", "area_id": "a1"}]
    entities = [{
        "entity_id": "light.p", "area_id": None, "device_id": "d1",
        "name": "레지스트리이름", "original_name": "원래이름",
    }]
    states = [{"entity_id": "light.p", "state": "on",
               "attributes": {"friendly_name": "프렌들리이름"}}]
    inv = merge_inventory(areas, devices, entities, states)
    assert inv["entities"][0]["name"] == "프렌들리이름"


def test_name_priority_entity_name_then_original_then_id():
    areas = []
    devices = []
    # friendly_name 없음 → entity.name
    e1 = {"entity_id": "light.a", "name": "엔티티이름", "original_name": "원본"}
    # friendly_name·name 없음 → original_name
    e2 = {"entity_id": "light.b", "name": None, "original_name": "원본B"}
    # 아무 이름도 없음 → entity_id
    e3 = {"entity_id": "light.c", "name": None, "original_name": None}
    states = [
        {"entity_id": "light.a", "state": "on", "attributes": {}},
        {"entity_id": "light.b", "state": "on", "attributes": {}},
        {"entity_id": "light.c", "state": "on", "attributes": {}},
    ]
    inv = merge_inventory(areas, devices, [e1, e2, e3], states)
    names = {e["entity_id"]: e["name"] for e in inv["entities"]}
    assert names["light.a"] == "엔티티이름"
    assert names["light.b"] == "원본B"
    assert names["light.c"] == "light.c"


def test_entity_without_state_has_none_state():
    # states 목록에 없는 엔티티는 state=None (unavailable 아님)
    areas = []
    devices = []
    entities = [{"entity_id": "light.ghost", "name": "유령"}]
    inv = merge_inventory(areas, devices, entities, states=[])
    ghost = inv["entities"][0]
    assert ghost["state"] is None
    assert ghost["device_class"] is None


def test_hidden_by_excluded():
    entities = [{"entity_id": "light.h", "hidden_by": "user"}]
    states = [{"entity_id": "light.h", "state": "on", "attributes": {}}]
    inv = merge_inventory([], [], entities, states)
    assert inv["entities"] == []


def test_entity_area_id_takes_precedence_over_device():
    # entity.area_id 가 있으면 device.area_id 보다 우선
    areas = [{"area_id": "room_a", "name": "A"}, {"area_id": "room_b", "name": "B"}]
    devices = [{"id": "d1", "name": "기기", "area_id": "room_b"}]
    entities = [{"entity_id": "light.x", "area_id": "room_a", "device_id": "d1"}]
    states = [{"entity_id": "light.x", "state": "on", "attributes": {}}]
    inv = merge_inventory(areas, devices, entities, states)
    assert inv["entities"][0]["area_id"] == "room_a"
    assert inv["entities"][0]["area_name"] == "A"
