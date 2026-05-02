import sys
import os
import json
import time
import re
import html
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
import yaml
import feedparser
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import anthropic
from curl_cffi import requests as curl_requests
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

load_dotenv()

@dataclass
class SourceConfig:
    name: str
    region: str
    homepage: str
    monitor_url: str
    mode: str
    enabled: bool
    priority: int
    list_selector: str = ""
    row_selector: str = ""
    title_selector: str = "a[href]"
    date_selector: str = ""
    algolia_filters: str = ""

@dataclass
class Article:
    source: str
    region: str
    title: str
    url: str
    summary_raw: str
    published: Optional[str]


@dataclass
class AnalyzedArticle:
    source: str
    region: str
    title: str
    url: str
    published: Optional[str]
    summary_ko: str
    importance_score: int
    category: str
    key_points: List[str]
    raw_excerpt: str
    topic_key: str = ""
    topic_label: str = ""
    issue_region: str = ""


@dataclass
class DigestTopicCluster:
    representative: AnalyzedArticle
    items: List[AnalyzedArticle]
    topic_keys: set
    representative_tokens: set


SEEN_PATH = 'data/seen_urls.json'
RESULTS_PATH = 'data/results.json'
DAILY_RESULTS_DIR = 'data/daily_results'
SENT_DIGEST_TOPICS_PATH = 'data/sent_digest_topics.json'
DIGEST_PATH = 'data/telegram_digest.txt'
RAW_REVIEW_DIGEST_PATH = 'data/telegram_raw_review.txt'
SOURCE_CHECK_REPORT_PATH = 'data/source_check_report.json'
FAILED_SOURCES_PATH = 'data/failed_sources.yaml'
RUN_LOG_DIR = 'data/run_logs'
SUPABASE_STATE_TABLE = 'monitor_state'
SUPABASE_SEEN_KEY = 'seen_urls'
SUPABASE_RESULTS_KEY = 'analysis_results'
SUPABASE_SENT_DIGEST_TOPICS_KEY = 'sent_digest_topics'
SUPABASE_RUN_LOG_PREFIX = 'run_log'
SUPABASE_LATEST_RUN_LOG_KEY = 'run_log_latest'

# max_items 기반 수집 제한은 seen 적용 전 후보 수를 자르는 방식이라 운영상
# 의미가 약해졌다. 설정에서는 제거하고 제한 helper도 no-op으로 둔다.



BAD_TITLE_PATTERNS = [
    # 공통 스킵/네비게이션
    r'^skip to\b',
    r'^jump to\b',
    r'^home$',
    r'^menu$',
    r'^search$',
    r'^site map$',
    r'^a-z$',
    r'^back to top$',

    # 언어/공통 유틸
    r'^english$',
    r'^français$',
    r'^español$',
    r'^privacy policy$',
    r'^contact us$',
    r'^contact$',
    r'^about us$',
    r'^about$',
    r'^help$',
    r'^faq$',
    r'^faqs$',
    r'^login$',
    r'^log in$',
    r'^sign in$',
    r'^subscribe to\b',

    # WTO / 국제기구 계열에서 자주 나오는 허브성 제목
    r'^news & events$',
    r'^meetings/events$',
    r'^about wto$',
    r'^trade topics$',
    r'^wto membership$',
    r'^documents & resources$',
    r'^external relations$',

    # USPTO / IP Australia 등 안내성 링크
    r'^patents?$',
    r'^trademarks?$',
    r'^copyright$',
    r'^understanding ip$',
    r'^types of ip$',
    r'^ip in everyday life$',
    r'^find it fast links$',
    r'^myuspto$',
    r'^leadership$',
    r'^internship$',
    r'^jobs$',

    # Copyright Office / 기타 허브
    r'^newsnet$',
    r'^newsnet archive$',


    # USPTO 사이트명/네비게이션
    r'^uspto\s*[-–]\s*united states patent',   # "USPTO - United States Patent..."
    r'^united states patent and trademark',
    r'^international patent filings?$',
    r'^patent trial and appeal board$',
    r'^search our (patent|trademark) database$',
    r'^trademark litigation$',
    r'^patent basics?$',
    r'^ip policy$',
    r'^learning and resources$',
    r'^find it fast',
    r'^subscription center$',
]
BAD_URL_PATTERNS = [
    # 앵커 / JS
    r'^#$',
    r'^#',
    r'^javascript:',

    # 페이지 내부 점프
    r'#main-content',
    r'#main$',
    r'#content$',
    r'#footer$',
    r'#launch-search',
    r'#launch-links',
    r'#launch-menu',

    # 공통 소개/안내/정적 페이지 — 말단 경로만 차단 (하위 경로 열어둠)
    r'/about/?$',           # ← 수정: /about 자체만
    r'/about-us/?$',        # ← 수정: /about-us 자체만
    r'/contact/?$',
    r'/help/?$',
    r'/faq/?$',
    r'/faqs/?$',
    r'/careers?/?$',
    r'/jobs/?$',
    r'/login/?$',
    r'/search/?$',
    r'/sitemap/?$',
    r'/site-map/?$',
    r'/privacy/?$',
    r'/accessibility/?$',
    r'/subscribe/?$',

    # 이벤트/포럼/허브성 경로
    r'/events(?:/|$)',
    r'/events_e(?:/|$)',
    r'/news_e/events_e(?:/|$)',
    r'/forums_e(?:/|$)',
    r'/thewto_e(?:/|$)',
    r'/info_e(?:/|$)',

    # 안내/교육성 페이지
    r'/understanding-ip(?:/|$)',
    r'/types-of-ip(?:/|$)',

    # USPTO 고정 안내/툴 페이지
    r'^/$',
    r'/patents/?$',
    r'/patents/basics/?$',
    r'/patents/search(?:/|$)',
    r'/patents/search/patent-public-search(?:/|$)',

    # EPO / 일반 안내성 페이지
    r'/trademarks?/?$',
    r'/copyright/?$',

    # Copyright Office 허브
    r'/newsnet/?$',
    r'/newsnet/archive/?$',
    r'/title17/?$',
    r'/title37/?$',

    # WTO 허브 페이지
    r'/english/news_e/news_e\.htm$',
    r'/english/news_e/news_e\.htm\?.*$',

    # USTR 월별 archive 허브
    r'/press-releases/\d{4}/(january|february|march|april|may|june|july|august|september|october|november|december)$',
    r'/fact-sheets/\d{4}/(january|february|march|april|may|june|july|august|september|october|november|december)$',
]

ALLOW_URL_PATTERNS_BY_SOURCE = {
    # 기존 '미국 특허상표청 (USPTO)' 에서 띄어쓰기를 제거하고, CSS 대신 정규식을 넣습니다.
    '미국 특허상표청(USPTO)': [
        r'/about-us/news-updates/',
        r'/news-updates/',
    ],
    '미국 저작권청': [
        r'/newsnet/\d{4}/\d+\.html$',
        r'/newsnet/\d{4}/[a-z0-9\-]+/?$',
    ],
    '미국 연방거래위원회 (FTC)': [
        r'/news-events/news/press-releases/\d{4}/',  # ← 수정: 연도 포함
    ],
    '미국 국제무역위원회(ITC)': [
        r'/press_room/news_release/',
        r'/press_room/news_release\.htm$',
    ],
    '미국 무역대표부': [
        r'/press-releases/',  # 모든 보도자료 허용 (연도 구분 없음)
        r'/fact-sheets/',  # 팩트시트 허용
        r'/speeches-and-remarks/',  # 연설문 및 발언록 추가
        r'/node/\d+$',  # /node/14307 같은 형태 유지
    ],
    '세계무역기구 (WTO)': [
        r'/english/news_e/news\d{2}_e/',
        r'/english/news_e/pres\d{2}_e/',
    ],
    '일본 특허청 (JPO)': [
        r'/news/',
    ],
    '일본 경제산업성 (METI)': [
        r'/press/',
    ],
    '일본 문화청': [
        r'/koho_hodo_oshirase/hodohappyo/\d{8}\.html$',  # ← 수정: 연도 인덱스 제외
    ],
    '일본 지식재산전략본부': [
        r'^https://www\.cas\.go\.jp/jp/seisakukaigi/titeki2/',
        r'^https://www\.cao\.go\.jp/chizai/',
    ],
    '중국 국가지식산권국(CNIPA)': [
    r'/art/',
    r'/\d{4}/\d{1,2}/\d{1,2}/',
    r'\.html$',
    ],
    '싱가포르 지식재산청 (IPOS)': [
        r'/news/news-collection/',
    ],
    '호주 지식재산청 (IP Australia)': [
        r'/news-and-community/news/',
    ],

    'IPRdaily': [
        r'/news_\d+\.html$',  # 로그에 찍힌 진짜 뉴스 패턴! (예: /news_42194.html)
        r'/article_\d+\.html$',  # 혹시 모를 article 패턴 허용
        r'/article/index/',  # 기존 경로도 예비로 유지
    ],
    '중국 국가판무국': [
        r'/zscqj-xxgk',        # 카테고리 경로 허용
        r'/\d{8}/.*\.html$',   # 날짜(8자리)와 html로 끝나는 패턴 완벽 허용
    ]
}

NEWS_HINTS = [
    # precision 위주로 좁힘
    '/news',
    '/press',
    '/press-releases',
    '/newsroom',
    '/updates',
    '/releases',
    '/fact-sheet',
    '/fact-sheets',
]

IP_KEYWORDS = [
    'ip', 'patent', 'copyright', 'trademark', 'licensing', 'innovation', 'design',
    'intellectual property',
    '지식', '특허', '저작권', '상표', '라이선스',
    # 일본어
    '特許',      # 특허
    '実用新案',  # 실용신안
    '意匠',      # 의장(디자인)
    '商標',      # 상표
    '著作権',    # 저작권
    '知財',      # 지재 (지식재산)
    '出願',      # 출원
    'ハーグ',    # 헤이그 (디자인 국제출원)
    'マドリッド' # 마드리드 (상표 국제출원)
]

ANALYSIS_KEEP_KEYWORDS = [
    'intellectual property', 'patent', 'trademark', 'copyright', 'trade secret',
    'licensing', 'frand', 'standard essential', 'sep', 'counterfeit',
    'piracy', 'infringement', 'ai', 'artificial intelligence', 'innovation',
    'special 301', 'watch list', 'wipo', 'uspto', 'epo', 'euipo', 'kipo',
    '지식재산', '지식재산권', '특허', '상표', '저작권', '영업비밀',
    '위조', '침해', '라이선스', '표준필수', '인공지능',
    '知的財産', '知財', '特許', '商標', '著作権', '侵害',
    '知识产权', '专利', '商标', '版权', '著作权',
]

ANALYSIS_SKIP_PATTERNS = [
    r'\bcareer(s)?\b',
    r'\bhiring\b',
    r'\brecruit(ment|ing)?\b',
    r'\binternship\b',
    r'\bstate dinner\b',
    r'\bmemorial\b',
    r'\bholiday\b',
    r'\bpublic forum\b',
    r'\bregistration opens\b',
    r'\bmedia registration\b',
    r'\bwebinar\b',
    r'\bpodcast\b',
    r'\bworkshop\b',
    r'\bobesity[- ]drug\b',
    r'\bdrug pricing\b',
    r'\bfirst lady\b',
    r'\bpresidential message\b',
    r'\bpermit: authorizing\b',
    r'採用',
    r'求人',
    r'落札者',
    r'劳动节',
    r'招聘',
]

BROAD_SEARCH_SOURCES = [
    'Bloomberg',
    'Thomson Reuters',
    '닛케이 검색',
    '요미우리 검색',
    '인민망 검색',
]

TRUSTED_ANALYSIS_SOURCE_KEYWORDS = [
    '특허청',
    '지식재산청',
    '지식재산기구',
    '지식재산권 정보',
    'USPTO',
    'WIPO',
    'EPO',
    'EUIPO',
    '통합특허법원',
    '저작권청',
    '국가판권국',
]


def _norm(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or '')).strip().lower()


def looks_like_non_article(title: str, href: str) -> bool:
    t = _norm(title)
    u = _norm(href)

    if not t or len(t) < 5:
        return True
    if u.startswith('#'):
        return True
    if any(re.search(p, t) for p in BAD_TITLE_PATTERNS):
        return True
    if any(re.search(p, u) for p in BAD_URL_PATTERNS):
        return True
    return False


def has_analysis_keep_keyword(text: str) -> bool:
    t = _norm(html.unescape(text or ""))
    return any(keyword.lower() in t for keyword in ANALYSIS_KEEP_KEYWORDS)


def should_skip_claude_analysis(art: Article) -> Optional[str]:
    content_text = " ".join([
        art.title or "",
        art.url or "",
        art.summary_raw or "",
    ])
    source_text = art.source or ""
    normalized = _norm(html.unescape(content_text))

    if any(keyword in source_text for keyword in TRUSTED_ANALYSIS_SOURCE_KEYWORDS):
        return None

    if has_analysis_keep_keyword(content_text):
        return None

    if any(pattern in source_text for pattern in BROAD_SEARCH_SOURCES):
        return "broad_search_without_ip_keyword"

    for pattern in ANALYSIS_SKIP_PATTERNS:
        if re.search(pattern, normalized, re.I):
            return f"low_relevance_pattern:{pattern}"

    return None


def looks_like_article_url(href: str) -> bool:
    u = _norm(href)

    if any(h in u for h in NEWS_HINTS):
        return True
    if re.search(r'/20\d{2}/', u):
        return True
    if re.search(r'\d{4}/\d{2}/\d{2}', u):
        return True
    return False


def passes_source_allowlist(source_name: str, url: str) -> bool:
    patterns = ALLOW_URL_PATTERNS_BY_SOURCE.get(source_name, [])
    if not patterns:
        return True
    return any(re.search(p, url, re.I) for p in patterns)

def load_config(path: str = 'config.yaml') -> Dict[str, Any]:
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def source_limit_reached(items: List[Any], source: SourceConfig) -> bool:
    # max_items 제한 로직은 운영상 비활성화했다.
    return False


