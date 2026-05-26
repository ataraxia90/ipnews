import sys
import os
import json
import time
import re
import html
from xml.etree import ElementTree
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

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

LOCAL_TZ = ZoneInfo("Asia/Seoul")

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
    digest_bullets: Optional[List[str]] = None
    ip_directness: int = 0
    policy_materiality: int = 0
    source_authority: int = 0
    korea_relevance: int = 0
    timeliness: int = 0
    score_reason: str = ""
    claude_model: str = ""
    claude_input_tokens: int = 0
    claude_output_tokens: int = 0
    claude_cache_creation_input_tokens: int = 0
    claude_cache_read_input_tokens: int = 0
    claude_estimated_cost_usd: float = 0.0


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
NOTION_PAGES_PATH = 'data/notion_pages.json'
NOTION_VERSION = '2025-09-03'

# Standard Claude Sonnet API pricing, USD per million tokens.
# The current prompt does not use prompt caching, but cache fields are recorded
# if Anthropic returns them.
CLAUDE_SONNET_INPUT_USD_PER_MTOK = 3.0
CLAUDE_SONNET_OUTPUT_USD_PER_MTOK = 15.0
CLAUDE_SONNET_CACHE_CREATE_USD_PER_MTOK = 3.75
CLAUDE_SONNET_CACHE_READ_USD_PER_MTOK = 0.30
FAILED_SOURCES_PATH = 'data/failed_sources.yaml'
RUN_LOG_DIR = 'data/run_logs'
KOREA_HOLIDAY_CACHE_DIR = 'data/holiday_cache'
KOREA_HOLIDAY_API_URL = 'http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo'
KOREA_HOLIDAY_API_URLS = [
    'http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo',
    'http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getHoliDeInfo',
    'https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo',
    'https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getHoliDeInfo',
]
SUPABASE_STATE_TABLE = 'monitor_state'
SUPABASE_SEEN_KEY = 'seen_urls'
SUPABASE_RESULTS_KEY = 'analysis_results'
SUPABASE_SENT_DIGEST_TOPICS_KEY = 'sent_digest_topics'
SUPABASE_NOTION_PAGES_KEY = 'notion_pages'
SUPABASE_RAW_REVIEW_MESSAGES_KEY = 'telegram_raw_review'
SUPABASE_DIGEST_MESSAGE_KEY = 'telegram_digest'
SUPABASE_RUN_LOG_PREFIX = 'run_log'
SUPABASE_LATEST_RUN_LOG_KEY = 'run_log_latest'

# max_items 기반 수집 제한은 seen 적용 전 후보 수를 자르는 방식이라 운영상
# 의미가 약해졌다. 설정에서는 제거하고 제한 helper도 no-op으로 둔다.


def local_datetime(ts: Optional[float] = None) -> datetime:
    if ts is None:
        return datetime.now(LOCAL_TZ)
    return datetime.fromtimestamp(ts, LOCAL_TZ)


def local_timestamp(ts: Optional[float] = None) -> str:
    return local_datetime(ts).strftime("%Y-%m-%d %H:%M:%S")


def local_run_id(ts: Optional[float] = None) -> str:
    return local_datetime(ts).strftime("%Y%m%d_%H%M%S")


def local_run_date(ts: Optional[float] = None) -> str:
    return local_datetime(ts).strftime("%Y-%m-%d")


def truthy_config_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return default


def korean_holiday_cache_path(year: int, cache_dir: str = KOREA_HOLIDAY_CACHE_DIR) -> str:
    return os.path.join(cache_dir, f"korean_holidays_{int(year):04d}.json")


def parse_korean_holiday_payload(payload: Any) -> Dict[str, str]:
    holidays: Dict[str, str] = {}
    body = (((payload or {}).get("response") or {}).get("body") or {}) if isinstance(payload, dict) else {}
    items = (body.get("items") or {}).get("item") if isinstance(body, dict) else None
    if not items:
        return holidays
    if isinstance(items, dict):
        items = [items]

    for item in items:
        if not isinstance(item, dict):
            continue
        locdate = str(item.get("locdate") or "").strip()
        if not re.fullmatch(r"\d{8}", locdate):
            continue
        is_holiday = str(item.get("isHoliday") or "Y").strip().upper()
        if is_holiday and is_holiday != "Y":
            continue
        date = f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:8]}"
        holidays[date] = str(item.get("dateName") or item.get("dateKind") or "공휴일").strip()
    return holidays


def parse_korean_holiday_xml(text: str) -> Dict[str, str]:
    holidays: Dict[str, str] = {}
    try:
        root = ElementTree.fromstring(text or "")
    except ElementTree.ParseError:
        return holidays

    for item in root.findall(".//item"):
        locdate = (item.findtext("locdate") or "").strip()
        if not re.fullmatch(r"\d{8}", locdate):
            continue
        is_holiday = (item.findtext("isHoliday") or "Y").strip().upper()
        if is_holiday and is_holiday != "Y":
            continue
        date = f"{locdate[:4]}-{locdate[4:6]}-{locdate[6:8]}"
        holidays[date] = (item.findtext("dateName") or item.findtext("dateKind") or "공휴일").strip()
    return holidays


def korean_holiday_api_error_message(text: str) -> str:
    try:
        payload = json.loads(text or "")
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        header = ((payload.get("response") or {}).get("header") or {})
        code = str(header.get("resultCode") or "").strip()
        message = str(header.get("resultMsg") or "").strip()
        if code and code != "00":
            return f"{code} {message}".strip()

    try:
        root = ElementTree.fromstring(text or "")
    except ElementTree.ParseError:
        return ""
    code = (root.findtext(".//resultCode") or "").strip()
    message = (root.findtext(".//resultMsg") or "").strip()
    if code and code != "00":
        return f"{code} {message}".strip()
    return ""


def read_korean_holiday_cache(year: int, cache_dir: str = KOREA_HOLIDAY_CACHE_DIR) -> Dict[str, str]:
    path = korean_holiday_cache_path(year, cache_dir=cache_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    holidays = data.get("holidays") if isinstance(data, dict) else data
    if not isinstance(holidays, dict):
        return {}
    return {str(k): str(v) for k, v in holidays.items() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(k))}


