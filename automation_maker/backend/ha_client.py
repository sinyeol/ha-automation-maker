"""Supervisor 프록시 클라이언트(REST + WebSocket 레지스트리) 및 인벤토리 병합 순수 함수."""
from __future__ import annotations

import os
from urllib.parse import quote

import aiohttp

# merge_inventory에서 제외할 도메인
EXCLUDED_DOMAINS = {
    "automation", "update", "tts", "stt", "conversation",
    "assist_satellite", "zone", "persistent_notification",
}

# 도메인별로 UI에 노출할 축약 attribute 키 (supported_features 등은 제외)
_DOMAIN_ATTRS = {
    "light": ["brightness"],
    "climate": ["temperature", "current_temperature"],
    "fan": ["percentage"],
}


def _automation_config_path(automation_id: str) -> str:
    """automation config REST 경로. id를 완전히 인용해 supervisor 경로 이탈('/', '..', '?', '#')을 막는다.

    프론트의 URL 인코딩은 aiohttp가 match_info에서 디코드해 버리므로 서버측 인용이 필수다.
    """
    return f"/api/config/automation/config/{quote(automation_id, safe='')}"


class HAClient:
    def __init__(self, base_url: str = "http://supervisor/core", token: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.token = token if token is not None else os.environ.get("SUPERVISOR_TOKEN")
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(headers=headers, timeout=timeout)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_states(self) -> list[dict]:
        async with self._session.get(f"{self.base_url}/api/states") as r:
            r.raise_for_status()
            return await r.json()

    async def fetch_registries(self) -> dict:
        ws_url = self.base_url.replace("https://", "wss://").replace("http://", "ws://") + "/websocket"
        out: dict = {"areas": [], "devices": [], "entities": []}
        commands = [
            ("areas", "config/area_registry/list"),
            ("devices", "config/device_registry/list"),
            ("entities", "config/entity_registry/list"),
        ]
        async with self._session.ws_connect(ws_url, receive_timeout=10) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": self.token})
            await ws.receive_json()  # auth_ok
            msg_id = 1
            for key, cmd in commands:
                await ws.send_json({"id": msg_id, "type": cmd})
                while True:
                    m = await ws.receive_json()
                    if m.get("id") == msg_id and m.get("type") == "result":
                        out[key] = m.get("result", []) or []
                        break
                msg_id += 1
        return out

    async def get_automation_config(self, automation_id: str) -> dict | None:
        url = self.base_url + _automation_config_path(automation_id)
        async with self._session.get(url) as r:
            if r.status == 404:
                return None
            r.raise_for_status()
            return await r.json()

    async def upsert_automation(self, automation_id: str, config: dict) -> None:
        body = {k: v for k, v in config.items() if k != "id"}
        url = self.base_url + _automation_config_path(automation_id)
        async with self._session.post(url, json=body) as r:
            r.raise_for_status()

    async def delete_automation(self, automation_id: str) -> None:
        url = self.base_url + _automation_config_path(automation_id)
        async with self._session.delete(url) as r:
            r.raise_for_status()

    async def call_service(self, domain: str, service: str, data: dict) -> None:
        url = f"{self.base_url}/api/services/{domain}/{service}"
        async with self._session.post(url, json=data) as r:
            r.raise_for_status()


def merge_inventory(areas: list, devices: list, entities: list, states: list) -> dict:
    """레지스트리(area/device/entity) + states 병합 → bootstrap의 areas/entities."""
    state_by_id = {s["entity_id"]: s for s in states}
    device_by_id = {d["id"]: d for d in devices}
    area_name_by_id = {a["area_id"]: (a.get("name") or a["area_id"]) for a in areas}

    area_out = [
        {"area_id": a["area_id"], "name": a.get("name") or a["area_id"], "icon": a.get("icon")}
        for a in areas
    ]

    ent_out = []
    for e in entities:
        if e.get("disabled_by"):
            continue
        if e.get("hidden_by"):
            continue
        if e.get("entity_category") in ("diagnostic", "config"):
            continue
        eid = e["entity_id"]
        domain = eid.split(".", 1)[0]
        if domain in EXCLUDED_DOMAINS:
            continue

        dev = device_by_id.get(e.get("device_id"))
        area_id = e.get("area_id")
        if not area_id and dev:
            area_id = dev.get("area_id")

        st = state_by_id.get(eid)
        attrs = st.get("attributes", {}) if st else {}
        name = (attrs.get("friendly_name") or e.get("name")
                or e.get("original_name") or eid)
        slim = {k: attrs.get(k) for k in _DOMAIN_ATTRS.get(domain, [])}

        ent_out.append({
            "entity_id": eid,
            "domain": domain,
            "name": name,
            "area_id": area_id,
            "area_name": area_name_by_id.get(area_id),
            "device_id": e.get("device_id"),
            "device_name": (dev.get("name_by_user") or dev.get("name")) if dev else None,
            "device_class": attrs.get("device_class"),
            "state": st.get("state") if st else None,
            "unit": attrs.get("unit_of_measurement"),
            "attributes": slim,
        })

    return {"areas": area_out, "entities": ent_out}
