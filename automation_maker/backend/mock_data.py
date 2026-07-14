"""DEV_MODE용 한국 아파트 목데이터 및 인메모리 MockHAClient."""
from __future__ import annotations

import copy
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 레지스트리: areas (7개, area_id는 영문 slug)
# ---------------------------------------------------------------------------
_AREAS = [
    {"area_id": "living_room", "name": "거실", "icon": "mdi:sofa"},
    {"area_id": "master_bedroom", "name": "안방", "icon": "mdi:bed-king"},
    {"area_id": "small_room", "name": "작은방", "icon": "mdi:bed"},
    {"area_id": "kitchen", "name": "주방", "icon": "mdi:countertop"},
    {"area_id": "entrance", "name": "현관", "icon": "mdi:door"},
    {"area_id": "veranda", "name": "베란다", "icon": "mdi:flower"},
    {"area_id": "bathroom", "name": "욕실", "icon": "mdi:shower"},
]

# ---------------------------------------------------------------------------
# 레지스트리: devices (일부 엔티티는 device를 통해 area를 상속)
# ---------------------------------------------------------------------------
_DEVICES = [
    {"id": "dev_wallpad", "name": "코콤 월패드", "name_by_user": None, "area_id": None},
    {"id": "dev_env_living", "name": "거실 환경센서", "name_by_user": None, "area_id": "living_room"},
    {"id": "dev_env_master", "name": "안방 환경센서", "name_by_user": None, "area_id": "master_bedroom"},
    {"id": "dev_erv", "name": "전열교환기", "name_by_user": None, "area_id": "veranda"},
]


def _ent(entity_id, area_id=None, device_id=None, name=None, category=None,
         disabled=None, hidden=None):
    return {
        "entity_id": entity_id,
        "area_id": area_id,
        "device_id": device_id,
        "name": None,
        "original_name": name,
        "entity_category": category,
        "disabled_by": disabled,
        "hidden_by": hidden,
        "platform": "mock",
    }


# ---------------------------------------------------------------------------
# 레지스트리: entities (노출 대상 30개 + 제외 확인용 2개)
# ---------------------------------------------------------------------------
_ENTITIES = [
    # 조명 8
    _ent("light.living_room_main", "living_room", "dev_wallpad", "거실 메인등"),
    _ent("light.living_room_mood", "living_room", None, "거실 무드등"),
    _ent("light.master_bedroom", "master_bedroom", "dev_wallpad", "안방 조명"),
    _ent("light.small_room", "small_room", "dev_wallpad", "작은방 조명"),
    _ent("light.kitchen", "kitchen", "dev_wallpad", "주방 조명"),
    _ent("light.entrance", "entrance", "dev_wallpad", "현관 조명"),
    _ent("light.veranda", "veranda", None, "베란다 조명"),
    _ent("light.bathroom", "bathroom", None, "욕실 조명"),
    # 감지기 (binary_sensor) 5 — 거실 모션은 area_id 없이 device로 상속
    _ent("binary_sensor.living_room_motion", None, "dev_env_living", "거실 모션"),
    _ent("binary_sensor.living_room_occupancy", "living_room", None, "거실 재실"),
    _ent("binary_sensor.entrance_door", "entrance", None, "현관문"),
    _ent("binary_sensor.small_room_window", "small_room", None, "작은방 창문"),
    _ent("binary_sensor.bathroom_moisture", "bathroom", None, "욕실 누수"),
    # 환경 센서 6
    _ent("sensor.living_room_temperature", None, "dev_env_living", "거실 온도"),
    _ent("sensor.living_room_humidity", None, "dev_env_living", "거실 습도"),
    _ent("sensor.master_bedroom_temperature", None, "dev_env_master", "안방 온도"),
    _ent("sensor.master_bedroom_humidity", None, "dev_env_master", "안방 습도"),
    _ent("sensor.kitchen_temperature", "kitchen", None, "주방 온도"),
    _ent("sensor.veranda_pm25", "veranda", None, "베란다 미세먼지"),
    # 미디어 1
    _ent("media_player.living_room_tv", "living_room", None, "거실 TV"),
    # 난방/공조 (climate) 2
    _ent("climate.living_room_boiler", "living_room", "dev_wallpad", "거실 보일러"),
    _ent("climate.master_bedroom_boiler", "master_bedroom", "dev_wallpad", "안방 보일러"),
    # 환기/팬 1
    _ent("fan.veranda_erv", None, "dev_erv", "전열교환기"),
    # 스위치/콘센트 2
    _ent("switch.gas_valve", "kitchen", "dev_wallpad", "가스밸브"),
    _ent("switch.standby_power", "living_room", None, "대기전력 콘센트"),
    # 커튼 1
    _ent("cover.living_room_curtain", "living_room", None, "거실 커튼"),
    # 잠금 1
    _ent("lock.entrance_door", "entrance", None, "현관 도어락"),
    # 사람/위치 1
    _ent("person.user", None, None, "나"),
    # 미배정 엔티티 2 (area_id None, device 없음)
    _ent("switch.unassigned_relay", None, None, "미배정 릴레이"),
    _ent("sensor.unassigned_power", None, None, "미배정 전력"),
    # 제외 확인용 (병합 시 빠져야 함)
    _ent("sensor.wallpad_signal", "living_room", "dev_wallpad", "월패드 신호", category="diagnostic"),
    _ent("light.disabled_spare", "small_room", None, "예비 조명", disabled="user"),
]


