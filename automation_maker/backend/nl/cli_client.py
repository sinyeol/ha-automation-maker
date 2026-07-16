"""SPEC-V3 §4.3 — Claude Code CLI(구독) 헤드리스 호출로 파싱 보조.

구독(Max) 로그인 상태의 `claude` 바이너리를 subprocess로 띄워, 도구·파일 접근 없이
순수 텍스트 파싱만 시킨다. 표준 라이브러리만 사용(새 파이썬 의존성 없음).

인증 함정(SPEC-V3 §4.3, 필수): 인증 우선순위에서 ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN이
CLAUDE_CODE_OAUTH_TOKEN을 이기므로, 호출 env에서 그 둘을 반드시 제거한다. --bare는 OAuth
토큰을 무시하므로 사용하지 않는다. 토큰이 없어도 개발 컨테이너 /login 상태면 동작한다.

토큰/키는 어떤 경로로도 로그에 남기지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
from pathlib import Path
from typing import Optional

log = logging.getLogger("automation_maker.cli")

_TIMEOUT = 60  # wait_for 상한(초)
# 동시 실행 제한 — CLI는 무겁다. 이벤트 루프에서 lazy 바인딩되므로 모듈 로드 시 생성 안전.
_SEMAPHORE = asyncio.Semaphore(2)

# --json-schema: 얕게 강제(SPEC-V3 §4.3). model은 자유 객체, warnings는 문자열 배열.
_STRUCT_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "object"},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["model"],
}


def _find_binary() -> Optional[str]:
    """CLAUDE_BIN 환경 → PATH의 claude. 없으면 None(백엔드 off와 동일 취급)."""
    explicit = os.environ.get("CLAUDE_BIN")
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    return shutil.which("claude")


def _work_dir() -> Path:
    """cwd의 CLAUDE.md/.claude 자동로드를 막기 위한 빈 격리 디렉터리.

    /data 우선(애드온 영속 볼륨), 실패하면 /tmp.
    """
    for base in ("/data/claude-work", "/tmp/claude-work"):
        try:
            p = Path(base)
            p.mkdir(parents=True, exist_ok=True)
            return p
        except OSError:
            continue
    # 최후: 현재 임시 디렉터리
    p = Path("/tmp") / "claude-work"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _build_env(oauth_token: str) -> dict:
    """인증 함정 대응 env. ANTHROPIC_* 제거 + OAuth 토큰만 주입 + 오토업데이트 차단."""
    env = dict(os.environ)
    # API 키 계열이 OAuth 토큰을 이기므로 반드시 제거.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env["DISABLE_AUTOUPDATER"] = "1"
    if oauth_token:
        # 명시 토큰이 있으면 그것만 사용. 없으면 기존 /login 크리덴셜(env 밖)에 맡긴다.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    return env


def available() -> bool:
    """CLI 바이너리 존재 여부(설정 UI의 준비상태 표시용). 인증까지 보장하진 않는다."""
    return _find_binary() is not None


async def cli_parse(prompt: str, system_prompt: str, *,
                    oauth_token: str = "") -> Optional[dict]:
    """헤드리스 claude 호출 → {"model":..., "warnings":[...]} 또는 None.

    prompt: 인벤토리 다이제스트 + 문장이 담긴 사용자 프롬프트.
    system_prompt: 파서 시스템 지시(한국어).
    실패(바이너리 부재·타임아웃·오류·스키마 불충족)는 모두 조용히 None.
    """
    binary = _find_binary()
    if not binary:
        return None

    schema_json = json.dumps(_STRUCT_SCHEMA, ensure_ascii=False)
    cmd = [
        binary,
        "-p", prompt,
        "--output-format", "json",
        "--json-schema", schema_json,
        "--system-prompt", system_prompt,
        # 순수 파싱: 도구 없음 + MCP/설정 소스 격리 + 세션 비영속.
        "--tools", "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--max-turns", "2",
        "--model", "haiku",
    ]
    env = _build_env(oauth_token)
    cwd = str(_work_dir())

    async with _SEMAPHORE:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 자식을 새 세션(=프로세스 그룹 리더)으로 격리 → 타임아웃 시 손자까지 그룹 단위로 정리.
                start_new_session=True,
            )
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("claude CLI 파싱 타임아웃(%ss)", _TIMEOUT)
            await _kill(proc)
            return None
        except (OSError, ValueError):
            # 실행 실패. 예외 메시지에 민감정보가 섞이지 않도록 상세 미출력.
            log.warning("claude CLI 실행 실패")
            await _kill(proc)
            return None

    return _parse_output(proc.returncode, stdout)


async def _kill(proc) -> None:
    """자식 프로세스 그룹 전체를 종료하고 반드시 reap 한다(FD·좀비 누수 방지).

    start_new_session=True 로 띄웠으므로 자식은 자기 세션의 그룹 리더다(pgid == pid).
    프로세스 그룹에 SIGKILL 을 보내 자식이 spawn 한 손자까지 정리하고, `await proc.wait()`
    로 커널 자원(파이프 FD·좀비 엔트리)을 회수한다. 이미 끝났으면 조용히 반환.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # 그룹 조회/전송 실패(이미 종료 등) → 직접 자식만 kill 로 폴백.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await proc.wait()
    except (ProcessLookupError, ChildProcessError):
        pass


def _parse_output(returncode: Optional[int], stdout: bytes) -> Optional[dict]:
    """stdout 한 줄 JSON의 .structured_output에서 model/warnings 추출.

    .result는 코드펜스가 섞여 오므로 절대 쓰지 않는다. 오류도 stdout JSON으로 온다.
    """
    if not stdout:
        return None
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        log.warning("claude CLI 출력 JSON 파싱 실패")
        return None
    if not isinstance(data, dict):
        return None
    # 오류 봉투: returncode != 0 또는 is_error(예: 401 Invalid bearer token) → None.
    if returncode not in (0, None) or data.get("is_error"):
        log.warning("claude CLI 오류 응답(rc=%s)", returncode)
        return None

    struct = data.get("structured_output")
    if not isinstance(struct, dict):
        return None
    model = struct.get("model")
    if not isinstance(model, dict):
        return None
    warnings = struct.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    return {"model": model, "warnings": warnings}
