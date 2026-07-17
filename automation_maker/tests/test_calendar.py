"""APP-PORT-PLAN S3 (달력 축) 결정적 테스트.

  - evaluator: weekday(negate)/day_of_month(list·last·2월 경계)/interval_anchor
    (앵커 주·차주·격주·3주·앵커 이전 주·오류 anchor) 순수 평가(§2.5).
  - 검증기: weekday/day_of_month/interval_anchor 정상·경계·오류(§3).
  - ha_map: weekday 여집합 전개·day_of_month/interval_anchor template(§2.6).
  - postpass: 요일 승격 게이트(explicit 항상 · bare 는 이벤트+비세그먼트일 때만) + negate.
  - 결정성: interval_anchor.anchor = 주입 now 가 속한 주의 월요일. now_fn 주입으로 2회
    재실행 동일, 주입 now 로만 anchor 결정(벽시계·랜덤 미사용).

날짜 기준(검증 완료): 2026-07-13=월(주 0), 07-17=금(라벨 작성일, 같은 주), 07-20=월(다음 주).
2026 비윤년 → 2월 말일=28일.
"""
from __future__ import annotations

from datetime import datetime

from backend.automation_builder import _build_condition
from backend.engine.evaluator import EvalContext, evaluate_condition
from backend.engine.ha_map import subrule_to_automation
from backend.engine.rule_model import validate_rule_model
from backend.nl import postpass
from backend.nl.gazetteer import Gazetteer
from backend.nl.parser import parse

# 2026-07-13 = 월요일(주 0). 07-14 화 … 07-18 토, 07-19 일.
MON = datetime(2026, 7, 13, 12, 0)
TUE = datetime(2026, 7, 14, 12, 0)
SAT = datetime(2026, 7, 18, 12, 0)
SUN = datetime(2026, 7, 19, 12, 0)


def _ctx(now):
    return EvalContext(cache=None, gvars=None, now_fn=lambda: now,
                       inventory_fn=lambda: {})


# ===========================================================================
# evaluator — weekday (§2.5)
# ===========================================================================
def test_eval_weekday_positive():
    c = {"type": "weekday", "days": ["mon"], "negate": False}
    assert evaluate_condition(c, _ctx(MON)) is True
    assert evaluate_condition(c, _ctx(TUE)) is False


def test_eval_weekday_multi():
    c = {"type": "weekday", "days": ["tue", "thu", "sat"], "negate": False}
    assert evaluate_condition(c, _ctx(TUE)) is True
    assert evaluate_condition(c, _ctx(SAT)) is True
    assert evaluate_condition(c, _ctx(MON)) is False


def test_eval_weekday_negate():
    """주말 빼고 = negate True, days [sat,sun] → 평일 통과, 주말 제외(XOR)."""
    c = {"type": "weekday", "days": ["sat", "sun"], "negate": True}
    assert evaluate_condition(c, _ctx(MON)) is True
    assert evaluate_condition(c, _ctx(SAT)) is False
    assert evaluate_condition(c, _ctx(SUN)) is False


# ===========================================================================
# evaluator — day_of_month (§2.5)
# ===========================================================================
def test_eval_day_of_month_list():
    c = {"type": "day_of_month", "days": [10, 20]}
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 20, 9, 0))) is True
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 15, 9, 0))) is False


def test_eval_day_of_month_last():
    """말일: 내일이 1일. 7월(31일)·2월(2026 비윤년=28일) 경계."""
    c = {"type": "day_of_month", "days": "last"}
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 31, 18, 0))) is True
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 30, 18, 0))) is False
    assert evaluate_condition(c, _ctx(datetime(2026, 2, 28, 18, 0))) is True
    assert evaluate_condition(c, _ctx(datetime(2026, 2, 27, 18, 0))) is False


# ===========================================================================
# evaluator — interval_anchor (§2.5)
# ===========================================================================
def _ia(interval=2, anchor="2026-07-13"):
    return {"type": "interval_anchor", "unit": "week",
            "interval": interval, "anchor": anchor}


def test_eval_interval_anchor_biweekly():
    c = _ia(2)
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 15))) is True   # 앵커 주(0)
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 19))) is True   # 앵커 주 일요일(월 정렬)
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 22))) is False  # 1주 뒤
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 29))) is True   # 2주 뒤