def write_korean_holiday_cache(
    year: int,
    holidays: Dict[str, str],
    cache_dir: str = KOREA_HOLIDAY_CACHE_DIR,
) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    path = korean_holiday_cache_path(year, cache_dir=cache_dir)
    payload = {
        "year": int(year),
        "updated_at": local_timestamp(),
        "source": "data.go.kr SpcdeInfoService/getRestDeInfo",
        "holidays": dict(sorted((holidays or {}).items())),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def fetch_korean_public_holidays_from_api(
    year: int,
    service_key: Optional[str] = None,
    timeout: int = 15,
) -> Dict[str, str]:
    service_key = (service_key or os.getenv("KOREA_HOLIDAY_API_KEY") or "").strip()
    if not service_key:
        raise RuntimeError("KOREA_HOLIDAY_API_KEY is not set")

    errors = []
    for api_url in KOREA_HOLIDAY_API_URLS:
        holidays: Dict[str, str] = {}
        for month in range(1, 13):
            base_params = {
                "solYear": f"{int(year):04d}",
                "solMonth": f"{month:02d}",
                "numOfRows": "20",
                "_type": "json",
            }
            request_variants = [
                (api_url, {"ServiceKey": service_key, **base_params}),
                (api_url, {"serviceKey": service_key, **base_params}),
                (f"{api_url}?ServiceKey={service_key}&{urlencode(base_params)}", None),
            ]
            month_ok = False
            last_error = ""
            for url, params in request_variants:
                try:
                    resp = requests.get(url, params=params, timeout=timeout)
                    resp.raise_for_status()
                except Exception as e:
                    last_error = type(e).__name__
                    continue

                api_error = korean_holiday_api_error_message(resp.text)
                if api_error:
                    last_error = api_error
                    continue

                try:
                    month_holidays = parse_korean_holiday_payload(resp.json())
                except ValueError:
                    month_holidays = parse_korean_holiday_xml(resp.text)

                if not month_holidays:
                    month_holidays = parse_korean_holiday_xml(resp.text)
                holidays.update(month_holidays)
                month_ok = True
                break

            if not month_ok:
                errors.append(f"{api_url.rsplit('/', 1)[-1]} {month:02d}: {last_error or 'empty response'}")
                break
        if holidays:
            return dict(sorted(holidays.items()))

        if not errors or api_url.rsplit('/', 1)[-1] not in errors[-1]:
            try:
                fallback_params = {
                    "ServiceKey": service_key,
                    "solYear": f"{int(year):04d}",
                    "numOfRows": "100",
                    "_type": "json",
                }
                resp = requests.get(api_url, params=fallback_params, timeout=timeout)
                resp.raise_for_status()
            except Exception as e:
                errors.append(f"{api_url.rsplit('/', 1)[-1]}: {type(e).__name__}")
                continue

            api_error = korean_holiday_api_error_message(resp.text)
            if api_error:
                errors.append(f"{api_url.rsplit('/', 1)[-1]}: {api_error}")
                continue
            try:
                holidays = parse_korean_holiday_payload(resp.json())
            except ValueError:
                holidays = parse_korean_holiday_xml(resp.text)

            if not holidays:
                holidays = parse_korean_holiday_xml(resp.text)
            if holidays:
                return dict(sorted(holidays.items()))
            errors.append(f"{api_url.rsplit('/', 1)[-1]}: empty response")

    raise RuntimeError("Korean holiday API failed: " + ", ".join(errors[-6:]))


def korean_holiday_overrides(cfg: Optional[Dict[str, Any]], year: int) -> Dict[str, str]:
    schedule_cfg = (cfg or {}).get("schedule") or {}
    merged: Dict[str, str] = {}
    for key in ("korean_public_holidays", "korean_public_holiday_overrides"):
        values = schedule_cfg.get(key) or {}
        if not isinstance(values, dict):
            continue
        year_values = values.get(str(year)) or values.get(int(year)) or {}
        if isinstance(year_values, dict):
            values = {**values, **year_values}
        for date, name in values.items():
            date_text = str(date).strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
                merged[date_text] = str(name or "공휴일")
    return {date: name for date, name in merged.items() if date.startswith(f"{int(year):04d}-")}


def load_korean_public_holidays(
    year: int,
    cfg: Optional[Dict[str, Any]] = None,
    refresh: bool = False,
    cache_dir: str = KOREA_HOLIDAY_CACHE_DIR,
) -> Dict[str, str]:
    holidays: Dict[str, str] = {}
    if not refresh:
        holidays = read_korean_holiday_cache(year, cache_dir=cache_dir)

    if refresh or not holidays:
        try:
            holidays = fetch_korean_public_holidays_from_api(year)
            write_korean_holiday_cache(year, holidays, cache_dir=cache_dir)
        except Exception as e:
            cached = read_korean_holiday_cache(year, cache_dir=cache_dir)
            if cached:
                print(f"한국 공휴일 API 조회 실패, 캐시 사용: {e}")
                holidays = cached
            else:
                print(f"한국 공휴일 API 조회 실패, 캐시 없음: {e}")
                holidays = {}

    holidays.update(korean_holiday_overrides(cfg, year))
    return dict(sorted(holidays.items()))


def korean_public_holiday_for_date(
    date_text: str,
    cfg: Optional[Dict[str, Any]] = None,
    refresh: bool = False,
    cache_dir: str = KOREA_HOLIDAY_CACHE_DIR,
) -> Optional[str]:
    try:
        target = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        return None
    holidays = load_korean_public_holidays(target.year, cfg=cfg, refresh=refresh, cache_dir=cache_dir)
    return holidays.get(date_text)


def should_skip_korean_public_holiday_run(
    date_text: str,
    cfg: Optional[Dict[str, Any]],
    cache_dir: str = KOREA_HOLIDAY_CACHE_DIR,
) -> Optional[str]:
    schedule_cfg = (cfg or {}).get("schedule") or {}
    enabled = truthy_config_value(schedule_cfg.get("skip_korean_public_holidays"), default=False)
    if not enabled:
        return None
    if os.getenv("FORCE_MONITOR_RUN", "").lower() in ("1", "true", "yes"):
        return None
    if "--ignore-korean-holiday" in sys.argv or "--ignore-korean-holidays" in sys.argv:
        return None
    return korean_public_holiday_for_date(date_text, cfg=cfg, refresh=False, cache_dir=cache_dir)


def seconds_until_local_time(target_hhmm: str, now: Optional[datetime] = None) -> int:
    m = re.match(r'^\s*(\d{1,2}):(\d{2})\s*$', target_hhmm or "")
    if not m:
        return 0
    hour = int(m.group(1))
    minute = int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return 0

    now = now or local_datetime()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        return 0
    return max(0, int((target - now).total_seconds()))


def normalize_published_value(value: Any) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, (int, float)) or re.fullmatch(r"\d{10,13}", str(value).strip()):
        try:
            raw_ts = float(value)
            ts = raw_ts / 1000 if raw_ts >= 10_000_000_000 else raw_ts
            return local_datetime(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            return str(value).strip() or None

    return str(value).strip() or None


def canonical_article_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return text

    parsed = urlparse(text)
    host = parsed.netloc.lower()
    path = parsed.path or ""
    if host.endswith("iam-media.com") and path.startswith("/index.php/"):
        path = path.replace("/index.php/", "/", 1)
        return parsed._replace(path=path).geturl()
    return text


def canonicalize_seen_urls(urls: Any) -> set:
    return {canonical_article_url(url) for url in urls or [] if str(url or "").strip()}



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
    r'^terms (of use|and conditions)$',
    r'^terms$',
    r'^product specific terms$',
    r'^cookie (notice|policy|settings)$',
    r'^glossary$',
    r'^contact us$',
    r'^contact$',
    r'^about us$',
    r'^about$',
    r'^help$',
    r'^faq$',
    r'^faqs$',
    r'^read blog$',
    r'^on-demand webinars?$',
    r'^webinars?$',
    r'^login$',
    r'^log in$',
    r'^sign in$',
    r'^subscribe to\b',
    r'^subscribe$',

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
    r'\bis seeking (?:a |an )?.*(attorney|agent|associate|counsel)\b',

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
    r'^innovation @ thomson reuters\b',
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
    r'/is-seeking-(?:a-|an-)?[^/]*(?:attorney|agent|associate|counsel)',
    r'/login/?$',
    r'/search/?$',
    r'/sitemap/?$',
    r'/site-map/?$',
    r'/privacy/?$',
    r'/privacy-policy/?$',
    r'/terms(?:-of-use|-and-conditions)?/?$',
    r'/terms-of-service/?$',
    r'/product-specific-terms/?$',
    r'/cookie-(?:notice|policy|settings)/?$',
    r'/glossary/?$',
    r'/accessibility/?$',
    r'/subscribe/?$',
    r'/on-demand-webinars?/?$',

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
    '미국 특허상표청(USPTO GovDelivery)': [
        r'^https://content\.govdelivery\.com/accounts/USPTO/bulletins/[a-z0-9]+',
        r'^https://www\.uspto\.gov/subscription-center/\d{4}/',
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

ALLOW_URL_PATTERNS_BY_SOURCE_MARKER = {
    'Thomson Reuters': [
        r'^https://www\.thomsonreuters\.com/en-us/posts/',
        r'^https://legal\.thomsonreuters\.com/blog/',
    ],
    'Bloomberg': [
        r'^https://www\.bloomberg\.com/news/articles/\d{4}-\d{2}-\d{2}/',
        r'^https://www\.bloomberg\.com/opinion/articles/\d{4}-\d{2}-\d{2}/',
    ],
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
    'intellectual property', 'trade secret', 'frand', 'standard essential', 'sep',
    'counterfeit', 'piracy', 'infringement',
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
    'マドリッド', # 마드리드 (상표 국제출원)
    # 중국어
    '知识产权',   # 지식재산권
    '专利',       # 특허
    '商标',       # 상표
    '版权',       # 판권/저작권
    '著作权',     # 저작권
    '侵权',       # 침해
    '商业秘密',   # 영업비밀
]

COLLECTION_IP_KEYWORD_FILTER_SOURCES = [
    '백악관',
    '연방거래위원회',
    'FTC',
    '국제무역위원회',
    'ITC',
    '상무부(新闻发布',
    '상무부(时政要闻',
    '시장감독관리총국(总局',
    '최고인민검찰원(最高检新闻',
    '최고인민검찰원(重点推荐',
    '최고인민법원(最高人民法院新闻',
    '일본 후생노동성',
    '일본 총무성',
    '유럽연합 집행위원회',
]
DETAIL_COLLECTION_IP_KEYWORD_FILTER_SOURCES = [
    '백악관',
    '연방거래위원회',
    'FTC',
    '국제무역위원회',
    'ITC',
]
DETAIL_KEYWORD_TEXT_CACHE: Dict[str, str] = {}
COLLECTION_SKIP_TITLE_PATTERNS = [
    r'招聘',
    r'聘用制',
    r'招考',
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

ROUNDUP_ARTICLE_PATTERNS = [
    r'\biam sunday digest\b',
    r'\bsunday digest\b',
    r'\bweekly digest\b',
    r'\bweek(?:ly)? in review\b',
    r'\bnews roundup\b',
    r'\broundup\b',
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


def looks_like_roundup_article(text: str) -> bool:
    normalized = _norm(html.unescape(text or ""))
    slug_normalized = re.sub(r'[-_/]+', ' ', normalized)
    return any(
        re.search(pattern, normalized, re.I) or re.search(pattern, slug_normalized, re.I)
        for pattern in ROUNDUP_ARTICLE_PATTERNS
    )


def should_skip_claude_analysis(art: Article) -> Optional[str]:
    content_text = " ".join([
        art.title or "",
        art.url or "",
        art.summary_raw or "",
    ])
    source_text = art.source or ""
    normalized = _norm(html.unescape(content_text))

    if looks_like_roundup_article(content_text):
        return "multi_item_roundup"

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


def source_requires_collection_ip_keyword_filter(source_name: str) -> bool:
    return any(marker in (source_name or "") for marker in COLLECTION_IP_KEYWORD_FILTER_SOURCES)


def source_allows_detail_collection_ip_keyword_filter(source_name: str) -> bool:
    return any(marker in (source_name or "") for marker in DETAIL_COLLECTION_IP_KEYWORD_FILTER_SOURCES)


def collection_keyword_matches(text: str) -> bool:
    normalized = _norm(html.unescape(text or ""))
    if not normalized:
        return False

    for keyword in IP_KEYWORDS:
        k = keyword.lower()
        if re.fullmatch(r"[a-z0-9]{1,3}", k):
            if re.search(rf"\b{re.escape(k)}\b", normalized):
                return True
        elif k in normalized:
            return True
    return False


def collection_title_should_skip(title: str) -> bool:
    normalized = _norm(html.unescape(title or ""))
    return any(re.search(pattern, normalized, re.I) for pattern in COLLECTION_SKIP_TITLE_PATTERNS)


def fetch_detail_text_for_keyword_filter(url: str, timeout: int = 20) -> str:
    if not url or not re.match(r"^https?://", url):
        return ""
    if url in DETAIL_KEYWORD_TEXT_CACHE:
        return DETAIL_KEYWORD_TEXT_CACHE[url]

    try:
        resp = curl_requests.get(
            url,
            impersonate="chrome120",
            timeout=timeout,
            verify=False,
        )
        if not resp.ok:
            DETAIL_KEYWORD_TEXT_CACHE[url] = ""
            return ""
    except Exception:
        DETAIL_KEYWORD_TEXT_CACHE[url] = ""
        return ""

    raw_html = decode_html_response(resp)
    text = extract_detail_keyword_text(raw_html)
    DETAIL_KEYWORD_TEXT_CACHE[url] = text[:20000]
    return DETAIL_KEYWORD_TEXT_CACHE[url]


def extract_detail_keyword_text(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        tag.decompose()
    for selector in [
        '[role="banner"]',
        '[role="navigation"]',
        '[role="contentinfo"]',
        '.breadcrumb',
        '.breadcrumbs',
        '.menu',
        '.nav',
        '.navigation',
        '.related',
        '.share',
        '.social',
        '.sidebar',
    ]:
        for tag in soup.select(selector):
            tag.decompose()

    content = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )
    return content.get_text(" ", strip=True)


def passes_collection_ip_keyword_filter(
    source_name: str,
    title: str,
    url: str = "",
    summary: str = "",
    fetch_detail: bool = False,
    timeout: int = 20,
) -> bool:
    if not source_requires_collection_ip_keyword_filter(source_name):
        return True
    if collection_title_should_skip(title):
        return False
    if collection_keyword_matches(" ".join([title or "", url or "", summary or ""])):
        return True
    if fetch_detail and source_allows_detail_collection_ip_keyword_filter(source_name):
        return collection_keyword_matches(fetch_detail_text_for_keyword_filter(url, timeout=timeout))
    return False


def passes_source_allowlist(source_name: str, url: str) -> bool:
    patterns = list(ALLOW_URL_PATTERNS_BY_SOURCE.get(source_name, []))
    for marker, marker_patterns in ALLOW_URL_PATTERNS_BY_SOURCE_MARKER.items():
        if marker in (source_name or ""):
            patterns.extend(marker_patterns)
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
        return canonicalize_seen_urls(remote_seen)
    if isinstance(remote_seen, dict) and isinstance(remote_seen.get("urls"), list):
        urls = remote_seen["urls"]
        print(f"Supabase seen_urls 로드: {len(urls)}개")
        return canonicalize_seen_urls(urls)

    if not os.path.exists(SEEN_PATH):
        return set()
    try:
        with open(SEEN_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return canonicalize_seen_urls(data)
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
            out[canonical_article_url(url)] = item
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
    run_id = log.get("run_id") or local_run_id()
    path = os.path.join(RUN_LOG_DIR, f"run_{run_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    run_log_key = f"{SUPABASE_RUN_LOG_PREFIX}_{run_id}"
    if save_supabase_state(run_log_key, log):
        print(f"Supabase {run_log_key} 저장")
    if save_supabase_state(SUPABASE_LATEST_RUN_LOG_KEY, log):
        print(f"Supabase {SUPABASE_LATEST_RUN_LOG_KEY} 저장")

    return path


def completed_run_log_for_date(run_date: str) -> Optional[Dict[str, Any]]:
    latest = load_supabase_state(SUPABASE_LATEST_RUN_LOG_KEY)
    if not isinstance(latest, dict):
        return None
    if latest.get("duplicate_skip"):
        original_run_id = latest.get("duplicate_of_run_id")
        if original_run_id:
            original = load_supabase_state(f"{SUPABASE_RUN_LOG_PREFIX}_{original_run_id}")
            if isinstance(original, dict):
                latest = original
        if latest.get("duplicate_skip"):
            return None
    if kst_date_from_run_log(latest) != run_date:
        return None
    if not latest.get("finished_at"):
        return None
    if latest.get("duration_seconds") is None:
        return None
    return latest


def is_scheduled_run() -> bool:
    if os.getenv("GITHUB_EVENT_NAME") == "schedule":
        return True
    return os.getenv("MONITOR_SCHEDULED_RUN", "").lower() in ("1", "true", "yes")


def should_skip_duplicate_scheduled_run(run_date: str) -> Optional[Dict[str, Any]]:
    if not is_scheduled_run():
        return None
    if os.getenv("FORCE_MONITOR_RUN", "").lower() in ("1", "true", "yes"):
        return None
    return completed_run_log_for_date(run_date)


def normalize_date_parts(year: str, month: str, day: str) -> str:
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def safe_normalize_date_parts(year: str, month: str, day: str) -> Optional[str]:
    try:
        normalized = normalize_date_parts(year, month, day)
        datetime.strptime(normalized, "%Y-%m-%d")
        return normalized
    except (TypeError, ValueError):
        return None


def extract_date_from_text(text: str) -> Optional[str]:
    if not text:
        return None

    normalized_text = (
        text.translate(str.maketrans("０１２３４５６７８９．／－", "0123456789./-"))
        .replace("Ｒ", "R")
        .replace("\u2003", " ")
        .replace("\u3000", " ")
    )

    slash_date = re.search(r'(?P<a>\d{1,2})/(?P<b>\d{1,2})/(?P<y>20\d{2})', normalized_text)
    if slash_date:
        first = int(slash_date.group("a"))
        second = int(slash_date.group("b"))
        # US sources such as USPTO use MM/DD/YYYY. If only one ordering is valid,
        # choose that; otherwise prefer MM/DD because most slash-dated feeds here
        # are US government or US media sources.
        if second > 12:
            date = safe_normalize_date_parts(slash_date.group("y"), str(first), str(second))
        elif first > 12:
            date = safe_normalize_date_parts(slash_date.group("y"), str(second), str(first))
        else:
            date = safe_normalize_date_parts(slash_date.group("y"), str(first), str(second))
        if date:
            return date

    patterns = [
        r'(?P<y>20\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})',
        r'(?P<y>20\d{2})年(?P<m>\d{1,2})月(?P<d>\d{1,2})日',
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
            date = safe_normalize_date_parts(parts["y"], str(month), parts["d"])
            if date:
                return date

    era = re.search(r'(?:令和|R)\s*(?P<y>\d{1,2})\s*[.年]\s*(?P<m>\d{1,2})\s*[.月]\s*(?P<d>\d{1,2})', normalized_text, re.I)
    if era:
        year = 2018 + int(era.group("y"))
        return safe_normalize_date_parts(str(year), era.group("m"), era.group("d"))

    return None


def extract_date_from_url(url: str) -> Optional[str]:
    if not url:
        return None

    m = re.search(r'/((20\d{2})/(\d{1,2})/(\d{1,2}))(?:/|$)', url)
    if m:
        return safe_normalize_date_parts(m.group(2), m.group(3), m.group(4))

    m = re.search(r'(20\d{2})(\d{2})(\d{2})(?=\.html?|[^\d]|$)', url)
    if m:
        return safe_normalize_date_parts(m.group(1), m.group(2), m.group(3))

    m = re.search(r'/((20\d{2}))/er(\d{2})(\d{2})_', url, re.I)
    if m:
        return safe_normalize_date_parts(m.group(2), m.group(3), m.group(4))

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
    "중국 상무부",
    "IPRdaily",
    "베트남 지식재산청",
    "중국 지식산권보",
    "Thomson Reuters",
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

    raw_html = decode_html_response(resp)
    labeled_date = re.search(
        r'(?:发布时间|发布日期|发表时间|更新时间)[^0-9]{0,120}'
        r'(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)',
        raw_html,
        re.I,
    )
    if labeled_date:
        date = extract_date_from_text(labeled_date.group(1))
        if date:
            return date

    soup = BeautifulSoup(raw_html, 'html.parser')

    for selector in [
        'meta[property="article:published_time"]',
        'meta[name="article:published_time"]',
        'meta[name="article-published_time"]',
        'meta[name="firstpublishedtime"]',
        'meta[name="lastmodifiedtime"]',
        'meta[name="PubDate"]',
        'meta[name="publishdate"]',
        'meta[property="og:updated_time"]',
        'time',
        '#time',
        '.pub_date',
        '.pages-date',
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
            if not passes_collection_ip_keyword_filter(source.name, title, full_url, fetch_detail=True):
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
        if rows and source_requires_collection_ip_keyword_filter(source.name):
            return []

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
            if not passes_collection_ip_keyword_filter(source.name, text, full_url, fetch_detail=True):
                print(f"[DEBUG] ❌ 탈락 (IP 키워드 없음) | {text[:20]}... | {full_url}")
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
    if not getattr(d, "entries", None):
        try:
            resp = curl_requests.get(
                source.monitor_url,
                impersonate="chrome120",
                timeout=timeout,
                verify=False,
            )
            if resp.ok:
                d = feedparser.parse(resp.content)
            elif "IP Watchdog" in source.name:
                raise RuntimeError(f"RSS HTTP {resp.status_code}")
        except Exception as e:
            if "IP Watchdog" in source.name:
                raise RuntimeError(f"RSS fetch failed: {e}") from e

    if "IP Watchdog" in source.name and not getattr(d, "entries", None):
        bozo_error = getattr(d, "bozo_exception", None)
        detail = f": {bozo_error}" if bozo_error else ""
        raise RuntimeError(f"RSS returned no entries{detail}")

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
        if not passes_collection_ip_keyword_filter(source.name, title, link, summary):
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
    resp = None
    last_error = None
    for attempt in range(1, 4):
        try:
            # impersonate="chrome120" 옵션이 핵심입니다. 크롬 120 버전의 통신 지문을 완벽 복제합니다.
            resp = curl_requests.get(
                source.monitor_url,
                impersonate="chrome120",
                timeout=max(timeout, 20),
                verify=False
            )

            print(f"\n[DEBUG] {source.name} HTTP 상태 코드: {resp.status_code} (attempt {attempt})")

            if resp.ok:
                break

            last_error = f"HTTP {resp.status_code}"
            print(f"[DEBUG] 응답 에러: {resp.status_code}")
        except Exception as e:
            last_error = str(e)
            print(f"[DEBUG] {source.name} 요청 실패(attempt {attempt}): {e}")
        if attempt < 3:
            time.sleep(2 * attempt)

    if not resp or not resp.ok:
        raise RuntimeError(f"{source.name} 요청 실패: {last_error or 'unknown error'}")

    if "CNIPA" in source.name:
        print(f"[DEBUG] 원문 HTML 미리보기 (최대 1000자):\n{resp.text[:1000]}\n")

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
        if "Bloomberg" in source.name and not rows:
            raise RuntimeError("Bloomberg search returned no result rows")

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
            if not passes_collection_ip_keyword_filter(source.name, title, full_url, fetch_detail=True, timeout=timeout):
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
        if rows and source_requires_collection_ip_keyword_filter(source.name):
            return []

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
            if not passes_collection_ip_keyword_filter(source.name, text, full_url, fetch_detail=True, timeout=timeout):
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
        published = normalize_published_value(item.get('published') or item.get('date') or item.get('post_date'))

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


def fetch_iprdaily_api(source: SourceConfig, timeout: int = 20) -> List[Article]:
    print(f"[DEBUG] {source.name} 수집 시작 (IPRdaily API 모드)")
    articles: List[Article] = []
    seen_url = set()

    headers = {
        'User-Agent': 'Mozilla/5.0 IP-Monitor-MVP',
        'Referer': source.homepage,
        'X-Requested-With': 'XMLHttpRequest',
    }

    for page in range(1, 6):
        try:
            resp = curl_requests.get(
                source.monitor_url,
                params={"page": page},
                headers=headers,
                impersonate="chrome120",
                timeout=timeout,
                verify=False,
            )
            print(f"[DEBUG] {source.name} API page={page} HTTP 상태 코드: {resp.status_code}")
            if not resp.ok:
                continue
            data = resp.json()
        except Exception as e:
            print(f"[DEBUG] {source.name} API page={page} 로드 실패: {e}")
            continue

        html_fragment = str(data.get("msg") or "")
        if not html_fragment.strip():
            continue

        soup = BeautifulSoup(html_fragment, "html.parser")
        rows = soup.select("li.box-list")
        print(f"[DEBUG] {source.name} API page={page} row 후보 개수: {len(rows)}")
        if not rows:
            continue

        for row in rows:
            title_el = row.select_one("dl.article dt.title")
            link_el = row.select_one("dl.article > a[href]") or row.select_one("a[href*='news_'], a[href*='article_']")
            if not link_el:
                continue

            title = (title_el.get_text(" ", strip=True) if title_el else "").strip()
            if not title:
                img = row.select_one("img[alt], img[title]")
                title = (
                    (img.get("alt") if img else "")
                    or (img.get("title") if img else "")
                    or link_el.get_text(" ", strip=True)
                    or ""
                ).strip()

            href = (link_el.get("href") or "").strip()
            if not title or not href:
                continue

            full_url = urljoin(source.homepage, href)
            if looks_like_non_article(title, full_url):
                continue
            if not passes_source_allowlist(source.name, full_url):
                continue
            if full_url in seen_url:
                continue

            summary = ""
            summary_el = row.select_one("dl.article dd.box-con")
            if summary_el:
                summary = summary_el.get_text(" ", strip=True)

            category = ""
            category_el = row.select_one(".l_bie")
            if category_el:
                category = category_el.get_text(" ", strip=True)
            if category:
                summary = f"{summary} [{category}]".strip()

            published = None
            date_el = row.select_one("dd.time")
            if date_el:
                published = extract_date_from_text(date_el.get_text(" ", strip=True))
            if not published:
                published = extract_date_from_context(row, full_url)

            seen_url.add(full_url)
            articles.append(Article(
                source=source.name,
                region=source.region,
                title=title,
                url=full_url,
                summary_raw=summary,
                published=published,
            ))

    print(f"[DEBUG] {source.name} API에서 {len(articles)}개 기사 파싱 성공")
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
    if source.mode == 'iprdaily_api':
        return fetch_iprdaily_api(source, timeout)
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


def normalize_korean_policy_terms(text: str) -> str:
    if not text:
        return ""
    normalized = str(text)
    normalized = re.sub(r'USPTO\s*(?:원장|국장)', 'USPTO 청장', normalized, flags=re.IGNORECASE)
    normalized = re.sub(r'미국\s*특허상표청\s*(?:원장|국장)', '미국 특허상표청 청장', normalized)
    normalized = re.sub(r'원장\s*\(Director\)', '청장(Director)', normalized)
    normalized = re.sub(r'국장\s*\(Director\)', '청장(Director)', normalized)
    normalized = re.sub(r'(?<![A-Za-z])KIPO(?![A-Za-z])', '지식재산처(MOIP)', normalized)
    normalized = re.sub(r'한국\s*특허청', '지식재산처(MOIP)', normalized)
    normalized = re.sub(r'대한민국\s*특허청', '지식재산처(MOIP)', normalized)
    return normalized


def clamp_score_axis(value: Any, default: int = 0) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(0, min(100, score))


class ClaudeClient:
    def __init__(self, model: str = 'claude-sonnet-4-6'):
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            raise RuntimeError('환경변수 ANTHROPIC_API_KEY가 설정되지 않았습니다.')
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def estimate_cost_usd(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> float:
        cost = (
            input_tokens * CLAUDE_SONNET_INPUT_USD_PER_MTOK
            + output_tokens * CLAUDE_SONNET_OUTPUT_USD_PER_MTOK
            + cache_creation_input_tokens * CLAUDE_SONNET_CACHE_CREATE_USD_PER_MTOK
            + cache_read_input_tokens * CLAUDE_SONNET_CACHE_READ_USD_PER_MTOK
        ) / 1_000_000
        return round(cost, 6)

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
1. 중요도 점수를 0~100 사이 정수로 매기되, 먼저 아래 5개 평가축을 각각 0~100으로 판단한 뒤 종합하라.
   - ip_directness: 특허·상표·디자인·저작권·영업비밀·IP 집행·라이선스·표준필수특허 등 지식재산 이슈가 얼마나 직접적인가.
   - policy_materiality: 법령·심사/심판 실무·집행 기준·판례·국제규범·기관 정책 변화가 얼마나 실질적인가.
   - source_authority: 공식기관·법원·국제기구·규제기관 등 출처의 권위가 얼마나 높은가.
   - korea_relevance: 한국 또는 지식재산처(MOIP), 한국 기업, 한국 제도에 직접 참고할 만한 정도가 얼마나 높은가.
   - timeliness: 최근성·긴급성·후속 조치 필요성이 얼마나 높은가.
   - 최종 importance_score는 위 축을 종합하되, source_authority만 높고 ip_directness나 policy_materiality가 낮으면 높은 점수를 주지 마라.
   - "Technology", "innovation", "AI", "digital" 같은 일반 기술 키워드만으로 IP 중요도를 높이지 말고, IP 제도·권리·집행·라이선스·분쟁과의 직접 연결을 확인하라.
   - 2차 출처라도 USPTO/PTAB/IPR/Director Review/Inter Partes Review처럼 특허심판·심사 실무 변화가 직접 드러나면 ip_directness와 policy_materiality를 높게 평가할 수 있다.
   - score_reason에는 최종 점수의 핵심 이유를 1문장으로 쓰되, 어떤 축이 높고 낮았는지 드러나게 하라.
   - '한국'이나 'South Korea' '삼성전자' 'samsung' 'sk'등 한국 기업을 직접 언급하고 있으면 중요도 점수를 상향하고, 요약에 해당 내용을 포함하라. 직접 언급이 없으면 한국 관련성만으로 과도하게 점수를 올리지 마라.
   - 1차 출처(외국 정부기관, 특허청, 법원 등)의 자료는 점수를 상향하고, 2차 출처(언론 보도 등)의 자료는 점수를 하향하라.
   - 다만 1차 출처라는 이유만으로 고득점을 주지 말고, 반드시 IP 직접성과 정책 실질성을 함께 확인하라.
   - 점수는 아래 구간을 기준으로 부여하라. 애매한 경우에는 높은 구간으로 올리지 말고 낮은 구간의 상단 점수를 사용하라.
     * 0~20: 비IP 이슈, 단순 행사·기관 소개·자료 게시·고정 안내 페이지
     * 21~40: IP 관련성은 있으나 정책 중요도 낮음, 홍보·교육·사례·개별 기업 동향 중심
     * 41~55: 단순 보도, 회의자료·배부자료·개최안내, 개별 사건이지만 제도 변화 신호가 약한 사안
     * 56~70: 정책·제도·집행 변화 가능성이 있거나 공식기관 조치가 확인되는 중요 동향
     * 71~85: 공식 법령·판결·규제·집행·국제규범 변화 등 정책 판단에 중요한 동향
     * 86~100: 한국 정책 판단에 직접 참고할 수 있는 고중요 동향 또는 중대한 국제 IP 규범·분쟁 변화
2. 아래 기준을 종합적으로 고려하라.
   - IP 법·제도·정책 변경 가능성
   - 국제 규범 변화(WIPO, WTO, FTA 등) 연관성
   - AI, 데이터, 표준필수특허, 반도체, 디지털 저작권 등 전략 분야 관련성
   - 국내 제도 개선 논의, 정책에 활용 가능성
   - 시의성·긴급성
3. 한국어로 120자 이내의 핵심 요약을 작성하라. 요약에는 시사점이나 평가가 들어가지 않고, 링크에서 나타난 가장 중요한 사실만 1문장으로 넣는다.
   - 원문에 한국, 대한민국, South Korea, Korea, KIPO 등 한국 관련 표현이 직접 등장하지 않으면 요약에 한국을 언급하지 마라.
   - 원문이 한국을 직접 언급하지 않는데 "한국도 평가 대상", "한국에 직접 영향", "한국이 지정될 경우"처럼 확정적·가정적 한국 중심 문장을 만들지 마라.
   - digest_bullets는 요약과 같은 문장을 반복하지 말고, 요약만으로는 알 수 없는 세부 사실을 보완하는 텔레그램용 하위 불릿이다.
   - digest_bullets는 2~3개로 작성하고, 각 항목은 내용을 가장 잘 설명하는 카테고리 라벨로 시작한다.
   - 카테고리 예: "배경: ...", "쟁점: ...", "문제점: ...", "향후일정: ...", "변경사항: ...", "결정: ...", "조치: ..."
   - 카테고리는 서로 겹치지 않게 상호배타적으로 정한다. 각 불릿은 요약 문장의 단순 재진술이 아니라 별도의 추가 정보여야 한다.
   - 범주화하기 어렵거나 요약과 중복되는 내용밖에 없다면 digest_bullets를 억지로 3개까지 채우지 말고 1~2개만 작성한다.
   - digest_bullets에는 원문에 나타난 사실관계만 담고, 시사점, 평가, 전망, 한국 정책 참고사항은 넣지 마라.
4. 1~2단어의 카테고리(예: 특허정책, 저작권, AI규제, 표준특허, 무역분쟁 등)를 정하라.
5. issue_region을 작성하라.
   - issue_region은 출처 매체의 소재지가 아니라 기사에서 다루는 실제 정책·분쟁·시장 이슈의 대상 지역이다.
   - 예: 미국 매체가 EU의 IP 정책을 다루면 issue_region은 "유럽" 또는 "EU"로 작성한다.
   - 전세계 또는 다자기구 이슈면 "국제" 또는 "국제기구"로 작성한다.
   - 명확하지 않으면 기사 메타정보의 지역/국가 값을 사용한다.
6. 같은 사건·보고서·판례·법안·정책 발표·기업 발표를 묶을 수 있도록 topic_key와 topic_label을 작성하라.
   - topic_key는 영문 소문자 slug로 작성한다. 예: "2026-ustr-special-301-report", "uspto-gen-ai-patent-examination"
   - topic_label은 사람이 읽기 쉬운 짧은 이슈명으로 작성한다. 예: "USTR 2026 Special 301 Report"
   - 같은 이슈를 다른 매체가 보도한 경우 동일한 topic_key가 나오도록 일반적이고 안정적인 이름을 사용한다.

추가 규칙:
- factual summary와 policy implication을 엄격히 분리하라. `summary_ko`와 `digest_bullets`에는 원문에 나타난 사실관계만 넣고, 정책적 시사점·평가·추론은 작성하지 마라.
- "한국도", "국내 제도", "한국 정책당국" 같은 표현은 원문 직접 언급이 없으면 `summary_ko`에서 금지한다.
- 용어 번역을 일관되게 하라. USPTO의 "Director"는 "청장"으로 번역하고, "원장" 또는 "국장"으로 번역하지 마라.
- 한국의 지식재산 행정기관은 "한국특허청(KIPO)"이 아니라 "지식재산처(MOIP)"로 표기하라.
- 단순 기관 소개, 서비스 소개, 검색도구 안내, 데이터베이스 안내, 메뉴 페이지, 고정된 법령 원문 페이지(예: Title 17 전체 텍스트)는 정책 변경이 없다면 중요도를 0~20 사이로 낮게 평가하라.
- 특허검색 서비스, 특허·저작권 등록부, 공보 시스템, 포털, 안내 페이지처럼 '운영 중인 툴/서비스' 중심인 문서는, 새로운 정책·제도 도입이나 변경 내용을 포함하지 않는 한 중요도를 0~20으로 제한하라.
- "Patents", "Patent basics", "Search our patent database" 같은 제도·툴 안내 랜딩 페이지는 신규 정책 내용이 없으면 중요도를 0~25로 제한하라.
- 내비게이션용 텍스트(예: "Skip to main content", "Skip to footer")나 메뉴/섹션 제목(예: "Understanding IP", "Types of IP")처럼 실제 기사·공지로 보이지 않으면 중요도를 0~10으로 낮추고, 요약에서 '실질적인 내용이 없는 페이지로 보이며, 재수집 또는 본문 확인 필요'라고 명시하라.

JSON으로만 응답하라:
{{
  "importance_score": 87,
  "ip_directness": 90,
  "policy_materiality": 85,
  "source_authority": 95,
  "korea_relevance": 50,
  "timeliness": 80,
  "score_reason": "IP 직접성과 정책 실질성이 모두 높고 공식기관 발표라 높은 점수를 부여했다.",
  "category": "AI규제",
  "summary_ko": "…",
  "digest_bullets": ["배경: …", "쟁점: …", "향후일정: …"],
  "topic_key": "uspto-ai-patent-examination",
  "topic_label": "USPTO AI Patent Examination",
  "issue_region": "미국"
}}
"""
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=1100,
            temperature=0.1,
            messages=[{'role': 'user', 'content': prompt}]
        )

        text = resp.content[0].text
        usage = getattr(resp, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_creation_input_tokens = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read_input_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        estimated_cost_usd = self.estimate_cost_usd(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
        )
        m = re.search(r'\{.*\}', text, re.S)
        data = {}
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = {}

        importance = int(data.get('importance_score', 50))
        importance = max(0, min(100, importance))
        ip_directness = clamp_score_axis(data.get("ip_directness"))
        policy_materiality = clamp_score_axis(data.get("policy_materiality"))
        source_authority = clamp_score_axis(data.get("source_authority"))
        korea_relevance = clamp_score_axis(data.get("korea_relevance"))
        timeliness = clamp_score_axis(data.get("timeliness"))
        score_reason = normalize_korean_policy_terms(str(data.get("score_reason", "")).strip())

        category = str(data.get('category', '기타')).strip() or '기타'
        summary_ko = normalize_korean_policy_terms(str(data.get('summary_ko', '')).strip())
        digest_bullets = data.get('digest_bullets', [])
        if not isinstance(digest_bullets, list):
            digest_bullets = [str(digest_bullets)]
        digest_bullets = [
            normalize_korean_policy_terms(str(x).strip())
            for x in digest_bullets
            if str(x).strip()
        ]
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
            key_points=[],
            raw_excerpt=art.summary_raw,
            topic_key=topic_key,
            topic_label=topic_label,
            issue_region=issue_region,
            digest_bullets=digest_bullets,
            ip_directness=ip_directness,
            policy_materiality=policy_materiality,
            source_authority=source_authority,
            korea_relevance=korea_relevance,
            timeliness=timeliness,
            score_reason=score_reason,
            claude_model=self.model,
            claude_input_tokens=input_tokens,
            claude_output_tokens=output_tokens,
            claude_cache_creation_input_tokens=cache_creation_input_tokens,
            claude_cache_read_input_tokens=cache_read_input_tokens,
            claude_estimated_cost_usd=estimated_cost_usd,
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

PAID_DIGEST_SOURCE_KEYWORDS = [
    "mlex",
    "iam",
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


def is_paid_digest_source(source: str, url: str = "") -> bool:
    source_text = normalize_topic_text(source or "")
    domain = urlparse(url or "").netloc.lower()
    return any(keyword in source_text or keyword in domain for keyword in PAID_DIGEST_SOURCE_KEYWORDS)


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
            not is_paid_digest_source(item.source, item.url),
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
            digest_source_authority_score(cluster.representative),
            digest_direct_report_score(cluster.representative),
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


def digest_source_label(source: str) -> str:
    source = re.sub(r'\s+', ' ', source or "").strip()
    return re.sub(r'\s*\([^)]*\)\s*$', '', source).strip() or source or "-"


DIGEST_REGION_ICONS = {
    "미국": "🇺🇸",
    "중국": "🇨🇳",
    "일본": "🇯🇵",
    "한국": "🇰🇷",
    "대한민국": "🇰🇷",
    "영국": "🇬🇧",
    "독일": "🇩🇪",
    "프랑스": "🇫🇷",
    "인도": "🇮🇳",
    "호주": "🇦🇺",
    "캐나다": "🇨🇦",
    "브라질": "🇧🇷",
    "말레이시아": "🇲🇾",
    "베트남": "🇻🇳",
    "태국": "🇹🇭",
    "싱가포르": "🇸🇬",
    "필리핀": "🇵🇭",
    "인도네시아": "🇮🇩",
    "유럽": "🇪🇺",
    "EU": "🇪🇺",
    "유럽연합": "🇪🇺",
}


def digest_region_icon(region_label: str) -> str:
    region = re.sub(r'\s*\(출처지역:[^)]*\)\s*$', '', region_label or "").strip()
    if not region or region == "-":
        return "🌐"
    if any(sep in region for sep in ("·", "/", ",")):
        return "🌐"
    if region in ("국제", "국제기구", "전세계", "글로벌"):
        return "🌐"
    return DIGEST_REGION_ICONS.get(region, "🌐")


def run_date_from_run_id(run_id: str) -> str:
    try:
        return datetime.strptime(run_id[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return local_run_date()


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
        f"{item.title} {item.summary_ko}"
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
    # Duplicate suppression is intentionally disabled by current operating policy:
    # send digest candidates regardless of whether the same issue was sent recently.
    return False

    # Previous policy kept for reference.
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
    max_paid_sources: int = 1,
) -> tuple:
    """Select top digest topics, counting one clustered issue as one slot."""
    filtered = [x for x in analyzed if x.importance_score >= min_importance]
    topic_clusters = cluster_digest_topics(filtered)
    run_date = run_date or local_run_date()
    # Duplicate suppression via sent_digest_topics is disabled. Keep the
    # parameters for compatibility, but do not use them to skip candidates.

    selected = []
    skipped = []
    paid_count = 0
    for cluster in topic_clusters:
        is_paid = is_paid_digest_source(cluster.representative.source, cluster.representative.url)
        if is_paid and paid_count >= max_paid_sources:
            skipped.append({
                "topic_key": digest_cluster_topic_key(cluster),
                "title": cluster.representative.title,
                "source": cluster.representative.source,
                "reason": "paid_source_limit",
            })
            continue
        selected.append(cluster)
        if is_paid:
            paid_count += 1
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
    full_results_url: Optional[str] = None,
) -> str:
    lines = []
    if run_date:
        parsed_date = parse_iso_date(run_date)
        if parsed_date:
            title_date = f"{parsed_date.year}년 {parsed_date.month}월 {parsed_date.day}일"
        else:
            title_date = run_date
    else:
        title_date = local_run_date()
    lines.append(f"< {title_date} 데일리 IP 브리핑 >")
    lines.append("")
    lines.append(
        "※ 안내: AI가 생성한 참고용 요약이므로, 정확한 내용은 반드시 원문을 확인해 주시기 바랍니다. "
        "중요도순으로 상위 5건의 내용만 제공되며, 아래 링크에서 전체 결과를 확인할 수 있습니다."

    )
    lines.append("")
    if full_results_url:
        lines.append(f"전체 분석결과: {full_results_url}")
    lines.append("")

    if not selected_clusters:
        lines.append("오늘은 기준 점수 이상 신규 동향이 없습니다.")
        return "\n".join(lines)

    for i, cluster in enumerate(selected_clusters, start=1):
        item = cluster.representative
        title = compact_digest_text(item.title, max_chars=120)
        region_label = digest_region_label(item)
        lines.append(f"{i}. {title}")
        source_label = digest_source_label(item.source)
        if is_paid_digest_source(item.source, item.url):
            source_label = f"{source_label} 🔒"
        lines.append(f"{digest_region_icon(region_label)} {region_label} | 📰 {source_label} | 🏷 {item.category}")
        lines.append("")
        digest_bullets = getattr(item, "digest_bullets", None) or []
        if item.summary_ko:
            lines.append("📝 요약")
            lines.append(compact_digest_text(item.summary_ko, max_chars=160))
            lines.append("")
        if digest_bullets:
            for bullet in digest_bullets[:3]:
                lines.append(f"• {compact_digest_text(bullet, max_chars=180)}")
            lines.append("")
        link_label = "원문(구독 필요)" if is_paid_digest_source(item.source, item.url) else "원문"
        lines.append(f"🔗 {link_label}: {item.url}")
        lines.append("")

    return "\n".join(lines)


def build_telegram_digest(
    analyzed: List[AnalyzedArticle],
    top_n: int = 5,
    min_importance: int = 0,
    run_date: Optional[str] = None,
    full_results_url: Optional[str] = None,
) -> str:
    selected_clusters, _ = select_digest_clusters(
        analyzed,
        top_n=top_n,
        min_importance=min_importance,
    )
    return render_telegram_digest(selected_clusters, run_date=run_date, full_results_url=full_results_url)


def save_digest(text: str):
    os.makedirs('data', exist_ok=True)
    with open(DIGEST_PATH, 'w', encoding='utf-8') as f:
        f.write(text)


def load_notion_pages() -> Dict[str, Any]:
    remote = load_supabase_state(SUPABASE_NOTION_PAGES_KEY)
    if isinstance(remote, dict):
        return remote

    if not os.path.exists(NOTION_PAGES_PATH):
        return {}
    try:
        with open(NOTION_PAGES_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_notion_pages(items: Dict[str, Any]) -> None:
    os.makedirs('data', exist_ok=True)
    with open(NOTION_PAGES_PATH, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    save_supabase_state(SUPABASE_NOTION_PAGES_KEY, items)


def notion_enabled(cfg: Dict[str, Any]) -> bool:
    notion_cfg = cfg.get("notion", {})
    if not notion_cfg.get("publish_enabled", False):
        return False
    return bool(os.getenv("NOTION_API_KEY") and notion_digest_parent_page_id())


def notion_review_enabled(cfg: Dict[str, Any]) -> bool:
    notion_cfg = cfg.get("notion", {})
    if not notion_cfg.get("review_publish_enabled", False):
        return False
    return bool(os.getenv("NOTION_API_KEY") and notion_review_parent_page_id())


def notion_digest_parent_page_id() -> str:
    return os.getenv("NOTION_DIGEST_PARENT_PAGE_ID") or os.getenv("NOTION_PARENT_PAGE_ID") or ""


def notion_review_parent_page_id() -> str:
    return os.getenv("NOTION_REVIEW_PARENT_PAGE_ID") or ""


def notion_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.getenv('NOTION_API_KEY')}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def normalize_notion_page_id(value: Optional[str]) -> str:
    text = str(value or "").strip()
    match = re.search(
        r"([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})",
        text,
    )
    if not match:
        return text
    raw = match.group(1).replace("-", "")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def notion_request(method: str, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://api.notion.com/v1/{path.lstrip('/')}"
    for attempt in range(3):
        kwargs = {"headers": notion_headers(), "timeout": 30}
        if method.upper() == "GET":
            kwargs["params"] = payload
        else:
            kwargs["json"] = payload
        try:
            resp = requests.request(method, url, **kwargs)
        except requests.RequestException:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            raise
        if resp.status_code == 429 and attempt < 2:
            retry_after = int(resp.headers.get("Retry-After") or "1")
            time.sleep(max(1, retry_after))
            continue
        if 200 <= resp.status_code < 300:
            return resp.json()
        raise RuntimeError(f"Notion API {method} {path} failed: {resp.status_code} {resp.text[:500]}")
    raise RuntimeError(f"Notion API {method} {path} failed after retries")


def notion_text(content: Any, max_chars: int = 1900) -> List[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return []
    return [{"type": "text", "text": {"content": text[:max_chars]}}]


def notion_paragraph(text: Any) -> Dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": notion_text(text)}}


def notion_bullet(text: Any) -> Dict[str, Any]:
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": notion_text(text)}}


def notion_heading(text: Any, level: int = 2) -> Dict[str, Any]:
    block_type = "heading_3" if level == 3 else "heading_2"
    return {"object": "block", "type": block_type, block_type: {"rich_text": notion_text(text)}}


def notion_divider() -> Dict[str, Any]:
    return {"object": "block", "type": "divider", "divider": {}}


def notion_callout(text: Any) -> Dict[str, Any]:
    return {"object": "block", "type": "callout", "callout": {"rich_text": notion_text(text)}}


def notion_code(text: Any, language: str = "plain text") -> Dict[str, Any]:
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": notion_text(text, max_chars=1900),
            "language": language,
        },
    }


def chunk_text(text: Any, max_chars: int = 1800) -> List[str]:
    raw = str(text or "")
    if not raw:
        return [""]
    return [raw[start:start + max_chars] for start in range(0, len(raw), max_chars)]


def notion_code_blocks(text: Any, language: str = "plain text", max_chars: int = 1800) -> List[Dict[str, Any]]:
    return [notion_code(chunk, language=language) for chunk in chunk_text(text, max_chars=max_chars)]


def notion_plain_text_blocks(text: Any, max_chars: int = 1800) -> List[Dict[str, Any]]:
    return [notion_paragraph(chunk) for chunk in chunk_text(text, max_chars=max_chars)]


def notion_review_message_blocks(messages: List[str]) -> List[Dict[str, Any]]:
    section_headings = {
        "요약 리포트",
        "확인 필요",
        "수집 실패/빈값 소스",
        "날짜 누락 샘플",
        "제목 확인 필요 샘플",
    }
    blocks: List[Dict[str, Any]] = []
    for message_index, message in enumerate(messages, start=1):
        lines = [line.rstrip() for line in str(message or "").splitlines()]
        saw_content = False
        for line_index, line in enumerate(lines):
            text = line.strip()
            if not text:
                continue

            if line_index == 0 and text.startswith("IP Monitor 수집 검증 목록"):
                blocks.append(notion_paragraph(text))
            elif text.startswith("- "):
                blocks.append(notion_bullet(text[2:].strip()))
            elif text in section_headings:
                blocks.append(notion_heading(text, level=3))
            else:
                blocks.extend(notion_plain_text_blocks(text, max_chars=1800))
            saw_content = True

        if message_index < len(messages):
            blocks.append(notion_divider())
    return blocks


def notion_page_children(page_id: str) -> List[Dict[str, Any]]:
    children: List[Dict[str, Any]] = []
    cursor = None
    while True:
        params: Dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data = notion_request("GET", f"blocks/{page_id}/children", params)
        children.extend(data.get("results") or [])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return children


def clear_notion_page(page_id: str) -> None:
    for child in notion_page_children(page_id):
        child_id = child.get("id")
        if not child_id:
            continue
        notion_request("PATCH", f"blocks/{child_id}", {"archived": True})
        time.sleep(0.05)


def notion_item_blocks(item: AnalyzedArticle, sent_to_digest: bool, index: int) -> List[Dict[str, Any]]:
    marker = "Digest 발송" if sent_to_digest else "전문 only"
    title = compact_digest_text(item.title, max_chars=160)
    blocks = [
        notion_heading(f"{index}. {title}", level=3),
        notion_bullet(f"상태: {marker}"),
        notion_bullet(f"점수/분류/출처: {item.importance_score} | {item.category} | {item.source}"),
        notion_bullet(f"지역: {digest_region_label(item)}"),
    ]
    score_reason = getattr(item, "score_reason", "") or ""
    if score_reason:
        blocks.append(notion_bullet(f"점수 근거: {score_reason}"))
    score_axes = [
        int(getattr(item, "ip_directness", 0) or 0),
        int(getattr(item, "policy_materiality", 0) or 0),
        int(getattr(item, "source_authority", 0) or 0),
        int(getattr(item, "korea_relevance", 0) or 0),
        int(getattr(item, "timeliness", 0) or 0),
    ]
    if any(score_axes):
        blocks.append(
            notion_bullet(
                "평가축: "
                f"IP직접성 {score_axes[0]} / 정책실질성 {score_axes[1]} / "
                f"출처권위 {score_axes[2]} / 한국관련성 {score_axes[3]} / 시의성 {score_axes[4]}"
            )
        )
    topic_label = getattr(item, "topic_label", "") or ""
    if topic_label:
        blocks.append(notion_bullet(f"이슈: {topic_label}"))
    if item.summary_ko:
        blocks.append(notion_paragraph(f"요약: {item.summary_ko}"))
    digest_bullets = getattr(item, "digest_bullets", None) or []
    for bullet in digest_bullets[:4]:
        blocks.append(notion_bullet(bullet))
    blocks.append(notion_bullet(f"원문: {item.url}"))
    blocks.append(notion_divider())
    return blocks


def append_notion_blocks(page_id: str, blocks: List[Dict[str, Any]], chunk_size: int = 80) -> None:
    for start in range(0, len(blocks), chunk_size):
        chunk = blocks[start:start + chunk_size]
        notion_request(
            "PATCH",
            f"blocks/{page_id}/children",
            {"children": chunk},
        )
        time.sleep(0.35)


def publish_notion_analysis_page(
    analyzed_items: List[AnalyzedArticle],
    selected_clusters: List[DigestTopicCluster],
    run_date: str,
    cfg: Dict[str, Any],
) -> Optional[str]:
    if not notion_enabled(cfg):
        return None
    if not analyzed_items:
        return None

    notion_cfg = cfg.get("notion", {})
    max_items = int(notion_cfg.get("max_items") or 80)
    title_prefix = str(notion_cfg.get("page_title_prefix") or "IP 동향 전체 분석결과")
    selected_urls = {cluster.representative.url for cluster in selected_clusters}
    selected_keys = {digest_cluster_topic_key(cluster) for cluster in selected_clusters}
    sorted_items = sorted(
        analyzed_items,
        key=lambda item: int(getattr(item, "importance_score", 0) or 0),
        reverse=True,
    )[:max_items]
    title = f"{title_prefix} - {run_date}"

    response = notion_request(
        "POST",
        "pages",
        {
            "parent": {
                "type": "page_id",
                "page_id": normalize_notion_page_id(notion_digest_parent_page_id()),
            },
            "properties": {"title": {"title": notion_text(title, max_chars=200)}},
        },
    )
    page_id = response.get("id")
    page_url = response.get("url")
    if not page_id:
        return page_url

    digest_count = sum(1 for item in sorted_items if item.url in selected_urls or item.topic_key in selected_keys)
    blocks: List[Dict[str, Any]] = [
        notion_heading(title),
        notion_paragraph(
            f"총 분석 {len(analyzed_items)}건 중 {len(selected_clusters)}건은 텔레그램 Digest로 발송되었습니다. "
            f"이 페이지에는 점수순 최대 {max_items}건을 제공합니다."
        ),
        notion_bullet(f"Digest 발송 후보: {digest_count}건"),
        notion_bullet(f"생성일: {run_date}"),
        notion_divider(),
    ]
    for index, item in enumerate(sorted_items, start=1):
        sent_to_digest = item.url in selected_urls or item.topic_key in selected_keys
        blocks.extend(notion_item_blocks(item, sent_to_digest, index))

    append_notion_blocks(page_id, blocks)

    pages = load_notion_pages()
    pages[run_date] = {
        "page_id": page_id,
        "url": page_url,
        "created_at": local_timestamp(),
        "item_count": len(sorted_items),
        "digest_count": len(selected_clusters),
    }
    save_notion_pages(pages)
    return page_url


def publish_notion_review_page(
    messages: List[str],
    run_id: str,
    cfg: Dict[str, Any],
) -> Optional[str]:
    if not notion_review_enabled(cfg):
        return None
    if not messages:
        return None

    run_date = run_date_from_run_id(run_id)
    notion_cfg = cfg.get("notion", {})
    title_prefix = str(notion_cfg.get("review_page_title_prefix") or "IP Monitor 수집 검증 목록")
    title = f"{title_prefix} - {run_date}"

    response = notion_request(
        "POST",
        "pages",
        {
            "parent": {
                "type": "page_id",
                "page_id": normalize_notion_page_id(notion_review_parent_page_id()),
            },
            "properties": {"title": {"title": notion_text(title, max_chars=200)}},
        },
    )
    page_id = response.get("id")
    page_url = response.get("url")
    if not page_id:
        return page_url

    blocks: List[Dict[str, Any]] = [
        notion_heading(title),
        notion_paragraph("리뷰 텔레그램으로 발송된 수집 검증 목록 전문입니다."),
        notion_bullet(f"실행 ID: {run_id}"),
        notion_bullet(f"생성일: {run_date}"),
        notion_bullet(f"텔레그램 메시지 수: {len(messages)}개"),
        notion_divider(),
    ]
    blocks.extend(notion_review_message_blocks(messages))
    append_notion_blocks(page_id, blocks)

    pages = load_notion_pages()
    pages[f"review_{run_date}"] = {
        "page_id": page_id,
        "url": page_url,
        "created_at": local_timestamp(),
        "message_count": len(messages),
    }
    save_notion_pages(pages)
    return page_url


def notion_dashboard_enabled(cfg: Dict[str, Any]) -> bool:
    notion_cfg = cfg.get("notion", {})
    if not notion_cfg.get("dashboard_enabled", False):
        return False
    return bool(os.getenv("NOTION_API_KEY") and os.getenv("NOTION_DASHBOARD_PARENT_PAGE_ID"))


def create_notion_child_page(parent_page_id: str, title: str) -> Dict[str, Any]:
    return notion_request(
        "POST",
        "pages",
        {
            "parent": {
                "type": "page_id",
                "page_id": normalize_notion_page_id(parent_page_id),
            },
            "properties": {"title": {"title": notion_text(title, max_chars=200)}},
        },
    )


def kst_date_from_run_log(run_log: Dict[str, Any]) -> str:
    run_id = str(run_log.get("run_id") or "")
    if len(run_id) >= 8 and run_id[:8].isdigit():
        return f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}"
    started_at = str(run_log.get("started_at") or "")
    return started_at[:10] if len(started_at) >= 10 else local_run_date()


def notion_dashboard_blocks(run_log: Dict[str, Any]) -> List[Dict[str, Any]]:
    summary = run_log.get("summary") or {}
    sources = run_log.get("sources") or []
    run_date = kst_date_from_run_log(run_log)
    failed_sources = [item for item in sources if item.get("status") == "fail"]
    empty_sources = [item for item in sources if item.get("status") == "empty"]
    top_new_sources = sorted(
        sources,
        key=lambda item: int(item.get("new_count") or 0),
        reverse=True,
    )[:10]
    article_equation = (
        f"{summary.get('total_fetched_articles', 0)} Fetch 후보 - "
        f"{summary.get('seen_skipped_count', 0)} Seen 제외 - "
        f"{summary.get('stale_skipped_count', 0)} 오래된 기사 제외 - "
        f"{summary.get('non_article_skipped_count', 0)} 비기사 제외 = "
        f"{summary.get('total_new_articles', 0)} 신규 기사"
    )
    notion_url = summary.get("notion_page_url")

    blocks: List[Dict[str, Any]] = [
        notion_heading(f"IP Monitor 운영 대시보드 - {run_date}"),
        notion_callout("관리자용 페이지입니다. 이 parent page를 외부 공개하지 않으면 관리자만 볼 수 있습니다."),
        notion_paragraph(f"실행: {run_log.get('run_id', '-')} | 시작: {run_log.get('started_at', '-')} | 종료: {run_log.get('finished_at', '-')}"),
        notion_paragraph(f"실행 링크: {(run_log.get('github') or {}).get('run_url') or '-'}"),
        notion_divider(),
        notion_heading("기사"),
        notion_bullet(f"신규 기사: {summary.get('total_new_articles', 0)}"),
        notion_bullet(f"산식: {article_equation}"),
        notion_bullet(f"분석 시도/성공/실패: {summary.get('analysis_attempted_count', 0)} / {summary.get('analysis_success_count', 0)} / {summary.get('analysis_failed_count', 0)}"),
        notion_bullet(f"분석 사전 제외: {summary.get('analysis_prefilter_skipped_count', 0)}"),
        notion_heading("소스"),
        notion_bullet(f"전체/정상/빈값/실패: {summary.get('total_sources', 0)} / {summary.get('ok_sources', 0)} / {summary.get('empty_sources', 0)} / {summary.get('failed_sources', 0)}"),
        notion_heading("시간"),
        notion_bullet(f"총 소요: {round(float(run_log.get('duration_seconds') or 0) / 60, 1)}분"),
        notion_bullet(f"수집/분석: {round(float(summary.get('fetch_duration_seconds') or 0) / 60, 1)}분 / {round(float(summary.get('analysis_duration_seconds') or 0) / 60, 1)}분"),
        notion_heading("Claude"),
        notion_bullet(f"입력/출력 토큰: {summary.get('claude_input_tokens', '-') or '-'} / {summary.get('claude_output_tokens', '-') or '-'}"),
        notion_bullet(f"추정 비용: ${float(summary.get('claude_estimated_cost_usd') or 0):.4f}"),
        notion_heading("링크"),
        notion_bullet(f"전체 분석결과: {notion_url or '-'}"),
        notion_bullet(f"리뷰 텔레그램 원문 저장: {summary.get('raw_review_supabase_saved', False)}"),
        notion_bullet(f"Digest 텔레그램 원문 저장: {summary.get('digest_supabase_saved', False)}"),
        notion_divider(),
    ]

    blocks.append(notion_heading("빈 소스", level=3))
    if empty_sources:
        for item in empty_sources[:30]:
            blocks.append(notion_bullet(f"{item.get('region', '')} | {item.get('name', '')} | {item.get('monitor_url', '')}"))
    else:
        blocks.append(notion_paragraph("빈 소스 없음"))

    blocks.append(notion_heading("실패 소스", level=3))
    if failed_sources:
        for item in failed_sources[:30]:
            blocks.append(notion_bullet(f"{item.get('region', '')} | {item.get('name', '')} | {item.get('error', '')}"))
    else:
        blocks.append(notion_paragraph("실패 소스 없음"))

    blocks.append(notion_heading("신규 기사 많은 소스", level=3))
    for item in top_new_sources:
        blocks.append(notion_bullet(f"{item.get('new_count', 0)}건 | {item.get('region', '')} | {item.get('name', '')}"))

    if run_log.get("analysis_errors"):
        blocks.append(notion_heading("분석 오류", level=3))
        for item in run_log["analysis_errors"][:20]:
            blocks.append(notion_bullet(f"{item.get('source', '')} | {item.get('title', '')} | {item.get('error', '')}"))

    return blocks


def should_reuse_dashboard_page(dashboard_info: Dict[str, Any], parent_page_id: str) -> bool:
    page_id = dashboard_info.get("page_id")
    if not page_id:
        return False
    url = str(dashboard_info.get("url") or "")
    title = str(dashboard_info.get("title") or "")
    if "TEST-" in url or title.startswith("TEST-"):
        return False
    stored_parent = normalize_notion_page_id(dashboard_info.get("parent_page_id"))
    if stored_parent and stored_parent != parent_page_id:
        return False
    return True


def publish_notion_dashboard_page(run_log: Dict[str, Any], cfg: Dict[str, Any]) -> Optional[str]:
    if not notion_dashboard_enabled(cfg):
        return None

    notion_cfg = cfg.get("notion", {})
    title = str(notion_cfg.get("dashboard_title") or "IP Monitor 운영 대시보드")
    parent_page_id = normalize_notion_page_id(os.getenv("NOTION_DASHBOARD_PARENT_PAGE_ID"))
    pages = load_notion_pages()
    dashboard_info = pages.get("admin_dashboard") if isinstance(pages.get("admin_dashboard"), dict) else {}
    if should_reuse_dashboard_page(dashboard_info, parent_page_id):
        page_id = dashboard_info.get("page_id")
        page_url = dashboard_info.get("url")
    else:
        page_id = normalize_notion_page_id(os.getenv("NOTION_DASHBOARD_PAGE_ID"))
        page_url = None

    if not page_id:
        response = create_notion_child_page(os.getenv("NOTION_DASHBOARD_PARENT_PAGE_ID", ""), title)
        page_id = response.get("id")
        page_url = response.get("url")
    if not page_id:
        return page_url

    clear_notion_page(page_id)
    append_notion_blocks(page_id, notion_dashboard_blocks(run_log))

    pages["admin_dashboard"] = {
        "page_id": page_id,
        "url": page_url,
        "title": title,
        "parent_page_id": parent_page_id,
        "updated_at": local_timestamp(),
        "run_id": run_log.get("run_id"),
    }
    save_notion_pages(pages)
    return page_url


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


def article_stale_reason(
    article: Article,
    run_date: str,
    stale_days: int,
) -> Optional[str]:
    if stale_days <= 0:
        return None

    article_date_text = format_review_date(
        article.published,
        article.url,
        generated_at=f"{run_date} 23:59",
    )
    article_date = parse_iso_date(article_date_text)
    run_dt = parse_iso_date(run_date)
    if not article_date or not run_dt:
        return None

    age_days = (run_dt - article_date).days
    if age_days > stale_days:
        return f"published {article_date.strftime('%Y-%m-%d')} ({age_days} days old)"
    return None


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
    stale_skipped_count = sum(int(r.get("stale_skipped_count") or 0) for r in source_check_records)
    problem_sources = [
        r for r in source_check_records
        if r.get("status") in ("fail", "empty")
    ]
    missing_date_articles = [
        a for a in articles
        if format_review_date(a.published, a.url, generated_at=generated_at) == "-"
    ]
    title_attention_articles = [a for a in articles if review_title_needs_attention(a.title)]

    lines = [
        "수집 요약",
        "",
        "소스 상태",
        f"- 전체 {total_sources}개 / 성공 {ok_sources}개 / 결과 없음 {empty_sources}개 / 실패 {failed_sources}개",
        "",
        "기사 처리",
        f"- 신규 기사 {len(articles)}개",
        f"- 전체 수집 후보 {fetch_candidate_count}개",
        f"- 기존 확인 기사 제외 {seen_skipped_count}개 / 비기사 제외 {non_article_skipped_count}개 / 오래된 기사 제외 {stale_skipped_count}개",
    ]

    attention_lines = []
    if problem_sources:
        attention_lines.append(f"- 수집 실패/결과 없음 소스 {len(problem_sources)}개")
    if missing_date_articles:
        attention_lines.append(f"- 날짜 확인 필요 기사 {len(missing_date_articles)}개")
    if title_attention_articles:
        attention_lines.append(f"- 제목 확인 필요 기사 {len(title_attention_articles)}개")

    lines.extend(["", "확인 필요"])
    if attention_lines:
        lines.extend(attention_lines)
    else:
        lines.append("- 특이사항 없음")

    if problem_sources:
        lines.extend(["", "수집 실패/결과 없음 소스"])
        for record in problem_sources[:10]:
            status = record.get("status", "-")
            name = record.get("name", "-")
            error = re.sub(r"\s+", " ", record.get("error") or "").strip()
            suffix = f" - {error[:90]}" if error else ""
            lines.append(f"- [{status}] {name}{suffix}")
        if len(problem_sources) > 10:
            lines.append(f"- 외 {len(problem_sources) - 10}개")

    if missing_date_articles:
        lines.extend(["", "날짜 확인 필요 기사"])
        for art in missing_date_articles[:5]:
            title = normalize_review_title(art.title)
            if len(title) > 90:
                title = title[:87].rstrip() + "..."
            lines.append(f"- {art.source} | {title}")
        if len(missing_date_articles) > 5:
            lines.append(f"- 외 {len(missing_date_articles) - 5}개")

    if title_attention_articles:
        lines.extend(["", "제목 확인 필요 기사"])
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
    generated_at = generated_at or local_datetime().strftime("%Y-%m-%d %H:%M")
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


def save_telegram_messages_state(
    base_key: str,
    run_id: str,
    messages: List[str],
    message_type: str,
) -> bool:
    run_date = run_date_from_run_id(run_id)
    payload = {
        "run_id": run_id,
        "run_date": run_date,
        "message_type": message_type,
        "message_count": len(messages),
        "messages": messages,
        "saved_at": local_timestamp(),
    }
    saved = False
    dated_key = f"{base_key}_{run_date.replace('-', '')}"
    if save_supabase_state(dated_key, payload):
        print(f"Supabase {dated_key} 저장: {len(messages)}개")
        saved = True
    latest_key = f"{base_key}_latest"
    if save_supabase_state(latest_key, payload):
        print(f"Supabase {latest_key} 저장: {len(messages)}개")
        saved = True
    return saved


def decode_stored_telegram_message(message: str) -> str:
    # Older digest payloads were accidentally stored with a newline between
    # every character. Collapse those separator newlines while keeping real
    # paragraph breaks readable.
    if not message:
        return ""
    single_newline_ratio = message.count("\n") / max(len(message), 1)
    if single_newline_ratio < 0.35:
        return message
    return re.sub(
        r"\n+",
        lambda m: "\n" * (len(m.group(0)) // 2),
        message,
    )


def load_telegram_messages_state(base_key: str, run_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    date_text = run_date or local_run_date()
    key_date = date_text.replace("-", "")
    key = f"{base_key}_{key_date}"
    payload = load_supabase_state(key)
    if not isinstance(payload, dict):
        return None

    messages = payload.get("messages")
    if isinstance(messages, list):
        payload = dict(payload)
        payload["messages"] = [
            decode_stored_telegram_message(str(message))
            for message in messages
        ]
    return payload


def print_telegram_digest_from_state(run_date: Optional[str] = None) -> int:
    payload = load_telegram_messages_state(SUPABASE_DIGEST_MESSAGE_KEY, run_date)
    if not payload:
        date_text = run_date or local_run_date()
        print(f"저장된 digest 텔레그램을 찾지 못했습니다: {SUPABASE_DIGEST_MESSAGE_KEY}_{date_text.replace('-', '')}")
        return 1

    print(f"run_id: {payload.get('run_id', '')}")
    print(f"run_date: {payload.get('run_date', '')}")
    print(f"saved_at: {payload.get('saved_at', '')}")
    print(f"message_count: {payload.get('message_count', '')}")
    print("---")
    print("\n\n--- MESSAGE BREAK ---\n\n".join(payload.get("messages") or []))
    return 0


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
    if '--load-telegram-digest' in sys.argv:
        arg_index = sys.argv.index('--load-telegram-digest')
        requested_date = None
        if len(sys.argv) > arg_index + 1 and not sys.argv[arg_index + 1].startswith("--"):
            requested_date = sys.argv[arg_index + 1]
        sys.exit(print_telegram_digest_from_state(requested_date))

    if '--check-korean-holiday' in sys.argv:
        arg_index = sys.argv.index('--check-korean-holiday')
        date_text = local_run_date()
        if len(sys.argv) > arg_index + 1 and not sys.argv[arg_index + 1].startswith("--"):
            date_text = sys.argv[arg_index + 1]
        cfg = load_config('config.yaml')
        refresh = '--refresh-korean-holidays' in sys.argv
        holiday_name = korean_public_holiday_for_date(date_text, cfg=cfg, refresh=refresh)
        year = int(date_text[:4])
        holidays = load_korean_public_holidays(year, cfg=cfg, refresh=False)
        print(f"date: {date_text}")
        print(f"is_korean_public_holiday: {bool(holiday_name)}")
        if holiday_name:
            print(f"name: {holiday_name}")
        print(f"cached_holidays_{year}: {len(holidays)}")
        print(f"cache_path: {korean_holiday_cache_path(year)}")
        sys.exit(0)

    run_started = time.time()
    run_id = local_run_id(run_started)
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
    stale_article_days = int(cfg.get('fetch', {}).get('stale_article_days', 14) or 0)
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
        "started_at": local_timestamp(run_started),
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
            "korean_holiday_cache": korean_holiday_cache_path(int(run_date_from_run_id(run_id)[:4])),
        },
        "summary": {
            "total_sources": len(sources),
            "total_fetched_articles": 0,
            "total_new_articles": 0,
            "fetch_duration_seconds": None,
            "raw_review_telegram_duration_seconds": None,
            "analysis_duration_seconds": None,
            "digest_telegram_duration_seconds": None,
            "digest_telegram_wait_seconds": 0,
            "digest_telegram_scheduled_send_time": "07:30",
            "digest_recent_topic_days": recent_topic_days,
            "digest_recent_topic_skipped_count": 0,
            "digest_selection_skipped_count": 0,
            "digest_paid_source_skipped_count": 0,
            "seen_skipped_count": 0,
            "already_analyzed_skipped_count": 0,
            "non_article_skipped_count": 0,
            "stale_skipped_count": 0,
            "ok_sources": 0,
            "empty_sources": 0,
            "failed_sources": 0,
            "analysis_attempted_count": 0,
            "analysis_success_count": 0,
            "analysis_failed_count": 0,
            "analysis_skipped_existing_count": 0,
            "analysis_prefilter_skipped_count": 0,
            "claude_input_tokens": 0,
            "claude_output_tokens": 0,
            "claude_cache_creation_input_tokens": 0,
            "claude_cache_read_input_tokens": 0,
            "claude_estimated_cost_usd": 0.0,
            "seen_saved": False,
            "raw_articles_saved": False,
            "raw_review_digest_saved": False,
            "raw_review_telegram_messages": 0,
            "raw_review_supabase_saved": False,
            "notion_review_page_saved": False,
            "notion_review_page_url": None,
            "source_check_report_saved": False,
            "failed_sources_yaml_saved": False,
            "results_saved": False,
            "daily_results_saved": False,
            "daily_results_path": None,
            "notion_page_saved": False,
            "notion_page_url": None,
            "notion_dashboard_saved": False,
            "notion_dashboard_url": None,
            "digest_saved": False,
            "digest_supabase_saved": False,
            "digest_telegram_messages": 0,
            "telegram_send_enabled": cfg.get("telegram", {}).get("send_enabled", False),
            "telegram_review_send_enabled": cfg.get("telegram", {}).get("review_send_enabled", False),
            "telegram_digest_send_enabled": cfg.get("telegram", {}).get("digest_send_enabled", False),
            "korean_public_holiday_skip_enabled": truthy_config_value(
                (cfg.get("schedule") or {}).get("skip_korean_public_holidays"),
                default=False,
            ),
            "korean_public_holiday_skipped": False,
            "korean_public_holiday_name": None,
            "korean_public_holiday_cached_count": 0,
        },
        "sources": [],
        "analysis_errors": [],
        "analysis_prefilter_skips": [],
        "stale_article_skips": [],
        "digest_recent_topic_skips": [],
        "digest_selection_skips": [],
    }

    run_date = run_date_from_run_id(run_id)
    holiday_name = should_skip_korean_public_holiday_run(run_date, cfg)
    if holiday_name:
        holiday_year = int(run_date[:4])
        cached_holidays = load_korean_public_holidays(holiday_year, cfg=cfg, refresh=False)
        run_log["korean_public_holiday_skip"] = True
        run_log["finished_at"] = local_timestamp()
        run_log["duration_seconds"] = round(time.time() - run_started, 3)
        run_log["summary"]["korean_public_holiday_skipped"] = True
        run_log["summary"]["korean_public_holiday_name"] = holiday_name
        run_log["summary"]["korean_public_holiday_cached_count"] = len(cached_holidays)
        print(f"한국 공휴일({run_date}, {holiday_name})이라 정규 실행을 건너뜁니다.")
        print(f"공휴일 캐시: {korean_holiday_cache_path(holiday_year)} ({len(cached_holidays)}건)")
        log_path = save_run_log(run_log)
        print(f'[DEBUG] 실행 로그 저장: {log_path}')
        return

    duplicate_run = should_skip_duplicate_scheduled_run(run_date)
    if duplicate_run:
        run_log["duplicate_skip"] = True
        run_log["duplicate_of_run_id"] = duplicate_run.get("run_id")
        run_log["finished_at"] = local_timestamp()
        run_log["duration_seconds"] = round(time.time() - run_started, 3)
        run_log["summary"]["duplicate_scheduled_run_skipped"] = True
        print(
            "오늘 이미 완료된 정규 실행이 있어 중복 실행을 건너뜁니다: "
            f"{duplicate_run.get('run_id')}"
        )
        log_path = save_run_log(run_log)
        print(f'[DEBUG] 실행 로그 저장: {log_path}')
        return

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
            "stale_skipped_count": 0,
            "error": "",
            "elapsed_seconds": None,
            "sample_articles": [],
        }
        try:
            arts = fetch_articles_for_source(src, timeout=fetch_timeout)
            for art in arts:
                art.url = canonical_article_url(art.url)
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

            stale_reason = article_stale_reason(a, run_date_from_run_id(run_id), stale_article_days)
            if stale_reason:
                source_log["stale_skipped_count"] += 1
                seen.add(a.url)
                if len(run_log["stale_article_skips"]) < 100:
                    run_log["stale_article_skips"].append({
                        "source": a.source,
                        "title": a.title,
                        "url": a.url,
                        "published": a.published,
                        "reason": stale_reason,
                    })
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
                "stale_skipped_count": source_log["stale_skipped_count"],
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
    run_log["summary"]["stale_skipped_count"] = sum(int(s["stale_skipped_count"]) for s in run_log["sources"])
    run_log["summary"]["ok_sources"] = ok_count
    run_log["summary"]["empty_sources"] = empty_count
    run_log["summary"]["failed_sources"] = fail_count

    print(f'[DEBUG] 소스 점검 결과 저장: {SOURCE_CHECK_REPORT_PATH}')
    print(f'[DEBUG] 실패/빈 소스 YAML 저장: {FAILED_SOURCES_PATH}')
    print(f'[DEBUG] 소스 점검 요약: ok={ok_count}, empty={empty_count}, fail={fail_count}')
    if not new_articles:
        print('신규 기사가 없습니다.')
        run_log["finished_at"] = local_timestamp()
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
    try:
        notion_review_url = publish_notion_review_page(raw_review_messages, run_id, cfg)
        if notion_review_url:
            run_log["summary"]["notion_review_page_saved"] = True
            run_log["summary"]["notion_review_page_url"] = notion_review_url
            print(f"Notion 수집 검증 페이지 생성: {notion_review_url}")
    except Exception as e:
        run_log["notion_review_error"] = str(e)
        print(f"Notion 수집 검증 페이지 생성 실패: {e}")
    run_log["summary"]["raw_review_supabase_saved"] = save_telegram_messages_state(
        SUPABASE_RAW_REVIEW_MESSAGES_KEY,
        run_id,
        raw_review_messages,
        "raw_review",
    )
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
        run_log["finished_at"] = local_timestamp()
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
            run_log["summary"]["claude_input_tokens"] += analyzed.claude_input_tokens
            run_log["summary"]["claude_output_tokens"] += analyzed.claude_output_tokens
            run_log["summary"]["claude_cache_creation_input_tokens"] += analyzed.claude_cache_creation_input_tokens
            run_log["summary"]["claude_cache_read_input_tokens"] += analyzed.claude_cache_read_input_tokens
            run_log["summary"]["claude_estimated_cost_usd"] = round(
                float(run_log["summary"]["claude_estimated_cost_usd"])
                + analyzed.claude_estimated_cost_usd,
                6,
            )

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
        run_log["finished_at"] = local_timestamp()
        run_log["duration_seconds"] = round(time.time() - run_started, 3)
        try:
            dashboard_url = publish_notion_dashboard_page(run_log, cfg)
            if dashboard_url:
                run_log["summary"]["notion_dashboard_saved"] = True
                run_log["summary"]["notion_dashboard_url"] = dashboard_url
                print(f"Notion 관리자 대시보드 업데이트: {dashboard_url}")
        except Exception as e:
            print("Notion 관리자 대시보드 업데이트 실패:", e)
            run_log["notion_dashboard_error"] = str(e)
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
    selected_digest_clusters, digest_selection_skips = select_digest_clusters(
        analyzed_items,
        top_n=top_n,
        min_importance=min_importance,
        sent_topics=sent_digest_topics,
        recent_topic_days=recent_topic_days,
        run_date=digest_run_date,
    )
    run_log["summary"]["digest_selection_skipped_count"] = len(digest_selection_skips)
    run_log["summary"]["digest_paid_source_skipped_count"] = sum(
        1 for item in digest_selection_skips if item.get("reason") == "paid_source_limit"
    )
    run_log["digest_selection_skips"] = digest_selection_skips[:100]

    notion_page_url = None
    try:
        notion_page_url = publish_notion_analysis_page(
            analyzed_items,
            selected_digest_clusters,
            digest_run_date,
            cfg,
        )
        if notion_page_url:
            run_log["summary"]["notion_page_saved"] = True
            run_log["summary"]["notion_page_url"] = notion_page_url
            print(f"Notion 전체 분석결과 페이지 생성: {notion_page_url}")
    except Exception as e:
        print("Notion 페이지 생성 실패:", e)
        run_log["notion_error"] = str(e)

    digest_text = render_telegram_digest(
        selected_digest_clusters,
        run_date=digest_run_date,
        full_results_url=notion_page_url,
    )
    save_digest(digest_text)
    run_log["summary"]["digest_saved"] = True
    digest_messages_for_state = split_telegram_messages(digest_text.splitlines())
    run_log["summary"]["digest_supabase_saved"] = save_telegram_messages_state(
        SUPABASE_DIGEST_MESSAGE_KEY,
        run_id,
        digest_messages_for_state,
        "digest",
    )
    digest_send_time = os.getenv("DIGEST_SEND_TIME_KST", "07:30")
    run_log["summary"]["digest_telegram_scheduled_send_time"] = digest_send_time
    digest_wait_seconds = 0
    if is_scheduled_run() and cfg.get("telegram", {}).get("digest_send_enabled", False) and '--no-telegram' not in sys.argv:
        digest_wait_seconds = seconds_until_local_time(digest_send_time)
        if digest_wait_seconds > 0:
            print(f"Digest 텔레그램 발송 예정 시각({digest_send_time} KST)까지 {digest_wait_seconds}초 대기합니다.")
            time.sleep(digest_wait_seconds)
    run_log["summary"]["digest_telegram_wait_seconds"] = digest_wait_seconds

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

    run_log["finished_at"] = local_timestamp()
    run_log["duration_seconds"] = round(time.time() - run_started, 3)
    try:
        dashboard_url = publish_notion_dashboard_page(run_log, cfg)
        if dashboard_url:
            run_log["summary"]["notion_dashboard_saved"] = True
            run_log["summary"]["notion_dashboard_url"] = dashboard_url
            print(f"Notion 관리자 대시보드 업데이트: {dashboard_url}")
    except Exception as e:
        print("Notion 관리자 대시보드 업데이트 실패:", e)
        run_log["notion_dashboard_error"] = str(e)

    print('완료. results.json / telegram_digest.txt 생성(또는 갱신).')
    log_path = save_run_log(run_log)
    print(f'[DEBUG] 실행 로그 저장: {log_path}')


if __name__ == '__main__':
    main()
