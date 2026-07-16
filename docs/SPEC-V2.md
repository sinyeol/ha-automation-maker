# SPEC-V2 — 자체 규칙 엔진 + 한국어 자연어 규칙 (구현 계약서)

v2의 목표: HA `automations.yaml`을 쓰지 않고 **애드온이 스스로 규칙을 실행**한다.
HA는 엔티티 상태 소스(WS 구독)와 액션 통로(서비스 호출)로만 쓴다.
규칙은 **한국어 문장**으로 만들고, 문장이 곧 규칙의 소스 오브 트루스다.

모든 구현 에이전트는 이 문서가 계약이다. §번호의 파일 경로/시그니처/JSON을 정확히 지킬 것.
v1 계약(docs/SPEC.md)은 그대로 유효하다 — v1 화면(HA 자동화 빌더)은 별도 탭으로 유지된다.
루트: `/root/projects/HA Automation Maker/automation_maker/` (이하 생략)

---

## 0. 데이터 디렉터리

- 실기기: `/data/` (애드온 영속 볼륨, HA 백업에 자동 포함)
- DEV_MODE: 환경변수 `DATA_DIR` (기본값 `./devdata` — .gitignore에 추가됨)
- 파일: `rules.json`, `settings.json`, `runlog.json`, `pending_timers.json`
- 쓰기는 항상 원자적: 같은 디렉터리에 tmp 파일 작성 후 `os.replace`. 저장은 0.5초 디바운스.
- 공용 모듈 `backend/engine/storage.py`:
  ```python
  def data_dir() -> Path                      # DATA_DIR 환경변수 → /data 폴백, 없으면 생성
  class JsonStore:                            # 원자적 쓰기 + 디바운스 저장
      def __init__(self, path: Path, default): ...
      data: Any                               # 현재 값 (mutable)
      def save_soon(self) -> None             # 0.5s 디바운스 저장 예약
      async def flush(self) -> None           # 즉시 저장 (종료 시)
  ```

## 1. 전역 변수 (`backend/engine/variables.py`)

Fibaro $TimeOfDay 개념의 내장판. 설정(§5)의 경계를 읽어 계산한다.

```python
SEGMENTS = ["dawn", "morning", "day", "evening", "night"]   # 새벽/아침/낮/저녁/밤
SEGMENT_LABELS = {"dawn": "새벽", "morning": "아침", "day": "낮", "evening": "저녁", "night": "밤"}

class GlobalVars:
    def __init__(self, settings: dict, now_fn=None): ...    # now_fn: 테스트용 시계 주입(기본 datetime.now)
    def segment(self) -> str                                # 현재 시간대 키
    def season(self) -> str                                 # "spring|summer|autumn|winter" (3-5/6-8/9-11/12-2월)
    def day_type(self) -> str                               # "weekday|weekend|holiday" (holidays KR, 공휴일이 우선)
    def is_in_segments(self, segs: list[str]) -> bool
    def next_boundary(self) -> datetime                     # 다음 시간대 경계 시각 (경계 재평가 스케줄용)
    def snapshot(self) -> dict                              # {"segment","season","day_type","date","weekday"} (API 노출용)
```
- 기본 경계(설정으로 변경 가능): dawn 00:00–06:00, morning 06:00–09:00, day 09:00–17:00,
  evening 17:00–21:00, night 21:00–24:00. 경계는 `"HH:MM"` 문자열 리스트가 아니라
  `settings["segments"] = {"dawn": "00:00", "morning": "06:00", "day": "09:00", "evening": "17:00", "night": "21:00"}`
  (각 값 = 해당 시간대의 시작 시각. 순서 고정, 자정 걸침은 night→dawn 자연 처리).
- `holidays` 라이브러리: `holidays.country_holidays("KR")`. import 실패 시 weekday/weekend만으로 동작(크래시 금지).

## 2. 규칙 모델과 저장 (`backend/engine/rule_store.py`)

