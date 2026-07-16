"""LLM 해석 백엔드 디스패치 (SPEC-V3 §4) 테스트.

- llm_parse: off→None, api(키없음 None / 모킹 성공), cli(subprocess 모킹).
- cli 호출 규약: 플래그, env 에서 ANTHROPIC_API_KEY 제거 + CLAUDE_CODE_OAUTH_TOKEN 주입,
  structured_output 파싱, is_error→None.
- 키/토큰이 로그에 절대 남지 않는다.
"""
from __future__ import annotations

import json

import pytest

from backend.nl import cli_client, llm_assist

pytestmark = pytest.mark.asyncio


_DIGEST = {
    "entities": [
        {"entity_id": "binary_sensor.living_room_motion", "name": "거실 모션",
         "area_name": "거실", "domain": "binary_sensor", "device_class": "motion"},
        {"entity_id": "light.living_room_main", "name": "거실 메인등",
         "area_name": "거실", "domain": "light"},
    ],
    "persons": {}, "modes": {"슬립 모드": {}},
}

_GOOD_MODEL = {
    "triggers": [{"type": "state", "entity_id": "binary_sensor.living_room_motion",
                  "to": "on"}],
    "actions": [{"type": "service", "action": "light.turn_on",
                 "target": {"entity_id": ["light.living_room_main"]}}],
}


# ---------------------------------------------------------------------------
# 디스패치: off
# ---------------------------------------------------------------------------
async def test_backend_off_returns_none():
    r = await llm_assist.llm_parse("아무 문장", _DIGEST, {}, backend="off",
                                   api_key="key", oauth_token="tok")
    assert r is None


async def test_backend_unknown_returns_none():
    r = await llm_assist.llm_parse("아무 문장", _DIGEST, {}, backend="nonsense")
    assert r is None


# ---------------------------------------------------------------------------
# api 백엔드
# ---------------------------------------------------------------------------
async def test_api_no_key_returns_none():
    r = await llm_assist.llm_parse("문장", _DIGEST, {}, backend="api", api_key="")
    assert r is None


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    _next = None       # (status, body)
    captured = {}

    def __init__(self, *a, **k):
        pass

    def post(self, url, headers=None, data=None):
        _FakeSession.captured = {"url": url, "headers": headers or {},
                                 "data": data}
        status, body = _FakeSession._next
        return _FakeResp(status, body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_api_mocked_success(monkeypatch):
    import aiohttp
    body = {"content": [{"type": "tool_use", "name": "emit_rule",
                         "input": {"model": _GOOD_MODEL}}]}
    _FakeSession._next = (200, body)
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)

    r = await llm_assist.llm_parse("거실에 움직이면 조명 켜줘", _DIGEST, {},
                                   backend="api", api_key="sk-secret")
    assert r is not None
    assert r["model"]["triggers"][0]["entity_id"] == "binary_sensor.living_room_motion"
    # x-api-key 헤더로 키가 전달되지만 응답/로그엔 노출되지 않는다.
    assert _FakeSession.captured["headers"]["x-api-key"] == "sk-secret"


async def test_api_non200_returns_none(monkeypatch):
    import aiohttp
    _FakeSession._next = (401, {"error": "unauthorized"})
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    r = await llm_assist.llm_parse("문장", _DIGEST, {}, backend="api",
                                   api_key="sk-secret")
    assert r is None


async def test_api_invalid_entity_downgraded(monkeypatch):
    import aiohttp
    bad_model = {
        "triggers": [{"type": "state", "entity_id": "light.does_not_exist", "to": "on"}],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": ["light.ghost"]}}],
    }
    body = {"content": [{"type": "tool_use", "name": "emit_rule",
                         "input": {"model": bad_model}}]}
    _FakeSession._next = (200, body)
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    r = await llm_assist.llm_parse("문장", _DIGEST, {}, backend="api",
                                   api_key="sk-secret")
    # 존재하지 않는 entity_id 는 None 으로 강등 + 경고
    assert r["model"]["triggers"][0]["entity_id"] is None
    assert r["model"]["actions"][0]["target"]["entity_id"] == []
    assert any("존재하지 않는" in w or "제외" in w for w in r["warnings"])


# ---------------------------------------------------------------------------
# cli 백엔드 — subprocess 모킹
# ---------------------------------------------------------------------------
class _FakeProc:
    def __init__(self, stdout: bytes, rc: int = 0):
        self._stdout = stdout
        self._rc = rc
        self.returncode = None

    async def communicate(self):
        self.returncode = self._rc
        return self._stdout, b""

    def kill(self):
        pass