def source_limited_sequence(items: List[Any], source: SourceConfig) -> List[Any]:
    # max_items 제한 로직은 운영상 비활성화했다.
    return items


def load_sources(cfg: Dict[str, Any]) -> List[SourceConfig]:
    out = []
    for s in cfg.get('sources', []):
        if not s.get('enabled', True):
            continue
        out.append(SourceConfig(
            name=s['name'],
            region=s.get('region', ''),
            homepage=s['homepage'],
            monitor_url=s['monitor_url'],
            mode=s.get('mode', 'html_list'),
            enabled=s.get('enabled', True),
            priority=s.get('priority', 3),
            list_selector=s.get('list_selector', ''),
            row_selector=s.get('row_selector', ''),
            title_selector=s.get('title_selector', 'a[href]'),
            date_selector=s.get('date_selector', ''),
            algolia_filters=s.get('algolia_filters', ''),
        ))
    return out


def supabase_config() -> Optional[Dict[str, str]]:
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    if not url or not key:
        return None
    url = url.rstrip('/')
    if url.endswith('/rest/v1'):
        url = url[:-len('/rest/v1')]
    return {
        "url": url,
        "key": key,
    }


def supabase_headers(cfg: Dict[str, str], prefer: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def load_supabase_state(key: str) -> Optional[Any]:
    cfg = supabase_config()
    if not cfg:
        return None

    endpoint = f"{cfg['url']}/rest/v1/{SUPABASE_STATE_TABLE}"
    try:
        resp = requests.get(
            endpoint,
            headers=supabase_headers(cfg),
            params={"key": f"eq.{key}", "select": "value"},
            timeout=20,
        )
        if not resp.ok:
            print(f"Supabase 상태 로드 실패({key}): {resp.status_code} {resp.text[:200]}")
            return None

        rows = resp.json()
        if not rows:
            return None
        return rows[0].get("value")
    except requests.RequestException as e:
        print(f"Supabase 상태 로드 실패({key}): {e.__class__.__name__}")
    except Exception as e:
        print(f"Supabase 상태 로드 실패({key}): {e}")
    return None


def save_supabase_state(key: str, value: Any) -> bool:
    cfg = supabase_config()
    if not cfg:
        return False

    endpoint = f"{cfg['url']}/rest/v1/{SUPABASE_STATE_TABLE}"
    payload = {
        "key": key,
        "value": value,
    }
    try:
        resp = requests.post(
            endpoint,
            headers=supabase_headers(cfg, prefer="resolution=merge-duplicates,return=minimal"),
            params={"on_conflict": "key"},
            json=payload,
            timeout=20,
        )
        if not resp.ok:
            print(f"Supabase 상태 저장 실패({key}): {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except requests.RequestException as e:
        print(f"Supabase 상태 저장 실패({key}): {e.__class__.__name__}")
    except Exception as e:
        print(f"Supabase 상태 저장 실패({key}): {e}")
    return False


def load_seen() -> set:
    remote_seen = load_supabase_state(SUPABASE_SEEN_KEY)
    if isinstance(remote_seen, list):
        print(f"Supabase seen_urls 로드: {len(remote_seen)}개")
        return set(remote_seen)
    if isinstance(remote_seen, dict) and isinstance(remote_seen.get("urls"), list):
        urls = remote_seen["urls"]
        print(f"Supabase seen_urls 로드: {len(urls)}개")
        return set(urls)

    if not os.path.exists(SEEN_PATH):
        return set()
    try:
        with open(SEEN_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return set(data)
    except Exception:
        return set()


def save_seen(seen: set):
    os.makedirs('data', exist_ok=True)
    seen_list = sorted(seen)
    with open(SEEN_PATH, 'w', encoding='utf-8') as f:
        json.dump(seen_list, f, ensure_ascii=False, indent=2)

    if save_supabase_state(SUPABASE_SEEN_KEY, seen_list):
        print(f"Supabase seen_urls 저장: {len(seen_list)}개")


def load_results() -> List[Dict[str, Any]]:
    remote_results = load_supabase_state(SUPABASE_RESULTS_KEY)
    if isinstance(remote_results, list):
        print(f"Supabase analysis_results 로드: {len(remote_results)}개")
        return remote_results
    if isinstance(remote_results, dict) and isinstance(remote_results.get("items"), list):
        items = remote_results["items"]
        print(f"Supabase analysis_results 로드: {len(items)}개")
        return items

    if not os.path.exists(RESULTS_PATH):
        return []
    try:
        with open(RESULTS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return []


def load_results_index() -> Dict[str, Dict[str, Any]]:
    items = load_results()
    out = {}
    for item in items:
        url = item.get('url')
        if url:
            out[url] = item
    return out


def load_analyzed_urls() -> set:
    return set(load_results_index().keys())


def save_results(items: List[Dict[str, Any]]):
    os.makedirs('data', exist_ok=True)
    with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    if save_supabase_state(SUPABASE_RESULTS_KEY, items):
        print(f"Supabase analysis_results 저장: {len(items)}개")


def save_daily_results(items: List[Dict[str, Any]], run_id: str) -> Optional[str]:
    if not items:
        return None

    run_date = run_id[:8]
    os.makedirs(DAILY_RESULTS_DIR, exist_ok=True)
    path = os.path.join(DAILY_RESULTS_DIR, f"results_{run_date}.json")

    existing: List[Dict[str, Any]] = []
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, list):
                existing = loaded
        except Exception:
            existing = []

    merged = {item.get('url'): item for item in existing if item.get('url')}
    for item in items:
        url = item.get('url')
        if url:
            merged[url] = item

    daily_items = list(merged.values())
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(daily_items, f, ensure_ascii=False, indent=2)

    daily_key = f"{SUPABASE_RESULTS_KEY}_{run_date}"
    if save_supabase_state(daily_key, daily_items):
        print(f"Supabase {daily_key} 저장: {len(daily_items)}개")

    return path


def load_sent_digest_topics() -> List[Dict[str, Any]]:
    remote = load_supabase_state(SUPABASE_SENT_DIGEST_TOPICS_KEY)
    if isinstance(remote, list):
        print(f"Supabase sent_digest_topics 로드: {len(remote)}개")
        return remote

    if os.path.exists(SENT_DIGEST_TOPICS_PATH):
        try:
            with open(SENT_DIGEST_TOPICS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except Exception:
            return []

    return []


def save_sent_digest_topics(items: List[Dict[str, Any]]):
    os.makedirs('data', exist_ok=True)
    with open(SENT_DIGEST_TOPICS_PATH, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    if save_supabase_state(SUPABASE_SENT_DIGEST_TOPICS_KEY, items):
        print(f"Supabase sent_digest_topics 저장: {len(items)}개")


def build_source_check_record(
    src: SourceConfig,
    articles: List[Article],
    error: str = ""
) -> Dict[str, Any]:
    """
    소스별 수집 성공/실패 여부를 기록한다.
    - ok: 기사 후보 1개 이상 수집
    - empty: 요청은 성공했으나 기사 후보 0개
    - fail: 요청 또는 파싱 중 예외 발생
    """
    if error:
        status = "fail"
    elif len(articles) == 0:
        status = "empty"
    else:
        status = "ok"

    return {
        "name": src.name,
        "region": src.region,
        "mode": src.mode,
        "monitor_url": src.monitor_url,
        "status": status,
        "count": len(articles),
        "error": error,
        "sample_titles": [a.title for a in articles[:3]],
        "sample_urls": [a.url for a in articles[:3]],
    }


def save_source_check_report(records: List[Dict[str, Any]], path: str = SOURCE_CHECK_REPORT_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def save_failed_sources_yaml(
    cfg: Dict[str, Any],
    records: List[Dict[str, Any]],
    path: str = FAILED_SOURCES_PATH
):
    """
    source_check_report 기준으로 fail/empty 소스만 별도 yaml로 저장한다.
    """
    failed_names = {
        r["name"]
        for r in records
        if r.get("status") in ("fail", "empty")
    }

    failed_sources = [
        s for s in cfg.get("sources", [])
        if s.get("name") in failed_names
    ]

    out = {
        "project": cfg.get("project", {}),
        "fetch": cfg.get("fetch", {}),
        "analysis": cfg.get("analysis", {}),
        "telegram": cfg.get("telegram", {}),
        "sources": failed_sources,
    }

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(out, f, allow_unicode=True, sort_keys=False)


def save_run_log(log: Dict[str, Any]) -> str:
    os.makedirs(RUN_LOG_DIR, exist_ok=True)
    run_id = log.get("run_id") or time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RUN_LOG_DIR, f"run_{run_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    run_log_key = f"{SUPABASE_RUN_LOG_PREFIX}_{run_id}"
    if save_supabase_state(run_log_key, log):
        print(f"Supabase {run_log_key} 저장")
    if save_supabase_state(SUPABASE_LATEST_RUN_LOG_KEY, log):
        print(f"Supabase {SUPABASE_LATEST_RUN_LOG_KEY} 저장")

    return path


def normalize_date_parts(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def extract_date_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    normalized_text = (
        text.translate(str.maketrans("０１２３４５６７８９．／－", "0123456789./-"))
        .replace("Ｒ", "R")
        .replace("\u2003", " ")
        .replace("\u3000", " ")
    )

    patterns = [
        r'(?P<y>20\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})',
        r'(?P<y>20\d{2})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日',
        r'(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>20\d{2})',
        r'(?P<d>\d{1,2})\s+(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Sept|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?P<y>20\d{2})',
        r'(?P<mon>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Sept|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(?P<d>\d{1,2}),?\s+(?P<y>20\d{2})',
    ]
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
        "nov": 11, "dec": 12,
    }

    for pattern in patterns:
        m = re.search(pattern, normalized_text, re.I)
        if not m:
            continue
        parts = m.groupdict()
        month = parts.get("m") or month_names.get(parts.get("mon", "").lower())
        if month:
            return normalize_date_parts(parts["y"], str(month), parts["d"])

    era = re.search(r'(?:令和|R)\s*(?P<y>\d{1,2})\s*[.年]\s*(?P<m>\d{1,2})\s*[.月]\s*(?P<d>\d{1,2})', normalized_text, re.I)
    if era:
        year = 2018 + int(era.group("y"))
        return normalize_date_parts(str(year), era.group("m"), era.group("d"))

    return None


def extract_date_from_url(url: str) -> Optional[str]:
    if not url:
        return None

    m = re.search(r'/((20\d{2})/(\d{1,2})/(\d{1,2}))(?:/|$)', url)
    if m:
        return normalize_date_parts(m.group(2), m.group(3), m.group(4))

    m = re.search(r'(20\d{2})(\d{2})(\d{2})(?=\.html?|[^\d]|$)', url)
    if m:
        return normalize_date_parts(m.group(1), m.group(2), m.group(3))

    m = re.search(r'/((20\d{2}))/er(\d{2})(\d{2})_', url, re.I)
    if m:
        return normalize_date_parts(m.group(2), m.group(3), m.group(4))

    return None


def extract_date_from_context(node, url: str = "") -> Optional[str]:
    date_from_url = extract_date_from_url(url)
    if date_from_url:
        return date_from_url

    texts = []
    target = node
    for _ in range(4):
        if not target:
            break
        texts.append(target.get_text(" ", strip=True))
        target = getattr(target, "parent", None)

    for text in texts:
        date = extract_date_from_text(text)
        if date:
            return date

    return None


DETAIL_DATE_SOURCE_PATTERNS = [
    "IPRdaily",
    "베트남 지식재산청",
]


def should_fetch_detail_date(source_name: str) -> bool:
    return any(pattern in source_name for pattern in DETAIL_DATE_SOURCE_PATTERNS)


def fetch_detail_date(source_name: str, url: str, timeout: int = 20) -> Optional[str]:
    if not should_fetch_detail_date(source_name):
        return None

    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome120",
            timeout=timeout,
            verify=False,
        )
        if not resp.ok:
            return None
    except Exception:
        return None

    soup = BeautifulSoup(decode_html_response(resp), 'html.parser')

    for selector in [
        'meta[property="article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[property="og:updated_time"]',
        'time',
        '.date',
        '.post-date',
        '.entry-date',
        '.published',
        '.metadata-info',
    ]:
        for node in soup.select(selector):
            text = node.get('content') or node.get('datetime') or node.get_text(" ", strip=True)
            date = extract_date_from_text(text)
            if date:
                return date

    return extract_date_from_text(soup.get_text(" ", strip=True)[:3000])


def decode_html_response(resp, default_encoding: str = "utf-8") -> str:
    content = resp.content
    if content.startswith(b'\xef\xbb\xbf'):
        return content.decode("utf-8-sig", errors="replace")

    head = content[:4096].decode("ascii", errors="ignore")
    match = re.search(r'charset=["\']?([\w.-]+)|encoding=["\']?([\w.-]+)', head, re.I)
    declared = next((g for g in match.groups() if g), None) if match else None

    if declared:
        normalized = declared.lower().replace("_", "-")
        if normalized in ("shift-jis", "shift_jis", "sjis", "windows-31j", "cp932"):
            return content.decode("cp932", errors="replace")
        try:
            return content.decode(declared, errors="replace")
        except LookupError:
            pass

    encoding = getattr(resp, "encoding", None) or default_encoding
    try:
        return content.decode(encoding, errors="replace")
    except LookupError:
        return content.decode(default_encoding, errors="replace")


def fetch_playwright(source: SourceConfig, timeout: int = 30000) -> List[Article]:
    print(f"[DEBUG] {source.name} 수집 시작 (Playwright 렌더링 모드)")

    html_content = ""
    # 🚨 [수정] 두 줄을 한 줄로 합쳐서 Playwright 전체에 스텔스를 씌웁니다!
    with Stealth().use_sync(sync_playwright()) as p:
        headless = os.getenv("PLAYWRIGHT_HEADLESS", "true").lower() != "false"
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"]
        )

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="zh-CN",
            viewport={'width': 1920, 'height': 1080},
            ignore_https_errors=True
        )

        page = context.new_page()

        try:
            print(f"[DEBUG] 이동 전 page.url = {page.url}")
            print(f"[DEBUG] 이동 대상 URL = {source.monitor_url}")

            resp = page.goto(
                source.monitor_url,
                timeout=30000,
                wait_until="commit"  # domcontentloaded보다 가볍게, 일단 응답만 오면 통과
            )

            print(f"[DEBUG] 이동 후 page.url = {page.url}")
            print(f"[DEBUG] 응답 객체 = {resp}")
            if resp:
                print(f"[DEBUG] status = {resp.status}")
                print(f"[DEBUG] final url = {resp.url}")

            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception as e:
                print(f"[DEBUG] domcontentloaded 대기 실패, 현재 DOM으로 계속 진행: {type(e).__name__}: {e}")

            wait_selector = getattr(source, "row_selector", "") or source.list_selector
            if wait_selector:
                print(f"[DEBUG] '{wait_selector}' 요소 대기")
                page.wait_for_selector(wait_selector, state="attached", timeout=15000)
            else:
                page.wait_for_timeout(5000)

            html_content = page.content()
            print(f"[DEBUG] Playwright 렌더링 완료 (길이: {len(html_content)})")
            print(f"[DEBUG] 최종 page.url = {page.url}")
            print(f"[DEBUG] 원문 HTML 미리보기:\n{html_content[:500]}")

        except Exception as e:
            print(f"[DEBUG] {source.name} Playwright 요청 실패: {type(e).__name__}: {e}")
            try:
                print(f"[DEBUG] 실패 시점 page.url = {page.url}")
                print(f"[DEBUG] 실패 시점 title = {page.title()}")
                print(f"[DEBUG] 실패 시점 HTML:\n{page.content()[:500]}")
                page.screenshot(path=f"debug_{source.name}.png", full_page=True)
            except Exception as ee:
                print(f"[DEBUG] 실패 진단 중 추가 오류: {ee}")
            return []
        finally:
            browser.close()

        # ... (상단 브라우저 닫는 finally: browser.close() 블록 직후부터 끝까지) ...

    import re
    if "CNIPA" in source.name:
        html_content = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', html_content, flags=re.DOTALL)

    soup = BeautifulSoup(html_content, 'html.parser')

    if getattr(source, "row_selector", ""):
        rows = soup.select(source.row_selector)
        print(f"[DEBUG] {source.name} row 후보 개수: {len(rows)}")

        items = []
        seen_url = set()
        for row in rows:
            try:
                title_el = row.select_one(source.title_selector or "a[href]")
            except Exception:
                title_el = None

            link_el = row if row.name == "a" and row.get("href") else None
            if not link_el:
                link_el = title_el if title_el and title_el.get("href") else row.select_one("a[href]")

            if not link_el:
                continue

            href = (link_el.get("href") or "").strip()
            title_source = title_el or link_el
            title = (
                title_source.get("title")
                or title_source.get_text(" ", strip=True)
                or link_el.get("aria-label")
                or ""
            ).strip()
            if not href or not title:
                continue

            full_url = urljoin(source.monitor_url, href)
            if looks_like_non_article(title, full_url):
                continue
            if not passes_source_allowlist(source.name, full_url):
                continue
            if full_url in seen_url:
                continue

            published = None
            if getattr(source, "date_selector", ""):
                try:
                    d = row.select_one(source.date_selector)
                except Exception:
                    d = None
                if d:
                    published = d.get_text(" ", strip=True)

            seen_url.add(full_url)
            items.append(Article(
                source=source.name,
                region=source.region,
                title=title,
                url=full_url,
                summary_raw='',
                published=published,
            ))
            if source_limit_reached(items, source):
                break

        if items:
            return items

    # 여기서부터 아까 만든 '완벽 로직'을 Playwright에도 동일하게 이식합니다!
    candidate_tags = soup.select(source.list_selector) if source.list_selector else []

    is_fallback = False
    if not candidate_tags:
        candidate_tags = soup.find_all('a', href=True)
        is_fallback = True

    print(f"[DEBUG] 후보 <a> 태그 개수: {len(candidate_tags)} (Fallback: {is_fallback})")

    links = []
    for a in candidate_tags:
        href = a.get('href')
        if not href:
            continue

        text = (a.get_text() or '').strip()
        if not text:
            text = (a.get('title') or '').strip()

        href = href.strip()
        if not text:
            print(f"[DEBUG] ❌ 탈락 (텍스트 없음) | {href}")
            continue

        full_url = urljoin(source.monitor_url, href)

        # 스크립트 내장 필터(너무 짧은 제목 등)에 걸리는지 확인
        if looks_like_non_article(text, full_url):
            print(f"[DEBUG] ❌ 탈락 (looks_like_non_article 필터) | {text[:20]}... | {full_url}")
            continue

        lowered = text.lower()
        score = 0

        if not is_fallback:
            score = 2
        else:
            if any(k in lowered for k in IP_KEYWORDS):
                score += 1
            if looks_like_article_url(full_url):
                score += 2
            if len(text) >= 20:
                score += 1

        if score >= 2:
            if not passes_source_allowlist(source.name, full_url):
                print(f"[DEBUG] ❌ 탈락 (URL 정규식 불일치) | {full_url}")
                continue

            # 모든 관문을 통과한 진짜 합격 기사!
            links.append((text, full_url))
            print(f"[DEBUG] ✅ 1차 합격! | {text[:20]}... | {full_url}")

        # ... (이하 기존 items append 로직 동일) ...
        else:
            # 텍스트가 10자 이상인 그럴싸한 기사인데 점수 미달로 버려진 경우
            if len(text) >= 10:
                print(f"[DEBUG] ❌ 탈락 (점수 미달: {score}점) | {text[:25]}... | {full_url}")

    items = []
    seen_url = set()
    for title, url in links:
        if url in seen_url:
            continue
        seen_url.add(url)
        items.append(
            Article(source=source.name, region=source.region, title=title, url=url, summary_raw='', published=None))
        if source_limit_reached(items, source):
            break

    return items


def fetch_rss(source: SourceConfig, timeout: int = 20) -> List[Article]:
    d = feedparser.parse(source.monitor_url)
    articles = []
    for entry in source_limited_sequence(list(d.entries), source):
        title = getattr(entry, 'title', '').strip()
        link = getattr(entry, 'link', '').strip()
        summary = getattr(entry, 'summary', '') or getattr(entry, 'description', '')
        published = (
            getattr(entry, 'published', None)
            or getattr(entry, 'updated', None)
            or getattr(entry, 'created', None)
        )

        if not title or not link:
            continue

        articles.append(Article(
            source=source.name,
            region=source.region,
            title=title,
            url=link,
            summary_raw=summary,
            published=published,
        ))
    return articles


HTML_LIST_SELECTORS_BY_SOURCE = {
    '미국 특허상표청 (USPTO)': [
        'main a[href]',
        '.region-content a[href]',
        '.usa-section a[href]',
        '.views-row a[href]',
    ],
    '미국 저작권청': [
        'main a[href]',
        '#maincontent a[href]',
        '.entry a[href]',
        '.news-item a[href]',
    ],
    '미국 연방거래위원회 (FTC)': [
        'main a[href]',
        '.view-content a[href]',
        '.views-row a[href]',
        '.node--view-mode-search-result a[href]',
    ],
    '미국 국제무역위원회(ITC)': [
        'main a[href]',
        '#main-content a[href]',
        '.view-content a[href]',
    ],
    '미국 무역대표부': [
        'main a[href]',
        '.view-content a[href]',
        '.views-row a[href]',
        '.field-content a[href]',
        '.usa-prose a[href]',
    ],
    '세계무역기구 (WTO)': [
        'main a[href]',
        '#content a[href]',
        '.newsitem a[href]',
        '.news a[href]',
        'td a[href]',
    ],
    '일본 특허청 (JPO)': [
        'main a[href]',
        '#main a[href]',
        '.mod-newsList a[href]',
        '.news-list a[href]',
    ],
    '일본 경제산업성 (METI)': [
        'main a[href]',
        '#main a[href]',
        '.listNews a[href]',
        '.module a[href]',
    ],
    '일본 문화청': [
        'main a[href]',
        '#main a[href]',
        '.news a[href]',
        '.list a[href]',
    ],
    '중국 국가지식산권국(CNIPA)': [
        'main a[href]',
        '.list a[href]',
        '.bd a[href]',
    ],
    '싱가포르 지식재산청 (IPOS)': [
        'main a[href]',
        '.news-list a[href]',
        '.listing a[href]',
        '.news-collection a[href]',
    ],
    '호주 지식재산청 (IP Australia)': [
        'main a[href]',
        '.news-list a[href]',
        '.view-content a[href]',
        '.content a[href]',
    ],
}

HTML_LIST_SELECTORS_BY_SOURCE = {
    '미국 특허상표청 (USPTO)': [
        'main a[href]',
        '.region-content a[href]',
        '.usa-section a[href]',
        '.views-row a[href]',
    ],
    '미국 저작권청': [
        'main a[href]',
        '#maincontent a[href]',
        '.entry a[href]',
        '.news-item a[href]',
    ],
    '미국 연방거래위원회 (FTC)': [
        'main a[href]',
        '.view-content a[href]',
        '.views-row a[href]',
        '.node--view-mode-search-result a[href]',
    ],
    '미국 국제무역위원회(ITC)': [
        'main a[href]',
        '#main-content a[href]',
        '.view-content a[href]',
    ],
    '미국 무역대표부': [
        'main a[href]',
        '.view-content a[href]',
        '.views-row a[href]',
        '.field-content a[href]',
        '.usa-prose a[href]',
    ],
    '세계무역기구 (WTO)': [
        'main a[href]',
        '#content a[href]',
        '.newsitem a[href]',
        '.news a[href]',
        'td a[href]',
    ],
    '일본 특허청 (JPO)': [
        'main a[href]',
        '#main a[href]',
        '.mod-newsList a[href]',
        '.news-list a[href]',
    ],
    '일본 경제산업성 (METI)': [
        'main a[href]',
        '#main a[href]',
        '.listNews a[href]',
        '.module a[href]',
    ],
    '일본 문화청': [
        'main a[href]',
        '#main a[href]',
        '.news a[href]',
        '.list a[href]',
    ],
    '중국 국가지식산권국(CNIPA)': [
        'main a[href]',
        '.list a[href]',
        '.bd a[href]',
    ],
    '싱가포르 지식재산청 (IPOS)': [
        'main a[href]',
        '.news-list a[href]',
        '.listing a[href]',
        '.news-collection a[href]',
    ],
    '호주 지식재산청 (IP Australia)': [
        'main a[href]',
        '.news-list a[href]',
        '.view-content a[href]',
        '.content a[href]',
    ],
}


def fetch_html_list(source: SourceConfig, timeout: int = 20) -> List[Article]:
    print(f"[DEBUG] {source.name} 수집 시작 (curl_cffi impersonate 모드)")
    print(
        f"[DEBUG] selectors | "
        f"list={source.list_selector!r} "
        f"row={getattr(source, 'row_selector', '')!r} "
        f"title={getattr(source, 'title_selector', '')!r} "
        f"date={getattr(source, 'date_selector', '')!r}"
    )
    try:
        # impersonate="chrome120" 옵션이 핵심입니다. 크롬 120 버전의 통신 지문을 완벽 복제합니다.
        resp = curl_requests.get(
            source.monitor_url,
            impersonate="chrome120",
            timeout=60,
            verify=False
        )

        print(f"\n[DEBUG] {source.name} HTTP 상태 코드: {resp.status_code}")

        # 200번대 응답이 아니면 에러 발생
        if not resp.ok:
            print(f"[DEBUG] 응답 에러: {resp.status_code}")
            return []
            # --- 여기에 디버그 코드를 추가합니다 ---
        if "CNIPA" in source.name:
            print(f"[DEBUG] 원문 HTML 미리보기 (최대 1000자):\n{resp.text[:1000]}\n")
            # -----------------------------------
    except Exception as e:
        print(f"[DEBUG] {source.name} 요청 실패: {e}")
        return []

    # ==============================================================
    # [추가] 중국 관공서(Hanweb) 특유의 CDATA 봉인 해제 로직
    if "patentsalon.com" in source.monitor_url:
        raw_html = resp.content.decode("cp932", errors="replace")
    else:
        raw_html = decode_html_response(resp)
    if "CNIPA" in source.name or "cnipa.gov.cn" in source.monitor_url:
        raw_html = re.sub(r'<!\[CDATA\[(.*?)\]\]>', r'\1', raw_html, flags=re.DOTALL)
        record_fragments = re.findall(r'<record>\s*(.*?)\s*</record>', raw_html, flags=re.DOTALL | re.IGNORECASE)
        if record_fragments:
            raw_html += "\n" + "\n".join(record_fragments)
        print("[DEBUG] CNIPA CDATA 텍스트 HTML로 변환 완료")
    # ==============================================================

    # 정상적으로 받아왔다면 BeautifulSoup으로 넘깁니다.
    soup = BeautifulSoup(raw_html, 'html.parser')
    # 0) row_selector가 있으면 row 기반 파싱을 우선 적용
    if getattr(source, "row_selector", ""):
        rows = soup.select(source.row_selector)
        print(f"[DEBUG] {source.name} row 후보 개수: {len(rows)}")

        items = []
        seen_title = set()
        seen_url = set()

        for row in rows:
            try:
                title_el = row.select_one(source.title_selector or "a[href]")
            except Exception:
                title_el = None

            link_el = row if row.name == "a" and row.get("href") else None
            if not link_el:
                link_el = title_el if title_el and title_el.get("href") else row.select_one("a[href]")

            if not link_el:
                continue

            href = (link_el.get("href") or "").strip()
            title_source = title_el or link_el
            title = (
                title_source.get("title")
                or title_source.get_text(" ", strip=True)
                or link_el.get("aria-label")
                or ""
            ).strip()

            if not href or not title:
                continue

            full_url = urljoin(source.monitor_url, href)

            if looks_like_non_article(title, full_url):
                continue

            if not passes_source_allowlist(source.name, full_url):
                continue

            published = None
            if getattr(source, "date_selector", ""):
                try:
                    d = row.select_one(source.date_selector)
                except Exception:
                    d = None
                if d:
                    published = d.get_text(" ", strip=True)
            if not published:
                published = extract_date_from_context(row, full_url)

            if title in seen_title or full_url in seen_url:
                continue

            seen_title.add(title)
            seen_url.add(full_url)

            items.append((title, full_url, published))

            if source_limit_reached(items, source):
                break

        articles = []
        for title, url, published in items:
            if not published:
                published = fetch_detail_date(source.name, url, timeout=timeout)
            articles.append(Article(
                source=source.name,
                region=source.region,
                title=title,
                url=url,
                summary_raw='',
                published=published,
            ))

        if articles:
            return articles

    # 1) selector 우선 수집 (config.yaml의 list_selector 최우선 적용)
    selectors = []
    if source.list_selector:
        selectors.append(source.list_selector)
    else:
        selectors = HTML_LIST_SELECTORS_BY_SOURCE.get(source.name, [])

    candidate_tags = []
    seen_tag_ids = set()

    for selector in selectors:
        try:
            found = soup.select(selector)
        except Exception:
            found = []

        for tag in found:
            tag_id = id(tag)
            if tag_id in seen_tag_ids:
                continue
            seen_tag_ids.add(tag_id)
            candidate_tags.append(tag)

    # 2) selector 결과가 없으면 전체 링크 fallback
    is_fallback = False
    if not candidate_tags:
        candidate_tags = soup.find_all('a', href=True)
        is_fallback = True

    print(f"[DEBUG] 후보 <a> 태그 개수: {len(candidate_tags)} (Fallback: {is_fallback})")

    links = []
    for a in candidate_tags:
        href = a.get('href')
        if not href:
            continue

        # 중국 사이트 등에서 태그 안이 비어있고 title 속성에만 글이 있는 경우 보완
        text = (a.get_text() or '').strip()
        if not text:
            text = (a.get('title') or '').strip()

        href = href.strip()

        if not text:
            continue

        full_url = urljoin(source.monitor_url, href)

        if looks_like_non_article(text, full_url):
            continue

        lowered = text.lower()
        score = 0

        # 선택자 프리패스 로직
        if not is_fallback:
            score = 2  # 선택자로 정확히 잡았으면 무조건 합격
        else:
            # Fallback 상태(전체 링크)일 때만 깐깐하게 채점
            if any(k in lowered for k in IP_KEYWORDS):
                score += 1
            if looks_like_article_url(full_url):
                score += 2
            if len(text) >= 20:
                score += 1

        if score >= 2:
            if not passes_source_allowlist(source.name, full_url):
                continue
            published = extract_date_from_context(a, full_url)
            links.append((text, full_url, published))
    seen_title = set()
    seen_url = set()
    items = []

    for title, url, published in links:
        if title in seen_title:
            continue
        if url in seen_url:
            continue

        seen_title.add(title)
        seen_url.add(url)

        items.append((title, url, published))
        if source_limit_reached(items, source):
            break

    articles = []
    for title, url, published in items:
        if not published:
            published = fetch_detail_date(source.name, url, timeout=timeout)
        articles.append(Article(
            source=source.name,
            region=source.region,
            title=title,
            url=url,
            summary_raw='',
            published=published,
        ))

    return articles


def fetch_algolia_api(source: SourceConfig, timeout: int = 20) -> List[Article]:
    print(f"[DEBUG] {source.name} 수집 시작 (Algolia API 모드)")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Origin': 'https://www.euipo.europa.eu',
        'Referer': 'https://www.euipo.europa.eu/',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    # Algolia에 보낼 검색 조건 (cURL의 data-raw를 파이썬 딕셔너리로 변환)
    # max_items 제한은 비활성화했으므로 넉넉한 기본 페이지 크기로 가져옵니다.
    payload = {
        "query": "",
        "page": 0,
        "hitsPerPage": 50,
        "facets": ["*"]
    }
    if source.algolia_filters:
        payload["filters"] = source.algolia_filters

    try:
        # Algolia는 POST 방식(requests.post)으로 요청해야 합니다.
        resp = requests.post(source.monitor_url, headers=headers, json=payload, timeout=timeout)
        print(f"\n[DEBUG] {source.name} HTTP 상태 코드: {resp.status_code}")
        resp.raise_for_status()

        data = resp.json()
        hits = data.get('hits', [])  # Algolia는 결과물을 항상 'hits' 배열 안에 줍니다.

    except Exception as e:
        print(f"[DEBUG] {source.name} 요청 실패: {e}")
        return []

    articles = []
    for item in hits:
        # 1. 제목 찾기 (EUIPO는 보통 'title'을 쓰지만 없을 경우 'description'의 앞부분 활용)
        title = item.get('title') or item.get('post_title') or item.get('label') or item.get('subject')

        # 2. URL 찾기 (EUIPO 알골리아는 보통 'url' 필드를 제공함)
        raw_url = (
            item.get('url')
            or item.get('permalink')
            or item.get('path')
            or item.get('link')
            or item.get('fullSlug')
            or item.get('objectID')
        )

        if not title or not raw_url:
            # 데이터 구조를 파악하기 위해 필드명을 디버그 출력
            # print(f"[DEBUG] 필드 누락 - 전체 키: {item.keys()}")
            continue

        # 3. HTML 태그 제거 및 정리
        title = BeautifulSoup(str(title), 'html.parser').get_text(strip=True)
        full_url = urljoin(source.homepage, raw_url)

        # 4. 요약문 (body/content/description 중 있는 것 선택)
        summary = item.get('content') or item.get('description') or item.get('excerpt') or item.get('summary') or ""
        clean_summary = BeautifulSoup(str(summary), 'html.parser').get_text(separator=' ', strip=True)
        published = item.get('published') or item.get('date') or item.get('post_date')

        articles.append(Article(
            source=source.name,
            region=source.region,
            title=title,
            url=full_url,
            summary_raw=clean_summary,
            published=published
        ))

    print(f"[DEBUG] {source.name} 기사 {len(articles)}개 파싱 성공")
    return articles

def fetch_json_api(source: SourceConfig, timeout: int = 20) -> List[Article]:
    headers = {'User-Agent': 'Mozilla/5.0 IP-Monitor-MVP'}
    print(f"[DEBUG] {source.name} JSON API 요청 중: {source.monitor_url}")

    try:
        resp = requests.get(source.monitor_url, headers=headers, timeout=timeout)
        resp.raise_for_status()

        data = resp.json()
        if isinstance(data, dict):
            for key in ('items', 'results', 'data', 'articles'):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                print(f"[DEBUG] {source.name} JSON 리스트 컨테이너를 찾지 못함")
                return []
        elif not isinstance(data, list):
            print(f"[DEBUG] {source.name} JSON 응답 형식 미지원: {type(data).__name__}")
            return []

    except Exception as e:
        print(f"[DEBUG] {source.name} JSON 로드 실패: {e}")
        return []

    articles = []

    # max_items 제한은 비활성화했습니다.
    for item in source_limited_sequence(data, source):
        title = item.get('title', '').strip()
        raw_url = item.get('url', '').strip()

        if not title or not raw_url:
            continue

        # 상대경로 URL을 절대경로로 변환
        full_url = urljoin(source.homepage, raw_url)

        # HTML 태그 제거 (날짜와 요약문에 섞여 있는 태그 청소)
        raw_body = item.get('body') or item.get('desc') or item.get('description') or item.get('summary') or ''
        clean_body = BeautifulSoup(raw_body, 'html.parser').get_text(separator=' ', strip=True) if raw_body else ''

        raw_date = item.get('date', '')
        clean_date = BeautifulSoup(raw_date, 'html.parser').get_text(separator=' ', strip=True) if raw_date else None

        articles.append(Article(
            source=source.name,
            region=source.region,
            title=title,
            url=full_url,
            summary_raw=clean_body,
            published=clean_date,
        ))

    print(f"[DEBUG] {source.name} JSON에서 {len(articles)}개 기사 파싱 성공")
    return articles


def fetch_oecd_search_api(source: SourceConfig, timeout: int = 20) -> List[Article]:
    print(f"[DEBUG] {source.name} 수집 시작 (OECD Search API 모드)")

    headers = {
        'User-Agent': 'Mozilla/5.0 IP-Monitor-MVP',
        'Accept': 'application/json',
    }

    try:
        resp = requests.get(source.monitor_url, headers=headers, timeout=timeout)
        print(f"\n[DEBUG] {source.name} HTTP 상태 코드: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[DEBUG] {source.name} OECD Search API 로드 실패: {e}")
        return []

    results = data.get('results', [])
    articles = []

    for item in source_limited_sequence(results, source):
        title = str(item.get('title') or '').strip()
        raw_url = str(item.get('url') or '').strip()

        if not title or not raw_url:
            continue

        summary = str(item.get('description') or '').strip()
        published = item.get('publicationDateTime') or item.get('startDateTime')

        articles.append(Article(
            source=source.name,
            region=source.region,
            title=title,
            url=urljoin(source.homepage, raw_url),
            summary_raw=summary,
            published=published,
        ))

    print(f"[DEBUG] {source.name} OECD Search API에서 {len(articles)}개 기사 파싱 성공")
    return articles


def fetch_sxa_search_api(source: SourceConfig, timeout: int = 20) -> List[Article]:
    """Fetch Sitecore SXA search-result pages that render cards from /sxa/search/results/."""
    headers = {
        'User-Agent': 'Mozilla/5.0 IP-Monitor-MVP',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest',
        'Referer': source.monitor_url,
    }
    print(f"[DEBUG] {source.name} SXA 페이지 요청 중: {source.monitor_url}")

    try:
        page_resp = curl_requests.get(
            source.monitor_url,
            headers={'User-Agent': headers['User-Agent']},
            impersonate='chrome120',
            timeout=timeout,
            verify=False,
        )
        print(f"[DEBUG] {source.name} SXA 페이지 상태 코드: {page_resp.status_code}")
        page_resp.raise_for_status()
        soup = BeautifulSoup(page_resp.text, 'html.parser')
        result_el = soup.select_one('.search-results[data-properties]')
        if not result_el:
            print(f"[DEBUG] {source.name} SXA search-results 컴포넌트를 찾지 못함")
            return []

        props = json.loads(result_el.get('data-properties') or '{}')
        endpoint = str(props.get('endpoint') or '/sxa/search/results/').lstrip('/')
        api_url = urljoin(source.homepage, endpoint)
        params = {
            'v': props.get('v', ''),
            's': props.get('s', ''),
            'itemid': props.get('itemid', ''),
            'p': int(props.get('p') or 50) or 50,
        }
        if props.get('sig'):
            params['sig'] = props.get('sig')
        if props.get('l'):
            params['l'] = props.get('l')
        if props.get('defaultSortOrder'):
            params['o'] = props.get('defaultSortOrder')

        print(f"[DEBUG] {source.name} SXA API 요청 중: {api_url}")
        resp = curl_requests.get(
            api_url,
            params=params,
            headers=headers,
            impersonate='chrome120',
            timeout=timeout,
            verify=False,
        )
        print(f"[DEBUG] {source.name} SXA API 상태 코드: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[DEBUG] {source.name} SXA API 로드 실패: {e}")
        return []

    articles = []
    seen_url = set()
    for item in source_limited_sequence(data.get('Results', []), source):
        card = BeautifulSoup(item.get('Html') or '', 'html.parser')
        title_el = card.select_one(source.title_selector or '.card-title')
        link_el = card.select_one(source.list_selector or 'a[href]')
        title = title_el.get_text(" ", strip=True) if title_el else ''
        raw_url = item.get('Url') or (link_el.get('href') if link_el else '')
        if not title or not raw_url:
            continue

        full_url = urljoin(source.homepage, raw_url)
        if full_url in seen_url:
            continue

        published = None
        text = card.get_text(" ", strip=True)
        date_match = re.search(
            r'\b\d{1,2}\s+'
            r'(?:January|February|March|April|May|June|July|August|September|October|November|December)'
            r'\s+\d{4}\b',
            text,
        )
        if date_match:
            published = date_match.group(0)

        summary = ''
        summary_el = card.select_one('.card-text, .description, p')
        if summary_el:
            summary = summary_el.get_text(" ", strip=True)

        seen_url.add(full_url)
        articles.append(Article(
            source=source.name,
            region=source.region,
            title=title,
            url=full_url,
            summary_raw=summary,
            published=published,
        ))

    print(f"[DEBUG] {source.name} SXA API에서 {len(articles)}개 기사 파싱 성공")
    return articles


def fetch_people_search_api(source: SourceConfig, timeout: int = 20) -> List[Article]:
    """Fetch People.cn keyword search results from its Nuxt-backed search API."""
    parsed = urlparse(source.monitor_url)
    query = parse_qs(parsed.query)
    keyword = unquote((query.get('keyword') or query.get('key') or [''])[0]).strip()

    if not keyword:
        print(f"[DEBUG] {source.name} People.cn 검색 키워드를 찾지 못함")
        return []

    api_url = 'http://search.people.cn/search-platform/front/search'
    payload = {
        'key': keyword,
        'page': 1,
        'limit': 50,
        'hasTitle': True,
        'hasContent': True,
        'isFuzzy': True,
        'type': 0,
        'sortType': 2,
        'startTime': 0,
        'endTime': 0,
        'belongsId': [],
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 IP-Monitor-MVP',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json;charset=UTF-8',
        'Origin': 'http://search.people.cn',
        'Referer': source.monitor_url,
    }

    print(f"[DEBUG] {source.name} People.cn 검색 API 요청 중: {keyword}")

    try:
        resp = curl_requests.post(
            api_url,
            json=payload,
            headers=headers,
            impersonate='chrome120',
            timeout=timeout,
            verify=False,
        )
        print(f"[DEBUG] {source.name} People.cn API 상태 코드: {resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[DEBUG] {source.name} People.cn 검색 API 로드 실패: {e}")
        return []

    records = ((data.get('data') or {}).get('records') or []) if isinstance(data, dict) else []
    articles = []
    seen_url = set()

    for item in records:
        title = BeautifulSoup(str(item.get('title') or ''), 'html.parser').get_text(' ', strip=True)
        raw_url = str(item.get('url') or item.get('originUrl') or '').strip()

        if not title or not raw_url:
            continue

        full_url = urljoin(source.homepage, raw_url)
        if full_url in seen_url:
            continue

        summary = BeautifulSoup(str(item.get('content') or ''), 'html.parser').get_text(' ', strip=True)
        published = None
        raw_time = item.get('displayTime') or item.get('inputTime')
        if raw_time:
            try:
                published = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(raw_time) / 1000))
            except Exception:
                published = str(raw_time)

        seen_url.add(full_url)
        articles.append(Article(
            source=source.name,
            region=source.region,
            title=title,
            url=full_url,
            summary_raw=summary,
            published=published,
        ))

        if source_limit_reached(articles, source):
            break

    print(f"[DEBUG] {source.name} People.cn 검색 API에서 {len(articles)}개 기사 파싱 성공")
    return articles


def fetch_articles_for_source(source: SourceConfig, timeout: int = 20) -> List[Article]:
    if source.mode == 'rss':
        return fetch_rss(source, timeout=timeout)
    if source.mode == 'html_list':
        return fetch_html_list(source, timeout=timeout)
    if source.mode == 'json_api':                 # <--- 이 두 줄을
        return fetch_json_api(source, timeout)    # <--- 추가해 줍니다.
    if source.mode == 'oecd_search_api':
        return fetch_oecd_search_api(source, timeout)
    if source.mode == 'sxa_search':
        return fetch_sxa_search_api(source, timeout)
    if source.mode == 'people_search_api':
        return fetch_people_search_api(source, timeout)
    if source.mode == 'algolia':  # <--- 추가할 부분
        return fetch_algolia_api(source, timeout)  # <--- 추가할 부분
    if source.mode == 'playwright':  # <--- 추가
        return fetch_playwright(source, timeout * 1000)  # playwright는 ms 단위 사용
    return []


class ClaudeClient:
    def __init__(self, model: str = 'claude-sonnet-4-6'):
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise RuntimeError('환경변수 ANTHROPIC_API_KEY가 설정되지 않았습니다.')
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def analyze_article(self, art: Article) -> AnalyzedArticle:
        prompt = f"""
당신은 지식재산(IP) 정책·제도 동향 분석가입니다.

다음 기사 정보를 보고 한국 정책의사결정자 관점에서 중요도를 평가하고, 한국어로 요약해 주세요.

[기사 메타정보]
- 출처 기관/매체: {art.source}
- 지역/국가: {art.region}
- 제목: {art.title}
- URL: {art.url}
- 원문 요약/발췌(있으면): {art.summary_raw}

요구 사항:
1. 중요도 점수를 0~100 사이 정수로 매겨라. (높을수록 한국 정책 측면 중요)
2. 아래 기준을 종합적으로 고려하라.
   - IP 법·제도·정책 변경 가능성
   - 국제 규범 변화(WIPO, WTO, FTA 등) 연관성
   - AI, 데이터, 표준필수특허, 디지털 저작권 등 전략 분야 관련성
   - 국내 제도 개선 논의에 활용 가능성
   - 시의성·긴급성
3. 한국어로 3~5문장 요약을 작성하라. 요약에는 시사점이나 평가가 들어가지 않고, 링크에서 나타난 사실만을 넣는다.
   - 원문에 한국, 대한민국, South Korea, Korea, KIPO 등 한국 관련 표현이 직접 등장하지 않으면 요약에 한국을 언급하지 마라.
   - 원문이 한국을 직접 언급하지 않는데 "한국도 평가 대상", "한국에 직접 영향", "한국이 지정될 경우"처럼 확정적·가정적 한국 중심 문장을 만들지 마라.
4. 1~2단어의 카테고리(예: 특허정책, 저작권, AI규제, 표준특허, 무역분쟁 등)를 정하라.
5. 핵심 시사점 2~3문장을 작성하라.
   - 원문에 한국이 직접 언급되지 않은 경우 시사점은 "한국에 대한 직접 영향"이 아니라 "한국 정책당국/기업이 참고할 만한 간접 동향" 수준으로 표현하라.
   - 한국 관련성은 제도 비교, 통상 환경, 해외 진출 기업 리스크 등 간접적 의미로만 설명하라.
6. '한국'이나 'South Korea'를 직접 언급하고 있으면 중요도 점수를 상향하고, 요약에 해당 내용을 포함하라. 직접 언급이 없으면 한국 관련성만으로 과도하게 점수를 올리지 마라.
7. 같은 사건·보고서·판례·법안·정책 발표·기업 발표를 묶을 수 있도록 topic_key와 topic_label을 작성하라.
   - topic_key는 영문 소문자 slug로 작성한다. 예: "2026-ustr-special-301-report", "uspto-gen-ai-patent-examination"
   - topic_label은 사람이 읽기 쉬운 짧은 이슈명으로 작성한다. 예: "USTR 2026 Special 301 Report"
   - 같은 이슈를 다른 매체가 보도한 경우 동일한 topic_key가 나오도록 일반적이고 안정적인 이름을 사용한다.
8. issue_region을 작성하라.
   - issue_region은 출처 매체의 소재지가 아니라 기사에서 다루는 실제 정책·분쟁·시장 이슈의 대상 지역이다.
   - 예: 미국 매체가 EU의 IP 정책을 다루면 issue_region은 "유럽" 또는 "EU"로 작성한다.
   - 전세계 또는 다자기구 이슈면 "국제" 또는 "국제기구"로 작성한다.
   - 명확하지 않으면 기사 메타정보의 지역/국가 값을 사용한다.

추가 규칙:
- factual summary와 policy implication을 엄격히 분리하라. `summary_ko`에는 원문에 없는 한국 관련 추론을 넣지 말고, 필요한 경우 `key_points`에 간접 시사점으로만 작성하라.
- "한국도", "국내 제도", "한국 정책당국" 같은 표현은 원문 직접 언급이 없으면 `summary_ko`에서 금지한다.
- 단순 기관 소개, 서비스 소개, 검색도구 안내, 데이터베이스 안내, 메뉴 페이지, 고정된 법령 원문 페이지(예: Title 17 전체 텍스트)는 정책 변경이 없다면 중요도를 0~20 사이로 낮게 평가하라.
- 특허검색 서비스, 특허·저작권 등록부, 공보 시스템, 포털, 안내 페이지처럼 '운영 중인 툴/서비스' 중심인 문서는, 새로운 정책·제도 도입이나 변경 내용을 포함하지 않는 한 중요도를 0~20으로 제한하라.
- "Patents", "Patent basics", "Search our patent database" 같은 제도·툴 안내 랜딩 페이지는 신규 정책 내용이 없으면 중요도를 0~25로 제한하라.
- 내비게이션용 텍스트(예: "Skip to main content", "Skip to footer")나 메뉴/섹션 제목(예: "Understanding IP", "Types of IP")처럼 실제 기사·공지로 보이지 않으면 중요도를 0~10으로 낮추고, 요약에서 '실질적인 내용이 없는 페이지로 보이며, 재수집 또는 본문 확인 필요'라고 명시하라.

JSON으로만 응답하라:
{{
  "importance_score": 87,
  "category": "AI규제",
  "summary_ko": "…",
  "key_points": ["…"],
  "topic_key": "uspto-ai-patent-examination",
  "topic_label": "USPTO AI Patent Examination",
  "issue_region": "미국"
}}
"""
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=800,
            temperature=0.1,
            messages=[{'role': 'user', 'content': prompt}]
        )

        text = resp.content[0].text
        m = re.search(r'\{.*\}', text, re.S)
        data = {}
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {}

        importance = int(data.get('importance_score', 50))
        importance = max(0, min(100, importance))

        category = str(data.get('category', '기타')).strip() or '기타'
        summary_ko = str(data.get('summary_ko', '')).strip()
        key_points = data.get('key_points', [])
        if not isinstance(key_points, list):
            key_points = [str(key_points)]
        key_points = [str(x).strip() for x in key_points if str(x).strip()]
        topic_key = normalize_topic_key(str(data.get('topic_key', '')).strip())
        topic_label = str(data.get('topic_label', '')).strip()
        issue_region = str(data.get('issue_region', '')).strip() or art.region

        return AnalyzedArticle(
            source=art.source,
            region=art.region,
            title=art.title,
            url=art.url,
            published=art.published,
            summary_ko=summary_ko,
            importance_score=importance,
            category=category,
            key_points=key_points,
            raw_excerpt=art.summary_raw,
            topic_key=topic_key,
            topic_label=topic_label,
            issue_region=issue_region,
        )


