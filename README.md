# 내 계좌 대시보드 (메리츠증권 Open API)

메리츠증권 Open API로 내 계좌를 **평일마다 자동 조회**해서 총자산 추이, 수익률, 보유 종목,
거래 내역, 실현손익을 한 화면에 보여 줍니다.

- **수집**: GitHub Actions가 평일 16:40(KST)에 `collector/`를 실행합니다. 조회 전용이라 주문·환전 API는 호출하지 않습니다.
- **저장**: 결과는 `docs/data.enc.json`에 **비밀번호로 암호화(AES-256-GCM)** 해서 커밋됩니다.
- **화면**: GitHub Pages가 `docs/index.html`을 배포하고, 비밀번호는 브라우저 안에서만 쓰여 데이터를 복호화합니다.

화면 구성은 `docs/index.html?demo`에서 가짜 샘플 데이터로 먼저 볼 수 있습니다.

## 대시보드에 나오는 것

| 항목 | 출처 API |
|---|---|
| 총자산·주식평가·예수금·외화예수금 | `valuation` (내 계좌 자산평가), `deposit` |
| 보유 종목(국내+해외, 원화 환산 손익·비중), 해외 종목의 현지통화 수익률 | `holdings`, `ovs_holdings` |
| 통화별 외화 잔고·환율 | `ovs_currencies` |
| 거래 내역(매수·매도·입출금·배당 등) | `transactions` (하루 단위로 조회, 연속조회 처리) |
| 국내 실현손익(일별·올해 합계), 해외 실현손익(건별, 원화 기준) | `profit_loss`, `profit_loss_summary`, `ovs_profit_loss` |
| 총자산 추이, 누적 수익률, 전일 대비 손익 | 매일 저장한 스냅샷으로 계산 |

### 수익률 계산 방식

- **누적 수익률(시간가중, TWR)**: 매일 `(오늘 총자산 − 오늘까지 순입금) ÷ 어제 총자산 − 1`을 구해 곱으로 이어 붙입니다.
  입금·출금은 수익으로 잡히지 않습니다. 입출금 여부는 거래 내역의 거래유형코드 `01`(입금)·`02`(출금)로 판단하며,
  환전 행은 제외합니다.
- **손익금액**: `현재 총자산 − 시작 총자산 − 기간 순입금`.
- **평가손익**: 보유 종목의 원화 환산 평가손익 합계(해외 종목은 환율 효과 포함). 현지통화 기준 수익률도 함께 표시합니다.
- Open API에는 과거 일자의 총자산을 주는 기능이 없어서 **총자산 추이와 수익률은 처음 실행한 날부터** 쌓입니다.
  거래 내역과 실현손익은 `BACKFILL_FROM`(워크플로 기본값 `2026-01-01`)부터 받아 옵니다. 나중에 이 날짜를 더 앞당기면
  다음 실행에서 빈 구간만 추가로 받습니다.

## 설정 방법

