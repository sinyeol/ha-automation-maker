"""자체 규칙 엔진 (v2). 상태 소스는 HA WS, 액션은 서비스 호출로만 사용한다."""
from __future__ import annotations

from .engine import RuleEngine
from .event_source import EventSource, HAEventSource, MockEventSource
from .rule_model import validate_rule_model
from .rule_store import RuleStore
from .runlog import RunLog
from .state_cache import StateCache
from .storage import JsonStore, data_dir
from .variables import SEGMENT_LABELS, SEGMENTS, GlobalVars

__all__ = [
    "RuleEngine",
    "EventSource",
    "HAEventSource",
    "MockEventSource",
    "validate_rule_model",
    "RuleStore",
    "RunLog",
    "StateCache",
    "JsonStore",
    "data_dir",
    "GlobalVars",
    "SEGMENTS",
    "SEGMENT_LABELS",
]
