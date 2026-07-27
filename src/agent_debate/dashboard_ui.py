"""Dependency-free local dashboard UI."""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent Debate Archive</title>
  <style>
    :root {
      --ink: #17332f;
      --muted: #6c7973;
      --paper: #f3efe5;
      --card: #fffdf7;
      --rail: #123d38;
      --rail-2: #0c2d2a;
      --line: #d7d1c4;
      --accent: #e85d3f;
      --accent-soft: #f9d6ca;
      --gold: #d7a933;
      --ok: #2d806b;
      --warn: #b75c2c;
      --bad: #a63e3e;
      --shadow: 0 18px 50px rgba(29, 51, 45, .11);
    }
    * { box-sizing: border-box; }
    html, body { margin: 0; min-height: 100%; background: var(--paper); color: var(--ink); }
    body {
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background-image:
        radial-gradient(circle at 8% 8%, rgba(232,93,63,.12), transparent 24rem),
        linear-gradient(rgba(23,51,47,.035) 1px, transparent 1px),
        linear-gradient(90deg, rgba(23,51,47,.035) 1px, transparent 1px);
      background-size: auto, 28px 28px, 28px 28px;
    }
    button, input { font: inherit; }
    .app { min-height: 100vh; display: grid; grid-template-columns: 360px minmax(0, 1fr); }
    .rail {
      position: sticky; top: 0; height: 100vh; overflow: hidden;
      background: linear-gradient(155deg, var(--rail), var(--rail-2));
      color: #eef7f1; display: flex; flex-direction: column;
      border-right: 1px solid rgba(255,255,255,.12);
    }
    .brand { padding: 28px 26px 20px; border-bottom: 1px solid rgba(255,255,255,.12); }
    .eyebrow { color: #f0b9a8; text-transform: uppercase; letter-spacing: .18em; font-size: 11px; font-weight: 800; }
    .brand h1 { margin: 7px 0 5px; font: 700 27px/1.05 "Iowan Old Style", "Palatino Linotype", serif; }
    .brand p { margin: 0; color: #afc8c1; font-size: 13px; }
    .search { padding: 16px 18px 12px; display: grid; gap: 10px; }
    .search input {
      width: 100%; padding: 11px 13px; border: 1px solid rgba(255,255,255,.18);
      border-radius: 9px; background: rgba(255,255,255,.08); color: white; outline: none;
    }
    .search input:focus { border-color: #f0b9a8; box-shadow: 0 0 0 3px rgba(232,93,63,.16); }
    .filter-row { display: flex; gap: 6px; flex-wrap: wrap; }
    .filter {
      border: 1px solid rgba(255,255,255,.18); color: #cce0d9; background: transparent;
      border-radius: 999px; padding: 5px 9px; cursor: pointer; font-size: 11px;
    }
    .filter.active { color: #18332f; background: #f6d9ce; border-color: #f6d9ce; }
    .history { padding: 4px 12px 24px; overflow: auto; display: grid; gap: 8px; }
    .run-card {
      text-align: left; color: inherit; border: 1px solid transparent; cursor: pointer;
      border-radius: 12px; padding: 13px 14px; background: rgba(255,255,255,.055);
      transition: transform .16s ease, background .16s ease, border-color .16s ease;
    }
    .run-card:hover { transform: translateX(3px); background: rgba(255,255,255,.09); }
    .run-card.active { background: #f8f3e8; color: var(--ink); border-color: #fff; }
    .run-title { font-weight: 750; font-size: 14px; line-height: 1.35; margin: 7px 0 6px; }
    .run-meta { color: #a9c3bb; font-size: 11px; display: flex; justify-content: space-between; gap: 8px; }
    .run-card.active .run-meta { color: var(--muted); }
    .status {
      display: inline-flex; align-items: center; gap: 5px; border-radius: 999px;
      padding: 3px 7px; font-size: 10px; font-weight: 850; text-transform: uppercase; letter-spacing: .06em;
      background: rgba(255,255,255,.12);
    }
    .status::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
    .status.finalized { color: #69d1b3; }
    .run-card.active .status.finalized { color: var(--ok); background: #dff1e9; }
    .status.failed, .status.blocked { color: #ff9b89; }
    .status.exhausted, .status.timed_out { color: #f5cb6d; }
    main { min-width: 0; padding: 28px clamp(20px, 4vw, 62px) 70px; }
    .topline { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }
    .title-wrap h2 {
      font: 700 clamp(30px, 4vw, 56px)/.98 "Iowan Old Style", "Palatino Linotype", serif;
      letter-spacing: -.035em; margin: 8px 0 13px; max-width: 980px;
    }
    .run-id { color: var(--muted); font: 12px/1.4 "SFMono-Regular", Consolas, monospace; word-break: break-all; }
    .menu { display: none; }
    .metrics {
      display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 10px; margin: 24px 0;
    }
    .metric { background: rgba(255,253,247,.72); border: 1px solid var(--line); border-radius: 13px; padding: 14px 16px; }
    .metric strong { display: block; font: 750 24px/1 "Iowan Old Style", serif; }
    .metric span { display: block; margin-top: 6px; color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: .09em; }
    .tabs { display: flex; gap: 5px; border-bottom: 1px solid var(--line); margin: 4px 0 22px; overflow: auto; }
    .tab {
      border: 0; border-bottom: 3px solid transparent; background: transparent; cursor: pointer;
      color: var(--muted); padding: 11px 13px 10px; font-weight: 750; white-space: nowrap;
    }
    .tab.active { color: var(--ink); border-bottom-color: var(--accent); }
    .panel { display: none; animation: rise .25s ease both; }
    .panel.active { display: block; }
    @keyframes rise { from { opacity: 0; transform: translateY(5px); } }
    .grid { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(280px, .7fr); gap: 16px; }
    .card {
      background: var(--card); border: 1px solid var(--line); border-radius: 16px;
      box-shadow: var(--shadow); padding: clamp(18px, 3vw, 30px);
    }
    .card + .card { margin-top: 16px; }
    .card h3 { margin: 0 0 14px; font: 700 22px/1.15 "Iowan Old Style", serif; }
    .decision { border-top: 5px solid var(--accent); }
    .decision .verdict { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; }
    .confidence { margin-left: auto; color: var(--muted); font-size: 13px; }
    .prose { color: #29453f; font: 16px/1.72 "Avenir Next", sans-serif; overflow-wrap: anywhere; }
    .prose h1, .prose h2, .prose h3 { font-family: "Iowan Old Style", serif; line-height: 1.15; color: var(--ink); }
    .prose h2 { margin-top: 28px; }
    .prose pre, .content-block {
      white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; color: #25433d;
      font: 13px/1.62 "SFMono-Regular", Consolas, monospace;
      background: #f2eee4; border: 1px solid #ddd5c7; border-radius: 10px; padding: 15px;
      max-height: 640px; overflow: auto;
    }
    .prose code { font-family: "SFMono-Regular", Consolas, monospace; background: #eee7d9; padding: 2px 5px; border-radius: 4px; }
    .list { display: grid; gap: 8px; }
    .list-item { padding: 10px 12px; border-left: 3px solid var(--gold); background: #faf6ec; border-radius: 0 8px 8px 0; line-height: 1.45; }
    .issue.major { border-left-color: var(--bad); }
    .issue.minor { border-left-color: var(--gold); }
    .roles { display: grid; gap: 9px; }
    .role { display: grid; grid-template-columns: 1fr auto; gap: 6px 10px; border-bottom: 1px solid var(--line); padding: 9px 0; }
    .role:last-child { border: 0; }
    .role strong { text-transform: capitalize; }
    .role small { color: var(--muted); grid-column: 1 / -1; font-family: "SFMono-Regular", monospace; }
    .provider { color: var(--accent); font-weight: 800; text-transform: uppercase; font-size: 11px; }
    .round { margin-bottom: 24px; }
    .round-head { display: flex; align-items: center; gap: 12px; margin: 0 0 12px; }
    .round-no {
      width: 42px; height: 42px; display: grid; place-items: center; border-radius: 50%;
      background: var(--ink); color: white; font: 750 18px "Iowan Old Style", serif;
    }
    .invocations { display: grid; gap: 10px; }
    details.invocation { background: var(--card); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
    details.invocation[open] { box-shadow: var(--shadow); }
    details.invocation > summary { list-style: none; cursor: pointer; padding: 15px 17px; display: flex; align-items: center; gap: 10px; }
    details.invocation > summary::-webkit-details-marker { display: none; }
    .role-mark { width: 9px; height: 32px; border-radius: 6px; background: var(--accent); }
    .inv-title { font-weight: 800; text-transform: capitalize; }
    .inv-sub { color: var(--muted); font-size: 12px; margin-left: auto; text-align: right; }
    .fresh { color: var(--ok); font-size: 11px; font-weight: 800; }
    .inv-body { border-top: 1px solid var(--line); padding: 16px; display: grid; gap: 10px; }
    .content-detail { border: 1px solid var(--line); border-radius: 9px; }
    .content-detail summary { cursor: pointer; padding: 10px 12px; font-weight: 750; color: var(--ink); }
    .content-detail > div { padding: 0 10px 10px; }
    .judge { margin: 12px 0 0 52px; border-left: 4px solid var(--gold); }
    .empty { min-height: 70vh; display: grid; place-items: center; text-align: center; color: var(--muted); }
    .empty strong { display: block; color: var(--ink); font: 700 32px "Iowan Old Style", serif; margin-bottom: 8px; }
    .loading { opacity: .55; pointer-events: none; }
    @media (max-width: 980px) {
      .app { grid-template-columns: 300px minmax(0, 1fr); }
      .grid { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
    }
    @media (max-width: 720px) {
      .app { display: block; }
      .rail { position: fixed; z-index: 20; width: min(88vw, 360px); transform: translateX(-102%); transition: transform .2s ease; }
      .rail.open { transform: translateX(0); box-shadow: 20px 0 60px rgba(0,0,0,.3); }
      main { padding: 18px 16px 50px; }
      .menu { display: block; border: 1px solid var(--line); border-radius: 9px; padding: 8px 11px; background: var(--card); cursor: pointer; }
      .title-wrap h2 { font-size: 34px; }
      .metrics { grid-template-columns: repeat(2, 1fr); }
      .inv-sub { display: none; }
      .judge { margin-left: 0; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="rail" id="rail">
      <div class="brand">
        <div class="eyebrow">Local evidence system</div>
        <h1>Debate Archive</h1>
        <p id="historyCount">Loading history...</p>
      </div>
      <div class="search">
        <input id="search" type="search" placeholder="搜索问题、状态或模型">
        <div class="filter-row" id="filters"></div>
      </div>
      <div class="history" id="history"></div>
    </aside>
    <main id="main">
      <div class="empty"><div><strong>选择一次辩论</strong>从左侧历史记录进入完整证据链。</div></div>
    </main>
  </div>
  <script>
    const state = { runs: [], selected: null, filter: "all", query: "", tab: "overview" };
    const $ = (s) => document.querySelector(s);
    const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    const date = (v) => v ? new Intl.DateTimeFormat("zh-CN", {dateStyle:"medium", timeStyle:"short"}).format(new Date(v)) : "未知时间";
    const duration = (v) => Number.isFinite(v) ? (v < 60 ? `${v.toFixed(1)} 秒` : `${Math.floor(v/60)} 分 ${Math.round(v%60)} 秒`) : "—";
    const markdown = (value) => {
      const lines = esc(value).split("\n");
      let inCode = false, list = false, out = [];
      for (const line of lines) {
        if (line.startsWith("```")) {
          if (list) { out.push("</ul>"); list = false; }
          out.push(inCode ? "</code></pre>" : "<pre><code>");
          inCode = !inCode; continue;
        }
        if (inCode) { out.push(line + "\n"); continue; }
        const h = line.match(/^(#{1,3})\s+(.+)$/);
        if (h) {
          if (list) { out.push("</ul>"); list = false; }
          const n = h[1].length;
          out.push(`<h${n}>${h[2]}</h${n}>`); continue;
        }
        const li = line.match(/^\s*[-*]\s+(.+)$/);
        if (li) {
          if (!list) { out.push("<ul>"); list = true; }
          out.push(`<li>${li[1]}</li>`); continue;
        }
        if (list) { out.push("</ul>"); list = false; }
        if (!line.trim()) out.push("");
        else out.push(`<p>${line.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")}</p>`);
      }
      if (list) out.push("</ul>");
      if (inCode) out.push("</code></pre>");
      return out.join("\n");
    };
    const status = (s) => `<span class="status ${esc(s)}">${esc(s || "unknown")}</span>`;

    async function loadHistory() {
      const response = await fetch("/api/runs");
      const data = await response.json();
      state.runs = data.runs || [];
      $("#historyCount").textContent = `${state.runs.length} 次历史运行`;
      renderFilters();
      renderHistory();
      const requested = new URLSearchParams(location.search).get("run");
      const initial = state.runs.find(run => run.key === requested) || state.runs[0];
      if (initial) selectRun(initial.key);
    }

    function renderFilters() {
      const values = ["all", ...new Set(state.runs.map(r => r.status).filter(Boolean))];
      $("#filters").innerHTML = values.map(v =>
        `<button class="filter ${state.filter === v ? "active" : ""}" data-filter="${esc(v)}">${v === "all" ? "全部" : esc(v)}</button>`
      ).join("");
      document.querySelectorAll("[data-filter]").forEach(btn => btn.onclick = () => {
        state.filter = btn.dataset.filter; renderFilters(); renderHistory();
      });
    }

    function renderHistory() {
      const q = state.query.toLowerCase();
      const runs = state.runs.filter(r => {
        const matchesStatus = state.filter === "all" || r.status === state.filter;
        const haystack = [r.title, r.status, r.request_preview, ...(r.providers || [])].join(" ").toLowerCase();
        return matchesStatus && haystack.includes(q);
      });
      $("#history").innerHTML = runs.map(r => `
        <button class="run-card ${state.selected === r.key ? "active" : ""}" data-run="${esc(r.key)}">
          ${status(r.status)}
          <div class="run-title">${esc(r.title)}</div>
          <div class="run-meta"><span>${date(r.started_at)}</span><span>${r.round_count || 0}R · ${r.invocation_count || 0} calls</span></div>
        </button>`).join("") || `<div style="padding:18px;color:#9db7b0">没有匹配记录</div>`;
      document.querySelectorAll("[data-run]").forEach(btn => btn.onclick = () => selectRun(btn.dataset.run));
    }

    async function selectRun(key) {
      state.selected = key; state.tab = "overview"; renderHistory();
      history.replaceState(null, "", `/?run=${encodeURIComponent(key)}`);
      $("#main").classList.add("loading");
      const response = await fetch(`/api/runs/${encodeURIComponent(key)}`);
      const run = await response.json();
      renderRun(run);
      $("#main").classList.remove("loading");
      $("#rail").classList.remove("open");
    }

    function metric(value, label) {
      return `<div class="metric"><strong>${esc(value)}</strong><span>${esc(label)}</span></div>`;
    }
    function items(values, cls="") {
      return `<div class="list">${(values || []).map(v => {
        const text = typeof v === "string" ? v : v.summary;
        const extra = typeof v === "object" ? ` ${esc(v.severity || "")}` : "";
        return `<div class="list-item ${cls}${extra}">${esc(text)}</div>`;
      }).join("") || `<div style="color:var(--muted)">暂无</div>`}</div>`;
    }
    function roles(values) {
      return `<div class="roles">${(values || []).map(r => `
        <div class="role"><strong>${esc(r.role_id)}</strong><span class="provider">${esc(r.adapter)}</span>
        <small>${esc(r.agent_id)} · ${esc(r.model || "provider default")}</small></div>`).join("")}</div>`;
    }
    function invocationCard(inv) {
      const content = inv.content || {}, session = inv.session || {};
      const block = (title, value, open=false) => `
        <details class="content-detail" ${open ? "open" : ""}><summary>${title}</summary>
        <div><pre class="content-block">${esc(value || "（空）")}</pre></div></details>`;
      return `<details class="invocation">
        <summary><span class="role-mark"></span><span><span class="inv-title">${esc(inv.role_id)}</span><br><span class="fresh">${esc(session.mode)}</span></span>
        <span class="inv-sub">${esc(inv.adapter)} · ${esc(inv.model || "default")}<br>${duration(inv.timing?.duration_seconds)}</span></summary>
        <div class="inv-body">
          <div class="run-id">${esc(inv.invocation_id)} · ${esc(inv.stage)} · attempt ${esc(inv.attempt)}</div>
          ${block("最终输出", content.output, true)}
          ${block("精确输入", content.input)}
          ${block("Raw stdout", content.stdout)}
          ${block("Raw stderr", content.stderr)}
        </div></details>`;
    }
    function rounds(values) {
      return (values || []).map(round => {
        const judge = round.judge?.decision || {};
        return `<section class="round"><div class="round-head"><div class="round-no">${round.number}</div>
          <div><strong>Round ${round.number}</strong><div style="color:var(--muted);font-size:12px">${round.invocations.length} 次调用</div></div></div>
          <div class="invocations">${round.invocations.map(invocationCard).join("")}</div>
          ${round.judge ? `<div class="card judge"><h3>Judge · ${esc(judge.verdict || "unknown")}</h3>
            <div class="confidence">置信度 ${Math.round((judge.confidence || 0) * 100)}%</div>
            <div class="prose">${markdown(judge.synthesis || round.judge.raw || "")}</div></div>` : ""}
        </section>`;
      }).join("");
    }

    function renderRun(doc) {
      const run = doc.run || {}, decision = doc.summary?.decision || {};
      const unresolved = decision.unresolved_issues || [];
      const major = unresolved.filter(x => x.severity === "major").length;
      $("#main").innerHTML = `
        <div class="topline"><div class="title-wrap"><div class="eyebrow">Decision record · schema v${esc(doc.schema_version)}</div>
          <h2>${esc(run.title)}</h2><div class="run-id">${esc(run.id)}</div></div>
          <button class="menu" id="menu">历史记录</button></div>
        <div class="metrics">
          ${metric(run.status || "unknown", "状态")}
          ${metric(run.round_count || 0, "Rounds")}
          ${metric(run.invocation_count || 0, "模型调用")}
          ${metric(decision.confidence != null ? `${Math.round(decision.confidence*100)}%` : "—", "Judge 置信度")}
        </div>
        <nav class="tabs">
          <button class="tab active" data-tab="overview">结论</button>
          <button class="tab" data-tab="rounds">完整轮次</button>
          <button class="tab" data-tab="request">原始问题</button>
          <button class="tab" data-tab="raw">原始结果</button>
        </nav>
        <section class="panel active" id="panel-overview">
          <div class="grid"><div>
            <div class="card decision"><div class="verdict">${status(decision.verdict || run.status)}
              <span class="confidence">${esc(run.stop_reason || "")}</span></div>
              <h3>最终综合</h3><div class="prose">${markdown(decision.synthesis || doc.summary?.final_markdown || "暂无最终结论")}</div></div>
            <div class="card"><h3>接受的决定</h3>${items(decision.accepted_decisions)}</div>
            <div class="card"><h3>拒绝的选项</h3>${items(decision.rejected_options)}</div>
          </div><aside>
            <div class="card"><h3>角色与模型</h3>${roles(doc.roles)}</div>
            <div class="card"><h3>未解决问题 · ${unresolved.length}</h3>
              ${major ? `<div style="color:var(--bad);font-weight:800;margin-bottom:10px">${major} 个重大问题</div>` : ""}
              ${items(unresolved, "issue")}</div>
            <div class="card"><h3>运行信息</h3><div class="prose">
              <p>开始：${date(run.started_at)}</p><p>结束：${date(run.finished_at)}</p><p>耗时：${duration(run.elapsed_seconds)}</p>
            </div></div>
          </aside></div>
        </section>
        <section class="panel" id="panel-rounds">${rounds(doc.rounds)}</section>
        <section class="panel" id="panel-request"><div class="card"><h3>原始问题</h3><div class="prose">${markdown(doc.request?.markdown || "")}</div></div></section>
        <section class="panel" id="panel-raw"><div class="card"><h3>result.json</h3><pre class="content-block">${esc(JSON.stringify(doc, null, 2))}</pre></div></section>`;
      document.querySelectorAll("[data-tab]").forEach(btn => btn.onclick = () => {
        document.querySelectorAll(".tab,.panel").forEach(x => x.classList.remove("active"));
        btn.classList.add("active");
        $(`#panel-${btn.dataset.tab}`).classList.add("active");
      });
      $("#menu").onclick = () => $("#rail").classList.toggle("open");
    }

    $("#search").addEventListener("input", e => { state.query = e.target.value; renderHistory(); });
    loadHistory().catch(error => {
      $("#main").innerHTML = `<div class="empty"><div><strong>无法读取历史</strong>${esc(error.message)}</div></div>`;
    });
  </script>
</body>
</html>
"""
