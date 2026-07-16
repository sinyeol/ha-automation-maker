"""규칙 저장 (§2). JsonStore(rules.json) 위의 CRUD."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .storage import JsonStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_meta() -> dict:
    return {
        "created": _now_iso(),
        "updated": _now_iso(),
        "last_fired": None,
        "fire_count": 0,
        "last_error": None,
        "auto_disabled": False,
    }


class RuleStore:
    def __init__(self, store: JsonStore):
        self._store = store
        if not isinstance(self._store.data, list):
            self._store.data = []

    def save(self) -> None:
        """엔진이 meta를 직접 갱신한 뒤 영속을 예약할 때 사용."""
        self._store.save_soon()

    def all(self) -> list[dict]:
        return list(self._store.data)

    def get(self, rule_id) -> dict | None:
        for r in self._store.data:
            if r.get("id") == rule_id:
                return r
        return None

    def upsert(self, rule: dict) -> dict:
        rule = dict(rule)
        meta = dict(rule.get("meta") or {})
        rule_id = rule.get("id")
        existing = self.get(rule_id) if rule_id else None

        base = dict(existing.get("meta") or {}) if existing else _default_meta()
        base.update(meta)
        # 신규는 created 보장, 항상 updated 갱신
        base.setdefault("created", _now_iso())
        for key, val in _default_meta().items():
            base.setdefault(key, val)
        base["updated"] = _now_iso()
        rule["meta"] = base

        rule.setdefault("name", "")
        rule.setdefault("enabled", True)
        rule.setdefault("pins", {})
        rule.setdefault("area_id", None)
        rule.setdefault("category", None)

        if not rule_id:
            rule["id"] = uuid4().hex
            self._store.data.append(rule)
        else:
            for i, r in enumerate(self._store.data):
                if r.get("id") == rule_id:
                    self._store.data[i] = rule
                    break
            else:
                self._store.data.append(rule)
        self._store.save_soon()
        return rule

    def delete(self, rule_id) -> bool:
        data = self._store.data
        for i, r in enumerate(data):
            if r.get("id") == rule_id:
                del data[i]
                self._store.save_soon()
                return True
        return False

    def set_enabled(self, rule_id, on: bool) -> dict | None:
        r = self.get(rule_id)
        if r is None:
            return None
        r["enabled"] = bool(on)
        meta = r.setdefault("meta", _default_meta())
        if on:
            # 재활성화 시 오류 상태 해제
            meta["auto_disabled"] = False
            meta["last_error"] = None
        meta["updated"] = _now_iso()
        self._store.save_soon()
        return r
