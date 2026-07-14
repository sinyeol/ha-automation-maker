# SPEC — 구현 계약서

모든 구현 에이전트는 이 문서를 **계약**으로 삼는다. 여기 정의된 파일 경로, 함수 시그니처,
JSON 형태, 엔드포인트를 정확히 지켜야 서로 다른 에이전트의 산출물이 맞물린다.
UI 문자열은 한국어, 식별자/코드는 영어. 외부 CDN 사용 금지(전부 로컬 파일).

루트: `/root/projects/HA Automation Maker/` (이하 `$ROOT`)

---

## 1. 패키징 (`$ROOT/automation_maker/`)

### config.yaml
```yaml
name: "HA Automation Maker"
version: "1.0.0"
slug: automation_maker
description: "방별·센서별 엔티티 탐색으로 자동화를 쉽게 만드는 UI (Korean-first automation builder)"
url: "https://github.com/sinyeol/ha-automation-maker"
arch: [aarch64, amd64]
init: false
ingress: true
ingress_port: 8099
panel_icon: mdi:robot-happy
panel_title: "자동화 메이커"
homeassistant_api: true
startup: application
boot: auto
options: {}
schema: {}
```
- `hassio_api`, `auth_api`, `ports`, `map`은 **넣지 않는다** (최소 권한).
- `build.yaml`을 만들지 않는다 (폐기됨).

### Dockerfile
```dockerfile
FROM ghcr.io/home-assistant/base-python:3.14-alpine3.24
WORKDIR /app
COPY backend/requirements.txt ./backend/
RUN pip3 install --no-cache-dir -r backend/requirements.txt
COPY backend ./backend
COPY frontend ./frontend
COPY run.sh /
RUN chmod a+x /run.sh
CMD ["/run.sh"]
LABEL io.hass.type="app" io.hass.version="1.0.0" io.hass.arch="aarch64|amd64"
```

### run.sh (LF 줄바꿈)
```bash
#!/usr/bin/with-contenv bashio
bashio::log.info "Starting HA Automation Maker..."
cd /app || bashio::exit.nok "app dir missing"
exec python3 -m backend.app
```

### 기타
- `translations/en.yaml`, `translations/ko.yaml`: `configuration: {}` 수준의 최소 파일.
- `$ROOT/repository.yaml`: `name: "HA Automation Maker Add-ons"` + `maintainer`.
- 애드온 `README.md`(짧은 소개), `DOCS.md`(사용법 상세, 한국어), `CHANGELOG.md`(1.0.0).
- `$ROOT/README.md`: 저장소 소개 + 로컬(/addons 복사) 및 GitHub 저장소 설치법 + 스크린샷 자리.
- `$ROOT/.gitignore`: `__pycache__/`, `*.pyc`, `.pytest_cache/`, `venv/`.

---

## 2. 백엔드 (`$ROOT/automation_maker/backend/`) — Python 3.11+, aiohttp + PyYAML만 사용

### 2.1 requirements.txt
```
aiohttp>=3.12,<4
PyYAML>=6,<7
```

### 2.2 모듈 구조와 시그니처 (정확히 준수)

**`backend/app.py`**
```python
def create_app(ha: "HAClient") -> aiohttp.web.Application: ...
def main() -> None:  # python3 -m backend.app 진입점
    # DEV_MODE 환경변수("1"이면) → MockHAClient, 아니면 HAClient
    # PORT 환경변수(기본 8099), host 0.0.0.0
```
- 미들웨어 `ingress_guard`: `request.remote`가 `172.30.32.2` 또는 `127.0.0.1`이 아니면 403.
  DEV_MODE에서는 검사 생략. (프록시 뒤 remote 판별: `request.remote` 사용)
- 정적 서빙: `/` → `frontend/index.html`, `/css/...`, `/js/...` (aiohttp static routes,
  `../frontend` 상대 경로는 `Path(__file__).parent.parent / "frontend"`로 해석).