```json
Rule = {
  "id": "hex",                       // uuid4().hex
  "sentence": "거실 조명은 새벽시간에 거실에 움직임이 있으면 10%로 켜줘",
  "name": "",                        // 비면 sentence를 표시명으로 사용
  "enabled": true,
  "area_id": "living_room" | null,   // 그룹핑용 (파서가 추정, 사용자가 변경 가능)
  "category": "lighting" | ...,      // v1 taxonomy 키 (액션 대상 기준 추정)
  "model": { RuleModel },            // §3
  "pins": { "span_key": "entity_id", ... },  // 사용자가 확정한 칩 (재해석 시 보존)
  "meta": { "created": iso, "updated": iso, "last_fired": iso|null,
            "fire_count": 0, "last_error": str|null, "auto_disabled": false }
}
```
```python
class RuleStore:
    def __init__(self, store: JsonStore): ...
    def all(self) -> list[dict]
    def get(self, rule_id) -> dict | None
    def upsert(self, rule: dict) -> dict          # id 없으면 생성, updated 갱신, save_soon
    def delete(self, rule_id) -> bool
    def set_enabled(self, rule_id, on: bool) -> dict | None   # auto_disabled/last_error 해제 포함
```

## 3. RuleModel — v1 AutomationModel의 확장

v1 SPEC §4의 노드를 그대로 쓰되(엔진이 해석), 아래 **엔진 전용 노드**를 추가한다.
`condition_mode`("and"|"or"), `triggers[]`, `conditions[]`, `actions[]` 구조 동일.

**추가 TriggerNode**:
| type | 필드 | 의미 |
|---|---|---|
| state_held | `entity_id`, `to: str`, `for: Duration` | trueFor: to 상태가 for 동안 연속 유지되면 발화. 중간에 깨지면 타이머 리셋 |
| group_held | `scope: Scope`, `to: str`, `for: Duration` | 스코프 내 **모든** 엔티티가 to 상태로 for 동안 유지 ("다른 곳은 움직임이 없는 상태로 30분") |
| daily | `at: "HH:MM"` | 매일 정시 (엔진 스케줄) |
| segment | `to: str(세그먼트 키)` | 시간대가 to로 바뀌는 순간 발화 ("새벽이 되면") |

**Scope** = `{"device_class": ["motion","occupancy","presence"], "domain": null|str, "area_id": null|str, "except_area_id": null|str}`
— except_area_id는 "침실만 빼고 전체" 표현용.

**추가 ConditionNode**:
| type | 필드 | 의미 |
|---|---|---|
| time_segment | `segments: [str]` | 현재 시간대가 목록 안 ("새벽시간에") |
| day_type | `types: ["weekday"|"weekend"|"holiday"]` | 요일 구분 |
| season | `seasons: [str]` | 계절 |
| held | `entity_id`, `state: str`, `for: Duration` | 상태가 이미 for 이상 유지 중 (state_cache의 last_changed 기준) |
| group_state | `scope: Scope`, `state: str`, `for?: Duration` | 스코프 내 전부 state (for 있으면 전부 for 이상 유지) |

**액션**: v1 그대로 (service/delay/…). delay는 엔진이 asyncio.sleep으로 실행.

v1 노드(state, numeric_state, time, sun*, zone, template*) 중 **sun/template은 v2 엔진 미지원** —
파서가 생성하지 않으며, 엔진 검증에서 거부(한국어 오류). zone 트리거는 person 엔티티의
state 변화(zone 이름/home/not_home)로 구현: `{type:"zone", entity_id, zone, event}` →
엔진은 person state가 `zone_friendly|home`과 일치하는지로 평가.

## 4. 엔진 (`backend/engine/`)

### 4.1 state_cache.py
```python
class StateCache:
    def __init__(self, now_fn=None): ...
    def replace_all(self, states: list[dict]) -> None       # get_states 스냅샷 (last_updated 비교로 stale 이벤트 무시)
    def apply_event(self, entity_id, old_state, new_state) -> None
    def get(self, entity_id) -> dict | None                 # {"state","attributes","last_changed": datetime}
    def held_for(self, entity_id, state: str, duration: timedelta) -> bool
    def entities_in_scope(self, scope: dict, inventory) -> list[str]   # Scope 해석 (inventory는 bootstrap entities)
```
- last_changed: 이벤트의 new_state.last_changed(ISO) 파싱. 캐시 자체 수신 시각이 아니라 HA 타임스탬프 사용.
- 상태값 동일 이벤트(old.state == new.state, 속성만 변경)는 apply만 하고 "변경 아님"으로 표시해
  트리거 평가를 건너뛴다 (엔진에 bool 반환).

