# SPEC-V3 — 상태 모드 · 다중 조건-액션 문법 · 수동 매핑 · Claude CLI 연동

v3 목표(사용자 요청 4가지):
1. **상태를 가진 모드 변수**: "슬립모드가 켜지면 …" 처럼 모드를 트리거/조건으로 사용
2. **다중 조건-액션 문법 확정**: 한 문장에 (조건→액션) 쌍이 여러 개 ("… 켜주고 3분 동안 … 없으면 꺼줘")
3. **수동 단어 매핑**: 파서가 이해 못 한 문장을 **단어별로** 역할/엔티티에 직접 지정
4. **Claude CLI(구독) 연동**: LLM 해석 백엔드를 off / API 키 / 구독 CLI 로 선택(on/off)

계약서다. §번호의 파일/시그니처/JSON을 정확히 지킨다. v1(docs/SPEC.md)·v2(docs/SPEC-V2.md)는 유효하며
v3는 그 위에 얹는다. 루트: `/root/projects/HA Automation Maker/automation_maker/`.
사용자 문자열 한국어, 식별자 영어, 외부 CDN 금지, DEV_MODE에서 전체 기능 동작.

---

## 1. 상태 모드 변수 (엔진 + 설정)

### 1.1 설정 스키마 확장 (settings.json `modes`)
모드를 **상태(on/off)를 가진 내부 변수**로 승격한다. 하위호환 유지.
```json
"modes": {
  "슬립 모드": {
    "initial": "off",
    "on_action":  {"action": "scene.turn_on", "target": {"entity_id": ["scene.sleep_mode"]}},
    "off_action": null
  }
}
```
- `initial`: "on"|"off" (기본 "off").
- `on_action`/`off_action`: 모드가 그 상태로 바뀔 때 **추가로** 실행할 서비스(선택, null 가능).
- **마이그레이션**: 기존 v2 형식 `{"action","target","data"}`(on_action 없음)은 로드 시
  `on_action = {action,target,data}`, `off_action=null`, `initial="off"` 로 변환(엔진/설정 로더가 처리).

### 1.2 엔진 모드 상태 (`backend/engine/engine.py` + `modes.py` 신규)
- `backend/engine/modes.py`: `class ModeState` — settings.modes와 `/data/modes_state.json`(JsonStore)에서
  상태를 로드/저장. 시그니처:
  ```python
  class ModeState:
      def __init__(self, settings: dict, store: JsonStore): ...
      def names(self) -> list[str]
      def get(self, name: str) -> str            # "on"|"off" (미정의 모드는 "off")
      def snapshot(self) -> dict                 # {name: "on"|"off"}
      def set(self, name: str, on: bool) -> bool # 상태 변경, 변했으면 True (persist는 호출측)
      def sync_settings(self, settings) -> None  # 설정 변경 시 새 모드 초기화·삭제된 모드 제거
  ```
- RuleEngine이 ModeState를 보유. `set_mode` 액션과 수동 토글이 이를 갱신.
- `RuleEngine.set_mode(name, on: bool, context: str)`:
  1. `changed = mode_state.set(name, on)` → persist(save_soon).
  2. 변했으면 해당 상태의 side-effect(on_action/off_action) 서비스 호출(있으면).
  3. 변했으면 **mode 트리거 pubsub**: 트리거가 `mode(name, to==새상태)`인 서브룰을 평가·발화
     (에지 의미론 — 실제 전이일 때만, 쿨다운 적용). 재귀 set_mode도 정상 처리(깊이 제한 8).

### 1.3 새 노드 타입 (rule_model.py 검증 + 파서 생성 + 엔진 평가)
| 종류 | 노드 | 의미 |
|---|---|---|
| 트리거 | `{"type":"mode","mode":"슬립 모드","to":"on"\|"off"}` | 모드가 to로 전이될 때 |
| 조건 | `{"type":"mode","mode":"슬립 모드","state":"on"\|"off"}` | 모드가 현재 state |
| 액션 | `{"type":"set_mode","mode":"슬립 모드","to":"on"\|"off"}` | 모드 상태 설정(+side-effect) |
- 검증: mode 노드의 `mode`는 settings.modes에 존재해야 함(없으면 오류 "설정에 없는 모드예요: X").
  단 파서/수동매핑이 새 모드를 즉석 생성하는 경우를 위해, 저장 시 settings에 없으면
  **경고 후 자동 등록**(initial off, side-effect 없음) — api_v2가 처리(§5).
