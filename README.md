# KBO 세이버 기록실

1982년부터 지금까지 KBO 1군 경기에 나온 모든 선수의 정규시즌·포스트시즌 기록과 세이버 지표(wOBA, wRC+, FIP, WAR 등)를 보는 사이트예요.
GitHub Actions가 시즌 중(3~11월) 매일 밤 23:40에 KBO 기록실에서 올해 기록을 다시 받아와 지표를 계산하고 사이트를 새로 배포해요.

## 폴더 구성

| 경로 | 내용 |
|---|---|
| `site/index.html` | 사이트 화면 전부 (HTML·CSS·JS 한 파일) |
| `site/data/*.json` | 계산이 끝난 기록 (사이트가 읽는 파일) |
| `data/kbo_raw.json` | KBO 기록실에서 받아온 원본 |
| `scraper/scrape.mjs` | KBO 기록 수집기 (Node.js) |
| `scraper/process.py` | 원본 → 세이버 지표 계산 (Python) |
| `.github/workflows/update.yml` | 매일 자동 업데이트 + 배포 설정 |

## 내 컴퓨터에서 실행하기 (VS Code 터미널)

```bash
npm install                 # 처음 한 번
npm run scrape              # 올해 기록만 다시 받기 (몇 분)
npm run build               # 세이버 지표 계산
npm run serve               # http://localhost:8000 에서 사이트 보기
```

- 특정 시즌만 다시 받기: `node scraper/scrape.mjs --years 2024-2026`
- 전부 다시 받기: `npm run scrape:all` (약 30분)

## 자동 업데이트가 도는지 확인하기

GitHub 저장소의 **Actions** 탭 → "기록 업데이트 & 배포"에서 실행 기록을 볼 수 있어요.
바로 돌려보고 싶으면 **Run workflow** 버튼을 누르면 돼요.

## 지표 계산 방식

사이트의 "지표 설명" 탭에 정리돼 있어요. 요약하면 FanGraphs 방식(fWAR)을 KBO 공식 기록에 맞게 적용했고,
다른 사이트(스탯티즈, KBReport 등)와 WAR 값이 조금 다를 수 있어요.

## 데이터 출처

KBO 공식 기록실 (koreabaseball.com). 공개 배포 전 KBO 이용약관을 확인하세요.