def test_eval_interval_anchor_triweekly():
    c = _ia(3)
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 15))) is True   # 0주
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 22))) is False  # 1주
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 29))) is False  # 2주
    assert evaluate_condition(c, _ctx(datetime(2026, 8, 5))) is True    # 3주


def test_eval_interval_anchor_before_anchor():
    """앵커 이전 주(음수 주차)도 floor 나눗셈으로 mod 정합."""
    c = _ia(2)
    assert evaluate_condition(c, _ctx(datetime(2026, 6, 29))) is True   # 2주 전
    assert evaluate_condition(c, _ctx(datetime(2026, 7, 6))) is False   # 1주 전


def test_eval_interval_anchor_bad_anchor_false():
    assert evaluate_condition(_ia(2, "not-a-date"), _ctx(MON)) is False
    assert evaluate_condition(_ia(2, None), _ctx(MON)) is False


# ===========================================================================
# 검증기 (rule_model, §3)
# ===========================================================================
_INV = {"entities": []}  # valid_ids 비움 → 실존 검사 스킵(노드 필드 검증만)


def _model(conds):
    return {"subrules": [{"triggers": [{"type": "daily", "at": "07:00"}],
                          "conditions": list(conds),
                          "actions": [{"type": "service", "action": "light.turn_on"}]}]}


def test_validate_weekday_ok():
    assert validate_rule_model(
        _model([{"type": "weekday", "days": ["mon", "fri"], "negate": False}]), _INV) == []
    assert validate_rule_model(
        _model([{"type": "weekday", "days": ["sat", "sun"], "negate": True}]), _INV) == []


def test_validate_weekday_bad():
    for bad in ([], ["funday"], ["mon", "xyz"]):
        errs = validate_rule_model(
            _model([{"type": "weekday", "days": bad, "negate": False}]), _INV)
        assert any(".days" in e["path"] for e in errs), bad
    errs = validate_rule_model(
        _model([{"type": "weekday", "days": ["mon"], "negate": "yes"}]), _INV)
    assert any(".negate" in e["path"] for e in errs)


def test_validate_day_of_month_ok():
    assert validate_rule_model(
        _model([{"type": "day_of_month", "days": [1, 15, 31]}]), _INV) == []
    assert validate_rule_model(
        _model([{"type": "day_of_month", "days": "last"}]), _INV) == []


def test_validate_day_of_month_bad():
    # 빈 목록·범위 밖(0/32)·잘못된 문자열·실수·불(bool) 전부 거부.
    for bad in ([], [0], [32], "first", [1.5], [True]):
        errs = validate_rule_model(_model([{"type": "day_of_month", "days": bad}]), _INV)
        assert any(".days" in e["path"] for e in errs), bad


def test_validate_interval_anchor_ok():
    assert validate_rule_model(
        _model([{"type": "interval_anchor", "unit": "week",
                 "interval": 2, "anchor": "2026-07-13"}]), _INV) == []


def test_validate_interval_anchor_bad():
    base = {"type": "interval_anchor", "unit": "week",
            "interval": 2, "anchor": "2026-07-13"}
    errs = validate_rule_model(_model([{**base, "unit": "day"}]), _INV)
    assert any(".unit" in e["path"] for e in errs)
    for bad in (1, 0, True, "2", 2.0):  # <2·bool·비정수 거부
        errs = validate_rule_model(_model([{**base, "interval": bad}]), _INV)
        assert any(".interval" in e["path"] for e in errs), bad
    errs = validate_rule_model(_model([{**base, "anchor": "2026/07/13"}]), _INV)
    assert any(".anchor" in e["path"] for e in errs)


# ===========================================================================
# ha_map (§2.6) — v2 → HA v1 방언
# ===========================================================================
def test_ha_map_weekday_plain():
    out = subrule_to_automation({"triggers": [], "conditions": [
        {"type": "weekday", "days": ["mon", "wed", "fri"], "negate": False}],
        "actions": []})
    cond = out["conditions"][0]
    assert cond == {"type": "time", "weekday": ["mon", "wed", "fri"]}
    assert _build_condition(cond) == {"condition": "time",
                                      "weekday": ["mon", "wed", "fri"]}
    assert out["warnings"] == []


