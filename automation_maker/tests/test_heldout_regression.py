"""APP-PORT-PLAN §4b / 게이트2 — held-out 정직 회귀 + 금지문 안전 게이트(앱 파서 전수).

정직 채점기(tests/structural_compare)로 실제 앱 parse 를 채점한다.
  1) 금지문 안전(게이트2): heldout_special.yaml 의 special:prohibition 전수(23문장)를 앱
     parse 로 돌려 **정반대(forbidden) 액션이 하나도 생성되지 않음**을 단언(0/23). 안전사고 방지.
  2) 정직 exact 스팟체크: S1 이식으로 실제 파서가 대표 held-out 문장군을 exact 로 내는지
     structural_compare.compare 로 확인(수사·후치재배열·수치에지·held-for·notify·repeat·toggle).

heldout_special.yaml 은 tools/corpus 에 있지만 스키마가 단순(forbidden 리스트 + variants)이라
augment 파이프라인 없이 직접 로드한다(앱 tests 자립성 유지).
"""
from __future__ import annotations

import asyncio
import os

import pytest
import yaml

from backend.ha_client import merge_inventory
from backend.mock_data import MockHAClient
from backend.nl.gazetteer import Gazetteer
from backend.nl.parser import parse
from tests import structural_compare as sc

_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user", "와이프": "person.wife"},
    "modes": {"슬립 모드": {"action": "scene.turn_on",
                         "target": {"entity_id": ["scene.sleep_mode"]}}},
    "near_home": {"zone_state": "home"},
    "aliases": [],
}

_SPECIAL_YAML = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "tools", "corpus", "heldout_special.yaml"))


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


def _load_prohibitions():
    if not os.path.exists(_SPECIAL_YAML):
        pytest.skip("heldout_special.yaml 미존재")
    with open(_SPECIAL_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    out = []
    for entry in data:
        if "special:prohibition" not in (entry.get("tags") or []):
            continue
        forbidden = (entry.get("gold") or {}).get("forbidden") or []
        for var in entry.get("variants", []) or []:
            sent = var.get("text") if isinstance(var, dict) else str(var)
            out.append((entry.get("id"), sent, forbidden))
    return out


def _emitted_actions(model: dict):
    if not isinstance(model, dict):
        return []
    subs = model.get("subrules")
    subs = subs if isinstance(subs, list) else [model]
    out = []
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        for a in sub.get("actions", []) or []:
            act = a.get("action")
            if not act:
                continue
            tgt = a.get("target") or {}
            ids = tgt.get("entity_id") if isinstance(tgt, dict) else None
            if isinstance(ids, str):
                ids = [ids]
            out.append((act, set(ids or [])))
    return out


# ===========================================================================
# 게이트2 — 금지문 위험 오동작 0/23 (정반대 액션 미생성)
# ===========================================================================
def test_prohibition_no_forbidden_action(gz):
    """special:prohibition 전수 — forbidden(정반대) 액션이 하나도 생성되지 않아야 한다."""
    entries = _load_prohibitions()
    assert entries, "금지문 셋이 비어 있음(로드 실패)"
    misfires = []
    for rid, sentence, forbidden in entries:
        model = parse(sentence, gz, _SETTINGS).get("model", {})
        emitted = _emitted_actions(model)
        for fb in forbidden:
            fact, feid = fb.get("action"), fb.get("entity_id")
            for act, ids in emitted:
                if act == fact and (feid is None or feid in ids):
                    misfires.append((rid, sentence, fb))
                    break
    assert misfires == [], f"금지문 위험 오동작 {len(misfires)}건: {misfires}"


def test_prohibition_not_ok(gz):
    """금지문은 자동화 생성 요청이 아니므로 parse 가 ok=True 를 내지 않아야 한다(§4.4)."""
    entries = _load_prohibitions()
    bad = [(rid, s) for rid, s, _ in entries if parse(s, gz, _SETTINGS).get("ok")]
    assert bad == [], f"금지문인데 ok=True: {bad}"


# ===========================================================================
# 정직 exact 스팟체크 — S1 이식 대표 승리(structural_compare.compare == exact)
# ===========================================================================
def _sub(triggers, conditions, actions):
    return {"subrules": [{"triggers": triggers, "conditions": conditions,
                          "actions": actions}]}


def _exact(gz, sentence, gold):
    model = parse(sentence, gz, _SETTINGS).get("model", {})
    v = sc.compare(gold, model)
    assert v["verdict"] == "exact", f"{sentence}\n  verdict={v['verdict']} diff={v['diff']}"


def test_honest_surface_numeral_daily(gz):
    """표면정규화(수사→숫자): '일곱 시 반' → daily 07:30."""
    _exact(gz, "일곱 시 반에 거실 조명 켜줘",
           _sub([{"type": "daily", "at": "07:30"}], [],
                [{"type": "service", "action": "light.turn_on",
                  "target": {"entity_id": ["light.living_room_main"]}}]))


def test_honest_postposed_reorder(gz):
    """후치 조건 재배열: '불 꺼줘, 문 열리면' → 문 열리면 불 꺼줘."""
    _exact(gz, "불 꺼줘, 문 열리면",
           _sub([{"type": "state", "entity_id": "binary_sensor.entrance_door", "to": "on"}],
                [], [{"type": "service", "action": "light.turn_off",
                      "target": {"entity_id": ["light.entrance"]}}]))


def test_honest_numeric_between(gz):
    """#14 수치 between: '20도에서 24도 사이' → above 20 + below 24 한 노드."""
    _exact(gz, "거실 온도가 20도에서 24도 사이면 보일러 꺼줘",
           _sub([{"type": "numeric_state", "entity_id": "sensor.living_room_temperature",
                  "above": 20, "below": 24}], [],
                [{"type": "service", "action": "climate.turn_off",
                  "target": {"entity_id": ["climate.living_room_boiler"]}}]))


def test_honest_surface_numeral_numeric(gz):
    """수사→숫자 + 수치 트리거: '스물여덟 도 넘으면' → above 28."""
    _exact(gz, "스물여덟 도 넘으면 거실 에어컨 켜줘",
           _sub([{"type": "numeric_state", "entity_id": "sensor.living_room_temperature",
                  "above": 28}], [],
                [{"type": "service", "action": "climate.turn_on",
                  "target": {"entity_id": ["climate.living_room_ac"]}}]))


def test_honest_held_for(gz):
    """#25 held-for: '10분 넘게 켜져 있으면' → state for 10분."""
    _exact(gz, "거실 조명이 10분 넘게 켜져 있으면 꺼줘",
           _sub([{"type": "state", "entity_id": "light.living_room_main",
                  "to": "off", "for": {"minutes": 10}}], [],
                [{"type": "service", "action": "light.turn_off",
                  "target": {"entity_id": ["light.living_room_main"]}}]))


def test_honest_notify_quoted(gz):
    """#29 notify: 인용 메시지 → notify.notify(message)."""
    _exact(gz, "현관문 열리면 '손님 왔어요'라고 알려줘",
           _sub([{"type": "state", "entity_id": "binary_sensor.entrance_door", "to": "on"}],
                [], [{"type": "service", "action": "notify.notify",
                      "data": {"message": "손님 왔어요"}}]))