- 캐시 헤더: 정적 파일 `Cache-Control: no-cache` (애드온 업데이트 반영 보장).
- 모든 API 응답은 JSON, 오류는 `{"error": {"code": str, "message": str(한국어)}}` +
  적절한 HTTP status. 예외 핸들링 미들웨어 1개.

**`backend/ha_client.py`**
```python
class HAClient:
    def __init__(self, base_url: str = "http://supervisor/core", token: str | None = None): ...
    async def start(self) -> None: ...   # aiohttp ClientSession 생성
    async def close(self) -> None: ...
    async def get_states(self) -> list[dict]: ...                 # GET {base}/api/states
    async def fetch_registries(self) -> dict: ...
        # ws://{base}/websocket 접속 → auth_required 수신 → {"type":"auth","access_token":token}
        # → auth_ok → config/area_registry/list, config/device_registry/list,
        #   config/entity_registry/list 3개 명령(증가 id) → 연결 종료
        # 반환: {"areas": [...], "devices": [...], "entities": [...]}
    async def get_automation_config(self, automation_id: str) -> dict | None: ...
        # GET {base}/api/config/automation/config/{id} → 404면 None
    async def upsert_automation(self, automation_id: str, config: dict) -> None: ...
        # POST 같은 경로, 본문에 id 필드 제거 후 전송
    async def delete_automation(self, automation_id: str) -> None: ...
    async def call_service(self, domain: str, service: str, data: dict) -> None: ...
        # POST {base}/api/services/{domain}/{service}

def merge_inventory(areas: list, devices: list, entities: list, states: list) -> dict:
    # 순수 함수 (테스트 대상). 반환 형태는 §3.1 bootstrap 응답의 "areas"/"entities".
    # 규칙:
    # - entity area 결정: entity.area_id 우선, 없으면 소속 device의 area_id
    # - name 결정: states의 friendly_name > entity.name > entity.original_name > entity_id
    # - device_class: states의 attributes.device_class (레지스트리에는 없음!)
    # - 제외: disabled_by가 truthy, entity_category가 "diagnostic"/"config",
    #        hidden_by가 truthy, 도메인이 EXCLUDED_DOMAINS(automation, update, tts, stt,
    #        conversation, assist_satellite, zone, persistent_notification)
    # - states에 없는 entity_id는 state=None으로 포함(unavailable 아님)
    # - area 미배정 엔티티는 area_id=None → 프론트에서 "미배정" 그룹
```

**`backend/mock_data.py`** — DEV_MODE용. `class MockHAClient` (HAClient와 동일 인터페이스,
저장은 인메모리 dict). 목데이터는 한국 아파트 가정:
- areas: 거실, 안방, 작은방, 주방, 현관, 베란다, 욕실 (7개, area_id는 영문 slug)
- entities 최소 28개: 방별 조명(light), 거실 모션(binary_sensor/motion), 현관문
  (binary_sensor/door), 방별 온도·습도(sensor/temperature·humidity), 거실 TV(media_player),
  방별 보일러(climate), 전열교환기(fan), 가스밸브(switch, "가스밸브"), 대기전력 콘센트(switch),
  커튼(cover), 욕실 누수(binary_sensor/moisture), person.user("나"), 미배정 엔티티 2개
- 기존 자동화 3개(그중 1개는 attributes.id 없는 YAML 관리형)

**`backend/automation_builder.py`** — 순수 함수만, aiohttp 의존 금지.
```python
class ValidationError(Exception):
    def __init__(self, errors: list[dict]): ...  # [{"path": "triggers[0].entity_id", "message": "..."}]

def validate_model(model: dict) -> list[dict]: ...   # 오류 리스트(빈 리스트면 유효), 한국어 메시지
def build_automation(model: dict) -> dict: ...
    # UI 모델(§4) → HA 자동화 config(신문법). 유효하지 않으면 ValidationError.
    # 출력 키 순서: alias, description, mode, (max), triggers, conditions, actions
def to_yaml(config: dict) -> str: ...
    # PyYAML dump: allow_unicode=True, sort_keys=False, default_flow_style=False
def summarize(model: dict, entity_names: dict[str, str]) -> str: ...
    # 한국어 자연어 요약 1~3문장. entity_names: entity_id → 표시명
```

