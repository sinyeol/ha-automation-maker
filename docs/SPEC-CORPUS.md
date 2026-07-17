# SPEC-CORPUS — 패턴 라이브러리 생성·증강·평가 도구 (하이브리드 파서 준비)

목적(사용자 요청): 다중규칙·수동매핑·모드 문장의 **예시를 생성하는 도구**를 만들어 파서의
**패턴 라이브러리를 보강**한다. 최종 지향은 "코드화된 규칙 + 예시 라이브러리"를 함께 쓰는 **하이브리드**.
이번 범위는 **도구만** 만들고 **앱(파서/엔진/프론트)은 수정하지 않는다** — 결과(커버리지·갭)를 보고
사용자가 앱 보강 여부를 결정한다.

계약서다. §번호의 파일/포맷/시그니처를 지킨다. 위치: 전부 `/root/projects/HA Automation Maker/tools/corpus/`
(신규). 앱 코드는 **읽기 전용 import만** 허용:
`backend.nl.parser.parse`, `backend.nl.gazetteer.Gazetteer`, `backend.mock_data.MockHAClient`,
`backend.ha_client.merge_inventory`, `backend.engine.rule_model.validate_rule_model`.
출력은 `tools/corpus/out/` 아래에만 쓴다. 사용자 노출 문자열 한국어, 코드 식별자 영어.

앱 파서 계약(이 도구가 소비): `parse(sentence, gazetteer, settings, pins={})` →
`{ok, model, chips, summary, area_id, category, unmatched, confidence, warnings, subrules_count}`.
`model`은 최상위 `triggers/conditions/condition_mode/actions` **또는** `subrules:[{...}]`.

---

## 1. 도구 파이프라인 (5단계)

```
templates.yaml + slots.yaml
      │  generate.py (문법×슬롯 조합, 라벨 자동)
      ▼
corpus(grammar) ─┐
                 ├─ evaluate.py (현재 파서 구동 → 구조 비교 → 정확/부분/실패)
paraphrases.yaml ┤        │
(LLM/에이전트 생성)│        ▼
      │ augment.py   coverage_report.md + results.jsonl
      ▼                    │  mine.py (실패 문장 → 템플릿 추상화 → 갭 클러스터)
corpus(paraphrase)─────────┤        ▼
                           │   gap_library.yaml + pattern_library.yaml
                           ▼
                     run.py → report.md (종합)
```

## 2. 데이터 포맷

### 2.1 slots.yaml — 공유 슬롯 필러 사전 (리서치 B가 시드)
```yaml
# 각 슬롯: 표면형 → (엔티티/값). 표면형은 여러 이형태.
device_light:
  - {surface: "거실 조명", entity: "light.living_room_main", area: living_room}
  - {surface: "거실 불",   entity: "light.living_room_main", area: living_room}
  - {surface: "거실등",    entity: "light.living_room_main", area: living_room}
  - {surface: "안방 조명", entity: "light.master_bedroom",  area: master_bedroom}
motion:
  - {surface: "거실 모션", entity: "binary_sensor.living_room_motion", area: living_room}
mode:
  - {surface: "슬립 모드", value: "슬립 모드"}
  - {surface: "슬립모드",  value: "슬립 모드"}
segment:
  - {surface: "새벽에",   value: "dawn"}
  - {surface: "새벽시간에", value: "dawn"}
percent:
  - {surface: "10%",    value: 10}
  - {surface: "10퍼센트", value: 10}
  - {surface: "10프로",  value: 10}
connective_pair:      # 다중 규칙 경계·액션 연결 이형태
  - {surface: "켜주고",  kind: action_then_boundary}
verb_off:             # 명령형 어미 변이
  - {surface: "꺼줘"}  {surface: "꺼"}  {surface: "꺼주세요"}
```
엔티티는 mock_data 실존 id만 사용(evaluate가 그 인벤토리로 파서를 돌리므로).

