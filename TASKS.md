# IP Monitor 작업 리스트

## Digest 품질 개선 1~6

- [x] 1. 같은 이슈 중복 노출 방지
  - digest 생성 전 topic clustering 적용
  - Claude `topic_key`/`topic_label` 기반 클러스터링 추가
  - 키워드/토큰 유사도 fallback 유지
- [x] 2. 같은 이슈 안에서 공식자료를 대표 기사로 우선 선정
  - 공식기관/정부/국제기구/법원 원문 우선
  - 언론·블로그·2차 보도는 관련 기사로 표시
- [ ] 3. 한국 관련성 과잉 추론 방지
  - 한국이 직접 언급되지 않으면 "직접 영향"이 아니라 "간접 참고"로 표현
  - 요약에는 원문 사실만 넣고, 추론은 시사점으로 분리
- [ ] 4. Telegram digest 포맷을 짧은 아침 브리핑형으로 개편
  - 항목별 핵심/한국 시사점/링크 중심
  - 긴 문단형 요약 축소
- [ ] 5. Claude 호출 전 저관련 기사 사전 필터링
  - 채용, 일반 행사, 노동절 메시지, 단순 공고 등 명백한 비IP 항목 제외
  - 토큰 비용 절감
- [ ] 6. `source_region`과 `issue_region` 분리
  - 수집 출처의 지역과 실제 이슈 대상 지역을 구분
  - 예: MLex 미국 소스에서 EU 이슈를 다루는 경우

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
