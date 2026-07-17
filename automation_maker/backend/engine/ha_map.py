"""v2 규칙 노드 → HA 자동화(v1 빌더 방언) 매핑 (APP-PORT-PLAN §2.6, SPEC-SCHEMA-90 §5).

automation_builder 는 이미 sun(±HH:MM:SS offset)·time_pattern(/N)·time(weekday)·template·
if/repeat 를 빌드한다. 여기서는 엔진이 저장/실행하는 **v2 방언**(sun.offset=초, sun_window
조건 등)을 v1 빌더가 이해하는 방언으로 변환한다. HA 내보내기·gold↔HA 패리티의 단일 지점.

S2 범위: sun 트리거(offset 초→±HH:MM:SS), sun_window 조건→sun 조건. daily→time 은 표(§5)에
있어 함께 넣는다. 나머지 노드는 그대로 통과(v1 빌더가 직접 처리). HA 로 직역 불가한 엔진
전용 노드(segment/mode/state_held/group_held/set_mode)는 경고로 보고한다.
"""
from __future__ import annotations

# HA 자동화로 직역 불가한 엔진 전용 노드(내보내기 시 경고).
_UNMAPPABLE_TRIGGERS = {"segment", "mode", "state_held", "group_held"}

_WEEKDAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _offset_str(sec) -> str | None:
    """오프셋 초(int) → '±HH:MM:SS'. 0/None/형식오류면 None(필드 생략)."""
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return None
    if sec == 0:
        return None
    sign = "-" if sec < 0 else "+"
    a = abs(sec)
    h, rem = divmod(a, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:02d}:{m:02d}:{s:02d}"


def _map_trigger(t: dict, warnings: list) -> dict | None:
    typ = t.get("type") if isinstance(t, dict) else None
    if typ == "sun":
        out = {"type": "sun", "event": t.get("event")}
        off = _offset_str(t.get("offset"))
        if off:
            out["offset"] = off
        return out
    if typ == "daily":
        at = str(t.get("at") or "")
        return {"type": "time", "at": at if at.count(":") == 2 else at + ":00"}
    if typ in _UNMAPPABLE_TRIGGERS:
        warnings.append(f"HA 자동화로 변환할 수 없는 트리거 유형: {typ}")
        return None
    # pass-through: state/numeric_state/zone/time/time_pattern/template/homeassistant
    return dict(t) if isinstance(t, dict) else None


def _map_condition(c: dict, warnings: list) -> dict | None:
    typ = c.get("type") if isinstance(c, dict) else None
    if typ == "sun_window":
        out = {"type": "sun", "after": c.get("after"), "before": c.get("before")}
        ao = _offset_str(c.get("after_offset"))
        bo = _offset_str(c.get("before_offset"))
        if ao:
            out["after_offset"] = ao
        if bo:
            out["before_offset"] = bo
        return out
    if typ == "weekday":
        # §2.6: negate=true 는 여집합으로 전개해 출력(HA time.weekday 은 부정 미지원).
        days = c.get("days") or []
        if c.get("negate"):
            days = [d for d in _WEEKDAY_ORDER if d not in days]
        return {"type": "time", "weekday": list(days)}
    if typ == "day_of_month":
        # §2.6: day_of_month → template 조건. 'last'(말일) = 내일이 1일.
        days = c.get("days")
        if days == "last":
            tmpl = "{{ (now() + timedelta(days=1)).day == 1 }}"
        else:
            tmpl = "{{ now().day in " + str(list(days or [])) + " }}"
        return {"type": "template", "value_template": tmpl}
    if typ == "interval_anchor":
        # §2.6: interval_anchor → template(월요일 정렬 주차 mod). anchor 는 이미 월요일로 저장.
        anchor = c.get("anchor")
        try:
            interval = int(c.get("interval") or 2)
        except (TypeError, ValueError):
            interval = 2
        tmpl = (
            "{{ ((as_timestamp(now().date() - timedelta(days=now().weekday())) "
            "- as_timestamp(as_datetime('" + str(anchor) + "'))) / 604800) "
            "| round(0, 'floor') % " + str(interval) + " == 0 }}")
        return {"type": "template", "value_template": tmpl}
    if typ in _UNMAPPABLE_TRIGGERS:
        warnings.append(f"HA 자동화로 변환할 수 없는 조건 유형: {typ}")
        return None
    return dict(c) if isinstance(c, dict) else None


def subrule_to_automation(sub: dict, inventory=None) -> dict:
    """v2 서브룰 → v1 빌더가 소비하는 model 조각(triggers/conditions/actions) + warnings.

    반환 dict 를 alias/mode 와 합치면 automation_builder.build_automation 이 그대로 소비한다.
    """
    warnings: list = []
    if not isinstance(sub, dict):
        return {"triggers": [], "conditions": [], "condition_mode": "and",
                "actions": [], "warnings": ["서브룰 형식 오류"]}
    trigs = [m for m in (_map_trigger(t, warnings) for t in (sub.get("triggers") or []))
             if m is not None]
    conds = [m for m in (_map_condition(c, warnings) for c in (sub.get("conditions") or []))
             if m is not None]
    actions = [dict(a) for a in (sub.get("actions") or []) if isinstance(a, dict)]
    return {
        "triggers": trigs,
        "conditions": conds,
        "condition_mode": sub.get("condition_mode", "and"),
        "actions": actions,
        "warnings": warnings,
    }