def test_ha_map_weekday_negate_complement():
    """negate=true → 여집합으로 전개(HA time.weekday 은 부정 미지원)."""
    out = subrule_to_automation({"triggers": [], "conditions": [
        {"type": "weekday", "days": ["sat", "sun"], "negate": True}], "actions": []})
    assert out["conditions"][0] == {
        "type": "time", "weekday": ["mon", "tue", "wed", "thu", "fri"]}


def test_ha_map_day_of_month_list_template():
    out = subrule_to_automation({"triggers": [], "conditions": [
        {"type": "day_of_month", "days": [10, 20]}], "actions": []})
    cond = out["conditions"][0]
    assert cond == {"type": "template",
                    "value_template": "{{ now().day in [10, 20] }}"}
    assert _build_condition(cond) == {
        "condition": "template", "value_template": "{{ now().day in [10, 20] }}"}


def test_ha_map_day_of_month_last_template():
    out = subrule_to_automation({"triggers": [], "conditions": [
        {"type": "day_of_month", "days": "last"}], "actions": []})
    assert out["conditions"][0]["value_template"] == \
        "{{ (now() + timedelta(days=1)).day == 1 }}"


def test_ha_map_interval_anchor_template():
    out = subrule_to_automation({"triggers": [], "conditions": [
        {"type": "interval_anchor", "unit": "week",
         "interval": 2, "anchor": "2026-07-13"}], "actions": []})
    cond = out["conditions"][0]
    assert cond["type"] == "template"
    vt = cond["value_template"]
    assert "604800" in vt and "% 2 == 0" in vt and "2026-07-13" in vt
    assert "now().weekday()" in vt          # 월요일 정렬 주차식
    assert _build_condition(cond)["condition"] == "template"


# ===========================================================================
# postpass — 요일 승격 게이트(§2.2)
# ===========================================================================
def _sub(triggers, conditions=()):
    return {"triggers": list(triggers), "conditions": list(conditions), "actions": []}


def test_postpass_weekday_explicit_always():
    """개별 요일(월요일)은 daily 문맥이라도 항상 weekday."""
    sub = _sub([{"type": "daily", "at": "06:30"}])
    postpass._augment_calendar("월요일마다 아침 6시 반", sub)
    assert {"type": "weekday", "days": ["mon"], "negate": False} in sub["conditions"]


def test_postpass_weekday_abbrev():
    """요일 축약(화목토)."""
    sub = _sub([{"type": "daily", "at": "19:00"}])
    postpass._augment_calendar("화목토 저녁 7시마다", sub)
    wd = [c for c in sub["conditions"] if c.get("type") == "weekday"]
    assert wd and wd[0]["days"] == ["tue", "thu", "sat"]


def test_postpass_weekday_bare_with_event_no_segment():
    """맨 평일 + 상태 이벤트(세그먼트 없음) → weekday 승격(_16 패턴)."""
    sub = _sub([{"type": "state", "entity_id": "binary_sensor.entrance_door", "to": "on"}])
    postpass._augment_calendar("평일에 현관문 열리면", sub)
    wd = [c for c in sub["conditions"] if c.get("type") == "weekday"]
    assert wd and wd[0]["days"] == ["mon", "tue", "wed", "thu", "fri"]


def test_postpass_weekday_bare_with_segment_stays_daytype():
    """맨 주말 + 시간대(time_segment) 동반 → 승격 안 함(기존 day_type 보존, test_regr_6)."""
    sub = _sub([{"type": "state", "entity_id": "binary_sensor.living_room_motion", "to": "on"}],
               [{"type": "day_type", "types": ["weekend"]},
                {"type": "time_segment", "segments": ["morning"]}])
    postpass._augment_calendar("주말 아침에 움직이면", sub)
    assert {"type": "day_type", "types": ["weekend"]} in sub["conditions"]
    assert not any(c.get("type") == "weekday" for c in sub["conditions"])


def test_postpass_weekday_bare_daily_only_no_promote():
    """맨 평일 + daily 트리거만(이벤트 없음) → 승격 안 함."""
    sub = _sub([{"type": "daily", "at": "07:20"}],
               [{"type": "day_type", "types": ["weekday"]}])
    postpass._augment_calendar("평일엔 아침 7시 20분마다", sub)
    assert not any(c.get("type") == "weekday" for c in sub["conditions"])


