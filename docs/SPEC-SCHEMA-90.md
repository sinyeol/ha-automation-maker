# SPEC-SCHEMA-90 — 정확도 90% 신규 의미축 목표 스키마 (gold 라벨링 계약)

> 지위: SPEC-ACCURACY-90 §3·§4 의 라벨링/구현 목표 스키마 확정 문서. SPEC-V3 위에 얹는다.
> 원칙: **기존 노드로 표현되는 축은 필드 추가/라벨 규약으로, 진짜 새 의미만 노드 신설.**
> 라벨러는 이 문서 + 엔티티 카탈로그만 보고 gold 를 찍는다. gold 는 전부
> `validate_rule_model` 통과가 원칙이다(§7 특수형 prohibition/out_of_scope 제외).

## 0. gold 공통 래퍼

```json
{"subrules": [
  {"triggers": [...], "conditions": [...], "actions": [...]}
]}
```
- **항상 subrules 래퍼**로 쓴다(1개여도 평탄화 금지 — 라벨 일관성).
- `condition_mode` 는 "or" 일 때만 명기(기본 and).
- Duration 객체: `{"hours":h,"minutes":m,"seconds":s}` — 부분 키 허용(예 `{"minutes":5}`).
- 상태값 문자열은 반드시 인용: `"on"`,`"off"` (YAML bool 오염 방지).
- 시각 트리거는 **`daily` 만** 사용한다. v1 `time` 트리거는 엔진이 스케줄하지 않아
  auto_disabled 되므로 gold 금지.
- 엔티티/모드/존은 반드시 엔티티 카탈로그(별첨) 실존 항목만.

### 0.1 기존 노드 인벤토리 (참조용 — gold 에 그대로 사용)
| 위치 | 노드 |
|---|---|
| 트리거 | `state(entity_id,to,from?,for?)` · `numeric_state(entity_id,above?,below?)` · `state_held(entity_id,to,for)` · `group_held(scope,to,for)` · `daily(at "HH:MM")` · `segment(to)` · `mode(mode,to)` · `zone(entity_id,zone,event)` |
| 조건 | `state(entity_id,state)` · `numeric_state(above?,below?)` · `time_segment(segments)` · `day_type(types)` · `season(seasons)` · `mode(mode,state)` · `held(entity_id,state,for)` · `group_state(scope,state,for?)` · `zone(entity_id,zone)` · `trigger(id)` · `and/or/not(conditions)` |
| 액션 | `service(action,target,data)` · `set_mode(mode,to)` · `delay(duration)` · `if(if,then,else?)` · `choose` · `repeat(kind,count|conditions,sequence)` · `condition` · `stop` |

주의: 기존 조건 `time` 의 `weekday` 필드는 gold 에서 **사용 금지** — 요일은 §2.2 `weekday`
노드로 통일한다(부정 여집합 표현 때문).

---

## 1. 신규 트리거 노드

### 1.1 `sun` — 일출/일몰 ± 오프셋
```json
{"type": "sun", "event": "sunrise" | "sunset", "offset": -1800}
```
- `offset`: **초 단위 정수**(음수=이전, 양수=이후, 생략=0, |offset| ≤ 43200).
- 예: "해 지기 30분 전" → `{"type":"sun","event":"sunset","offset":-1800}`.
- HA 매핑: `{"trigger":"sun","event":"sunset","offset":"-00:30:00"}` (초→±HH:MM:SS 변환, 0이면 생략).
- 엔진: `SunProvider` 신설 — settings.location(`{"latitude","longitude"}`, 기본 서울
  37.5665/126.9780)으로 NOAA 근사식(순수 파이썬, 의존성 0) 일출/일몰 계산.
  `_schedule_daily` 와 동형의 self-rescheduling 타이머: 다음 (event+offset) 시각 계산 →
  `call_later` → 발화(조건 재평가) → 익일 재장전. 재시작/resync 시 재계산만(자동 발화 금지).
- 검증(rule_model): `_UNSUPPORTED` 에서 `"sun"` 제거. event ∈ {sunrise,sunset},
  offset 은 int, |offset| ≤ 43200.