def _iso(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).isoformat()


def _st(entity_id, state, attrs=None):
    return {"entity_id": entity_id, "state": state, "attributes": attrs or {}}


# ---------------------------------------------------------------------------
# states (friendly_name / device_class / unit / 축약 attribute 포함)
# ---------------------------------------------------------------------------
_STATES = [
    _st("light.living_room_main", "off", {"friendly_name": "거실 메인등", "brightness": None}),
    _st("light.living_room_mood", "on", {"friendly_name": "거실 무드등", "brightness": 120}),
    _st("light.master_bedroom", "off", {"friendly_name": "안방 조명", "brightness": None}),
    _st("light.small_room", "off", {"friendly_name": "작은방 조명", "brightness": None}),
    _st("light.kitchen", "on", {"friendly_name": "주방 조명", "brightness": 255}),
    _st("light.entrance", "off", {"friendly_name": "현관 조명", "brightness": None}),
    _st("light.veranda", "off", {"friendly_name": "베란다 조명", "brightness": None}),
    _st("light.bathroom", "off", {"friendly_name": "욕실 조명", "brightness": None}),
    _st("binary_sensor.living_room_motion", "off",
        {"friendly_name": "거실 모션", "device_class": "motion"}),
    _st("binary_sensor.living_room_occupancy", "on",
        {"friendly_name": "거실 재실", "device_class": "occupancy"}),
    _st("binary_sensor.entrance_door", "off",
        {"friendly_name": "현관문", "device_class": "door"}),
    _st("binary_sensor.small_room_window", "off",
        {"friendly_name": "작은방 창문", "device_class": "window"}),
    _st("binary_sensor.bathroom_moisture", "off",
        {"friendly_name": "욕실 누수", "device_class": "moisture"}),
    _st("sensor.living_room_temperature", "23.4",
        {"friendly_name": "거실 온도", "device_class": "temperature", "unit_of_measurement": "°C"}),
    _st("sensor.living_room_humidity", "48",
        {"friendly_name": "거실 습도", "device_class": "humidity", "unit_of_measurement": "%"}),
    _st("sensor.master_bedroom_temperature", "22.1",
        {"friendly_name": "안방 온도", "device_class": "temperature", "unit_of_measurement": "°C"}),
    _st("sensor.master_bedroom_humidity", "51",
        {"friendly_name": "안방 습도", "device_class": "humidity", "unit_of_measurement": "%"}),
    _st("sensor.kitchen_temperature", "24.0",
        {"friendly_name": "주방 온도", "device_class": "temperature", "unit_of_measurement": "°C"}),
    _st("sensor.veranda_pm25", "17",
        {"friendly_name": "베란다 미세먼지", "device_class": "pm25", "unit_of_measurement": "µg/m³"}),
    _st("media_player.living_room_tv", "off", {"friendly_name": "거실 TV"}),
    _st("climate.living_room_boiler", "heat",
        {"friendly_name": "거실 보일러", "temperature": 22.0, "current_temperature": 23.4}),
    _st("climate.master_bedroom_boiler", "off",
        {"friendly_name": "안방 보일러", "temperature": 20.0, "current_temperature": 22.1}),
    _st("fan.veranda_erv", "off", {"friendly_name": "전열교환기", "percentage": 0}),
    _st("switch.gas_valve", "on", {"friendly_name": "가스밸브"}),
    _st("switch.standby_power", "on", {"friendly_name": "대기전력 콘센트"}),
    _st("cover.living_room_curtain", "closed", {"friendly_name": "거실 커튼"}),
    _st("lock.entrance_door", "locked", {"friendly_name": "현관 도어락"}),
    _st("person.user", "home", {"friendly_name": "나"}),
    # zone.*은 인벤토리에서 제외되지만 bootstrap의 zones 목록으로 내려간다.
    _st("zone.home", "1", {"friendly_name": "집"}),
    _st("zone.work", "0", {"friendly_name": "회사"}),
    _st("switch.unassigned_relay", "off", {"friendly_name": "미배정 릴레이"}),
    _st("sensor.unassigned_power", "12.5",
        {"friendly_name": "미배정 전력", "device_class": "power", "unit_of_measurement": "W"}),
    _st("sensor.wallpad_signal", "-62",
        {"friendly_name": "월패드 신호", "device_class": "signal_strength", "unit_of_measurement": "dBm"}),
    _st("light.disabled_spare", "unavailable", {"friendly_name": "예비 조명"}),
    # 기존 자동화 3개 (automation.* 엔티티)
    _st("automation.morning_routine", "on",
        {"friendly_name": "아침 루틴", "id": "mock_morning_0001",
         "last_triggered": _iso(2026, 7, 14, 7, 0)}),
    _st("automation.night_off", "on",
        {"friendly_name": "취침 소등", "id": "mock_night_0002",
         "last_triggered": _iso(2026, 7, 13, 23, 30)}),
    # YAML 수동 관리형: attributes.id 없음 → editable false
    _st("automation.legacy_yaml", "on",
        {"friendly_name": "YAML 관리 자동화", "last_triggered": None}),
]