### 4.2 event_source.py — HA/모의 공용 인터페이스
```python
class EventSource(Protocol):
    async def start(self, on_event, on_resync) -> None
    # on_event(entity_id, old_state: dict|None, new_state: dict|None)
    # on_resync(states: list[dict])  — 시작 직후와 재연결 시 전체 스냅샷
    async def stop(self) -> None

class HAEventSource:      # 실기기: supervisor WS
    # auth_required→auth→auth_ok → subscribe_events(state_changed) → get_states (이 순서 필수)
    # ws_connect(heartbeat=30, receive_timeout=None) + 25초 간격 앱레벨 ping/pong(10초 타임아웃)
    # 끊기면 지수 백오프(1s→최대 60s, ±25% jitter) 무한 재연결, 연결마다 메시지 id 1부터
    # 재연결 성공 시 on_resync 호출 (엔진은 조용히 캐시 교체 + held 타이머 재평가만, 트리거 발화 금지)

class MockEventSource:    # DEV: MockHAClient와 연동
    def inject(self, entity_id, state, attributes=None) -> None   # 상태 주입 → on_event 발생
```
- MockHAClient(mock_data.py)에 `set_state(entity_id, state, attributes=None)` 메서드를 추가하고
  콜백 훅 `on_state_changed`를 둔다. MockEventSource가 이 훅을 구독한다.
  call_service("light","turn_on",...)도 mock 상태를 바꾸고 훅을 발생시킨다 (E2E 폐루프).

### 4.3 evaluator.py + engine.py
```python
class RuleEngine:
    def __init__(self, rule_store, state_cache, global_vars, ha, inventory_fn, runlog, now_fn=None, loop=None): ...
    async def start(self, event_source) -> None
    async def stop(self) -> None                 # 타이머 정리 + pending_timers.json 저장
    def reload_rule(self, rule_id) -> None       # 규칙 변경 시 구독/타이머 재계산
    async def fire_rule(self, rule: dict, context: str) -> None   # 수동 실행("run")에도 사용
    def status(self) -> dict                     # {"connected": bool, "rules": n, "active_timers": n, "vars": snapshot}
```
동작 규약:
1. **트리거 인덱스**: 규칙 로드 시 model 전체를 스캔해 entity_id 집합·scope·segment 의존을 수집
   (Fibaro식 자동 추출). `entity_id → [rule]` dict로 이벤트를 O(1) 라우팅.
   트리거가 하나도 없는 규칙은 로드 시 last_error 기록 + auto_disabled.
2. **평가 순서**: 상태 변경 → 해당 규칙의 트리거 매칭(에지: old→new 실제 변화만) →
   conditions 전부 평가(condition_mode 적용) → 통과 시 actions 순차 실행.
3. **for 타이머** (state_held/group_held, trigger의 for):
   조건 성립 순간 `loop.call_later` 등록(키: (rule_id, node_index)), 도중 깨지면 cancel.
   만료 콜백에서 조건 재확인 후 발화. 종료 시 잔여 타이머를 만료 예정 시각과 함께
  `pending_timers.json`에 저장, 기동 시 남은 시간으로 복원(이미 지났고 조건 여전히 참이면 즉시 발화).
4. **시간대/daily 스케줄**: GlobalVars.next_boundary()와 daily 트리거 시각으로
   self-rescheduling 타이머(벽시계 기준 재계산). 경계 도달 시: segment 트리거 규칙 발화 검사 +
   time_segment 조건을 가진 규칙 재평가(EventRunner 구간 경계 의미론).
5. **쿨다운**: 같은 규칙은 마지막 발화 후 5초 내 재발화 금지(하드코딩 기본, model에 없음).
6. **오류 격리**: 액션 실행 중 예외 → 그 규칙만 meta.last_error 기록, 연속 3회 오류 시
   auto_disabled=true. 엔진 루프는 절대 죽지 않는다.
7. **실행 로그** (runlog.py): ring buffer 200건, 각 항목
   `{"ts": iso, "rule_id", "sentence", "result": "fired|error|skipped_condition", "detail": str}`.
   JsonStore(runlog.json)로 영속.
