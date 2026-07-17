"""tools/corpus 도구 자체 검증 — SPEC-CORPUS §6 (앱 tests/ 와 분리).

이 파일은 **도구 파이프라인만** 검증한다. 앱 pytest(현재 321개)는 건드리지 않는다.
앱 코드는 읽기 전용 import 만 하고(§계약), 출력은 임시 디렉터리에만 써서
커밋 스냅샷 ``out/report.md`` 등을 침해하지 않는다.

검증 범위(§6 체크리스트):
  - generate 결정성(같은 seed→동일, 다른 seed→상이) + CorpusItem 스키마(§3.1)
    + 생성 시점 gold 자가검증(§7.4)
  - structural_match 의 exact/partial/fail 분류가 손수 만든 gold/actual 쌍에서 정확
    (각 분류 2케이스 이상) + diff 5분류 오류태그 확인(§7.6)
  - mine.abstract_to_template 이 엔티티/값 스팬을 슬롯 자리표시자로 역치환(§3.5)
  - evaluate 가 실제 파서로 커버리지를 산출(§6 "실제 파서 기준")
  - run.py 가 out/ 산출물(report.md·gap_library.yaml·pattern_library.yaml) 생성(§3.6/§5)

실행:
  cd tools/corpus && python -m pytest test_tool.py -q
  cd tools/corpus && python test_tool.py        # pytest 없이도 동작(간이 러너)

core 담당과 병렬 구현이므로, 내부 표현이 아니라 **SPEC §3 계약**(verdict 문자열,
§7.6 오류태그명, compare 반환 키, 산출물 파일명, 결정성)에 대해 단언한다.
"""
from __future__ import annotations

import os
import sys
import tempfile

import yaml