DIGEST_TOPIC_STOPWORDS = {
    "about", "after", "again", "against", "also", "and", "annual", "are", "from",
    "into", "its", "new", "news", "not", "over", "policy", "press", "release",
    "releases", "report", "reports", "said", "says", "the", "their", "this",
    "through", "with", "year", "years",
    "관련", "기타", "동향", "발표", "보도", "분야", "정책", "제도",
}


OFFICIAL_SOURCE_KEYWORDS = [
    "ustr", "uspto", "whitehouse", "copyright office", "ftc", "itc",
    "wipo", "wto", "oecd", "epo", "euipo", "upc",
    "특허청", "무역대표부", "백악관", "연방거래위원회", "국제무역위원회",
    "세계지식재산기구", "세계무역기구", "경제협력개발기구", "통합특허법원",
    "일본 특허청", "일본 지식재산전략본부", "일본 경제산업성", "일본 총무성",
    "일본 문화청", "일본 후생노동성", "일본 지적재산고등재판소",
    "중국 상무부", "중국 시장감독관리총국", "중국 국가판권국",
    "중국 최고인민법원", "중국 최고인민검찰원", "중국 지식재산국",
    "베트남 지식재산청", "말레이시아 지식재산청", "싱가포르 지식재산청",
    "캐나다 지식재산청", "호주 지식재산청", "필리핀 지식재산청",
]

