"""Phase 3B+3C — api_v2 하이브리드/학습 배선 테스트.

- L2 편입: parse 가 not ok 일 때 template_matcher / learned_store 를 채택하고 source 를 표기.
- 학습 엔드포인트: POST learn(off→409, on+모킹→preview), learn/confirm→저장·로컬 재사용,
  learned 목록/삭제, settings 의 learn.enabled 왕복(계산 필드 비저장).

배선은 conftest 의 make_v2_app 팩토리를 재사용한다. matcher/analyze 는 결정적으로 모킹한다.
"""
from __future__ import annotations

import copy

import pytest

from backend.nl import learn as learn_mod
from backend.nl.learn import LearnedStore, model_entities

pytestmark = pytest.mark.asyncio

# mock 인벤토리에 실존하는 엔티티로 구성한 저장 가능한 model.
_FAN_MODEL = {"subrules": [{
    "triggers": [{"type": "state", "entity_id": "binary_sensor.bathroom_motion", "to": "on"}],
    "conditions": [],
    "actions": [{"type": "service", "action": "fan.turn_on",
                 "target": {"entity_id": ["fan.bathroom_fan"]}}],
}]}

# L1 규칙 파서가 해석하지 못하는(ok=False) 짧은 문장.
_UNPARSED = "욕실 환풍기 켜"


def _settings_with(**over) -> dict:
    from tests.conftest import DEFAULT_V2_SETTINGS
    s = copy.deepcopy(DEFAULT_V2_SETTINGS)
    s.update(over)
    return s


class _StubMatcher:
    """항상 정해진 model 을 돌려주는 템플릿 매처 대역."""

    def __init__(self, result):
        self._result = result
        self.calls = []

    def match(self, sentence):
        self.calls.append(sentence)
        return self._result


# ===========================================================================
# L2 편입 — parse 채택 + source 표기
# ===========================================================================
async def test_parse_adopts_template_matcher(aiohttp_client, make_v2_app):
    app = make_v2_app()
    app["template_matcher"] = _StubMatcher({
        "model": copy.deepcopy(_FAN_MODEL), "matched_id": "target_fan_on",
        "score": 0.9, "mode": "slot_fill"})
    client = await aiohttp_client(app)

    resp = await client.post("/api/v2/parse", json={"sentence": _UNPARSED})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["used_matcher"] == "pattern"
    assert body["source"] == "pattern"
    assert body["used_llm"] is False
    assert body["matched_id"] == "target_fan_on"
    assert body["chips"] == []
    assert body["model"]["subrules"][0]["actions"][0]["action"] == "fan.turn_on"


async def test_parse_adopts_learned_before_matcher(aiohttp_client, make_v2_app,
                                                   v2_data_dir):
    app = make_v2_app()
    store = LearnedStore(v2_data_dir / "learned.yaml")
    store.add(_UNPARSED, "욕실 모션이 감지되면 욕실 환풍기를 켜",
              _FAN_MODEL, model_entities(_FAN_MODEL))
    app["learned_store"] = store
    # 매처도 있지만 learned 가 우선한다.
    app["template_matcher"] = _StubMatcher({
        "model": copy.deepcopy(_FAN_MODEL), "matched_id": "x", "score": 0.9,
        "mode": "slot_fill"})
    client = await aiohttp_client(app)

    resp = await client.post("/api/v2/parse", json={"sentence": _UNPARSED})
    body = await resp.json()
    assert body["ok"] is True
    assert body["used_matcher"] == "learned"
    assert body["source"] == "learned"
    assert app["template_matcher"].calls == []  # learned 채택 시 매처는 호출 안 됨


async def test_parse_ok_sentence_keeps_rule_source(aiohttp_client, make_v2_app):
    """규칙(L1)이 ok 면 매처를 부르지 않고 source='rule'(회귀 방지)."""
    app = make_v2_app()
    app["template_matcher"] = _StubMatcher({
        "model": copy.deepcopy(_FAN_MODEL), "matched_id": "x", "score": 1.0,
        "mode": "slot_fill"})
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/v2/parse",
        json={"sentence": "거실에 움직임이 감지되면 거실 조명을 켜줘"})
    body = await resp.json()
    assert body["ok"] is True
    assert body["source"] == "rule"
    assert body["used_matcher"] is None
    assert app["template_matcher"].calls == []


# ===========================================================================
# POST /api/v2/learn
# ===========================================================================
async def test_learn_disabled_returns_409(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())  # 기본 설정: learn 없음 → off
    resp = await client.post("/api/v2/learn", json={"sentence": _UNPARSED})
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "learn_disabled"


async def test_learn_enabled_but_backend_unavailable_409(aiohttp_client, make_v2_app):
    app = make_v2_app(_settings_with(learn={"enabled": True},
                                     llm={"backend": "off"}))
    client = await aiohttp_client(app)
    resp = await client.post("/api/v2/learn", json={"sentence": _UNPARSED})
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "learn_unavailable"


async def test_learn_preview_when_enabled_and_mocked(aiohttp_client, make_v2_app,
                                                     monkeypatch):
    # 백엔드 준비: cli + OAuth 토큰(env) → _llm_available True.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-token")
    app = make_v2_app(_settings_with(learn={"enabled": True},
                                     llm={"backend": "cli"}))

    async def _stub_analyze(sentence, gz, settings, inventory, **kw):
        return {"normalized": "욕실 모션이 감지되면 욕실 환풍기를 켜",
                "model": copy.deepcopy(_FAN_MODEL), "template_id": "target_fan_on",
                "entities": model_entities(_FAN_MODEL), "ok": True, "warnings": []}
    monkeypatch.setattr(learn_mod, "analyze", _stub_analyze)

    client = await aiohttp_client(app)
    resp = await client.post("/api/v2/learn", json={"sentence": _UNPARSED})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["normalized"] == "욕실 모션이 감지되면 욕실 환풍기를 켜"
    assert body["model"]["subrules"][0]["actions"][0]["action"] == "fan.turn_on"
    assert "preview" in body


