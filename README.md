# HA Automation Maker

Home Assistant용 **한국어 우선(Korean-first) 자동화 애드온**입니다. 이제 두
가지 방식으로 자동화를 만들 수 있습니다.

- **루틴**(기본 화면): "화장실은 5분 동안 움직임이 없으면 환풍기와 조명을
  꺼줘"처럼 한국어 문장을 쓰면 애드온이 스스로 해석해 규칙을 만듭니다. 이
  규칙은 Home Assistant의 `automations.yaml`을 쓰지 않고, 애드온이 직접
  상태를 구독하고 실행하는 **자체 규칙 엔진**으로 동작합니다. 규칙·설정은
  애드온 데이터 폴더(`/data`)에 저장되어 Home Assistant 백업에 자동으로
  포함됩니다.
- **HA 자동화**(별도 탭): 기존 방식대로 방(거실·안방·작은방 등)과 센서
  카테고리(조명/감지기/환경 센서/난방 등) 단위로 엔티티를 탐색하며, 코드 한
  줄 없이 트리거·조건·액션을 조합해 Home Assistant 표준 자동화를 만드는
  화면입니다.

v3부터는 상태를 가진 모드 변수, 한 문장에 여러 규칙을 담는 문법, 파서가
이해하지 못한 문장을 단어 단위로 직접 지정하는 편집기, Claude 구독 CLI
연동까지 지원합니다.

주요 특징:

- 자연어 문장으로 루틴을 등록하고, 애매한 부분만 **해석 확인 카드**에서
  사람이 확정. 파서가 전혀 이해하지 못한 문장은 **직접 지정** 편집기에서
  단어 단위로 역할을 지정해 규칙을 만들 수 있음
- **상태를 가진 모드 변수**(예: "슬립 모드")를 트리거·조건으로 사용
  ("슬립모드가 켜지면…", "…슬립모드이고…"), 설정 화면에서 모드 추가·삭제·
  수동 켜기/끄기 가능
