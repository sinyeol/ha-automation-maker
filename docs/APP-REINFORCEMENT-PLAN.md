# 앱 보강 계획 (하이브리드 편입 + CLI 학습) — Phase 3

전제: tools/corpus 오프라인 프로토타입에서 **held-out 정직 정확도 ≥70% 달성**(측정 확정 후 수치 기입).
이 계획은 그 프로토타입(parser_overlay.py의 A+B 규칙, pattern_match.py, cli_normalize.py)을 실제 앱에
반영하는 것이다. 각 변경은 프로토타입에서 **회귀 0**으로 검증된 것만 이식한다.

## 0. 측정 근거 (프로토타입, held-out 205문장 — 확정)
- **정직 지표(test +L3) = 75.2% ✅** (≥70% 달성). 누적: L1 16.8% → **L1A(A+B 규칙) 69.0%** → +L2 69.0%
  → **+L3 CLI 75.2%**. 전체 held-out 기준 L1 14.6% → L1A 64.9% → +L3 73.2%. **회귀 0**, net_lift 120,
  solved_by rule 30 + ruleA 103 + cli 17.
- 난이도별(test+L3): easy 87.5% · medium 87.0% · hard 54.8%.
- 즉 **정직 70%의 대부분이 결정적 규칙(A+B)에서 나온다**(103/120). 매처·CLI는 롱테일 보완 + 학습.

## 1. Phase 3A — 규칙 이식 (parser.py / gazetteer.py / normalize.py)
parser_overlay.py의 monkeypatch를 실제 함수/사전에 반영. 그룹별:

### A그룹 (라운드1, 이미 실측)
- A1 gazetteer: MODE_SYNONYMS(취침/수면/자기전 → 슬립 모드) — mode_surfaces 빌드에 동의어 오버레이.
- A6 gazetteer.DEVICE_CONCEPTS: 무드등/메인등/천장등/[방]등 → light(label 매칭).
- A2 parser._detect_mode: 해제|취소|종료|풀리|풀려|해지 → mode trigger off.
- A4 normalize/parser duration: `동안`→`(?:동안|간)`.
- A5 parser.VERB_STEMS: 잡히 등 어간 추가.
- A8 parser._emit_time_aspect: 세그먼트 트리거 승격을 "세그먼트어+되면/전환"으로 한정.
- A3/A9/A10 parser._build_action: 전부/다 스코프, 모드off 동사(취소/풀/해지/종료), 기기어 없는 전량 off.

### B그룹 (라운드2, 실측 회귀 0)
- B1 zone 귀가/외출: 집에 오면/도착/귀가/퇴근/들어오→zone enter; 나가면/외출/집 비우/아무도 없으→leave;
  person 매칭(나/와이프), 기본 person.user. (parser 트리거 빌더 + gazetteer person)
- B2 value: 절반→50, 최대/제일 밝게→100, 은은/약하게→저값, 단위없는 N; 도메인별 데이터 키.
- B3 climate-set: "N도로 맞춰/설정" → climate.set_temperature.
- B4 time-range: "A부터 B까지/사이" → time after&before; 요일 → weekday/day_type; markerless 시각 daily.
- B5 scope-exclude: "X 빼고/제외/남기고" → 도메인 전체 중 area X 제외.
- B6 safety: 가스밸브/누수/연기 개념 + valve/switch off/notify.
- B7 media: TV/tv → media_player, volume_set.
- enabler: 모드 트리거 어미 확장, 온도 리터럴 추론, numeric 트리거 방 전파, COMMAND_HINTS 확장.

이식 방식: parser_overlay.py의 각 패치를 대응 함수에 **직접 반영**(monkeypatch 아님). 소유권 분할로 병렬 구현.
검증: 앱 pytest(현재 321) + parser_overlay가 통과시킨 held-out 케이스를 앱 tests에 회귀로 추가. **기존 321 회귀 0**.

## 2. Phase 3B — L2 템플릿 매처 앱 레이어
- backend/nl/pattern_match.py 신규(프로토타입 이식): delexicalize + 인덱스 + 2단 매칭 + 슬롯 바인딩 + 3게이트.
- pattern_library는 빌드시 생성(A+B 반영 covered) — 애드온 이미지에 정적 포함(read-only).
- api_v2 parse 배선: 규칙(L1) needs_help(not ok 또는 conf<0.6·unresolved) → 매처(L2). 매처는 L1 결과가
  not ok일 때만 채택(오탐 억제, 실측). chips 출처 "패턴 매칭" 표시(프론트 무수정 — sublabel 렌더 재사용).
- 폴백 단계별 회귀 테스트(HA 2025.3 #139415 교훈): L1→L2 순서·게이트가 지켜지는지 고정.

## 3. Phase 3C — CLI 학습 기능 (사용자 선택 옵션, 새 기능)
사용자 요청: 파서가 못 읽는 문장 → Claude CLI가 라이브러리가 아는 패턴으로 변환 → 라이브러리에 학습.
- backend/nl/cli_normalize.py 신규(프로토타입 이식): claude 정규화 프롬프트(--json-schema {normalized,
  matched_template_id, changed, confidence, notes}, 새 기기/방/모드 추가 금지), few-shot=pattern_library 유사예시.
- 흐름: 미해석(L1+L2 실패) + 사용자가 "AI로 분석해서 배우기" 선택 → CLI 정규화 → 정규형을 L1+L2로 재파싱 →
  ok면 (원문→정규형→gold) 미리보기 → 사용자 승인 → /data/learned_patterns.yaml append.
- 이후 같은 원문/유사문은 learned로 로컬 처리(CLI 없이 — 자기학습 복리).
- 안전: 엔티티 실존 검증(llm_assist._validate_entities), 환각 방어(정규형 엔티티 ⊆ 원문), 서비스 도메인
  화이트리스트, 저장 전 사용자 확인, 학습항목 설정에서 삭제. 재기동 시 learned 엔티티 재검증.
- 설정: 학습 기능 on/off + 기존 llm_backend(off/api/cli) 재사용. UI: 미해석 카드에 "AI로 분석해서 배우기" 버튼.
- API: POST api/v2/learn {sentence} → {normalized, model, preview}; POST api/v2/learn/confirm → learned 저장.

## 4. 실행 순서·검증
1. 3A 규칙 이식(Opus, 파서/게이트/노멀라이즈 분담) + 앱 회귀 테스트 → 앱 자체 정확도 상승 확인(도구 재측정).
2. 3B 매처 레이어(Opus) + 배선 + 폴백 회귀.
3. 3C CLI 학습(Opus) + UI(Opus) + 설정.
4. 다차원 리뷰 + 적대적 검증(Fable) + 수정.
5. E2E(문장→미해석→AI분석→학습→재사용 폐루프) + Docker + 커밋.
- **원칙**: 각 이식은 프로토타입에서 회귀 0인 것만. 앱 pytest 321 유지 + 신규 회귀. 사용자 선택 옵션은 기본 off.

## 5. 리스크
- A6 bare "등" 1글자 표면형 오매칭 여지 → "[room]등" 앵커링으로 좁힘.
- 매처 오탐 → L1 not ok 게이트 + 3게이트 유지.
- CLI 비결정성·비용 → 학습 캐시로 복리 절감, 기본 off, 사용자 확인.
- learned.yaml 성장 관리 → hits 카운트로 저가치 정리, 설정 삭제 UI.
