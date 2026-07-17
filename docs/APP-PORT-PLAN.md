# APP-PORT-PLAN — 정확도 90% Phase 7: 오프라인 프로토타입의 앱 이식 계획

> 지위: SPEC-ACCURACY-90 §7 로드맵 Phase 7("앱 편입")의 실행 설계 확정 문서.
> 전제(실측, 커밋 8382c85): 오프라인 오버레이(L1A) **정직 held-out466 = 74.2%**,
> test L1A 72.8%, 회귀 0, 금지문 위험 오동작 0/23. 채점은 정직 채점기
> (`tools/corpus/structural_match.py` — 신규노드·중첩·시각 의미필드 전부 비교) 기준.
> 이 문서는 그 능력을 실제 애드온(`automation_maker/`)에 **엔진이 실제 실행하는 형태로**
> 이식하는 "무엇을 어느 파일에 어떻게"를 확정한다. 담당: 구현 Opus, 문서/패키징 Sonnet.

---

## 0. 원칙과 전체 그림

1. **파이프라인 형태 보존**: 오프라인에서 검증된 것은 `표면정규화 → 금지문 게이트 →
   (규칙 파서) → 모델 후처리(augment)` 라는 파이프라인 전체다. 앱에서도 같은 형태를
   유지하되 monkeypatch 를 없앤다 — 메서드 패치는 **파서 본체 직접 수정**, 후처리는
   `parse()` 반환 직전에 호출되는 **일급 모듈**(`backend/nl/postpass.py`)로 이식한다.
   형태를 보존해야 74.2% 와의 A/B 패리티 검증(§5.2)이 가능하다.
2. **차분 이식**: A/B 규칙 상당수는 v3.1(커밋 f2e0fd9)에서 이미 앱에 반영됐다. 이번
   대상은 "오버레이 최종본 − 앱 현재본"의 **차분**이다(§1.2 표에 메서드별 차분 명시).
   오버레이 원문을 통짜 복사하면 v3.1 이후 앱에서 고친 결함(§결함2/3, ⓐⓑⓒ)을 되돌린다.
3. **수직 슬라이스**: 신규 노드는 `검증기+엔진+빌더+파서+테스트`를 한 커밋 단위로 묶는다.
   순서가 어긋나면 위험하다 — 검증기만 먼저 열면 파서/매처가 만든 신규 노드 규칙이
   저장은 되는데 `engine._index_rule` 이 몰라서 **auto_disabled** 되는 중간 상태가 생긴다
   (engine.py:277 `_mark_no_trigger`, 기존 `time` 트리거가 정확히 이 함정이다).
4. **게이트**: 슬라이스마다 ① 앱 pytest 390개 회귀 0, ② held-out 축별 리프트 실측
   (`evaluate_hybrid.py`), ③ 금지문 적대 세트 위험 오동작 0. 미달 슬라이스는 머지 금지.

파이프라인 최종형(앱):

```
api_v2.handle_parse
  └ parser.parse(sentence)
      ├ surface.normalize_surface(sentence) → normalized      # normalize90 이식
      ├ surface.is_prohibition(normalized) → 조기 반환(모델 미생성)  # 금지문 게이트
      ├ _Parser(sentence=원문, normalized).parse()             # A/B/P2/L1 본체 반영
      └ postpass.apply(result, 원문, normalized, gz, settings, now_fn)  # augment 이식
  └ (not ok) learned → matcher(L2 재설계 §4) → (수동선택) learn CLI
엔진: rule_store 저장 모델을 RuleEngine 이 실행(신규 노드 §2) / HA 내보내기는 ha_map(§2.6)
```

---

## 1. 포팅 매핑 표

범례 — 방식: **신규모듈**(새 파일로 이식) · **차분반영**(기존 함수에 diff 만) ·
**postpass**(후처리 모듈로 이식) · **스킵**(이미 앱에 반영됨, 검증만).

### 1.1 신규 모듈 (2개)

| # | 능력 | 소스(오프라인) | 목적지 | 방식 |
|---|---|---|---|---|
| 1 | 표면정규화 5축+(수사→숫자·어미통일·존칭·완화어·후치재배열·임계괄호) | `normalize90.py` 전체 | **신규 `backend/nl/surface.py`** — `normalize_surface(sentence)` 그대로. `parser.parse()` 진입부에서 호출 | 신규모듈 |
| 2 | 금지문 오독 방어(-지 마/못 -게/-면 안 돼, 인용부 예외) | `parser_overlay._QUOTE_SPAN_RE/_PROHIBIT_RE/_is_prohibition/_prohibition_result` | `backend/nl/surface.py` 에 동거(`is_prohibition`), `parser.parse()` 가 normalize 직후 검사해 조기 반환(ok=False, subrules=[], 경고 문자열 유지) | 신규모듈 |

통합 지점: `parser.py:1687 parse()` 를 아래로 교체(모듈 함수만 변경, `_Parser` 시그니처에
`normalized` 추가):

```python
def parse(sentence, gazetteer, settings, pins=None, now_fn=None):
    normalized = surface.normalize_surface(sentence)
    if surface.is_prohibition(normalized):
        return surface.prohibition_result(sentence)
    res = _Parser(sentence, gazetteer, settings, pins or {}, normalized=normalized).parse()
    return postpass.apply(res, sentence, normalized, gazetteer, settings, now_fn)
```

`_Parser.parse()` 의 `text = normalize_ws(self.sentence)`(parser.py:392) 는
`normalize_ws(self.normalized)` 로 바꾸되 **`self.sentence` 는 원문 유지** — alias·칩
스팬·인용 메시지(notify)가 원문 기준이어야 한다. 정규화로 표면이 바뀐 칩(예 "일곱 시"→"7시")은
`_span_of` 미스로 [0,0] 이 되며 프론트가 이미 [0,0] 을 처리한다(허용 손실).

### 1.2 파서 본체 차분(메서드별) — `backend/nl/parser.py`·`gazetteer.py`·`normalize.py`