### 2.3 변환 규칙 (build_automation)
- 신문법으로만 생성: `triggers:[{"trigger": "state", ...}]`, `actions` 내 서비스 호출은
  `{"action": "light.turn_on", "target": {...}, "data": {...}}`. 빈 `data`/`target`은 생략.
- Duration 객체 `{"hours":h,"minutes":m,"seconds":s}` → `"HH:MM:SS"` 문자열(2자리 패딩).
  모두 0이면 해당 필드 자체를 생략.
- `condition_mode == "or"`이고 conditions가 2개 이상이면 전체를
  `[{"condition":"or","conditions":[...]}]`로 감싼다. "and"는 평탄한 리스트 그대로.
- 트리거가 여러 개면 각각 `id`를 부여하지 않는다(v1은 trigger 조건에서 인덱스 문자열 사용).
- `mode`가 queued/parallel일 때만 `max`(기본 10) 포함 허용.
- 검증 예: state 트리거에 entity_id 필수, numeric_state는 above/below 중 1개 이상,
  time.at은 `HH:MM` 또는 `HH:MM:SS` 형식, zone은 entity_id+zone 필수, delay는 duration 필수,
  choose는 options 1개 이상, repeat.count는 1 이상 정수, alias는 공백 제거 후 1자 이상.

### 2.4 REST API (모두 `api/` 프리픽스, 상대경로)

| 메서드/경로 | 동작 | 응답(200) |
|---|---|---|
| GET `api/health` | 상태 확인 | `{"ok":true,"mode":"ha"\|"dev","version":"1.0.0"}` |
| GET `api/bootstrap` | 전체 인벤토리 | §3.1 |
| GET `api/automations` | 자동화 목록 | `{"automations":[§3.2]}` |
| GET `api/automations/{id}` | 단건 config 조회 | `{"id":..., "config": {...HA config...}}` (404 가능) |
| POST `api/automations` | 생성. body=`{"model": §4}` | `{"id": "...", "yaml": "..."}` |
| PUT `api/automations/{id}` | 수정. body=`{"model": §4}` | `{"id": "...", "yaml": "..."}` |
| DELETE `api/automations/{id}` | 삭제 | `{"ok":true}` |
| POST `api/automations/{id}/toggle` | body=`{"on": bool}` → automation.turn_on/off | `{"ok":true}` |
| POST `api/automations/{id}/run` | automation.trigger 호출 | `{"ok":true}` |
| POST `api/preview` | body=`{"model": §4}` → 저장 없이 변환 | `{"yaml":"...","summary":"...","errors":[...]}` |

- 생성 시: `id = uuid4().hex` → `get_automation_config(id)`가 None일 때까지 재생성(최대 3회)
  → `upsert_automation`. 응답의 `yaml`은 `to_yaml(config)`.
- 수정 시: URL의 기존 id 재사용. 대상이 없으면(404) `{"error":{"code":"not_found",...}}`.
- POST `api/preview`는 검증 실패여도 200으로 `errors`를 채워 반환(yaml은 빈 문자열).
- 나머지 엔드포인트의 검증 실패는 400 + `{"error":{"code":"invalid_model","errors":[...]}}`.

## 3. 데이터 형태

### 3.1 GET api/bootstrap 응답
```json
{
  "mode": "dev",
  "areas": [{"area_id":"living_room","name":"거실","icon":"mdi:sofa"}],
  "entities": [{
    "entity_id":"light.living_room_main","domain":"light","name":"거실 메인등",
    "area_id":"living_room","area_name":"거실",
    "device_id":"dev1","device_name":"월패드 조명","device_class":null,
    "state":"off","unit":null,"attributes":{"brightness":null}
  }],
  "services": {"light":["turn_on","turn_off","toggle"], "...": []},
  "automations": [§3.2]
}
```
- `attributes`는 UI에 필요한 것만 축약: brightness, temperature(climate 현재 설정),
  current_temperature, percentage(fan), supported_features는 **제외**(v1 미사용).
