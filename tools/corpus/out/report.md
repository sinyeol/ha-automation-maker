# 패턴 라이브러리 종합 리포트

## 1. 코퍼스 규모
- 총 1597문장 (grammar 1435 · paraphrase 162)
- 영역별: climate-set 40, cover 52, media 100, misparse 14, mode 360, multiclause 312, numeric 160, safety 58, scope-exclude 28, single 22, target 349, time 14, time-range 80, value 8
- gold_invalid(격리): 0

## 2. 현재 파서 커버리지
- exact 653 · partial 504 · fail 440 · 전체 정확률 40.9%

| 소스 | exact | partial | fail | 정확률 |
|---|---:|---:|---:|---:|
| grammar | 609 | 463 | 363 | 42.4% |
| paraphrase | 44 | 41 | 77 | 27.2% |

> §7.7 낙관편향: grammar 는 템플릿 동형이라 낙관적. 일반화 지표는 **paraphrase 정확률** 우선.

## 3. 상위 갭 패턴 (빈도순)
| # | 빈도 | 영역 | 오류태그 | 추상 패턴 | 예시 |
|---:|---:|---|---|---|---|
| 1 | 23 | climate-set | extra_node,value_mismatch | `{DEVICE}{J} {TEMP} 이상이면 {DEVICE}{J} {TEMP}{J} 설정해줘` | 거실 온도가 18도 이상이면 에어컨을 26도로 설정해줘 |
| 2 | 11 | multiclause | wrong_node_type | `{ROOM} {DEVICE}{J} 감지되면 {DEVICE}{J} 켜주고 {DUR}간 {DEVICE}{J} 없으면 꺼` | 거실 인기척이 감지되면 작은방 조명을 켜주고 5분간 움직임이 없으면 꺼 |
| 3 | 10 | climate-set | extra_node,value_mismatch | `{DEVICE}{J} {TEMP} 이상이면 {ROOM} {DEVICE}{J} {TEMP}{J} 설정해줘` | 안방 온도가 30도 이상이면 거실 냉방을 20도로 설정해줘 |
| 4 | 8 | multiclause | extra_node,missing_node | `{DEVICE}{J} 감지되면 {ROOM} {DEVICE}{J} 켜주고 {DUR}간 {DEVICE}{J} 없으면 꺼줘` | 욕실 모션이 감지되면 거실 불을 켜주고 5분간 움직임이 없으면 꺼줘 |
| 5 | 7 | climate-set | extra_node,value_mismatch | `{DEVICE}{J} 영하 {TEMP} 이상이면 {DEVICE}{J} {TEMP}{J} 설정해줘` | 안방 온도가 영하 5도 이상이면 거실 에어컨을 22도로 설정해줘 |
| 6 | 6 | multiclause | extra_node,missing_node | `{ROOM} {DEVICE}{J} 감지되면 {DEVICE}{J} 켜주고 {DUR}간 {DEVICE}{J} 없으면 끄다` | 거실 움직임이 감지되면 안방 조명을 켜주고 5분간 움직임이 없으면 끄다 |
| 7 | 6 | multiclause | extra_node,missing_node | `{DEVICE}{J} 감지되면 {ROOM} {DEVICE}{J} 켜주고 {DUR}간 {DEVICE}{J} 없으면 꺼주세요` | 안방 모션이 감지되면 현관 불을 켜주고 5분간 움직임이 없으면 꺼주세요 |
| 8 | 6 | numeric | missing_node | `{DEVICE}{J} {PERCENT} 이상이면 환기장치를 켜줘` | 거실 습도가 70% 이상이면 환기장치를 켜줘 |
| 9 | 6 | numeric | missing_node | `{DEVICE}{J} {PERCENT} 이상이면 환기장치를 켜주세요` | 안방 습도가 40% 이상이면 환기장치를 켜주세요 |
| 10 | 5 | target | entity_confusion | `{ROOM} {DEVICE}{J} 감지되면 전부 {ROOM} {DEVICE}{J} 꺼줘` | 거실 움직임이 감지되면 전부 현관 불을 꺼줘 |
| 11 | 5 | target | value_mismatch | `{ROOM} {DEVICE}{J} 감지되면 {ROOM} {DEVICE}{J} 최대로 켜줘` | 안방 움직임이 감지되면 화장실 조명을 최대로 켜줘 |
| 12 | 5 | numeric | missing_node | `{DEVICE}{J} {PERCENT} 이상이면 환기장치를 켜` | 안방 습도가 40% 이상이면 환기장치를 켜 |
| 13 | 4 | mode | missing_node | `수면 모드이고 {ROOM} {DEVICE}{J} 감지되면 {ROOM} {DEVICE}{J} 켜` | 수면 모드이고 거실 움직임이 감지되면 현관 불을 켜 |
| 14 | 4 | mode | entity_confusion,missing_node | `자기 전 모드이고 {ROOM} {DEVICE}{J} 작동하면 {ROOM}등을 꺼줘` | 자기 전 모드이고 거실 움직임이 작동하면 거실등을 꺼줘 |
| 15 | 4 | mode | missing_node | `수면모드이고 {ROOM} {DEVICE}{J} 작동하면 {ROOM} {DEVICE}{J} 꺼줘` | 수면모드이고 거실 움직임이 작동하면 거실 불을 꺼줘 |

## 4. 하이브리드 권고
- 규칙 우선(현행 parser.py) → 낮은 confidence/미해결 시 pattern_library 템플릿 매처 → 최후 LLM few-shot (docs/HYBRID-PARSER.md 참조).
- 추가 후보 패턴은 `out/gap_library.yaml` 참조(총 629 클러스터).
- 커버 상태별 시드 템플릿은 `out/pattern_library.yaml`(하이브리드 데이터 자산).
