"""§6.4 선택 기능: Anthropic Messages API로 로컬 파서 결과 보조.

로컬 confidence < 0.6 이거나 unresolved 스팬이 있을 때만 api_v2가 호출한다.
실패는 조용히 None을 돌려주고 로컬 결과를 쓴다. API 키는 로그에 남기지 않는다.
aiohttp 만 외부 의존(다른 nl 모듈은 표준 라이브러리만).
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

_API_URL = "https://api.anthropic.com/v1/messages"
_MODEL = "claude-haiku-4-5-20251001"
_TIMEOUT = 20

# ParseResult의 model/chips 스키마를 tool 로 강제
_TOOL = {
    "name": "emit_rule",
    "description": "한국어 자동화 문장을 RuleModel과 chips로 변환한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "model": {
                "type": "object",
                "properties": {
                    "triggers": {"type": "array", "items": {"type": "object"}},
                    "condition_mode": {"type": "string", "enum": ["and", "or"]},
                    "conditions": {"type": "array", "items": {"type": "object"}},
                    "actions": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["triggers", "actions"],
            },
            "chips": {"type": "array", "items": {"type": "object"}},
            "summary": {"type": "string"},
        },
        "required": ["model"],
    },
}

_SYSTEM = (
    "너는 홈어시스턴트 자동화를 만드는 한국어 파서다. 주어진 문장을 RuleModel(JSON)로 바꾼다. "
    "트리거 타입: state{entity_id,to}, state_held{entity_id,to,for}, "
    "group_held{scope,to,for}, numeric_state{entity_id,above,below}, zone{entity_id,zone,event}. "
    "조건 타입: state, numeric_state, time{after,before}, time_segment{segments}, day_type, season. "
    "액션: service{action,target:{entity_id:[...]},data}. Duration은 {hours,minutes,seconds}. "
    "entity_id 는 반드시 제공된 인벤토리의 실제 id만 쓴다. 모르면 비운다. "
    "sun/template 은 쓰지 않는다. 반드시 emit_rule 도구로만 답한다."
)


def _digest_text(inventory_digest: dict, sentence: str) -> str:
    ents = inventory_digest.get("entities", [])
    # 200개 초과 시 문장 매칭 가능성 있는 것 우선
    if len(ents) > 200:
        def rel(e):
            nm = e.get("name", "")
            return any(part and part in sentence for part in (nm, (e.get("area_name") or "")))
        ents = sorted(ents, key=lambda e: not rel(e))[:200]
    lines = []
    for e in ents:
        lines.append(f"{e.get('entity_id')} | {e.get('name')} | 방:{e.get('area_name')} | "
                     f"종류:{e.get('device_class') or e.get('domain')}")
    persons = inventory_digest.get("persons") or {}
    modes = inventory_digest.get("modes") or {}
    extra = ""
    if persons:
        extra += "\n사람: " + ", ".join(f"{k}={v}" for k, v in persons.items() if v)
    if modes:
        extra += "\n모드: " + ", ".join(modes.keys())
    return "\n".join(lines) + extra


async def llm_parse(sentence: str, inventory_digest: dict, settings: dict,
                    api_key: str) -> Optional[dict]:
    if not api_key:
        return None
    try:
        import aiohttp
    except ImportError:
        return None

    digest = _digest_text(inventory_digest, sentence)
    user = (f"인벤토리:\n{digest}\n\n문장: {sentence}\n\n"
            "이 문장을 RuleModel로 변환해줘.")
    payload = {
        "model": _MODEL,
        "max_tokens": 2000,
        "system": _SYSTEM,
        "tools": [_TOOL],
        "tool_choice": {"type": "tool", "name": "emit_rule"},
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(_API_URL, headers=headers,
                                    data=json.dumps(payload)) as resp:
                if resp.status != 200:
                    return None
                body = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None

    tool_input = None
    for block in body.get("content", []):
        if block.get("type") == "tool_use" and block.get("name") == "emit_rule":
            tool_input = block.get("input")
            break
    if not tool_input:
        return None

    result = _validate_entities(tool_input, inventory_digest)
    return result


def _validate_entities(tool_input: dict, inventory_digest: dict) -> dict:
    """응답의 entity_id 가 인벤토리에 실존하는지 전수 검증. 없으면 강등."""
    valid_ids = {e.get("entity_id") for e in inventory_digest.get("entities", [])}
    # person/scene 등 settings 유래 id 도 허용
    for v in (inventory_digest.get("persons") or {}).values():
        if v:
            valid_ids.add(v)
    warnings = []

    def check_node(node):
        eid = node.get("entity_id")
        if eid and eid not in valid_ids:
            warnings.append(f"존재하지 않는 엔티티: {eid}")
            node["entity_id"] = None
        tgt = node.get("target") or {}
        ids = tgt.get("entity_id")
        if isinstance(ids, list):
            filtered = [i for i in ids if i in valid_ids or i.startswith("scene.")]
            if len(filtered) != len(ids):
                warnings.append("일부 액션 대상이 인벤토리에 없어 제외했어요.")
            tgt["entity_id"] = filtered

    model = tool_input.get("model", {})
    for key in ("triggers", "conditions", "actions"):
        for node in model.get(key, []) or []:
            check_node(node)

    tool_input.setdefault("warnings", [])
    tool_input["warnings"].extend(warnings)
    return tool_input
