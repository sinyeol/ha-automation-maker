"""실행 로그 — ring buffer 200건, JsonStore로 영속 (§4.3-7)."""
from __future__ import annotations

from datetime import datetime, timezone

from .storage import JsonStore

_MAX = 200
_RESULTS = {"fired", "error", "skipped_condition"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunLog:
    def __init__(self, store: JsonStore):
        self._store = store
        if not isinstance(self._store.data, list):
            self._store.data = []

    def add(self, rule_id: str, sentence: str, result: str, detail: str = "") -> dict:
        entry = {
            "ts": _now_iso(),
            "rule_id": rule_id,
            "sentence": sentence,
            "result": result if result in _RESULTS else "skipped_condition",
            "detail": detail or "",
        }
        buf = self._store.data
        buf.append(entry)
        if len(buf) > _MAX:
            del buf[:-_MAX]  # 오래된 항목 절단(ring)
        self._store.save_soon()
        return entry

    def entries(self, limit: int = _MAX) -> list[dict]:
        """최신순 최대 limit건."""
        buf = self._store.data
        out = list(reversed(buf))
        return out[:limit] if limit else out
