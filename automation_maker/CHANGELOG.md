# 변경 이력

## 1.0.0

첫 릴리스.

- 방 → 카테고리 → 엔티티 3단 피커, 한글 초성 검색 지원
- 트리거 카테고리 위저드(시간/사람 이동/센서 감지/수치 변화)로 새 자동화 시작
- 트리거·조건·액션 카드 스택 편집기 (`~할 때` / `~인 동안` / `실행`)
- 트리거 8종(state, numeric_state, time, time_pattern, sun, zone, template,
  homeassistant), 조건(state, numeric_state, time, sun, zone, template,
  trigger, and/or/not 조합), 액션 10종(service, delay, wait_template,
  wait_for_trigger, condition, choose, if, repeat, parallel, stop) 지원
- 저장 전 한국어 자연어 요약 및 YAML 미리보기
- 자동화 목록 조회, 생성/수정/삭제, 켜기·끄기 토글, 수동 실행
- 가스밸브·잠금장치 등 안전 관련 액션 포함 시 저장 확인 경고 표시
- Home Assistant Ingress 전용 접근 제어(게이트웨이 IP만 허용)
- `DEV_MODE=1`로 한국 아파트 가정 목데이터를 이용한 로컬 개발 모드 지원
