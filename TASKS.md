# IP Monitor 작업 리스트

## Digest 품질 개선 1~6

- [x] D1. 같은 이슈 중복 노출 방지
  - digest 생성 전 topic clustering 적용
  - Claude `topic_key`/`topic_label` 기반 클러스터링 추가
  - 키워드/토큰 유사도 fallback 유지
- [x] D2. 같은 이슈 안에서 공식자료를 대표 기사로 우선 선정
  - 공식기관/정부/국제기구/법원 원문 우선
  - 언론·블로그·2차 보도는 관련 기사로 표시
- [x] D3. 한국 관련성 과잉 추론 방지
  - 한국이 직접 언급되지 않으면 "직접 영향"이 아니라 "간접 참고"로 표현
  - 요약에는 원문 사실만 넣고, 추론은 시사점으로 분리
- [x] D4. Telegram digest 포맷을 짧은 아침 브리핑형으로 개편
  - 항목별 핵심/한국 시사점/링크 중심
  - 긴 문단형 요약 축소
- [x] D5. Claude 호출 전 저관련 기사 사전 필터링
  - 채용, 일반 행사, 노동절 메시지, 단순 공고 등 명백한 비IP 항목 제외
  - 토큰 비용 절감
- [x] D6. `source_region`과 `issue_region` 분리
  - 수집 출처의 지역과 실제 이슈 대상 지역을 구분
  - 예: MLex 미국 소스에서 EU 이슈를 다루는 경우
- [x] D7. 날짜를 넘는 topic 중복 억제
  - 현재 digest clustering은 당일 분석 대상 안에서만 중복을 묶음
  - D일 공식 발표 후 D+1/D+2일 언론 후속보도가 같은 `topic_key`로 다시 digest에 뜰 수 있음
  - Supabase에 `sent_digest_topics` 저장 검토
  - 최근 N일 내 발송된 `topic_key`는 기본적으로 digest 대표 항목에서 제외
  - 단, 실질 업데이트는 예외 허용
    - 기존 대표보다 중요도 점수가 크게 높음
    - 공식기관 후속 발표
    - 판결/조사개시/제재/법안통과 등 새 이벤트
    - 사람이 승인한 재노출

## Review Telegram 품질 개선

파일럿 단계에서는 review의 목적이 두 가지다.

1. 사람이 수집 결과를 빠르게 훑어보고 이상징후를 확인한다.
2. 빠진 아티클 없이 모두 수집되는지 검증할 수 있도록 전체 수집 목록도 남긴다.

따라서 전체 아티클 나열 기능은 유지하되, Telegram 메시지는 사람이 읽을 수 있는 구조로 개선하고, 전체 목록 검증은 `telegram_raw_review.txt`, `raw_articles.json`, Actions artifact를 함께 활용한다.

- [x] R1. review 메시지 chunk 헤더 개선
  - 모든 메시지 첫 줄을 고정 헤더로 시작
  - 예: `IP Monitor 수집 검증 - 2026-05-01 (2/8)`
  - 기사 제목이나 링크 줄에 `(2/8)`이 붙지 않게 수정
- [ ] R2. source 단위 분할 적용
  - 한 소스 묶음이 메시지 중간에서 끊기지 않도록 개선
  - 너무 긴 소스는 별도 메시지로 나누되 소스 헤더를 반복
- [ ] R3. Telegram review와 artifact review 역할 분리
  - Telegram: 요약, 이상징후, 소스별 샘플 중심
  - Artifact: 전체 아티클 목록 유지
  - 파일럿 중에는 전체 목록도 계속 `telegram_raw_review.txt`에 저장
- [x] R4. review 첫 메시지를 요약 리포트화
  - 신규 기사 수
  - 성공/빈/실패 소스 수
  - `max_items_reached` 소스 수
  - 날짜 누락 기사 수
  - 제목 깨짐 의심 수
- [ ] R5. 소스별 Telegram 표시 개수 제한 검토
  - 파일럿 중 전체 목록 검증 필요가 있으므로 제한은 Telegram 표시용으로만 적용
  - 전체 목록은 artifact에 보존
- [ ] R6. 확인 필요 섹션 추가
  - 수집 실패/빈 소스
  - `max_items_reached: true` 소스
  - 날짜 누락 기사
  - 제목 깨짐 의심 기사
  - 비정상적으로 많은 후보가 나온 검색 소스