MEDIA_SOURCE_KEYWORDS = [
    "mlex", "iam", "ip watchdog", "patently-o", "patentlyo", "patent salon", "patent result",
    "ipr daily", "iprdaily", "asia ip law", "thomson reuters",
    "bloomberg", "nikkei", "요미우리", "닛케이", "reuters",
]

OFFICIAL_DOMAIN_HINTS = [
    ".gov", ".go.", ".gouv", ".gc.ca", ".gov.uk", ".europa.eu",
    "wipo.int", "wto.org", "oecd.org", "epo.org", "euipo.europa.eu",
    "unifiedpatentcourt.org", "courts.go.jp", "jpo.go.jp",
]

DIGEST_DIRECT_REPORT_PATTERNS = [
    r"\brules?\b", r"\bruled\b", r"\bjudg(e)?ment\b", r"\bdecision\b",
    r"\border(s|ed)?\b", r"\bfinding(s)?\b", r"\bfinds?\b",
    r"\breleases?\b", r"\bannounces?\b", r"\bissues?\b", r"\bpublishes?\b",
    r"\badopts?\b", r"\bapproves?\b", r"\bpasses?\b", r"\bfiles?\b",
    r"\bsettles?\b", r"\blaunches?\b", r"\bopens?\b", r"\bconsultation\b",
    r"\bfinal rule\b", r"\bhigh court\b", r"\bcourt\b",
    r"판결", r"결정", r"명령", r"발표", r"공개", r"공표", r"고시", r"시행",
    r"제정", r"개정", r"승인", r"출원", r"제소", r"합의", r"개시",
]