8. 액션의 service 호출은 v1 HAClient.call_service 재사용 (DEV에서는 MockHAClient).

## 5. 설정 (`settings.json`) — 기본값 포함 전체 스키마

```json
{
  "segments": {"dawn":"00:00","morning":"06:00","day":"09:00","evening":"17:00","night":"21:00"},
  "persons": {"나": "person.user", "와이프": ""},        // 표면형 → person entity_id
  "modes": {"슬립 모드": {"action": "scene.turn_on", "target": {"entity_id": ["scene.sleep"]}}},
  "near_home": {"zone_state": "home", "note": "사람 엔티티가 이 상태면 '집 근처'"},   // v2.0: person state 문자열 매칭
  "aliases": [ {"surface": "안방 무드등", "entity_id": "light.master_mood"} ],
  "confirm_actions": ["lock", "valve"],                   // 이 도메인 액션은 발화 로그에 강조
  "llm": {"enabled": false}                               // API 키는 애드온 옵션(환경)에서만
}
```
- API 키는 settings.json에 저장하지 않는다. 애드온 `config.yaml` options의
  `anthropic_api_key: "password?"` → `/data/options.json` → 환경에서 읽는다.
  DEV_MODE에서는 환경변수 `ANTHROPIC_API_KEY`.

## 6. 한국어 파서 (`backend/nl/`)

의존성 0 (표준 라이브러리만). 형태소 분석기 금지.

### 6.1 gazetteer.py
```python
class Gazetteer:
    @classmethod
    def build(cls, inventory: dict, settings: dict) -> "Gazetteer"
    # inventory = {"areas":[...], "entities":[...], "zones":[...]} (v1 bootstrap 형태)
    def match(self, text: str) -> list[Span]
    # Span = {"start","end","text","type","candidates":[{"id","label","score","reason"}]}
    # type ∈ area|entity|device_word|person|mode|segment|value|duration|clock|percent|temperature
```
- 표면형 소스: area 이름, entity name(공백 변형 포함), 내장 동의어 사전
  (조명=불=전등=등=라이트, 에어컨=에어콘=냉방, 환풍기=팬=환기팬, 커튼=블라인드,
  보일러=난방, TV=티비=텔레비전, 문=도어, 움직임=모션=인기척, ...) — `SYNONYMS` 상수,
  설정의 persons/modes/aliases 오버레이가 항상 우선.
- 매칭: 표면형 길이 내림차순 최장일치(겹침 불가). 조사 스트리핑은 사전 검증형
  최장일치(자른 결과가 사전에 있을 때만 확정) — 조사 목록은 리서치 권고안 그대로.
- 후보 스코어: 별칭 정확일치 1.0 > friendly name 정확일치 0.9 > (방 맥락 일치 +0.2)
  > 부분일치 0.6 > 초성일치 0.5. reason은 한국어("이름 일치", "거실의 조명" 등).

### 6.2 parser.py — 5단계 파이프라인
```python
def parse(sentence: str, gazetteer: Gazetteer, settings: dict) -> dict   # ParseResult
```
1. 전처리: 공백 정규화. (해줘/해주세요 등 종결어미는 제거하지 않는다 — 액션 판정에 사용)
2. 패턴 선추출 (placeholder 치환):
   - 지속: `(\d+)(초|분|시간)\s*동안\s*(...)이/가?\s*(없|있)으면` → state_held/group_held 프레임
   - 지속2: `(...)(이|가)?\s*(없|있)는\s*상태(로|가)\s*(\d+)(초|분|시간)...(되면|지나면|유지되면)` → 동일
   - 수치: `\d+\s*(%|퍼센트|프로)`→percent, `\d+(\.\d+)?\s*도`→temperature(직전이 숫자일 때만),
     `\d+\s*(초|분|시간)`→duration, `(오전|오후|아침|저녁|밤|새벽)?\s*\d{1,2}시(\s*\d{1,2}분|반)?`→clock
