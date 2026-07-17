"""APP-PORT-PLAN S7 — else/if 분기 조립 + 신규 노드 확인 카드(요약·칩) 테스트.

  - postpass #32: "A면 X, 아니면 Y" / 평일-주말 대비쌍 → 한 트리거 + if/then/else.
    ★ 명시 대비(아니면·평일·주말)일 때만. 대칭 전이형은 서브룰 그대로(과잉조립 방지).
  - parser/summary: 신규 노드(sun/sun_window/weekday/day_of_month/interval_anchor/
    time_pattern/presence_agg/if)가 확인 카드 summary 에 한국어로 서술됨(미서술/미지원 0).
  - postpass 칩: 신규 노드마다 확인 카드 칩(label/sublabel/score) 방출.

now_fn 은 interval_anchor.anchor 결정성용으로 고정 주입한다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from backend.nl import summary as S
from backend.nl.gazetteer import Gazetteer
from backend.nl.parser import parse

_NOW = datetime(2026, 7, 17, 12, 0, 0)

_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {"나": "person.user", "와이프": "person.wife"},
    "near_home": {"zone_state": "home"},
    "modes": {"슬립 모드": {"initial": "off",
                          "on_action": {"action": "scene.turn_on",
                                        "target": {"entity_id": ["scene.sleep_mode"]}},
                          "off_action": None}},
    "aliases": [],
}


def _build_inventory():
    from backend.app import extract_zones
    from backend.ha_client import merge_inventory
    from backend.mock_data import MockHAClient

    async def _go():
        ha = MockHAClient()
        await ha.start()
        reg = await ha.fetch_registries()
        states = await ha.get_states()
        inv = merge_inventory(reg["areas"], reg["devices"], reg["entities"], states)
        return {"areas": inv["areas"], "entities": inv["entities"],
                "zones": extract_zones(states)}

    return asyncio.run(_go())


@pytest.fixture(scope="module")
def gz():
    return Gazetteer.build(_build_inventory(), _SETTINGS)


def _p(gz, s):
    return parse(s, gz, _SETTINGS, now_fn=lambda: _NOW)


def _one_sub(model):
    """단일 서브룰 뷰(subrules 리스트든 평탄 model 이든)."""
    subs = model.get("subrules")
    if isinstance(subs, list):
        assert len(subs) == 1, f"기대 단일 서브룰, 실제 {len(subs)}: {subs}"
        return subs[0]
    return model


def _if_node(sub):
    ifs = [a for a in sub.get("actions", []) if a.get("type") == "if"]
    assert len(ifs) == 1, f"기대 if 액션 1개, 실제 {ifs}"
    return ifs[0]


# ===========================================================================
# 1) else 분기 조립 — 구조 단정
# ===========================================================================
def test_else_numeric_contrast(gz):
    """'A도 넘으면 켜고, 아니면 꺼' → daily 트리거 + if[numeric]/then[on]/else[off]."""
    m = _p(gz, "오후 3시쯤 거실이 28도보다 높으면 에어컨 켜주고, 아니면 꺼줘.")["model"]
    sub = _one_sub(m)
    assert sub["triggers"] == [{"type": "daily", "at": "15:00"}]
    node = _if_node(sub)
    assert node["if"] and node["if"][0]["type"] == "numeric_state"
    assert node["if"][0]["above"] == 28
    assert node["then"][0]["action"] == "climate.turn_on"
    assert node["else"][0]["action"] == "climate.turn_off"
    # then/else 대상 동일(같은 기기 켜기/끄기)
    assert node["then"][0]["target"] == node["else"][0]["target"]


def test_else_weekday_contrast(gz):
    """'평일엔 켜고 주말엔 꺼' → if[day_type weekday]/then[on]/else[off], 극성 복원."""
    m = _p(gz, "아침 7시 되면 평일엔 안방 보일러 좀 켜주고, 주말엔 꺼 줬으면 해.")["model"]
    sub = _one_sub(m)
    assert sub["triggers"] == [{"type": "daily", "at": "07:00"}]
    node = _if_node(sub)
    assert node["if"] == [{"type": "day_type", "types": ["weekday"]}]
    assert node["then"][0]["action"] == "climate.turn_on"
    assert node["else"][0]["action"] == "climate.turn_off"


def test_else_segment_condition(gz):
    """'밤 시간대엔 켜고 아니면 꺼' → numeric 트리거 + if[time_segment night]."""
    m = _p(gz, "안방 습도 70 넘으면 밤 시간대엔 전열교환기 켜 주시고, 아니면 꺼 주세요.")["model"]
    sub = _one_sub(m)
    assert sub["triggers"][0]["type"] == "numeric_state"
    node = _if_node(sub)
    assert node["if"] == [{"type": "time_segment", "segments": ["night"]}]
    assert node["then"][0]["action"] == "fan.turn_on"
    assert node["else"][0]["action"] == "fan.turn_off"


def test_else_sun_trigger_weekday(gz):
    """'해 뜨면 평일엔 열고 주말엔 닫아' → sun 트리거 + cover open/close."""
    m = _p(gz, "해 뜨면 평일엔 커튼 좀 열어주고, 주말엔 그냥 닫아뒀으면 좋겠어.")["model"]
    sub = _one_sub(m)
    assert sub["triggers"] == [{"type": "sun", "event": "sunrise"}]
    node = _if_node(sub)
    assert node["then"][0]["action"] == "cover.open_cover"
    assert node["else"][0]["action"] == "cover.close_cover"


# ===========================================================================
# 2) 과잉조립 방지 — 명시 대비 없으면 collapse 금지
# ===========================================================================
def test_symmetric_transition_not_collapsed(gz):
    """'닫히면 켜고 열리면 꺼'(아니면·평일/주말 없음) → if 로 합치지 않는다(회귀 방지)."""
    m = _p(gz, "커튼 닫히면 거실 무드등 켜고, 열리면 그냥 꺼줘.")["model"]
    all_actions = [a for s in (m.get("subrules") or [m]) for a in s.get("actions", [])]
    assert not any(a.get("type") == "if" for a in all_actions), \
        "명시 대비가 없는 대칭 전이형을 if/else 로 과잉조립했다"


# ===========================================================================
# 3) 신규 노드 요약 완비(gate #4) — 한국어 서술·미지원/미서술 0
# ===========================================================================
_BAD_MARKERS = ("미지원", "지원하지 않", "sun_window", "presence_agg", "day_of_month",
                "interval_anchor", "time_pattern", "None")

_SUMMARY_CASES = [
    ("해 지기 30분 전에 거실 불 켜줘", "해 지기 30분 전"),
    ("월수금 아침 7시에 거실 조명 켜줘", "월·수·금"),
    ("집에 아무도 없으면 거실 조명 꺼줘", "나가면"),
    ("30분마다 욕실 환풍기 켜줘", "30분마다"),
    ("밤사이에 거실 움직임 감지되면 거실 조명 켜줘", "해 진 뒤부터"),
    ("오후 3시쯤 거실이 28도보다 높으면 에어컨 켜주고, 아니면 꺼줘.", "아니면"),
    ("아침 7시 되면 평일엔 안방 보일러 좀 켜주고, 주말엔 꺼 줬으면 해.", "평일"),
]


@pytest.mark.parametrize("sentence,expect", _SUMMARY_CASES)
def test_new_node_summary_described(gz, sentence, expect):
    summ = _p(gz, sentence)["summary"]
    assert summ and summ.strip(), f"빈 요약: {sentence!r}"
    assert expect in summ, f"기대 서술 '{expect}' 없음: {summ!r}"
    for bad in _BAD_MARKERS:
        assert bad not in summ, f"미지원/미서술 문구 '{bad}': {summ!r}"


def test_day_of_month_and_interval_summary(gz):
    s1 = _p(gz, "매달 1일 저녁 8시에 거실 조명 켜줘")["summary"]
    assert "매달 1일" in s1 and "미지원" not in s1
    s2 = _p(gz, "격주 금요일 저녁 7시에 거실 무드등 켜줘")["summary"]
    assert "격주" in s2 and "interval_anchor" not in s2


# ===========================================================================
# 4) 신규 노드 칩 방출(postpass)
# ===========================================================================
def _chip_ids(r):
    return {c.get("chosen") for c in r.get("chips", [])}


def test_sun_chip_emitted(gz):
    r = _p(gz, "해 지기 30분 전에 거실 불 켜줘")
    assert "sun:sunset" in _chip_ids(r)
    chip = next(c for c in r["chips"] if c["chosen"] == "sun:sunset")
    assert chip["candidates"][0]["sublabel"] == "일몰 트리거"
    assert chip["candidates"][0]["score"] == 1.0
    assert chip["text"] == "해 지기 30분 전"


def test_else_if_chip_emitted(gz):
    r = _p(gz, "오후 3시쯤 거실이 28도보다 높으면 에어컨 켜주고, 아니면 꺼줘.")
    assert "if_else" in _chip_ids(r)


def test_weekday_chip_emitted(gz):
    r = _p(gz, "월수금 아침 7시에 거실 조명 켜줘")
    ids = _chip_ids(r)
    assert any(str(i).startswith("weekday") for i in ids)


# ===========================================================================
# 5) 요약 모듈 단위 — 신규 노드 라벨(순수 함수)
# ===========================================================================
def test_summary_labels_unit():
    assert S.sun_label({"type": "sun", "event": "sunset", "offset": -1800}) == "해 지기 30분 전"
    assert S.sun_label({"type": "sun", "event": "sunrise"}) == "해 뜰 때"
    assert S.weekday_label({"days": ["mon", "wed", "fri"], "negate": False}) == "월·수·금"
    assert S.weekday_label({"days": ["sat", "sun"], "negate": True}) == "토·일 제외"
    assert S.day_of_month_label({"days": "last"}) == "매달 말일"
    assert S.day_of_month_label({"days": [1, 15]}) == "매달 1일, 15일"
    assert S.interval_label({"interval": 2}) == "격주마다"
    assert S.time_pattern_label({"minutes": 30}) == "30분마다"
    assert S.presence_label({"quant": "none"}, False) == "아무도 없으면"
    assert S.presence_label({"quant": "all"}, True) == "모두 집에 도착하면"
    # if 액션 서술
    if_node = {"type": "if",
               "if": [{"type": "day_type", "types": ["weekday"]}],
               "then": [{"type": "service", "action": "light.turn_on",
                         "target": {"entity_id": ["light.a"]}}],
               "else": [{"type": "service", "action": "light.turn_off",
                         "target": {"entity_id": ["light.a"]}}]}
    txt = S._describe_action(if_node, None)
    assert "평일" in txt and "아니면" in txt


def test_else_branch_model_is_savable(gz):
    """else 분기(if 조건에 신규 노드)를 담은 모델이 저장 검증을 통과해야 한다 —
    v1 검증기가 신규 조건을 '지원하지 않는 조건 유형'으로 오거부하면 규칙 저장이 막힌다."""
    from backend.engine.rule_model import validate_rule_model
    inv = _build_inventory()
    modes = list(_SETTINGS["modes"].keys())
    for s in ("아침 7시 되면 평일엔 안방 보일러 좀 켜주고, 주말엔 꺼 줬으면 해.",
              "안방 습도 70 넘으면 밤 시간대엔 전열교환기 켜 주시고, 아니면 꺼 주세요.",
              "해 뜨면 평일엔 커튼 좀 열어주고, 주말엔 그냥 닫아뒀으면 좋겠어."):
        m = _p(gz, s)["model"]
        errs = validate_rule_model(m, inv, modes)
        assert errs == [], f"else 분기 모델 검증 실패({s!r}): {errs}"


def test_summary_no_new_node_unchanged(gz):
    """신규 노드가 없으면 기존 파서 요약과 동일(재생성 안 함 — 회귀 방지)."""
    r = _p(gz, "거실에 움직임이 감지되면 거실 조명을 켜줘")
    assert "모션이 감지되면" in r["summary"]
    assert not any(a.get("type") == "if"
                   for s in (r["model"].get("subrules") or [r["model"]])
                   for a in s.get("actions", []))