- 엔진 status()에 `"modes": mode_state.snapshot()` 추가.

### 1.4 모드 API (§5에 통합) & 수동 토글
- `GET api/v2/status` → 기존 + `"modes": {"슬립 모드":"off", ...}`.
- `POST api/v2/modes/{name}` body `{"on":bool}` → `engine.set_mode(name,on,"manual")` → `{"modes":{...}}`.
- `GET api/v2/modes` → `{"modes":[{"name","state","initial","has_on_action","has_off_action"}]}`.

---

## 2. 다중 조건-액션 문법 확정 (RuleGroup / subrules)

### 2.1 확정 문법 (이 절이 사용자 요청 "문법 확정" 산출물)
문장은 **하나 이상의 (조건→액션) 쌍**으로 구성되며, 각 쌍은 내부적으로 하나의 서브룰이 된다.
한 문장 = 하나의 카드 = 하나의 RuleModel(여러 subrule 포함).

```
문장      := [주제절]? 규칙쌍 ( 연결 규칙쌍 )*
규칙쌍     := 조건부 종결어미('(으)면'|'되면'|'거든') 액션부
조건부     := 절 ( ('-고'|'-며'|'-는데'|',') 절 )*          # 트리거 1개 + 조건 0개+
액션부     := 액션 ( '-고' 액션 )*                          # 뒤에 또 '면'이 오지 않는 '-고'만 액션연결
연결      := 액션부의 마지막 명령동사에 붙은 '-고/-며'    # 그 뒤에 새 '면'절이 오면 규칙 경계
```

**핵심 분해 규칙 (경계 판정)** — `-고`의 두 얼굴:
- **액션 연결** "A하고 B해줘": 뒤따르는 것이 또 다른 **명령**(동사가 켜/꺼/틀…로 끝나는 액션)이면 같은 쌍.
- **규칙 경계** "A해주고 [조건]-면 B": 뒤따르는 구간에 또 다른 **조건 종결어미('면' 등)**가 있으면
  거기서 새 규칙쌍이 시작. 이 `-고`는 규칙 경계.

**분해 알고리즘 (파서 구현 계약):**
1. 문장에서 동사-검증된 조건 종결어미('(으)면/되면/거든') 위치를 **모두** 찾는다 → pivot 목록. pivot N개 → 서브룰 N개.
2. `주제절`(맨 앞의 "X는/은")은 있으면 추출해 **모든 서브룰의 기본 대상(default target)** 후보로 둔다.
3. 서브룰[0].조건 = 문장 시작(주제 이후) ~ pivot[0]. 서브룰[i>0].조건 = (서브룰[i-1] 액션 끝) ~ pivot[i].
4. 서브룰[i].액션 = pivot[i] 직후 ~ 다음 규칙 경계(또는 문장 끝). 규칙 경계는
   "명령동사+('-고'|'-며') 직후에 다음 조건 구간이 시작되는 지점".
5. **컨텍스트 상속**: 서브룰[i]의 액션에 대상이 생략되면, 같은 그룹의 앞 서브룰에서 **마지막으로 언급된
   동일 도메인 엔티티**를 상속한다(예: 2번째 "꺼줘"가 1번째 "거실조명"을 상속). 조건의 엔티티도
   앞 절에서 언급된 것을 재참조("모션이 없으면"의 모션 = 앞의 거실 모션)한다.

### 2.2 RuleModel 확장 (subrules)
```json
RuleModel = {
  "alias": "", "mode": "single",
  "subrules": [
    {"triggers":[...], "condition_mode":"and", "conditions":[...], "actions":[...]},
    ...
  ]
}
```
- **하위호환**: `subrules`가 없으면 엔진은 최상위 `triggers/conditions/condition_mode/actions`를
  단일 서브룰로 취급한다. 기존 v2 규칙·테스트 전부 그대로 동작.
- 파서는 단일 쌍 문장은 기존처럼 최상위 필드로, 다중 쌍 문장은 `subrules`로 방출한다
  (혹은 항상 subrules로 방출하되 1개면 최상위로 평탄화 — 구현 재량, 단 하위호환 유지).