DIGEST_COMMENTARY_PATTERNS = [
    r"\bframework\b", r"\bimplication(s)?\b", r"\banalysis\b",
    r"\bcommentary\b", r"\bopinion\b", r"\bexplainer\b", r"\broundup\b",
    r"\bwhat\b.*\bmeans\b", r"\bbarks?\s*&\s*bites?\b", r"\bnote on\b",
    r"시사점", r"해설", r"논평", r"칼럼", r"정리", r"브리핑",
]


def normalize_topic_key(value: str) -> str:
    value = html.unescape(value or "").lower().strip()
    value = re.sub(r'[^a-z0-9]+', '-', value)
    value = re.sub(r'-+', '-', value).strip('-')
    return value


def digest_source_authority_score(item: AnalyzedArticle) -> int:
    source = normalize_topic_text(item.source or "")
    url = (item.url or "").lower()
    domain = urlparse(url).netloc.lower()

    if any(domain_hint in domain for domain_hint in OFFICIAL_DOMAIN_HINTS):
        return 4
    if any(keyword in source for keyword in OFFICIAL_SOURCE_KEYWORDS):
        return 4

    if any(keyword in source or keyword in domain for keyword in MEDIA_SOURCE_KEYWORDS):
        return 1

    return 2


def digest_direct_report_score(item: AnalyzedArticle) -> int:
    title = normalize_topic_text(item.title or "")
    source = normalize_topic_text(item.source or "")
    raw_excerpt = normalize_topic_text(item.raw_excerpt or "")
    url = (item.url or "").lower()
    domain = urlparse(url).netloc.lower()
    text = " ".join([title, source, raw_excerpt])
    score = 0

    if any(domain_hint in domain for domain_hint in OFFICIAL_DOMAIN_HINTS):
        score += 4
    if any(keyword in source for keyword in OFFICIAL_SOURCE_KEYWORDS):
        score += 4

    for pattern in DIGEST_DIRECT_REPORT_PATTERNS:
        if re.search(pattern, text):
            score += 2
            break

    if re.search(r"\b(original|exclusive|breaking)\b", text):
        score += 1
    if re.search(r"\b(feed|rss|blog)\b", url):
        score -= 1
    if "patentlyo" in domain or "patently-o" in source:
        score -= 1

    for pattern in DIGEST_COMMENTARY_PATTERNS:
        if re.search(pattern, text):
            score -= 2
            break

    return score


