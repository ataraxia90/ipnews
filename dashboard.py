import json
from pathlib import Path
from typing import Any, Dict, List, Optional


DATA_DIR = Path("data")
OUTPUT_PATH = Path("dashboard.html")


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def collect_run_logs() -> List[Dict[str, Any]]:
    paths = list(DATA_DIR.glob("run_logs/*.json"))
    paths.extend(DATA_DIR.glob("actions_*/run_logs/*.json"))
    runs: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        item = read_json(path)
        if not isinstance(item, dict):
            continue
        run_id = str(item.get("run_id") or path.stem)
        item["_path"] = str(path)
        runs[run_id] = item
    return sorted(runs.values(), key=lambda x: str(x.get("started_at", "")), reverse=True)


def run_date(run: Dict[str, Any]) -> str:
    started_at = str(run.get("started_at") or "")
    if len(started_at) >= 10:
        return started_at[:10]
    path = Path(str(run.get("_path") or ""))
    for parent in path.parents:
        if parent.name.startswith("actions_"):
            date_part = parent.name.replace("actions_", "")
            if len(date_part) == 8:
                return f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:]}"
    return ""


def action_dir_for_run(run: Dict[str, Any]) -> Optional[Path]:
    path = Path(str(run.get("_path") or ""))
    for parent in path.parents:
        if parent.name.startswith("actions_"):
            return parent
    return None


def collect_articles(action_dir: Optional[Path]) -> List[Dict[str, Any]]:
    if not action_dir:
        return []

    date_part = action_dir.name.replace("actions_", "")
    candidates = [
        action_dir / "daily_results" / f"results_{date_part}.json",
        action_dir / "results.json",
        action_dir / "raw_articles.json",
    ]
    for path in candidates:
        data = read_json(path)
        if isinstance(data, list):
            rows = []
            for item in data:
                if isinstance(item, dict):
                    copied = dict(item)
                    copied["_path"] = str(path)
                    rows.append(copied)
            return rows
    return []


def run_row(run: Dict[str, Any]) -> Dict[str, Any]:
    summary = run.get("summary") or {}
    github = run.get("github") or {}
    return {
        "date": run_date(run),
        "run_id": run.get("run_id", ""),
        "started_at": run.get("started_at", ""),
        "finished_at": run.get("finished_at", ""),
        "duration_min": round(float(run.get("duration_seconds") or 0) / 60, 1),
        "new_articles": summary.get("total_new_articles", 0),
        "fetched": summary.get("total_fetched_articles", 0),
        "seen_skipped": summary.get("seen_skipped_count", 0),
        "stale_skipped": summary.get("stale_skipped_count", 0),
        "ok_sources": summary.get("ok_sources", 0),
        "empty_sources": summary.get("empty_sources", 0),
        "failed_sources": summary.get("failed_sources", 0),
        "analysis_success": summary.get("analysis_success_count", 0),
        "analysis_failed": summary.get("analysis_failed_count", 0),
        "review_messages": summary.get("raw_review_telegram_messages", 0),
        "digest_messages": summary.get("digest_telegram_messages", 0),
        "event": github.get("event_name", ""),
        "run_url": github.get("run_url", ""),
        "path": run.get("_path", ""),
    }