### 2.3 엔진 반영 (engine.py)
- 규칙 로드/인덱싱/발화가 `_subrules(model)` 헬퍼로 서브룰을 순회하도록 일반화
  (`_subrules` = model.get("subrules") or [{top-level 4필드}]).
- 트리거 인덱스 키에 `(rule_id, subrule_index)` 포함. 타이머 키도 서브룰 인덱스 포함.
- 발화·쿨다운·runlog은 서브룰 단위. runlog 문장 컨텍스트는 rule.sentence 유지.
- 검증(validate_rule_model)도 subrules를 순회(각 서브룰에 트리거 1개 이상 등 기존 규칙 적용).

---

## 3. 수동 단어 매핑 (핵심 요청)

파서가 이해 못 하거나 사용자가 원할 때, 문장을 **토큰(단어)** 으로 쪼개 각 토큰에 역할·대상을 직접 지정한다.
사용자 예시 분해가 목표(새벽→전역변수, 슬립모드→전역변수, 거실모션→엔티티, 작동하면→상태값 …).

### 3.1 백엔드: 토큰화 + 모델 빌더 (`backend/nl/manual.py` 신규)
```python
def tokenize(sentence: str) -> list[dict]
# [{"index":0,"text":"거실조명은","core":"거실조명","start":0,"end":5}] — 공백 분절 + 조사 분리(core)

def suggest_roles(tokens, gazetteer, settings) -> list[dict]
# 각 토큰에 파서 힌트로 후보 역할·대상을 채운 초안(파서 gazetteer.match 재사용).

def build_model_from_tokens(assignments: list[dict], inventory, settings) -> dict
# assignments = [{"index","role","ref","value","state","boundary":bool}]  (§3.2)
# → {"ok":bool, "model": RuleModel(subrules 가능), "summary":str, "errors":[...], "warnings":[...]}
```

### 3.2 토큰 역할 taxonomy (프론트 셀렉트 = 백엔드 빌더 계약)
| role | 부가입력 | 빌더 동작 |
|---|---|---|
| `ignore` | — | 무시 |
| `trigger_entity` | ref=entity_id | 새 트리거 대상 시작(직후 `event_state`가 상태 결정) |
| `condition_entity` | ref=entity_id | 새 조건 대상(직후 `event_state`) |
| `action_target` | ref=entity_id | 액션 대상(직후/직전 `action_verb`) |
| `event_state` | state=on\|off\|detected\|clear\|open\|close | 가장 가까운 앞 entity의 상태. 트리거존이면 state/state_held, 조건존이면 state/held |
| `numeric` | value, cmp=above\|below | 앞 entity의 수치 조건/트리거(numeric_state) |
| `duration` | value(초/분/시간) | 앞 트리거를 *_held로, 또는 앞 액션에 delay 부여(위치로 판정) |
| `segment` | ref=dawn\|…\|night | time_segment 조건(또는 "되면"이면 segment 트리거) |
| `mode_ref` | ref=모드명, state=on\|off | 모드 조건(또는 트리거 to) |
| `daytype` | ref=weekday\|weekend\|holiday | day_type 조건 |
| `season` | ref=spring\|…\|winter | season 조건 |
| `value` | value, kind=brightness\|temperature\|fan_mode | 가장 가까운 action_target/verb의 data |
| `action_verb` | verb=on\|off\|toggle\|open\|close\|set_mode | 액션 동작. set_mode면 ref=모드명·state |
| `boundary` | — | 여기서부터 **다음 서브룰**(규칙 경계). 토큰 role과 별개로 `boundary:true` 플래그로도 지정 가능 |

- 빌더는 토큰을 **순서대로** 훑어 트리거/조건/액션을 누적하고, `boundary`에서 서브룰을 분리한다.
  트리거존/조건존/액션존 구분: `action_verb`가 나오기 전 = 트리거·조건존(첫 event_state 있는 entity가
  트리거, 나머지가 조건), `action_verb` 이후 = 액션존. duration은 앞이 트리거면 held화, 앞이 액션이면 delay.
- 결과가 유효하지 않아도(트리거/액션 누락) errors를 채워 반환(프론트가 안내).