def _patch_cli(monkeypatch, tmp_path, stdout: bytes, rc: int = 0):
    """create_subprocess_exec 를 모킹하고 호출 인자를 캡처한다."""
    captured = {}

    async def _fake_exec(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        captured["start_new_session"] = kwargs.get("start_new_session")
        return _FakeProc(stdout, rc)

    monkeypatch.setattr(cli_client, "_find_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cli_client, "_work_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_client.asyncio, "create_subprocess_exec", _fake_exec)
    return captured


async def test_cli_structured_output_parsed(monkeypatch, tmp_path):
    out = json.dumps({"structured_output": {"model": _GOOD_MODEL, "warnings": ["주의"]},
                      "result": "```json ...```", "is_error": False}).encode()
    _patch_cli(monkeypatch, tmp_path, out, rc=0)
    r = await cli_client.cli_parse("프롬프트", "시스템", oauth_token="oauth-secret")
    assert r is not None
    assert r["model"] == _GOOD_MODEL
    assert r["warnings"] == ["주의"]


async def test_cli_flags_and_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-removed")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-should-be-removed")
    out = json.dumps({"structured_output": {"model": _GOOD_MODEL}}).encode()
    captured = _patch_cli(monkeypatch, tmp_path, out, rc=0)
    await cli_client.cli_parse("내 프롬프트", "내 시스템", oauth_token="oauth-secret")

    cmd = captured["cmd"]
    # 검증된 플래그가 그대로 존재
    assert cmd[0] == "/usr/bin/claude"
    assert "-p" in cmd and "내 프롬프트" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--json-schema" in cmd
    assert cmd[cmd.index("--system-prompt") + 1] == "내 시스템"
    assert cmd[cmd.index("--model") + 1] == "haiku"
    assert "--no-session-persistence" in cmd
    assert "--strict-mcp-config" in cmd
    assert "--disable-slash-commands" in cmd
    # --bare 는 OAuth 토큰을 무시하므로 사용 금지
    assert "--bare" not in cmd
    # env: ANTHROPIC_* 제거 + OAuth 토큰 주입 + 오토업데이트 차단
    env = captured["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-secret"
    assert env["DISABLE_AUTOUPDATER"] == "1"


async def test_cli_is_error_returns_none(monkeypatch, tmp_path):
    out = json.dumps({"is_error": True, "api_error_status": 401,
                      "result": "Invalid bearer token"}).encode()
    _patch_cli(monkeypatch, tmp_path, out, rc=0)
    r = await cli_client.cli_parse("프롬프트", "시스템", oauth_token="oauth-secret")
    assert r is None


async def test_cli_nonzero_returncode_returns_none(monkeypatch, tmp_path):
    out = json.dumps({"structured_output": {"model": _GOOD_MODEL}}).encode()
    _patch_cli(monkeypatch, tmp_path, out, rc=1)
    r = await cli_client.cli_parse("프롬프트", "시스템", oauth_token="oauth-secret")
    assert r is None


async def test_cli_no_binary_returns_none(monkeypatch):
    monkeypatch.setattr(cli_client, "_find_binary", lambda: None)
    r = await cli_client.cli_parse("프롬프트", "시스템", oauth_token="oauth-secret")
    assert r is None


async def test_cli_result_field_ignored(monkeypatch, tmp_path):
    """.result 는 코드펜스가 섞여 오므로 무시하고 structured_output 만 쓴다."""
    out = json.dumps({"result": "```json\n{\"model\":{}}\n```",
                      "structured_output": {"model": _GOOD_MODEL}}).encode()
    _patch_cli(monkeypatch, tmp_path, out, rc=0)
    r = await cli_client.cli_parse("프롬프트", "시스템", oauth_token="oauth-secret")
    assert r["model"] == _GOOD_MODEL


async def test_llm_parse_cli_dispatch(monkeypatch, tmp_path):
    out = json.dumps({"structured_output": {"model": _GOOD_MODEL}}).encode()
    _patch_cli(monkeypatch, tmp_path, out, rc=0)
    r = await llm_assist.llm_parse("거실 움직이면 켜줘", _DIGEST, {}, backend="cli",
                                   oauth_token="oauth-secret")
    assert r is not None
    assert r["model"]["triggers"][0]["entity_id"] == "binary_sensor.living_room_motion"


# ---------------------------------------------------------------------------
# 보안: 키/토큰이 로그에 노출되지 않는다
# ---------------------------------------------------------------------------
async def test_cli_token_not_in_logs(monkeypatch, tmp_path, caplog):
    token = "oauth-VERYSECRET-TOKEN"
    out = json.dumps({"is_error": True, "result": "Invalid bearer token"}).encode()
    _patch_cli(monkeypatch, tmp_path, out, rc=0)
    with caplog.at_level("DEBUG"):
        await cli_client.cli_parse("프롬프트", "시스템", oauth_token=token)
    assert token not in caplog.text


async def test_api_key_not_in_logs(monkeypatch, caplog):
    import aiohttp
    key = "sk-VERYSECRET-KEY"
    _FakeSession._next = (401, {"error": "unauthorized"})
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    with caplog.at_level("DEBUG"):
        await llm_assist.llm_parse("문장", _DIGEST, {}, backend="api", api_key=key)
    assert key not in caplog.text


# ---------------------------------------------------------------------------
# 결함 1: 스칼라 target.entity_id 실존 검증 우회 (SPEC-V3 §4.2)
# ---------------------------------------------------------------------------
def _api_body(model):
    return {"content": [{"type": "tool_use", "name": "emit_rule",
                         "input": {"model": model}}]}


async def test_api_scalar_target_hallucinated_downgraded(monkeypatch):
    """비-리스트(스칼라) target.entity_id 도 실존 검증 후 제외된다.

    기존 결함: _validate_entities 가 target.entity_id 를 리스트일 때만 필터해, CLI/모델이
    내는 스칼라 문자열 target(예: 'light.HALLUCINATED')이 검증을 통과했다.
    """
    import aiohttp
    bad = {
        "triggers": [{"type": "state",
                      "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": "light.HALLUCINATED"}}],  # 스칼라!
    }
    _FakeSession._next = (200, _api_body(bad))
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    r = await llm_assist.llm_parse("문장", _DIGEST, {}, backend="api", api_key="sk")
    # 스칼라 → 리스트 정규화 후 실존 필터에서 제거
    assert r["model"]["actions"][0]["target"]["entity_id"] == []
    assert any("제외" in w for w in r["warnings"])


