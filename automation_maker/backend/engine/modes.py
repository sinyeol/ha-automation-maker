"""상태를 가진 모드 변수 (SPEC-V3 §1.2).

모드는 on/off 상태를 갖는 내부 변수다. 정의(초기값·side-effect)는 settings.modes 에서,
현재 상태는 /data/modes_state.json(JsonStore)에서 로드/저장한다.
v2 형식({action,target,data})은 로드 시 on_action 형식으로 마이그레이션한다.
"""
from __future__ import annotations

from .storage import JsonStore

_ON = "on"
_OFF = "off"


def _normalize_def(d) -> dict:
    """모드 정의를 v3 형식 {initial, on_action, off_action}로 정규화한다.

    v2 형식({action,target,data})은 on_action 으로 변환(off_action=null, initial=off).
    """
    if not isinstance(d, dict):
        return {"initial": _OFF, "on_action": None, "off_action": None}
    # 이미 v3 형식(신형 키가 하나라도 있으면 신형으로 간주)
    if "on_action" in d or "off_action" in d or "initial" in d:
        return {
            "initial": _ON if d.get("initial") == _ON else _OFF,
            "on_action": d.get("on_action") or None,
            "off_action": d.get("off_action") or None,
        }
    # v2 레거시: {action, target, data} → on_action 으로 승격
    if d.get("action"):
        on_action = {"action": d.get("action")}
        if d.get("target"):
            on_action["target"] = d.get("target")
        if d.get("data"):
            on_action["data"] = d.get("data")
        return {"initial": _OFF, "on_action": on_action, "off_action": None}
    return {"initial": _OFF, "on_action": None, "off_action": None}


class ModeState:
    def __init__(self, settings: dict, store: JsonStore):
        self._settings = settings if isinstance(settings, dict) else {}
        self._store = store
        # store.data = {name: "on"|"off"} — 재시작 간 유지되는 런타임 상태
        if not isinstance(store.data, dict):
            store.data = {}
        self._runtime: dict = store.data
        self.sync_settings(self._settings)

    # ------------------------------------------------------------------ 정의 접근
    def _defs(self) -> dict:
        defs = self._settings.get("modes")
        return defs if isinstance(defs, dict) else {}

    def names(self) -> list[str]:
        return list(self._defs().keys())

    def definition(self, name: str) -> dict:
        """정규화된 모드 정의({initial, on_action, off_action})."""
        return _normalize_def(self._defs().get(name))

    def side_effect(self, name: str, state: str):
        """모드가 state 로 바뀔 때 실행할 서비스 액션 정의(없으면 None)."""
        d = self.definition(name)
        return d.get("on_action") if state == _ON else d.get("off_action")

    # ------------------------------------------------------------------ 상태
    def get(self, name: str) -> str:
        return self._runtime.get(name, _OFF)

    def snapshot(self) -> dict:
        return dict(self._runtime)

    def set(self, name: str, on: bool) -> bool:
        """상태를 설정한다. 실제로 바뀌었으면 True(persist 는 호출측 책임).

        정의(settings.modes)에 없는 모드는 무시하고 False 를 돌려준다. 이렇게 하지 않으면
        삭제/미정의 모드로 set(True) 했을 때 유령 상태가 modes_state.json 에 영속되고,
        그 모드가 나중에 재정의되면 sync_settings 가 initial 을 무시하게 된다(SPEC-V3 §1.2).
        """
        if name not in self._defs():
            return False
        new = _ON if on else _OFF
        if self._runtime.get(name, _OFF) == new:
            return False
        self._runtime[name] = new
        return True

    def save(self) -> None:
        self._store.save_soon()

    # ------------------------------------------------------------------ 설정 동기화
    def sync_settings(self, settings) -> None:
        """설정 변경 반영: 정의 정규화 + 새 모드 초기화 + 삭제된 모드 제거."""
        self._settings = settings if isinstance(settings, dict) else {}
        defs = self._settings.get("modes")
        if not isinstance(defs, dict):
            defs = {}
            self._settings["modes"] = defs
        # settings 안의 정의를 v3 형식으로 제자리 정규화(마이그레이션)
        for name in list(defs.keys()):
            defs[name] = _normalize_def(defs[name])
        # 새로 정의된 모드는 초기값으로 런타임에 추가
        for name, d in defs.items():
            if name not in self._runtime:
                self._runtime[name] = d.get("initial", _OFF)
        # 설정에서 사라진 모드는 런타임에서 제거
        for name in list(self._runtime.keys()):
            if name not in defs:
                del self._runtime[name]