3. 절 분리: 마지막 `(으)면` 경계(직전 어절이 동사 활용형일 때만 — 어간 사전
   `VERB_STEMS` ≈ 있|없|되|지나|하|열리|닫히|눌리|도착|감지|바뀌|올라가|내려가|넘|떨어지 등 40여 개)
   → 좌측 존을 `-고/-는데/-며`로, 우측 존을 `-고`로 재분할.
   함정 처리: 체언+하고/와/과/이랑/랑 은 절 분리가 아니라 **명사 병렬**(직전 토큰이 gazetteer 매칭이면).
4. 절 → 노드: 이벤트 동사(도착하|열리|닫히|눌리|감지되|움직임이 있)→트리거,
   비교·상태(이상|이하|초과|미만|인 상태|없는)→조건, 명령형(켜|꺼|틀어|바꿔|올려|내려|멈춰 + 줘/라/자)→액션.
   시간 절: "밤 9시 이후" → time_segment(밤)가 아니라 `time` 조건(after 21:00) — 시각이 명시되면
   시각 우선, "새벽시간에"처럼 시간대 단어만 있으면 time_segment.
   "다른 곳(은|에는)" + 부정 → group_held/group_state(scope: device_class 모션류, except_area = 문맥 방).
   "모든 X" → 스코프 전체(모든 조명 = domain light 전 엔티티, area 제한 없음).
   사람 복수("나와 와이프") → 각각 person 매핑, "-와/과 ...가 도착하고"의 and는 **두 사람 모두** 조건
   (트리거는 마지막 사건, 앞 사람은 상태 조건으로), "나 또는 와이프"의 or → condition_mode/or 그룹.
5. IR 방출 (ParseResult):
```json
{
  "ok": true,
  "model": { RuleModel },
  "chips": [ {"span":[s,e], "text":"거실 조명", "role":"target|trigger|condition|action|value",
              "slot_key":"actions[0].target", "status":"confirmed|uncertain|unresolved",
              "chosen": "light.living_room_main"|null,
              "candidates":[{"id","label","sublabel","score"}] } ],
  "summary": "새벽 시간대에 거실에서 움직임이 감지되면 → 거실 메인등을 10% 밝기로 켭니다.",
  "area_id": "living_room"|null, "category": "lighting",
  "unmatched": ["해석 못 한 구간 텍스트"],
  "confidence": 0.0~1.0,
  "warnings": ["..."]
}
```
- status 규칙: 후보 1개·score≥0.8 → confirmed, 후보 2개 이상 또는 score<0.8 → uncertain,
  후보 0 → unresolved (ok는 unresolved가 없고 model이 검증 통과일 때만 true).
- `pins` 인자(재해석 시): `parse(sentence, gz, settings, pins={slot_key: entity_id})` —
  pin된 슬롯은 후보 계산을 건너뛰고 confirmed로 유지.

### 6.3 validate (engine 쪽 재사용)
`backend/engine/rule_model.py`에 `validate_rule_model(model, inventory) -> list[{"path","message"}]`
— v1 automation_builder.validate_model과 유사하되 §3 확장 노드 포함, sun/template 거부.

### 6.4 llm_assist.py (선택 기능)
```python
async def llm_parse(sentence: str, inventory_digest: dict, settings: dict, api_key: str) -> dict | None
```
- 로컬 파서의 confidence < 0.6 이거나 unresolved가 있을 때만 호출(호출 여부는 api_v2가 결정).
- Anthropic Messages API 직접 호출(aiohttp): model `claude-haiku-4-5-20251001`, max_tokens 2000,
  tool 강제(tool_choice)로 ParseResult의 model/chips 스키마를 받는다. 타임아웃 20초.
  실패는 조용히 None (로컬 결과 사용). inventory_digest = 엔티티 id/이름/방/클래스 축약 목록
  (200개 초과 시 문장에 매칭 가능성 있는 것 우선 절단).
- 응답의 entity_id가 inventory에 실존하는지 전수 검증, 없는 id는 unresolved로 강등.

## 7. API (`backend/api_v2.py`)

```python
def register_v2_routes(app: web.Application) -> None
```
app에는 `app["engine"]`, `app["rule_store"]`, `app["settings_store"]`, `app["gazetteer_fn"]`(재빌드 함수),
`app["global_vars"]`가 이미 담겨 있다(§8 통합 담당이 배선).