### 1.2 `time_pattern` — N분/시간마다
```json
{"type": "time_pattern", "minutes": 30}
{"type": "time_pattern", "hours": 2}
```
- `hours`|`minutes`|`seconds` 중 **정확히 1개**, 정수 N(≥1, minutes/seconds ≤59, hours ≤23).
- 의미(HA `/N` 동형): 벽시계 값이 N 의 배수인 시점마다 발화(minutes 30 → 매시 :00·:30).
- HA 매핑: `{"trigger":"time_pattern","minutes":"/30"}`.
- 엔진: 다음 배수 시각 계산 → self-rescheduling 타이머, 발화마다 조건 재평가.
- 검증: 필드 1개 필수·정수 범위.

### 1.3 `presence_agg` — 프레즌스 양화 (트리거)
```json
{"type": "presence_agg", "quant": "first"|"last"|"any"|"all",
 "persons": ["person.user", "person.wife"],   // 선택 — 생략 = 인벤토리 전체 person.*
 "for": {"minutes": 10}}                       // 선택 — last/all 에만 허용
```
| quant | 에지 의미(집 인원수 count 기준, home 상태) |
|---|---|
| `first` | 0 → ≥1 (첫 귀가) |
| `last` | ≥1 → 0 (마지막 외출, "아무도 없게 되면") |
| `any` | count 증가(누구든 도착) |
| `all` | 마지막 도착으로 count == len(persons) 도달(전원 귀가 완료) |
- `for` 가 있으면 결과 상태가 for 동안 유지될 때 발화(held 타이머 재사용; last=무인 유지, all=전원재실 유지).
- HA 매핑: first → `{"trigger":"numeric_state","entity_id":"zone.home","above":0}`,
  last → `{"trigger":"numeric_state","entity_id":"zone.home","below":1}` (+for),
  any/all → person 별 `state` 트리거(to "home") + all 은 전원 home 조건 병기.
- 엔진: persons 를 `_index` 에 등록, person 상태 변경 시 (old_count,new_count) 계산해
  quant 에지 판정. for 는 `_for_timers` 재사용(키 (rid, flat)).
- 검증: quant ∈ 집합, persons 는 실존 person.* 목록(생략 가능), for 는 duration.

---

## 2. 신규 조건 노드

### 2.1 `sun_window` — 일몰~일출 창 (자정 걸침)
```json
{"type": "sun_window", "after": "sunset", "before": "sunrise",
 "after_offset": 0, "before_offset": 0}
```
- (after 이벤트+offset) ~ (before 이벤트+offset) 사이면 참. **start > end(벽시계)면 자정
  걸침 창**: `now >= start or now <= end`. 오프셋은 초 단위 정수(생략=0).
- "어두울 때/해 진 뒤엔" → `{"after":"sunset","before":"sunrise"}`.
- HA 매핑: `{"condition":"sun","after":"sunset","before":"sunrise","after_offset":"+00:15:00"}`
  (0 오프셋 생략).
- 엔진: evaluator 에서 SunProvider 로 오늘 기준 이벤트 시각 계산 후 창 판정(순수 평가).
- 검증: after·before 모두 필수, 값 ∈ {sunrise,sunset}, 오프셋 int.

### 2.2 `weekday` — 요일 집합 · 부정 여집합
```json
{"type": "weekday", "days": ["mon","wed","fri"], "negate": false}
```
- days ⊂ {mon,tue,wed,thu,fri,sat,sun}, 비어 있으면 안 됨. `negate:true` = "이 요일들 **빼고**".
- "주말 빼고" → `{"days":["sat","sun"],"negate":true}` (평일로 전개하지 말 것 — 발화 의도 보존).
- HA 매핑: `{"condition":"time","weekday":[...]}` — negate 는 **여집합으로 전개**해 출력.
- 엔진: `_WEEKDAYS[now.weekday()] in days` XOR negate.
- 검증: days 비어있지 않은 부분집합, negate bool.

### 2.3 `day_of_month` — 매달 N일 / 말일
```json
{"type": "day_of_month", "days": [1, 15]}
{"type": "day_of_month", "days": "last"}
```
- days: 1..31 정수 목록 **또는** 문자열 `"last"`(말일).
- HA 매핑(네이티브 없음 → template 조건): `{{ now().day in [1, 15] }}` /
  last: `{{ (now() + timedelta(days=1)).day == 1 }}`.