### 3.3 프론트: 수동 매핑 에디터 (`frontend/js/components/manual-map.js` 신규)
- 해석 카드(parse-card.js) 하단에 **"직접 지정"** 토글 버튼. 켜면 manual-map 에디터를 인라인 표시.
- 토큰을 칩 행으로 렌더. 각 토큰 탭 → 바텀시트: **역할 셀렉트**(위 taxonomy의 한국어 라벨) +
  역할에 따른 부가 컨트롤(엔티티 피커 재사용 / 값 입력 / 상태 on·off 세그먼트 / 전역변수·모드·시간대 셀렉트).
  "여기서 규칙 나눔" 체크(boundary).
- 상단에 "초안 채우기"(suggest_roles 호출) 버튼 — 파서 힌트로 역할 자동 채움 후 사용자가 수정.
- 하단 실시간 요약(build_model_from_tokens의 summary) + [루틴으로 저장](유효할 때만).
- 역할 라벨(한국어): 무시 / 트리거 대상 / 조건 대상 / 액션 대상 / 상태값 / 수치 조건 / 지속시간 /
  시간대 / 모드 / 요일 / 계절 / 값(밝기·온도) / 동작 / 규칙 나눔.

### 3.4 API
- `POST api/v2/tokenize` body `{"sentence"}` → `{"tokens":[...], "suggestions":[...]}`
  (tokenize + suggest_roles).
- `POST api/v2/build` body `{"sentence","assignments":[...]}` → build_model_from_tokens 결과
  (`{ok,model,summary,errors,warnings}`). 저장은 기존 `POST api/v2/rules`가 model을 받으므로 재사용.

---

## 4. Claude CLI(구독) / API / off — LLM 해석 백엔드

### 4.1 설정·옵션
- `config.yaml` options/schema:
  ```yaml
  options: { log_level: "info", llm_backend: "off" }
  schema:
    log_level: "list(debug|info|warning|error)"
    llm_backend: "list(off|api|cli)"
    anthropic_api_key: "password?"
    claude_code_oauth_token: "password?"
  ```
- `run.sh`: `anthropic_api_key` → `ANTHROPIC_API_KEY`, `claude_code_oauth_token` →
  `CLAUDE_CODE_OAUTH_TOKEN`, `llm_backend` → `LLM_BACKEND` 환경 export(있을 때만).
- settings.json에도 `"llm": {"backend": "off"}` 를 두어 **UI에서 on/off 전환** 가능(설정 우선,
  없으면 환경 LLM_BACKEND, 그것도 없으면 off). API 키/토큰 자체는 settings에 저장하지 않음(환경만).

### 4.2 llm_assist.py 리팩터 (백엔드 디스패치)
```python
async def llm_parse(sentence, digest, settings, *, backend: str, api_key: str, oauth_token: str) -> dict | None
```
- `backend=="off"` → None.
- `backend=="api"` → 기존 Anthropic Messages HTTP(aiohttp), model `claude-haiku-4-5-20251001`.
  api_key 없으면 None.
- `backend=="cli"` → **subprocess로 `claude` 헤드리스 호출**(§4.3). oauth_token(또는 api_key) 없거나
  바이너리 없으면 None.
- 공통: 20~30초 타임아웃, 실패는 조용히 None(로컬 파서 결과 사용). 응답 JSON의 entity_id는
  inventory 실존 검증 후 없는 건 unresolved 강등(기존 규약 유지). **API 키/토큰을 로그에 절대 출력 금지.**

### 4.3 CLI 호출 규약 (`backend/nl/cli_client.py` 신규) — 리서치로 전부 실측 확정
- 바이너리 탐색: `CLAUDE_BIN` 환경 → PATH의 `claude`. `claude auth status`(JSON, exit 0=로그인)로 헬스체크.
- **인증 함정(필수)**: 인증 우선순위에서 `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`이
  `CLAUDE_CODE_OAUTH_TOKEN`을 **이긴다**. 따라서 CLI 호출 env에서 그 둘을 **반드시 제거**하고
  `CLAUDE_CODE_OAUTH_TOKEN`(구독 토큰)만 주입. `--bare`는 OAuth 토큰을 무시하므로 **사용 금지**.
  DEV_MODE에서 토큰 없이 `/login` 상태(이 개발 컨테이너 = Max 구독 로그인)면 토큰 없이도 동작.
