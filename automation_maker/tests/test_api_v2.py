"""v2 REST API (SPEC-V2 §7) 테스트 — register_v2_routes 로 배선된 DEV 앱.

핵심: parse → rules 저장 → dev/state 주입 → runlog 에 fired 기록 + mock 조명 상태 변화까지의
폐루프 1개를 포함한다. 배선은 conftest 의 make_v2_app 팩토리가 제공한다(통합 전에도 실행 가능).
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio

_LOOP_SENTENCE = "거실에 움직임이 감지되면 거실 조명을 켜줘"


# ---------------------------------------------------------------------------
# 상태 / 파싱
# ---------------------------------------------------------------------------
async def test_status(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    resp = await client.get("/api/v2/status")
    assert resp.status == 200
    body = await resp.json()
    assert body["connected"] is True
    assert "rules" in body and "active_timers" in body
    assert body["vars"]["segment"] in ("dawn", "morning", "day", "evening", "night")


async def test_parse_returns_model(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    resp = await client.post("/api/v2/parse", json={"sentence": _LOOP_SENTENCE})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["used_llm"] is False  # API 키 없음
    assert body["model"]["triggers"][0]["entity_id"] == "binary_sensor.living_room_motion"
    assert body["model"]["actions"][0]["action"] == "light.turn_on"


async def test_parse_bad_body(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    resp = await client.post("/api/v2/parse", json={"sentence": 123})
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == "bad_request"


async def test_parse_rejects_too_long_sentence(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    resp = await client.post("/api/v2/parse", json={"sentence": "가" * 501})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == "bad_request"
    assert "500" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 규칙 CRUD
# ---------------------------------------------------------------------------
async def test_rules_crud_roundtrip(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    model = {
        "triggers": [{"type": "state", "entity_id": "binary_sensor.living_room_motion",
                      "to": "on"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.living_room_main"]}}],
    }
    # create
    resp = await client.post("/api/v2/rules", json={
        "sentence": _LOOP_SENTENCE, "model": model,
        "area_id": "living_room", "category": "lighting"})
    assert resp.status == 200
    rule = (await resp.json())["rule"]
    rid = rule["id"]
    assert rule["sentence"] == _LOOP_SENTENCE
    assert rule["enabled"] is True

    # list
    resp = await client.get("/api/v2/rules")
    ids = [r["id"] for r in (await resp.json())["rules"]]
    assert rid in ids

    # update
    resp = await client.put(f"/api/v2/rules/{rid}", json={
        "sentence": "수정된 문장", "model": model,
        "area_id": "living_room", "category": "lighting"})
    assert resp.status == 200
    assert (await resp.json())["rule"]["sentence"] == "수정된 문장"

    # toggle off
    resp = await client.post(f"/api/v2/rules/{rid}/toggle", json={"on": False})
    assert resp.status == 200
    assert (await resp.json())["rule"]["enabled"] is False

    # run (조건 무시 액션 실행)
    resp = await client.post(f"/api/v2/rules/{rid}/run", json={})
    assert resp.status == 200
    assert (await resp.json())["ok"] is True

    # delete
    resp = await client.delete(f"/api/v2/rules/{rid}")
    assert resp.status == 200
    resp = await client.get("/api/v2/rules")
    assert rid not in [r["id"] for r in (await resp.json())["rules"]]


async def test_rules_create_invalid_400(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    # 트리거 없음 → 검증 실패
    resp = await client.post("/api/v2/rules", json={
        "sentence": "x", "model": {"triggers": [], "conditions": [],
                                    "actions": [{"type": "service", "action": "light.turn_on"}]}})
    assert resp.status == 400
    err = (await resp.json())["error"]
    assert err["code"] == "invalid_rule"
    assert err["errors"]


async def test_update_not_found_404(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    resp = await client.put("/api/v2/rules/deadbeef", json={
        "model": {"triggers": [{"type": "state", "entity_id": "binary_sensor.living_room_motion",
                                "to": "on"}], "conditions": [],
                  "actions": [{"type": "service", "action": "light.turn_on"}]}})
    assert resp.status == 404
    assert (await resp.json())["error"]["code"] == "not_found"


def _valid_model():
    return {
        "triggers": [{"type": "state", "entity_id": "binary_sensor.living_room_motion",
                      "to": "on"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.living_room_main"]}}],
    }


async def test_rules_create_rejects_unknown_service_domain(aiohttp_client, make_v2_app):
    # 화이트리스트 밖 도메인(hassio.*)은 검증에서 거부 → admin 권한 임의 서비스 실행 차단
    client = await aiohttp_client(make_v2_app())
    model = _valid_model()
    model["actions"] = [{"type": "service", "action": "hassio.addon_stop"}]
    resp = await client.post("/api/v2/rules", json={"sentence": "x", "model": model})
    assert resp.status == 400
    err = (await resp.json())["error"]
    assert err["code"] == "invalid_rule"
    assert any(e["message"] == "지원하지 않는 동작이에요: hassio.addon_stop"
               for e in err["errors"])


async def test_rules_create_allows_notify_domain(aiohttp_client, make_v2_app):
    # notify 는 KNOWN_SERVICES 에 없지만 화이트리스트에 추가된 도메인이라 통과해야 한다
    client = await aiohttp_client(make_v2_app())
    model = _valid_model()
    model["actions"] = [{"type": "service", "action": "notify.mobile_app"}]
    resp = await client.post("/api/v2/rules", json={"sentence": "x", "model": model})
    assert resp.status == 200


async def test_rules_create_rejects_too_long_sentence(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    resp = await client.post("/api/v2/rules", json={
        "sentence": "가" * 501, "model": _valid_model()})
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == "bad_request"


async def test_rules_create_rejects_when_at_capacity(aiohttp_client, make_v2_app):
    app = make_v2_app()
    client = await aiohttp_client(app)
    store = app["rule_store"]
    # 상한(200개)까지 직접 채운다(엔진 반영 없이 카운트만 확보).
    for i in range(200):
        store.upsert({"sentence": f"r{i}", "model": {}})
    resp = await client.post("/api/v2/rules", json={
        "sentence": "one more", "model": _valid_model()})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == "too_many_rules"
    assert "200" in body["error"]["message"]


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
async def test_settings_get_and_put(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    resp = await client.get("/api/v2/settings")
    assert resp.status == 200
    body = await resp.json()
    assert "segments" in body
    assert body["llm_available"] is False

    resp = await client.put("/api/v2/settings", json={
        "aliases": [{"surface": "안방 무드등", "entity_id": "light.master_bedroom"}]})
    assert resp.status == 200
    out = await resp.json()
    assert out["aliases"] == [{"surface": "안방 무드등", "entity_id": "light.master_bedroom"}]


# ---------------------------------------------------------------------------
# dev/state 게이팅
# ---------------------------------------------------------------------------
async def test_dev_state_blocked_when_not_dev(aiohttp_client, make_v2_app):
    app = make_v2_app()
    app["dev_mode"] = False  # 비 DEV 모사
    client = await aiohttp_client(app)
    resp = await client.post("/api/v2/dev/state", json={
        "entity_id": "binary_sensor.living_room_motion", "state": "on"})
    assert resp.status == 404


# ---------------------------------------------------------------------------
# 폐루프: parse → 저장 → dev/state 주입 → runlog fired + 조명 상태 변화
# ---------------------------------------------------------------------------
async def test_closed_loop_motion_to_light(aiohttp_client, make_v2_app):
    app = make_v2_app()
    client = await aiohttp_client(app)
    ha = app["ha"]

    # 1) 문장 해석
    resp = await client.post("/api/v2/parse", json={"sentence": _LOOP_SENTENCE})
    parsed = await resp.json()
    assert parsed["ok"] is True
    model = parsed["model"]

    # 2) 규칙 저장(엔진 반영)
    resp = await client.post("/api/v2/rules", json={
        "sentence": _LOOP_SENTENCE, "model": model,
        "area_id": parsed["area_id"], "category": parsed["category"]})
    assert resp.status == 200
    rid = (await resp.json())["rule"]["id"]

    # 조명 초기 상태 off
    assert ha._states["light.living_room_main"]["state"] == "off"

    # 3) 모션 on 주입
    resp = await client.post("/api/v2/dev/state", json={
        "entity_id": "binary_sensor.living_room_motion", "state": "on"})
    assert resp.status == 200

    # 4) 짧은 대기 후 발화 확인
    await asyncio.sleep(0.1)

    # runlog 에 fired 항목
    resp = await client.get("/api/v2/runlog")
    entries = (await resp.json())["entries"]
    fired = [e for e in entries if e["result"] == "fired" and e["rule_id"] == rid]
    assert fired, f"fired 로그가 없습니다: {entries}"

    # mock 조명 상태 변화(off → on)
    assert ha._states["light.living_room_main"]["state"] == "on"
    assert ("light", "turn_on", {"entity_id": ["light.living_room_main"]}) in ha.service_calls


async def test_closed_loop_no_fire_when_disabled(aiohttp_client, make_v2_app):
    # 토글로 비활성화된 규칙은 발화하지 않는다
    app = make_v2_app()
    client = await aiohttp_client(app)
    ha = app["ha"]
    model = {
        "triggers": [{"type": "state", "entity_id": "binary_sensor.living_room_motion",
                      "to": "on"}], "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.living_room_main"]}}]}
    resp = await client.post("/api/v2/rules", json={"sentence": _LOOP_SENTENCE, "model": model})
    rid = (await resp.json())["rule"]["id"]
    await client.post(f"/api/v2/rules/{rid}/toggle", json={"on": False})

    await client.post("/api/v2/dev/state", json={
        "entity_id": "binary_sensor.living_room_motion", "state": "on"})
    await asyncio.sleep(0.08)

    assert ha._states["light.living_room_main"]["state"] == "off"