| 메서드/경로 | 동작 | 응답 |
|---|---|---|
| GET `api/v2/status` | 엔진 상태 | `{"connected","rules","active_timers","vars":{...}}` |
| POST `api/v2/parse` | body `{"sentence", "pins"?}` | ParseResult (§6.2) |
| GET `api/v2/rules` | 목록 | `{"rules":[Rule...]}` (model 포함) |
| POST `api/v2/rules` | body `{"sentence","model","pins","area_id","category","name"?}` → 검증 후 저장+엔진 반영 | `{"rule": Rule}` |
| PUT `api/v2/rules/{id}` | 동일 body | `{"rule": Rule}` |
| DELETE `api/v2/rules/{id}` | 삭제+엔진 반영 | `{"ok":true}` |
| POST `api/v2/rules/{id}/toggle` | body `{"on":bool}` | `{"rule": Rule}` |
| POST `api/v2/rules/{id}/run` | 조건 무시하고 액션 실행(테스트) | `{"ok":true}` |
| GET `api/v2/runlog` | 최근 로그 | `{"entries":[...]}` (최신순 최대 200) |
| GET `api/v2/settings` | 설정 | settings.json 전체 + `{"llm_available": bool}` |
| PUT `api/v2/settings` | 부분 갱신(merge) → gazetteer 재빌드 + vars 갱신 | 갱신된 설정 |
| POST `api/v2/dev/state` | **DEV_MODE 전용** body `{"entity_id","state","attributes"?}` → MockEventSource.inject | `{"ok":true}` (비DEV 404) |

- POST parse: 로컬 파서 실행 → confidence<0.6 또는 unresolved 존재 && API 키 존재 → llm_parse 병합
  (LLM이 채운 슬롯은 status "uncertain"으로, score 0.7). 응답에 `"used_llm": bool`.
- rules 저장 시 validate_rule_model 실패 → 400 `{"error":{"code":"invalid_rule","errors":[...]}}`.
- 오류 봉투/한국어 메시지는 v1과 동일 규약.

## 8. 통합 (app.py / mock_data.py / 패키징) — 담당: integration 에이전트만 수정

1. `app.py`: `from backend.api_v2 import register_v2_routes` + create_app에서
   엔진 구성요소 생성·`app[...]` 배선·`register_v2_routes(app)` 호출,
   on_startup에서 engine.start(event_source), on_cleanup에서 engine.stop().
   DEV_MODE → MockEventSource+MockHAClient, 실기기 → HAEventSource+HAClient.
2. `mock_data.py`: `set_state`/`on_state_changed` 훅 추가(§4.2), call_service가 상태 반영
   (light.turn_on→"on"+brightness, switch/fan/climate/media_player/lock/cover 기본 동작, scene은 no-op 로그).
3. `config.yaml`: version 2.0.0, `options: {log_level: "info"}`,
   `schema: {log_level: "list(debug|info|warning|error)", anthropic_api_key: "password?"}`.
4. `Dockerfile`: 변경 불필요(requirements 경유). `backend/requirements.txt`에 `holidays>=0.100` 추가.
5. `run.sh`: options.json의 anthropic_api_key를 환경으로 노출:
   `export ANTHROPIC_API_KEY="$(bashio::config 'anthropic_api_key')"` (null 처리 포함).
6. `.gitignore`: `devdata/` 추가.

## 9. 프론트엔드 v2

