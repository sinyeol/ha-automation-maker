# 앱 L1 정직 측정 + A/B 패리티 (parity_check)

- held-out n = 520
- **앱 L1 (backend.nl.parser.parse) exact = 57.3% (298/520)**
- 오버레이 L1A (parse_patched, 목표 74.2%) exact = 71.0% (369/520)
- 갭(오버레이 − 앱) = +13.7%p

## 금지문 안전 게이트 (special:prohibition)
- 전수 23문장 · **forbidden 액션 방출(misfire) = 0/23** · ok=True = 0

## 카테고리별 (dataset)

| 그룹 | 앱 L1 | 오버레이 L1A | 갭 | n |
|---|---:|---:|---:|---:|
| heldout | 58.6% (273/466) | 72.3% (337/466) | +13.7%p | 466 |
| para_hard | 46.3% (25/54) | 59.3% (32/54) | +13.0%p | 54 |

## 카테고리별 (difficulty)

| 그룹 | 앱 L1 | 오버레이 L1A | 갭 | n |
|---|---:|---:|---:|---:|
| ? | 46.3% (25/54) | 59.3% (32/54) | +13.0%p | 54 |
| easy | 72.5% (103/142) | 87.3% (124/142) | +14.8%p | 142 |
| hard | 48.2% (67/139) | 56.1% (78/139) | +7.9%p | 139 |
| medium | 55.7% (103/185) | 73.0% (135/185) | +17.3%p | 185 |

## 카테고리별 (tag)

| 그룹 | 앱 L1 | 오버레이 L1A | 갭 | n |
|---|---:|---:|---:|---:|
| ? | 65.4% (134/205) | 78.0% (160/205) | +12.7%p | 205 |
| action_params | 62.5% (15/24) | 87.5% (21/24) | +25.0%p | 24 |
| aspect_condition | 77.3% (17/22) | 86.4% (19/22) | +9.1%p | 22 |
| duration_revert | 59.1% (13/22) | 72.7% (16/22) | +13.6%p | 22 |
| else_branch | 4.2% (1/24) | 25.0% (6/24) | +20.8%p | 24 |
| interval_repeat | 30.4% (7/23) | 17.4% (4/23) | -13.0%p | 23 |
| negation_exception | 33.3% (8/24) | 45.8% (11/24) | +12.5%p | 24 |
| numeric_edge_between | 79.2% (19/24) | 83.3% (20/24) | +4.2%p | 24 |
| postposed_clause | 48.0% (12/25) | 64.0% (16/25) | +16.0%p | 25 |
| presence_quantifier | 0.0% (0/25) | 80.0% (20/25) | +80.0%p | 25 |
| quoted_message | 60.0% (15/25) | 72.0% (18/25) | +12.0%p | 25 |
| sun_offset | 86.4% (19/22) | 86.4% (19/22) | +0.0%p | 22 |
| surface_variation | 60.0% (15/25) | 60.0% (15/25) | +0.0%p | 25 |
| weather_external | 60.0% (6/10) | 60.0% (6/10) | +0.0%p | 10 |
| weekday_calendar | 85.0% (17/20) | 90.0% (18/20) | +5.0%p | 20 |

## 패리티 diff (앱 L1 ≠ 오버레이 L1A): 89문장
- 앱 L1 exact 인데 오버레이 non-exact: ['ho90_interval_repeat_08~p126.0', 'ho90_interval_repeat_09~p127.0', 'ho90_interval_repeat_10~p128.0', 'ho90_interval_repeat_11~p129.0', 'ho90_interval_repeat_13~p131.0', 'ho90_interval_repeat_15~p133.0', 'ho90_interval_repeat_22~p140.0', 'ho90_surface_variation_10~p317.0', 'ho90_postposed_clause_15~p347.0']
