# HA Automation Maker

Home Assistant용 **한국어 우선(Korean-first) 자동화 빌더 애드온**입니다.
방(거실·안방·작은방 등)과 센서 카테고리(조명/감지기/환경 센서/난방 등) 단위로 엔티티를
탐색하며, 코드 한 줄 없이 트리거·조건·액션을 조합해 자동화를 만들 수 있습니다.

기본 홈 어시스턴트 자동화 편집기와 달리 다음을 목표로 합니다.

- **한글 초성 검색**을 포함한 엔티티 탐색(예: `ㄱㅅ` → "거실")
- **방 → 카테고리 → 엔티티** 3단 피커로 빈 화면 공포 없이 시작하는 위저드
- 저장 전 **자연어 요약**과 **YAML 미리보기**로 무엇이 만들어지는지 확인
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

두 방법 모두 설치 후에는 애드온 설정에서 별도로 지정할 옵션이 없습니다
(`options: {}`). 시작만 하면 바로 사용할 수 있습니다.

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
├── docs/SPEC.md            # 구현 계약(내부 개발 문서)
└── automation_maker/       # /addons 에 복사되는 애드온 본체
    ├── config.yaml          # 애드온 매니페스트
    ├── Dockerfile / run.sh
    ├── README.md / DOCS.md / CHANGELOG.md
    ├── backend/             # aiohttp REST 서버 (Python)
    ├── frontend/            # vanilla JS 프론트엔드 (빌드 없음)
    └── tests/               # pytest
```

## 라이선스

이 프로젝트는 [MIT 라이선스](./LICENSE)를 따릅니다.
