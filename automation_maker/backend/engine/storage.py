"""데이터 디렉터리 및 원자적 쓰기 + 디바운스 저장 JsonStore (§0)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

_DEBOUNCE = 0.5  # 저장 디바운스(초)


def data_dir() -> Path:
    """DATA_DIR 환경변수 → /data 폴백. 없으면 생성해서 반환한다."""
    raw = os.environ.get("DATA_DIR")
    path = Path(raw) if raw else Path("/data")
    path.mkdir(parents=True, exist_ok=True)
    return path


class JsonStore:
    """단일 JSON 파일의 인메모리 표현. 쓰기는 tmp→os.replace로 원자적, 저장은 0.5초 디바운스."""

    def __init__(self, path: Path, default: Any, loop: asyncio.AbstractEventLoop | None = None):
        self.path = Path(path)
        self._loop = loop
        self._timer: asyncio.TimerHandle | None = None
        self._dirty = False
        self.data = self._load(default)

    def _load(self, default: Any) -> Any:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except (ValueError, OSError):
            # 손상 파일은 기본값으로 폴백(크래시 금지)
            return default

    def _resolve_loop(self) -> asyncio.AbstractEventLoop | None:
        if self._loop is not None:
            return self._loop
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            return None

    def save_soon(self) -> None:
        """0.5초 디바운스 저장 예약. 실행 중 루프가 없으면 즉시 동기 저장."""
        self._dirty = True
        loop = self._resolve_loop()
        if loop is None:
            self._write()
            return
        if self._timer is not None:
            self._timer.cancel()
        self._timer = loop.call_later(_DEBOUNCE, self._write)

    def _write(self) -> None:
        self._timer = None
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, self.path)
        self._dirty = False

    async def flush(self) -> None:
        """즉시 저장(종료 시). 예약된 디바운스 타이머는 취소하고 현재 data를 반드시 기록한다."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._dirty = True
        self._write()
