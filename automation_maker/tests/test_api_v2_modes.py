"""SPEC-V3 API 확장 (§5·§1.4·§3.4) 테스트 — modes/tokenize/build + 모드 자동등록.

make_v2_app 팩토리(conftest)가 ModeState 를 배선한 DEV 앱을 제공한다.
"""
from __future__ import annotations

import copy

import pytest

pytestmark = pytest.mark.asyncio


_SETTINGS_WITH_MODE = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user"}, "near_home": {"zone_state": "home"},
    "aliases": [],
    "modes": {"슬립 모드": {"initial": "off",
                          "on_action": {"action": "scene.turn_on",
                                        "target": {"entity_id": ["scene.sleep_mode"]}},
                          "off_action": None}},
}


# ---------------------------------------------------------------------------
# GET api/v2/modes
# ---------------------------------------------------------------------------
async def test_modes_list(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    resp = await client.get("/api/v2/modes")
    assert resp.status == 200
    modes = (await resp.json())["modes"]
    assert len(modes) == 1
    m = modes[0]
    assert m["name"] == "슬립 모드"
    assert m["state"] == "off"
    assert m["initial"] == "off"
    assert m["has_on_action"] is True
    assert m["has_off_action"] is False


# ---------------------------------------------------------------------------
# POST api/v2/modes/{name} — 수동 토글
# ---------------------------------------------------------------------------
async def test_mode_toggle(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    resp = await client.post("/api/v2/modes/슬립 모드", json={"on": True})
    assert resp.status == 200
    assert (await resp.json())["modes"]["슬립 모드"] == "on"

    # 상태 반영 확인
    resp = await client.get("/api/v2/modes")
    modes = {m["name"]: m["state"] for m in (await resp.json())["modes"]}
    assert modes["슬립 모드"] == "on"

    # off 로 토글
    resp = await client.post("/api/v2/modes/슬립 모드", json={"on": False})
    assert (await resp.json())["modes"]["슬립 모드"] == "off"


async def test_status_includes_modes(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    resp = await client.get("/api/v2/status")
    body = await resp.json()
    assert body["modes"] == {"슬립 모드": "off"}
    await client.post("/api/v2/modes/슬립 모드", json={"on": True})
    body = await (await client.get("/api/v2/status")).json()
    assert body["modes"]["슬립 모드"] == "on"


# ---------------------------------------------------------------------------
# POST api/v2/tokenize
# ---------------------------------------------------------------------------
async def test_tokenize_endpoint(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    resp = await client.post("/api/v2/tokenize", json={"sentence": "거실조명은 켜줘"})
    assert resp.status == 200
    body = await resp.json()
    assert [t["text"] for t in body["tokens"]] == ["거실조명은", "켜줘"]
    assert body["tokens"][0]["core"] == "거실조명"
    assert len(body["suggestions"]) == 2


async def test_tokenize_bad_body(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    resp = await client.post("/api/v2/tokenize", json={"sentence": 123})
    assert resp.status == 400


# ---------------------------------------------------------------------------
# POST api/v2/build
# ---------------------------------------------------------------------------
async def test_build_endpoint(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    assignments = [
        {"index": 0, "role": "trigger_entity",
         "ref": "binary_sensor.living_room_motion"},
        {"index": 1, "role": "event_state", "state": "on"},
        {"index": 2, "role": "action_target", "ref": "light.living_room_main"},
        {"index": 3, "role": "action_verb", "verb": "on"},
    ]
    resp = await client.post("/api/v2/build",
                             json={"sentence": "거실 움직이면 켜줘",
                                   "assignments": assignments})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["model"]["triggers"][0]["entity_id"] == "binary_sensor.living_room_motion"
    assert body["model"]["actions"][0]["action"] == "light.turn_on"


async def test_build_bad_assignments(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    resp = await client.post("/api/v2/build",
                             json={"sentence": "x", "assignments": "notalist"})
    assert resp.status == 400


async def test_build_non_dict_assignment_element_is_400(aiohttp_client, make_v2_app):
    """[Fix3] assignments 원소가 dict 가 아니면 500 이 아니라 400(bad_request)이어야 한다."""
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    resp = await client.post("/api/v2/build",
                             json={"sentence": "x", "assignments": ["oops"]})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == "bad_request"
    # 다른 non-dict 원소(숫자·null)도 동일하게 400.
    resp2 = await client.post("/api/v2/build",
                              json={"sentence": "x", "assignments": [{"index": 0,
                                    "role": "ignore"}, 123, None]})
    assert resp2.status == 400


# ---------------------------------------------------------------------------
# 모드 자동 등록 (§5) — 저장 시 참조 모드가 settings 에 없으면 추가
# ---------------------------------------------------------------------------
async def test_autoregister_mode_on_save(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    # "외출 모드" 는 settings 에 없다. set_mode 액션이 이를 참조.
    model = {
        "alias": "", "mode": "single",
        "triggers": [{"type": "state",
                      "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "set_mode", "mode": "외출 모드", "to": "on"}],
    }
    resp = await client.post("/api/v2/rules",
                             json={"sentence": "움직이면 외출 모드 켜", "model": model})
    assert resp.status == 200
    # 자동 등록으로 검증 통과 + 규칙 저장
    body = await resp.json()
    assert "rule" in body

    # GET modes 에 새 모드가 초기값 off 로 나타난다.
    modes = {m["name"]: m for m in (await (await client.get("/api/v2/modes")).json())["modes"]}
    assert "외출 모드" in modes
    assert modes["외출 모드"]["state"] == "off"
    assert modes["외출 모드"]["initial"] == "off"


async def test_reject_unknown_mode_without_autoregister_reference(aiohttp_client, make_v2_app):
    """참조되지 않는 모드는 자동등록 대상이 아니며, mode 조건이 없는 모델은 그대로 저장된다."""
    client = await aiohttp_client(make_v2_app(copy.deepcopy(_SETTINGS_WITH_MODE)))
    model = {
        "alias": "", "mode": "single",
        "triggers": [{"type": "state",
                      "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.living_room_main"]}}],
    }
    resp = await client.post("/api/v2/rules",
                             json={"sentence": "움직이면 켜", "model": model})
    assert resp.status == 200
    # 모드는 여전히 슬립 모드 하나뿐
    modes = (await (await client.get("/api/v2/modes")).json())["modes"]
    assert [m["name"] for m in modes] == ["슬립 모드"]