- 엔진: `now.day in days` / last: 내일이 1일인지. 순수 평가.
- 검증: 목록이면 1..31 정수, 아니면 "last".

### 2.4 `interval_anchor` — 격주(앵커 기준 N주기)
```json
{"type": "interval_anchor", "unit": "week", "interval": 2, "anchor": "2026-07-13"}
```
- unit 은 현재 `"week"` 만. interval ≥ 2 정수. anchor = ISO 날짜(그 날이 속한 주가 0번째 주기).
- 판정: `((monday_of(now) - monday_of(anchor)).days // 7) % interval == 0` (월요일 기준 주 정렬).
- 라벨 규약: 문장에 기준일이 없으면 anchor 는 **라벨 작성일이 속한 주의 월요일**로 찍는다.
- HA 매핑: template 조건
  `{{ (((as_datetime('2026-07-13').date() | ... )) ... ) }}` — 빌더가 주차 계산식 생성.
- 엔진: 순수 벽시계 평가.
- 검증: unit=="week", interval int ≥2, anchor 날짜 형식.

### 2.5 `presence_agg` (조건형)
```json
{"type": "presence_agg", "quant": "none"|"any"|"all", "persons": [...]}
```
- 레벨 의미: `none`=아무도 집에 없음, `any`=한 명 이상 집, `all`=전원 집.
  (트리거는 first/last/any/all, 조건은 none/any/all — quant 허용값이 위치에 따라 다름.)
- HA 매핑: none → `numeric_state zone.home below 1` 조건, any → above 0,
  all → person 별 state 조건 and 묶음.
- 엔진: cache 에서 persons 의 home 카운트 계산.

---

## 3. 액션 확장 — 새 노드 아님, `service` 의 `data` 필드

### 3.1 light.turn_on 파라미터
```json
{"type": "service", "action": "light.turn_on",
 "target": {"entity_id": ["light.living_room_mood"]},
 "data": {"brightness_pct": 30, "rgb_color": [255, 0, 0],
          "color_temp_kelvin": 2700, "transition": 5}}
```
| 필드 | 형 | 범위 | 라벨 규약 |
|---|---|---|---|
| `brightness_pct` | int | 1~100 | 절대 밝기("30 프로로") |
| `brightness_step_pct` | int | -100~100, ≠0 | 상대("더 밝게" +20 / "좀 어둡게" -20 — 수치 없으면 ±20 고정) |
| `rgb_color` | [int,int,int] | 각 0~255 | 색 이름→고정 팔레트: 빨강[255,0,0]·주황[255,126,0]·노랑[255,220,0]·초록[0,255,0]·파랑[0,0,255]·보라[160,32,240]·분홍[255,105,180]·흰색[255,255,255] |
| `color_temp_kelvin` | int | 2000~6500 | **전구색/따뜻한 색=2700**, 주백색=4000, **주광색/하얀 불=6500** |
| `transition` | number(초) | ≥0 | "서서히/부드럽게"=5, "천천히"=10 (수치 명시 시 그 값) |
- brightness_pct 와 brightness_step_pct 동시 금지. HA 매핑: data 그대로 통과(HA 네이티브).
- 엔진: `_call_service` 통과. MockHAClient `_reflect` 에 rgb_color/color_temp_kelvin/
  brightness_step_pct 반영 추가(구현 노트).

### 3.2 toggle
- 단일 도메인 대상 → `<domain>.toggle` (예 `light.toggle`).
- 혼합/불명 도메인 → `homeassistant.toggle`.
- **필수 코드 변경**: `rule_model._ALLOWED_ACTION_DOMAINS` 에 `"homeassistant"` 추가
  (현재 `_DOMAIN_AGNOSTIC_SERVICE_DOMAINS` 에는 있으나 화이트리스트에 없어
  parser.py:1374 의 `homeassistant.turn_on` 도 거부되는 기존 불일치 해소).

### 3.3 notify — 메시지/채널
```json
{"type": "service", "action": "notify.notify",
 "data": {"message": "욕실 누수 발생", "title": "알림", "target": "mobile"}}
```
- action 은 항상 `notify.notify`(target.entity_id 없음). `data.message` **필수**.
- `data.target`(선택): 채널 힌트 문자열 — "폰으로"→`"mobile"`, "스피커로/방송"→`"speaker"`.
  채널 미지정이면 생략.
