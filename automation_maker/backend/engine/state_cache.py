"""엔티티 상태 캐시 (§4.1). last_changed는 HA 타임스탬프 기준."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        s = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


class StateCache:
    def __init__(self, now_fn=None):
        # entity_id → {"state","attributes","last_changed": datetime, "last_updated": datetime}
        self._states: dict[str, dict] = {}
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    def replace_all(self, states: list[dict]) -> None:
        """get_states 스냅샷. 이미 더 최신 이벤트를 받은 엔티티는 덮어쓰지 않는다."""
        now = self._now()
        for s in states or []:
            eid = s.get("entity_id")
            if not eid:
                continue
            lu = _parse_ts(s.get("last_updated")) or _parse_ts(s.get("last_changed")) or now
            prev = self._states.get(eid)
            if prev is not None and prev.get("last_updated") is not None and prev["last_updated"] > lu:
                continue  # 스냅샷보다 최신 이벤트 보존(재연결 순서 뒤바뀜 방지)
            self._states[eid] = {
                "state": s.get("state"),
                "attributes": dict(s.get("attributes") or {}),
                "last_changed": _parse_ts(s.get("last_changed")) or lu,
                "last_updated": lu,
            }

    def apply_event(self, entity_id, old_state, new_state) -> bool:
        """state_changed 반영. 상태값이 실제로 바뀌었으면 True(트리거 평가 대상)."""
        now = self._now()
        if new_state is None:
            # 엔티티 제거
            existed = self._states.pop(entity_id, None)
            return existed is not None
        lu = _parse_ts(new_state.get("last_updated")) or _parse_ts(new_state.get("last_changed")) or now
        prev = self._states.get(entity_id)
        if prev is not None and prev.get("last_updated") is not None and lu < prev["last_updated"]:
            return False  # 오래된(순서 뒤바뀐) 이벤트 무시
        new_val = new_state.get("state")
        old_val = old_state.get("state") if isinstance(old_state, dict) else (
            prev.get("state") if prev else None)
        lc = _parse_ts(new_state.get("last_changed"))
        if lc is None:
            # 상태값 동일이면 last_changed 유지, 바뀌었으면 now
            lc = prev["last_changed"] if (prev and old_val == new_val) else now
        self._states[entity_id] = {
            "state": new_val,
            "attributes": dict(new_state.get("attributes") or {}),
            "last_changed": lc,
            "last_updated": lu,
        }
        return old_val != new_val

    def get(self, entity_id) -> dict | None:
        return self._states.get(entity_id)

    def held_for(self, entity_id, state: str, duration: timedelta) -> bool:
        entry = self._states.get(entity_id)
        if entry is None or entry.get("state") != state:
            return False
        lc = entry.get("last_changed")
        if lc is None:
            return False
        return (self._now() - lc) >= duration

    def hold_remaining(self, entity_id, state: str, duration: timedelta) -> float | None:
        """state 유지 중이면 duration 완료까지 남은 초(초과 시 음수).

        상태가 state 가 아니거나 last_changed 를 알 수 없으면 None.
        비교는 캐시 자체 시계(now_fn) 기준이라 엔진 벽시계와의 tz 혼용을 피한다.
        """
        entry = self._states.get(entity_id)
        if entry is None or entry.get("state") != state:
            return None
        lc = entry.get("last_changed")
        if lc is None:
            return None
        elapsed = (self._now() - lc).total_seconds()
        return duration.total_seconds() - elapsed

    def entities_in_scope(self, scope: dict, inventory) -> list[str]:
        """Scope 해석 → 매칭 entity_id 목록. inventory는 bootstrap entities."""
        scope = scope or {}
        ents = inventory.get("entities") if isinstance(inventory, dict) else inventory
        ents = ents or []
        dclasses = scope.get("device_class")
        domain = scope.get("domain")
        area_id = scope.get("area_id")
        except_area = scope.get("except_area_id")

        out: list[str] = []
        for e in ents:
            eid = e.get("entity_id")
            if not eid:
                continue
            edomain = e.get("domain") or eid.split(".", 1)[0]
            if domain and edomain != domain:
                continue
            if dclasses:
                dc = e.get("device_class")
                if dc is None:
                    cached = self._states.get(eid)
                    if cached:
                        dc = cached.get("attributes", {}).get("device_class")
                if dc not in dclasses:
                    continue
            earea = e.get("area_id")
            if area_id and earea != area_id:
                continue
            if except_area and earea == except_area:
                continue
            out.append(eid)
        return out
