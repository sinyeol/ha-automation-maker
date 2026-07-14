"""공용 픽스처 및 import 경로 조정.

tests/ 는 automation_maker/ 아래에 있고, 백엔드는 `from backend import ...` 로
불러온다. 따라서 automation_maker 디렉터리(= 이 파일의 상위의 상위)를 sys.path 에
넣어 import 루트로 삼는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# automation_maker/ 를 import 루트로
_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))


# ---------------------------------------------------------------------------
# 모델 헬퍼 (여러 테스트에서 공용)
# ---------------------------------------------------------------------------
def dur(hours: int = 0, minutes: int = 0, seconds: int = 0) -> dict:
    """UI 모델의 Duration 객체(§4)."""
    return {"hours": hours, "minutes": minutes, "seconds": seconds}


# 검증을 통과하는 최소 트리거/액션 (개별 노드 골든 테스트의 채움용)
FILLER_TRIGGER = {"type": "time", "at": "07:00"}
FILLER_ACTION = {"type": "service", "action": "light.turn_on"}


def make_model(*, triggers=None, conditions=None, condition_mode="and",
               actions=None, alias="테스트 자동화", **extra) -> dict:
    """§4 AutomationModel. 지정하지 않은 부분은 검증을 통과하는 기본값으로 채운다."""
    m = {
        "alias": alias,
        "description": "",
        "mode": extra.pop("mode", "single"),
        "triggers": triggers if triggers is not None else [dict(FILLER_TRIGGER)],
        "condition_mode": condition_mode,
        "conditions": conditions if conditions is not None else [],
        "actions": actions if actions is not None else [dict(FILLER_ACTION)],
    }
    m.update(extra)
    return m


@pytest.fixture
def model_factory():
    return make_model


# ---------------------------------------------------------------------------
# DEV_MODE aiohttp 앱 팩토리 (MockHAClient 기반)
# ---------------------------------------------------------------------------
@pytest.fixture
def make_app(monkeypatch):
    """create_app 이 DEV_MODE 환경변수를 호출 시점에 읽으므로, 앱 생성 직전에 설정한다."""
    def _make(dev: bool = True):
        if dev:
            monkeypatch.setenv("DEV_MODE", "1")
        else:
            monkeypatch.delenv("DEV_MODE", raising=False)
        from backend.app import create_app
        from backend.mock_data import MockHAClient
        return create_app(MockHAClient())
    return _make


@pytest.fixture
def valid_model_payload():
    """POST/PUT api/automations 에 넣을 유효한 {"model": ...} 본문."""
    return {
        "model": make_model(
            alias="거실 저녁등",
            triggers=[{"type": "state", "entity_id": "light.living_room_main", "to": "on"}],
            actions=[{"type": "service", "action": "light.turn_on",
                      "target": {"entity_id": ["light.kitchen"]}}],
        )
    }
