# IP News Monitor

해외 IP 정책/뉴스 소스를 주기적으로 수집하고, 검증용 원문 목록과 Claude 요약 digest를 텔레그램 채널로 보낼 수 있는 Python 크롤러입니다.

## 주요 기능

- `config.yaml`에 정의된 RSS, JSON API, HTML 목록, Algolia, Playwright 소스 수집
- URL 기준 중복 제거 및 `data/seen_urls.json` 저장
- 신규 기사 원본을 `data/raw_articles.json`로 저장
- 사람이 수집 누락/오수집을 검토할 수 있는 `data/telegram_raw_review.txt` 생성
- 텔레그램 채널 2개 분리 지원
  - 수집 검증용: `TELEGRAM_REVIEW_CHAT_ID`
  - Claude 요약 digest용: `TELEGRAM_DIGEST_CHAT_ID`
- 실행 결과 로그를 `data/run_logs/run_YYYYMMDD_HHMMSS.json`로 저장
- 실패/빈 소스 목록을 `data/failed_sources.yaml`로 저장하고 `--failed-only` 재실행 지원
- GitHub Actions 정기 실행 및 Supabase 기반 `seen_urls` 상태 저장 지원

## 파일 구성

- `monitor.py`: 수집, 중복 제거, 검증 메시지 생성, Claude 분석, 텔레그램 전송
- `config.yaml`: 소스 목록과 실행 설정
- `requirements.txt`: Python 패키지 목록
- `.env`: API 키와 텔레그램 채널 ID. Git에 올리지 않습니다.
- `data/`: 실행 결과와 상태 파일 저장 폴더. 대부분 Git에서 제외합니다.

## 설치

Windows PowerShell 기준:

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Playwright 소스를 사용할 경우:

```powershell
playwright install chromium
```

Playwright는 기본적으로 headless 모드로 실행됩니다. 로컬에서 브라우저 창을 보고 디버깅하려면 `.env`에 아래 값을 추가합니다.

```env
PLAYWRIGHT_HEADLESS=false
```

Linux/VPS 기준:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 환경변수

프로젝트 루트에 `.env` 파일을 만들고 아래 값을 설정합니다.

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_REVIEW_CHAT_ID=
TELEGRAM_DIGEST_CHAT_ID=
ANTHROPIC_API_KEY=
SUPABASE_URL=
SUPABASE_SERVICE_ROLE_KEY=
```

현재 코드는 `python-dotenv`로 `.env`를 자동 로드합니다.
`SUPABASE_URL`과 `SUPABASE_SERVICE_ROLE_KEY`가 있으면 `seen_urls`를 Supabase의 `monitor_state` 테이블에서 우선 읽고 저장합니다. 없으면 기존처럼 `data/seen_urls.json`을 사용합니다.

## 실행

전체 소스 수집:

```bash
python monitor.py
```

실패/빈 소스만 다시 실행:

```bash
python monitor.py --failed-only
```

## 현재 실행 흐름

1. `config.yaml`에서 활성화된 소스를 읽습니다.
2. 각 소스에서 최대 `max_items`개를 수집합니다.
3. 이미 본 URL과 이미 분석된 URL을 제외합니다.
4. `data/raw_articles.json`에 신규 원본 기사를 저장합니다.
5. `data/telegram_raw_review.txt`를 생성합니다.
6. `telegram.review_send_enabled: true`이면 검증용 텔레그램 채널로 발송을 시도합니다.
7. `SKIP_ANALYSIS = True`이면 Claude 분석 없이 종료합니다.
8. `SKIP_ANALYSIS = False`이면 Claude 분석 후 `data/results.json`, `data/telegram_digest.txt`를 생성하고 요약 채널로 발송합니다.

## 텔레그램 설정

`config.yaml`:

```yaml
telegram:
  send_enabled: true
  review_send_enabled: true
  digest_send_enabled: true
  max_messages: 5
  timeout_seconds: 10
```

- `send_enabled`: 기본 전송 스위치
- `review_send_enabled`: 수집 검증용 메시지 전송 여부
- `digest_send_enabled`: Claude 요약 digest 전송 여부
- `max_messages`: 검증용 메시지 chunk 최대 전송 수
- `timeout_seconds`: 텔레그램 API 요청 타임아웃

텔레그램 API가 회사망에서 차단되어도 txt 파일 생성은 진행되며, 전송 실패 시 전체 실행은 중단되지 않습니다.

## 지역 분류

`config.yaml`의 `region` 값은 아래 6개만 사용합니다.

```text
미국
일본
중국
유럽
국제기구
기타
```

`raw_articles.json`의 `region`은 각 소스의 `region` 값을 그대로 가져옵니다.

## 운영 메모

- VPS에서 매일 실행하려면 cron을 사용할 수 있습니다.
- GitHub Actions에서는 `.github/workflows/run-monitor.yml`이 매일 한국시간 오전 8시에 실행됩니다.
- `.env`, `venv/`, `data/*.json`, `data/run_logs/`는 Git에 올리지 않습니다.
- VPS 배포 시에는 GitHub에서 clone/pull 후 서버에 별도 `.env`를 작성합니다.

Supabase에는 아래 테이블이 필요합니다.

```sql
create table if not exists monitor_state (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);

alter table monitor_state enable row level security;

insert into monitor_state (key, value)
values ('seen_urls', '[]'::jsonb)
on conflict (key)
do update set value = excluded.value, updated_at = now();
```

예시 cron:

```cron
0 8 * * * cd /home/ubuntu/ipnews && /home/ubuntu/ipnews/venv/bin/python monitor.py >> data/cron.log 2>&1
```