async def test_learn_analyze_failure_returns_502(aiohttp_client, make_v2_app,
                                                 monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-token")
    app = make_v2_app(_settings_with(learn={"enabled": True},
                                     llm={"backend": "cli"}))

    async def _stub_none(*a, **k):
        return None
    monkeypatch.setattr(learn_mod, "analyze", _stub_none)
    client = await aiohttp_client(app)
    resp = await client.post("/api/v2/learn", json={"sentence": _UNPARSED})
    assert resp.status == 502
    assert (await resp.json())["error"]["code"] == "learn_failed"


# ===========================================================================
# POST /api/v2/learn/confirm + 목록/삭제 + 로컬 재사용
# ===========================================================================
async def test_learn_confirm_saves_and_reuses_locally(aiohttp_client, make_v2_app,
                                                      v2_data_dir):
    app = make_v2_app(_settings_with(learn={"enabled": True}))
    app["learned_store"] = LearnedStore(v2_data_dir / "learned.yaml")
    client = await aiohttp_client(app)

    # confirm → 저장
    resp = await client.post("/api/v2/learn/confirm", json={
        "sentence": _UNPARSED, "normalized": "욕실 모션이 감지되면 욕실 환풍기를 켜",
        "model": copy.deepcopy(_FAN_MODEL)})
    assert resp.status == 200
    learned_id = (await resp.json())["learned_id"]

    # 목록
    resp = await client.get("/api/v2/learned")
    learned = (await resp.json())["learned"]
    assert len(learned) == 1
    assert learned[0]["id"] == learned_id
    assert learned[0]["raw"] == _UNPARSED

    # 저장 후 같은 문장을 parse → CLI 없이 learned 로 로컬 처리(자기학습 복리).
    resp = await client.post("/api/v2/parse", json={"sentence": _UNPARSED})
    body = await resp.json()
    assert body["ok"] is True
    assert body["source"] == "learned"

    # 삭제
    resp = await client.delete(f"/api/v2/learned/{learned_id}")
    assert (await resp.json())["ok"] is True
    resp = await client.get("/api/v2/learned")
    assert (await resp.json())["learned"] == []


async def test_learn_confirm_wires_runtime_templates(aiohttp_client, make_v2_app,
                                                     v2_data_dir):
    """§4.5 배선: confirm 시 매처 런타임 템플릿 인덱스에 즉시 편입, delete 시 제거."""
    from backend.nl.pattern_match import TemplateMatcher
    app = make_v2_app(_settings_with(learn={"enabled": True}))
    app["learned_store"] = LearnedStore(v2_data_dir / "learned.yaml")
    gz = app["gazetteer_fn"]()
    app["template_matcher"] = TemplateMatcher([], gz, gz.inventory)
    client = await aiohttp_client(app)

    resp = await client.post("/api/v2/learn/confirm", json={
        "sentence": _UNPARSED, "normalized": "욕실 모션이 감지되면 욕실 환풍기를 켜",
        "model": copy.deepcopy(_FAN_MODEL)})
    learned_id = (await resp.json())["learned_id"]
    assert len(app["template_matcher"]._runtime) == 1     # confirm → 즉시 편입

    await client.delete(f"/api/v2/learned/{learned_id}")
    assert app["template_matcher"]._runtime == []          # delete → 제거


async def test_learn_confirm_disabled_returns_409(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())  # learn off
    resp = await client.post("/api/v2/learn/confirm", json={
        "sentence": _UNPARSED, "model": copy.deepcopy(_FAN_MODEL)})
    assert resp.status == 409
    assert (await resp.json())["error"]["code"] == "learn_disabled"


async def test_learn_confirm_invalid_model_rejected(aiohttp_client, make_v2_app,
                                                    v2_data_dir):
    app = make_v2_app(_settings_with(learn={"enabled": True}))
    app["learned_store"] = LearnedStore(v2_data_dir / "learned.yaml")
    client = await aiohttp_client(app)
    bad = {"subrules": [{"triggers": [{"type": "state",
            "entity_id": "fan.nonexistent", "to": "on"}], "conditions": [],
            "actions": [{"type": "service", "action": "fan.turn_on",
            "target": {"entity_id": ["fan.nonexistent"]}}]}]}
    resp = await client.post("/api/v2/learn/confirm", json={
        "sentence": _UNPARSED, "model": bad})
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == "invalid_rule"


# ===========================================================================
# settings — learn.enabled 왕복 + 계산 필드 비저장
# ===========================================================================
async def test_settings_learn_enabled_roundtrip(aiohttp_client, make_v2_app):
    client = await aiohttp_client(make_v2_app())
    # 기본 off
    resp = await client.get("/api/v2/settings")
    assert (await resp.json())["learn_enabled"] is False

    # learn.enabled 켜기
    resp = await client.put("/api/v2/settings", json={"learn": {"enabled": True}})
    body = await resp.json()
    assert body["learn_enabled"] is True
    assert body["learn"] == {"enabled": True}

    # 다시 조회해도 유지
    resp = await client.get("/api/v2/settings")
    body = await resp.json()
    assert body["learn_enabled"] is True

    # 계산 필드(learn_enabled 등)를 PUT 해도 원본 dict 에는 저장되지 않는다.
    resp = await client.put("/api/v2/settings",
                            json={"learn_enabled": False, "learn": {"enabled": True}})
    body = await resp.json()
    assert "learn_enabled" not in body["learn"]
    assert body["learn_enabled"] is True  # 계산값은 learn.enabled 로부터 재산출
