# HA Automation Maker — 기획서

> Home Assistant에서 방별·센서 카테고리별로 엔티티를 탐색하며 트리거/조건/액션/딜레이를
> 조합해 자동화를 만드는 **UI 애드온**. 한국 사용자 UX(한글 초성 검색, 거실/안방 프리셋,
> SmartThings식 용어)를 기본으로 한다.

## 1. 리서치 요약 (2026-07, 공식 문서 원문 검증 완료)

### 1.1 아키텍처 제약 (확정)
- Ingress iframe 안의 프론트엔드는 HA 토큰을 얻을 수 없다 → **백엔드 릴레이 필수**:
  `프론트(상대경로 REST) → 애드온 백엔드 → SUPERVISOR_TOKEN → http://supervisor/core/api`
- Ingress의 WebSocket 릴레이는 미해결 안정성 이슈(core #93619)가 있음 → 프론트↔백엔드는
  **REST 전용**으로 설계. 레지스트리 조회용 백엔드↔Core WS(`ws://supervisor/core/websocket`)는
  제한 없음(인증: auth_required → auth with SUPERVISOR_TOKEN).
- Supervisor 프록시 경유 요청은 Core에서 **admin 권한**으로 실행됨 → 백엔드는 반드시
  ingress 게이트웨이 IP `172.30.32.2`만 허용해야 한다.
- 권한은 `homeassistant_api: true` 하나만 필요. `hassio_api`는 불필요(최소 권한).
- Ingress UI는 사실상 admin 사용자 전용(비관리자 진입 불가 이슈 존재) → 접근제어 단순화.

### 1.2 패키징 (2026 최신 사양)
- `build.yaml` **폐기됨**. Supervisor 2026.04.0부터 `BUILD_FROM` 자동 주입도 중단 →
  Dockerfile에 `FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24` 직접 명시(버전 고정).
- `arch: [aarch64, amd64]` (armhf/armv7/i386은 문서에서 제거됨).
- 베이스 이미지는 S6-overlay V3 포함 → `init: false` **필수**.
- `run.sh` 셔뱅: `#!/usr/bin/with-contenv bashio` (LF 필수).
- Ingress: 포트 8099 리슨, `172.30.32.2`만 allow, 상대경로 필수, 베이스 URL은
  `X-Ingress-Path` 헤더(우리는 전부 상대경로라 불필요).
- 로컬 설치: `/addons/automation_maker/`에 폴더 복사 → 스토어 새로고침 → Local 저장소.

### 1.3 HA API 함정 (확정)
- `config/entity_registry/list` 응답에는 **device_class가 없다** →
  `GET /api/states`의 `attributes.device_class`와 병합해야 한다.
- `POST /api/config/automation/config/{id}`는 **경고 없는 upsert** →
  신규 id는 `uuid4().hex`, 저장 직전 `GET .../{id}`가 404인지 확인.
  본문에 id를 넣지 않는다(URL의 id가 강제 주입됨). 저장 시 자동 reload됨.
- 기존 자동화 id 수집: `GET /api/states`에서 `automation.*` 엔티티의 `attributes.id`.
  `attributes.id`가 없는 자동화는 YAML 수동 관리형 → **편집 불가 배지** 처리.
- 자동화 삭제: `DELETE /api/config/automation/config/{id}`.
- on/off/수동실행: `POST /api/services/automation/turn_on|turn_off|trigger`.

### 1.4 자동화 YAML 문법 (2024.10+ 신문법으로 생성)
- 최상위 키 복수형: `triggers:` / `conditions:` / `actions:`, 트리거 항목은 `- trigger: state`.
- 액션 호출은 `action: light.turn_on` + `target:` + `data:` (2024.8 `service:`→`action:`).
- delay 형식: `"HH:MM:SS"` 문자열로 통일 생성. mode: single/restart/queued/parallel + max.

### 1.5 UX 벤치마킹 결론
- **3층 구조**: ① Apple식 트리거 카테고리 위저드로 진입(빈 캔버스 공포 제거) →
  ② SmartThings식 단일 화면 카드 스택 편집(HA 어휘 유지: `~할 때`/`~인 동안`/`실행`) →
  ③ 저장 전 자연어 요약 + YAML 미리보기.
- **엔티티 탐색**: 방(area) 그리드(엔티티 수 배지) → 카테고리 칩(조명/감지기/센서/공조…)
  → 엔티티 목록(현재 상태 + 소속 기기·방 병기). HA 2025.11 신형 target picker 수준이 기준선.
- **논리 명시**: 트리거 여러 개 = "이 중 하나라도"(OR) 고정 문구, 조건은
  "모든 조건 충족(AND) / 하나라도 충족(OR)" 세그먼트 토글.
- **한국 특화**: 방 이름 프리셋(거실·안방·작은방·주방·현관·베란다·욕실), 한글 부분일치
  + 초성 검색(ㄱㅅ→거실), 12시간제 + 평일/주말 프리셋, 가스밸브 등 안전 액션 확인 다이얼로그,
  월패드(RS485) 연동 환경 고려(난방=climate, 환기=fan, 가스밸브=switch/valve).

## 2. 제품 범위 (v1)

| 영역 | 포함 | 제외(v2 후보) |
|---|---|---|
| 트리거 | state, numeric_state, time, time_pattern, sun, zone, template, homeassistant | device, mqtt, webhook, event, calendar |
| 조건 | state, numeric_state, time, sun, zone, template, trigger, and/or/not | device |
| 액션 | 서비스 호출, delay, wait_template, wait_for_trigger, 중간 condition, choose, if-then-else, repeat, parallel, stop | variables, event 발행 |
| 관리 | 목록/생성/수정/삭제, on/off, 수동 실행, YAML 미리보기 | 트레이스 뷰, 블루프린트 |
| 탐색 | 방→카테고리→엔티티, 검색(초성), 상태 표시 | floor/label 계층 |

## 3. 작업 순서 (모델 배분: 사용자 지시 반영)

| 단계 | 내용 | 담당 | 상태 |
|---|---|---|---|
| 1 | 리서치 (패키징/API/스키마/UX 4방향 + 갭 체크) | Fable ×5 | ✅ 완료 |
| 2 | 기획서(PLAN.md) + 구현 계약(docs/SPEC.md) | Fable | ✅ 이 문서 |
| 3 | 구현: 백엔드/프론트엔드/테스트 | **Opus** ×3 | ✅ 완료 |
| 4 | 구현: 패키징/문서 | **Sonnet** ×2 | ✅ 완료 |
| 5 | 로컬 검증: pytest 126개 + dev 서버 + JS 문법 검사 | Fable | ✅ 완료 |
| 6 | 다차원 코드리뷰(6관점) + 적대적 검증(2인 반박제) + 확정 결함 26건 수정 | Fable 워크플로우 + Opus | ✅ 완료 |
| 7 | Docker 빌드 + 컨테이너 기동 + Playwright E2E 4종 | Fable | ✅ 완료 |
| 8 | 데모 아티팩트(목데이터 인터랙티브 UI) + 최종 보고 | Fable | ✅ 완료 |

## 3.5 v2 — 자체 엔진 + 자연어 (2026-07-16 방향 전환)

사용자 지시: HA automations.yaml 대신 **애드온 자체 규칙 엔진**(Fibaro Event Runner 스타일)으로 실행,
**한국어 자연어 문장**으로 규칙 생성. 계약: [docs/SPEC-V2.md](docs/SPEC-V2.md).

리서치 결론(전문은 리서치 워크플로우 로그): EventRunner의 트리거 자동추출·trueFor·시간대 변수 이식 /
subscribe_events+재연결·/data 영속화·holidays(KR) 확정 / 형태소 분석기 불필요(마지막 '-면' 경계 절분리
+ 사전검증 조사 스트리핑) / 해석 확인 카드(칩 3상태)가 UX 차별점.

| 단계 | 내용 | 담당 | 상태 |
|---|---|---|---|
| v2-1 | 리서치 4방향 (EventRunner/엔진기술/한국어NL/NL UX) | Fable ×4 | ✅ |
| v2-2 | SPEC-V2 계약 작성 | Fable | ✅ |
| v2-3 | 구현: 엔진/NL파서/API/프론트/테스트/통합 | **Opus** ×6 | ✅ |
| v2-4 | 문서 갱신 | **Sonnet** | ✅ |
| v2-5 | 검증: pytest 242 + NL 골든 + E2E 폐루프(문장→저장→모의 이벤트→발화) | Fable | ✅ |
| v2-6 | 다차원 리뷰 + 적대적/직접 검증 + 확정 결함 19건 수정 | Fable + Opus | ✅ |
| v2-7 | 커밋/푸시(290b181) + 보고 (데모 아티팩트는 v1 유지 — 파서/엔진 JS 포팅 필요) | Fable | ✅ |

## 4. 저장소 구조

```
HA Automation Maker/
├── PLAN.md                  # 이 문서
├── README.md                # 저장소 소개 (설치 방법)
├── repository.yaml          # HA 애드온 저장소 메타 (GitHub 설치용)
├── docs/SPEC.md             # 구현 계약 (API/모델/파일 배치)
└── automation_maker/        # ← /addons 에 복사되는 애드온 본체
    ├── config.yaml          # 애드온 매니페스트 (ingress, homeassistant_api)
    ├── Dockerfile           # FROM base-python 직접 명시 (build.yaml 없음)
    ├── run.sh               # with-contenv bashio
    ├── README.md / DOCS.md / CHANGELOG.md
    ├── translations/ko.yaml, en.yaml
    ├── backend/
    │   ├── app.py               # aiohttp 서버 (정적 + REST, ingress IP 가드)
    │   ├── ha_client.py         # supervisor 프록시 클라이언트 (REST + WS 레지스트리)
    │   ├── automation_builder.py# UI 모델 → HA 자동화 config 변환·검증
    │   ├── mock_data.py         # DEV_MODE용 한국 가정 목데이터
    │   └── requirements.txt     # aiohttp, PyYAML
    ├── frontend/                # 빌드 불필요 vanilla ES modules
    │   ├── index.html
    │   ├── css/styles.css
    │   └── js/ (app, api, store, views/, components/)
    └── tests/                   # pytest (builder 골든 테스트, API, 보안, 병합)
```