- `services`는 하드코딩 상수 `KNOWN_SERVICES` (백엔드 automation_builder.py에 정의):
  light/switch/fan/cover/climate/media_player/lock/valve/scene/script/vacuum/
  humidifier/button/input_boolean 도메인의 대표 서비스만.

### 3.2 자동화 목록 항목
```json
{"entity_id":"automation.morning","automation_id":"abc123"|null,"alias":"아침 루틴",
 "state":"on","last_triggered":"2026-07-14T07:00:00+00:00"|null,"editable":true}
```
`editable = automation_id != null` (YAML 수동 관리형은 false).

### 3.3 카테고리 분류 (프론트 `js/taxonomy.js`에 상수로 구현)
```
lighting   조명        domain: light
switch     스위치/콘센트 domain: switch (state가 gas/valve 성격이면 안전 카테고리로)
safety     안전        domain: valve | switch·binary_sensor 중 device_class가
                       gas, smoke, carbon_monoxide, moisture 이거나 이름에 "가스"
detect     감지기      binary_sensor: motion, occupancy, presence, door, window,
                       garage_door, opening, vibration (없으면 detect 기타)
sensor     환경 센서    sensor: temperature, humidity, illuminance, pm25, pm10,
                       co2, pressure, battery, power, energy, 기타
climate    난방/공조    domain: climate
fan        환기/팬     domain: fan
cover      커튼/블라인드 domain: cover
media      미디어      domain: media_player
lock       잠금        domain: lock
presence   사람/위치    domain: person, device_tracker
etc        기타        나머지 전부
```
정렬: 위 표 순서. 카테고리 내 정렬은 이름 가나다.

## 4. UI 모델 (AutomationModel) — 프론트↔백엔드 공용 계약

```json
{
  "alias": "저녁 조명", "description": "", "mode": "single",
  "triggers": [TriggerNode, ...],          // 1개 이상
  "condition_mode": "and",                 // "and" | "or"
  "conditions": [ConditionNode, ...],      // 0개 이상
  "actions": [ActionNode, ...]             // 1개 이상
}
```

Duration은 항상 `{"hours":0,"minutes":5,"seconds":0}` 객체.

**TriggerNode** (`type` 판별):
| type | 필드 |
|---|---|
| state | `entity_id: str`, `from?: str`, `to?: str`, `for?: Duration` |
| numeric_state | `entity_id: str`, `above?: number`, `below?: number`, `for?: Duration` |
| time | `at: "HH:MM"` 또는 `"HH:MM:SS"` |
| time_pattern | `hours?: str`, `minutes?: str`, `seconds?: str` (예: "/5") |
| sun | `event: "sunrise"\|"sunset"`, `offset?: "-00:45:00"` (± HH:MM:SS 문자열) |
| zone | `entity_id: str(person.*)`, `zone: str(zone.*)`, `event: "enter"\|"leave"` |
| template | `value_template: str`, `for?: Duration` |
| homeassistant | `event: "start"\|"shutdown"` |

**ConditionNode**:
| type | 필드 |
|---|---|
| state | `entity_id`, `state: str`, `for?: Duration` |
| numeric_state | `entity_id`, `above?`, `below?` |
| time | `after?: "HH:MM:SS"`, `before?`, `weekday?: ["mon".."sun"]` |
| sun | `after?: "sunrise"\|"sunset"`, `before?`, `after_offset?`, `before_offset?` |
| zone | `entity_id`, `zone` |
| template | `value_template` |
| trigger | `id: str` (트리거 인덱스 문자열 "0","1"...) |
| and / or / not | `conditions: [ConditionNode,...]` |