def test_postpass_weekday_negate():
    sub = _sub([{"type": "daily", "at": "07:00"}])
    postpass._augment_calendar("주말 빼고 매일 아침 7시에", sub)
    assert {"type": "weekday", "days": ["sat", "sun"], "negate": True} in sub["conditions"]


def test_postpass_day_of_month():
    sub = _sub([{"type": "daily", "at": "09:00"}])
    postpass._augment_calendar("매달 25일 아침 9시엔", sub)
    assert {"type": "day_of_month", "days": [25]} in sub["conditions"]
    sub2 = _sub([{"type": "daily", "at": "18:00"}])
    postpass._augment_calendar("이번 달 마지막 날 저녁 6시엔", sub2)
    assert {"type": "day_of_month", "days": "last"} in sub2["conditions"]


# ===========================================================================
# 결정성 — interval_anchor.anchor = 주입 now 가 속한 주의 월요일
# ===========================================================================
def test_monday_iso_from_injected_now():
    # 라벨주(2026-07-17 금)의 월요일 = 2026-07-13.
    assert postpass._monday_iso(lambda: datetime(2026, 7, 17, 12, 0)) == "2026-07-13"
    # 다른 주 → 다른 앵커.
    assert postpass._monday_iso(lambda: datetime(2026, 7, 20, 9, 0)) == "2026-07-20"
    # 월요일 당일도 그 주 월요일.
    assert postpass._monday_iso(lambda: datetime(2026, 7, 13, 0, 0)) == "2026-07-13"


def test_postpass_interval_anchor_from_now():
    sub = _sub([{"type": "daily", "at": "20:00"}])
    postpass._augment_calendar("격주 화요일 저녁 8시엔", sub, now_fn=lambda: MON)
    assert {"type": "weekday", "days": ["tue"], "negate": False} in sub["conditions"]
    ia = [c for c in sub["conditions"] if c.get("type") == "interval_anchor"]
    assert ia and ia[0]["interval"] == 2 and ia[0]["anchor"] == "2026-07-13"


# --- parse() 통합: 라벨주 now 주입으로 결정성 확인 ---
_CAL_INV = {
    "areas": [{"area_id": "living_room", "name": "거실", "icon": ""}],
    "entities": [{"entity_id": "light.living_room_main", "domain": "light",
                  "name": "거실 메인등", "area_id": "living_room", "area_name": "거실",
                  "device_id": None, "device_name": None, "device_class": None,
                  "state": "off", "unit": None, "attributes": {}}],
    "zones": [{"entity_id": "zone.home", "name": "집"}],
}
_CAL_SETTINGS = {
    "segments": {"dawn": "00:00", "morning": "06:00", "day": "09:00",
                 "evening": "17:00", "night": "21:00"},
    "persons": {}, "modes": {}, "near_home": {"zone_state": "home"}, "aliases": [],
}


def _cal_gz():
    return Gazetteer.build(_CAL_INV, _CAL_SETTINGS)


def _conds(model):
    """단일 규칙(flat) / subrules 두 형태 모두에서 conditions 추출."""
    sub = model["subrules"][0] if "subrules" in model else model
    return sub.get("conditions", [])


def test_parse_interval_anchor_deterministic():
    gz = _cal_gz()
    def now():
        return datetime(2026, 7, 17, 12, 0)
    r1 = parse("격주 월요일 밤 9시에 거실 메인등 켜줘", gz, _CAL_SETTINGS, now_fn=now)
    r2 = parse("격주 월요일 밤 9시에 거실 메인등 켜줘", gz, _CAL_SETTINGS, now_fn=now)
    assert r1["model"] == r2["model"]                  # 2회 재실행 동일(결정성)
    ia = [c for c in _conds(r1["model"]) if c.get("type") == "interval_anchor"]
    assert ia and ia[0]["anchor"] == "2026-07-13"      # 라벨주 월요일


def test_parse_interval_anchor_follows_injected_week():
    """anchor 는 주입 now 로만 결정 — 다른 주 now → 다른 anchor(고정상수 아님)."""
    gz = _cal_gz()
    r = parse("격주 월요일 밤 9시에 거실 메인등 켜줘", gz, _CAL_SETTINGS,
              now_fn=lambda: datetime(2026, 7, 20, 9, 0))
    ia = [c for c in _conds(r["model"]) if c.get("type") == "interval_anchor"]
    assert ia and ia[0]["anchor"] == "2026-07-20"
