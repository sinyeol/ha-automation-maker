# SPEC-ACCURACY — 정확도 70% 연구 + 하이브리드 프로토타입 + CLI 학습 (앱 미수정)

사용자 요청: 레퍼런스 리서치로 케이스를 더 모으고, 라이브러리 도구로 문법·문장 데이터를 키워
**정확도를 획기적으로(≥70%) 올리는 방법**을 연구. **앱 수정 없이**(tools/corpus/ 안에서 프로토타입).
70% 돌파 시 → 앱 보강 계획 + 하이브리드 구현 + CLI 학습 기능. 미달 시 → 다각적 연구로 재도전.

## 0. 방법론 원칙 (정직성)
- **정직한 정확도 = held-out 실사용 문장에 대한 exact 비율**. 라이브러리(템플릿/패턴) 구축에 쓴 문장으로
  평가하지 않는다(teaching-to-test 금지). train/test 분리.
- 3층 누적 리프트 측정: **L1 규칙(현행 파서) → L2 템플릿 매처 → L3 CLI/LLM 정규화**. 각 층 추가 시
  held-out 정확도가 얼마나 오르는지.
- 문법 생성(템플릿 동형) 문장은 낙관적 → 헤드라인 숫자는 **자연 패러프레이즈 held-out** 기준.

## 1. 이번 턴 범위 (앱 미수정, tools/corpus/ 만)
### 1.1 코퍼스 대량 확장
- slots.yaml/templates.yaml/paraphrases.yaml 확장 + 신규 `heldout.yaml`(평가 전용, 라이브러리에 미편입).
- 리서치 수집 케이스(100+)와 gap_library(340 클러스터)를 재료로. 신규 영역(numeric/time-range/cover/
  climate-set/media/safety/scope-exclude) 포함. 각 문장 gold 라벨(손/검증).
- train(라이브러리): templates + gap 패턴 covered화. test(held-out): 신규 어순·조합 실사용 문장(라이브러리
  미노출). 최소 held-out 200문장 목표.

### 1.2 하이브리드 프로토타입 (tools/corpus/hybrid.py)
- L2 템플릿 매처: 입력 delexicalize(gazetteer.match + normalize find_*) → pattern_library covered 템플릿과
  대조(완전일치 우선 + 순서보존 유사도 임계값) → 매칭 gold 골격에 슬롯 바인딩 → 후보 model.
- L3 CLI 정규화(프로토타입): 파서/매처 실패 문장 → Claude CLI(구독, 이 컨테이너 로그인)로 "라이브러리가 아는
  정규 문형으로 재작성" → 재파싱. (backend.nl.cli_client 재사용, 읽기 전용.)
- evaluate_hybrid.py: held-out에 L1 / L1+L2 / L1+L2+L3 정확도 누적 측정 → out/hybrid_report.md.

### 1.3 반복
- L1+L2가 70% 미만이면: 갭 분류(A/B/C)의 A(규칙성 패턴)를 **템플릿 매처가 흡수하도록** 패턴 추가 +
  매처 개선(퍼지·정규화 사전) + L3 CLI. 70% 도달 경로를 실측으로 탐색. 결과 out/hybrid_report.md에 기록.

### 1.4 산출물
- 확장된 templates/slots/paraphrases + heldout.yaml, hybrid.py, evaluate_hybrid.py,
  out/hybrid_report.md(L1/L2/L3 누적 정확도, 영역·난이도별, 70% 도달 여부), 갱신된 gap_library/pattern_library.
- **앱 파일 수정 금지**(읽기 전용 import). 커밋은 tools/corpus/ + docs.