- 한 문장에 **여러 조건-액션 쌍**을 담는 문법 지원(예: "켜주고 3분 동안
  없으면 꺼줘") — 한 문장이 카드 하나로, 안에는 규칙 여러 개가 들어감
- 전역 시간 변수(새벽/아침/낮/저녁/밤 시간대, 계절, 평일/주말/공휴일)를
  기준으로 시간 표현을 해석, 시간대 경계는 설정 화면에서 조정 가능
- 선택적 **LLM 해석 백엔드**(끄기 / Anthropic API 키 / Claude 구독 CLI 중
  선택, 기본은 끄기로 완전히 로컬 파서만 사용)
- **한글 초성 검색**을 포함한 엔티티 탐색(예: `ㄱㅅ` → "거실")
- **방 → 카테고리 → 엔티티** 3단 피커로 빈 화면 공포 없이 시작하는 위저드
  (HA 자동화 탭)
- 저장 전 **자연어 요약**과 **YAML 미리보기**로 무엇이 만들어지는지 확인
  (HA 자동화 탭)
- 가스밸브·잠금장치 등 안전과 관련된 동작에는 별도 확인 문구 표시

## 스크린샷

> 스크린샷은 준비 중입니다. 실제 실행 화면은 추후 이 위치에 추가될 예정입니다.

| 목록 | 위저드 | 편집기 |
|---|---|---|
| _(준비 중)_ | _(준비 중)_ | _(준비 중)_ |

## 설치 방법

### 방법 1. 로컬 애드온으로 설치 (Samba/SSH)

Home Assistant OS/Supervised 환경에서 애드온 폴더를 직접 복사하는 방법입니다.

1. Samba 애드온 또는 SSH로 Home Assistant 서버에 접속합니다.
2. 이 저장소의 `automation_maker/` 폴더 전체를 Home Assistant의
   `/addons/automation_maker/` 경로로 복사합니다.
   ```bash
   # 예: SSH/SCP를 사용하는 경우
   scp -r automation_maker root@homeassistant.local:/addons/automation_maker
   ```
3. Home Assistant 화면에서 **설정 → 애드온 → 애드온 스토어**로 이동해
   우측 상단 메뉴(⋮) → **저장소 새로고침**을 실행합니다.
4. 애드온 스토어 목록 하단의 **Local add-ons(로컬 저장소)** 섹션에
   "HA Automation Maker"가 나타나면 설치 → 시작합니다.
5. 사이드바에 "자동화 메이커" 패널이 표시됩니다.

### 방법 2. GitHub 저장소 URL로 설치

1. Home Assistant 화면에서 **설정 → 애드온 → 애드온 스토어**로 이동합니다.
2. 우측 상단 메뉴(⋮) → **저장소(Repositories)**를 선택합니다.
3. 이 저장소의 URL(예: `https://github.com/sinyeol/ha-automation-maker`)을 추가합니다.
4. 저장소 목록에서 "HA Automation Maker"를 찾아 설치 → 시작합니다.

두 방법 모두 설치 후 애드온 **설정(구성)** 탭에서 다음 옵션을 선택적으로
지정할 수 있습니다(둘 다 비워두거나 기본값 그대로 두어도 바로 사용할 수
있습니다).

| 옵션 | 설명 |
|---|---|
| `log_level` | 로그 레벨(`debug`/`info`/`warning`/`error`, 기본 `info`) |
| `llm_backend` | 루틴 해석이 애매할 때만 보조로 쓰이는 LLM 해석 백엔드. `off`(기본, 완전 로컬) / `api`(Anthropic API 키) / `cli`(Claude 구독 CLI) 중 선택 |
| `anthropic_api_key` | `llm_backend`가 `api`일 때 쓰는 Anthropic API 키(선택) |
| `claude_code_oauth_token` | `llm_backend`가 `cli`일 때 쓰는 Claude 구독 CLI 토큰. `claude setup-token` 명령으로 발급받아 붙여넣습니다(선택) |

`llm_backend`를 끄기로 두면(기본값) 위 두 값은 필요 없고 외부로 나가는 요청도
없습니다. 구독 CLI 설정 절차와 API 키·구독 토큰 우선순위 주의사항은
[automation_maker/DOCS.md](automation_maker/DOCS.md)의 "6. LLM 해석
백엔드"를 참고하세요.

## 개발 모드로 실행하기

실제 Home Assistant 인스턴스 없이 로컬에서 프론트엔드/백엔드를 확인하려면
`DEV_MODE=1` 환경변수로 목데이터(한국 아파트 가정: 거실·안방·작은방·주방·현관·
베란다·욕실) 기반 백엔드를 띄울 수 있습니다.

```bash
cd "automation_maker"
python3 -m venv venv && source venv/bin/activate   # 선택 사항
pip install -r backend/requirements.txt
DEV_MODE=1 python3 -m backend.app
```

기본 포트는 `8099`이며, `PORT` 환경변수로 변경할 수 있습니다. 서버가 뜨면
브라우저에서 `http://localhost:8099` 로 접속합니다. 개발 모드에서는 Ingress IP
검사(`172.30.32.2` / `127.0.0.1` 제한)를 생략하고, 실제 Home Assistant 대신
인메모리 목데이터(`backend/mock_data.py`)를 사용합니다.

루틴(자연어 규칙) 엔진의 데이터는 기본적으로 `automation_maker/devdata/`
폴더(환경변수 `DATA_DIR`로 변경 가능)에 저장되며, 실제 애드온에서는 `/data`에
저장되어 Home Assistant 백업에 포함됩니다. LLM 해석 백엔드를 개발 모드에서
테스트하려면 환경변수로 `LLM_BACKEND`(`off`/`api`/`cli`)를 지정하고, `api`면
`ANTHROPIC_API_KEY`를, `cli`면 `CLAUDE_CODE_OAUTH_TOKEN`(또는 이미 `claude`
CLI에 로그인된 상태)을 지정하면 됩니다(모두 선택 사항이며, 설정 화면에서도
같은 값을 켜고 끌 수 있습니다).

### 테스트 실행

```bash
cd "automation_maker"
python -m pytest tests/ -q
```

## 저장소 구조

```
HA Automation Maker/
├── README.md              # 이 문서
├── LICENSE                # MIT 라이선스
├── PLAN.md                # 기획/리서치 문서
├── repository.yaml        # HA 애드온 저장소 메타 (GitHub 설치용)
├── docs/SPEC.md            # v1(HA 자동화 탭) 구현 계약(내부 개발 문서)
├── docs/SPEC-V2.md         # v2(루틴/자체 엔진) 구현 계약(내부 개발 문서)
├── docs/SPEC-V3.md         # v3(상태 모드·다중 규칙·수동 매핑·Claude CLI) 구현 계약
└── automation_maker/       # /addons 에 복사되는 애드온 본체
    ├── config.yaml          # 애드온 매니페스트
    ├── Dockerfile / run.sh
    ├── README.md / DOCS.md / CHANGELOG.md
    ├── backend/             # aiohttp REST 서버 (Python)
    │   ├── engine/          # 자체 규칙 엔진(상태 캐시·평가기·전역 시간 변수 등)
    │   └── nl/              # 한국어 문장 → 규칙 파서(+ 선택적 AI 해석 보조)
    ├── frontend/            # vanilla JS 프론트엔드 (빌드 없음)
    │   └── js/views/         # routines.js(루틴) · settings.js(설정) ·
    │                          # list.js/wizard.js/editor.js(HA 자동화)
    └── tests/               # pytest
```

## 라이선스

이 프로젝트는 [MIT 라이선스](./LICENSE)를 따릅니다.