def choose_digest_representative(items: List[AnalyzedArticle]) -> AnalyzedArticle:
    return max(
        items,
        key=lambda item: (
            digest_source_authority_score(item),
            digest_direct_report_score(item),
            item.importance_score,
            len(item.summary_ko or ""),
        )
    )


def normalize_topic_text(text: str) -> str:
    text = html.unescape(text or "").lower()
    text = re.sub(r'https?://\S+', ' ', text)
    text = re.sub(r'[\W_]+', ' ', text, flags=re.UNICODE)
    return re.sub(r'\s+', ' ', text).strip()


def digest_topic_text(item: AnalyzedArticle) -> str:
    return " ".join([
        item.title or "",
        item.category or "",
        item.summary_ko or "",
        " ".join(item.key_points or []),
    ])


def extract_digest_topic_keys(item: AnalyzedArticle) -> set:
    text = normalize_topic_text(digest_topic_text(item))
    keys = set()
    topic_key = normalize_topic_key(getattr(item, "topic_key", ""))
    if topic_key:
        keys.add(f"topic:{topic_key}")

    if "samsung" in text and "zte" in text and "frand" in text:
        keys.add("entity:samsung-zte-frand")
    if "392 million" in text and "frand" in text and ("zte" in text or "samsung" in text):
        keys.add("entity:samsung-zte-frand")
    if "frand" in text and "meade" in text and ("zte" in text or "samsung" in text):
        keys.add("entity:samsung-zte-frand")
    if "frand" in text and ("english high court" in text or "uk patent judgment" in text) and (
        "zte" in text or "samsung" in text
    ):
        keys.add("entity:samsung-zte-frand")

    if (
        "special 301" in text
        or "스페셜 301" in text
        or "priority foreign country" in text
        or "최대 우려국" in text
        or "最大の懸念国" in text
        or ("ustr" in text and "watch list" in text)
    ):
        keys.add("special-301")
    if "skinny label" in text or ("amarin" in text and "hikma" in text):
        keys.add("skinny-label")
    if ("gen ai" in text or "generative ai" in text or "생성형 ai" in text) and (
        "patent examination" in text or "특허 심사" in text
    ):
        keys.add("gen-ai-patent-examination")
    if "wipo adr" in text or ("wipo" in text and "domain" in text and "artificial intelligence" in text):
        keys.add("wipo-ai-adr")

    return keys


def extract_digest_topic_tokens(item: AnalyzedArticle) -> set:
    text = normalize_topic_text(digest_topic_text(item))
    tokens = set()
    for token in text.split():
        if token in DIGEST_TOPIC_STOPWORDS:
            continue
        if len(token) < 3 and not token.isdigit():
            continue
        tokens.add(token)
    return tokens


def same_digest_topic(
    keys: set,
    tokens: set,
    cluster: DigestTopicCluster
) -> bool:
    if keys and cluster.topic_keys:
        return bool(keys.intersection(cluster.topic_keys))

    if not tokens or not cluster.representative_tokens:
        return False

    intersection = tokens.intersection(cluster.representative_tokens)
    union = tokens.union(cluster.representative_tokens)
    jaccard = len(intersection) / len(union) if union else 0
    return len(intersection) >= 4 and jaccard >= 0.45


def cluster_digest_topics(items: List[AnalyzedArticle]) -> List[DigestTopicCluster]:
    clusters: List[DigestTopicCluster] = []
    sorted_items = sorted(items, key=lambda x: x.importance_score, reverse=True)

    for item in sorted_items:
        keys = extract_digest_topic_keys(item)
        tokens = extract_digest_topic_tokens(item)

        matched = None
        for cluster in clusters:
            if same_digest_topic(keys, tokens, cluster):
                matched = cluster
                break

        if matched:
            matched.items.append(item)
            matched.topic_keys.update(keys)
        else:
            clusters.append(DigestTopicCluster(
                representative=item,
                items=[item],
                topic_keys=keys,
                representative_tokens=tokens,
            ))

    for cluster in clusters:
        cluster.representative = choose_digest_representative(cluster.items)
        cluster.items.sort(
            key=lambda item: (
                item is cluster.representative,
                digest_source_authority_score(item),
                digest_direct_report_score(item),
                item.importance_score,
                len(item.summary_ko or ""),
            ),
            reverse=True,
        )

    clusters.sort(
        key=lambda cluster: (
            max(item.importance_score for item in cluster.items),
            cluster.representative.importance_score,
        ),
        reverse=True,
    )
    return clusters


def compact_digest_text(text: str, max_chars: int = 220) -> str:
    text = html.unescape(text or "")
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= max_chars:
        return text

    cut = text[:max_chars + 1]
    sentence_end = max(cut.rfind("."), cut.rfind("다."), cut.rfind("요."), cut.rfind("음."))
    if sentence_end >= 80:
        return cut[:sentence_end + 1].strip()
    return text[:max_chars].rstrip() + "..."


def compact_digest_key_point(points: List[str], max_chars: int = 180) -> str:
    if not points:
        return ""
    return compact_digest_text(points[0], max_chars=max_chars)


def digest_region_label(item: AnalyzedArticle) -> str:
    issue_region = (getattr(item, "issue_region", "") or "").strip()
    source_region = (item.region or "").strip()
    if issue_region and source_region and issue_region != source_region:
        return f"{issue_region} (출처지역: {source_region})"
    return issue_region or source_region or "-"


def run_date_from_run_id(run_id: str) -> str:
    try:
        return datetime.strptime(run_id[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")


def parse_iso_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except Exception:
        return None


def digest_cluster_topic_key(cluster: DigestTopicCluster) -> str:
    stable_keys = sorted(
        str(key).replace("entity:", "", 1)
        for key in cluster.topic_keys
        if str(key).startswith("entity:")
    )
    if stable_keys:
        return stable_keys[0]

    representative_key = normalize_topic_key(getattr(cluster.representative, "topic_key", ""))
    if representative_key:
        return representative_key

    topic_keys = sorted(str(key) for key in cluster.topic_keys if str(key).strip())
    if topic_keys:
        return topic_keys[0].replace("topic:", "", 1)

    return ""


def digest_cluster_max_score(cluster: DigestTopicCluster) -> int:
    return max((item.importance_score for item in cluster.items), default=0)


def has_substantial_update_signal(cluster: DigestTopicCluster) -> bool:
    text = normalize_topic_text(" ".join(
        f"{item.title} {item.summary_ko} {' '.join(item.key_points or [])}"
        for item in cluster.items
    ))
    update_keywords = [
        "ruling", "judgment", "decision", "lawsuit filed", "investigation",
        "sanction", "settlement", "law passed", "signed into law",
        "판결", "결정", "조사", "제재", "소송", "법안 통과", "시행",
    ]
    return any(keyword in text for keyword in update_keywords)


def should_suppress_recent_digest_topic(
    cluster: DigestTopicCluster,
    sent_topic: Optional[Dict[str, Any]],
    run_date: str,
    recent_days: int
) -> bool:
    if not sent_topic:
        return False

    last_sent = parse_iso_date(str(sent_topic.get("last_sent_date", "")))
    current = parse_iso_date(run_date)
    if not last_sent or not current:
        return False

    if current - last_sent > timedelta(days=recent_days):
        return False

    previous_score = int(sent_topic.get("representative_score") or 0)
    current_score = digest_cluster_max_score(cluster)
    previous_authority = int(sent_topic.get("representative_authority") or 0)
    current_authority = digest_source_authority_score(cluster.representative)

    if current_score >= previous_score + 10:
        return False
    if current_authority > previous_authority and current_score >= previous_score:
        return False
    if has_substantial_update_signal(cluster) and current_score >= 85:
        return False

    return True


def select_digest_clusters(
    analyzed: List[AnalyzedArticle],
    top_n: int = 5,
    min_importance: int = 0,
    sent_topics: Optional[List[Dict[str, Any]]] = None,
    recent_topic_days: int = 3,
    run_date: Optional[str] = None,
) -> tuple:
    """Select top digest topics, counting one clustered issue as one slot."""
    filtered = [x for x in analyzed if x.importance_score >= min_importance]
    topic_clusters = cluster_digest_topics(filtered)
    run_date = run_date or time.strftime("%Y-%m-%d")
    sent_index = {
        str(item.get("topic_key", "")): item
        for item in (sent_topics or [])
        if item.get("topic_key")
    }

    selected = []
    skipped = []
    for cluster in topic_clusters:
        topic_key = digest_cluster_topic_key(cluster)
        sent_topic = sent_index.get(topic_key) if topic_key else None
        if topic_key and should_suppress_recent_digest_topic(
            cluster,
            sent_topic,
            run_date,
            recent_topic_days,
        ):
            skipped.append({
                "topic_key": topic_key,
                "topic_label": getattr(cluster.representative, "topic_label", ""),
                "representative_title": cluster.representative.title,
                "last_sent_date": sent_topic.get("last_sent_date") if sent_topic else "",
                "score": digest_cluster_max_score(cluster),
            })
            continue

        selected.append(cluster)
        if len(selected) >= top_n:
            break

    return selected, skipped


def update_sent_digest_topics(
    sent_topics: List[Dict[str, Any]],
    selected_clusters: List[DigestTopicCluster],
    run_date: str,
    max_history_days: int = 30,
) -> List[Dict[str, Any]]:
    index = {
        str(item.get("topic_key", "")): dict(item)
        for item in sent_topics
        if item.get("topic_key")
    }

    for cluster in selected_clusters:
        topic_key = digest_cluster_topic_key(cluster)
        if not topic_key:
            continue

        item = cluster.representative
        existing = index.get(topic_key, {})
        index[topic_key] = {
            "topic_key": topic_key,
            "topic_label": getattr(item, "topic_label", "") or existing.get("topic_label", ""),
            "first_sent_date": existing.get("first_sent_date") or run_date,
            "last_sent_date": run_date,
            "representative_url": item.url,
            "representative_title": item.title,
            "representative_source": item.source,
            "representative_score": item.importance_score,
            "representative_authority": digest_source_authority_score(item),
            "sent_count": int(existing.get("sent_count") or 0) + 1,
        }

    cutoff = parse_iso_date(run_date) - timedelta(days=max_history_days)
    out = []
    for item in index.values():
        last_sent = parse_iso_date(str(item.get("last_sent_date", "")))
        if not last_sent or last_sent >= cutoff:
            out.append(item)

    return sorted(out, key=lambda x: str(x.get("last_sent_date", "")), reverse=True)


def render_telegram_digest(
    selected_clusters: List[DigestTopicCluster],
    run_date: Optional[str] = None,
) -> str:
    lines = []
    date_part = f"{run_date} " if run_date else ""
    lines.append(f"IP 동향 Digest - {date_part}상위 {len(selected_clusters)}건")
    lines.append("")

    if not selected_clusters:
        lines.append("오늘은 기준 점수 이상 신규 동향이 없습니다.")
        return "\n".join(lines)

    for i, cluster in enumerate(selected_clusters, start=1):
        item = cluster.representative
        title = compact_digest_text(item.title, max_chars=120)
        topic_label = getattr(item, "topic_label", "") or ""
        lines.append(f"{i}. {title}")
        lines.append(f"점수: {item.importance_score} | {item.category} | {item.source}")
        lines.append(f"지역: {digest_region_label(item)}")
        if topic_label:
            lines.append(f"이슈: {compact_digest_text(topic_label, max_chars=90)}")
        if len(cluster.items) > 1:
            related = [
                f"{x.source}({x.importance_score}점)"
                for x in cluster.items[1:4]
            ]
            suffix = f": {', '.join(related)}" if related else ""
            lines.append(f"관련: {len(cluster.items) - 1}건{suffix}")
        if item.summary_ko:
            lines.append(f"핵심: {compact_digest_text(item.summary_ko, max_chars=220)}")
        if item.key_points:
            key_point = compact_digest_key_point(item.key_points, max_chars=180)
            if key_point:
                lines.append(f"시사점: {key_point}")
        lines.append(f"링크: {item.url}")
        lines.append("")

    return "\n".join(lines)


def build_telegram_digest(
    analyzed: List[AnalyzedArticle],
    top_n: int = 5,
    min_importance: int = 0,
    run_date: Optional[str] = None,
) -> str:
    selected_clusters, _ = select_digest_clusters(
        analyzed,
        top_n=top_n,
        min_importance=min_importance,
    )
    return render_telegram_digest(selected_clusters, run_date=run_date)


def save_digest(text: str):
    os.makedirs('data', exist_ok=True)
    with open(DIGEST_PATH, 'w', encoding='utf-8') as f:
        f.write(text)


def split_telegram_messages(lines: List[str], max_chars: int = 3500) -> List[str]:
    messages = []
    current = []
    current_len = 0

    for line in lines:
        if len(line) > max_chars:
            if current:
                messages.append("\n".join(current).rstrip())
                current = []
                current_len = 0
            for start in range(0, len(line), max_chars):
                messages.append(line[start:start + max_chars].rstrip())
            continue

        add_len = len(line) + 1
        if current and current_len + add_len > max_chars:
            messages.append("\n".join(current).rstrip())
            current = []
            current_len = 0

        current.append(line)
        current_len += add_len

    if current:
        messages.append("\n".join(current).rstrip())

    return messages


REVIEW_REGION_ORDER = ["미국", "일본", "중국", "유럽", "국제기구", "기타"]


def normalize_review_title(title: Any) -> str:
    text = html.unescape(str(title or ""))
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def review_title_needs_attention(title: Optional[str]) -> bool:
    text = normalize_review_title(title)
    if not text:
        return True
    if "\ufffd" in text:
        return True
    if text.endswith(("...", "…")):
        return True
    return False


def normalize_review_region(region: Optional[str]) -> str:
    text = (region or "").strip()
    if "미국" in text:
        return "미국"
    if "일본" in text:
        return "일본"
    if "중국" in text:
        return "중국"
    if "유럽" in text or "영국" in text:
        return "유럽"
    if "국제기구" in text or "국제" in text:
        return "국제기구"
    return "기타"


def extract_time_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    normalized_text = text.translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))
    match = re.search(r'(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?(?!\d)', normalized_text)
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def parse_generated_datetime(generated_at: Optional[str]) -> datetime:
    if not generated_at:
        return datetime.now()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(generated_at, fmt)
        except ValueError:
            continue
    return datetime.now()