# ---------------------------------------------------------------------------
# 편집 가능한 기존 자동화의 config (get_automation_config 대상)
# ---------------------------------------------------------------------------
_AUTOMATION_CONFIGS = {
    "mock_morning_0001": {
        "alias": "아침 루틴",
        "description": "평일 아침 7시에 거실 조명을 켭니다.",
        "mode": "single",
        "triggers": [{"trigger": "time", "at": "07:00:00"}],
        "conditions": [{"condition": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}],
        "actions": [{"action": "light.turn_on", "target": {"entity_id": ["light.living_room_main"]}}],
    },
    "mock_night_0002": {
        "alias": "취침 소등",
        "description": "밤 11시 30분에 모든 조명을 끕니다.",
        "mode": "single",
        "triggers": [{"trigger": "time", "at": "23:30:00"}],
        "conditions": [],
        "actions": [{"action": "light.turn_off",
                     "target": {"entity_id": ["light.living_room_main", "light.kitchen"]}}],
    },
}


class MockHAClient:
    """HAClient와 동일한 인터페이스. 저장은 인메모리 dict. CRUD 왕복/토글이 실제 반영됨."""

    def __init__(self):
        self._areas = copy.deepcopy(_AREAS)
        self._devices = copy.deepcopy(_DEVICES)
        self._entities = copy.deepcopy(_ENTITIES)
        self._states = {s["entity_id"]: copy.deepcopy(s) for s in _STATES}
        self._automations = copy.deepcopy(_AUTOMATION_CONFIGS)

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def get_states(self) -> list[dict]:
        return [copy.deepcopy(s) for s in self._states.values()]

    async def fetch_registries(self) -> dict:
        return {
            "areas": copy.deepcopy(self._areas),
            "devices": copy.deepcopy(self._devices),
            "entities": copy.deepcopy(self._entities),
        }

    async def get_automation_config(self, automation_id: str) -> dict | None:
        cfg = self._automations.get(automation_id)
        return copy.deepcopy(cfg) if cfg is not None else None

    def _entity_for(self, automation_id: str) -> str | None:
        for eid, s in self._states.items():
            if eid.startswith("automation.") and s.get("attributes", {}).get("id") == automation_id:
                return eid
        return None

    async def upsert_automation(self, automation_id: str, config: dict) -> None:
        body = {k: v for k, v in config.items() if k != "id"}
        self._automations[automation_id] = body
        alias = body.get("alias") or automation_id
        eid = self._entity_for(automation_id)
        if eid is None:
            eid = f"automation.{automation_id}"
            self._states[eid] = {
                "entity_id": eid, "state": "on",
                "attributes": {"id": automation_id, "friendly_name": alias, "last_triggered": None},
            }
        else:
            self._states[eid]["attributes"]["friendly_name"] = alias

    async def delete_automation(self, automation_id: str) -> None:
        self._automations.pop(automation_id, None)
        eid = self._entity_for(automation_id)
        if eid is not None:
            self._states.pop(eid, None)

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        targets = data.get("entity_id") if isinstance(data, dict) else None
        if isinstance(targets, str):
            targets = [targets]
        if not targets:
            return
        for eid in targets:
            st = self._states.get(eid)
            if st is None:
                continue
            if domain == "automation":
                if service == "turn_on":
                    st["state"] = "on"
                elif service == "turn_off":
                    st["state"] = "off"
                elif service == "trigger":
                    st["attributes"]["last_triggered"] = datetime.now(timezone.utc).isoformat()
