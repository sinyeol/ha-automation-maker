"""§3.3 — gold RuleModel 과 파서 실제 출력의 구조 비교(핵심).

- normalize_model: subrules 로 평탄화, 각 노드를 핵심필드만 남긴 정렬 표준형으로.
- compare: exact | partial | fail 판정 + diff(5분류 태그, §7.6).

핵심필드(SPEC §3.3):
  trigger  : type, entity_id, to, for, mode, segments, above, below, event
  condition: type, entity_id, state, segments, mode, types, seasons, after, before
  action   : action, target.entity_id(정렬), data, type, mode, to, duration
chips/summary/confidence 는 비교에서 무시.
"""
from __future__ import annotations

_TRIGGER_FIELDS = ("type", "entity_id", "to", "for", "mode", "segments",
                   "above", "below", "event")
_COND_FIELDS = ("type", "entity_id", "state", "segments", "mode", "types",
                "seasons", "after", "before", "above", "below")
_ACTION_FIELDS = ("type", "action", "mode", "to")


# ---------------------------------------------------------------------------
# 값 정규화(YAML on/off=bool, 숫자 int/float, 리스트 정렬, 지속시간 튜플 등)
# ---------------------------------------------------------------------------
def _canon_scalar(v):
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _canon_duration(d):
    if not isinstance(d, dict):
        return d
    return (int(d.get("hours", 0)), int(d.get("minutes", 0)), int(d.get("seconds", 0)))


def _canon_value(key, v):
    if key in ("for", "duration"):
        return _canon_duration(v)
    if key in ("segments", "types", "seasons"):
        return tuple(sorted(v)) if isinstance(v, list) else v
    if key in ("above", "below"):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return v
    if key == "target":
        # action target.entity_id 정렬
        ids = v.get("entity_id") if isinstance(v, dict) else None
        if isinstance(ids, str):
            ids = [ids]
        return ("target", tuple(sorted(ids)) if isinstance(ids, list) else ())
    if key == "data":
        if isinstance(v, dict):
            return tuple(sorted((k, _canon_scalar(x)) for k, x in v.items()))
        return v
    return _canon_scalar(v)


def _canon_node(node: dict, fields) -> tuple:
    items = []
    for k in fields:
        if k in node and node.get(k) is not None:
            items.append((k, _canon_value(k, node[k])))
    # action 은 target/data 도 핵심필드
    if "action" in fields or node.get("type") == "service":
        if node.get("target") is not None:
            items.append(_canon_value("target", node["target"]))
        if node.get("data") is not None:
            items.append(("data", _canon_value("data", node["data"])))
    return tuple(items)


def _flatten_subrules(model: dict) -> list[dict]:
    if not isinstance(model, dict):
        return []
    subs = model.get("subrules")
    if isinstance(subs, list):
        return subs
    return [{"triggers": model.get("triggers", []),
             "conditions": model.get("conditions", []),
             "actions": model.get("actions", [])}]


def normalize_model(model: dict) -> list[dict]:
    """model → subrule별 정렬 표준형 리스트."""
    out = []
    for sub in _flatten_subrules(model):
        out.append({
            "triggers": sorted(
                (_canon_node(t, _TRIGGER_FIELDS) for t in sub.get("triggers", [])),
                key=repr),
            "conditions": sorted(
                (_canon_node(c, _COND_FIELDS) for c in sub.get("conditions", [])),
                key=repr),
            "actions": sorted(
                (_canon_node(a, _ACTION_FIELDS) for a in sub.get("actions", [])),
                key=repr),
        })
    return out


# ---------------------------------------------------------------------------
# 멀티셋 비교 + diff 5분류
# ---------------------------------------------------------------------------
def _multiset(subs, key):
    out = []
    for s in subs:
        out.extend(s[key])
    return out


def _remove_first(lst, val):
    for i, x in enumerate(lst):
        if x == val:
            del lst[i]
            return True
    return False


def _node_get(node_tuple, field):
    for k, v in node_tuple:
        if k == field:
            return v
    return None


def _classify_pair(g, a):
    """gold 노드 g 와 actual 노드 a 사이의 오류 유형(§7.6)."""
    gt, at = _node_get(g, "type"), _node_get(a, "type")
    ge = _node_get(g, "entity_id")
    ae = _node_get(a, "entity_id")
    # action 은 entity_id 대신 target 사용
    if ge is None and ae is None:
        ge = _node_get(g, "target")
        ae = _node_get(a, "target")
    if gt != at:
        return "wrong_node_type"
    if ge is not None and ae is not None and ge != ae:
        return "entity_confusion"
    return "value_mismatch"


def _diff_category(gold_ms, actual_ms, category):
    """한 범주(trigger/condition/action)의 diff 태그 목록."""
    g = list(gold_ms)
    a = list(actual_ms)
    # 완전 일치 항목 제거
    for x in list(g):
        if x in a:
            _remove_first(g, x)
            _remove_first(a, x)
    diffs = []
    # 남은 것끼리 짝지어 세부 분류
    for gm in list(g):
        best = None
        for am in a:
            ge = _node_get(gm, "entity_id") or _node_get(gm, "target")
            ae = _node_get(am, "entity_id") or _node_get(am, "target")
            gt, at = _node_get(gm, "type"), _node_get(am, "type")
            if (ge is not None and ge == ae) or (gt == at):
                best = am
                break
        if best is not None:
            diffs.append({"tag": _classify_pair(gm, best), "category": category,
                          "gold": dict(gm), "actual": dict(best)})
            _remove_first(g, gm)
            _remove_first(a, best)
    for gm in g:
        diffs.append({"tag": "missing_node", "category": category, "gold": dict(gm)})
    for am in a:
        diffs.append({"tag": "extra_node", "category": category, "actual": dict(am)})
    return diffs


def compare(gold: dict, actual: dict) -> dict:
    """gold vs actual → verdict + diff + 범주별 일치 플래그."""
    g_subs = normalize_model(gold)
    a_subs = normalize_model(actual)

    g_trig, a_trig = _multiset(g_subs, "triggers"), _multiset(a_subs, "triggers")
    g_cond, a_cond = _multiset(g_subs, "conditions"), _multiset(a_subs, "conditions")
    g_act, a_act = _multiset(g_subs, "actions"), _multiset(a_subs, "actions")

    trigger_match = sorted(map(repr, g_trig)) == sorted(map(repr, a_trig))
    cond_match = sorted(map(repr, g_cond)) == sorted(map(repr, a_cond))
    action_match = sorted(map(repr, g_act)) == sorted(map(repr, a_act))
    subrule_count_match = len(g_subs) == len(a_subs)

    diff = []
    diff += _diff_category(g_trig, a_trig, "trigger")
    diff += _diff_category(g_cond, a_cond, "condition")
    diff += _diff_category(g_act, a_act, "action")

    if not trigger_match:
        verdict = "fail"
    elif cond_match and action_match and subrule_count_match:
        verdict = "exact"
    else:
        verdict = "partial"

    return {"verdict": verdict, "diff": diff, "trigger_match": trigger_match,
            "subrule_count_match": subrule_count_match, "cond_match": cond_match,
            "action_match": action_match}