- HA 매핑: 그대로(실기기에선 서비스명을 설정된 notify 서비스로 치환하는 것은 후속).
- 검증: `notify.notify` 는 data.message 비어있지 않아야 함(`_REQUIRED_SERVICE_DATA` 계열 추가).
- 엔진: `_call_service` → `ha.call_service("notify","notify",data)`.
  MockHAClient 에 notify 로그 반영 추가(구현 노트).

### 3.4 repeat — 기존 노드 사용
```json
{"type": "repeat", "kind": "count", "count": 3,
 "sequence": [{"type":"service",...}, {"type":"delay","duration":{"seconds":1}}, ...]}
```
- "세 번 깜빡여" → kind count + [on, delay 1s, off, delay 1s] 시퀀스.
- "될 때까지" → kind `until` + conditions. 이미 검증/엔진 실행 지원(`_run_repeat`).

---

## 4. 파싱 처리축 — 라벨 규약 (스키마 아님)

### 4.1 상(aspect): 조건 vs 트리거
- **전이형 어미**(켜지면/열리면/감지되면/도착하면/넘으면/떨어지면/되면) → **트리거**.
- **상태형 어미**(켜져 있으면/열려 있는데/-인 동안/-일 때/집에 있으면/높으면/사이면) → **조건**.
- 승격 규칙: 문장에 전이형 절이 하나도 없으면 **첫 상태형 절을 트리거로 승격**
  (state/numeric_state 트리거 = 해당 상태·구간 **진입 에지**). 나머지 상태형 절은 조건 유지.

### 4.2 수치: 에지 vs 레벨 vs between
- "넘으면/올라가면/도달하면" = `numeric_state` **트리거**(above 경계 상향 돌파),
  "떨어지면/내려가면" = below 하향 돌파. 엔진 `_immediate_edge` 가 crossing 을 보장.
- "높으면/낮으면/사이면"(레벨) = `numeric_state` **조건** — 단 4.1 승격 규칙 적용.
- between("20도에서 24도 사이") = above 20 + below 24 를 **한 노드에 결합**(경계값 미포함).

### 4.3 부정(negation)
| 표현 | 라벨 |
|---|---|
| 이분 상태 부정("모션이 없으면", "안 켜져 있으면") | **반대 상태로 직접**(state "off") |
| 요일 예외("주말 빼고/일요일만 빼고") | `weekday` + `negate:true` |
| 대상 예외("안방 빼고 다 꺼줘") | target.entity_id 를 **제외 후 명시 목록**으로 전개 |
| 그 외("집이 아니면") | `{"type":"not","conditions":[...]}` 래퍼 |

### 4.4 금지문(prohibition) — 위험 오동작 0
"-지 마 / 못 -게 해 / 절대 -하지 마" 는 **자동화 생성 요청이 아니다**. gold:
```json
{"prohibition": true,
 "forbidden": [{"action": "lock.unlock", "entity_id": "lock.entrance_door"}],
 "subrules": []}
```
- 판정(evaluate_hybrid, exact 아님): 파서가 모델 미생성(needs_help/거절) → **통과**.
  모델을 만들었어도 forbidden (action, entity_id) 매치 액션이 없으면 통과.
  forbidden 매치 액션 생성 = **위험 오동작 실패**. entity_id 생략 항목은 action 만으로 매치.