- [x] R7. 날짜 표시 정규화
  - 가능한 경우 `YYYY-MM-DD`로 표시
  - `5 min ago`, `2 hr ago` 같은 상대시간은 실행일 기준 변환 또는 상대시간으로 명시
  - 요미우리 검색 결과에서 `05:00`처럼 시간만 표시되는 문제 분석
    - 원인: `.c-list-date time`에서 시간만 잡히면 `published`가 비어있지 않아 URL 날짜 fallback이 실행되지 않음
    - 예: URL `.../20260429-GYT8T00055/`에는 `2026-04-29`가 있지만 review에는 `05:00`만 표시됨
    - 해결: `date_selector` 결과를 그대로 쓰기 전에 `extract_date_from_text()`로 날짜 포함 여부를 검증
    - 날짜 없이 시간만 있으면 `extract_date_from_url(full_url)` 결과와 결합해 `YYYY-MM-DD HH:MM`으로 보정
    - URL 날짜도 없으면 상세 페이지 날짜 fallback 또는 `시간만 추출됨` 플래그를 review 이상징후에 표시
- [ ] R8. 제목 정리
  - `html.unescape()` 적용
  - `&nbsp;` 등 HTML entity 제거
  - 과도한 공백 정리
- [ ] R9. 검색 소스 저관련 결과 필터 강화
  - Bloomberg 등 키워드 검색 소스에서 IP와 무관한 기사 다수 유입
  - review에는 저관련 의심 소스를 표시하고, 이후 수집 필터 보강

## 실행 시간/운영 로그 개선

- [x] 실행 로그를 Supabase에도 저장
  - 개별 실행: `run_log_YYYYMMDD_HHMMSS`
  - 최신 실행: `run_log_latest`
  - 기존 로컬 `data/run_logs/*.json` 저장은 유지
- [x] GitHub Actions 메타데이터를 실행 로그에 포함
  - event name
  - GitHub run id
  - run attempt
  - workflow
  - repository
  - run URL
- [x] 단계별 소요시간을 실행 로그에 포함
  - `fetch_duration_seconds`
  - `raw_review_telegram_duration_seconds`
  - `analysis_duration_seconds`
  - `digest_telegram_duration_seconds`
- [ ] GitHub cron 지연 여부 추적
  - Actions run 생성 시각과 실제 코드 시작 시각 비교
  - 필요 시 VPS + cron 전환 검토

## 완료된 작업

- GitHub Actions에서 매일 KST 09:00 자동 실행되도록 설정
- 전체 소스 `max_items`를 5로 변경
- Supabase 기반 `seen_urls` 저장/로드 적용
- Supabase URL이 `/rest/v1` 포함 여부와 관계없이 동작하도록 정규화
- 수집 검증용 Telegram 채널과 digest Telegram 채널 분리
- Telegram 메시지가 너무 길 때 3500자 단위로 분할 전송
- 수집 검증용 원문 목록 `data/telegram_raw_review.txt` 생성
- Claude 분석 결과 누적 저장
  - 로컬/Artifact: `data/results.json`
  - Supabase key: `analysis_results`
- Claude 분석 결과 날짜별 저장
  - 로컬/Artifact: `data/daily_results/results_YYYYMMDD.json`
  - Supabase key: `analysis_results_YYYYMMDD`
- Actions artifact에 주요 실행 결과 포함
  - `raw_articles.json`
  - `results.json`
  - `daily_results/*.json`
  - `source_check_report.json`
  - `telegram_digest.txt`
  - `telegram_raw_review.txt`
  - `failed_sources.yaml`
  - `run_logs/*.json`
- `max_items` 도달 여부를 수집 로그에 기록
  - `max_items`
  - `max_items_reached`
  - 콘솔 메시지: `max_items 도달: N/N개 수집`
- 일본 지식재산전략본부 등 일부 사이트의 인코딩/날짜 추출 개선
- 베트남/IPRdaily 등 상세 페이지 기반 날짜 보강

## 파일럿 운영 체크