### 2.2 templates.yaml — 시드 문법 (라벨 자동)
```yaml
- id: mode_trig_all_off
  area: mode
  template: "{mode}가 켜지면 {scope} {device_light}을 {verb_off}"
  gold:                          # 슬롯 참조로 gold RuleModel 기술
    subrules:
      - triggers: [{type: mode, mode: "{mode.value}", to: on}]
        conditions: []
        actions: [{type: service, action: "light.turn_off",
                   target: {entity_id: "{scope.expand}"}}]
  slots: [mode, scope, device_light, verb_off]
  tags: [mode-trigger, scope-all]

- id: multiclause_motion_dim_then_off
  area: multiclause
  template: "{segment} {mode}이고 {motion}이 작동하면 {device_light}을 {percent} 켜주고 {duration} 동안 모션이 없으면 {verb_off}"
  gold:
    subrules:
      - triggers: [{type: state, entity_id: "{motion.entity}", to: on}]
        conditions: [{type: time_segment, segments: ["{segment.value}"]},
                     {type: mode, mode: "{mode.value}", state: on}]
        actions: [{type: service, action: "light.turn_on",
                   target: {entity_id: ["{device_light.entity}"]},
                   data: {brightness_pct: "{percent.value}"}}]
      - triggers: [{type: state_held, entity_id: "{motion.entity}", to: off,
                    for: "{duration.dur}"}]
        conditions: []
        actions: [{type: service, action: "light.turn_off",
                   target: {entity_id: ["{device_light.entity}"]}}]
  slots: [segment, mode, motion, device_light, percent, duration, verb_off]
  tags: [multiclause, context-inherit, mode-condition]
```
- `{slot}` = 표면형을 문장에 삽입. `{slot.value}`/`{slot.entity}`/`{slot.dur}`/`{scope.expand}` = gold에 값 삽입.
- `{scope.expand}` = "모든/집의 모든" 류 → 해당 도메인 전체 entity_id 리스트로 확장(evaluate가 인벤토리로 계산).
- 최소 시드: mode 영역 8+ 템플릿, multiclause 8+, target(수동매핑 유발 포함) 8+, single(기존 회귀) 6+.

### 2.3 paraphrases.yaml — LLM/에이전트 생성 자연 변형 (라벨 보존)
```yaml
- gold_ref: multiclause_motion_dim_then_off   # 이 템플릿의 gold를 그대로 상속
  variants:
    - "새벽에 슬립모드일 때 거실에서 움직임 감지되면 거실등 10프로로 켜고 3분간 인기척 없으면 꺼줘"
    - "취침모드 켜져 있고 새벽에 거실 모션 뜨면 거실 조명 10% 켰다가 3분 동안 안 움직이면 다시 꺼"
```
- 워크플로우의 Fable 에이전트가 시드 문장을 자연스러운 한국어로 패러프레이즈(의미·라벨 보존)해 채운다.
- augment.py가 dedup + **라벨보존 가드**(variant를 파서에 넣었을 때 최소한 gold의 엔티티가 인벤토리에서
  해석 가능한지, 명백한 의미 드리프트가 없는지 휴리스틱 검사) 후 코퍼스에 편입. gold는 gold_ref에서 상속.

## 3. 스크립트 시그니처

### 3.1 generate.py
```python
def load_templates(path) -> list[dict]
def load_slots(path) -> dict
def expand_template(tpl: dict, slots: dict, inventory: dict, limit_per_template=40) -> list[dict]
    # 슬롯 조합(카테시안, 조합 폭발 방지 위해 템플릿당 상한 샘플링) → CorpusItem 리스트
def generate(templates, slots, inventory, seed=0) -> list[CorpusItem]
# CorpusItem = {"id","sentence","gold": RuleModel(구체 엔티티), "area","template_id","source":"grammar","tags":[...]}
# seed 고정으로 결정적 샘플링(Date/random 미사용 — 순수 인덱스 기반 셔플).
```

### 3.2 augment.py
```python
def load_paraphrases(path, templates) -> list[CorpusItem]   # gold_ref로 gold 상속, source="paraphrase"
def dedup(items) -> list[CorpusItem]                          # 문장 정규화(공백) 후 중복 제거
def label_preservation_ok(item, gazetteer) -> (bool, str)     # 드리프트 가드(엔티티 해석성)
```

### 3.3 structural_match.py — 모델 비교(핵심)
```python
def normalize_model(model: dict) -> list[dict]     # subrules로 평탄화, 각 subrule을 정렬된 표준형으로
def compare(gold: dict, actual: dict) -> dict
    # → {"verdict":"exact"|"partial"|"fail", "diff":[...], "trigger_match":bool,
    #    "subrule_count_match":bool, "cond_match":bool, "action_match":bool}
    # exact = 모든 subrule의 triggers/conditions/actions 핵심필드 집합 일치.
    # partial = triggers는 맞고 일부 조건/액션/개수 불일치.
    # fail = ok=False, 예외, 또는 triggers 불일치.
    # 비교 대상 핵심필드: trigger(type,entity_id,to,for,mode,segments,above,below,event),
    #   condition(type,entity_id,state,segments,mode,types,seasons,after,before),
    #   action(action,target.entity_id정렬,data,type,mode,to,duration). chips/summary/confidence 무시.
```