| # | 능력 | 오버레이 심볼 | 목적지 | 차분 내용(오버레이−앱) |
|---|---|---|---|---|
| 3 | 절 경계 어간 확장 | `VERB_STEMS` 패치 | parser.py:23 `VERB_STEMS` | `"높","낮","들어가","지나가","찍"` 추가 (잡히·움직이·나서·비우·새 는 반영됨) |
| 4 | 주중=weekday | `DAY_TYPE_WORDS` 패치 | gazetteer.py:87 `DAY_TYPE_WORDS` | `"주중": "weekday"` 추가 |
| 5 | 경계 '면' 쉼표 허용 | `_is_myeon_boundary_B` | parser.py:147 | `w = word.rstrip(",")` 한 줄 + copula 주석 |
| 6 | 지속 프레임 p3(주어생략 부정모션 "5분간 안 움직이면") | `_duration_frames_A4` 의 p3 | parser.py:104 `_duration_frames` | p3 정규식 + `_sub3` 추가 (p1 '간' 확장은 반영됨) |
| 7 | 모드 극성 기본값 | `_detect_mode_B` | parser.py:628 `_detect_mode` | 최종 폴백을 `("condition",…)`→`("trigger", name, "on")` 로(문두 "모드+면"). 나머지 분기는 동일 — **회귀 주의**: test_nl_parser 모드 조건 케이스 재확인 |
| 8 | aspect 라우팅(결과상/지속상=조건, 승격 규칙) | `_RESULTATIVE_RE/_DURATIVE_RE/_is_state_aspect` + `_process_antecedent_B` | parser.py:681 `_process_antecedent` | state_events/trans_events 분리, 이벤트·수치 존재 시 모드 트리거→조건 강등, 트리거 0개면 첫 상태절만 승격. 정규식 상수는 parser.py 상단에 |
| 9 | 지속상 조건 분리("…동안엔 켜") | `_split_durative_condition/_route_condition_segment_B/_process_consequent_B` | parser.py:1111 `_process_consequent` | 절 앞부분 지속상 조건 분리 + 트리거 방 임시 전파 해석 |
| 10 | 결과상 상태값·도메인 상태(cover/lock)·lock 재매핑·bare 문/창 | `_aspect_state/_build_state_event_B/_UNLOCK_RE` | parser.py:997 `_build_state_event` | `_aspect_state` 로 to/state 계산(XOR 부정), bare 문/창→door/window 개념, binary_sensor+잠금표현→같은 방 lock, `inh_trigger_entity` 재참조, 제어가능 대상 `inh_action_entity` 설정, 방 전파 |
| 11 | 이벤트 인식 확장(결과상/풀리/방 입실·통과) | `_clause_is_event_B/_room_enter_motion_area/_ROOM_ENTER_RE` | parser.py:613 `_clause_is_event` + `_build_event_clause`(796) | 방 입실/통과→그 방 모션 최우선, 결과상·풀리 이벤트 인정. **주의**: 앱의 `_build_arrival`(다인 도착: 마지막=트리거·앞사람=조건) 분기를 오버레이처럼 없애지 말고 유지한 채 앞뒤로 신규 분기만 삽입 — held-out 재측정으로 gold 와 대조(§5.3 패리티 diff 목록에 올림) |
| 12 | 모션 방 전파 | `_build_motion_B` | parser.py:974 `_build_motion` | `default_area` 미설정 시 모션 방을 전파(2줄) |
| 13 | 방 전파 catch-all | `_process_antecedent_B` 말미 | parser.py:681 말미 | 트리거/조건의 첫 유효 엔티티 area 를 `default_area` 로(개별 빌더 누락 보정) |
| 14 | 수치: between·도달어·날씨형·재참조 | `_between/_NUM_CMP_RE/_emit_numeric_aspect_B/_build_numeric_B/_weather_numeric/_clause_has_numeric` | parser.py:787 `_emit_numeric_aspect` + 1020 `_build_numeric` | 비교어에 `도달|닿|찍` 추가, between→above+below 한 노드, 날씨형 전이(습해지/더워지/추워지)→습도·온도 센서+관례 임계, 개념 없음+`inh_trigger_entity` 재참조 진행, `_process_antecedent` 의 promote 판정에 `_clause_has_numeric` 사용 |
| 15 | 액션: 후치 전량("불 싹 다 꺼")·다중 배제("A랑 B 빼고")·잠근 | `_build_action_A3910` | parser.py:1115 `_build_action` | ① `turn_off` 에 `잠근` 추가 ② L1 후치 전량 스코프 블록(라이트 한정+방 한정) ③ 배제 스코프를 다중(방·엔티티 병렬, 스위치/콘센트 도메인 선택)으로 교체 ④ `_SCOPE_RE` 의 `전부(?![가-힣])` 미세수정 |
| 16 | 액션 경계 쉼표 허용 | `_find_action_boundary_B` | parser.py:484 | `tok.rstrip(",.…")` 한 줄 |
| 17 | 주제어 오인(동사 관형형 '-는') 배제 | `_extract_topic_B/_VERB_ADNOMINAL_RE` | parser.py:355 `_extract_topic` | 관형형 정규식 매치 시 `continue` |
| 18 | 조명 라벨 개념(무드등/메인등) | `_A6_CONCEPTS` 잔여분 | gazetteer.py:16 `DEVICE_CONCEPTS` | `무드등/무드 조명/무드조명/메인등/메인 조명/메인조명` 6키만 추가. **bare "등"(1글자)은 제외** — 앱이 의도적으로 "[방]등" 앵커링을 선택했고(test_defect2, gazetteer.py:51 주석) 오버레이의 bare 등은 등록/고등 오매칭 위험. held-out 재측정에서 이 차이로 깨지는 문장이 나오면 그 문장 단위로 재검토(§5.3) |
| 19 | 환기팬→ERV 재매핑(습도/공기질 문맥) | `_remap_erv_fan` | postpass.py (모델 후처리라 §1.3 으로) | — |

가제티어의 A1(모드 동의어)·B1(사람 동의어)·B6(가스밸브 등)·시간측면(`_emit_time_aspect`)·
`_domain_service`(B2/B3/B7)·`_split_daily_no_boundary`·전량/선두 스코프·상속/미해석 처리는
**이미 앱에 반영돼 있다(스킵)**. 슬라이스 착수 시 각 항목을 오버레이본과 diff 로 재확인만 한다.

### 1.3 후처리 이식 — 신규 `backend/nl/postpass.py` (오버레이 1196~2558행 이식)

`apply(result, sentence, normalized, gz, settings, now_fn=None) -> result` 하나를 공개하고,
내부 순서는 오버레이 `parse_patched` 와 동일하게 고정한다:
`_augment_time_calendar → _remap_erv_fan → _augment_else_branch → _augment_negation_not`.