def source_rows(run: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for source in run.get("sources") or []:
        rows.append({
            "status": source.get("status", ""),
            "region": source.get("region", ""),
            "source": source.get("name", ""),
            "mode": source.get("mode", ""),
            "fetched": source.get("fetched_count", source.get("count", 0)),
            "new": source.get("new_count", 0),
            "seen": source.get("seen_skipped_count", 0),
            "non_article": source.get("non_article_skipped_count", 0),
            "stale": source.get("stale_skipped_count", 0),
            "elapsed_sec": source.get("elapsed_seconds", 0),
            "error": source.get("error", ""),
            "url": source.get("monitor_url", ""),
        })
    return rows


def article_rows(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for item in items:
        rows.append({
            "score": item.get("importance_score", ""),
            "region": item.get("issue_region") or item.get("region", ""),
            "source": item.get("source", ""),
            "category": item.get("category", ""),
            "topic_key": item.get("topic_key", ""),
            "title": item.get("title", ""),
            "published": item.get("published", ""),
            "url": item.get("url", ""),
        })

    def score_value(row: Dict[str, Any]) -> int:
        value = row.get("score")
        return int(value) if str(value).isdigit() else -1

    return sorted(rows, key=score_value, reverse=True)


def pct(success: Any, total: Any) -> str:
    try:
        total = float(total)
        success = float(success)
    except Exception:
        return "-"
    if total <= 0:
        return "-"
    return f"{success / total * 100:.1f}%"


def kpis_for_run(run: Dict[str, Any]) -> Dict[str, Any]:
    summary = run.get("summary") or {}
    fetched = summary.get("total_fetched_articles", 0)
    seen_skipped = summary.get("seen_skipped_count", 0)
    non_article_skipped = summary.get("non_article_skipped_count", 0)
    stale_skipped = summary.get("stale_skipped_count", 0)
    new_articles = summary.get("total_new_articles", 0)
    return {
        "new_articles": new_articles,
        "fetched": fetched,
        "seen_skipped": seen_skipped,
        "non_article_skipped": non_article_skipped,
        "stale_skipped": stale_skipped,
        "total_sources": summary.get("total_sources", 0),
        "empty_sources": summary.get("empty_sources", 0),
        "failed_sources": summary.get("failed_sources", 0),
        "duration_min": round(float(run.get("duration_seconds") or 0) / 60, 1),
        "fetch_min": round(float(summary.get("fetch_duration_seconds") or 0) / 60, 1),
        "analysis_min": round(float(summary.get("analysis_duration_seconds") or 0) / 60, 1),
        "claude_input_tokens": summary.get("claude_input_tokens"),
        "claude_output_tokens": summary.get("claude_output_tokens"),
        "claude_estimated_cost_usd": summary.get("claude_estimated_cost_usd"),
        "article_equation": {
            "fetched": fetched,
            "seen_skipped": seen_skipped,
            "non_article_skipped": non_article_skipped,
            "stale_skipped": stale_skipped,
            "new_articles": new_articles,
            "calculated_new_articles": fetched - seen_skipped - stale_skipped - non_article_skipped,
        },
    }


def dataset_for_run(run: Dict[str, Any]) -> Dict[str, Any]:
    action_dir = action_dir_for_run(run)
    sources = source_rows(run)
    articles = article_rows(collect_articles(action_dir))
    return {
        "date": run_date(run),
        "run": run_row(run),
        "kpis": kpis_for_run(run),
        "sources": sources,
        "articles": articles,
        "alerts": {
            "problem_sources": [
                item for item in sources
                if item.get("status") in ("fail", "empty")
            ],
            "missing_dates": [
                item for item in articles
                if not str(item.get("published") or "").strip()
            ],
        },
        "article_snapshot_available": bool(action_dir and articles),
    }


def dashboard_data() -> Dict[str, Any]:
    runs = collect_run_logs()
    datasets = [dataset_for_run(run) for run in runs]
    dates = sorted({item["date"] for item in datasets if item["date"]}, reverse=True)
    return {
        "generated_from": str(DATA_DIR.resolve()),
        "generated_at_note": "dashboard.py 실행 시점의 로컬 data 폴더 기준",
        "dates": dates,
        "datasets": datasets,
        "runs": [item["run"] for item in datasets],
        "latest_run": datasets[0]["run"] if datasets else {},
    }


HTML = """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IP Monitor Dashboard</title>
  <style>
    :root {
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --line: #d9dee7;
      --accent: #0f766e;
      --warn: #b45309;
      --bad: #b91c1c;
      --good: #047857;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Malgun Gothic", Arial, sans-serif;
      letter-spacing: 0;
    }
    header {
      background: #18212f;
      color: white;
      padding: 20px 28px;
    }
    h1 { margin: 0 0 6px; font-size: 24px; font-weight: 700; }
    h2 { margin: 24px 0 12px; font-size: 18px; }
    h3 { margin: 18px 0 10px; font-size: 15px; }
    .sub { color: #cbd5e1; font-size: 13px; }
    main { padding: 22px 28px 40px; }
    .kpis {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 10px;
    }
    .kpi-group {
      margin-top: 16px;
    }
    .kpi-group h2 {
      margin: 0 0 10px;
      font-size: 15px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { font-size: 24px; font-weight: 750; margin-top: 5px; }
    .metric[role="button"] { cursor: pointer; }
    .metric[role="button"]:hover {
      border-color: var(--accent);
      box-shadow: 0 1px 6px rgba(15, 118, 110, 0.16);
    }
    .metric[role="button"]:focus-visible {
      outline: 2px solid var(--accent);
      outline-offset: 2px;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin: 16px 0 12px;
      align-items: center;
    }
    .toolbar label {
      color: var(--muted);
      font-size: 13px;
    }
    input, select, button {
      border: 1px solid var(--line);
      background: white;
      border-radius: 6px;
      min-height: 36px;
      padding: 7px 10px;
      font: inherit;
    }
    button {
      cursor: pointer;
      color: var(--text);
    }
    button.active {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }
    section { display: none; }
    section.active { display: block; }
    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      font-size: 13px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 8px 10px;
      text-align: left;
      vertical-align: top;
    }
    th {
      background: #eef2f7;
      font-weight: 700;
      position: sticky;
      top: 0;
    }
    tr:hover td { background: #f8fafc; }
    a { color: #075985; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .table-wrap {
      max-height: 620px;
      overflow: auto;
      border-radius: 8px;
    }
    .pill {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      background: #e5e7eb;
      font-size: 12px;
    }
    .pill.ok { background: #d1fae5; color: var(--good); }
    .pill.empty { background: #fef3c7; color: var(--warn); }
    .pill.fail { background: #fee2e2; color: var(--bad); }
    .empty-state {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      color: var(--muted);
    }
    .note {
      color: var(--muted);
      font-size: 13px;
      margin: 8px 0 0;
    }
    @media (max-width: 720px) {
      header, main { padding-left: 16px; padding-right: 16px; }
      .toolbar > * { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <h1>IP Monitor 운영 대시보드</h1>
    <div class="sub" id="subtitle"></div>
  </header>
  <main>
    <div class="toolbar">
      <label for="dateSelect">기준일</label>
      <select id="dateSelect"></select>
      <label for="runSelect">실행</label>
      <select id="runSelect"></select>
      <label for="costPeriod">비용 누적</label>
      <select id="costPeriod">
        <option value="week">주</option>
        <option value="month">월</option>
        <option value="year">년</option>
      </select>
    </div>
    <div class="note" id="selectionNote"></div>

    <div id="kpis"></div>
    <div class="toolbar" id="tabs">
      <button class="active" data-tab="runs">실행 이력</button>
      <button data-tab="sources">소스 상태</button>
      <button data-tab="articles">기사/분석 결과</button>
      <button data-tab="alerts">확인 필요</button>
    </div>

    <section id="runs" class="active">
      <h2>실행 이력</h2>
      <div class="table-wrap" id="runsTable"></div>
    </section>

    <section id="sources">
      <h2>소스 상태</h2>
      <div class="toolbar">
        <input id="sourceSearch" placeholder="소스명, 지역, 상태 검색">
        <select id="sourceStatus"><option value="">전체 상태</option></select>
      </div>
      <div class="table-wrap" id="sourcesTable"></div>
    </section>

    <section id="articles">
      <h2>기사/분석 결과</h2>
      <div class="toolbar">
        <input id="articleSearch" placeholder="제목, 소스, topic 검색">
        <select id="articleRegion"><option value="">전체 지역</option></select>
      </div>
      <div class="note" id="articleNote"></div>
      <div class="table-wrap" id="articlesTable"></div>
    </section>

    <section id="alerts">
      <h2>확인 필요</h2>
      <h3>수집 실패/빈값 소스</h3>
      <div class="table-wrap" id="problemSourcesTable"></div>
      <h3>날짜 누락 기사</h3>
      <div class="table-wrap" id="missingDatesTable"></div>
    </section>
  </main>

  <script>
    const DATA = __DATA__;
    let current = DATA.datasets[0] || {run: {}, kpis: {}, sources: [], articles: [], alerts: {}};

    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, ch => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    }[ch]));

    function linkCell(url, label) {
      if (!url) return "";
      return `<a href="${esc(url)}" target="_blank" rel="noreferrer">${esc(label || "열기")}</a>`;
    }

    function statusPill(status) {
      const cls = status === "ok" ? "ok" : status === "empty" ? "empty" : status === "fail" ? "fail" : "";
      return `<span class="pill ${cls}">${esc(status)}</span>`;
    }

    function renderTable(id, rows, columns) {
      const el = document.getElementById(id);
      if (!rows || rows.length === 0) {
        el.innerHTML = `<div class="empty-state">표시할 데이터가 없습니다.</div>`;
        return;
      }
      const head = columns.map(col => `<th>${esc(col.label)}</th>`).join("");
      const body = rows.map(row => `<tr>${columns.map(col => {
        const raw = row[col.key];
        const value = col.render ? col.render(raw, row) : esc(raw);
        return `<td>${value}</td>`;
      }).join("")}</tr>`).join("");
      el.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function parseDashboardDate(value) {
      if (!value) return null;
      const parts = String(value).slice(0, 10).split("-").map(Number);
      if (parts.length !== 3 || parts.some(Number.isNaN)) return null;
      return new Date(parts[0], parts[1] - 1, parts[2]);
    }

    function periodLabel(period) {
      if (period === "year") return "년";
      if (period === "month") return "월";
      return "주";
    }

    function samePeriod(dateText, anchorText, period) {
      const date = parseDashboardDate(dateText);
      const anchor = parseDashboardDate(anchorText);
      if (!date || !anchor) return false;

      if (period === "year") {
        return date.getFullYear() === anchor.getFullYear();
      }
      if (period === "month") {
        return date.getFullYear() === anchor.getFullYear() && date.getMonth() === anchor.getMonth();
      }

      const start = new Date(anchor);
      const day = start.getDay() || 7;
      start.setDate(start.getDate() - day + 1);
      start.setHours(0, 0, 0, 0);
      const end = new Date(start);
      end.setDate(start.getDate() + 7);
      return date >= start && date < end;
    }

    function cumulativeClaudeCost(period) {
      let total = 0;
      let hasCost = false;
      for (const item of DATA.datasets || []) {
        if (!samePeriod(item.date, current.date, period)) continue;
        const value = item.kpis ? item.kpis.claude_estimated_cost_usd : null;
        if (value === null || value === undefined || Number.isNaN(Number(value))) continue;
        total += Number(value);
        hasCost = true;
      }
      return hasCost ? total : null;
    }

    function renderKpis() {
      const k = current.kpis || {};
      const e = k.article_equation || {};
      const formula = e.calculated_new_articles === e.new_articles ? "= A-B-D-C" : "산식 확인 필요";
      const tokenLabel = (v) => v === null || v === undefined ? "-" : Number(v).toLocaleString();
      const costLabel = (v) => v === null || v === undefined ? "-" : `$${Number(v).toFixed(4)}`;
      const costPeriod = document.getElementById("costPeriod").value || "week";
      const cumulativeCost = cumulativeClaudeCost(costPeriod);
      const groups = [
        {
          title: "기사",
          items: [
            [`신규 기사 (${formula})`, k.new_articles],
            ["Fetch 후보 (A)", k.fetched],
            ["Seen 제외 (B)", k.seen_skipped],
            ["오래된 기사 제외 (D)", k.stale_skipped],
            ["비기사 제외 (C)", k.non_article_skipped],
          ],
        },
        {
          title: "소스",
          items: [
            ["전체 소스", k.total_sources, "allSources"],
            ["빈 소스", k.empty_sources, "emptySources"],
            ["실패 소스", k.failed_sources, "failedSources"],
          ],
        },
        {
          title: "시간",
          items: [
            ["총 소요", `${k.duration_min ?? 0}분`],
            ["수집", `${k.fetch_min ?? 0}분`],
            ["분석", `${k.analysis_min ?? 0}분`],
          ],
        },
        {
          title: "Claude",
          items: [
            ["입력 토큰", tokenLabel(k.claude_input_tokens)],
            ["출력 토큰", tokenLabel(k.claude_output_tokens)],
            ["추정 비용", costLabel(k.claude_estimated_cost_usd)],
            [`추정 비용(누적/${periodLabel(costPeriod)})`, costLabel(cumulativeCost)],
          ],
        },
      ];
      document.getElementById("kpis").innerHTML = groups.map(group =>
        `<div class="kpi-group"><h2>${esc(group.title)}</h2><div class="kpis">` +
        group.items.map(([label, value, action]) =>
          `<div class="metric" ${action ? `role="button" tabindex="0" data-action="${esc(action)}"` : ""}>` +
          `<div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`
        ).join("") +
        `</div></div>`
      ).join("");
    }

    function activateTab(tab) {
      document.querySelectorAll("#tabs button").forEach(x => x.classList.remove("active"));
      document.querySelectorAll("section").forEach(x => x.classList.remove("active"));
      const button = document.querySelector(`#tabs button[data-tab="${tab}"]`);
      if (button) button.classList.add("active");
      document.getElementById(tab).classList.add("active");
    }

    function showSources(status) {
      activateTab("sources");
      document.getElementById("sourceSearch").value = "";
      document.getElementById("sourceStatus").value = status || "";
      renderSources();
    }

    function runKpiAction(action) {
      if (action === "allSources") showSources("");
      if (action === "emptySources") showSources("empty");
      if (action === "failedSources") showSources("fail");
    }

    function fillSelect(el, values, selected) {
      el.innerHTML = values.map(item => {
        const value = typeof item === "string" ? item : item.value;
        const label = typeof item === "string" ? item : item.label;
        const isSelected = value === selected ? " selected" : "";
        return `<option value="${esc(value)}"${isSelected}>${esc(label)}</option>`;
      }).join("");
    }

    function populateDateControls() {
      const dates = DATA.dates || [];
      fillSelect(document.getElementById("dateSelect"), dates, current.date);
      populateRunSelect(current.date);
    }

    function populateRunSelect(date) {
      const runs = DATA.datasets.filter(item => item.date === date);
      const options = runs.map(item => ({
        value: item.run.run_id,
        label: `${item.run.started_at || item.run.run_id} · 신규 ${item.run.new_articles} · Fetch ${item.run.fetched}`
      }));
      fillSelect(document.getElementById("runSelect"), options, current.run.run_id);
    }

    function resetFilters() {
      document.getElementById("sourceSearch").value = "";
      document.getElementById("sourceStatus").innerHTML = `<option value="">전체 상태</option>`;
      const statuses = [...new Set(["ok", "empty", "fail"].concat((current.sources || []).map(x => x.status).filter(Boolean)))].sort();
      document.getElementById("sourceStatus").innerHTML += statuses.map(x => `<option>${esc(x)}</option>`).join("");

      document.getElementById("articleSearch").value = "";
      document.getElementById("articleRegion").innerHTML = `<option value="">전체 지역</option>`;
      const regions = [...new Set((current.articles || []).map(x => x.region).filter(Boolean))].sort();
      document.getElementById("articleRegion").innerHTML += regions.map(x => `<option>${esc(x)}</option>`).join("");
    }

    function renderSelectionText() {
      const run = current.run || {};
      const snapshotText = current.article_snapshot_available
        ? `기사 스냅샷 ${current.articles.length}건`
        : "이 실행의 기사 스냅샷 없음";
      document.getElementById("subtitle").textContent =
        `선택 실행: ${run.started_at || "-"} · 데이터: ${DATA.generated_from}`;
      document.getElementById("selectionNote").textContent =
        `기준일 ${current.date || "-"} / 실행 ${run.run_id || "-"} / ${snapshotText}`;
      document.getElementById("articleNote").textContent =
        current.article_snapshot_available ? "" : "이 과거 실행은 run log만 있고 기사 결과 파일은 보관되어 있지 않아 기사 테이블은 비어 있습니다.";
    }

    function renderRuns() {
      renderTable("runsTable", DATA.runs, [
        {key: "date", label: "기준일"},
        {key: "run_id", label: "Run"},
        {key: "started_at", label: "시작"},
        {key: "finished_at", label: "종료"},
        {key: "duration_min", label: "분"},
        {key: "new_articles", label: "신규"},
        {key: "fetched", label: "Fetch"},
        {key: "empty_sources", label: "빈값"},
        {key: "failed_sources", label: "실패"},
        {key: "analysis_success", label: "분석 성공"},
        {key: "review_messages", label: "Review"},
        {key: "digest_messages", label: "Digest"},
        {key: "run_url", label: "Actions", render: (v) => linkCell(v, "열기")},
      ]);
    }

    function renderSources() {
      const q = document.getElementById("sourceSearch").value.toLowerCase();
      const status = document.getElementById("sourceStatus").value;
      const rows = (current.sources || []).filter(row => {
        const text = `${row.status} ${row.region} ${row.source} ${row.mode} ${row.error}`.toLowerCase();
        return (!q || text.includes(q)) && (!status || row.status === status);
      });
      renderTable("sourcesTable", rows, [
        {key: "status", label: "상태", render: statusPill},
        {key: "region", label: "지역"},
        {key: "source", label: "소스"},
        {key: "mode", label: "모드"},
        {key: "fetched", label: "Fetch"},
        {key: "new", label: "신규"},
        {key: "seen", label: "Seen"},
        {key: "stale", label: "오래됨"},
        {key: "non_article", label: "비기사"},
        {key: "elapsed_sec", label: "초"},
        {key: "error", label: "오류"},
        {key: "url", label: "URL", render: (v) => linkCell(v, "열기")},
      ]);
    }

    function renderArticles() {
      const q = document.getElementById("articleSearch").value.toLowerCase();
      const region = document.getElementById("articleRegion").value;
      const rows = (current.articles || []).filter(row => {
        const text = `${row.title} ${row.source} ${row.topic_key} ${row.category}`.toLowerCase();
        return (!q || text.includes(q)) && (!region || row.region === region);
      });
      renderTable("articlesTable", rows, [
        {key: "score", label: "점수"},
        {key: "region", label: "지역"},
        {key: "source", label: "소스"},
        {key: "category", label: "분류"},
        {key: "topic_key", label: "Topic"},
        {key: "title", label: "제목"},
        {key: "published", label: "날짜"},
        {key: "url", label: "URL", render: (v) => linkCell(v, "열기")},
      ]);
    }

    function renderAlerts() {
      renderTable("problemSourcesTable", current.alerts.problem_sources || [], [
        {key: "status", label: "상태", render: statusPill},
        {key: "region", label: "지역"},
        {key: "source", label: "소스"},
        {key: "fetched", label: "Fetch"},
        {key: "new", label: "신규"},
        {key: "elapsed_sec", label: "초"},
        {key: "error", label: "오류"},
        {key: "url", label: "URL", render: (v) => linkCell(v, "열기")},
      ]);
      renderTable("missingDatesTable", current.alerts.missing_dates || [], [
        {key: "score", label: "점수"},
        {key: "region", label: "지역"},
        {key: "source", label: "소스"},
        {key: "title", label: "제목"},
        {key: "url", label: "URL", render: (v) => linkCell(v, "열기")},
      ]);
    }

    function renderCurrent() {
      populateRunSelect(current.date);
      resetFilters();
      renderSelectionText();
      renderKpis();
      renderSources();
      renderArticles();
      renderAlerts();
    }

    document.getElementById("dateSelect").addEventListener("change", event => {
      const runs = DATA.datasets.filter(item => item.date === event.target.value);
      current = runs[0] || current;
      renderCurrent();
    });

    document.getElementById("runSelect").addEventListener("change", event => {
      current = DATA.datasets.find(item => item.run.run_id === event.target.value) || current;
      renderCurrent();
    });

    document.getElementById("costPeriod").addEventListener("change", renderKpis);

    document.getElementById("tabs").addEventListener("click", event => {
      const button = event.target.closest("button[data-tab]");
      if (!button) return;
      activateTab(button.dataset.tab);
    });

    document.getElementById("kpis").addEventListener("click", event => {
      const metric = event.target.closest("[data-action]");
      if (!metric) return;
      runKpiAction(metric.dataset.action);
    });

    document.getElementById("kpis").addEventListener("keydown", event => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const metric = event.target.closest("[data-action]");
      if (!metric) return;
      event.preventDefault();
      runKpiAction(metric.dataset.action);
    });

    document.getElementById("sourceSearch").addEventListener("input", renderSources);
    document.getElementById("sourceStatus").addEventListener("change", renderSources);
    document.getElementById("articleSearch").addEventListener("input", renderArticles);
    document.getElementById("articleRegion").addEventListener("change", renderArticles);

    populateDateControls();
    resetFilters();
    renderSelectionText();
    renderKpis();
    renderRuns();
    renderSources();
    renderArticles();
    renderAlerts();
  </script>
</body>
</html>
"""


def main() -> None:
    data = dashboard_data()
    html = HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"dashboard written: {OUTPUT_PATH.resolve()}")


if __name__ == "__main__":
    main()