## 2. 다음 턴 범위 (70% 돌파 시 — 앱 보강 계획)
### 2.1 하이브리드 파서 앱 편입 (HYBRID-PARSER.md 설계 구현)
- parser.py 뒤에 L2 템플릿 매처 레이어 삽입(pattern_library 로더, confidence 게이트), api_v2 parse 배선,
  폴백 단계별 회귀 테스트(HA 2025.3 #139415 교훈).
### 2.2 CLI 학습 기능 (사용자 선택 옵션) — 새 앱 기능
- 파서가 못 읽는(unresolved/저confidence) 문장에 대해, 사용자가 "AI로 분석" 선택 시:
  Claude CLI가 **라이브러리가 아는 패턴/정규 문형으로 변환** → 파서/매처가 처리 → 성공 시 사용자 확인 후
  (원문 → 정규형 → gold)를 `/data/learned_patterns.yaml`에 append(런타임 학습 라이브러리).
- 이후 같은 유형 문장은 학습된 패턴으로 로컬 처리(CLI 없이). UI: 미해석 시 "AI로 분석해서 배우기" 버튼.
- 안전: 엔티티 실존 검증, 저장 전 사용자 확인, 서비스 도메인 화이트리스트(기존), 학습 항목 관리(설정에서 삭제).
- 설정 옵션: 학습 기능 on/off, CLI 백엔드(기존 llm_backend) 재사용.

## 2.5 A그룹 규칙 수정 목록 (리서치 실측 확정 — 오프라인 오버레이 + 이후 앱 반영)
전부 결정적 규칙, 실측 회귀 0. 오프라인은 tools/corpus/parser_overlay.py 가 monkeypatch로 적용(앱 미수정),
Phase 3에서 실제 파일에 반영.
- **A1** 모드 동의어: gazetteer mode_surfaces에 취침 모드/취침모드/취침 → "슬립 모드" 오버레이 (단독 +14.9%p, 최대)
- **A6** 조명 접미사: DEVICE_CONCEPTS에 무드등→{light,label:무드등}, 메인등→{light,label:메인등},
  "[방]등"/접미사 등→light (label 이름매칭 보너스로 정확 엔티티) (+9.6%p)
- **A4** 지속시간: duration p1 정규식 `동안`→`(?:동안|간)` ("5분간") (+3.4%p)
- **A2** 모드 극성 트리거: _detect_mode에서 켜지 체크 앞에 `해제|취소|종료|풀리|풀려|해지`→trigger off (+2.0%p)
- **A8** 세그먼트 오승격 차단: _emit_time_aspect가 세그먼트를 트리거로 올리는 조건을 "세그먼트 단어+되면/전환"
  으로 한정("저녁에…감지되면"의 오승격 차단) (+1.5%p)
- **A3** 스코프 확장: _split_targets/_emit_service에서 "모든"뿐 아니라 선두 "전부/다"도 스코프
- **A5** 어간: VERB_STEMS에 "잡히" 추가("잡히면" 절 경계)
- **A9** 모드 off 액션: _build_action 모드 분기에 취소|풀|해지|종료 → set_mode off
- **A10** 기기어 없는 전량: "다 꺼/전부 꺼" → 조명 전체 off
- **코퍼스 gold 버그(도구 교정, 앱 아님)**: generate.py의 `{scope.expand}`가 **문맥 도메인(light) 전체**로
  전개되도록 교정(현재 binary_sensor로 전개돼 86문장이 영구 partial). 교정 시 측정 정직화 +8%p.

## 2.6 프로토타입 인터페이스 (tools/corpus/, 앱 미수정)
- `parser_overlay.py`: `parse_patched(sentence, gz, settings, pins={})` — A그룹을 임시 monkeypatch 후 앱 parse
  호출, 복원(결정적). `build_overlay_gazetteer(inventory, settings)` — 모드/조명 사전 확장 gazetteer.
- `pattern_match.py`: `class TemplateMatcher(pattern_library, gazetteer, inventory)` ·
  `match(sentence) -> {model, matched_id, score, mode:"slot_fill"|"struct_replace"} | None`
  (delexicalize→인덱스 2단 매칭[exact 골격 + 순서보존 유사도 τ], 오탐 3게이트: 마진·gap템플릿제외·구조태그부분집합).
- `cli_normalize.py`: `normalize(sentence, few_shot, inventory) -> {normalized, matched_id, changed} | None`
  (claude 정규화 프롬프트 --json-schema, out/cli_cache.jsonl 캐시로 결정적 재현).
- `evaluate_hybrid.py`: held-out에 **L1(앱 parse) → L1+A(overlay) → +L2(matcher, needs_help시) → +L3(cli)** 누적
  측정, 각 단계 exact%·순리프트(이득−회귀)·source×area×difficulty·solved_by 귀속 → out/hybrid_report.md.
  paraphrase held-out(dev/test hash 분할, τ는 dev에서만) exact%가 **정직 70% 지표**.

## 3. 공통
- 순수 파이썬 + PyYAML. 결정적(시드 고정). CLI는 이 개발 컨테이너 Max 구독으로 실측 가능.
- 리서치·수치는 실측 근거. gold는 mock_data 실존 엔티티. 앱 미수정 원칙 엄수(이번 턴).
