"""Phase 3C — CLI 학습(backend.nl.learn) 테스트.

두 축(APP-REINFORCEMENT-PLAN §3):
  1. LearnedStore — add/match(원문·정규형·근접)/persist/재검증(revalidate)/delete.
  2. learn.analyze — CLI 정규화(subprocess 모킹)→재파싱→model + 엔티티 실존검증 + 환각방어.
     실호출 스모크는 RUN_CLI_SMOKE=1 이고 claude 바이너리가 있을 때만(기본은 비용/비결정성 회피).

subprocess 모킹: cli_client._find_binary 를 가짜 경로로, asyncio.create_subprocess_exec 를
가짜 프로세스(정해진 structured_output JSON 반환)로 바꿔 _run_cli_normalize 경로 전체를 탄다.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

from backend.app import extract_zones
from backend.ha_client import merge_inventory
from backend.mock_data import MockHAClient
from backend.nl import cli_client, learn
from backend.nl.gazetteer import Gazetteer
from backend.nl.learn import LearnedStore, model_entities

_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user"},
    "modes": {},
    "near_home": {"zone_state": "home"},
    "aliases": [],
}

_FAN_MODEL = {"subrules": [{
    "triggers": [{"type": "state", "entity_id": "binary_sensor.bathroom_motion", "to": "on"}],
    "conditions": [],
    "actions": [{"type": "service", "action": "fan.turn_on",
                 "target": {"entity_id": ["fan.bathroom_fan"]}}],
}]}


# ---------------------------------------------------------------------------
# subprocess 모킹 헬퍼
# ---------------------------------------------------------------------------
class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode
        self.pid = 0

    async def communicate(self):
        return self._stdout, b""

    async def wait(self):
        return


def _fake_cli(monkeypatch, struct: dict, returncode: int = 0):
    """claude CLI 호출을 가짜로 대체 — struct 를 structured_output 으로 돌려준다."""
    monkeypatch.setattr(cli_client, "_find_binary", lambda: "/fake/claude")
    payload = json.dumps({"structured_output": struct, "is_error": False}).encode()

    async def _fake_exec(*args, **kwargs):
        return _FakeProc(payload, returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


@pytest.fixture(scope="module")
def inventory():
    async def _build():
        ha = MockHAClient()
        await ha.start()
        reg = await ha.fetch_registries()
        states = await ha.get_states()
        inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
        await ha.close()
        return {"areas": inv["areas"], "entities": inv["entities"],
                "zones": extract_zones(states)}
    return asyncio.new_event_loop().run_until_complete(_build())


@pytest.fixture(scope="module")
def gz(inventory):
    return Gazetteer.build(inventory, _SETTINGS)


# ===========================================================================
# LearnedStore
# ===========================================================================
def test_learnedstore_add_and_match_exact(tmp_path):
    store = LearnedStore(tmp_path / "learned.yaml")
    ents = model_entities(_FAN_MODEL)
    entry = store.add("욕실 환풍기 켜", "욕실 모션이 감지되면 욕실 환풍기를 켜",
                      _FAN_MODEL, ents)
    assert entry["hits"] == 0
    assert entry["entities"] == ents

    # 원문 완전일치
    m_raw = store.match("욕실 환풍기 켜")
    assert m_raw is not None
    assert m_raw["subrules"][0]["actions"][0]["action"] == "fan.turn_on"
    assert m_raw["alias"] == "욕실 환풍기 켜"
    # 정규형 완전일치
    assert store.match("욕실 모션이 감지되면 욕실 환풍기를 켜") is not None


def test_learnedstore_match_near_and_miss(tmp_path):
    store = LearnedStore(tmp_path / "learned.yaml")
    store.add("욕실 환풍기 켜", "욕실 모션이 감지되면 욕실 환풍기를 켜",
              _FAN_MODEL, model_entities(_FAN_MODEL))
    # 근접(문자 3-gram Jaccard ≥ 0.85) → 매칭
    assert store.match("욕실 모션이 감지되면 욕실 환풍기를 켜줘") is not None
    # 무관한 문장 → None(오탐 방지)
    assert store.match("거실 커튼 열어줘 완전 다른 문장") is None


def test_learnedstore_hits_increment_and_persist(tmp_path):
    path = tmp_path / "learned.yaml"
    store = LearnedStore(path)
    store.add("욕실 환풍기 켜", "욕실 모션이 감지되면 욕실 환풍기를 켜",
              _FAN_MODEL, model_entities(_FAN_MODEL))
    store.match("욕실 환풍기 켜")
    store.match("욕실 환풍기 켜")
    assert path.exists()
    # 재기동(파일 재로드) — 항목·hits 가 보존된다.
    reloaded = LearnedStore(path)
    assert len(reloaded.all()) == 1
    assert reloaded.all()[0]["hits"] == 2


def test_learnedstore_add_replaces_same_raw(tmp_path):
    store = LearnedStore(tmp_path / "learned.yaml")
    store.add("욕실 환풍기 켜", "n1", _FAN_MODEL, model_entities(_FAN_MODEL))
    store.add("욕실 환풍기 켜", "n2", _FAN_MODEL, model_entities(_FAN_MODEL))
    # 같은 원문은 교체(무한 성장 방어) — 항목 1개.
    assert len(store.all()) == 1
    assert store.all()[0]["normalized"] == "n2"


def test_learnedstore_delete(tmp_path):
    store = LearnedStore(tmp_path / "learned.yaml")
    entry = store.add("욕실 환풍기 켜", "n", _FAN_MODEL, model_entities(_FAN_MODEL))
    assert store.delete(entry["id"]) is True
    assert store.all() == []
    assert store.delete(entry["id"]) is False  # 없는 id


def test_learnedstore_revalidate_removes_stale(tmp_path, inventory):
    store = LearnedStore(tmp_path / "learned.yaml")
    entry = store.add("욕실 환풍기 켜", "n", _FAN_MODEL, model_entities(_FAN_MODEL))
    # 인벤토리에 실존 → 유지.
    assert store.revalidate(inventory) == []
    assert len(store.all()) == 1
    # 엔티티가 사라진 인벤토리 → 해당 항목 제거.
    removed = store.revalidate({"entities": [{"entity_id": "light.other"}]})
    assert removed == [entry["id"]]
    assert store.all() == []


def test_revalidate_empty_inventory_keeps_data(tmp_path):
    """Finding: HA core 미준비로 인벤토리 로드가 실패해 빈 인벤토리가 넘어와도, 학습 데이터를
    프루닝하지 않는다(일시적 장애 한 번으로 전량 삭제되는 사고 방지)."""
    store = LearnedStore(tmp_path / "learned.yaml")
    store.add("욕실 환풍기 켜", "n", _FAN_MODEL, model_entities(_FAN_MODEL))
    before = len(store.all())
    assert before == 1
    # 빈 인벤토리(로드 실패) → 아무것도 제거하지 않는다.
    assert store.revalidate({"entities": []}) == []
    assert store.revalidate({}) == []
    assert store.revalidate(None) == []
    assert len(store.all()) == before


def test_model_entities_collects_all(tmp_path):
    ents = model_entities(_FAN_MODEL)
    assert ents == ["binary_sensor.bathroom_motion", "fan.bathroom_fan"]


# ===========================================================================
# learn.analyze — 정규화 → 재파싱 → 검증 (subprocess 모킹)
# ===========================================================================
@pytest.mark.asyncio
async def test_analyze_normalizes_and_reparses(monkeypatch, gz, inventory):
    """미해석 원문을 표준 문형으로 재작성 → 재파싱 → ok=True, 실존 엔티티 model."""
    _fake_cli(monkeypatch, {
        "normalized": "욕실 모션이 감지되면 욕실 환풍기를 켜",
        "matched_template_id": "target_fan_on", "changed": True,
        "confidence": 0.9, "notes": ""})
    res = await learn.analyze("욕실 움직임 환풍기", gz, _SETTINGS, inventory,
                              oauth_token="")
    assert res is not None
    assert res["ok"] is True
    assert res["normalized"] == "욕실 모션이 감지되면 욕실 환풍기를 켜"
    assert res["warnings"] == []
    # 재파싱된 model 이 참조하는 엔티티는 전부 인벤토리에 실존한다(환각 없음).
    valid = {e["entity_id"] for e in inventory["entities"]}
    assert res["entities"] and set(res["entities"]) <= valid


@pytest.mark.asyncio
async def test_analyze_hallucination_defense(monkeypatch, gz, inventory):
    """정규형이 원문에 없는 방/기기를 새로 만들면 ok=False + 경고(환각 방어)."""
    _fake_cli(monkeypatch, {
        "normalized": "거실 에어컨을 켜",  # 원문 '환풍기 켜' 에 없는 거실/에어컨
        "matched_template_id": None, "changed": True,
        "confidence": 0.9, "notes": ""})
    res = await learn.analyze("환풍기 켜", gz, _SETTINGS, inventory, oauth_token="")
    assert res is not None
    assert res["ok"] is False
    assert any("새로 만든" in w for w in res["warnings"])


@pytest.mark.asyncio
async def test_analyze_cli_failure_returns_none(monkeypatch, gz, inventory):
    """CLI 오류(returncode!=0)면 analyze 는 None(로컬 처리로 폴백)."""
    _fake_cli(monkeypatch, {"normalized": "x", "changed": False}, returncode=1)
    assert await learn.analyze("욕실 움직임 환풍기", gz, _SETTINGS, inventory,
                               oauth_token="") is None


@pytest.mark.asyncio
async def test_analyze_no_binary_returns_none(monkeypatch, gz, inventory):
    """claude 바이너리가 없으면 None."""
    monkeypatch.setattr(cli_client, "_find_binary", lambda: None)
    assert await learn.analyze("욕실 움직임 환풍기", gz, _SETTINGS, inventory,
                               oauth_token="") is None


@pytest.mark.asyncio
async def test_analyze_allow_live_false_returns_none(gz, inventory):
    """allow_live=False 면 CLI 를 부르지 않고 None."""
    assert await learn.analyze("욕실 움직임 환풍기", gz, _SETTINGS, inventory,
                               allow_live=False) is None


@pytest.mark.asyncio
async def test_analyze_empty_sentence_returns_none(gz, inventory):
    assert await learn.analyze("   ", gz, _SETTINGS, inventory) is None


# ===========================================================================
# 실호출 스모크 (기본 skip — 비용/비결정성). RUN_CLI_SMOKE=1 + claude 있으면 1건 실행.
# ===========================================================================
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("RUN_CLI_SMOKE") != "1" or not cli_client.available(),
    reason="실 CLI 스모크는 RUN_CLI_SMOKE=1 이고 claude 바이너리가 있을 때만")
async def test_analyze_live_smoke(gz, inventory):
    res = await learn.analyze("욕실 움직임 환풍기", gz, _SETTINGS, inventory)
    # 성공하면 실존 엔티티만, 실패하면 None — 둘 다 허용(비결정성). 예외만 없으면 통과.
    if res is not None:
        valid = {e["entity_id"] for e in inventory["entities"]}
        assert set(res["entities"]) <= valid