| # | 능력 | 소스 심볼(이식 대상) | 비고(신규 노드 방출 여부) |
|---|---|---|---|
| 20 | sun 트리거·sun_window 조건(일몰/일출±오프셋, 밤창) | `_SUNSET_RE/_SUNRISE_RE/_NIGHTWIN_RE/_sun_offset` + `_augment_time_calendar` 해당 절 | **신규 노드** `sun`/`sun_window` — 슬라이스 S2 이후에만 방출 |
| 21 | weekday(요일 집합·negate)·day_of_month·interval_anchor | `_DAY_MAP/_days_of_token/_detect_weekdays/_detect_day_of_month/_detect_interval` | **신규 노드** — S3 이후 방출. `interval_anchor.anchor` 는 고정상수가 아니라 **`now_fn()` 이 속한 주의 월요일**로 산출(파서가 결정적이도록 `now_fn` 주입, 기본 `datetime.now`) |
| 22 | time_pattern(N분/시간마다) | `_detect_time_pattern` + 강등 로직(상태/수치 트리거→조건) | **신규 노드** — S4 이후 방출 |
| 23 | presence_agg(인원 양화: first/last/any/all·none/any/all) | `_presence_info/_presence_is_condition/_presence_for/_augment_presence` | **신규 노드** — S5 이후 방출. persons 는 `settings.persons` 값 정렬 |
| 24 | 수치 에지 마무리(범위이탈 2트리거·이중에지·between-트리거·단일에지 fallback·센서액션 제거·`_mark_savable`) | `_NUM_EXIT_RE/_NUM_DUAL_RE/_NUM_BETWEEN_TRIG_RE/_numeric_sensor_id/_drop_sensor_service/_mark_savable/_augment_numeric_edge` | 기존 노드만 사용(엔진 무변) |
| 25 | held-for 마무리("N분 넘게 …있으면"→state+for, 오파싱 교정) | `_HELD_FOR_RE/_augment_held_for` | 기존 노드 |
| 26 | 부정 NOT 래퍼("넘지 않으면") | `_NUM_NEG_ABOVE_RE/_NUM_NEG_BELOW_RE/_augment_negation_not` | 기존 `not` 노드 |
| 27 | 액션 파라미터(색 팔레트·색온도·상대밝기·transition) | `_RGB_PALETTE/_KELVIN_PALETTE/_light_service_data/_apply_light_params` | `service.data` 확장(엔진 통과) |
| 28 | toggle(반대로/토글 → domain.toggle/homeassistant.toggle) | `_TOGGLE_RE/_TOGGLABLE/_apply_toggle` | 검증기 `homeassistant` 허용 필요(§3) |
| 29 | notify(인용 메시지·-다고/-라고·채널 폰/스피커·부정) | `_NOTIFY_VERB_RE/_detect_notify` | `notify.notify` + `data.message` — **원문 sentence** 에서 인용부 추출 |
| 30 | repeat 풀구조(count: N번 깜빡→[on,delay1s,off,delay1s]×N / until: 때까지 계속) | `_is_repeat_action/_detect_repeat_count/_build_count_repeat/_build_until_repeat/_repeat_on_off_services` | 기존 `repeat` 노드(엔진 `_run_repeat` 이미 실행) |
| 31 | 한정지속 복원("5분만 켰다가 꺼"→[on,delay,off], 결과상 트리거 승격) | `_REVERT_RE/_REVERT_SERVICES/_apply_revert` | 기존 노드 |
| 32 | else 분기 조립(아니면/평일-주말 대비→한 트리거+if/then/else, 극성 복원) | `_ELSE_MARK_RE/_detect_if_condition/_clause_polarity/_polar_service/_weekday_contrast_then_else/_augment_else_branch` | 기존 `if` 노드(엔진 실행 있음). if 조건에 신규 노드(presence/weekday/sun_window)를 넣는 분기는 해당 슬라이스 이후 활성화 |
| 33 | 달력 문맥 daily 보강·spurious delay/time 제거·time_pattern 강등 등 조립 규칙 | `_augment_time_calendar` 본문 잔여 | — |

**이식 시 결정 사항**: postpass 는 `_primary_subrule`(단일 서브룰만) 게이트, `_QUOTE_SPAN_RE`
공유, `_mark_savable`(오버레이가 트리거를 세우면 ok/confidence 승급 — L2 가 덮지 않게) 를
그대로 유지한다. postpass 가 새 노드를 만들 때 **칩도 함께 추가**한다(score 1.0, 예:
`{"id":"sun:sunset","label":"해 질 때","sublabel":"일몰 트리거"}`) — parse-card 는 sublabel 을
범용 렌더하므로 프론트 무수정. `parser._summary()` 에 신규 노드 서술(해 지면/N분마다/
아무도 없으면/매달 N일…) 분기를 추가한다(parser.py:1548).

### 1.4 매핑 요약 카운트

능력군 **33개** = 신규모듈 2 + 파서 차분 17 + postpass 14. 이 중 신규 엔진 노드를 방출하는
것은 #20~#23 (4개 능력군, 노드 8종 — §2).

---

## 2. 신규 엔진 노드 실행 설계

신규 노드 8종: 트리거 `sun`·`time_pattern`·`presence_agg`(3) + 조건 `sun_window`·`weekday`·
`day_of_month`·`interval_anchor`·`presence_agg`(5). 액션은 신규 노드 없음 — `repeat`/`if`/
`delay` 는 엔진이 이미 실행한다(engine.py:673 if, 692 `_run_repeat`, 666 delay).

### 2.1 SunProvider — 신규 `backend/engine/sun.py`

```python
class SunProvider:
    def __init__(self, settings_ref: dict, now_fn=None): ...
    def events(self, d: date) -> dict:   # {"sunrise": datetime, "sunset": datetime} 로컬 naive
    def next_event(self, event: str, offset_sec: int, now: datetime) -> datetime
```

- NOAA 근사식 순수 파이썬(의존성 0): fractional year → eqtime/decl → zenith 90.833° 시간각.
  좌표는 `settings["location"] = {"latitude","longitude"}`, 부재 시 **서울 37.5665/126.9780**.
- 일 단위 캐시 `{(iso_date, lat, lon): events}`. 극지 무일출/무일몰은 07:00/18:00 고정
  폴백 + `log.warning`(한국 좌표에선 미발생, 결정성 보장용).
- 배선: `app.py` 의 RuleEngine 생성부에서 `sun_provider=SunProvider(settings_store.data, now_fn)`
  를 만들어 엔진 kwarg 로 주입(설정 dict 는 공유 참조라 위치 변경이 자동 반영).
  `handle_settings_put` 에서 `"location" in body` 면 `sun.invalidate()`(캐시 비움) 호출.
- 설정 UI: settings 탭에 위도/경도 2필드(frontend/js settings 섹션 — S2 부속, 기본값 표기).

### 2.2 `sun` 트리거 스케줄 — engine.py