### 4.5 인용 메시지
- 따옴표(' " “) 또는 "-라고/-하고 (알려줘/보내줘/말해줘)" 인용부 → `notify` `data.message` 에
  **인용 내부 원문 그대로**(조사·어미 정규화 금지, 따옴표만 제거).

### 4.6 else 분기
- 기본형: 한 트리거 아래 조건 분기 → **`if` 액션 노드**(then/else).
- 대칭 전이형("켜지면 A, 꺼지면 B")에만 반대 에지 **서브룰 2개**로 분해.

### 4.7 한정 지속·복원(duration_revert)
- "N분만 -했다가 (원래대로/꺼줘)" → 액션 원자 분해 `[on, delay N, off]` (스냅샷 복원은 2단계, gold 는 이 분해형).

### 4.8 범위(out_of_scope) — weather 등
인벤토리에 대응 엔티티가 없는 외부 의존 문장(날씨 등):
```json
{"out_of_scope": true, "reason": "weather", "subrules": []}
```
- 판정: 모델 미생성이면 통과, 그럴듯한 임의 모델 생성은 실패(정직 미달 측정용).
- 단, **실존 센서로 표현 가능한 외부 축**(미세먼지 = sensor.veranda_pm25)은 일반 gold 로 라벨.

---

## 5. HA 자동화 매핑 요약표

| 앱 노드 | HA config |
|---|---|
| trigger sun(event,offset초) | `trigger: sun, event, offset: "±HH:MM:SS"` |
| trigger time_pattern(minutes N) | `trigger: time_pattern, minutes: "/N"` |
| trigger presence_agg first/last | `trigger: numeric_state, entity_id: zone.home, above 0 / below 1` |
| trigger presence_agg any/all | person 별 `trigger: state, to: home` (+all: 전원 home 조건) |
| condition sun_window | `condition: sun, after/before(+offset)` |
| condition weekday(negate) | `condition: time, weekday: [여집합 전개]` |
| condition day_of_month | `condition: template` (now().day / 말일식) |
| condition interval_anchor | `condition: template` (앵커 주차 mod 식) |
| condition presence_agg | `condition: numeric_state zone.home` / person state and 묶음 |
| service data(rgb·kelvin·step·transition) | data 그대로(HA 네이티브) |
| notify.notify | `action: notify.notify, data.message` |
| prohibition / out_of_scope | 매핑 없음(자동화 미생성) |

## 6. 검증 규칙 변경 계약 (rule_model.py)

1. `_UNSUPPORTED = {"template"}` — sun 제거(트리거 sun·조건 sun_window 자체 노드로 수용).
2. `_validate_trigger` 에 sun / time_pattern / presence_agg 분기 추가(§1 규칙).
3. `_validate_condition` 에 sun_window / weekday / day_of_month / interval_anchor /
   presence_agg 분기 추가(§2 규칙). 기존 time 조건의 weekday 필드는 deprecate 경고.
4. `_ALLOWED_ACTION_DOMAINS` 에 `"homeassistant"` 추가.
5. service data 검증: light.turn_on 파라미터 범위(§3.1), brightness_pct·step 동시 금지,
   `notify.notify` 의 data.message 필수.
6. presence_agg.persons 는 인벤토리 실존 person.* 검증(valid_ids 재사용).

## 7. 엔진 반영 요약 (engine.py / evaluator.py)

- **SunProvider 신설**(engine/sun.py): NOAA 근사식, settings.location, 캐시(일 단위).
- `_index_rule`: sun → `_schedule_sun`(daily 계열), time_pattern → `_schedule_pattern`,
  presence_agg → persons 를 `_index` 등록(+`has_trigger`).
- `_eval_rule_triggers`: presence_agg 에지 판정(홈 카운트 old/new).
- evaluator `evaluate_condition`: sun_window / weekday / day_of_month / interval_anchor /
  presence_agg(레벨) 분기 추가 — 전부 순수 평가(§2).
- 재시작/resync: sun·time_pattern 타이머는 재계산만, 자동 발화 금지(기존 §4.2 의미론).
- MockHAClient: notify 로그 반영, light data(rgb_color/color_temp_kelvin/brightness_step_pct) 반영.

## 8. 라벨러 체크리스트

1. gold 는 항상 `{"subrules":[...]}` (금지문·범위밖은 §4.4·§4.8 특수형).
2. 엔티티는 카탈로그 실존 id 만, 모드는 "슬립 모드"만, 존은 zone.home/zone.work 만.
3. 시각 트리거 = daily("HH:MM"), 시간대 = segment/time_segment, 요일 = weekday 노드.
4. sun offset 은 초 정수(30분 전 = -1800).
5. 상태값은 문자열 인용("on"/"off"/"unlocked"/"open"/"home"/"not_home").
6. 색·색온도·transition 은 §3.1 고정 팔레트/기본값 표를 그대로 쓴다.
7. 금지문에서 절대 정방향 자동화를 만들지 않는다.