async def test_api_scalar_target_valid_normalized_to_list(monkeypatch):
    """실존하는 스칼라 target 은 리스트로 정규화되어 보존된다(회귀 방지)."""
    import aiohttp
    good = {
        "triggers": [{"type": "state",
                      "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": "light.living_room_main"}}],  # 스칼라 valid
    }
    _FakeSession._next = (200, _api_body(good))
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    r = await llm_assist.llm_parse("문장", _DIGEST, {}, backend="api", api_key="sk")
    assert r["model"]["actions"][0]["target"]["entity_id"] == ["light.living_room_main"]
    assert not any("제외" in w for w in r["warnings"])


async def test_api_scene_target_not_blanket_allowed(monkeypatch):
    """인벤토리에 없는 scene.* 는 더 이상 무조건 허용되지 않는다(실존하는 것만)."""
    import aiohttp
    bad = {
        "triggers": [{"type": "state",
                      "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
        "actions": [{"type": "service", "action": "scene.turn_on",
                     "target": {"entity_id": ["scene.does_not_exist"]}}],
    }
    _FakeSession._next = (200, _api_body(bad))
    monkeypatch.setattr(aiohttp, "ClientSession", _FakeSession)
    r = await llm_assist.llm_parse("문장", _DIGEST, {}, backend="api", api_key="sk")
    assert r["model"]["actions"][0]["target"]["entity_id"] == []
    assert any("제외" in w for w in r["warnings"])


# ---------------------------------------------------------------------------
# 결함 1 근본 수정: validate_rule_model 의 service target 실존 검사
# ---------------------------------------------------------------------------
_INV = {"entities": [
    {"entity_id": "binary_sensor.living_room_motion"},
    {"entity_id": "light.living_room_main"},
]}


def _svc_rule(target):
    return {
        "triggers": [{"type": "state",
                      "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
        "condition_mode": "and", "conditions": [],
        "actions": [{"type": "service", "action": "light.turn_on",
                     "target": {"entity_id": target}}],
    }


async def test_validate_rule_model_rejects_nonexistent_service_target():
    """근본 수정: service 액션 target.entity_id(리스트/스칼라 모두)가 인벤토리에 없으면 오류.

    로컬/LLM/수동 모든 저장 경로가 존재하지 않는 대상을 저장하지 못하게 막는다.
    (파일의 module-level asyncio 마크에 맞춰 async 로 두되 본문은 순수 동기.)
    """
    from backend.engine.rule_model import validate_rule_model
    # 리스트 target 에 hallucinated
    errs = validate_rule_model(_svc_rule(["light.ghost"]), _INV)
    assert any("존재하지 않는 엔티티" in e["message"] for e in errs)
    # 스칼라 target 에 hallucinated
    errs2 = validate_rule_model(_svc_rule("light.ghost"), _INV)
    assert any("존재하지 않는 엔티티" in e["message"] for e in errs2)
    # 실존하는 대상(리스트/스칼라)은 통과
    assert validate_rule_model(_svc_rule(["light.living_room_main"]), _INV) == []
    assert validate_rule_model(_svc_rule("light.living_room_main"), _INV) == []


async def test_validate_rule_model_target_skipped_without_inventory():
    """인벤토리 미제공(valid_ids 비어있음)이면 target 실존 검사는 건너뛴다(_need_entity 규약)."""
    from backend.engine.rule_model import validate_rule_model
    errs = validate_rule_model(_svc_rule(["light.anything"]), {"entities": []})
    assert not any("존재하지 않는 엔티티" in e["message"] for e in errs)


# ---------------------------------------------------------------------------
# 결함 3: CLI 타임아웃 시 프로세스 그룹 종료 + reap → FD/프로세스 미누수
# ---------------------------------------------------------------------------
async def test_cli_spawns_with_new_session(monkeypatch, tmp_path):
    """자식은 start_new_session=True 로 격리해서 spawn 한다(그룹 단위 종료 전제)."""
    out = json.dumps({"structured_output": {"model": _GOOD_MODEL}}).encode()
    captured = _patch_cli(monkeypatch, tmp_path, out, rc=0)
    await cli_client.cli_parse("프롬프트", "시스템", oauth_token="tok")
    assert captured["start_new_session"] is True


def _fd_count() -> int:
    import os
    return len(os.listdir(f"/proc/{os.getpid()}/fd"))


def _alive(pid: int) -> bool:
    import os
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


async def test_cli_timeout_kills_process_group_no_leak(monkeypatch, tmp_path):
    """타임아웃 시 자식이 spawn 한 손자까지 그룹 단위로 종료·reap 하여 누수가 없다.

    기존 결함: _kill 이 proc.kill()(직계 자식만) + await wait 없음 → 자식이 띄운 손자가
    고아로 생존하며 stdout 파이프를 붙잡아 FD·프로세스가 단조 증가. 손자를 만드는 셸을
    띄워 타임아웃을 유발하고, 반복 후 손자 전멸 + FD 안정을 확인한다.
    (판별력 검증: 이 시나리오에서 옛 동작은 손자 6/6 생존·FD +12, 새 동작은 0/6·FD +0.)
    """
    import asyncio as _asyncio
    import os as _os
    import signal as _signal

    real_exec = _asyncio.create_subprocess_exec
    gpid_files = []

    async def _hang_exec(*cmd, **kwargs):
        pf = tmp_path / f"gpid_{len(gpid_files)}"
        gpid_files.append(pf)
        # sh 가 sleep 을 백그라운드 손자로 띄우고 pid 를 기록 → 직계 자식(sh)만 kill 하면 손자 생존.
        return await real_exec(
            "sh", "-c", f"sleep 30 & echo $! > {pf}; wait",
            stdin=kwargs.get("stdin"), stdout=kwargs.get("stdout"),
            stderr=kwargs.get("stderr"),
            start_new_session=kwargs.get("start_new_session", False),
        )

    monkeypatch.setattr(cli_client, "_find_binary", lambda: "/usr/bin/claude")
    monkeypatch.setattr(cli_client, "_work_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_client.asyncio, "create_subprocess_exec", _hang_exec)
    monkeypatch.setattr(cli_client, "_TIMEOUT", 0.4)

    assert await cli_client.cli_parse("p", "s", oauth_token="t") is None  # warmup
    base = _fd_count()
    for _ in range(5):
        assert await cli_client.cli_parse("p", "s", oauth_token="t") is None
    await _asyncio.sleep(0.2)
    after = _fd_count()

    gpids = []
    for pf in gpid_files:
        try:
            gpids.append(int(pf.read_text().strip()))
        except (OSError, ValueError):
            pass
    survivors = [g for g in gpids if _alive(g)]
    for g in survivors:                       # 테스트 격리: 혹시 생존자가 있으면 정리
        try:
            _os.kill(g, _signal.SIGKILL)
        except OSError:
            pass
    assert gpids, "손자 pid 를 수집하지 못했습니다(테스트 셋업 오류)."
    assert not survivors, f"손자 프로세스 누수: {survivors}"
    assert after - base <= 2, f"FD 누수 의심: {base} -> {after}"