**ActionNode**:
| type | 필드 |
|---|---|
| service | `action: "light.turn_on"`, `target?: {entity_id?: [str], area_id?: [str], device_id?: [str]}`, `data?: {}` |
| delay | `duration: Duration` |
| wait_template | `wait_template: str`, `timeout?: Duration`, `continue_on_timeout?: bool` |
| wait_for_trigger | `triggers: [TriggerNode]`, `timeout?: Duration`, `continue_on_timeout?: bool` |
| condition | `condition: ConditionNode` (중간 게이트) |
| choose | `options: [{"conditions":[ConditionNode],"sequence":[ActionNode]}]`, `default?: [ActionNode]` |
| if | `if: [ConditionNode]`, `then: [ActionNode]`, `else?: [ActionNode]` |
| repeat | `kind: "count"\|"while"\|"until"`, `count?: int`, `conditions?: [ConditionNode]`, `sequence: [ActionNode]` |
| parallel | `branches: [[ActionNode]]` → HA `parallel: [{sequence: [...]}, ...]` |
| stop | `message: str` |

## 5. 프론트엔드 (`$ROOT/automation_maker/frontend/`) — vanilla ES modules, 빌드 없음

### 5.1 파일 배치
```
index.html            # <script type="module" src="js/app.js">, 모든 경로 상대("./", 절대경로 금지)
css/styles.css        # CSS 변수 테마(라이트/다크: prefers-color-scheme), HA 스타일 톤
js/app.js             # 진입점: 상태 저장소 + 해시 라우터(#/list, #/new, #/edit/{id})
js/api.js             # fetch 래퍼: get/post/put/del, 상대경로("api/..."), 오류 → 한국어 토스트
js/store.js           # bootstrap 캐시, 인덱스(entity by id/area/category), matchesSearch()
js/taxonomy.js        # §3.3 카테고리 상수 + categorize(entity), 카테고리 아이콘/라벨
js/hangul.js          # 초성 검색: getChoseong(str), matchKorean(text, query) — 부분일치+초성
js/nl-summary.js      # summarizeModel(model, entityName) → 한국어 요약 (백엔드 summarize와 유사)
js/components/toast.js
js/components/modal.js          # 재사용 모달/바텀시트
js/components/entity-picker.js  # 방 그리드 → 카테고리 칩 → 엔티티 리스트(+검색) 3단 피커
js/components/duration-input.js # 시/분/초 입력
js/views/list.js      # 자동화 목록 뷰
js/views/wizard.js    # 새 자동화: 트리거 카테고리 4택 위저드
js/views/editor.js    # 카드 스택 편집기(트리거/조건/액션) + 저장 플로우
js/forms/trigger-forms.js   # 트리거 타입별 폼 렌더/수집
js/forms/condition-forms.js
js/forms/action-forms.js
```
- 프레임워크/CDN 금지. `document.createElement` 헬퍼 `el(tag, attrs, ...children)`을
  app.js에 정의해 공용 사용. innerHTML에 사용자 데이터 넣지 않기(XSS).
- JS 문법: ES2020까지 허용(optional chaining OK). top-level await 금지.

### 5.2 화면 흐름
1. **목록 뷰** `#/list`: 자동화 카드(이름, on/off 토글, 마지막 실행, ▶실행, 편집, 삭제).
   `editable=false`면 "YAML 관리형" 배지 + 편집/삭제 비활성. 상단 `+ 새 자동화` 버튼.
2. **위저드** `#/new`: "언제 실행할까요?" — 4개 대형 카드:
   ⏰ 시간이 되면(time/sun/time_pattern) / 🚶 사람이 오가면(zone, person state) /
   📡 센서가 감지하면(binary_sensor state) / 📊 값이 변하면(numeric_state) + 하단
   "고급: 직접 구성"(빈 편집기). 선택 → 해당 트리거 폼 프리셋과 함께 편집기로.