`_schedule_daily`(544) 와 동형의 self-rescheduling. 타이머는 기존 `_daily_timers` dict 를
재사용한다(키 `(rid, flat)` — `stop()`/`_unindex_rule`/`_compile_all` 의 취소 경로가 공짜로
적용된다).

- `_index_rule`(251): `elif typ == "sun": self._schedule_sun(rid, flat, t); has_trigger = True`
- `_schedule_sun(rid, flat, t)`: `when = self._sun.next_event(t["event"], int(t.get("offset") or 0), self._now_fn())`
  → `delay = (when - now).total_seconds()` → `_daily_timers[(rid, flat)] = loop.call_later(max(1,delay), self._on_sun, rid, flat)`.
- `_on_sun(rid, flat)`: `_on_daily`(558) 와 동일 골격 — `_locate` → `_try_fire(rule, si, ti)`
  → finally 재장전(`_schedule_sun`). 다음 이벤트가 오늘 이미 지났으면 `next_event` 가
  내일 것을 돌려주므로 이중발화 없음.
- 재시작/resync: 타이머 재계산만(자동 발화 금지) — daily 와 동일하게 `_compile_all` 이
  재장전하고 pending 저장 대상이 아니다(§4.2 의미론 유지).

### 2.3 `time_pattern` 트리거 — engine.py

- `_index_rule`: `elif typ == "time_pattern": self._schedule_pattern(rid, flat, t); has_trigger = True`
- `_schedule_pattern`: HA `/N` 동형 — 벽시계 값이 N 배수인 다음 시점.
  `minutes: N` → 다음 `minute % N == 0, second=0`; `hours: N` → `hour % N == 0, minute=second=0`;
  `seconds: N` → `second % N == 0`. `_daily_timers` 재사용, 콜백 `_on_pattern` 은 `_try_fire`
  후 재장전. 발화 시 조건은 `_try_fire` 가 재평가하므로 별도 처리 없음.

### 2.4 `presence_agg` 트리거 — engine.py (이벤트 구동 + for 타이머)

- **인덱싱**: `_index_rule` 에서 `persons = t.get("persons") or [person.* in inventory]` 를
  구해 각 pid 를 `targets`(엔티티 인덱스)에 추가, `has_trigger = True`. 인벤토리 person 은
  `self._inventory_fn()` 의 entities 중 `domain == "person"`.
- **에지 판정**: `_eval_rule_triggers`(408) 에 분기 추가 —
  `elif typ == "presence_agg": if entity_id in persons: self._handle_presence(rule, flat, si, ti, t, old_state, new_state)`.
  `_on_event` 가 캐시를 **먼저** 갱신하므로(316), `new_count = Σ cache[pid].state=="home"`,
  `old_count = new_count ± 1`(바뀐 pid 의 old/new home 여부로 보정). quant 별:
  `first: old==0 and new>=1` · `last: old>=1 and new==0` · `any: new>old` ·
  `all: new==len(persons) and old<new`.
- **for(last/all)**: 에지 도달 시 `_arm_timer((rid, flat), duration_to_seconds(t["for"]))`,
  레벨 붕괴 시(`last` 인데 new>0 / `all` 인데 new<len) `_cancel_key`. 만료 콜백은 기존
  `_on_hold_expire`(474) 에 presence 분기를 추가해 **레벨 재확인 후** `_try_fire`.
- **재시작/재연결 의미론 공짜 획득**: `_held_spec`(74) 에
  `if typ == "presence_agg" and t.get("for") and t.get("quant") in ("last","all"): return ("presence", t, target_state, t["for"])`
  를 추가하고(`target_state` = last→"not_home", all→"home"),
  `_held_remaining` 에 presence 분기(모든 pid 가 target_state 이고 남은 시간 = max of
  `cache.hold_remaining(pid, target_state, dur)`, 아니면 None)를 추가하면 fix2(재검증)/
  fix3(누락 장전)/pending 복원 루프가 그대로 presence 를 다룬다.
- `zone.home` 상태값 주의: person 엔티티 상태가 "home"/"not_home"(HA 규약) — mock_data 가
  이미 이 값을 쓴다(person.user "home").

### 2.5 evaluator 순수 조건 5분기 — evaluator.py

`EvalContext.__init__` 에 `sun=None` 파라미터 추가(engine `_ctx()` 가 `self._sun` 전달).
`evaluate_condition`(81) 의 `return False` 폴백 앞에 분기 추가:

```python
if typ == "sun_window":     # §2.1: 자정 걸침 창
    ev = ctx.sun.events(ctx.now().date())
    start = ev[cond.get("after","sunset")] + timedelta(seconds=int(cond.get("after_offset") or 0))
    end   = ev[cond.get("before","sunrise")] + timedelta(seconds=int(cond.get("before_offset") or 0))
    now = ctx.now();  st, en = start.time(), end.time()
    return (st <= now.time() <= en) if st <= en else (now.time() >= st or now.time() <= en)
if typ == "weekday":
    return (_WEEKDAYS[ctx.now().weekday()] in (cond.get("days") or [])) != bool(cond.get("negate"))
if typ == "day_of_month":
    days = cond.get("days");  now = ctx.now()
    return ((now + timedelta(days=1)).day == 1) if days == "last" else (now.day in (days or []))
if typ == "interval_anchor":   # 월요일 정렬 주차 mod
    anchor = date.fromisoformat(cond["anchor"]);  nowd = ctx.now().date()
    monday = lambda d: d - timedelta(days=d.weekday())
    return ((monday(nowd) - monday(anchor)).days // 7) % int(cond.get("interval") or 2) == 0
if typ == "presence_agg":      # 레벨: none/any/all
    persons = cond.get("persons") or [person.* from ctx.inventory_fn()]
    cnt = sum(1 for p in persons if (ctx.cache.get(p) or {}).get("state") == "home")
    q = cond.get("quant");  return {"none": cnt == 0, "any": cnt > 0, "all": cnt == len(persons)}.get(q, False)
```

`ctx.sun is None`(테스트 등 미주입)일 때 sun_window 는 False + 경고 로그(크래시 금지).

### 2.6 HA 자동화 매핑 — 신규 `backend/engine/ha_map.py` + automation_builder 소폭

v1 `automation_builder` 는 이미 `sun`(±HH:MM:SS offset)·`time_pattern`(/N)·`time`(weekday)·
`template`·`if`/`repeat` 을 빌드한다(automation_builder.py:98~121, 141~170, 216~231). 부족한
것은 **v2 방언 → v1 빌더 방언 변환**이다. `ha_map.py` 에 `subrule_to_automation(sub, inventory)
-> model(v1)` 을 두고 SPEC-SCHEMA-90 §5 표를 그대로 구현한다:

| v2 노드 | 변환(ha_map) |
|---|---|
| `sun(event, offset초)` | `{"type":"sun","event":…,"offset": f"{'-' if s<0 else '+'}{HH:MM:SS}"}` (0이면 생략) |
| `time_pattern(minutes N)` | `{"type":"time_pattern","minutes": f"/{N}"}` |
| `presence_agg` first/last | `{"type":"numeric_state","entity_id":"zone.home","above":0 / "below":1}` (+for) |
| `presence_agg` any/all(트리거) | person 별 `{"type":"state","to":"home"}` + all 은 전원 home state 조건 병기 |
| `sun_window` | `{"type":"sun","after":…,"before":…,"after_offset":±HH:MM:SS}` |
| `weekday(days,negate)` | `{"type":"time","weekday": negate ? 여집합 : days}` |
| `day_of_month(days)` | `{"type":"template","value_template":"{{ now().day in [..] }}"}` / last: `"{{ (now()+timedelta(days=1)).day == 1 }}"` |
| `interval_anchor` | template — `"{{ ((as_timestamp(now().date() - timedelta(days=now().weekday())) - as_timestamp(as_datetime('ANCHOR'))) / 604800) | round(0,'floor') % INTERVAL == 0 }}"` (anchor 는 이미 월요일로 저장됨) |
| `presence_agg`(조건 none/any) | `numeric_state zone.home below 1 / above 0` 조건, all → person state and 묶음 |
| `daily(at)` | `{"type":"time","at": at+":00"}` (기존 노드지만 export 시 필요) |
| `segment/mode/state_held/group_held/set_mode` | 기존 미지원 — 현행대로 변환 불가 항목 경고 반환 |

`automation_builder` 자체 변경은 2건: `_REQUIRED_SERVICE_DATA` 에
`"notify.notify": ("message", "알림 메시지를 입력해 주세요.")` 추가(v1 검증에도 안전 강화),
`KNOWN_SERVICES` 에 `"homeassistant": ["turn_on","turn_off","toggle"]` 추가(§3 화이트리스트가
KNOWN_SERVICES 합집합이므로 한 곳 수정으로 관통). ha_map 은 당장 UI 노출이 없어도
**gold↔HA 패리티 테스트**(§5)와 향후 "HA 로 내보내기"의 단일 지점이 된다.

### 2.7 MockHAClient / DEV 폐루프 — mock_data.py

- `_reflect` 에 `rgb_color`/`color_temp_kelvin`/`brightness_step_pct`(현재값±step 클램프
  1~100)/`transition`(속성 저장만) 반영 추가.
- `notify.notify` 호출 로그: `self.notifications: list` 에 `{message,title,target,ts}` 적재
  (+ 상한 100). DEV E2E 에서 알림 검증용.
- `homeassistant.toggle`: target 각 엔티티의 도메인별 on/off 반전.

---

## 3. `validate_rule_model` 확장 — engine/rule_model.py

SPEC-SCHEMA-90 §6 계약 그대로. 도메인 안전(화이트리스트·도메인 일치·실존·널 방어)은 유지.

1. `_UNSUPPORTED = {"sun", "template"}` → `{"template"}` (rule_model.py:21). `sun` 거부 문구
   갱신(:87, :130).
2. `_validate_trigger` 분기 추가:
   - `sun`: `event ∈ {sunrise,sunset}`, `offset` 은 int(bool 제외), `|offset| ≤ 43200`.
   - `time_pattern`: `hours|minutes|seconds` 중 **정확히 1개**, int ≥1, minutes/seconds ≤59,
     hours ≤23.
   - `presence_agg`: `quant ∈ {first,last,any,all}`; `persons` 는 생략 가능, 있으면 비어있지
     않은 list[str] 이고 전원 `person.` 접두 + `valid_ids` 실존; `for` 는 `_check_duration`,
     단 `quant ∈ {last,all}` 일 때만 허용(first/any + for 는 오류).
3. `_validate_condition` 분기 추가:
   - `sun_window`: `after`·`before` 필수, 값 ∈ {sunrise,sunset}, `after_offset`/`before_offset`
     int(생략 가능, |x| ≤ 43200).
   - `weekday`: `days` 는 비어있지 않은 부분집합(`_WEEKDAYS` 셋 재사용), `negate` bool.
   - `day_of_month`: `days == "last"` 또는 1..31 정수의 비어있지 않은 list.
   - `interval_anchor`: `unit == "week"`, `interval` int ≥2, `anchor` 는 `date.fromisoformat`
     파싱 가능.
   - `presence_agg`(조건형): `quant ∈ {none,any,all}` + persons 규칙 동일, `for` 불허.
   - 기존 `time` 조건의 `weekday` 필드 사용 시 경고 1건 추가(차단은 아님 — deprecate).
4. `_ALLOWED_ACTION_DOMAINS` 에 `"homeassistant"` — §2.6 의 KNOWN_SERVICES 추가로 자동 편입
   되는지 확인만(rule_model.py:26 이 `set(KNOWN_SERVICES) | {...}`). 이로써 parser.py:1374 의
   `homeassistant.turn_on` 기존 불일치도 해소.
5. service data 검증 추가(`_scan_service_actions` 확장): `light.turn_on` 계열에서
   `brightness_pct` 1~100, `brightness_step_pct` -100~100·≠0·**pct 와 동시 금지**,
   `rgb_color` [0..255]×3, `color_temp_kelvin` 2000~6500, `transition` ≥0.
   `notify.notify` 는 `data.message` 비어있지 않은 str 필수.
6. `presence_agg.persons`/`sun_window` 등 신규 필드는 **위치별 허용값이 다르므로**(트리거
   first/last/any/all vs 조건 none/any/all) 트리거/조건 분기에서 각각 검증한다 — 공용 헬퍼
   `_check_presence(node, path, errors, valid_ids, allowed_quants, allow_for)`.

**schema90.py 처분**: 앱으로 편입하지 않는다. schema90 은 "엔진이 아직 실행 못 하는 gold 를
정직 분모에 넣기 위한" 과도기 검증기다. S2~S5 완료 시 `validate_rule_model` 이 목표 스키마
전체를 수용하므로, tools 쪽 `augment.load_paraphrases` 의 rescue 경로가 자연히 0건이 되는지
확인 후 schema90 을 **tools 전용 가드로 유지**(차기 의미축 라벨링 때 재사용)하고 docstring 에
"앱 검증기가 전 노드 수용 — 신규 축 라벨링 전용" 을 명시한다. 삭제하지 않는 이유: 90% 이후
사이클(실사용 로그 held-out)에서 같은 패턴이 반복된다.

