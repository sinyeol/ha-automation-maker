"""Phase 3A 정확도 이식 회귀 — parser_overlay(오프라인 프로토타입, 회귀 0)가
통과시킨 held-out 대표 승리를 **실제 파서**(오버레이 없이)로 재현한다.

- SPEC: tools/corpus/parser_overlay.py (A+B 규칙). 각 기대 model 은 parser_overlay 를
  동일 인벤토리에 돌린 결과와 대조해 확정한 값이다(병렬 이식이라 SPEC=overlay 결과).
- 인벤토리는 앱 MockHAClient(가스밸브/누수/TV 등 안전·미디어 엔티티 포함)를 그대로 쓴다.
- 이 테스트는 gazetteer/parser 이식이 반영된 실제 parse 를 기준으로 하므로, 이식이
  완료되어야 통과한다.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.ha_client import merge_inventory
from backend.mock_data import MockHAClient
from backend.nl import normalize as N
from backend.nl.gazetteer import Gazetteer
from backend.nl.parser import parse


# ---------------------------------------------------------------------------
# 실제 앱 인벤토리(MockHAClient) + settings. 오버레이 평가와 동일 구성.
# ---------------------------------------------------------------------------
_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user", "와이프": "person.wife"},
    "modes": {"슬립 모드": {"action": "scene.turn_on",
                         "target": {"entity_id": ["scene.sleep_mode"]}}},
    "near_home": {"zone_state": "home"},
    "aliases": [],
}

# 전체 조명(scope 케이스 골든). MockHAClient 의 활성 light 엔티티(disabled 제외) 순서.
_ALL_LIGHTS = [
    "light.living_room_main", "light.living_room_mood", "light.master_bedroom",
    "light.small_room", "light.kitchen", "light.entrance", "light.veranda",
    "light.bathroom",
]


def _build_inventory():
    ha = MockHAClient()

    async def _fetch():
        return await ha.fetch_registries(), await ha.get_states()

    reg, states = asyncio.run(_fetch())
    inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
    inv["zones"] = [
        {"entity_id": s["entity_id"],
         "name": s.get("attributes", {}).get("friendly_name", "")}
        for s in states if s["entity_id"].startswith("zone.")
    ]
    return inv


@pytest.fixture(scope="module")
def gz():
    return Gazetteer.build(_build_inventory(), _SETTINGS)


def _p(gz, sentence):
    return parse(sentence, gz, _SETTINGS, {})


def _svc(action, entity_ids, data=None):
    node = {"type": "service", "action": action,
            "target": {"entity_id": entity_ids}}
    if data is not None:
        node["data"] = data
    return node


# ===========================================================================
# A1/A2/A9 — 모드 극성 + 동의어
# ===========================================================================
def test_mode_release_trigger_off(gz):
    """A2: '해제되면' → 모드 off 트리거."""
    r = _p(gz, "취침모드가 해제되면 거실 조명을 켜줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [
        {"type": "mode", "mode": "슬립 모드", "to": "off"}]
    assert r["model"]["actions"] == [
        _svc("light.turn_on", ["light.living_room_main"])]


def test_mode_synonym_sleep_on_trigger(gz):
    """A1: '수면 모드'(동의어) + '켜지면' → 슬립 모드 on 트리거."""
    r = _p(gz, "수면 모드가 켜지면 안방 조명을 꺼줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [
        {"type": "mode", "mode": "슬립 모드", "to": "on"}]
    assert r["model"]["actions"] == [
        _svc("light.turn_off", ["light.master_bedroom"])]


# ===========================================================================
# B1 — zone 귀가/외출
# ===========================================================================
def test_zone_enter_default_person(gz):
    """B1: '집에 오면' → 기본 사용자 zone enter."""
    r = _p(gz, "집에 오면 현관 조명을 켜줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [
        {"type": "zone", "entity_id": "person.user", "zone": "zone.home",
         "event": "enter"}]
    assert r["model"]["actions"] == [
        _svc("light.turn_on", ["light.entrance"])]


def test_zone_leave_all_lights(gz):
    """B1+A10: '외출하면 모든 조명' → zone leave + 전체 조명 off."""
    r = _p(gz, "외출하면 모든 조명을 꺼줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [
        {"type": "zone", "entity_id": "person.user", "zone": "zone.home",
         "event": "leave"}]
    assert r["model"]["actions"] == [_svc("light.turn_off", _ALL_LIGHTS)]


def test_zone_enter_named_person(gz):
    """B1: '와이프가 퇴근하면' → person.wife zone enter."""
    r = _p(gz, "와이프가 퇴근하면 거실 조명 켜줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [
        {"type": "zone", "entity_id": "person.wife", "zone": "zone.home",
         "event": "enter"}]


# ===========================================================================
# B2 — 값(절반/최대/은은/위치/팬)
# ===========================================================================
def test_value_half_brightness(gz):
    r = _p(gz, "거실 조명 절반으로 켜줘")
    assert r["model"]["actions"] == [
        _svc("light.turn_on", ["light.living_room_main"], {"brightness_pct": 50})]


def test_value_max_brightness(gz):
    r = _p(gz, "거실 조명 최대로 켜줘")
    assert r["model"]["actions"] == [
        _svc("light.turn_on", ["light.living_room_main"], {"brightness_pct": 100})]


def test_value_dim_brightness(gz):
    r = _p(gz, "안방 조명 은은하게 켜줘")
    assert r["model"]["actions"] == [
        _svc("light.turn_on", ["light.master_bedroom"], {"brightness_pct": 20})]


def test_value_cover_position_half(gz):
    r = _p(gz, "거실 커튼 절반만 열어줘")
    assert r["model"]["actions"] == [
        _svc("cover.set_cover_position", ["cover.living_room_curtain"],
             {"position": 50})]


# ===========================================================================
# B3 — climate set_temperature
# ===========================================================================
def test_climate_set_temperature(gz):
    r = _p(gz, "거실 에어컨 26도로 맞춰줘")
    assert r["model"]["actions"] == [
        _svc("climate.set_temperature", ["climate.living_room_ac"],
             {"temperature": 26})]


# ===========================================================================
# B4/A8 — 시간대(시각 범위 → 선두 세그먼트 트리거로 정규화)
#   'A시부터 B시까지'는 이 설정에서 선두 시간대어로 정규화돼 segment 트리거가 된다
#   (overlay·실제 파서 동일 결과). A8 시간대 승격 경로 회귀.
# ===========================================================================
def test_time_segment_trigger(gz):
    r = _p(gz, "밤 10시부터 아침 6시까지 거실 조명을 꺼줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [{"type": "segment", "to": "night"}]
    assert r["model"]["actions"] == [
        _svc("light.turn_off", ["light.living_room_main"])]


# ===========================================================================
# B5 — 배제 스코프
# ===========================================================================
def test_scope_exclude_area(gz):
    """B5: '안방 빼고 다' → 조명 전체 중 안방 제외 off."""
    r = _p(gz, "안방 빼고 다 꺼줘")
    acts = r["model"]["actions"]
    assert len(acts) == 1
    assert acts[0]["action"] == "light.turn_off"
    ids = acts[0]["target"]["entity_id"]
    assert "light.master_bedroom" not in ids
    assert set(ids) == set(_ALL_LIGHTS) - {"light.master_bedroom"}


# ===========================================================================
# A3/A10 — 선두 전량 스코프(기기어 없는 '전부/다')
# ===========================================================================
def test_scope_all_bare(gz):
    for sent in ("전부 꺼줘", "다 꺼줘"):
        r = _p(gz, sent)
        assert r["model"]["actions"] == [_svc("light.turn_off", _ALL_LIGHTS)], sent


# ===========================================================================
# B6 — 안전(누수/가스 → 스위치 off)
# ===========================================================================
def test_safety_leak_trigger(gz):
    """B6: '누수가 감지되면 가스밸브를 잠가줘' → 누수 센서 on 트리거 + 밸브 off."""
    r = _p(gz, "누수가 감지되면 가스밸브를 잠가줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [
        {"type": "state", "entity_id": "binary_sensor.bathroom_moisture", "to": "on"}]
    assert r["model"]["actions"] == [
        _svc("switch.turn_off", ["switch.gas_valve"])]


def test_safety_gas_trigger(gz):
    r = _p(gz, "부엌에 가스가 감지되면 가스밸브를 꺼줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [
        {"type": "state", "entity_id": "switch.gas_valve", "to": "on"}]
    assert r["model"]["actions"] == [
        _svc("switch.turn_off", ["switch.gas_valve"])]


# ===========================================================================
# B7 — 미디어(TV 볼륨)
# ===========================================================================
def test_media_volume_set(gz):
    r = _p(gz, "거실 TV 볼륨을 20%로 해줘")
    assert r["model"]["actions"] == [
        _svc("media_player.volume_set", ["media_player.living_room_tv"],
             {"volume_level": 0.2})]


# ===========================================================================
# A6 — 조명 접미사(무드등/메인등/[방]등)
# ===========================================================================
def test_light_suffix_mood(gz):
    r = _p(gz, "거실 무드등 켜줘")
    assert r["model"]["actions"] == [
        _svc("light.turn_on", ["light.living_room_mood"])]


def test_light_suffix_main(gz):
    r = _p(gz, "거실 메인등 꺼줘")
    assert r["model"]["actions"] == [
        _svc("light.turn_off", ["light.living_room_main"])]


def test_light_suffix_room(gz):
    """A6: '[방]등'(안방등/거실등) → 방 접두 area + 조명."""
    assert _p(gz, "안방등 켜줘")["model"]["actions"] == [
        _svc("light.turn_on", ["light.master_bedroom"])]
    assert _p(gz, "거실등 꺼줘")["model"]["actions"] == [
        _svc("light.turn_off", ["light.living_room_main"])]


# ===========================================================================
# E5/영하 — numeric_state 음수 온도
# ===========================================================================
def test_numeric_below_freezing(gz):
    """영하 리터럴 → below 음수(numeric_state)."""
    r = _p(gz, "거실 온도가 영하 5도 밑으로 떨어지면 보일러를 켜줘")
    assert r["ok"] is True
    trg = r["model"]["triggers"]
    assert trg[0]["type"] == "numeric_state"
    assert trg[0]["entity_id"] == "sensor.living_room_temperature"
    assert trg[0]["below"] == -5.0


# ===========================================================================
# A4 — 지속시간 '간'/'동안'(state_held)
# ===========================================================================
def test_duration_gan_suffix(gz):
    """A4: '5분간' → 5분 state_held(동안 확장)."""
    r = _p(gz, "화장실에 5분간 사람이 없으면 환풍기를 꺼줘")
    assert r["ok"] is True
    assert r["model"]["triggers"] == [{
        "type": "state_held", "entity_id": "binary_sensor.bathroom_motion",
        "to": "off", "for": {"hours": 0, "minutes": 5, "seconds": 0}}]


# ===========================================================================
# normalize.py 헬퍼 단위 회귀 (B2 값 / 영하 온도 / duration 간 스칼라)
# ===========================================================================
def test_normalize_value_pct():
    assert N.value_pct("절반으로 켜줘") == 50
    assert N.value_pct("최대로 켜줘") == 100
    assert N.value_pct("은은하게 켜줘") == 20
    assert N.value_pct("30%로 켜줘") == 30
    assert N.value_pct("30으로 켜줘") == 30
    # 시각·온도 리터럴은 값이 아님
    assert N.value_pct("26도로 맞춰줘") is None
    assert N.value_pct("9시로 켜줘") is None
    assert N.value_pct("거실 조명 켜줘") is None


def test_normalize_value_dict():
    assert N.value_dict("절반으로 켜줘") == {"value": 50}
    assert N.value_dict("거실 조명 켜줘") is None


def test_normalize_find_temperature_signed():
    # 기본(부호 미반영) — 기존 동작 유지
    assert N.find_temperature("영하 5도")["value"] == 5.0
    assert N.find_temperature("26도")["value"] == 26.0
    # signed=True — 영하/마이너스 부호 반영
    assert N.find_temperature("영하 5도", signed=True)["value"] == -5.0
    assert N.find_temperature("마이너스 3도", signed=True)["value"] == -3.0
    assert N.find_temperature("26도", signed=True)["value"] == 26.0
    assert N.find_temperature("사람 없음", signed=True) is None


def test_normalize_duration_gan_scalar():
    # '동안'/'간' 접미사가 있어도 스칼라 추출은 동일(잉여 텍스트로 무시)
    assert N.find_duration("5분간")["seconds"] == 300
    assert N.find_duration("5분 동안")["seconds"] == 300
    assert N.find_duration("10분 동안 없으면")["value"] == 10
