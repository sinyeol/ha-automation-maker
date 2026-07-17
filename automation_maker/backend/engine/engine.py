"""규칙 엔진 (§4.3 + SPEC-V3 §2.3·§1.2). 트리거 인덱싱·에지 평가·for 타이머·경계 스케줄·오류 격리.

SPEC-V3: 규칙 로드/인덱싱/발화가 서브룰(_subrules)을 순회하도록 일반화되고, 상태 모드
변수(ModeState)와 mode 트리거/조건/set_mode 액션을 처리한다. 타이머/인덱스 키는
(rule_id, flat_index) — flat_index 는 서브룰을 가로질러 트리거를 0부터 센 값이라 단일
서브룰(레거시) 규칙에서는 기존 (rule_id, trigger_index) 와 그대로 일치한다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .evaluator import (
    EvalContext, duration_to_seconds, duration_to_timedelta, evaluate_condition,
    evaluate_conditions, scope_all_state, _num, _passes_bounds,
)
from .storage import JsonStore, data_dir

log = logging.getLogger("automation_maker.engine")

_COOLDOWN = 5.0          # 같은 서브룰 재발화 최소 간격(초)
_ERROR_LIMIT = 3         # 연속 오류 시 auto_disable
_MAX_REPEAT = 1000       # repeat 무한루프 방지 상한
_HOLD_EPS = 0.05         # 재검증 시 타이머 재장전 판단 허용오차(초)
_MODE_MAX_DEPTH = 8      # set_mode → mode 트리거 → set_mode 재귀 깊이 제한

# for 타이머를 갖는 트리거 유형
_HELD_TYPES = ("state_held", "group_held")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _subrules(model: dict) -> list[dict]:
    """모델의 서브룰 목록. subrules 가 없으면 최상위 4필드를 단일 서브룰로 취급(하위호환)."""
    subs = model.get("subrules") if isinstance(model, dict) else None
    if isinstance(subs, list) and subs:
        return subs
    return [{
        "triggers": model.get("triggers") or [],
        "conditions": model.get("conditions") or [],
        "condition_mode": model.get("condition_mode", "and"),
        "actions": model.get("actions") or [],
    }]


def _iter_triggers(model: dict):
    """(flat_index, subrule_index, trigger_index, trigger_node) 를 순회한다.

    flat_index 는 서브룰을 가로질러 트리거를 0부터 세며, 타이머/인덱스 키로 쓰인다.
    """
    flat = 0
    for si, sub in enumerate(_subrules(model)):
        for ti, t in enumerate(sub.get("triggers") or []):
            yield flat, si, ti, t
            flat += 1


def _locate(model: dict, flat: int):
    """flat_index → (subrule_index, trigger_index, trigger_node) 또는 None."""
    for f, si, ti, t in _iter_triggers(model):
        if f == flat:
            return si, ti, t
    return None


def _subrule_at(model: dict, si: int):
    subs = _subrules(model)
    return subs[si] if 0 <= si < len(subs) else None


def _held_spec(node: dict):
    """held 계열 트리거 → (kind, target, to, for) 튜플. 아니면 None.

    kind="single":   state_held 또는 for 를 가진 state 트리거(entity_id 기준).
    kind="group":    group_held(scope 기준).
    kind="presence": for 를 가진 presence_agg 트리거(quant last/all). target 은 노드 자체
                     (persons 해석은 엔진이 인벤토리와 함께 수행), to 는 유지할 person 상태.
    """
    typ = node.get("type")
    if typ == "group_held":
        return ("group", node.get("scope"), node.get("to"), node.get("for"))
    if typ == "state_held" or (typ == "state" and node.get("for")):
        return ("single", node.get("entity_id"), node.get("to"), node.get("for"))
    if typ == "presence_agg" and node.get("for") and node.get("quant") in ("last", "all"):
        # last=무인 유지("not_home"), all=전원 재실 유지("home"). fix2/fix3/pending 복원
        # 루프가 group_held 와 동일하게 이 spec 을 다루므로 새 복원 코드가 최소화된다.
        target_state = "not_home" if node.get("quant") == "last" else "home"
        return ("presence", node, target_state, node.get("for"))
    return None


class RuleEngine:
    def __init__(self, rule_store, state_cache, global_vars, ha, inventory_fn, runlog,
                 now_fn=None, loop=None, mode_state=None, sun_provider=None):
        self._rule_store = rule_store
        self._cache = state_cache
        self._gvars = global_vars
        self._ha = ha
        self._inventory_fn = inventory_fn
        self._runlog = runlog
        self._now_fn = now_fn or (lambda: datetime.now())  # 벽시계(스케줄용, gvars와 동일)
        self._loop = loop
        self._mode_state = mode_state                       # SPEC-V3 §1.2 (없으면 모드 비활성)
        self._sun = sun_provider                            # APP-PORT-PLAN §2.1 (없으면 sun 미스케줄)

        self._index: dict[str, set[str]] = {}          # entity_id → rule_ids
        self._mode_index: dict[str, set[tuple]] = {}    # mode 이름 → {(rid, si, ti)}
        self._rules: dict[str, dict] = {}               # 활성 규칙 rule_id → rule
        self._for_timers: dict[tuple, asyncio.TimerHandle] = {}
        self._daily_timers: dict[tuple, asyncio.TimerHandle] = {}
        self._boundary_timer: asyncio.TimerHandle | None = None
        self._error_streak: dict[str, int] = {}
        self._last_fired: dict[tuple, float] = {}        # (rid, subrule_index) → loop time
        self._tasks: set = set()
        self._exec_tasks: dict[str, set] = {}           # rule_id → 진행 중 실행 태스크(fix 4)
        self._connected = False
        self._event_source = None
        self._pending_store: JsonStore | None = None
        self._pending_expired: list[tuple] = []          # 첫 resync 이후 처리할 만료 pending(fix 1)
        self._last_segment: str | None = None            # 마지막 처리 세그먼트(fix 5)

    # ------------------------------------------------------------------ 수명주기
    async def start(self, event_source) -> None:
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        self._event_source = event_source
        if self._pending_store is None:
            self._pending_store = JsonStore(data_dir() / "pending_timers.json", [], loop=self._loop)
        self._compile_all()
        # 만료 pending 은 첫 resync 이후에 조건 재확인 후 처리하기 위해 미리 로드한다(fix 1).
        # (HAEventSource.start 는 즉시 반환하므로 이 시점의 StateCache 는 비어 있을 수 있다.)
        self._load_pending()
        await event_source.start(self._on_event, self._on_resync,
                                 self._on_connect, self._on_disconnect)
        # 연결 상태는 on_connect/on_resync 통지로만 갱신한다(start 직후 무조건 True 금지, fix 8).
        self._schedule_boundary()

    async def stop(self) -> None:
        self._save_pending()
        if self._pending_store is not None:
            await self._pending_store.flush()
        if self._mode_state is not None:
            self._mode_state.save()
        for h in list(self._for_timers.values()):
            h.cancel()
        self._for_timers.clear()
        for h in list(self._daily_timers.values()):
            h.cancel()
        self._daily_timers.clear()
        if self._boundary_timer is not None:
            self._boundary_timer.cancel()
            self._boundary_timer = None
        for task in list(self._tasks):
            task.cancel()
        self._exec_tasks.clear()
        if self._event_source is not None:
            await self._event_source.stop()
        self._connected = False

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "rules": len(self._rules),
            "active_timers": len(self._for_timers),
            "vars": self._gvars.snapshot(),
            "modes": self._mode_state.snapshot() if self._mode_state is not None else {},
        }

    def _on_connect(self) -> None:
        self._connected = True

    def _on_disconnect(self) -> None:
        self._connected = False

    def reschedule_boundary(self) -> None:
        """시간대 경계 설정 변경 시 경계 타이머를 재계산한다(fix 7).

        NOTE(api_v2 담당): handle_settings_put 에서 설정 저장(store.save_soon) 뒤에
        `request.app["engine"].reschedule_boundary()` 를 호출해야 바뀐 경계가 반영된다.
        """
        if self._boundary_timer is not None:
            self._boundary_timer.cancel()
            self._boundary_timer = None
        if self._loop is not None:
            self._schedule_boundary()

    # ------------------------------------------------------------------ 모드 (SPEC-V3 §1.2)
    def set_mode(self, name: str, on: bool, context: str = "manual", depth: int = 0) -> bool:
        """모드 상태를 설정한다. 상태 변경 → persist → side-effect → mode 트리거 pubsub.

        실제로 상태가 바뀌었으면 True. mode 트리거의 액션이 다시 set_mode 를 호출하는
        재귀는 depth 로 제한한다(_MODE_MAX_DEPTH).
        """
        if self._mode_state is None or not name:
            return False
        changed = self._mode_state.set(name, bool(on))
        if not changed:
            return False
        self._mode_state.save()
        new_state = "on" if on else "off"
        self._run_mode_side_effect(name, new_state)
        if depth > _MODE_MAX_DEPTH:
            log.warning("모드 전이 재귀 깊이 초과로 트리거를 건너뜁니다: %s", name)
            return True
        self._fire_mode_triggers(name, new_state, depth)
        return True

    def _run_mode_side_effect(self, name: str, state: str) -> None:
        action_def = self._mode_state.side_effect(name, state)
        if not isinstance(action_def, dict) or not action_def.get("action") or self._loop is None:
            return
        task = self._loop.create_task(self._run_side_effect(action_def))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_side_effect(self, action_def) -> None:
        try:
            await self._call_service(action_def)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("모드 side-effect 실행 오류")

    def _fire_mode_triggers(self, name: str, new_state: str, depth: int) -> None:
        for (rid, si, ti) in list(self._mode_index.get(name, ())):
            rule = self._rules.get(rid)
            if rule is None:
                continue
            sub = _subrule_at(rule.get("model") or {}, si)
            trigs = (sub.get("triggers") if sub else None) or []
            if ti >= len(trigs):
                continue
            t = trigs[ti]
            if (t.get("type") == "mode" and t.get("mode") == name
                    and t.get("to") == new_state):
                self._try_fire(rule, si, ti, depth=depth, context="mode")

    # ------------------------------------------------------------------ 컴파일/인덱스
    def _compile_all(self) -> None:
        for h in list(self._for_timers.values()):
            h.cancel()
        for h in list(self._daily_timers.values()):
            h.cancel()
        self._for_timers.clear()
        self._daily_timers.clear()
        self._index.clear()
        self._mode_index.clear()
        self._rules.clear()
        for rule in self._rule_store.all():
            if not rule.get("enabled"):
                continue
            if (rule.get("meta") or {}).get("auto_disabled"):
                continue
            self._index_rule(rule)

    def _index_rule(self, rule: dict) -> None:
        rid = rule["id"]
        model = rule.get("model") or {}
        targets: set[str] = set()
        has_trigger = False
        for flat, si, ti, t in _iter_triggers(model):
            typ = t.get("type")
            if typ in ("state", "numeric_state", "state_held", "zone"):
                eid = t.get("entity_id")
                if eid:
                    targets.add(eid)
                    has_trigger = True
            elif typ == "group_held":
                for eid in self._cache.entities_in_scope(t.get("scope"), self._inventory_fn()):
                    targets.add(eid)
                has_trigger = True
            elif typ == "daily":
                self._schedule_daily(rid, flat, t)
                has_trigger = True
            elif typ == "sun":
                self._schedule_sun(rid, flat, t)
                has_trigger = True
            elif typ == "time_pattern":
                self._schedule_pattern(rid, flat, t)
                has_trigger = True
            elif typ == "presence_agg":
                # 집(zone.home) 인원 양화 트리거(APP-PORT-PLAN §2.4). persons 각각을
                # 엔티티 인덱스에 등록해 person 상태 변경이 _eval_rule_triggers 로 라우팅되게 한다.
                for pid in self._persons_of(t):
                    targets.add(pid)
                has_trigger = True
            elif typ == "segment":
                has_trigger = True
            elif typ == "mode":
                mode = t.get("mode")
                if mode:
                    self._mode_index.setdefault(mode, set()).add((rid, si, ti))
                    has_trigger = True
        if not has_trigger:
            self._mark_no_trigger(rule)
            return
        self._rules[rid] = rule
        for eid in targets:
            self._index.setdefault(eid, set()).add(rid)

    def _unindex_rule(self, rid: str) -> None:
        self._rules.pop(rid, None)
        for s in self._index.values():
            s.discard(rid)
        for s in self._mode_index.values():
            for key in [k for k in s if k[0] == rid]:
                s.discard(key)
        for key in [k for k in self._for_timers if k[0] == rid]:
            self._for_timers.pop(key).cancel()
        for key in [k for k in self._daily_timers if k[0] == rid]:
            self._daily_timers.pop(key).cancel()
        # 진행 중인 액션 실행(delay 등)을 취소한다 — HA automation.turn_off 의미론(fix 4).
        for task in list(self._exec_tasks.get(rid, ())):
            task.cancel()
        self._exec_tasks.pop(rid, None)
        self._error_streak.pop(rid, None)
        for key in [k for k in self._last_fired if k[0] == rid]:
            self._last_fired.pop(key, None)

    def reload_rule(self, rule_id: str) -> None:
        self._unindex_rule(rule_id)
        rule = self._rule_store.get(rule_id)
        if rule and rule.get("enabled") and not (rule.get("meta") or {}).get("auto_disabled"):
            self._index_rule(rule)

    def _mark_no_trigger(self, rule: dict) -> None:
        meta = rule.setdefault("meta", {})
        meta["last_error"] = "트리거가 없어 실행할 수 없습니다."
        meta["auto_disabled"] = True
        self._rule_store.save()

    # ------------------------------------------------------------------ 이벤트 라우팅
    def _on_event(self, entity_id, old_state, new_state) -> None:
        try:
            changed = self._cache.apply_event(entity_id, old_state, new_state)
            if not changed:
                return
            for rid in list(self._index.get(entity_id, ())):
                rule = self._rules.get(rid)
                if rule is not None:
                    self._eval_rule_triggers(rule, entity_id, old_state, new_state)
        except Exception:
            log.exception("이벤트 처리 중 오류")  # 루프는 죽지 않는다

    def _on_resync(self, states) -> None:
        self._cache.replace_all(states)
        self._connected = True
        # 조용히 캐시 교체 + held 타이머 재평가(발화 금지). §4.2 재연결 의미론.
        # 1) 기존 held 타이머의 유지 연속성 재검증 (fix 2)
        for key in list(self._for_timers.keys()):
            rid, flat = key
            spec = self._held_spec_of(self._rules.get(rid), flat)
            if spec is None:
                self._cancel_key(key)
                continue
            self._revalidate_held_timer(key, spec)
        # 2) 이미 to 상태인데 타이머가 없는 held 트리거를 잔여시간으로 장전 (fix 3)
        self._arm_missing_held_timers()
        # 3) 첫 resync 이후에 만료 pending 을 조건 재확인 후 발화 (fix 1)
        if self._pending_expired:
            self._flush_expired_pending()

    def _held_spec_of(self, rule, flat):
        if rule is None:
            return None
        loc = _locate(rule.get("model") or {}, flat)
        if loc is None:
            return None
        return _held_spec(loc[2])

    def _persons_of(self, node: dict) -> list[str]:
        """presence_agg 노드의 persons 목록. 명시되면 그대로, 생략이면 인벤토리 person.*."""
        persons = node.get("persons")
        if isinstance(persons, list) and persons:
            return [p for p in persons if isinstance(p, str)]
        inv = self._inventory_fn() or {}
        ents = inv.get("entities") if isinstance(inv, dict) else inv
        out: list[str] = []
        for e in ents or []:
            if not isinstance(e, dict):
                continue
            eid = e.get("entity_id")
            if eid and (e.get("domain") or eid.split(".", 1)[0]) == "person":
                out.append(eid)
        return out

    def _held_remaining(self, spec) -> float | None:
        """held 트리거의 for 완료까지 남은 초. 유지 중이 아니면 None(음수면 이미 초과)."""
        kind, target, to, dur = spec
        duration = duration_to_timedelta(dur)
        if kind == "single":
            return self._cache.hold_remaining(target, to, duration)
        if kind == "presence":
            # target 은 presence 노드. 모든 pid 가 유지상태(to)여야 하며, 남은 시간은
            # '가장 늦게 진입한' 사람 기준(=최대값). 하나라도 to 가 아니면 유지 깨짐.
            persons = self._persons_of(target)
            if not persons:
                return None
            worst = None
            for pid in persons:
                r = self._cache.hold_remaining(pid, to, duration)
                if r is None:
                    return None
                worst = r if worst is None else max(worst, r)
            return worst
        # group: 구성원 전부가 to 여야 하며, 남은 시간은 '가장 늦게 진입한' 구성원 기준(=최대값)
        eids = self._cache.entities_in_scope(target, self._inventory_fn())
        if not eids:
            return None
        worst = None
        for eid in eids:
            r = self._cache.hold_remaining(eid, to, duration)
            if r is None:
                return None  # 한 구성원이라도 to 가 아니면 유지 깨짐
            worst = r if worst is None else max(worst, r)
        return worst

    def _revalidate_held_timer(self, key, spec) -> None:
        """재연결 스냅샷 기준으로 armed 타이머를 유지/재장전/취소한다(fix 2)."""
        remaining = self._held_remaining(spec)
        if remaining is None:
            self._cancel_key(key)                 # 상태 != to → 유지 깨짐
            return
        handle = self._for_timers.get(key)
        if handle is None:
            return
        current = handle.when() - self._loop.time()
        if remaining <= 0:
            # 단절 중 유지가 완료됨 — 재연결 직후 자동 발화 금지(§4.2) → 취소
            self._cancel_key(key)
        elif remaining > current + _HOLD_EPS:
            # last_changed 가 원래 유지 시작 시각보다 뒤 → last_changed+for 로 재장전
            self._arm_timer(key, remaining)
        # else: 유지 연속성 정상 → 기존 타이머 유지

    def _arm_missing_held_timers(self) -> None:
        """현재 to 상태이지만 타이머가 없는 held 트리거를 잔여시간으로 장전(fix 3).

        이미 for 를 초과한 경우엔 장전하지 않는다(재연결 직후 발화 금지).
        """
        for rule in list(self._rules.values()):
            rid = rule["id"]
            for flat, si, ti, t in _iter_triggers(rule.get("model") or {}):
                spec = _held_spec(t)
                if spec is None:
                    continue
                key = (rid, flat)
                if key in self._for_timers or key in self._pending_expired:
                    continue  # 이미 타이머가 있거나, 재시작 pending 소유(아래 flush 담당)
                remaining = self._held_remaining(spec)
                if remaining is not None and remaining > 0:
                    self._arm_timer(key, remaining)

    def _eval_rule_triggers(self, rule, entity_id, old_state, new_state) -> None:
        for flat, si, ti, t in _iter_triggers(rule.get("model") or {}):
            typ = t.get("type")
            if typ == "state" and t.get("for"):
                if t.get("entity_id") == entity_id:
                    self._handle_held(rule, flat, t)
            elif typ in ("state", "numeric_state", "zone"):
                if t.get("entity_id") == entity_id and self._immediate_edge(t, old_state, new_state):
                    self._try_fire(rule, si, ti)
            elif typ == "state_held":
                if t.get("entity_id") == entity_id:
                    self._handle_held(rule, flat, t)
            elif typ == "group_held":
                self._handle_group_held(rule, flat, t)
            elif typ == "presence_agg":
                if entity_id in self._persons_of(t):
                    self._handle_presence(rule, flat, si, ti, t, old_state, new_state)

    @staticmethod
    def _immediate_edge(t, old_state, new_state) -> bool:
        new_val = new_state.get("state") if isinstance(new_state, dict) else None
        old_val = old_state.get("state") if isinstance(old_state, dict) else None
        typ = t.get("type")
        if typ == "state":
            to, frm = t.get("to"), t.get("from")
            if to is not None and new_val != to:
                return False
            if frm is not None and old_val != frm:
                return False
            return old_val != new_val
        if typ == "numeric_state":
            nv, ov = _num(new_val), _num(old_val)
            new_ok = nv is not None and _passes_bounds(nv, t.get("above"), t.get("below"))
            old_ok = ov is not None and _passes_bounds(ov, t.get("above"), t.get("below"))
            return new_ok and not old_ok
        if typ == "zone":
            target = t.get("zone")
            if t.get("event", "enter") == "leave":
                return old_val == target and new_val != target
            return new_val == target and old_val != target
        return False

    # ------------------------------------------------------------------ for 타이머
    def _handle_held(self, rule, flat, t) -> None:
        key = (rule["id"], flat)
        entry = self._cache.get(t.get("entity_id"))
        if entry is not None and entry.get("state") == t.get("to"):
            self._arm_timer(key, duration_to_seconds(t.get("for")))
        else:
            self._cancel_key(key)

    def _handle_group_held(self, rule, flat, t) -> None:
        key = (rule["id"], flat)
        if scope_all_state(t.get("scope"), t.get("to"), self._ctx()):
            if key not in self._for_timers:  # 그룹 유지 중엔 재설정하지 않음
                self._arm_timer(key, duration_to_seconds(t.get("for")))
        else:
            self._cancel_key(key)

    def _presence_level_ok(self, t) -> bool:
        """presence 결과 레벨이 지금 성립 중인가 — last=무인(count 0), all=전원 재실(count len)."""
        persons = self._persons_of(t)
        if not persons:
            return False
        cnt = sum(1 for p in persons
                  if (self._cache.get(p) or {}).get("state") == "home")
        return (cnt == 0) if t.get("quant") == "last" else (cnt == len(persons))

    def _handle_presence(self, rule, flat, si, ti, t, old_state, new_state) -> None:
        """person 상태 변경 시 집 인원 에지 판정(APP-PORT-PLAN §2.4).

        _on_event 가 캐시를 먼저 갱신하므로 new_count 는 현재 캐시 기준이고, old_count 는
        바뀐 pid 의 old/new home 여부로 보정한다. quant 별 에지:
          first: 0→≥1 · last: ≥1→0 · any: count↑ · all: 마지막 도착으로 count==len.
        for(last/all): 결과 레벨 유지 시 발화 — group_held 와 동일하게 유지 중엔 재설정하지
        않는다(_held_spec/_held_remaining 확장으로 재시작·재연결 복원 상속).
        """
        persons = self._persons_of(t)
        if not persons:
            return
        quant = t.get("quant")
        n = len(persons)
        new_count = sum(1 for p in persons
                        if (self._cache.get(p) or {}).get("state") == "home")
        new_home = (new_state.get("state") if isinstance(new_state, dict) else None) == "home"
        old_home = (old_state.get("state") if isinstance(old_state, dict) else None) == "home"
        old_count = new_count - (1 if new_home else 0) + (1 if old_home else 0)

        if quant in ("last", "all") and t.get("for"):
            key = (rule["id"], flat)
            level_ok = (new_count == 0) if quant == "last" else (new_count == n)
            if level_ok:
                if key not in self._for_timers:  # 유지 중엔 재설정하지 않음(group_held 동형)
                    self._arm_timer(key, duration_to_seconds(t.get("for")))
            else:
                self._cancel_key(key)  # 레벨 붕괴(귀가/외출) → 취소
            return

        if quant == "first":
            edge = old_count == 0 and new_count >= 1
        elif quant == "last":
            edge = old_count >= 1 and new_count == 0
        elif quant == "any":
            edge = new_count > old_count
        elif quant == "all":
            edge = new_count == n and old_count < new_count
        else:
            return
        if edge:
            self._try_fire(rule, si, ti)

    def _arm_timer(self, key, seconds) -> None:
        self._cancel_key(key)  # cancel-before-replace
        self._for_timers[key] = self._loop.call_later(
            max(0.0, seconds), self._on_hold_expire, key[0], key[1])

    def _cancel_key(self, key) -> None:
        h = self._for_timers.pop(key, None)
        if h is not None:
            h.cancel()

    def _on_hold_expire(self, rid, flat) -> None:
        self._for_timers.pop((rid, flat), None)
        rule = self._rules.get(rid)
        if rule is None:
            return
        try:
            loc = _locate(rule.get("model") or {}, flat)
            if loc is None:
                return
            si, ti, t = loc
            if t.get("type") == "group_held":
                if scope_all_state(t.get("scope"), t.get("to"), self._ctx()):
                    self._try_fire(rule, si, ti)
            elif t.get("type") == "presence_agg":
                if self._presence_level_ok(t):  # 만료 후 레벨 재확인(무인/전원재실 유지)
                    self._try_fire(rule, si, ti)
            else:
                entry = self._cache.get(t.get("entity_id"))
                if entry is not None and entry.get("state") == t.get("to"):
                    self._try_fire(rule, si, ti)
        except Exception:
            log.exception("for 타이머 만료 처리 오류")

    # ------------------------------------------------------------------ 경계/daily 스케줄
    def _schedule_boundary(self) -> None:
        try:
            nb = self._gvars.next_boundary()
            delay = (nb - self._now_fn()).total_seconds()
        except Exception:
            delay = 3600.0
        self._boundary_timer = self._loop.call_later(max(1.0, delay), self._on_boundary)

    def _on_boundary(self) -> None:
        try:
            new_seg = self._gvars.segment()
            if new_seg != self._last_segment:  # 세그먼트 실제 전환 시에만(fix 5)
                self._last_segment = new_seg
                for rule in list(self._rules.values()):
                    for flat, si, ti, t in _iter_triggers(rule.get("model") or {}):
                        if t.get("type") == "segment" and t.get("to") == new_seg:
                            self._try_fire(rule, si, ti)
                self._reeval_time_segment(new_seg)  # 경계 time_segment 조건 재평가(fix 6)
        except Exception:
            log.exception("시간대 경계 처리 오류")
        finally:
            self._schedule_boundary()  # 벽시계 기준 self-rescheduling

    def _reeval_time_segment(self, new_seg) -> None:
        """새 세그먼트 진입 시, time_segment 조건을 가진 서브룰 중 트리거가 '현재 성립 중'인
        것을 재평가해 조건 통과 시 발화한다(EventRunner 구간 경계 의미론, fix 6)."""
        for rule in list(self._rules.values()):
            model = rule.get("model") or {}
            for si, sub in enumerate(_subrules(model)):
                conds = sub.get("conditions") or []
                if not any(c.get("type") == "time_segment" for c in conds):
                    continue
                for ti, t in enumerate(sub.get("triggers") or []):
                    if self._trigger_currently_true(t):
                        self._try_fire(rule, si, ti)  # 조건은 _try_fire 가 재평가
                        break

    def _trigger_currently_true(self, t) -> bool:
        """트리거가 지금 성립 중인가 — state: 현재 상태==to, held: 유지 완료 상태."""
        typ = t.get("type")
        if typ == "state" and not t.get("for"):
            entry = self._cache.get(t.get("entity_id"))
            return entry is not None and entry.get("state") == t.get("to")
        spec = _held_spec(t)
        if spec is not None:
            r = self._held_remaining(spec)
            return r is not None and r <= 0  # 유지 완료(for 경과)
        return False

    def _schedule_daily(self, rid, flat, t) -> None:
        at = str(t.get("at") or "00:00")
        try:
            h, m = int(at.split(":")[0]), int(at.split(":")[1])
        except (ValueError, IndexError):
            return
        now = self._now_fn()
        cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        delay = (cand - now).total_seconds()
        self._daily_timers[(rid, flat)] = self._loop.call_later(
            max(1.0, delay), self._on_daily, rid, flat)

    def _on_daily(self, rid, flat) -> None:
        self._daily_timers.pop((rid, flat), None)
        rule = self._rules.get(rid)
        if rule is None:
            return
        loc = _locate(rule.get("model") or {}, flat)
        try:
            if loc is not None:
                self._try_fire(rule, loc[0], loc[1])
        except Exception:
            log.exception("daily 트리거 처리 오류")
        finally:
            if loc is not None:
                self._schedule_daily(rid, flat, loc[2])

    def _schedule_sun(self, rid, flat, t) -> None:
        """sun 트리거의 다음 (event+offset) 시각으로 타이머를 무장한다(APP-PORT-PLAN §2.2).

        _daily_timers 를 재사용하므로 stop/_unindex_rule/_compile_all 의 취소 경로가 그대로
        적용된다. sun_provider 미주입 시 무스케줄(발화 없음). 재시작/재연결은 _compile_all/
        resync 가 재계산만 하며 이 콜백을 직접 호출하지 않으므로 자동 발화가 없다(§4.2).
        """
        if self._sun is None:
            return
        event = t.get("event")
        try:
            offset = int(t.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        now = self._now_fn()
        try:
            when = self._sun.next_event(event, offset, now)
            delay = (when - now).total_seconds()
        except Exception:
            log.exception("sun 트리거 스케줄 계산 오류")
            return
        self._daily_timers[(rid, flat)] = self._loop.call_later(
            max(1.0, delay), self._on_sun, rid, flat)

    def _on_sun(self, rid, flat) -> None:
        self._daily_timers.pop((rid, flat), None)
        rule = self._rules.get(rid)
        if rule is None:
            return
        loc = _locate(rule.get("model") or {}, flat)
        try:
            if loc is not None:
                self._try_fire(rule, loc[0], loc[1])
        except Exception:
            log.exception("sun 트리거 처리 오류")
        finally:
            if loc is not None:
                self._schedule_sun(rid, flat, loc[2])  # 익일 재장전(next_event 가 내일 반환)

    @staticmethod
    def _next_pattern_time(now: datetime, unit: str, n: int) -> datetime:
        """벽시계 필드가 N 배수인 **now 보다 엄격히 이후**의 다음 시각(HA `/N` 동형).

        seconds: second%N==0(시/분 무관) · minutes: minute%N==0·second=0 ·
        hours: hour%N==0·minute=second=0. 분/초 경계에서 롤오버 시 하위 필드 0 은 항상
        N 의 배수(0%N==0)라 자연히 다음 슬롯으로 넘어간다. 결정적(주입 now 로만 통제).
        """
        cand = now.replace(microsecond=0)
        if unit == "minutes":
            cand = cand.replace(second=0)
            step, field = timedelta(minutes=1), "minute"
        elif unit == "hours":
            cand = cand.replace(minute=0, second=0)
            step, field = timedelta(hours=1), "hour"
        else:  # seconds
            step, field = timedelta(seconds=1), "second"
        for _ in range(1500):  # 안전 상한(최악 60틱이면 충분, 무한루프 방지)
            if cand > now and getattr(cand, field) % n == 0:
                return cand
            cand += step
        return cand

    def _schedule_pattern(self, rid, flat, t) -> None:
        """time_pattern 트리거의 다음 배수 시각으로 타이머를 무장한다(APP-PORT-PLAN §2.3).

        hours|minutes|seconds 중 정수 N(≥1) 하나를 읽어 다음 배수 시각을 계산한다(HA `/N`).
        _daily_timers 를 재사용하므로 stop/_unindex_rule/_compile_all 의 취소 경로가 그대로
        적용된다. 재시작(_compile_all)·재연결(resync)은 재계산만 하며 이 콜백을 직접 호출하지
        않으므로 자동 발화가 없다(§4.2 게이트4). 발화 시 조건 재평가는 _try_fire 가 담당.
        """
        unit = n = None
        for k in ("seconds", "minutes", "hours"):
            v = t.get(k)
            if v is None:
                continue
            try:
                cand_n = int(str(v).lstrip("/"))  # 정수 또는 "/N" 문자열 모두 허용
            except (TypeError, ValueError):
                return
            if cand_n >= 1:
                unit, n = k, cand_n
                break
        if unit is None:
            return
        now = self._now_fn()
        when = self._next_pattern_time(now, unit, n)
        delay = (when - now).total_seconds()
        self._daily_timers[(rid, flat)] = self._loop.call_later(
            max(1.0, delay), self._on_pattern, rid, flat)

    def _on_pattern(self, rid, flat) -> None:
        self._daily_timers.pop((rid, flat), None)
        rule = self._rules.get(rid)
        if rule is None:
            return
        loc = _locate(rule.get("model") or {}, flat)
        try:
            if loc is not None:
                self._try_fire(rule, loc[0], loc[1])
        except Exception:
            log.exception("time_pattern 트리거 처리 오류")
        finally:
            if loc is not None:
                self._schedule_pattern(rid, flat, loc[2])  # 다음 배수 시각으로 재장전

    # ------------------------------------------------------------------ 발화
    def _ctx(self, fired_index=None) -> EvalContext:
        return EvalContext(self._cache, self._gvars, self._now_fn, self._inventory_fn,
                           fired_index, self._mode_state, self._sun)

    def _try_fire(self, rule, si, ti, depth=0, context="trigger") -> None:
        rid = rule["id"]
        now_t = self._loop.time()
        last = self._last_fired.get((rid, si))
        if last is not None and (now_t - last) < _COOLDOWN:
            return
        sub = _subrule_at(rule.get("model") or {}, si)
        if sub is None:
            return
        ctx = self._ctx(ti)
        if not evaluate_conditions(sub, ctx):
            self._runlog.add(rid, self._sentence(rule), "skipped_condition", "조건 불충족")
            return
        self._last_fired[(rid, si)] = now_t
        self._launch(rule, si, context, depth)

    def _launch(self, rule, si, context, depth=0) -> None:
        rid = rule["id"]
        task = self._loop.create_task(self._execute(rule, si, context, depth))
        self._tasks.add(task)  # 강참조 보관
        self._exec_tasks.setdefault(rid, set()).add(task)  # rule 별 추적(fix 4)

        def _done(t, rid=rid):
            self._tasks.discard(t)
            s = self._exec_tasks.get(rid)
            if s is not None:
                s.discard(t)
                if not s:
                    self._exec_tasks.pop(rid, None)
        task.add_done_callback(_done)

    async def fire_rule(self, rule: dict, context: str) -> None:
        """조건 무시하고 액션 실행(수동 'run'). 모든 서브룰의 액션을 순차 실행한다."""
        for si, _sub in enumerate(_subrules(rule.get("model") or {})):
            await self._execute(rule, si, context)

    async def _execute(self, rule, si, context, depth=0) -> None:
        try:
            sub = _subrule_at(rule.get("model") or {}, si)
            actions = (sub.get("actions") if sub else None) or []
            await self._run_sequence(actions, self._ctx(), depth)
            self._on_success(rule, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._on_error(rule, e)

    def _on_success(self, rule, context) -> None:
        meta = rule.setdefault("meta", {})
        meta["last_fired"] = _utc_iso()
        meta["fire_count"] = int(meta.get("fire_count") or 0) + 1
        meta["last_error"] = None
        self._error_streak[rule["id"]] = 0
        self._rule_store.save()
        self._runlog.add(rule["id"], self._sentence(rule), "fired", context)

    def _on_error(self, rule, exc) -> None:
        rid = rule["id"]
        detail = str(exc)[:300]
        meta = rule.setdefault("meta", {})
        meta["last_error"] = detail
        self._error_streak[rid] = self._error_streak.get(rid, 0) + 1
        self._runlog.add(rid, self._sentence(rule), "error", detail)
        if self._error_streak[rid] >= _ERROR_LIMIT:
            meta["auto_disabled"] = True
            self._unindex_rule(rid)  # 라우팅에서 제거
        self._rule_store.save()

    @staticmethod
    def _sentence(rule) -> str:
        return rule.get("sentence") or rule.get("name") or rule.get("id") or ""

    # ------------------------------------------------------------------ 액션 실행
    async def _run_sequence(self, actions, ctx, depth=0) -> bool:
        for a in actions:
            if await self._run_action(a, ctx, depth) is False:
                return False
        return True

    async def _run_action(self, a, ctx, depth=0) -> bool:
        typ = a.get("type")
        if typ == "service":
            await self._call_service(a)
            return True
        if typ == "set_mode":
            self.set_mode(a.get("mode"), a.get("to") == "on", "action", depth + 1)
            return True
        if typ == "delay":
            await asyncio.sleep(duration_to_seconds(a.get("duration")))
            return True
        if typ == "condition":
            return evaluate_condition(a.get("condition") or {}, ctx)
        if typ == "stop":
            return False
        if typ == "if":
            ok = all(evaluate_condition(c, ctx) for c in (a.get("if") or []))
            seq = a.get("then") if ok else (a.get("else") or [])
            return await self._run_sequence(seq or [], ctx, depth)
        if typ == "choose":
            for opt in a.get("options") or []:
                if all(evaluate_condition(c, ctx) for c in (opt.get("conditions") or [])):
                    return await self._run_sequence(opt.get("sequence") or [], ctx, depth)
            return await self._run_sequence(a.get("default") or [], ctx, depth)
        if typ == "repeat":
            return await self._run_repeat(a, ctx, depth)
        if typ == "parallel":
            await asyncio.gather(*[
                self._run_sequence(br, ctx, depth) for br in (a.get("branches") or [])])
            return True
        if typ in ("wait_template", "wait_for_trigger"):
            return True  # 엔진 미구현(파서 미생성) — no-op
        log.debug("미지원 액션 유형: %s", typ)
        return True

    async def _run_repeat(self, a, ctx, depth=0) -> bool:
        kind = a.get("kind")
        seq = a.get("sequence") or []
        if kind == "count":
            for _ in range(int(a.get("count") or 0)):
                if await self._run_sequence(seq, ctx, depth) is False:
                    return False
        elif kind == "while":
            n = 0
            while n < _MAX_REPEAT and all(
                    evaluate_condition(c, ctx) for c in (a.get("conditions") or [])):
                if await self._run_sequence(seq, ctx, depth) is False:
                    return False
                n += 1
        elif kind == "until":
            n = 0
            while n < _MAX_REPEAT:
                if await self._run_sequence(seq, ctx, depth) is False:
                    return False
                if all(evaluate_condition(c, ctx) for c in (a.get("conditions") or [])):
                    break
                n += 1
        return True

    async def _call_service(self, a) -> None:
        action = str(a.get("action") or "")
        if "." not in action:
            raise ValueError(f"서비스 형식이 올바르지 않습니다: {action}")
        domain, service = action.split(".", 1)
        data = dict(a.get("data") or {})
        for k, v in (a.get("target") or {}).items():
            if v:
                data[k] = v
        await self._ha.call_service(domain, service, data)

    # ------------------------------------------------------------------ pending_timers
    def _save_pending(self) -> None:
        if self._pending_store is None:
            return
        pending = []
        for (rid, flat), handle in self._for_timers.items():
            remaining = handle.when() - self._loop.time()
            expiry = self._now_fn() + timedelta(seconds=max(0.0, remaining))
            pending.append({"rule_id": rid, "node_index": flat, "expiry": expiry.isoformat()})
        self._pending_store.data = pending
        self._pending_store.save_soon()

    def _load_pending(self) -> None:
        """pending_timers.json 복원(fix 1).

        미만료 엔트리는 지금처럼 잔여시간으로 타이머 arm(첫 resync 에서 재검증).
        만료 엔트리는 즉시 발화하지 않고 self._pending_expired 에 보관했다가,
        StateCache 가 채워지는 첫 on_resync 이후에 조건 재확인 후 발화한다.
        (HAEventSource.start 직후엔 캐시가 비어 만료 pending 이 유실될 수 있기 때문)
        """
        if self._pending_store is None:
            return
        entries = self._pending_store.data or []
        now = self._now_fn()
        self._pending_expired = []
        for e in entries:
            rid, flat = e.get("rule_id"), e.get("node_index")
            if self._rules.get(rid) is None:
                continue
            try:
                expiry = datetime.fromisoformat(e.get("expiry"))
            except (ValueError, TypeError):
                continue
            remaining = (expiry - now).total_seconds()
            if remaining <= 0:
                self._pending_expired.append((rid, flat))  # 첫 resync 이후 처리
            else:
                self._for_timers[(rid, flat)] = self._loop.call_later(
                    remaining, self._on_hold_expire, rid, flat)
        self._pending_store.data = []
        self._pending_store.save_soon()

    def _flush_expired_pending(self) -> None:
        """첫 resync 이후 만료 pending 을 조건 재확인 후 발화한다(fix 1).

        재시작 전 이미 arm 돼 있던 타이머이므로(사용자 의도 확정) 발화가 맞다 —
        재연결 resync 의 '발화 금지' 원칙과 구분된다.
        """
        pend = self._pending_expired
        self._pending_expired = []
        for rid, flat in pend:
            try:
                self._on_hold_expire(rid, flat)  # 콜백이 조건(상태==to)을 재확인 후 발화
            except Exception:
                log.exception("복원 pending 만료 처리 오류")