def format_review_date(
    published: Any,
    url: str = "",
    generated_at: Optional[str] = None,
) -> str:
    text = re.sub(r"\s+", " ", str(published or "")).strip()
    url_date = extract_date_from_url(url)
    date = extract_date_from_text(text)
    time_part = extract_time_from_text(text)

    if date:
        return f"{date} {time_part}" if time_part else date

    if time_part and url_date:
        return f"{url_date} {time_part}"

    relative_match = re.search(
        r'(\d+)\s*(min|mins|minute|minutes|hr|hrs|hour|hours|day|days)\s+ago',
        text.lower(),
    )
    if relative_match:
        amount = int(relative_match.group(1))
        unit = relative_match.group(2)
        base = parse_generated_datetime(generated_at)
        if unit.startswith("min"):
            dt = base - timedelta(minutes=amount)
        elif unit.startswith(("hr", "hour")):
            dt = base - timedelta(hours=amount)
        else:
            dt = base - timedelta(days=amount)
        return dt.strftime("%Y-%m-%d")

    if url_date:
        return url_date

    return text or "-"


def build_raw_review_summary_lines(
    articles: List[Article],
    source_check_records: Optional[List[Dict[str, Any]]] = None,
    generated_at: Optional[str] = None,
) -> List[str]:
    source_check_records = source_check_records or []
    total_sources = len(source_check_records)
    ok_sources = sum(1 for r in source_check_records if r.get("status") == "ok")
    empty_sources = sum(1 for r in source_check_records if r.get("status") == "empty")
    failed_sources = sum(1 for r in source_check_records if r.get("status") == "fail")
    fetch_candidate_count = sum(int(r.get("count") or 0) for r in source_check_records)
    seen_skipped_count = sum(int(r.get("seen_skipped_count") or 0) for r in source_check_records)
    non_article_skipped_count = sum(int(r.get("non_article_skipped_count") or 0) for r in source_check_records)
    problem_sources = [
        r for r in source_check_records
        if r.get("status") in ("fail", "empty")
    ]
    high_count_sources = [
        r for r in source_check_records
        if int(r.get("count") or 0) >= 50
    ]
    missing_date_articles = [
        a for a in articles
        if format_review_date(a.published, a.url, generated_at=generated_at) == "-"
    ]
    title_attention_articles = [a for a in articles if review_title_needs_attention(a.title)]

    lines = [
        "요약 리포트",
        "",
        f"- 신규 기사: {len(articles)}개",
        f"- 전체 fetch 후보: {fetch_candidate_count}개 / seen 제외 {seen_skipped_count}개 / 비기사 제외 {non_article_skipped_count}개",
        f"- 소스 상태: 성공 {ok_sources}개 / 빈값 {empty_sources}개 / 실패 {failed_sources}개 / 전체 {total_sources}개",
        f"- 날짜 누락 기사: {len(missing_date_articles)}개",
        f"- 제목 잘림/깨짐 의심: {len(title_attention_articles)}개",
    ]

    attention_lines = []
    if problem_sources:
        attention_lines.append(f"- 수집 실패/빈값 소스: {len(problem_sources)}개")
    if missing_date_articles:
        attention_lines.append(f"- 날짜 누락 기사: {len(missing_date_articles)}개")
    if title_attention_articles:
        attention_lines.append(f"- 제목 잘림/깨짐 의심: {len(title_attention_articles)}개")
    if high_count_sources:
        attention_lines.append(f"- 후보 과다 소스: {len(high_count_sources)}개")

    lines.extend(["", "확인 필요"])
    if attention_lines:
        lines.extend(attention_lines)
    else:
        lines.append("- 특이사항 없음")

    if problem_sources:
        lines.extend(["", "수집 실패/빈값 소스"])
        for record in problem_sources[:10]:
            status = record.get("status", "-")
            name = record.get("name", "-")
            error = re.sub(r"\s+", " ", record.get("error") or "").strip()
            suffix = f" - {error[:90]}" if error else ""
            lines.append(f"- [{status}] {name}{suffix}")
        if len(problem_sources) > 10:
            lines.append(f"- 외 {len(problem_sources) - 10}개")

    if high_count_sources:
        lines.extend(["", "후보 과다 소스"])
        for record in high_count_sources[:10]:
            name = record.get("name", "-")
            count = record.get("count", 0)
            lines.append(f"- {name}: {count}개")
        if len(high_count_sources) > 10:
            lines.append(f"- 외 {len(high_count_sources) - 10}개")

    if missing_date_articles:
        lines.extend(["", "날짜 누락 샘플"])
        for art in missing_date_articles[:5]:
            title = normalize_review_title(art.title)
            if len(title) > 90:
                title = title[:87].rstrip() + "..."
            lines.append(f"- {art.source} | {title}")
        if len(missing_date_articles) > 5:
            lines.append(f"- 외 {len(missing_date_articles) - 5}개")

    if title_attention_articles:
        lines.extend(["", "제목 확인 필요 샘플"])
        for art in title_attention_articles[:5]:
            title = normalize_review_title(art.title)
            if len(title) > 90:
                title = title[:87].rstrip() + "..."
            lines.append(f"- {art.source} | {title or '(빈 제목)'}")
        if len(title_attention_articles) > 5:
            lines.append(f"- 외 {len(title_attention_articles) - 5}개")

    return lines


def lines_text_length(lines: List[str]) -> int:
    return len("\n".join(lines).rstrip())


def build_source_review_chunks(
    overview_lines: List[str],
    source_blocks: List[tuple],
    max_chars: int,
) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []

    def flush_current():
        nonlocal current
        if current:
            chunks.append("\n".join(current).rstrip())
            current = []

    def can_add(block: List[str]) -> bool:
        if not current:
            return lines_text_length(block) <= max_chars
        return lines_text_length(current + block) <= max_chars

    def add_small_block(block: List[str]):
        nonlocal current
        if can_add(block):
            current.extend(block)
            return
        flush_current()
        if lines_text_length(block) <= max_chars:
            current.extend(block)
        else:
            chunks.extend(split_telegram_messages(block, max_chars=max_chars))

    add_small_block(overview_lines)

    for source_header, article_blocks in source_blocks:
        source_lines: List[str] = [source_header]
        for article_block in article_blocks:
            source_lines.extend(article_block)

        if can_add(source_lines):
            current.extend(source_lines)
            continue

        flush_current()
        if lines_text_length(source_lines) <= max_chars:
            current.extend(source_lines)
            continue

        part: List[str] = [source_header]
        for article_block in article_blocks:
            candidate = part + article_block
            if lines_text_length(candidate) <= max_chars:
                part = candidate
                continue

            if len(part) > 1:
                chunks.append("\n".join(part).rstrip())
                part = [source_header] + article_block
                if lines_text_length(part) <= max_chars:
                    continue

            chunks.extend(split_telegram_messages([source_header] + article_block, max_chars=max_chars))
            part = [source_header]

        if len(part) > 1:
            chunks.append("\n".join(part).rstrip())

    flush_current()
    return chunks


