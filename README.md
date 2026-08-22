# quant-data — 퀀트 프로젝트 데이터 파이프라인

"주식브로커" 프로젝트(모의 1억, 무레버리지, 스윙/중기 퀀트)의 데이터 인프라.

## 구조

| 경로 | 내용 |
|---|---|
| `pipeline/universe.py` | 유니버스 갱신 — S&P500 시점별(1996~) + NASDAQ100 현재 |
| `pipeline/fetch_prices.py` | 일봉 수집 (2005~, 수정주가) → `data/prices/{연도}.parquet` |
| `pipeline/fetch_fundamentals.py` | 재무 지표 월간 스냅샷 → `data/fundamentals/` |
| `pipeline/validate.py` | 품질 검증 → `reports/data_quality.md`, 실패 시 워크플로 실패 처리 |
| `.github/workflows/daily_data.yml` | 매일 미국 장 마감 후(UTC 22:30) 자동 실행 |

## 문서화된 데이터 한계

1. **상장폐지 종목의 가격 데이터 부재** — 시점별 구성종목 리스트로 유니버스의 생존 편향은 제거했지만, 야후는 상장폐지 종목 가격을 대부분 제공하지 않아 잔여 편향이 있음. 백테스트 리포트에 명시하고 성과를 보수적으로 해석.
2. **재무 히스토리 얕음** — 멀티팩터 전략은 축적 스냅샷 + 제한된 과거만 검증 가능.
3. **NASDAQ100은 현재 구성만** — 시점별 히스토리 없음.

## 운영

- 자동: 평일 장 마감 후 1회 수집·검증·커밋
- 수동: Actions 탭 → daily-data → Run workflow (최초 백필 시 사용)
- 실패 시: Actions 탭에 빨간 X → data_quality.md 확인
