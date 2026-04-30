# IP Policy Monitor MVP

주말 동안 바로 테스트할 수 있는 최소 기능 MVP입니다.

## 구성
- `monitor.py`: RSS/웹페이지 소스를 읽고 요약/중요도 평가 후 결과를 저장하고 텔레그램 초안 메시지를 생성
- `config.example.yaml`: 소스 목록 및 실행 설정 예시
- `requirements.txt`: 필요한 파이썬 패키지
- `dashboard.html`: 생성된 `results.json`을 브라우저에서 보는 간단한 로컬 대시보드

## 동작 방식
1. `config.yaml`에 소스 목록을 넣습니다.
2. `monitor.py`가 새 글을 가져옵니다.
3. 아직 처리하지 않은 항목만 선별합니다.
4. Claude API를 사용하면 중요도/요약/시사점을 생성합니다.
5. API 키가 없으면 규칙 기반 임시 요약으로 동작합니다.
6. 결과는 `data/results.json`, `data/seen_urls.json`, `data/telegram_digest.txt`에 저장됩니다.

## 빠른 시작
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python monitor.py
```

## 환경변수
- `ANTHROPIC_API_KEY`: Claude API 키
- `TELEGRAM_BOT_TOKEN`: 텔레그램 봇 토큰
- `TELEGRAM_CHAT_ID`: 텔레그램 채널/그룹 chat id

텔레그램 전송은 기본적으로 비활성화되어 있고, `config.yaml`에서 `telegram.send_enabled: true`로 켜면 됩니다.

## 추천 주말 테스트 절차
1. 소스 5개만 넣고 시작
2. 토요일 오전/저녁 두 번 실행
3. 중요도 점수와 요약 품질 점검
4. 일요일에 프롬프트 문구와 카테고리 조정
5. 월요일부터 하루 1회 자동 실행 여부 결정

## 참고
- RSS가 있는 소스는 RSS 우선
- RSS가 없으면 `type: html`로 목록 페이지 CSS selector 지정
- PDF는 이 MVP 범위에서 제외