### 9.1 내비게이션 (app.js 수정 — frontend 담당)
상단 탭 3개: **루틴**(#/, 기본) · **HA 자동화**(#/ha → 기존 v1 목록, 기존 라우트 #/list·#/new·#/edit 유지) · **설정**(#/settings).

### 9.2 파일
```
js/views/routines.js        # v2 메인: 문장 입력창 + 예시 칩 + 문장 카드 리스트
js/views/settings.js        # 시간대 경계/사람/모드/별칭 편집
js/components/parse-card.js # 해석 확인 카드
js/api2.js                  # api/v2/* fetch 래퍼 (api.js 재사용해도 됨 — 얇게)
```

### 9.3 루틴 화면 (routines.js)
- 최상단: 큰 입력창 "예: 화장실은 5분 동안 움직임이 없으면 환풍기와 조명을 꺼줘" + [해석] 버튼.
  아래 예시 문장 칩 3개(고정 문구, 탭하면 입력창에 채움).
- 해석 결과 → **parse-card** 표시(입력창 아래 인라인).
- 그 아래 규칙 리스트: 카드 제목 = **문장 그대로**, 부제 = `⏰/📡 아이콘 + 방 이름 + 대상 n개`,
  우측 활성 토글, 메타 = 마지막 실행 시각·오류 배지(auto_disabled면 "오류로 꺼짐" 빨강),
  메뉴 = ▶테스트 실행 / 편집 / 삭제(확인 모달).
- 그룹핑: 방별 섹션(area_name, 미배정 마지막) — 상단 세그먼트로 [방별|카테고리별|전체] 전환.
  검색창(matchKorean, 문장 전문 검색).
- 편집 = 카드 탭 → 입력창에 문장 로드 + 기존 pins 로 재해석(parse-card 재표시) → 저장 시 PUT.
- 헤더에 엔진 상태 점(연결됨 초록/끊김 빨강, GET api/v2/status 30초 폴링) + 현재 시간대 라벨("지금: 새벽").

### 9.4 해석 확인 카드 (parse-card.js)
- 원문 문장을 span 단위로 렌더: 칩 role 색상 4종(CSS 변수 —chip-trigger 파랑/—chip-condition 보라/
  —chip-target 초록/—chip-value 주황), status 3상태(confirmed: 채움, uncertain: 노란 테두리+탭 시
  후보 드롭다운(label + sublabel(방·기기), "이 중에 없음" 항목 포함), unresolved: 빨강 점선+탭 시
  엔티티 피커(v1 entity-picker 재사용) 열기).
- 후보 선택/피커 선택 → pins에 기록 → POST api/v2/parse 재호출(pins 포함) → 카드 갱신.
- 카드 하단: summary 한 줄 + unmatched 있으면 "이 부분은 이해하지 못했어요: …" 경고 +
  [다시 해석] [루틴 저장] 버튼(unresolved 있으면 저장 비활성).
- used_llm이면 "AI 도움으로 해석했어요" 캡션.

### 9.5 설정 화면
- 시간대 경계 5개(time input), 사람 매핑(표면형 고정: 나/와이프 + 추가 버튼, person 엔티티 셀렉트),
  모드 매핑(이름 + scene/script 엔티티 피커), 별칭 목록(표면형 텍스트 + 엔티티 피커, 추가/삭제),
  저장 버튼 → PUT api/v2/settings. llm_available이면 "AI 해석 보조: 사용 가능" 표시(키는 애드온 설정에서).

## 10. 테스트 (`tests/`)

- `test_variables.py`: 시계 주입으로 segment/season/day_type/next_boundary (공휴일은 holidays 있을 때만 skip 마커).
- `test_nl_parser.py`: **골든 테스트** — 사용자 예시 5문장 각각의 기대 model을 하드코딩 대조
  (엔티티는 mock inventory 기준: 거실 모션→binary_sensor.living_room_motion 등),
  변형 20문장 이상(단위 이형태·조사 생략·어순), 실패 케이스(빈 문장, 무의미 문장 → unmatched).
- `test_engine.py`: 시계·루프 주입 가능한 구조로 —
  (a) 모션 on 이벤트 → 발화, (b) state_held: on 후 for 경과 시 발화·중간 off면 미발화
  (asyncio 테스트에서 loop.call_later를 짧은 실시간(0.05s)으로 축소한 Duration 사용),
  (c) 조건 불충족 skip 로그, (d) 오류 3회 → auto_disabled, (e) segment 경계 재평가,
  (f) pending timer 저장/복원.
- `test_api_v2.py`: parse→rules 저장→dev/state 주입→runlog에 fired 기록되는 폐루프 1개 포함.
- 기존 126개 테스트는 계속 통과해야 한다.

## 11. 공통 규칙
- 새 파이썬 의존성: `holidays`만. NL 파서는 표준 라이브러리만.
- 모든 사용자 노출 문자열 한국어. 로그에 API 키 출력 금지.
- 파일당 600줄 이내 목표. asyncio task는 강참조 보관.
- DEV_MODE에서 전체 기능이 HA 없이 동작해야 한다 (E2E 전제).