---

## 4. L2 매처 재설계 + CLI 증류

현 상태(실측): +L2 순리프트 **-1.1%p**(테스트 71.0→69.9, l2_regressions 4·이득 0). 원인은
①라이브러리가 미패치 파서 기준 covered 라 오버레이 이후 세계와 불일치, ②not-ok 게이트로
볼 수 있는 문장과 풀 수 있는 문장이 서로소, ③delexicalize 어휘 빈약. 처방(SPEC-ACCURACY-90
§5 의 5축)을 앱 파일에 다음과 같이 배치한다.

### 4.1 라이브러리 재생성(이식 후 필수 — 이것만으로 ①해소)

- S1~S8 완료 후 `tools/corpus/build_overlay_library.py` 를 **앱 parse 직접 호출**로 바꾼
  `--engine app` 모드(오버레이가 본체에 흡수되면 parse == parse_patched)로 재실행 →
  `out/pattern_library_app.yaml` → `automation_maker/backend/nl/pattern_library.yaml` 로 복사
  하는 원커맨드 `tools/corpus/export_app_library.py` 신설(복사+개수/게이트 sanity 출력).
- covered 판정 채점기는 정직 채점기(structural_match 최신) 사용 — 낡은 채점으로 covered 가
  과대 판정되면 매처가 오답 골드를 흡수한다.

### 4.2 게이트 재설계 — api_v2.py `handle_parse`(217)

```python
if not result.get("ok"):                       # 기존: 흡수 창
    learned → matcher 채택(현행 유지)
elif result.get("confidence", 1.0) < 0.6:      # 신설: shadow-try 창
    m = matcher.match(sentence)
    if m and validate_rule_model(m["model"], inv, modes) == [] \
         and not _struct_equal(m["model"], result["model"]) \
         and _same_subrule_count(m["model"], result["model"]):
        # 채택하되 L1 모델을 result["l1_model"] 로 보존(런로그·회귀 분석용)
        _apply_matched_model(..., source="pattern-shadow")
    # 채택 실패든 성공이든 shadow 계측 필드 기록
result["shadow"] = {"tried": bool, "adopted": bool, "matched_id": ...}
```

- **절대 미덮음 게이트**: `ok and confidence ≥ 0.6` 인 L1 결과는 어떤 경우에도 대체하지
  않는다(런타임의 exact 프록시). postpass 의 `_mark_savable` 이 오버레이-구제 문장을
  ok/0.7 로 승급시키므로 이 게이트가 오프라인 회귀 0 계약과 정합한다.
- `_struct_equal` 은 §4b 로 이식되는 구조 비교 유틸(트리거/조건/액션 canonical 비교)의
  경량 호출 — 동형이면 채택 무의미(합의)라 스킵.
- 계측: `result["shadow"]` + RunLog 에 `l2_shadow` 이벤트(채택 시). evaluate_hybrid 의
  shadow 지표(시도/채택/exact/회귀)와 대응.

### 4.3 delexicalize v2 — pattern_match.py

- `_sym`(147) 심볼 확장: `느껴지|잡히` → EVTON, `풀리|풀려`(액션존 아닌 위치) 처리 정교화,
  `밝게|어둡게` → 값심볼 VAL, `알려|보내` → NOTIF, 사역 wrapper("켜지게 해줘"→ACTON) 정규화
  는 **surface.normalize_surface 를 매처 입력에도 적용**해 공유(입력/템플릿 양쪽 동일 함수).
- 미인식 어절률 지표: `delexicalize` 가 (전체 어절, 심볼/태그로 소비된 어절) 카운트를
  반환하도록 확장 — evaluate_hybrid 리포트 상설 + 앱 DEBUG 로그.
- 2단(struct_replace)의 극성 멀티셋 **완전일치 게이트를 가중 패널티로 완화**: 불일치 심볼당
  0.08 감점(단, ACTON↔ACTOFF·TRIGON↔TRIGOFF 직접 충돌은 여전히 하드 차단). τ 는 dev 재스윕
  후 동결(test 접근 금지).

### 4.4 절 단위 매칭 — pattern_match.py + parser 공유

`parser._find_pivots` 를 pattern_match 에서 재사용해(순환 import 없음 — parser 가 matcher 를
import 하지 않음) pivot ≥2 문장은 서브룰 구간별로 delexicalize·매칭 후 `subrules` 로 조립
한다. 각 구간이 독립 매칭·검증을 통과해야 전체 채택(부분 성공은 버림 — 안전).

### 4.5 CLI 증류(learn) — 라이브러리로의 승격

- **런타임**: `LearnedStore.add` 시 `TemplateMatcher.delexicalize(normalized)` 스트림을
  entry("stream" 필드)에 함께 저장. `TemplateMatcher` 에
  `add_runtime_templates(learned_entries)` 를 추가해 learned 항목을 struct_replace 후보로
  인덱싱(gold=entry.model, 슬롯 재바인딩 없이 **엔티티 실존 재검증만**). 이로써 "비슷하지만
  3-gram 0.85 미만"인 문장도 학습 항목이 흡수한다. 배선: app.py:449 매처 생성 후
  `matcher.add_runtime_templates(learned_store.all())`, learn/confirm·learned/delete 시 재호출.
- **프리워밍(배포 전 배치)**: 신규 `tools/corpus/distill.py` — gap_library 상위 골격 +
  리서치 롱테일 문장을 `cli_normalize`(캐시 우선, `--cli-budget`)로 표준문형화 → 앱 parse 로
  재파싱 ok 인 것만 `(원문→정규형→model)` 튜플로 `backend/nl/pattern_library.yaml` 에
  `origin: distilled` 템플릿으로 병합. **held-out 문장 md5 블랙리스트 검사 필수**(오염 방지).
- 계약 재확인: CLI 발동은 ①사용자의 [AI로 분석해서 배우기] 클릭(learn 엔드포인트)과
  ②설정 backend != off 일 때의 unresolved 칩 병합(_try_llm_merge, 기존)뿐. 자동 신규 경로
  없음. 목표 지표: 실사용 CLI 발동률 <10%, 기여 <5%p(hybrid_report 상설).

## 4b. 정직 측정 이식

- **구조 비교 유틸**: `tools/corpus/structural_match.py` 를
  `automation_maker/tests/structural_compare.py` 로 복사(수정 없음 — stdlib 전용이라 그대로
  동작; 단일 소스 원칙을 위해 tools 쪽이 앱 tests 본을 import 하도록 역전:
  `tools/corpus/structural_match.py` → `from tests.structural_compare import *` 셔임으로 축소).
  §4.2 의 `_struct_equal` 도 이 유틸의 `normalize_model` 비교를 사용.