- Actions 수동 실행 후 아래 항목 확인
  - `Supabase seen_urls 로드: N개`
  - 리뷰 채널 Telegram 도착
  - digest 채널 Telegram 도착
  - `Supabase seen_urls 저장`
  - `Supabase analysis_results 저장`
  - 날짜별 `analysis_results_YYYYMMDD` 저장
- 다음날 KST 09:00 자동 실행 여부 확인
- `data/run_logs/run_*.json`에서 소스별 실패/빈 결과 확인
- `source_check_report.json`에서 `max_items_reached: true`인 소스 확인
- `max_items_reached`가 자주 발생하는 소스는 `max_items` 상향 검토
- Telegram digest가 여러 메시지로 정상 분할되는지 확인
- Claude 분석 실패 항목이 있는 경우 `analysis_errors` 확인

## 가까운 개선 과제

- 수집 후보 전체 개수(`candidate_count`)를 가능한 소스부터 로그에 추가
  - 현재는 `count == max_items`이면 제한 도달 가능성만 판단
  - 후보 전체 수를 알 수 있는 HTML/RSS 소스부터 보강
- 날짜 누락 소스 추가 개선
  - `published`가 비어 있는 소스 목록을 주기적으로 확인
  - 소스별 `date_selector` 또는 상세 페이지 fallback 추가
- 인코딩이 깨지는 소스 추가 점검
  - 실행 결과에서 제목 mojibake가 보이면 `decode_html_response` 보강
- Claude 분석량 제한 옵션 검토
  - 하루 분석 최대 개수
  - 중요도 낮은 소스 분석 제외
  - 특정 priority 이상만 분석
- Claude 실패 시 재시도/부분 저장 강화
  - API 오류
  - timeout
  - rate limit
- digest 생성 전 최종 메시지 길이와 전송 메시지 개수를 로그에 기록

## 승인형 운영 전환 아이디어

- 자동 Telegram 발송 대신 사람 승인 후 발송하는 구조 검토
- 1차 추천 방식: Notion 검토 DB
  - Claude 분석 결과를 Notion DB에 초안으로 등록
  - 사람이 요약/점수/최종 발송문을 수정
  - `승인상태`를 `승인`으로 바꾸면 Telegram 발송
- Notion DB 컬럼 예시
  - 날짜
  - 소스
  - 지역
  - 원문 제목
  - 원문 URL
  - Claude 요약
  - Claude 중요도
  - Claude 시사점
  - 최종 발송 제목
  - 최종 발송 본문
  - 승인상태: 대기 / 승인 / 보류 / 제외
  - 발송여부
  - 발송시각
- 2차 확장 방식: Supabase + 관리자 웹페이지
  - Supabase에 분석 결과/승인 상태/최종 문안 저장
  - 내부용 웹페이지에서 수정/승인/발송
  - Streamlit 또는 Next.js 검토

## 사람 피드백 반영 아이디어

- Claude 점수와 사람이 수정한 점수를 함께 저장
- 저장할 피드백 필드 예시
  - `claude_score`
  - `human_score`
  - `score_delta`
  - `human_reason`
  - `human_decision`
  - `final_summary`
  - `feedback_used_for_prompt`
- Claude 자체를 직접 강화학습하기보다는, 피드백 데이터를 다음 프롬프트에 반영
- 대표 수정 사례를 few-shot 예시로 관리
  - 단순 행사/세미나/홍보성 기사는 낮게 평가
  - 법령 개정/판례/집행 강화/한국 영향 가능성은 높게 평가
- 주기적으로 `feedback_rules.md` 또는 `scoring_examples.json` 생성 검토

## 장기 과제

- 관리자 웹페이지 구축
  - 오늘 분석 결과 목록
  - 상태/지역/중요도 필터
  - 요약/최종문안 수정
  - 승인/제외/보류 처리
  - Telegram 미리보기 및 발송
- Supabase 테이블 정규화 검토
  - 현재는 `monitor_state` key-value 구조
  - 장기적으로 `articles`, `analysis_items`, `review_decisions`, `send_logs` 테이블 분리 가능
- 운영 대시보드 추가
  - 일별 수집 수
  - 소스별 실패율
  - 날짜 누락률
  - Claude 분석 성공률
  - Telegram 발송 성공률
- 비용 관리
  - 일별 Claude 호출 수
  - 분석 대상 필터링
  - priority/source별 분석 정책