### 3.4 evaluate.py
```python
def build_inventory() -> (dict, Gazetteer, dict)   # MockHAClient→merge_inventory, settings(모드 포함), gazetteer
def evaluate(corpus, gazetteer, settings, inventory) -> dict
    # 각 item: parse() 호출(예외 캡처) → compare(gold, actual) → 결과 누적.
    # 반환: {"total","by_verdict":{exact,partial,fail}, "by_area":{...}, "by_template":{...},
    #        "items":[{item, verdict, diff, actual_ok, confidence, exception}]}
```
- 결과를 `out/results.jsonl`(item별)과 `out/coverage_report.md`(요약표: 영역별/템플릿별 정확률)로 출력.

### 3.5 mine.py
```python
def abstract_to_template(item, gazetteer) -> str
    # 실패/부분 문장에서 매칭된 엔티티/값/시간/모드 스팬을 슬롯 자리표시자로 역치환 → 추상 템플릿 문자열.
def mine_gaps(eval_result, gazetteer) -> list[dict]
    # 실패·부분 아이템을 추상 템플릿으로 클러스터 → 빈도순 정렬. 각 클러스터:
    # {"pattern": 추상문자열, "count", "area", "examples":[문장..], "sample_diff", "verdict_mix"}
```
- 출력 `out/gap_library.yaml`(추가 후보 패턴, 빈도순) + `out/pattern_library.yaml`
  (시드 템플릿 전체 + 커버 상태[covered/partial/gap] + 예시). 이 pattern_library가 하이브리드의 데이터 자산.

### 3.6 run.py
```python
def main():  # generate → (paraphrases 있으면 augment) → evaluate → mine → report.md 작성
```
- `out/report.md`: 코퍼스 규모(문장 수, 영역별), **현재 파서 커버리지 %**(정확/부분/실패, 영역별·템플릿별),
  상위 갭 패턴 표(빈도·예시·왜 실패하는지 진단), 하이브리드 권고 요약 + gap_library 링크.
- 인자: `--no-augment`(문법만), `--limit N`(템플릿당 상한). 결정적(랜덤 시드 고정).

## 4. 하이브리드 파서 설계 문서 (`docs/HYBRID-PARSER.md`) — 설계만, 앱 미수정
현재(규칙 우선) 위에 예시 라이브러리를 얹는 3단 폴백 권고를 문서화:
1. **규칙 레이어**(현행 parser.py) — 빠르고 결정적. confidence 산출.
2. **템플릿/예시 레이어** — pattern_library.yaml을 로드해 hassil식 표면 템플릿 매칭.
   규칙 confidence가 낮거나 unresolved일 때, 가장 잘 맞는 covered 템플릿의 gold를 슬롯 치환해 후보 생성.
3. **LLM 레이어**(현행 llm_assist) — 위 둘 다 실패 시. pattern_library의 예시를 few-shot으로 주입.
- 예시 라이브러리의 3용도 명시: (a) 회귀 코퍼스(테스트) (b) 런타임 템플릿 매처 (c) LLM few-shot.
- 앱 편입 시 변경점 목록(파서에 템플릿 레이어 삽입 지점, 로더, 우선순위) — 사용자가 나중에 결정.

## 5. 산출물 커밋 정책
- 커밋: templates.yaml, slots.yaml, paraphrases.yaml, *.py, README.md, docs/HYBRID-PARSER.md,
  그리고 스냅샷 `out/report.md` + `out/gap_library.yaml` + `out/pattern_library.yaml`.
- gitignore: `tools/corpus/out/results.jsonl`, `out/corpus_*.jsonl`(재생성 가능한 대용량 덤프).

## 6. 검증 (도구 자체)
- `tests/`(도구 로컬, 앱 tests와 분리 — `tools/corpus/test_tool.py`): generate가 결정적,
  structural_match의 exact/partial/fail 분류가 손수 만든 케이스에서 정확, mine의 추상화가 엔티티→슬롯
  역치환 정확, run.py가 out/ 산출물 생성. 앱 pytest(현재 321)는 건드리지 않음(도구는 별도).
- `python tools/corpus/run.py`가 오류 없이 report.md를 만들고, report에 커버리지 수치가 실제 파서 기준으로 채워짐.

