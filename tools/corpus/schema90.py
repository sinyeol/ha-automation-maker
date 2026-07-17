"""SPEC-SCHEMA-90 목표 스키마 검증기 (held-out gold 전용, 앱 검증기와 분리).

`backend.engine.rule_model.validate_rule_model` 은 **엔진이 실제 실행 가능한** 노드만 통과
시킨다(라이브 저장 경로 보호). 그러나 정확도 90% held-out 은 아직 구현되지 않은 신규 의미축
(sun/weekday/presence_agg 등, SPEC-SCHEMA-90 §1·§2)을 gold 로 포함해야 정직한 분모가 된다
(Phase 3 구현 후에야 파서가 맞힐 수 있고, 그 전엔 honest fail 로 집계).

이 모듈은 held-out gold 를 **목표 스키마**(기존 노드 ∪ 신규 노드)에 대해 검증한다:
genuinely 깨진 gold(미실존 엔티티·미허용 서비스 도메인·알 수 없는 노드 타입·빈 구조)만
잡아내고, well-formed 한 신규노드 gold 는 통과시킨다. augment.load_paraphrases 가 앱 검증
실패 시 이 검증으로 rescue 한다(gold_invalid 재판정).
"""
from __future__ import annotations

import os
import sys

_APP_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "automation_maker"))
if _APP_ROOT not in sys.path:
    sys.path.insert(0, _APP_ROOT)

from backend.engine.rule_model import _ALLOWED_ACTION_DOMAINS  # noqa: E402

# 기존(엔진 지원) 노드 타입 — SPEC-SCHEMA-90 §0.1.
_EXISTING_TRIGGERS = {"state", "numeric_state", "state_held", "group_held",
                      "daily", "segment", "mode", "zone"}
_EXISTING_CONDITIONS = {"state", "numeric_state", "time_segment", "day_type",
                        "season", "mode", "held", "group_state", "zone",
                        "trigger", "and", "or", "not"}
_EXISTING_ACTIONS = {"service", "set_mode", "delay", "if", "choose", "repeat",
                     "condition", "stop"}

# 신규(목표 스키마) 노드 타입 — SPEC-SCHEMA-90 §1·§2.
_NEW_TRIGGERS = {"sun", "time_pattern", "presence_agg"}
_NEW_CONDITIONS = {"sun_window", "weekday", "day_of_month", "interval_anchor",
                   "presence_agg"}

_ALLOWED_TRIGGERS = _EXISTING_TRIGGERS | _NEW_TRIGGERS
_ALLOWED_CONDITIONS = _EXISTING_CONDITIONS | _NEW_CONDITIONS
_ALLOWED_ACTIONS = _EXISTING_ACTIONS

# toggle 목표 스키마(SPEC-SCHEMA-90 §3.2)는 homeassistant.toggle 을 허용한다.
_ALLOWED_DOMAINS = set(_ALLOWED_ACTION_DOMAINS) | {"homeassistant"}

NEW_NODE_TYPES = _NEW_TRIGGERS | _NEW_CONDITIONS


def _inventory_ids(inventory) -> set:
    ents = inventory.get("entities") if isinstance(inventory, dict) else inventory
    ids = {e.get("entity_id") for e in (ents or []) if e.get("entity_id")}
    for z in (inventory.get("zones") or []):
        if isinstance(z, dict) and z.get("entity_id"):
            ids.add(z["entity_id"])
    ids.add("zone.home")
    return ids


def _collect_entity_ids(node, out: list) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "entity_id":
                if isinstance(v, str):
                    out.append(v)
                elif isinstance(v, list):
                    out.extend(x for x in v if isinstance(x, str))
            elif k == "persons" and isinstance(v, list):
                out.extend(x for x in v if isinstance(x, str))
            else:
                _collect_entity_ids(v, out)
    elif isinstance(node, list):
        for x in node:
            _collect_entity_ids(x, out)


def _scan_actions_domains(actions, errors) -> None:
    stack = list(actions or [])
    while stack:
        n = stack.pop()
        if isinstance(n, dict):
            if n.get("type") == "service":
                action = str(n.get("action") or "")
                dom = action.split(".", 1)[0] if "." in action else ""
                if dom and dom not in _ALLOWED_DOMAINS:
                    errors.append(f"미허용 서비스 도메인: {action}")
            stack.extend(n.values())
        elif isinstance(n, list):
            stack.extend(n)


def _check_types(nodes, allowed, label, errors) -> None:
    for nd in (nodes or []):
        if isinstance(nd, dict):
            t = nd.get("type")
            # 중첩(and/or/not/if/choose/repeat)은 하위 타입까지 강제하지 않고 최상위만 확인.
            if t and t not in allowed and t not in _ALLOWED_ACTIONS \
                    and t not in _ALLOWED_CONDITIONS and t not in _ALLOWED_TRIGGERS:
                errors.append(f"알 수 없는 {label} 노드 타입: {t}")


def target_schema_errors(model: dict, inventory, mode_names=None) -> list:
    """held-out gold 를 목표 스키마로 검증. genuinely 깨진 gold 만 오류 반환(빈 리스트=유효).

    특수형(prohibition/out_of_scope)은 정상 모델이 아니므로 유효로 간주(별도 지표에서 처리).
    """
    if not isinstance(model, dict):
        return ["모델 형식 오류"]
    if model.get("prohibition") or model.get("out_of_scope"):
        return []

    errors: list = []
    subs = model.get("subrules")
    if not isinstance(subs, list) or not subs:
        return ["subrules 가 비어 있음"]

    valid_ids = _inventory_ids(inventory)
    for sub in subs:
        if not isinstance(sub, dict):
            errors.append("서브룰 형식 오류")
            continue
        trigs = sub.get("triggers")
        acts = sub.get("actions")
        if not isinstance(trigs, list) or not trigs:
            errors.append("트리거 없음")
        if not isinstance(acts, list) or not acts:
            errors.append("액션 없음")
        _check_types(trigs, _ALLOWED_TRIGGERS, "트리거", errors)
        _check_types(sub.get("conditions"), _ALLOWED_CONDITIONS, "조건", errors)
        _check_types(acts, _ALLOWED_ACTIONS, "액션", errors)
        _scan_actions_domains(acts, errors)

    ids: list = []
    _collect_entity_ids(subs, ids)
    missing = sorted({i for i in ids if i not in valid_ids})
    if missing:
        errors.append("미실존 엔티티: " + ", ".join(missing))
    return errors


def uses_new_nodes(model: dict) -> bool:
    """gold 가 신규(미구현) 노드를 하나라도 쓰는지 — 리포트 분류용."""
    found = [False]

    def walk(o):
        if isinstance(o, dict):
            if o.get("type") in NEW_NODE_TYPES:
                found[0] = True
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(model)
    return found[0]
