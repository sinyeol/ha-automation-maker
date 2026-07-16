"""DEV_MODE aiohttp 앱(MockHAClient) REST 엔드포인트 테스트.

pytest-aiohttp 의 aiohttp_client 픽스처를 사용. 앱은 make_app() 팩스처(conftest)가
DEV_MODE=1 로 구성하므로 ingress_guard 를 통과한다.
"""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest
from multidict import CIMultiDict
from yarl import URL

from backend.app import error_middleware
from backend.automation_builder import KNOWN_SERVICES
from backend.ha_client import _automation_config_path
from tests.conftest import make_model

MOCK_AUTOMATION_ID = "mock_morning_0001"  # mock_data 의 편집 가능한 기존 자동화


# ---------------------------------------------------------------------------
# health / bootstrap
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    resp = await client.get("/api/health")
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "mode": "dev", "version": "3.0.0"}


@pytest.mark.asyncio
async def test_bootstrap_schema(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    resp = await client.get("/api/bootstrap")
    assert resp.status == 200
    body = await resp.json()

    assert body["mode"] == "dev"
    # areas: 7개
    assert len(body["areas"]) == 7
    assert {"area_id", "name", "icon"} <= set(body["areas"][0])

    # services 는 KNOWN_SERVICES 그대로
    assert body["services"] == KNOWN_SERVICES

    # zones: zone.* 상태에서 추출 (인벤토리와 별도)
    zones = {z["entity_id"]: z["name"] for z in body["zones"]}
    assert zones == {"zone.home": "집", "zone.work": "회사"}
    assert not any(e["entity_id"].startswith("zone.") for e in body["entities"])

    ents = {e["entity_id"]: e for e in body["entities"]}
    # 기대 엔티티 키
    sample = ents["light.living_room_main"]
    assert {"entity_id", "domain", "name", "area_id", "area_name", "device_id",
            "device_name", "device_class", "state", "unit", "attributes"} <= set(sample)
    assert sample["area_id"] == "living_room"
    assert sample["area_name"] == "거실"
    assert sample["state"] == "off"

    # 제외 규칙 반영
    assert "sensor.wallpad_signal" not in ents
    assert "light.disabled_spare" not in ents
    assert "switch.gas_valve" in ents

    # automations: 3개 (편집 불가 1개 포함)
    autos = {a["entity_id"]: a for a in body["automations"]}
    assert len(autos) == 3
    assert autos["automation.legacy_yaml"]["editable"] is False
    assert autos["automation.morning_routine"]["editable"] is True


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_preview_valid(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    model = make_model(
        alias="미리보기 테스트",
        triggers=[{"type": "state", "entity_id": "light.living_room_main", "to": "on"}],
        actions=[{"type": "service", "action": "light.turn_off",
                  "target": {"entity_id": ["light.kitchen"]}}],
    )
    resp = await client.post("/api/preview", json={"model": model})
    assert resp.status == 200
    body = await resp.json()
    assert body["errors"] == []
    assert body["yaml"].startswith("alias:")
    assert "미리보기 테스트" in body["yaml"]
    assert isinstance(body["summary"], str) and body["summary"]


@pytest.mark.asyncio
async def test_preview_invalid(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    # alias 없음, 트리거 빈 배열 → 검증 실패지만 preview 는 200
    model = make_model(alias="", triggers=[])
    resp = await client.post("/api/preview", json={"model": model})
    assert resp.status == 200
    body = await resp.json()
    assert body["yaml"] == ""
    assert len(body["errors"]) >= 1
    paths = {e["path"] for e in body["errors"]}
    assert "alias" in paths and "triggers" in paths


# ---------------------------------------------------------------------------
# CRUD 왕복 (생성 → 조회 → 목록 → 수정 → 삭제 → 404)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_crud_roundtrip(aiohttp_client, make_app, valid_model_payload):
    client = await aiohttp_client(make_app())

    # 생성
    resp = await client.post("/api/automations", json=valid_model_payload)
    assert resp.status == 200
    created = await resp.json()
    aid = created["id"]
    assert aid and created["yaml"].startswith("alias:")

    # 조회
    resp = await client.get(f"/api/automations/{aid}")
    assert resp.status == 200
    got = await resp.json()
    assert got["id"] == aid
    assert got["config"]["alias"] == "거실 저녁등"
    assert got["config"]["triggers"][0]["trigger"] == "state"

    # 목록에 노출
    resp = await client.get("/api/automations")
    listing = await resp.json()
    assert any(a["automation_id"] == aid for a in listing["automations"])

    # 수정 (alias 변경)
    upd = {"model": make_model(
        alias="거실 저녁등 수정",
        triggers=[{"type": "state", "entity_id": "light.living_room_main", "to": "on"}],
        actions=[{"type": "service", "action": "light.turn_off",
                  "target": {"entity_id": ["light.kitchen"]}}],
    )}
    resp = await client.put(f"/api/automations/{aid}", json=upd)
    assert resp.status == 200
    assert (await resp.json())["id"] == aid

    resp = await client.get(f"/api/automations/{aid}")
    assert (await resp.json())["config"]["alias"] == "거실 저녁등 수정"

    # 삭제
    resp = await client.delete(f"/api/automations/{aid}")
    assert resp.status == 200
    assert await resp.json() == {"ok": True}

    # 삭제 후 404
    resp = await client.get(f"/api/automations/{aid}")
    assert resp.status == 404
    assert (await resp.json())["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_create_invalid_model_400(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    resp = await client.post("/api/automations",
                             json={"model": make_model(alias="", actions=[])})
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == "invalid_model"
    assert body["error"]["errors"]


# ---------------------------------------------------------------------------
# toggle / run
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_toggle(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())

    resp = await client.post(f"/api/automations/{MOCK_AUTOMATION_ID}/toggle",
                             json={"on": False})
    assert resp.status == 200
    assert await resp.json() == {"ok": True}

    # 상태가 off 로 반영됐는지 목록에서 확인
    listing = await (await client.get("/api/automations")).json()
    morning = next(a for a in listing["automations"]
                   if a["automation_id"] == MOCK_AUTOMATION_ID)
    assert morning["state"] == "off"

    # 다시 켜기
    resp = await client.post(f"/api/automations/{MOCK_AUTOMATION_ID}/toggle",
                             json={"on": True})
    assert resp.status == 200
    listing = await (await client.get("/api/automations")).json()
    morning = next(a for a in listing["automations"]
                   if a["automation_id"] == MOCK_AUTOMATION_ID)
    assert morning["state"] == "on"


@pytest.mark.asyncio
async def test_run(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    resp = await client.post(f"/api/automations/{MOCK_AUTOMATION_ID}/run", json={})
    assert resp.status == 200
    assert await resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# 존재하지 않는 id → 404
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_missing_404(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    resp = await client.get("/api/automations/does_not_exist")
    assert resp.status == 404


@pytest.mark.asyncio
async def test_update_missing_404(aiohttp_client, make_app, valid_model_payload):
    client = await aiohttp_client(make_app())
    resp = await client.put("/api/automations/does_not_exist", json=valid_model_payload)
    assert resp.status == 404
    assert (await resp.json())["error"]["code"] == "not_found"


@pytest.mark.asyncio
async def test_toggle_missing_404(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    resp = await client.post("/api/automations/does_not_exist/toggle", json={"on": True})
    assert resp.status == 404


@pytest.mark.asyncio
async def test_run_missing_404(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    resp = await client.post("/api/automations/does_not_exist/run", json={})
    assert resp.status == 404


# ---------------------------------------------------------------------------
# preview / _read_model 방어 (item 2): body/model 이 dict 가 아니어도 500 금지
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_preview_body_not_dict_400(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    # body 가 리스트 → 400 bad_request (500 이 아니어야 함)
    resp = await client.post("/api/preview", data=json.dumps([1, 2, 3]),
                             headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == "bad_request"


@pytest.mark.asyncio
async def test_preview_model_not_dict_returns_200_errors(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    # model 이 문자열 → {} 로 정규화되어 검증 실패지만 preview 계약대로 200 + errors
    resp = await client.post("/api/preview", json={"model": "oops"})
    assert resp.status == 200
    body = await resp.json()
    assert body["yaml"] == ""
    assert len(body["errors"]) >= 1
    assert isinstance(body["summary"], str)


@pytest.mark.asyncio
async def test_create_body_not_dict_400(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    resp = await client.post("/api/automations", data=json.dumps("nope"),
                             headers={"Content-Type": "application/json"})
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == "bad_request"


@pytest.mark.asyncio
async def test_create_model_not_dict_400_invalid_model(aiohttp_client, make_app):
    client = await aiohttp_client(make_app())
    # model 이 dict 가 아니면 {} 로 정규화 → 검증 실패 → 400 invalid_model
    resp = await client.post("/api/automations", json={"model": [1, 2]})
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == "invalid_model"


# ---------------------------------------------------------------------------
# error_middleware: HA(ClientResponseError)·타임아웃·연결오류 매핑 (item 13)
# ---------------------------------------------------------------------------
def _client_response_error(status, message=""):
    ri = aiohttp.RequestInfo(URL("http://supervisor/core/x"), "GET",
                             CIMultiDict(), URL("http://supervisor/core/x"))
    return aiohttp.ClientResponseError(ri, (), status=status, message=message)


async def _raise(exc):
    async def _handler(_request):
        raise exc
    return _handler


@pytest.mark.asyncio
async def test_middleware_client_response_400_ha_rejected():
    handler = await _raise(_client_response_error(400, "잘못된 구성"))
    resp = await error_middleware(None, handler)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert body["error"]["code"] == "ha_rejected"
    assert "잘못된 구성" in body["error"]["message"]


@pytest.mark.asyncio
async def test_middleware_client_response_404_not_found():
    handler = await _raise(_client_response_error(404))
    resp = await error_middleware(None, handler)
    assert resp.status == 404
    assert json.loads(resp.body)["error"]["code"] == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 409, 500, 503])
async def test_middleware_client_response_other_502_ha_upstream(status):
    handler = await _raise(_client_response_error(status))
    resp = await error_middleware(None, handler)
    assert resp.status == 502
    assert json.loads(resp.body)["error"]["code"] == "ha_upstream"


@pytest.mark.asyncio
async def test_middleware_timeout_504():
    handler = await _raise(asyncio.TimeoutError())
    resp = await error_middleware(None, handler)
    assert resp.status == 504
    assert json.loads(resp.body)["error"]["code"] == "ha_timeout"


@pytest.mark.asyncio
async def test_middleware_connector_error_502():
    from aiohttp.client_reqrep import ConnectionKey
    ck = ConnectionKey("supervisor", 80, False, True, None, None, None)
    handler = await _raise(aiohttp.ClientConnectorError(ck, OSError("refused")))
    resp = await error_middleware(None, handler)
    assert resp.status == 502
    assert json.loads(resp.body)["error"]["code"] == "ha_unreachable"


# ---------------------------------------------------------------------------
# ha_client._automation_config_path: id 인용으로 supervisor 경로 이탈 방지 (item 14)
# ---------------------------------------------------------------------------
def test_automation_config_path_quotes_dangerous_ids():
    base = "/api/config/automation/config/"
    assert _automation_config_path("abc123") == base + "abc123"
    assert _automation_config_path("a/b") == base + "a%2Fb"
    assert _automation_config_path("../secret") == base + "..%2Fsecret"
    assert _automation_config_path("x?y#z") == base + "x%3Fy%23z"
    # 인용된 경로에는 경로 구분자 '/' 가 남지 않는다 (프리픽스 이후)
    assert "/" not in _automation_config_path("a/b/c").rsplit("config/", 1)[1]