## 7. 리서치 반영 델타 (구현 필수)
문헌·선례(hassil, Chatito, Snips 2단, HA intents 테스트 미러, delexicalization, component matching) 검증 결과:
1. **templates.yaml 문장부에 hassil 인라인 문법 채택**: `[옵션]`, `(대안|대안)`, 공용 `expansion_rules`
   (예: `<off>: "(꺼|꺼줘|꺼주세요)"`). 어미·존대 변이는 슬롯이 아니라 인라인으로 처리해 슬롯 수 절감.
   generate.py가 이 인라인 문법을 전개(대안=곱집합, 옵션=있음/없음 2분기, expansion_rules 치환).
2. **조사 자동 계산**: 치환된 표면형의 받침으로 이/가·을/를을 재계산. `backend.nl.normalize`의
   `josa_i_ga`/`josa_eul_reul` **재사용**. 템플릿 표기: `"{mode}{가} 켜지면 {device_light}{을} <off>"`
   (`{가}`/`{을}`는 앞 슬롯 표면형 기준 조사 토큰).
3. **샘플링 even 분포**: 템플릿당 상한 40 **균등** + seed 고정. regular(조합수 비례)는 커버리지 % 왜곡 금지.
4. **생성 시점 gold 자가검증**: 각 CorpusItem의 gold를 `validate_rule_model(gold, inventory, mode_names)`에
   통과시켜 **템플릿 자체 오류(gold 드리프트)를 파서 평가와 분리**. 실패 gold는 리포트에 `gold_invalid`로
   격리(파서 커버리지 통계에서 제외). 도구 신뢰성의 핵심.
5. **패러프레이즈 가드 = 파서 무관 검사**(중요): (a) **슬롯 앵커링** — gold의 각 슬롯 표면 이형태 중
   하나가 variant 문자열에 존재, (b) **gazetteer 해석성** — gold 엔티티가 인벤토리에서 해석 가능,
   (c) exact + 근접(문자 3-gram Jaccard ≥0.9) dedup. **파서 재파싱은 필터가 아니라 커버리지 신호**로만
   기록(검사기=피시험 파서이면 갭 발견이 무너짐). 리포트는 소스별(grammar/paraphrase) 커버리지 분리 표기.
6. **diff 오류 5분류 태그**: `missing_node` / `extra_node` / `value_mismatch`(above/below/pct/state 등) /
   `wrong_node_type` / `entity_confusion`(다른 엔티티 바인딩). gap_library의 진단열로 사용.
7. **template split 낙관편향 표기**: 문법 생성 문장은 템플릿과 동형이라 커버리지가 낙관적 → 리포트에
   "패러프레이즈 소스만의 커버리지"를 별도 수치로.

## 8. 하이브리드 설계 문서 보강 (HYBRID-PARSER.md에 반영)
- **이론 근거**: Snips `DeterministicIntentParser`→`ProbabilisticIntentParser` 순차(첫 긍정 채택,
  훈련 예시 F1 1.0 보장) = 우리 "규칙 우선" 근거. Rasa `FallbackClassifier`(confidence threshold +
  ambiguity_threshold) = 우리 0.6 게이트 동형. HA Assist "prefer local" 폴백(2024-06).
- **2단 템플릿 매처 알고리즘**: 입력을 delexicalize → pattern_library의 covered 템플릿과 대조 → gold 골격에
  슬롯 바인딩(gazetteer 재사용)해 후보 생성, chips 출처 "패턴 매칭" 표시.
- **3단 LLM few-shot 선택기**: 의존성 0 유지 위해 dense embedding 대신 **어절/문자 n-gram Jaccard kNN**으로
  pattern_library에서 유사 예시 3~5개 선택 → llm_assist 프롬프트에 few-shot 주입(_TOOL 스키마 강제 병용).
- **경고(HA 2025.3 #139415 교훈)**: 폴백 배선은 단계가 순서대로 시도되고 게이트가 지켜지는지
  **단계별 회귀 테스트로 고정**해야 함(LLM에 전부 새는 버그 방지). 앱 편입 시 이 테스트를 함께.

## 9. 공통
- 순수 파이썬 + PyYAML(앱 requirements에 이미 있음). 외부 의존 없음. 결정적(Date/random 시드 고정).
- 앱 파일 **수정 금지**(읽기 전용 import만). 출력은 tools/corpus/out/ 아래에만.