- 호출(검증 완료된 플래그 그대로, 순수 파싱·도구 없음·격리):
  ```
  claude -p "<user prompt>" \
    --output-format json \
    --json-schema '<STRUCT_SCHEMA>' \
    --system-prompt "<파서 시스템 프롬프트>" \
    --tools "" --strict-mcp-config --setting-sources "" \
    --disable-slash-commands --no-session-persistence \
    --max-turns 2 --model haiku
  ```
  env: `CLAUDE_CODE_OAUTH_TOKEN`(있으면), `DISABLE_AUTOUPDATER=1`. cwd=미리 만든 **빈 디렉터리**
  (`/tmp/claude-work` 또는 `/data/claude-work`) — cwd의 CLAUDE.md/.claude 자동로드 방지.
- `--json-schema`는 얕게: `{"type":"object","properties":{"model":{"type":"object"},
  "warnings":{"type":"array","items":{"type":"string"}}},"required":["model"]}`.
- 출력: stdout 한 줄 JSON. **`.structured_output`**(스키마 준수 객체)에서 `model`/`warnings`를 읽는다
  (`.result`는 코드펜스가 섞여 옴 — 쓰지 말 것). 오류도 stdout JSON으로 오므로
  `returncode != 0 or data.get("is_error")` → None. (오류 예: `is_error:true, api_error_status:401,
  result:"...Invalid bearer token"`.)
- `asyncio.create_subprocess_exec` + `wait_for(60초)`, stdin 미사용, 동시 실행 세마포어(1~2개).
  반환은 api 백엔드와 **동일 계약** `{"model":..., "warnings":[...]}` (없으면 None).

### 4.4 CLI 설치 (이미지에 굽기 — apk 공식 저장소)
- Dockerfile에 Alpine 공식 저장소로 `claude-code` 설치(네이티브 바이너리, **Node 런타임 불필요**):
  ```dockerfile
  RUN wget -O /etc/apk/keys/claude-code.rsa.pub https://downloads.claude.ai/keys/claude-code.rsa.pub \
   && echo "https://downloads.claude.ai/claude-code/apk/stable" >> /etc/apk/repositories \
   && apk add --no-cache claude-code
  ```
- 이미지 +~250MB 감수(사용자가 CLI 옵션을 명시 요청). `DISABLE_AUTOUPDATER=1`로 버전 누적 방지
  (run.sh에서 export). 크리덴셜 파일 불필요(토큰 env 주입). `nodejs`/`npm`/lazy-install 불필요.
- 네트워크 사정으로 apk 설치가 실패해도 이미지 빌드가 깨지지 않게 이 RUN은 실패 허용(`|| true`)하고,
  cli_client는 바이너리 부재 시 조용히 None(백엔드 off와 동일). DEV/CI에서 apk 저장소 접근 불가 시에도
  나머지 기능은 정상.

### 4.5 parse 엔드포인트 반영
- 백엔드 선택: settings.llm.backend(우선) → env LLM_BACKEND → "off".
- 로컬 파서 confidence<0.6 또는 unresolved 존재 && backend!=off 일 때 llm_parse 호출.
- 응답에 `"used_llm":bool, "llm_backend":"api"|"cli"|null`.

---

## 5. API 요약 (`backend/api_v2.py` 확장 — 이 파일만)
기존 12개 + 추가:
| 메서드/경로 | 동작 |
|---|---|
| GET `api/v2/modes` | 모드 목록·상태 |
| POST `api/v2/modes/{name}` | 수동 on/off 토글 |
| POST `api/v2/tokenize` | 문장 토큰화 + 역할 초안 |
| POST `api/v2/build` | 토큰 매핑 → RuleModel |
- `POST api/v2/rules` 저장 시 model이 참조하는 모드가 settings에 없으면 자동 등록(initial off) +
  warnings, 그리고 gazetteer/mode_state에 반영.
- status/settings 응답에 modes·llm.backend·llm_available(api_key/oauth_token/cli 준비 여부) 포함.
- 오류 봉투·게이팅(dev/state)·입력 상한(문장 500자, 규칙 200개)은 v2 규약 유지.

---

## 6. 파서 반영 (`backend/nl/`)
- **모드 트리거/조건**: gazetteer.mode_surfaces로 표면형 매칭. "슬립모드가 켜지면/켜지고"→ 트리거
  mode(to on), "슬립모드가 꺼지면"→ to off, "슬립모드이고/슬립모드면/슬립모드일 때"→ 조건 mode(on),
  "슬립모드 아니면/슬립모드가 아니고"→ 조건 mode(off). 액션 "슬립 모드로 바꿔/켜"→ set_mode(on)
  (기존 side-effect도 유지되도록 set_mode가 side-effect 실행).