### 1. 앱키 발급
[메리츠증권 Open API 포털](https://openapi.imeritz.com)에서 App Key/Secret을 발급받습니다. 앱키에 계좌가 묶여 있어
계좌번호는 넣지 않습니다. 계좌 조회 API들(자산평가, 종목별 평가, 입출금 내역, 실현손익, 해외 잔고)의 사용 권한도 신청해 두세요.
권한이 없는 API는 `0760` 오류가 나며, 대시보드 상단에 경고로 표시됩니다(나머지는 정상 갱신).

### 2. 저장소 Secrets 등록
**Settings → Secrets and variables → Actions → New repository secret**

| 이름 | 값 |
|---|---|
| `MERITZ_APP_KEY` | 발급받은 앱키 |
| `MERITZ_APP_SECRET` | 발급받은 시크릿 |
| `DASHBOARD_PASSWORD` | 대시보드를 열 때 쓸 비밀번호 (길게 정하세요) |

선택: 원금 대비 수익률을 보려면 Secret `PRINCIPAL`(원금, 예: `20000000`)을 추가하세요. 처음 넣은 날이 기준일이 되고,
그 뒤 입금은 원금에 더하고 출금은 뺍니다. 기준일을 직접 정하려면 Secret `PRINCIPAL_DATE`(`YYYY-MM-DD`)를 함께 넣습니다.
Secret 으로 넣는 이유는 공개 저장소의 Actions 로그에 금액이 남지 않게 하려는 것입니다.

선택: **Variables** 탭에 `BACKFILL_FROM`(거래 내역·실현손익을 받기 시작할 날, `YYYY-MM-DD`)을 넣으면 기본값 `2026-01-01` 대신 씁니다.

`DASHBOARD_PASSWORD`가 없으면 수집이 중단됩니다. GitHub Pages는 저장소가 비공개여도 주소를 아는 누구나 열 수 있어서,
계좌 정보를 평문으로 올리지 않기 위해서입니다.

### 3. GitHub Pages 켜기
**Settings → Pages → Build and deployment → Source: GitHub Actions**

> 비공개 저장소에서 Pages를 쓰려면 GitHub Pro/Team 요금제가 필요합니다. 무료 계정이면 저장소를 공개로 바꾸셔도
> 데이터는 암호화되어 있어 비밀번호 없이는 볼 수 없습니다(단, 거래가 언제 갱신됐는지 정도는 커밋 기록으로 보입니다).
> 공개가 꺼려지면 아래 "로컬에서 실행" 방식을 쓰세요.

### 4. 첫 실행
이 브랜치를 `main`에 병합한 뒤 **Actions → 계좌 데이터 갱신 → Run workflow**를 누릅니다. 스케줄 실행은 기본 브랜치에서만 동작합니다.
끝나면 `https://<사용자>.github.io/<저장소>/`에서 비밀번호를 넣고 확인합니다.

## 로컬에서 실행

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # 앱키·시크릿·비밀번호 입력
python -m collector.collect
python -m http.server -d docs 8000   # http://localhost:8000
```

PC에서 매일 돌리려면 cron(macOS/Linux)이나 작업 스케줄러(Windows)에 `python -m collector.collect`를 등록하면 됩니다.

## 알아 둘 점

- **GitHub 서버에서 메리츠 API에 접속이 안 될 수 있습니다.** GitHub Actions 실행기는 해외(주로 미국) IP라서 증권사 쪽에서
  해외 접속을 막으면 `서버에 연결하지 못했습니다` 오류가 납니다. 그때는 로컬 실행이나
  [self-hosted runner](https://docs.github.com/actions/hosting-your-own-runners)(국내 PC)로 돌리세요 — 워크플로의 `runs-on`만 바꾸면 됩니다.
- 앱키에 연결된 계좌는 실계좌입니다. 이 프로젝트는 `MERITZ_READ_ONLY=1`을 강제해서 주문 API가 호출되지 않습니다.
- 수치는 증권사 앱과 대조해 보세요. API 응답 판정·연속조회·호출 간격은 메리츠증권 공식 예제 코드
  ([meritz-securities/open-api](https://github.com/meritz-securities/open-api), 커밋 고정)를 그대로 씁니다.
- 비밀번호를 바꾸면 기존 `docs/data.enc.json`을 풀 수 없습니다. 바꾸기 전에 로컬에서 예전 비밀번호로 한 번 실행해 평문으로 받은 뒤
  새 비밀번호로 다시 저장하거나, 파일을 지우고 새로 쌓으세요.

## 개발

```bash
pip install -r requirements.txt pytest
python -m pytest -q             # 가짜 API로 수집·증분 갱신·암호화를 검사
python scripts/make_sample.py   # docs/sample.json(데모 데이터) 다시 만들기
```

```
collector/collect.py   API 조회 → 상태 병합 → 저장
collector/metrics.py   응답 정리, 입출금 반영 수익률 계산
collector/crypto.py    PBKDF2 + AES-GCM (브라우저 WebCrypto와 같은 형식)
docs/index.html        대시보드 (Chart.js)
tests/                 가짜 API와 테스트
```
