"""SPEC-V3 §4.2 — LLM 해석 백엔드 디스패치(off / api / cli).

로컬 파서의 confidence < 0.6 이거나 unresolved 스팬이 있을 때만 api_v2가 호출한다.
백엔드는 세 가지:
  - off: 항상 None(로컬 파서 결과만 사용).
  - api: Anthropic Messages HTTP(aiohttp). api_key 없으면 None.
  - cli: 구독 Claude Code CLI 헤드리스 호출(cli_client). 토큰/바이너리 없으면 None.

세 백엔드 모두 성공 시 통일 계약 `{"model":..., "warnings":[...]}` 또는 None을 돌려준다.
실패는 조용히 None(로컬 결과 사용). API 키/토큰은 로그에 절대 남기지 않는다.
aiohttp만 외부 의존(api 경로에서만 import). cli 경로는 표준 라이브러리(subprocess)만.
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

from backend.nl import cli_client

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
    "너는 홈어시스턴트 자동화를 만드는 한국어 파서다. 주어진 한국어 스마트홈 문장을 "
    "RuleModel(JSON)로 바꾼다. "
    "트리거 타입: state{entity_id,to}, state_held{entity_id,to,for}, "
    "group_held{scope,to,for}, numeric_state{entity_id,above,below}, zone{entity_id,zone,event}, "
    "mode{mode,to}. "
    "조건 타입: state, numeric_state, time{after,before}, time_segment{segments}, day_type, "
    "season, mode{mode,state}. "
    "액션: service{action,target:{entity_id:[...]},data}, set_mode{mode,to}. "
    "Duration은 {hours,minutes,seconds}. "
    "entity_id는 반드시 아래 제공된 인벤토리(digest)에 실존하는 id만 쓴다. 모르면 비운다. "
    "모드 이름은 제공된 모드 목록에서만 쓴다. sun/template은 쓰지 않는다."
)

# cli 백엔드용 시스템 프롬프트: 구조화 출력(model/warnings)만 요구.
_CLI_SYSTEM = (
    _SYSTEM
    + " 반드시 지정된 JSON 스키마의 구조화 출력만 낸다. 다른 설명·코드펜스·마크다운은 금지한다. "
    "출력 객체는 {\"model\": RuleModel, \"warnings\": [문자열]} 형태다."
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


def _user_prompt(inventory_digest: dict, sentence: str) -> str:
    digest = _digest_text(inventory_digest, sentence)
    return (f"인벤토리:\n{digest}\n\n문장: {sentence}\n\n"
            "이 문장을 RuleModel로 변환해줘.")


async def llm_parse(sentence: str, inventory_digest: dict, settings: dict, *,
                    backend: str, api_key: str = "",
                    oauth_token: str = "") -> Optional[dict]:
    """백엔드(off/api/cli)를 디스패치. 통일 계약 {"model",...,"warnings":[...]} 또는 None."""
    if backend == "api":
        return await _parse_api(sentence, inventory_digest, api_key)
    if backend == "cli":
        return await _parse_cli(sentence, inventory_digest, oauth_token)
    # off 또는 알 수 없는 백엔드
    return None


async def _parse_cli(sentence: str, inventory_digest: dict,
                     oauth_token: str) -> Optional[dict]:
    prompt = _user_prompt(inventory_digest, sentence)
    raw = await cli_client.cli_parse(prompt, _CLI_SYSTEM, oauth_token=oauth_token)
    if not raw or not isinstance(raw.get("model"), dict):
        return None
    return _validate_entities(raw, inventory_digest)


async def _parse_api(sentence: str, inventory_digest: dict,
                     api_key: str) -> Optional[dict]:
    if not api_key:
        return None
    try:
        import aiohttp
    except ImportError:
        return None

    user = _user_prompt(inventory_digest, sentence)
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
    if not tool_input or not isinstance(tool_input.get("model"), dict):
        return None

    return _validate_entities(tool_input, inventory_digest)


def _validate_entities(tool_input: dict, inventory_digest: dict) -> dict:
    """응답의 entity_id 가 인벤토리에 실존하는지 전수 검증. 없으면 강등."""
    valid_ids = {e.get("entity_id") for e in inventory_digest.get("entities", [])}
    # person/scene 등 settings 유래 id 도 허용
    for v in (inventory_digest.get("persons") or {}).values():
        if v:
            valid_ids.add(v)
    warnings = []

    def check_node(node):
        if not isinstance(node, dict):
            return
        eid = node.get("entity_id")
        if eid and eid not in valid_ids:
            warnings.append(f"존재하지 않는 엔티티: {eid}")
            node["entity_id"] = None
        tgt = node.get("target")
        if isinstance(tgt, dict) and "entity_id" in tgt:
            ids = tgt.get("entity_id")
            # 스칼라 문자열/리스트를 모두 리스트로 정규화한 뒤 인벤토리에 실존하는 id 만
            # 남긴다. scene.*/script.* 도 예외 없이 실존하는 것만 통과(무조건 허용 금지).
            as_list = ids if isinstance(ids, list) else ([ids] if ids else [])
            filtered = [i for i in as_list if i in valid_ids]
            if len(filtered) != len(as_list):
                warnings.append("일부 액션 대상이 인벤토리에 없어 제외했어요.")
            tgt["entity_id"] = filtered

    model = tool_input.get("model", {})
    # subrules(SPEC-V3 §2.2) 또는 최상위 4필드 모두 순회.
    subrules = model.get("subrules")
    scopes = subrules if isinstance(subrules, list) else [model]
    for sub in scopes:
        if not isinstance(sub, dict):
            continue
        for key in ("triggers", "conditions", "actions"):
            for node in sub.get(key, []) or []:
                check_node(node)

    tool_input.setdefault("warnings", [])
    tool_input["warnings"].extend(warnings)
    return tool_input
