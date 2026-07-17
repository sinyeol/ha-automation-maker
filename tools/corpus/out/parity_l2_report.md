# 앱 L1 vs 앱 L1+L2 정직 측정 (parity_check --l2, S9)

- held-out n = 520  ·  pattern_library 템플릿 45개
- **앱 L1        exact = 65.6% (341/520)**
- **앱 L1+L2     exact = 66.0% (343/520)**
- **L2 순리프트 = +0.4%p (+2문장)**
- **l2_regressions = 0** (L1 exact→L1+L2 non-exact, 0 이어야 함)
- l2_gains = 2 (L1 miss→L1+L2 exact)
- 채택 분포 = {'pattern': 7}  ·  shadow(시도/채택) = 5/0

## 금지문 게이트(L1+L2, gate #3)
- 전수 23문장 · **forbidden 방출(misfire) = 0/23** · ok=True = 0