3. **편집기** `#/edit/{id}` 또는 위저드에서 진입: 세로 카드 스택
   - 카드1 `~할 때` (트리거들, 2개 이상이면 "이 중 하나라도 일어나면" 문구 표시)
   - 카드2 `~인 동안 (선택)` (조건들 + AND/OR 세그먼트 토글)
   - 카드3 `실행` (액션 시퀀스, 위/아래 이동 버튼, 딜레이 추가 버튼이 1급으로 노출)
   - 각 카드 `+ 추가` → 모달: 기본 탭(타입 목록) / 고급 탭(choose, if, repeat, parallel,
     wait, stop — 액션 카드에만)
   - 항목별 메뉴: 복제/삭제/위아래 이동
   - 하단 고정 바: [YAML 미리보기] [저장]. 저장 클릭 → 요약 확인 모달:
     자연어 요약(nl-summary) + 이름(alias) 입력(필수) + mode 셀렉트(기본 single,
     설명 툴팁) → 확정 시 POST/PUT. 가스/밸브/잠금 관련 액션 포함 시 경고 문구 추가.
4. **엔티티 피커** (모달): 1단 방 그리드(이름+엔티티수, "미배정" 포함) → 2단 카테고리 칩
   (해당 방에 존재하는 카테고리만) → 3단 엔티티 리스트(이름, 현재 상태, 방·기기 보조텍스트).
   상단 검색창은 전 단계 무시하고 전역 검색(matchKorean). 트리거/조건 컨텍스트에서는
   호출측이 `filter` 옵션(도메인/디바이스클래스 제한)을 넘길 수 있다.
5. **서비스 액션 폼**: 엔티티 선택 → 도메인에 맞는 서비스 셀렉트(KNOWN_SERVICES 미러 상수)
   + 대표 파라미터 폼(light.turn_on: brightness_pct 슬라이더, climate.set_temperature:
   온도 스텝퍼, media_player.volume_set: 슬라이더 등). 기타 도메인은 서비스명만.

### 5.3 스타일 지침
- CSS 변수: `--bg, --card, --text, --muted, --accent(#03a9f4 HA 블루), --danger, --border`.
  다크: `prefers-color-scheme` 미디어쿼리로 변수 재정의.
- 모바일 우선(HA 모바일 앱 iframe), 최대폭 720px 중앙 정렬, 터치 타깃 44px 이상.
- 카드: 12px radius, 미묘한 그림자. 시스템 폰트 스택(한글: Pretendard 폴백
  `-apple-system, "Malgun Gothic", sans-serif`).

## 6. 테스트 (`$ROOT/automation_maker/tests/`) — pytest, 표준 asyncio(pytest-aiohttp 사용)

```
tests/conftest.py        # sys.path 조정(automation_maker를 import 루트로), 공용 픽스처
tests/test_builder.py    # 모델→config 골든 테스트: 트리거 8종, 조건 9종, 액션 11종 각 1+,
                         #  duration 변환, or 래핑, 검증 오류 케이스 10+
tests/test_merge.py      # merge_inventory: device area 상속, friendly_name 우선순위,
                         #  device_class 병합, diagnostic/disabled 제외, 미배정 처리
tests/test_api.py        # DEV_MODE 앱(aiohttp test client): bootstrap, preview(유효/무효),
                         #  CRUD 왕복(생성→조회→수정→삭제), toggle/run
tests/test_security.py   # ingress_guard: 허용 IP 통과/기타 403 (DEV_MODE=off 상태로 앱 구성)
```
- 실행: `cd automation_maker && python -m pytest tests/ -q` 가 통과해야 한다.
- 테스트에서 실제 supervisor 접속 금지 — MockHAClient 또는 로컬 aiohttp 목서버 사용.

## 7. 공통 규칙
- Python: 표준 라이브러리 + aiohttp + PyYAML만. 타입힌트 사용. 파일당 500줄 이내 목표.
- 로그: `logging` 표준 모듈, 시작 시 mode(ha/dev)와 포트 출력.
- 모든 사용자 노출 문자열(오류 포함)은 한국어. 코드 주석은 꼭 필요한 제약만.
- 프론트 fetch 실패 시 토스트로 한국어 메시지 + 콘솔에 원본 오류.
- YAML 미리보기는 백엔드 `api/preview` 결과 사용(프론트에서 YAML 직접 생성 금지).