- **다중 쌍**: §2.1 알고리즘으로 subrules 방출 + 컨텍스트 상속.
- **"집의 모든 조명"/"모든 조명"**: 인벤토리 전체 light.* 로 확장(기존 "모든 조명"과 동일).
- 골든 문장 2개(테스트 대상):
  - A: "슬립모드가 켜지면 집의 모든 조명을 꺼줘" → subrule 1개:
    trigger mode(슬립 모드,on); actions light.turn_off(전체 light.*).
  - B: "새벽에 슬립모드이고 거실 모션이 작동하면 거실조명을 10% 켜주고 3분 동안 모션이 없으면 꺼줘"
    → subrules 2개:
    (1) trigger state(binary_sensor.living_room_motion→on); conditions [time_segment[dawn],
        mode(슬립 모드,on)]; action light.turn_on(light.living_room_main,brightness_pct 10)
    (2) trigger state_held(binary_sensor.living_room_motion→off,3분); action
        light.turn_off(light.living_room_main)  ← 컨텍스트 상속
- 기존 골든 5문장·회귀 테스트 전부 계속 통과.

## 7. 통합/패키징 (`app.py`, `mock_data.py`, config.yaml, run.sh, requirements, Dockerfile)
- app.py: ModeState 생성·배선(app["engine"]에 포함), settings.modes 마이그레이션 호출,
  register_v2_routes는 그대로(새 라우트는 api_v2 내부에서 등록). engine.start/stop에 mode_state flush.
- mock_data.py: 이미 scene.sleep_mode 존재. 변경 최소(모드는 settings 기반). 기존 엔티티/카운트 어서션 유지.
- config.yaml/run.sh: §4.1. Dockerfile: nodejs·npm 추가. requirements: 변경 없음(holidays 유지).
- 기본 settings의 modes를 신형(initial/on_action)으로: "슬립 모드"→ on_action scene.sleep_mode.

## 8. 프론트 반영
- settings.js: **LLM 백엔드** 섹션(끄기/API 키/구독 CLI 라디오 + 준비상태 표시: 키·토큰·CLI설치 여부),
  **모드 관리** 섹션(모드 목록·현재 상태 토글·초기값·side-effect scene 지정·추가/삭제).
- routines.js: 규칙 카드가 subrules 다중 쌍이면 부제에 "규칙 2개" 표기, 모드 트리거 아이콘(🌙 등).
  헤더 상태줄에 활성 모드 표시("슬립 모드 켜짐") 선택.
- parse-card.js: 하단 "직접 지정" 토글 → manual-map.js 에디터. used_llm 캡션에 백엔드 표기.

## 9. 테스트 (`tests/`)
- test_modes.py: ModeState set/get/persist/마이그레이션, 엔진 set_mode pubsub(mode 트리거 발화),
  side-effect 호출, mode 조건 평가.
- test_multiclause.py: §2.1 분해 — 골든 B가 subrules 2개로 정확히, 컨텍스트 상속, 단일 쌍 하위호환.
- test_manual.py: tokenize/suggest_roles/build_model_from_tokens — 사용자 예시 토큰 분해가
  올바른 subrules 모델을 만드는지(골든 B 토큰 매핑 재현), boundary 분리.
- test_llm_backend.py: 백엔드 디스패치(off→None, api 경로 모킹, cli 경로 subprocess 모킹으로
  플래그·env 검증, 실패 시 None), 로그에 키/토큰 미노출.
- test_api_v2 확장: modes 토글, tokenize/build, 모드 자동등록.
- 기존 242개 전부 통과.

## 10. 공통
- 새 파이썬 의존성 없음(cli_client는 표준 subprocess). Node/claude-code는 런타임 설치.
- 파일당 600줄 목표. asyncio task 강참조. 사용자 문자열 한국어.
- **보안**: cli 호출은 파일/도구 접근 없이 순수 텍스트 파싱만. 토큰/키 로그 금지.
  set_mode 재귀 깊이 제한. 저장 모델의 서비스 도메인 화이트리스트(v2) 유지.