- **앱 회귀 테스트**: 신규 `automation_maker/tests/test_heldout_regression.py` +
  `tests/data/heldout_subset.yaml`. 서브셋 선정 기준(고정·문서화): solved_by ruleA 승리 중
  축별 대표 각 5~8문장(총 ~90) + **금지문 적대 23문장 전부** + 기존 test_accuracy_port 미포함
  분만. 각 항목 `{sentence, gold, axis}` — 실행: MockHAClient 인벤토리로 parse() →
  `structural_compare.compare` verdict == exact 단언, 금지문은 "forbidden 매치 액션 미생성"
  단언(exact 아님 — SPEC §4.4 판정 그대로 이식).
- **전량 측정은 tools 에 유지**: `evaluate_hybrid.py` 에 `--l1 app` 모드(오버레이 대신 앱
  parse 를 L1 로) 추가. 이식 완료 판정 = `--l1 app` 의 L1 열이 오버레이 L1A(74.2%/466,
  72.8% test)와 **±0.5%p 이내**. 패리티 확인 후 parser_overlay 를 "역사적 비교용" 주석과
  함께 동결(삭제하지 않음 — 차기 축 프로토타이핑 템플릿).

---

## 5. 회귀·테스트 전략

1. **기존 390 pytest 회귀 0**: 슬라이스마다 `python3 -m pytest automation_maker/tests -q`.
   충돌 예상 지점을 선제 식별해 둔다 — ⓐ `_detect_mode` 기본값 변경(#7)은
   test_nl_parser 의 모드 조건 케이스와, ⓑ 무드등 개념(#18)은 test_defect2(미해석 대상
   조용한 상속 금지)와, ⓒ arrival 라우팅(#11)은 다인 도착 테스트와 부딪힐 수 있다.
   충돌 시 **기존 테스트의 의도(안전 불변식)가 우선** — 오버레이 쪽을 앱 불변식에 맞게
   조정하고 held-out 영향을 재측정해 기록한다.
2. **A/B 패리티 하네스(이식 기간 한정)**: 신규 `tools/corpus/parity_check.py` — held-out
   520문장을 (a) parse_patched(오버레이), (b) 앱 parse 로 각각 돌려 `normalize_model` 동형
   여부를 문장 단위 diff 로 출력. 슬라이스마다 "의도된 차이 목록"(#7/#11/#18 등)만 남고
   나머지 0 이어야 통과. 완료 후 §4b 의 `--l1 app` 게이트로 대체.
3. **신규 노드 엔진 실행 테스트**(tests/test_engine_*.py 확장):
   - sun: `now_fn` 고정 + SunProvider 스텁(고정 일출/일몰)으로 `_schedule_sun` 지연 계산,
     발화 후 재장전, `_unindex_rule` 취소, resync 시 무발화.
   - time_pattern: `/30` 이 :00·:30 에 발화, 경계 직전/직후 케이스.
   - presence_agg: MockEventSource.inject 로 person 상태 전이 → first/last/any/all 에지,
     last+for 는 중도 귀가 시 취소, pending 저장/복원(엔진 재시작 시뮬레이션 — 기존
     test_engine 의 pending 패턴 재사용).
   - evaluator: weekday(negate)/day_of_month(last·경계 28~31)/interval_anchor(앵커 주·차주)/
     sun_window(자정 걸침)/presence 레벨 — now_fn 파라미터화 순수 단위 테스트.
   - 검증기: 신규 노드 필드별 정상/경계/오류(§3 규칙 1:1), homeassistant.toggle 통과,
     notify message 누락 거부, brightness 동시 지정 거부.
   - ha_map: SPEC §5 표의 노드별 왕복 스냅샷(gold 노드 → HA config dict 비교).
4. **금지문 적대 세트 상설**: test_heldout_regression 의 금지문 23 + 새 표현 발견 시 추가.
   판정은 "정반대 액션 미생성"(§4.4) — parse 가 ok=True 를 내면 즉시 실패.
5. **폴백 배선 회귀 6종 유지**(HYBRID-PARSER §10 계약): L1 ok 시 L2 미발동(고신뢰) ·
   shadow 창 조건 · L2 채택 시 검증 통과 필수 · learned 우선순위 · LLM off 시 무호출 ·
   출처 캡션(source 필드). shadow-try 도입으로 케이스 2종 추가(shadow 채택/기각).
6. **DEV 폐루프 E2E**: DEV_MODE 서버 기동 → parse→저장→`api/v2/dev/state` person/센서 주입
   → runlog 발화 확인(sun 은 now_fn 조작 불가하므로 단위 테스트로 한정, presence/pattern 은
   E2E 포함). 스크래치패드 e2e_*.py 패턴 재사용.

---

## 6. 수직 슬라이스 실행 순서

각 슬라이스 = 1 커밋(가능하면), 게이트 = §0.4. "빌더"는 ha_map/automation_builder,
"검증"은 rule_model, "측정"은 evaluate_hybrid 축별 실측 기록.

| S | 내용 | 파일 | 산출/게이트 |
|---|---|---|---|
| **S0** | 측정 기반: structural_compare 앱 편입(§4b) + parity_check.py + evaluate_hybrid `--l1 app` | tests/structural_compare.py, tools/corpus | 기준선 리포트(현 앱 L1) 커밋 |
| **S1** | L1 코어(기존 노드만): surface.py(정규화+금지문) + parser 차분 #3~#17 + postpass 골격(#19·#24~#31 중 기존노드 항목: 수치에지·held-for·NOT·light params·revert·repeat·notify·toggle 단 homeassistant.toggle 은 S6 전까지 domain.toggle 만) + test_accuracy_port 확장 | nl/surface.py, nl/parser.py, nl/gazetteer.py, nl/postpass.py, tests | held-out 최대 리프트 슬라이스. 금지문 0/23·pytest 회귀 0·패리티 diff = 의도 목록만 |
| **S2** | sun 축: SunProvider + engine `_schedule_sun` + evaluator sun_window + 검증기 sun/sun_window + ha_map sun + postpass #20 활성화 + settings.location(+UI 필드) | engine/sun.py, engine/engine.py, engine/evaluator.py, engine/rule_model.py, engine/ha_map.py, nl/postpass.py, app.py, frontend settings | sun-offset·밤창 카테고리 실측, 엔진 타이머 테스트 |
| **S3** | 달력 축: evaluator weekday/day_of_month/interval_anchor + 검증기 + ha_map(여집합·template) + postpass #21(anchor=now 주 월요일, now_fn 배선) | evaluator.py, rule_model.py, ha_map.py, postpass.py, parser.py(parse 시그니처 now_fn) | weekday-calendar·interval 실측 |
| **S4** | 주기 축: engine `_schedule_pattern` + 검증기 time_pattern + ha_map + postpass #22 | engine.py, rule_model.py, ha_map.py, postpass.py | interval-repeat 실측, /N 경계 테스트 |
| **S5** | 프레즌스 축: engine presence 에지+for(§2.4, `_held_spec` 확장) + evaluator 레벨 + 검증기 + ha_map + postpass #23 | engine.py, evaluator.py, rule_model.py, ha_map.py, postpass.py | presence-quantifier 실측, 재시작 복원 테스트 |
| **S6** | 액션 마감: 검증기 light data 범위·notify message·homeassistant 도메인 + builder KNOWN_SERVICES/_REQUIRED_SERVICE_DATA + MockHA reflect/notify/toggle + postpass #28 homeassistant.toggle 허용 | rule_model.py, automation_builder.py, mock_data.py, postpass.py | action-params·quoted-message 실측, DEV E2E 알림 확인 |
| **S7** | else/if 조립 + 잔여 다중절: postpass #32 전체 활성화(신규 노드 if 조건 포함) + parser._summary 신규 노드 서술 + postpass 칩 방출 | postpass.py, parser.py | else-branch·multiclause 실측 |
| **S8** | 패리티 게이트: evaluate_hybrid `--l1 app` ≥ 74.2%±0.5(466)·회귀 0 확인 → parser_overlay 동결 선언 | tools/corpus | **74.2% 재현 리포트 커밋** |
| **S9** | L2 재설계: 라이브러리 재생성(export_app_library) + shadow-try 게이트 + delexicalize v2 + 절 단위 매칭 + learned 런타임 템플릿 | nl/pattern_match.py, api_v2.py, nl/learn.py, app.py, tools | **L2 순리프트 > 0** & l2_regressions 0 (음수면 τ dev 재스윕, 그래도 음수면 shadow 창 축소 롤백) |
| **S10** | CLI 증류 프리워밍 + 지표 상설 + 문서(DOCS/CHANGELOG/SPEC 갱신) | tools/corpus/distill.py, docs, 번역 | CLI 발동률 <10%·기여 <5%p 지표 리포트, held-out 오염 블랙리스트 검사 로그 |

의존성: S2~S5 는 상호 독립(순서 교환 가능하나 held-out 비중 순 권장), S7 은 S2/S3/S5 의
노드를 if 조건으로 쓰므로 그 뒤. S9 은 S8 패리티 이후(라이브러리가 최종 파서 기준이어야 함).

### 최대 리스크 3 + 완화

1. **엔진 스케줄링·상태복원 결함**(S2/S4/S5): 재연결 resync 직후 오발화, sun 타이머 이중
   장전, presence for 의 pending 복원 누락 → 새벽에 조명이 켜지는 실사용 사고로 직결.
   완화: 신규 타이머를 전부 기존 수명주기 dict(`_daily_timers`/`_for_timers`)에 태워 취소
   경로를 상속, "재시작·재연결은 재계산만, 자동 발화 금지"를 각 축 테스트로 고정,
   presence 는 `_held_spec` 확장으로 기존 fix1~3 루프를 재사용(새 복원 코드 최소화).
2. **파서 본체 통합 회귀**(S1): monkeypatch → 본체 병합에서 v3.1 이후 앱 결함수정(§결함2/3,
   ⓐⓑⓒ)과 오버레이 규칙이 충돌(식별된 충돌점 #7/#11/#18). 완화: 차분 이식 원칙 + 패리티
   하네스(S0)로 문장 단위 diff 를 슬라이스마다 검토, 충돌 시 앱 불변식 우선 + held-out
   영향 실측 기록, 기존 390 테스트 회귀 0 게이트.
3. **L2 게이트 완화·검증기 확장의 안전망 약화**(S9/S2~S6): shadow-try 가 저신뢰 창에서
   오답 대체(오프라인 실측 -4 의 재발), 신규 필드 검증 부실 시 매처/LLM/학습 경로로 위험
   모델 저장. 완화: 절대 미덮음 게이트(ok·conf≥0.6) + 채택 전 validate_rule_model 전면 통과
   + 구조 동형 스킵, l2_regressions>0 즉시 롤백 계약, 검증기는 필드 화이트리스트·범위·persons
   실존까지 §3 표대로 강제하고 적대 테스트(§5.3 검증기 항목) 동반.

---

## 7. 핵심 요약

- **포팅 대상**: 능력군 **33개**(신규 모듈 2 · 파서 차분 17 · postpass 이식 14). 원본은
  `tools/corpus/normalize90.py`(382행)·`parser_overlay.py`(2558행)이고, 목적지는
  `backend/nl/{surface.py(신규), postpass.py(신규), parser.py, gazetteer.py}` +
  `backend/engine/{sun.py(신규), ha_map.py(신규), engine.py, evaluator.py, rule_model.py}` +
  `automation_builder.py`·`mock_data.py`·`api_v2.py`·`pattern_match.py`·`learn.py`·`app.py`.
- **신규 엔진 노드 8종**: 트리거 `sun`·`time_pattern`·`presence_agg` + 조건 `sun_window`·
  `weekday`·`day_of_month`·`interval_anchor`·`presence_agg(레벨)` — 스케줄형 2종은 daily
  타이머 패턴 재사용, presence 는 이벤트 에지+`_held_spec` 확장, 조건 5종은 evaluator 순수
  평가. `repeat`/`if`/`delay`/`notify`/`toggle` 은 기존 실행 경로 재사용(검증·빌더·Mock 보강).
- **수직 슬라이스 11단계**: S0 측정기반 → S1 L1 코어(최대 리프트) → S2 sun → S3 달력 →
  S4 주기 → S5 프레즌스 → S6 액션 마감 → S7 else/다중절 → S8 패리티 게이트(74.2% 재현) →
  S9 L2 재설계(순리프트>0) → S10 CLI 증류·문서.
- **최대 리스크 3**: ① 엔진 스케줄링·재시작 복원(기존 타이머 수명주기 재사용으로 완화)
  ② 파서 본체 통합 회귀(차분 이식 + A/B 패리티 하네스 + 390 테스트 게이트)
  ③ L2 완화·검증 확장의 안전망 약화(절대 미덮음 게이트 + 전면 검증 + 즉시 롤백 계약).
