"""조건 평가 + 지속시간/스코프 헬퍼 (§4.3의 evaluator 로직)."""
from __future__ import annotations

from datetime import time as dtime, timedelta

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def duration_to_seconds(d) -> float:
    if not isinstance(d, dict):
        return 0.0
    return (_f(d.get("hours")) * 3600.0 + _f(d.get("minutes")) * 60.0 + _f(d.get("seconds")))


def duration_to_timedelta(d) -> timedelta:
    return timedelta(seconds=duration_to_seconds(d))


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_clock(s) -> dtime | None:
    try:
        parts = [int(x) for x in str(s).split(":")]
        while len(parts) < 3:
            parts.append(0)
        return dtime(parts[0], parts[1], parts[2])
    except (ValueError, TypeError):
        return None


class EvalContext:
    """조건 평가에 필요한 런타임 참조 묶음."""

    def __init__(self, cache, gvars, now_fn, inventory_fn, fired_index=None):
        self.cache = cache
        self.gvars = gvars
        self.now = now_fn
        self.inventory_fn = inventory_fn
        self.fired_index = fired_index


def scope_all_state(scope, state, ctx, duration=None) -> bool:
    """스코프 내 모든 엔티티가 state인지(있으면 duration 이상 유지)."""
    eids = ctx.cache.entities_in_scope(scope, ctx.inventory_fn())
    if not eids:
        return True  # 대상이 없으면 '모두 만족'(vacuous truth)
    for eid in eids:
        if duration is not None:
            if not ctx.cache.held_for(eid, state, duration):
                return False
        else:
            entry = ctx.cache.get(eid)
            if entry is None or entry.get("state") != state:
                return False
    return True


def evaluate_conditions(model: dict, ctx: EvalContext) -> bool:
    conds = model.get("conditions") or []
    if not conds:
        return True
    results = [evaluate_condition(c, ctx) for c in conds]
    if model.get("condition_mode", "and") == "or":
        return any(results)
    return all(results)


def evaluate_condition(cond: dict, ctx: EvalContext) -> bool:
    typ = cond.get("type")
    cache = ctx.cache

    if typ == "state":
        entry = cache.get(cond.get("entity_id"))
        if cond.get("for"):
            return cache.held_for(cond.get("entity_id"), cond.get("state"),
                                  duration_to_timedelta(cond["for"]))
        return entry is not None and entry.get("state") == cond.get("state")

    if typ == "numeric_state":
        entry = cache.get(cond.get("entity_id"))
        val = _num(entry.get("state")) if entry else None
        if val is None:
            return False
        return _passes_bounds(val, cond.get("above"), cond.get("below"))

    if typ == "time":
        return _eval_time(cond, ctx)

    if typ == "time_segment":
        return ctx.gvars.is_in_segments(cond.get("segments") or [])

    if typ == "day_type":
        return ctx.gvars.day_type() in (cond.get("types") or [])

    if typ == "season":
        return ctx.gvars.season() in (cond.get("seasons") or [])

    if typ == "held":
        return cache.held_for(cond.get("entity_id"), cond.get("state"),
                              duration_to_timedelta(cond.get("for")))

    if typ == "group_state":
        dur = duration_to_timedelta(cond["for"]) if cond.get("for") else None
        return scope_all_state(cond.get("scope"), cond.get("state"), ctx, dur)

    if typ == "zone":
        entry = cache.get(cond.get("entity_id"))
        return entry is not None and entry.get("state") == cond.get("zone")

    if typ == "trigger":
        return ctx.fired_index is not None and str(cond.get("id")) == str(ctx.fired_index)

    if typ == "and":
        return all(evaluate_condition(c, ctx) for c in (cond.get("conditions") or []))
    if typ == "or":
        return any(evaluate_condition(c, ctx) for c in (cond.get("conditions") or []))
    if typ == "not":
        return not any(evaluate_condition(c, ctx) for c in (cond.get("conditions") or []))

    return False  # sun/template 등 미지원(검증에서 이미 거부)


def _passes_bounds(val, above, below) -> bool:
    if above is not None and not (val > float(above)):
        return False
    if below is not None and not (val < float(below)):
        return False
    return above is not None or below is not None


def _eval_time(cond, ctx) -> bool:
    now = ctx.now()
    t = now.time()
    after = _parse_clock(cond.get("after")) if cond.get("after") else None
    before = _parse_clock(cond.get("before")) if cond.get("before") else None
    ok = True
    if after and before:
        ok = (after <= t <= before) if after <= before else (t >= after or t <= before)
    elif after:
        ok = t >= after
    elif before:
        ok = t <= before
    wd = cond.get("weekday")
    if wd:
        ok = ok and _WEEKDAYS[now.weekday()] in wd
    return ok