# --- import 배선: 위치 기준(실행 CWD 무관) ---------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", "automation_maker"))
for _p in (_HERE, _APP_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import evaluate as ev  # noqa: E402
import generate as gen  # noqa: E402
import mine  # noqa: E402
import run  # noqa: E402
import structural_match as sm  # noqa: E402

TEMPLATES = os.path.join(_HERE, "templates.yaml")
SLOTS = os.path.join(_HERE, "slots.yaml")

# 목데이터 실존 엔티티 id (mock_data.py) — 손수 만든 gold/actual 쌍에 사용.
LR_MOTION = "binary_sensor.living_room_motion"
BR_MOTION = "binary_sensor.bathroom_motion"
LR_MAIN = "light.living_room_main"
BR_LIGHT = "light.bathroom"
LR_TEMP = "sensor.living_room_temperature"
PERSON_USER = "person.user"
PERSON_WIFE = "person.wife"


# ---------------------------------------------------------------------------
# 공용 픽스처(인벤토리/게이저티어)는 한 번만 만들어 재사용(빌드 비용 절감).
# ---------------------------------------------------------------------------
_CACHE: dict = {}


def _env():
    """(inventory, gazetteer, settings, mode_names) 캐시."""
    if "v" not in _CACHE:
        inv, gz, settings = ev.build_inventory()
        _CACHE["v"] = (inv, gz, settings, set(settings.get("modes", {})))
    return _CACHE["v"]


def _tags(diff):
    return sorted({d.get("tag") for d in (diff or [])})


# ---------------------------------------------------------------------------
# 손수 만든 gold/actual 모델 (top-level triggers/conditions/actions)
# ---------------------------------------------------------------------------
def _trig(entity=LR_MOTION, to="on"):
    return {"type": "state", "entity_id": entity, "to": to}


def _act_on(entity=LR_MAIN, data=None):
    node = {"type": "service", "action": "light.turn_on",
            "target": {"entity_id": [entity]}}
    if data:
        node["data"] = data
    return node


def _model(triggers, conditions, actions):
    return {"triggers": triggers, "conditions": conditions, "actions": actions}


# ===========================================================================
# 1. generate 결정성 + 스키마 + gold 자가검증
# ===========================================================================
def test_generate_deterministic():
    """같은 seed → 완전히 동일한 코퍼스, 다른 seed → 다른 표본(§3.1/§7.3)."""
    inv, _gz, _settings, modes = _env()
    tpls = gen.load_templates(TEMPLATES)
    slots = gen.load_slots(SLOTS)
    assert tpls and slots, "templates.yaml/slots.yaml 로드 실패"

    c1 = gen.generate(tpls, slots, inv, seed=0, limit_per_template=6, mode_names=modes)
    c2 = gen.generate(tpls, slots, inv, seed=0, limit_per_template=6, mode_names=modes)
    assert len(c1) > 0
    # 같은 seed → id·문장·gold 까지 완전 동일(결정적)
    assert c1 == c2, "동일 seed 인데 코퍼스가 달라짐(비결정적)"

    c3 = gen.generate(tpls, slots, inv, seed=1, limit_per_template=6, mode_names=modes)
    s1 = [it["sentence"] for it in c1]
    s3 = [it["sentence"] for it in c3]
    assert s1 != s3, "seed 를 바꿔도 표본이 동일함(셔플이 seed 를 안 씀)"


def test_generate_corpusitem_schema():
    """CorpusItem 이 §3.1 필수 키를 갖고 source='grammar'."""
    inv, _gz, _settings, modes = _env()
    tpls = gen.load_templates(TEMPLATES)
    slots = gen.load_slots(SLOTS)
    corpus = gen.generate(tpls, slots, inv, seed=0, limit_per_template=4, mode_names=modes)
    assert corpus
    required = {"id", "sentence", "gold", "area", "template_id", "source", "tags"}
    for it in corpus[:20]:
        assert required <= set(it), f"CorpusItem 키 누락: {required - set(it)}"
        assert it["source"] == "grammar"
        assert isinstance(it["sentence"], str) and it["sentence"].strip()
        # gold 는 validate 가능한 model dict
        assert isinstance(it["gold"], dict)
        assert "subrules" in it["gold"] or "triggers" in it["gold"]


def test_generate_gold_self_validation():
    """생성 시점 gold 자가검증(§7.4): 잘못된 gold 는 gold_invalid 로 격리, 정상은 통과."""
    inv, _gz, _settings, modes = _env()
    # 존재하지 않는 엔티티를 액션 대상으로 둔 인라인 템플릿 → gold_invalid True 기대
    bad_tpl = {
        "id": "selfcheck_bad", "area": "test", "template": "테스트 문장", "slots": [],
        "gold": _model([_trig()], [], [_act_on("light.NOPE_DOES_NOT_EXIST")]),
    }
    bad = gen.generate([bad_tpl], {}, inv, seed=0, limit_per_template=2, mode_names=modes)
    assert bad and bad[0]["gold_invalid"] is True
    assert bad[0]["gold_errors"], "gold_invalid 인데 사유가 비어 있음"

    # 실존 엔티티만 쓰는 인라인 템플릿 → gold_invalid False 기대
    good_tpl = {
        "id": "selfcheck_good", "area": "test", "template": "테스트 문장", "slots": [],
        "gold": _model([_trig()], [], [_act_on(LR_MAIN)]),
    }
    good = gen.generate([good_tpl], {}, inv, seed=0, limit_per_template=2, mode_names=modes)
    assert good and good[0]["gold_invalid"] is False


# ===========================================================================
# 2. structural_match — exact / partial / fail (각 2케이스 이상)
# ===========================================================================
def test_structural_match_exact():
    """exact = 모든 subrule 핵심필드 집합 일치(§3.3). 정규화(bool/str, str/list) 포함."""
    # (1) 완전 동일
    g = _model([_trig()], [], [_act_on()])
    a = _model([_trig()], [], [_act_on()])
    r = sm.compare(g, a)
    assert r["verdict"] == "exact"
    assert r["diff"] == [], "exact 인데 diff 가 비어 있지 않음"
    assert r["trigger_match"] and r["cond_match"] and r["action_match"]

    # (2) YAML 정규화: to=True(bool) vs "on", target 문자열 vs 리스트 → 여전히 exact
    g2 = _model([{"type": "state", "entity_id": LR_MOTION, "to": True}], [],
                [{"type": "service", "action": "light.turn_on",
                  "target": {"entity_id": LR_MAIN}}])
    a2 = _model([{"type": "state", "entity_id": LR_MOTION, "to": "on"}], [],
                [{"type": "service", "action": "light.turn_on",
                  "target": {"entity_id": [LR_MAIN]}}])
    r2 = sm.compare(g2, a2)
    assert r2["verdict"] == "exact", "bool/str·str/list 정규화 실패"


def test_structural_match_partial():
    """partial = triggers 일치하나 조건/액션/subrule 개수 불일치(§3.3)."""
    # (1) 액션 data 값 불일치(brightness_pct)
    g = _model([_trig()], [], [_act_on(data={"brightness_pct": 10})])
    a = _model([_trig()], [], [_act_on(data={"brightness_pct": 50})])
    r = sm.compare(g, a)
    assert r["verdict"] == "partial"
    assert r["trigger_match"] is True and r["action_match"] is False

    # (2) 조건 개수 불일치(gold 에 time_segment 조건, actual 에는 없음)
    g2 = _model([_trig()], [{"type": "time_segment", "segments": ["dawn"]}], [_act_on()])
    a2 = _model([_trig()], [], [_act_on()])
    r2 = sm.compare(g2, a2)
    assert r2["verdict"] == "partial"
    assert r2["trigger_match"] is True and r2["cond_match"] is False

    # (3) subrule 개수 불일치(같은 노드, 다른 그룹핑) → count_match False 로 partial
    g3 = _model([_trig(LR_MOTION), _trig(BR_MOTION)], [],
                [_act_on(LR_MAIN), _act_on(BR_LIGHT)])
    a3 = {"subrules": [
        {"triggers": [_trig(LR_MOTION)], "conditions": [], "actions": [_act_on(LR_MAIN)]},
        {"triggers": [_trig(BR_MOTION)], "conditions": [], "actions": [_act_on(BR_LIGHT)]},
    ]}
    r3 = sm.compare(g3, a3)
    assert r3["verdict"] == "partial"
    assert r3["trigger_match"] is True and r3["subrule_count_match"] is False


def test_structural_match_fail():
    """fail = triggers 불일치(§3.3)."""
    # (1) 트리거 to 값 불일치
    g = _model([_trig(to="on")], [], [_act_on()])
    a = _model([_trig(to="off")], [], [_act_on()])
    r = sm.compare(g, a)
    assert r["verdict"] == "fail" and r["trigger_match"] is False

    # (2) 트리거 엔티티 불일치(다른 센서에 바인딩)
    a2 = _model([_trig(entity=BR_MOTION)], [], [_act_on()])
    r2 = sm.compare(g, a2)
    assert r2["verdict"] == "fail" and r2["trigger_match"] is False

    # (3) 트리거 개수 부족(gold 2 트리거, actual 1) → 멀티셋 불일치로 fail
    g3 = _model([_trig(LR_MOTION), _trig(BR_MOTION)], [], [_act_on()])
    a3 = _model([_trig(LR_MOTION)], [], [_act_on()])
    r3 = sm.compare(g3, a3)
    assert r3["verdict"] == "fail"


def test_structural_match_error_tags_five():
    """diff 5분류 오류태그(§7.6): missing/extra/value_mismatch/wrong_node_type/entity_confusion."""
    # missing_node: gold 조건이 actual 에 없음
    g = _model([_trig()], [{"type": "time_segment", "segments": ["dawn"]}], [_act_on()])
    a = _model([_trig()], [], [_act_on()])
    assert "missing_node" in _tags(sm.compare(g, a)["diff"])

    # extra_node: actual 에만 있는 조건
    assert "extra_node" in _tags(sm.compare(a, g)["diff"])

    # value_mismatch: 같은 타입·엔티티, 값만 다름(액션 data)
    gv = _model([_trig()], [], [_act_on(data={"brightness_pct": 10})])
    av = _model([_trig()], [], [_act_on(data={"brightness_pct": 80})])
    assert "value_mismatch" in _tags(sm.compare(gv, av)["diff"])

    # wrong_node_type: 같은 엔티티, 노드 타입만 다름(state vs numeric_state)
    gt = _model([_trig()], [{"type": "state", "entity_id": LR_TEMP, "state": "on"}], [_act_on()])
    at = _model([_trig()], [{"type": "numeric_state", "entity_id": LR_TEMP, "above": 25}], [_act_on()])
    assert "wrong_node_type" in _tags(sm.compare(gt, at)["diff"])

    # entity_confusion: 같은 타입, 다른 엔티티 바인딩(사람 혼동)
    ge = _model([_trig()], [{"type": "state", "entity_id": PERSON_USER, "state": "home"}], [_act_on()])
    ae = _model([_trig()], [{"type": "state", "entity_id": PERSON_WIFE, "state": "home"}], [_act_on()])
    assert "entity_confusion" in _tags(sm.compare(ge, ae)["diff"])


def test_structural_match_return_shape():
    """compare 반환 계약 키(§3.3)."""
    r = sm.compare(_model([_trig()], [], [_act_on()]),
                   _model([_trig()], [], [_act_on()]))
    for k in ("verdict", "diff", "trigger_match", "subrule_count_match",
              "cond_match", "action_match"):
        assert k in r, f"compare 반환 키 누락: {k}"
    assert r["verdict"] in ("exact", "partial", "fail")


# ===========================================================================
# 3. mine.abstract_to_template — 엔티티/값 스팬 → 슬롯 역치환(§3.5)
# ===========================================================================
def test_abstract_to_template_delexicalizes():
    """엔티티/방/수치/시간대 스팬이 슬롯 자리표시자로 바뀌고 원표면형은 사라진다."""
    _inv, gz, _settings, _modes = _env()

    # 다중 스팬 문장: 새벽/거실/움직임/조명/10%/3분/모션이 각각 슬롯으로.
    s = "새벽에 거실에서 움직임이 감지되면 거실 조명을 10% 켜주고 3분 동안 모션이 없으면 꺼줘"
    out = mine.abstract_to_template({"sentence": s}, gz)
    # 슬롯 자리표시자가 들어갔는가
    for ph in ("{SEG}", "{ROOM}", "{DEVICE}", "{PERCENT}", "{DUR}"):
        assert ph in out, f"{ph} 자리표시자 누락 — {out!r}"
    # 엔티티/값 원표면형이 남아 있으면 역치환 실패
    for raw in ("새벽", "거실", "조명", "움직임", "모션", "10%", "3분"):
        assert raw not in out, f"원표면형 '{raw}' 가 남음(역치환 실패) — {out!r}"
    # 비-슬롯(동사/구조) 토큰은 보존
    for kept in ("감지되면", "켜주고", "동안", "없으면", "꺼줘"):
        assert kept in out, f"구조 토큰 '{kept}' 소실 — {out!r}"

    # 간단 문장: 방·기기 → {ROOM} {DEVICE}, 조사는 {J}
    out2 = mine.abstract_to_template({"sentence": "거실 조명을 꺼줘"}, gz)
    assert "{ROOM}" in out2 and "{DEVICE}" in out2
    assert "거실" not in out2 and "조명" not in out2
    assert "꺼줘" in out2


# ===========================================================================
# 4. mine.mine_gaps — 실패/부분 문장 클러스터(§3.5)
# ===========================================================================
def test_mine_gaps_clusters():
    """fail/partial 만 클러스터, 같은 추상패턴은 병합, 빈도순 정렬, 스키마 보유."""
    _inv, gz, _settings, _modes = _env()
    # 두 문장은 표면형(10% vs 50프로)이 다르지만 delexicalize 하면 동일 추상패턴
    # '{ROOM} {DEVICE}{J} {PERCENT} 켜줘' → 한 클러스터로 병합되어야 한다.
    items = [
        {"item": {"sentence": "거실 조명을 10% 켜줘", "area": "target", "template_id": "t1"},
         "verdict": "fail", "diff": [{"tag": "missing_node", "category": "trigger"}]},
        {"item": {"sentence": "거실 조명을 50프로 켜줘", "area": "target", "template_id": "t1"},
         "verdict": "fail", "diff": [{"tag": "missing_node", "category": "trigger"}]},
        {"item": {"sentence": "안방 온도가 25도 이상이면 안방 에어컨을 켜줘",
                  "area": "num", "template_id": "t2"},
         "verdict": "partial", "diff": [{"tag": "value_mismatch", "category": "condition"}]},
        # exact 는 갭 마이닝 대상 아님 → 제외되어야 함
        {"item": {"sentence": "이건 무시되어야 함", "area": "x", "template_id": "t3"},
         "verdict": "exact", "diff": []},
    ]
    clusters = mine.mine_gaps({"items": items}, gz)
    assert clusters, "fail/partial 이 있는데 클러스터가 비었음"
    # exact 문장은 클러스터 예시에 없어야 한다
    all_examples = [ex for c in clusters for ex in c["examples"]]
    assert "이건 무시되어야 함" not in all_examples

    # 빈도순 내림차순
    counts = [c["count"] for c in clusters]
    assert counts == sorted(counts, reverse=True)

    # 표면형이 다른 두 문장이 같은 추상패턴으로 병합 → count 2 클러스터
    top = clusters[0]
    assert top["count"] == 2, f"동형 추상패턴 2개가 병합 안 됨: {[(c['pattern'], c['count']) for c in clusters]}"
    for k in ("pattern", "count", "area", "examples", "verdict_mix"):
        assert k in top, f"클러스터 스키마 키 누락: {k}"
    assert top["verdict_mix"].get("fail") == 2


# ===========================================================================
# 5. evaluate — 실제 파서로 커버리지 산출(§6 "실제 파서 기준")
# ===========================================================================
def test_evaluate_real_parser_coverage():
    """작은 코퍼스를 실제 parse() 로 평가 → 반환 구조·집계가 채워진다."""
    inv, gz, settings, modes = _env()
    tpls = gen.load_templates(TEMPLATES)
    slots = gen.load_slots(SLOTS)
    corpus = gen.generate(tpls, slots, inv, seed=0, limit_per_template=2, mode_names=modes)
    assert corpus
    res = ev.evaluate(corpus, gz, settings, inv)
    for k in ("total", "by_verdict", "by_area", "by_template", "by_source", "items"):
        assert k in res, f"evaluate 반환 키 누락: {k}"
    assert res["total"] == len(corpus)
    bv = res["by_verdict"]
    assert set(bv) >= {"exact", "partial", "fail"}
    # 집계 합(격리 제외)이 verdict 합과 맞물린다
    assert sum(bv.values()) == res["evaluated"]
    assert res["evaluated"] + res["gold_invalid"] == res["total"]
    # 실제 파서가 최소 몇 문장은 정확히 맞힌다(커버리지 신호가 실제로 채워짐)
    assert bv["exact"] >= 1, "실제 파서가 단 한 문장도 exact 로 못 맞힘(파서 배선 의심)"


# ===========================================================================
# 6. run.py — out/ 산출물 생성(§3.6/§5). 임시 dir 로 커밋 스냅샷 비침해.
# ===========================================================================
def test_run_pipeline_writes_artifacts():
    """run.main 이 report.md·gap_library.yaml·pattern_library.yaml 을 생성(파싱 가능)."""
    saved = run.OUT_DIR
    with tempfile.TemporaryDirectory() as td:
        run.OUT_DIR = td
        try:
            run.main(["--no-augment", "--limit", "3"])
        finally:
            run.OUT_DIR = saved

        report = os.path.join(td, "report.md")
        gap = os.path.join(td, "gap_library.yaml")
        patlib = os.path.join(td, "pattern_library.yaml")
        for p in (report, gap, patlib):
            assert os.path.exists(p), f"산출물 미생성: {os.path.basename(p)}"
            assert os.path.getsize(p) > 0, f"산출물 비어 있음: {os.path.basename(p)}"

        # report.md 는 커버리지 종합 리포트
        text = open(report, encoding="utf-8").read()
        assert "커버리지" in text and "갭" in text

        # gap_library.yaml 은 클러스터 리스트(빈도순), 각 항목에 pattern/count
        gaps = yaml.safe_load(open(gap, encoding="utf-8"))
        assert isinstance(gaps, list)
        if gaps:
            assert {"pattern", "count", "area"} <= set(gaps[0])

        # pattern_library.yaml 은 시드 템플릿 전체 + 커버 상태(하이브리드 데이터 자산)
        patterns = yaml.safe_load(open(patlib, encoding="utf-8"))
        assert isinstance(patterns, list) and patterns
        entry = patterns[0]
        assert {"id", "status", "template"} <= set(entry)
        assert entry["status"] in ("covered", "partial", "gap", "unknown")


# ---------------------------------------------------------------------------
# pytest 없이도 실행 가능한 간이 러너
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import traceback

    _tests = [(n, f) for n, f in sorted(globals().items())
              if n.startswith("test_") and callable(f)]
    _failed = 0
    for _name, _fn in _tests:
        try:
            _fn()
            print(f"PASS  {_name}")
        except Exception:  # noqa: BLE001 — 러너: 실패를 모아 보고
            _failed += 1
            print(f"FAIL  {_name}")
            traceback.print_exc()
    print(f"\n{len(_tests) - _failed}/{len(_tests)} passed")
    sys.exit(1 if _failed else 0)