def build_raw_review_messages(
    articles: List[Article],
    generated_at: Optional[str] = None,
    max_chars: int = 3500,
    source_check_records: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    generated_at = generated_at or time.strftime("%Y-%m-%d %H:%M")
    generated_date = generated_at.split()[0]

    region_counts: Dict[str, int] = {}
    source_groups: Dict[tuple, List[Article]] = {}

    for art in articles:
        region = normalize_review_region(art.region)
        region_counts[region] = region_counts.get(region, 0) + 1
        key = (region, art.source)
        source_groups.setdefault(key, []).append(art)

    overview_lines = [
        f"IP Monitor 수집 검증 목록 - {generated_date}",
        "",
        f"신규 기사: {len(articles)}개",
        f"수집 소스: {len(source_groups)}개",
        "",
        "국가/지역별 수집 개수",
    ]

    for region in REVIEW_REGION_ORDER:
        overview_lines.append(f"- {region}: {region_counts.get(region, 0)}개")

    overview_lines.append("")
    source_blocks = []
    for (region, source), items in sorted(
        source_groups.items(),
        key=lambda x: (REVIEW_REGION_ORDER.index(x[0][0]), x[0][1])
    ):
        article_blocks = []
        for idx, art in enumerate(items, start=1):
            title = normalize_review_title(art.title)
            if len(title) > 160:
                title = title[:157].rstrip() + "..."
            article_blocks.append([
                f"{idx}. {title}",
                f"   날짜: {format_review_date(art.published, art.url, generated_at=generated_at)}",
                f"   링크: {art.url}",
                "",
            ])
        source_blocks.append((f"[{region}] {source} - {len(items)}개", article_blocks))

    # Reserve room for the per-message review header so every Telegram chunk
    # starts with the same title instead of whichever article line was split first.
    body_max_chars = max(1000, max_chars - 120)
    summary_lines = build_raw_review_summary_lines(
        articles,
        source_check_records=source_check_records,
        generated_at=generated_at,
    )
    chunks = split_telegram_messages(summary_lines, max_chars=body_max_chars)
    chunks.extend(build_source_review_chunks(overview_lines, source_blocks, max_chars=body_max_chars))
    total = len(chunks)
    titled = []
    for idx, chunk in enumerate(chunks, start=1):
        header = f"IP Monitor 수집 검증 목록 - {generated_date} ({idx}/{total})"
        chunk_lines = chunk.splitlines()
        if chunk_lines and chunk_lines[0].startswith("IP Monitor 수집 검증 목록 - "):
            chunk_lines = chunk_lines[1:]
            if chunk_lines and not chunk_lines[0].strip():
                chunk_lines = chunk_lines[1:]
        body = "\n".join(chunk_lines).strip()
        titled.append(f"{header}\n\n{body}" if body else header)
    return titled


def save_raw_review_messages(messages: List[str], path: str = RAW_REVIEW_DIGEST_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n--- MESSAGE BREAK ---\n\n".join(messages))


def telegram_send_enabled(cfg: Dict[str, Any], key: str) -> bool:
    if '--no-telegram' in sys.argv:
        return False
    tg_cfg = cfg.get('telegram', {})
    return tg_cfg.get(key, tg_cfg.get('send_enabled', False))


def send_telegram_message_chunks(
    messages: List[str],
    cfg: Dict[str, Any],
    chat_id_env: str = 'TELEGRAM_CHAT_ID',
    enabled_key: str = 'send_enabled',
):
    if not telegram_send_enabled(cfg, enabled_key):
        return

    token = os.getenv('TELEGRAM_BOT_TOKEN')
    chat_id = os.getenv(chat_id_env)
    if not token or not chat_id:
        print(f'텔레그램 토큰 또는 {chat_id_env}가 없어 실제 발송은 건너뜁니다.')
        return

    tg_cfg = cfg.get('telegram', {})
    max_messages = int(tg_cfg.get('max_messages', len(messages)) or len(messages))
    timeout_seconds = int(tg_cfg.get('timeout_seconds', 10) or 10)
    messages_to_send = messages[:max_messages]
    skipped_count = max(0, len(messages) - len(messages_to_send))
    if skipped_count:
        print(f'텔레그램 메시지 {skipped_count}개는 max_messages 제한으로 전송하지 않습니다.')

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    for idx, message in enumerate(messages_to_send, start=1):
        payload = {
            'chat_id': chat_id,
            'text': message,
            'disable_web_page_preview': True,
        }
        try:
            resp = requests.post(url, json=payload, timeout=timeout_seconds)
            if not resp.ok:
                print(f'텔레그램 전송 실패({idx}/{len(messages_to_send)}):', resp.text)
                break
        except requests.RequestException as e:
            print(f'텔레그램 전송 실패({idx}/{len(messages_to_send)}): {e.__class__.__name__}')
            break
        time.sleep(0.3)


def send_telegram_messages(
    text: str,
    cfg: Dict[str, Any],
    chat_id_env: str = 'TELEGRAM_CHAT_ID',
    enabled_key: str = 'send_enabled',
) -> int:
    messages = split_telegram_messages(text.splitlines(), max_chars=3500)
    send_telegram_message_chunks(
        messages,
        cfg,
        chat_id_env=chat_id_env,
        enabled_key=enabled_key,
    )
    return len(messages)


def main():
    run_started = time.time()
    run_id = time.strftime("%Y%m%d_%H%M%S", time.localtime(run_started))
    config_path = 'data/failed_sources.yaml' if '--failed-only' in sys.argv else 'config.yaml'
    failed_only = '--failed-only' in sys.argv
    skip_analysis = '--skip-analysis' in sys.argv
    cfg = load_config(config_path)
    sources = load_sources(cfg)

    seen = load_seen()
    existing_results = load_results()
    existing_results_index = load_results_index()
    already_analyzed = set(existing_results_index.keys())

    fetch_timeout = cfg.get('fetch', {}).get('timeout_seconds', 20)
    top_n = cfg.get('analysis', {}).get('top_n_for_digest', 5)
    min_importance = cfg.get('analysis', {}).get('min_importance_for_digest', 0)
    recent_topic_days = int(cfg.get('analysis', {}).get('recent_topic_days', 3) or 3)

    print(f'활성화된 소스 수: {len(sources)}')
    print(f'기존 분석 완료 URL 수: {len(already_analyzed)}')

    # =========================================================
    # [추가 1] 테스트용 스위치 변수 설정
    SKIP_ANALYSIS = skip_analysis  # True로 설정하면 Claude 분석 및 digest 텔레그램 발송을 건너뜁니다.
    RAW_RESULTS_PATH = 'data/raw_articles.json'  # 수집 원본만 저장할 파일 경로
    # =========================================================

    run_log: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(run_started)),
        "finished_at": None,
        "duration_seconds": None,
        "config_path": config_path,
        "failed_only": failed_only,
        "skip_analysis": SKIP_ANALYSIS,
        "no_telegram": '--no-telegram' in sys.argv,
        "github": {
            "event_name": os.getenv("GITHUB_EVENT_NAME", ""),
            "run_id": os.getenv("GITHUB_RUN_ID", ""),
            "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", ""),
            "workflow": os.getenv("GITHUB_WORKFLOW", ""),
            "repository": os.getenv("GITHUB_REPOSITORY", ""),
            "server_url": os.getenv("GITHUB_SERVER_URL", ""),
            "run_url": (
                f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/"
                f"{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/{os.getenv('GITHUB_RUN_ID', '')}"
                if os.getenv("GITHUB_REPOSITORY") and os.getenv("GITHUB_RUN_ID")
                else ""
            ),
        },
        "paths": {
            "raw_articles": RAW_RESULTS_PATH,
            "raw_review_digest": RAW_REVIEW_DIGEST_PATH,
            "source_check_report": SOURCE_CHECK_REPORT_PATH,
            "failed_sources": FAILED_SOURCES_PATH,
            "seen_urls": SEEN_PATH,
            "results": RESULTS_PATH,
            "daily_results_dir": DAILY_RESULTS_DIR,
            "sent_digest_topics": SENT_DIGEST_TOPICS_PATH,
            "telegram_digest": DIGEST_PATH,
        },
        "summary": {
            "total_sources": len(sources),
            "total_fetched_articles": 0,
            "total_new_articles": 0,
            "fetch_duration_seconds": None,
            "raw_review_telegram_duration_seconds": None,
            "analysis_duration_seconds": None,
            "digest_telegram_duration_seconds": None,
            "digest_recent_topic_days": recent_topic_days,
            "digest_recent_topic_skipped_count": 0,
            "seen_skipped_count": 0,
            "already_analyzed_skipped_count": 0,
            "non_article_skipped_count": 0,
            "ok_sources": 0,
            "empty_sources": 0,
            "failed_sources": 0,
            "analysis_attempted_count": 0,
            "analysis_success_count": 0,
            "analysis_failed_count": 0,
            "analysis_skipped_existing_count": 0,
            "analysis_prefilter_skipped_count": 0,
            "seen_saved": False,
            "raw_articles_saved": False,
            "raw_review_digest_saved": False,
            "raw_review_telegram_messages": 0,
            "source_check_report_saved": False,
            "failed_sources_yaml_saved": False,
            "results_saved": False,
            "daily_results_saved": False,
            "daily_results_path": None,
            "digest_saved": False,
            "digest_telegram_messages": 0,
            "telegram_send_enabled": cfg.get("telegram", {}).get("send_enabled", False),
            "telegram_review_send_enabled": cfg.get("telegram", {}).get("review_send_enabled", False),
            "telegram_digest_send_enabled": cfg.get("telegram", {}).get("digest_send_enabled", False),
        },
        "sources": [],
        "analysis_errors": [],
        "analysis_prefilter_skips": [],
        "digest_recent_topic_skips": [],
    }

    new_articles: List[Article] = []
    source_check_records: List[Dict[str, Any]] = []

    fetch_started = time.time()
    total_sources = len(sources)
    for source_idx, src in enumerate(sources, start=1):
        progress_label = f'[{source_idx}/{total_sources}]'
        print(f'{progress_label} [{src.name}] 수집 시작 ({src.mode})')
        source_started = time.time()
        source_log = {
            "name": src.name,
            "region": src.region,
            "mode": src.mode,
            "monitor_url": src.monitor_url,
            "status": None,
            "fetched_count": 0,
            "new_count": 0,
            "seen_skipped_count": 0,
            "already_analyzed_skipped_count": 0,
            "non_article_skipped_count": 0,
            "error": "",
            "elapsed_seconds": None,
            "sample_articles": [],
        }
        try:
            arts = fetch_articles_for_source(src, timeout=fetch_timeout)
            source_check_records.append(
                build_source_check_record(src, arts)
            )
            source_log["fetched_count"] = len(arts)
            source_log["status"] = "ok" if arts else "empty"
            source_log["sample_articles"] = [
                {"title": a.title, "url": a.url, "published": a.published}
                for a in arts[:3]
            ]
        except Exception as e:
            print(f'  - 수집 실패: {e}')
            source_check_records.append(
                build_source_check_record(src, [], str(e))
            )
            source_log["status"] = "fail"
            source_log["error"] = str(e)
            source_log["elapsed_seconds"] = round(time.time() - source_started, 3)
            run_log["sources"].append(source_log)
            continue

        fresh = []
        for a in arts:
            if looks_like_non_article(a.title, a.url):
                source_log["non_article_skipped_count"] += 1
                continue

            if a.url in seen:
                source_log["seen_skipped_count"] += 1
                continue

            if a.url in already_analyzed:
                print(f'  - 이미 분석된 URL 재수집 제외: {a.url}')
                source_log["already_analyzed_skipped_count"] += 1
                seen.add(a.url)
                continue

            fresh.append(a)
            seen.add(a.url)

        print(f'{progress_label} 신규 기사 후보: {len(fresh)}개')
        source_log["new_count"] = len(fresh)
        if source_check_records:
            source_check_records[-1].update({
                "fetched_count": source_log["fetched_count"],
                "new_count": source_log["new_count"],
                "seen_skipped_count": source_log["seen_skipped_count"],
                "already_analyzed_skipped_count": source_log["already_analyzed_skipped_count"],
                "non_article_skipped_count": source_log["non_article_skipped_count"],
            })
        source_log["elapsed_seconds"] = round(time.time() - source_started, 3)
        run_log["sources"].append(source_log)
        new_articles.extend(fresh)
        time.sleep(1)

    run_log["summary"]["fetch_duration_seconds"] = round(time.time() - fetch_started, 3)

    save_seen(seen)
    save_source_check_report(source_check_records, SOURCE_CHECK_REPORT_PATH)
    save_failed_sources_yaml(cfg, source_check_records, FAILED_SOURCES_PATH)
    run_log["summary"]["seen_saved"] = True
    run_log["summary"]["source_check_report_saved"] = True
    run_log["summary"]["failed_sources_yaml_saved"] = True

    ok_count = sum(1 for r in source_check_records if r["status"] == "ok")
    empty_count = sum(1 for r in source_check_records if r["status"] == "empty")
    fail_count = sum(1 for r in source_check_records if r["status"] == "fail")
    run_log["summary"]["total_fetched_articles"] = sum(int(s["fetched_count"]) for s in run_log["sources"])
    run_log["summary"]["total_new_articles"] = len(new_articles)
    run_log["summary"]["seen_skipped_count"] = sum(int(s["seen_skipped_count"]) for s in run_log["sources"])
    run_log["summary"]["already_analyzed_skipped_count"] = sum(int(s["already_analyzed_skipped_count"]) for s in run_log["sources"])
    run_log["summary"]["non_article_skipped_count"] = sum(int(s["non_article_skipped_count"]) for s in run_log["sources"])
    run_log["summary"]["ok_sources"] = ok_count
    run_log["summary"]["empty_sources"] = empty_count
    run_log["summary"]["failed_sources"] = fail_count

    print(f'[DEBUG] 소스 점검 결과 저장: {SOURCE_CHECK_REPORT_PATH}')
    print(f'[DEBUG] 실패/빈 소스 YAML 저장: {FAILED_SOURCES_PATH}')
    print(f'[DEBUG] 소스 점검 요약: ok={ok_count}, empty={empty_count}, fail={fail_count}')
    if not new_articles:
        print('신규 기사가 없습니다.')
        run_log["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        run_log["duration_seconds"] = round(time.time() - run_started, 3)
        log_path = save_run_log(run_log)
        print(f'[DEBUG] 실행 로그 저장: {log_path}')
        return

    print(f'총 신규 기사 수: {len(new_articles)}')
    # =========================================================
    # [추가 2] 수집된 원본 데이터만 따로 JSON으로 저장하고 분석 스킵하기
    os.makedirs('data', exist_ok=True)
    with open(RAW_RESULTS_PATH, 'w', encoding='utf-8') as f:
        # new_articles 리스트 안의 Article 객체들을 딕셔너리로 변환하여 저장
        json.dump([asdict(a) for a in new_articles], f, ensure_ascii=False, indent=2)
    print(f'[DEBUG] 수집 원본 데이터 {len(new_articles)}개가 {RAW_RESULTS_PATH}에 저장되었습니다.')
    run_log["summary"]["raw_articles_saved"] = True

    raw_review_messages = build_raw_review_messages(
        new_articles,
        source_check_records=source_check_records,
    )
    save_raw_review_messages(raw_review_messages, RAW_REVIEW_DIGEST_PATH)
    raw_review_telegram_started = time.time()
    send_telegram_message_chunks(
        raw_review_messages,
        cfg,
        chat_id_env='TELEGRAM_REVIEW_CHAT_ID',
        enabled_key='review_send_enabled',
    )
    run_log["summary"]["raw_review_telegram_duration_seconds"] = round(
        time.time() - raw_review_telegram_started,
        3
    )
    print(f'[DEBUG] 수집 검증용 텔레그램 메시지 {len(raw_review_messages)}개가 {RAW_REVIEW_DIGEST_PATH}에 저장되었습니다.')
    run_log["summary"]["raw_review_digest_saved"] = True
    run_log["summary"]["raw_review_telegram_messages"] = len(raw_review_messages)

    if SKIP_ANALYSIS:
        print('[DEBUG] SKIP_ANALYSIS가 True이므로 Claude 분석을 건너뛰고 스크립트를 종료합니다.')
        run_log["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        run_log["duration_seconds"] = round(time.time() - run_started, 3)
        log_path = save_run_log(run_log)
        print(f'[DEBUG] 실행 로그 저장: {log_path}')
        return
    # =========================================================

    claude = ClaudeClient()
    analyzed_items: List[AnalyzedArticle] = []
    existing_urls = {item.get('url') for item in existing_results if item.get('url')}

    analysis_started = time.time()
    for art in new_articles:
        already_exists = art.url in existing_urls

        if already_exists:
            print(f'  - 분석 스킵(이미 results.json 존재): {art.url}')
            run_log["summary"]["analysis_skipped_existing_count"] += 1
            continue

        prefilter_reason = should_skip_claude_analysis(art)
        if prefilter_reason:
            print(f'  - 분석 사전 제외({prefilter_reason}): {art.source} | {art.title[:60]}')
            run_log["summary"]["analysis_prefilter_skipped_count"] += 1
            if len(run_log["analysis_prefilter_skips"]) < 100:
                run_log["analysis_prefilter_skips"].append({
                    "source": art.source,
                    "title": art.title,
                    "url": art.url,
                    "reason": prefilter_reason,
                })
            continue

        try:
            print(f'- 분석 중: {art.source} | {art.title[:40]}...')
            run_log["summary"]["analysis_attempted_count"] += 1
            analyzed = claude.analyze_article(art)
            analyzed_items.append(analyzed)
            run_log["summary"]["analysis_success_count"] += 1

            if not already_exists:
                existing_urls.add(art.url)
        except Exception as e:
            print('  분석 실패:', e)
            run_log["summary"]["analysis_failed_count"] += 1
            run_log["analysis_errors"].append({
                "source": art.source,
                "title": art.title,
                "url": art.url,
                "error": str(e),
            })
            continue

        time.sleep(0.5)

    run_log["summary"]["analysis_duration_seconds"] = round(time.time() - analysis_started, 3)

    if not analyzed_items:
        print('새로 분석된 결과가 없습니다.')
        run_log["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        run_log["duration_seconds"] = round(time.time() - run_started, 3)
        log_path = save_run_log(run_log)
        print(f'[DEBUG] 실행 로그 저장: {log_path}')
        return

    merged = {item['url']: item for item in existing_results if item.get('url')}
    for item in analyzed_items:
        merged[item.url] = asdict(item)
    save_results(list(merged.values()))
    run_log["summary"]["results_saved"] = True
    daily_path = save_daily_results([asdict(item) for item in analyzed_items], run_id)
    if daily_path:
        run_log["summary"]["daily_results_saved"] = True
        run_log["summary"]["daily_results_path"] = daily_path

    sent_digest_topics = load_sent_digest_topics()
    digest_run_date = run_date_from_run_id(run_id)
    selected_digest_clusters, recent_topic_skips = select_digest_clusters(
        analyzed_items,
        top_n=top_n,
        min_importance=min_importance,
        sent_topics=sent_digest_topics,
        recent_topic_days=recent_topic_days,
        run_date=digest_run_date,
    )
    run_log["summary"]["digest_recent_topic_skipped_count"] = len(recent_topic_skips)
    run_log["digest_recent_topic_skips"] = recent_topic_skips[:100]

    digest_text = render_telegram_digest(selected_digest_clusters, run_date=digest_run_date)
    save_digest(digest_text)
    run_log["summary"]["digest_saved"] = True
    digest_telegram_started = time.time()
    digest_telegram_messages = send_telegram_messages(
        digest_text,
        cfg,
        chat_id_env='TELEGRAM_DIGEST_CHAT_ID',
        enabled_key='digest_send_enabled',
    )
    run_log["summary"]["digest_telegram_messages"] = digest_telegram_messages
    run_log["summary"]["digest_telegram_duration_seconds"] = round(
        time.time() - digest_telegram_started,
        3
    )
    updated_sent_topics = update_sent_digest_topics(
        sent_digest_topics,
        selected_digest_clusters,
        digest_run_date,
    )
    save_sent_digest_topics(updated_sent_topics)

    print('완료. results.json / telegram_digest.txt 생성(또는 갱신).')
    run_log["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    run_log["duration_seconds"] = round(time.time() - run_started, 3)
    log_path = save_run_log(run_log)
    print(f'[DEBUG] 실행 로그 저장: {log_path}')


if __name__ == '__main__':
    main()
