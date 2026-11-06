/* 三角洲复盘助手 · 界面逻辑
 * 版式和视觉来自 Stitch 设计；所有数字都来自本机后端，没有数据时显示空状态，不显示示例数字。
 */
"use strict";
(() => {
  // ---------- 工具 ----------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 0) => (v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(d));
  const pct = (v, d = 1) => (v === null || v === undefined ? "—" : (Number(v) * 100).toFixed(d) + "%");
  const signed = (v, d = 1, unit = "") => (v === null || v === undefined ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(d) + unit);
  const bytes = (n) => {
    if (n === null || n === undefined) return "—";
    if (n >= 1024 ** 4) return (n / 1024 ** 4).toFixed(1) + " TB";
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + " GB";
    if (n >= 1024 ** 2) return (n / 1024 ** 2).toFixed(0) + " MB";
    return Math.max(0, Math.round(n / 1024)) + " KB";
  };
  const mmss = (s) => {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(x).padStart(2, "0");
  };
  const durCN = (s) => {
    s = Math.max(0, Math.round(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h}小时${m}分` : `${m}分${String(s % 60).padStart(2, "0")}秒`;
  };
  const when = (ts) => {
    if (!ts) return "—";
    const d = new Date(ts * 1000), now = new Date();
    const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
    const diff = Math.round((day(now) - day(d)) / 86400000);
    if (diff === 0) return "今日 " + hm;
    if (diff === 1) return "昨天 " + hm;
    return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")} ${hm}`;
  };
  const icon = (name, cls = "text-[18px]", fill = false) => `<span class="material-symbols-outlined ${cls}${fill ? " icon-fill" : ""}">${name}</span>`;

  const CAUSES = {
    unseen: "没先看到人", crosshair: "预瞄位置偏了", slow: "开火太晚", recoil: "压枪散了",
    exposed: "身位暴露", outnumbered: "被多打少", gear: "装备劣势", other: "其他",
  };
  const MODES = { "": "未标注", extraction: "烽火地带", warfare: "全面战场" };
  const RESULTS = { "": "未标注", extracted: "撤离成功", failed: "撤离失败", win: "胜利", loss: "失败" };

  // ---------- 后端连接 ----------
  let api = null;
  function connect() {
    return new Promise((resolve, reject) => {
      const params = new URLSearchParams(location.search);
      if (params.get("mock") === "1" && window.MockBackend) return resolve(window.MockBackend);
      if (window.qt && window.qt.webChannelTransport && window.QWebChannel) {
        new window.QWebChannel(window.qt.webChannelTransport, (channel) => {
          const backend = channel.objects.backend;
          const pending = new Map();
          let seq = 0;
          backend.reply.connect((id, raw) => {
            const p = pending.get(id);
            if (!p) return;
            pending.delete(id);
            let out;
            try { out = JSON.parse(raw); } catch (e) { return p.rej(new Error("后台返回内容无法解析")); }
            out.ok ? p.res(out.data) : p.rej(new Error(out.error || "未知错误"));
          });
          resolve({
            call: (method, args = {}) => new Promise((res, rej) => {
              const id = ++seq;
              pending.set(id, { res, rej });
              backend.request(id, method, JSON.stringify(args));
            }),
          });
        });
        return;
      }
      reject(new Error("没有连接到本机后台"));
    });
  }
  const call = async (method, args) => {
    try { return await api.call(method, args); }
    catch (e) { toast(e.message || String(e), "error"); throw e; }
  };

  // ---------- 提示 ----------
  function toast(msg, kind = "info") {
    const cls = { info: "bg-surface-container-high border-surface-container-highest text-on-surface",
      ok: "bg-secondary-container/20 border-secondary/40 text-secondary",
      error: "bg-error-container/40 border-error/40 text-on-error-container" }[kind];
    const el = document.createElement("div");
    el.className = "toast " + cls;
    el.textContent = msg;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
  }

  function modal(html) {
    $("#modal-root").innerHTML = `<div class="fixed inset-0 z-[70] bg-black/60 flex items-center justify-center p-space-xl" data-action="modal-bg">
      <div class="panel w-full max-w-[640px] p-space-lg flex flex-col gap-space-md border border-surface-container-highest" data-stop="1">${html}</div></div>`;
  }
  const closeModal = () => { $("#modal-root").innerHTML = ""; };

  // ---------- 全局状态（每秒刷新） ----------
  let state = null;
  let view = "overview";
  let lastJobsSig = "";
  const pageData = {};

  async function refreshState() {
    try { state = await api.call("state"); } catch (e) { return; }
    renderChrome();
    const sig = JSON.stringify((state.jobs || []).map((j) => [j.id, j.status]));
    if (!lastJobsSig) (state.jobs || []).forEach((j) => { if (j.status !== "running") seenJobs.add(j.id); });
    if (sig !== lastJobsSig) {
      const finished = (state.jobs || []).filter((j) => j.status !== "running" && !seenJobs.has(j.id));
      finished.forEach((j) => {
        seenJobs.add(j.id);
        if (j.notify !== false) toast(j.message || j.label, j.status === "done" ? "ok" : "error");
      });
      if (lastJobsSig && finished.length) renderView(true);
      lastJobsSig = sig;
    }
    if (view === "overview") updateGuardian();
    if (view === "matches") updateJobBadges();
  }
  const seenJobs = new Set();

  function renderChrome() {
    const s = state;
    $("#hdr-version").textContent = "v" + s.version + " 测试版";
    const g = s.game || {};
    const dot = g.running ? (s.recorder.recording ? "bg-error" : "bg-secondary") : "bg-outline";
    const gameText = g.running ? (s.recorder.recording ? "录制中 · " + esc(g.exe || g.title) : "已检测到游戏 · " + esc(g.exe || g.title)) : "等待游戏启动";
    $("#hdr-status").innerHTML = `
      <span class="relative flex h-2 w-2 shrink-0">${g.running ? `<span class="animate-ping absolute inline-flex h-full w-full rounded-full ${dot} opacity-75"></span>` : ""}<span class="relative inline-flex rounded-full h-2 w-2 ${dot}"></span></span>
      <span class="font-label-sm text-label-sm ${g.running ? "text-secondary" : "text-on-surface-variant"} truncate">${gameText}</span>
      <span class="font-label-sm text-label-sm text-on-surface-variant">|</span>
      <span class="font-label-sm text-label-sm text-on-surface-variant truncate">只截取游戏窗口画面</span>
      <span class="font-label-sm text-label-sm text-on-surface-variant">|</span>
      <span class="font-label-sm text-label-sm text-secondary-fixed-dim truncate">不读内存 · 不注入 · 不模拟输入</span>`;
    const r = s.recorder;
    $("#hdr-rec").innerHTML = r.recording
      ? `<span class="w-1.5 h-1.5 rounded-full bg-error"></span><span class="text-on-surface-variant">REC</span><span class="text-primary font-bold">${esc(r.fps || "—")}</span><span class="text-on-surface-variant">FPS</span><span class="text-on-surface-variant ml-space-xs">DROP</span><span class="text-secondary font-bold">${esc(r.drop || "0")}</span>`
      : `<span class="w-1.5 h-1.5 rounded-full ${s.auto_record ? "bg-secondary" : "bg-outline"}"></span><span class="text-on-surface-variant">REC</span><span class="${s.auto_record ? "text-secondary" : "text-outline"} font-bold">${s.auto_record ? "STANDBY" : "OFF"}</span>`;
    const ai = s.ai || {};
    $("#hdr-ai").innerHTML = ai.configured && ai.error
      ? `<span class="material-symbols-outlined text-[14px] text-error">neurology</span><span class="text-error" title="${esc(ai.error)}">AI 暂停</span>`
      : ai.configured && ai.enabled && !ai.vision_ok
      ? `<span class="material-symbols-outlined text-[14px] text-primary">neurology</span><span class="text-primary">AI 看图待自检</span>`
      : ai.configured
      ? `<span class="material-symbols-outlined text-[14px] text-tertiary">neurology</span><span class="text-tertiary">AI</span><span class="text-on-surface">${esc(ai.vision_model || ai.text_model || "已接入")}</span>`
      : `<span class="material-symbols-outlined text-[14px] text-outline">neurology</span><span class="text-outline">AI 未接入</span>`;
    const st = s.storage || {};
    const used = st.used_bytes || 0, free = st.free_bytes || 0;
    $("#side-storage").textContent = bytes(used) + " / 剩 " + bytes(free);
    $("#side-storage-bar").style.width = Math.min(100, (used / Math.max(1, used + free)) * 100).toFixed(1) + "%";
    const gpu = s.gpu;
    $("#side-gpu").textContent = gpu ? `${gpu.temp_c ?? "—"}°C · ${gpu.vram_used_mb ? Math.round(gpu.vram_used_mb / 1024 * 10) / 10 + "G" : "—"} 显存` : "仅 NVIDIA 显卡可读";
    $("#side-build").textContent = "LOCAL ENGINE · BUILD " + (s.build || "");
    $$("#nav .nav-item").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  }

  // ---------- 路由 ----------
  const VIEWS = {};
  async function renderView(keepScroll = false) {
    const y = window.scrollY;
    const fn = VIEWS[view];
    if (!fn) return;
    try {
      const html = await fn();
      $("#view").innerHTML = html;
      afterRender[view] && afterRender[view]();
    } catch (e) {
      $("#view").innerHTML = `<div class="panel p-space-xl empty">${icon("error", "text-[32px] text-error")}<div>页面加载失败：${esc(e.message)}</div></div>`;
    }
    if (keepScroll) window.scrollTo(0, y); else window.scrollTo(0, 0);
  }
  const afterRender = {};
  function goto(v, anchor) {
    view = v;
    try { history.replaceState(null, "", "#" + v); } catch (e) { /* file:// 下个别情况不允许改地址 */ }
    $$("#nav .nav-item").forEach((a) => a.classList.toggle("active", a.dataset.view === v));
    renderView().then(() => { if (anchor) { const el = document.getElementById("sec-" + anchor); el && el.scrollIntoView({ block: "start" }); } });
  }

  // ---------- 通用部件 ----------
  function sparkline(values, w = 240, h = 32) {
    if (!values || values.length < 2) return `<div class="h-8 flex items-center font-label-sm text-label-sm text-outline-variant">暂无测量数据</div>`;
    const min = Math.min(...values), max = Math.max(...values), span = max - min || 1;
    const pts = values.map((v, i) => [(i / (values.length - 1)) * w, h - 4 - ((v - min) / span) * (h - 8)]);
    const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
    return `<svg class="w-full h-8 text-secondary overflow-visible" preserveAspectRatio="none" viewBox="0 0 ${w} ${h}">
      <path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5"></path>
      <path d="${d} L${w},${h} L0,${h} Z" fill="currentColor" opacity="0.08"></path></svg>`;
  }
  function frameChart(before, after, w = 860, h = 150) {
    const series = [[before, "#a08e7a"], [after, "#ffb95f"]].filter((x) => x[0] && x[0].length > 1);
    if (!series.length) return `<div class="h-[150px] flex items-center justify-center font-label-md text-label-md text-outline-variant">测一次帧率后，这里显示逐帧耗时曲线</div>`;
    const all = series.flatMap((s) => s[0]);
    const max = Math.max(...all) * 1.1, min = 0;
    const path = (vals) => vals.map((v, i) => (i ? "L" : "M") + ((i / (vals.length - 1)) * w).toFixed(1) + "," + (h - ((v - min) / (max - min)) * h).toFixed(1)).join(" ");
    const grid = [0.25, 0.5, 0.75].map((f) => `<line x1="0" x2="${w}" y1="${h * f}" y2="${h * f}" stroke="#222b33" stroke-width="1"/>`).join("");
    return `<svg class="w-full h-[150px]" preserveAspectRatio="none" viewBox="0 0 ${w} ${h}">${grid}
      ${series.map(([v, c]) => `<path d="${path(v)}" fill="none" stroke="${c}" stroke-width="1.6"/>`).join("")}</svg>
      <div class="flex justify-between font-label-sm text-label-sm text-outline-variant mt-1"><span>0 ms</span><span>纵轴：每帧耗时，越平越稳 · 最高 ${num(max, 1)} ms</span></div>`;
  }
  const sectionTitle = (code, title, right = "") => `
    <div class="flex items-center justify-between gap-space-md">
      <div class="flex items-center gap-space-sm"><span class="font-label-sm text-label-sm text-primary px-space-xs py-0.5 rounded bg-primary-container/10">${code}</span>
      <span class="font-headline-md text-headline-md text-on-surface">${title}</span></div>${right}</div>`;
  const statusChip = (status) => ({
    ok: `<span class="chip bg-secondary/15 text-secondary">● 正常</span>`,
    applied: `<span class="chip bg-primary-container/15 text-primary">● 已调整</span>`,
    warn: `<span class="chip bg-error/15 text-error">● 需处理</span>`,
    info: `<span class="chip bg-tertiary/15 text-tertiary">● 建议</span>`,
    unknown: `<span class="chip bg-surface-container-highest text-on-surface-variant">● 未检测</span>`,
  }[status] || "");

  // =====================================================================
  // 复盘总览
  // =====================================================================
  VIEWS.overview = async () => {
    const d = await call("dashboard");
    pageData.overview = d;
    return `
    <section class="panel p-space-lg flex flex-col gap-space-md" id="guardian">${guardianHtml()}</section>
    <section class="grid grid-cols-1 xl:grid-cols-3 gap-space-md">${preCard(d.pre)}${midCard(d.mid)}${postCard(d.post)}</section>
    ${debriefHtml(d)}`;
  };

  function guardianHtml() {
    const s = state || { game: {}, recorder: {}, ai: {} };
    const g = s.game || {}, r = s.recorder || {};
    const on = !!s.auto_record;
    const badge = on ? `<span class="chip bg-secondary/15 text-secondary">已开启 · 守护中</span>` : `<span class="chip bg-surface-container-highest text-outline">已关闭</span>`;
    const target = g.running ? `${esc(g.exe || g.title)}（${g.foreground ? "在前台" : "不在前台"} · ${g.width || "?"}×${g.height || "?"}）` : "等待游戏启动";
    return `
      <div class="flex flex-col lg:flex-row items-start lg:items-center justify-between gap-space-md">
        <div class="flex items-center gap-space-md">
          <div class="relative w-12 h-12 rounded-lg bg-surface-container flex items-center justify-center shadow-inner">
            ${icon("radar", "text-primary text-[28px]", true)}
            ${on ? `<span class="absolute -top-1 -right-1 flex h-3 w-3"><span class="animate-ping absolute inline-flex h-full w-full rounded-full bg-secondary opacity-75"></span><span class="relative inline-flex rounded-full h-3 w-3 bg-secondary"></span></span>` : ""}
          </div>
          <div class="flex flex-col">
            <div class="flex items-center gap-space-sm flex-wrap">
              <span class="font-headline-md text-headline-md text-on-surface">自动复盘守护</span>${badge}
              <span class="chip bg-surface-container-high text-on-surface-variant"><span class="w-1.5 h-1.5 rounded-full ${r.recording ? "bg-error" : "bg-secondary"}"></span><span id="g-status">${esc(r.status || "")}</span></span>
            </div>
            <div class="flex items-center gap-space-sm mt-0.5 text-on-surface-variant font-label-md text-label-md flex-wrap">
              <span>TARGET:</span><span class="text-tertiary" id="g-target">${target}</span>
              <span class="text-outline-variant">|</span><span>ENCODER: <span class="text-on-surface">${esc(r.encoder || (r.encoder_busy ? "检测中…" : "未就绪"))}</span></span>
              <span class="text-outline-variant">|</span><span class="text-secondary-fixed-dim">${esc(r.capture || "DDA 桌面复制")}</span>
              ${r.output_changes ? `<span class="text-outline-variant">|</span><span class="text-on-surface-variant">游戏切换画面后自动续录 ${r.output_changes} 次</span>` : ""}
              ${r.failures ? `<span class="text-outline-variant">|</span><span class="text-error">录制出错 ${r.failures} 次（已自动重试）</span>` : ""}
            </div>
          </div>
        </div>
        <div class="flex items-center gap-space-md">
          <div class="text-right"><div class="font-label-sm text-label-sm text-on-surface-variant">MASTER GUARDIAN</div><div class="font-label-md text-label-md text-primary">游戏启动即自动录制</div></div>
          <label class="switch"><input type="checkbox" data-action="toggle-auto" ${on ? "checked" : ""}/><span class="track"></span><span class="knob"></span></label>
        </div>
      </div>
      <div class="w-full bg-surface-container rounded p-space-sm flex flex-col md:flex-row items-start md:items-center justify-between gap-space-sm">
        <div class="flex items-center gap-space-sm">
          <div class="w-6 h-6 rounded bg-secondary-container/20 flex items-center justify-center shrink-0">${icon("verified_user", "text-secondary text-[16px]", true)}</div>
          <div class="flex items-center gap-space-xs flex-wrap"><span class="font-headline-sm text-headline-sm text-secondary">安全边界：</span>
          <span class="font-body-sm text-body-sm text-on-surface-variant">不读写游戏内存、不注入、不模拟键鼠、不装驱动；对局中不弹窗、不叠加画面，只在后台用最低优先级整理已经录完的录像。</span></div>
        </div>
        <span class="flex items-center gap-space-xs font-label-sm text-label-sm text-outline shrink-0" data-action="goto" data-view="settings">${icon("shield", "text-[14px]")}查看完整说明</span>
      </div>`;
  }
  function updateGuardian() {
    const el = $("#guardian");
    if (el && !el.contains(document.activeElement)) el.innerHTML = guardianHtml();
  }

  function preCard(p) {
    p = p || {};
    const gain = p.fps_gain;
    const checks = (p.checks || []).slice(0, 5).map((c) => `
      <div class="flex items-center justify-between gap-space-sm">
        <span class="flex items-center gap-1.5 min-w-0">${icon(c.status === "ok" || c.status === "applied" ? "check_circle" : c.status === "warn" ? "error" : "info",
          (c.status === "ok" || c.status === "applied" ? "text-secondary" : c.status === "warn" ? "text-error" : "text-tertiary") + " text-[14px]")}<span class="truncate">${esc(c.label)}</span></span>
        <span class="font-label-sm text-label-sm text-outline shrink-0">${esc(c.value || "")}</span></div>`).join("");
    return `
    <div class="panel p-space-md flex flex-col justify-between gap-space-md">
      <div class="flex flex-col gap-space-xs">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-space-xs"><span class="tag-label text-primary">[STAGE 01 // 赛前]</span><span class="w-1.5 h-1.5 rounded-full bg-primary"></span></div>
          <button class="btn-quiet !py-0.5 !px-space-xs font-label-sm text-label-sm" data-action="opt-restore">一键还原</button>
        </div>
        <div class="font-headline-md text-headline-md text-on-surface mt-1">${p.applied ? "系统性能已按建议调整" : "赛前性能检查"}</div>
        <div class="font-label-md text-label-md ${p.applied ? "text-secondary" : "text-on-surface-variant"}">${p.applied ? `已调整 ${p.applied} 项系统设置，可一键还原` : p.auto ? "已开启自动优化，系统设置已是推荐状态" : "还没有开启自动优化"}</div>
        <div class="grid grid-cols-2 gap-space-xs mt-space-xs">
          <div class="tile p-space-xs flex flex-col"><span class="font-label-sm text-label-sm text-on-surface-variant">平均帧率变化</span>
            <span class="font-headline-sm text-headline-sm ${gain ? "text-secondary" : "text-outline"} font-bold">${gain ? signed(gain.avg_after - gain.avg_before, 0, " FPS") : "未测量"}</span></div>
          <div class="tile p-space-xs flex flex-col"><span class="font-label-sm text-label-sm text-on-surface-variant">1% LOW 变化</span>
            <span class="font-headline-sm text-headline-sm ${gain ? "text-secondary" : "text-outline"} font-bold">${gain ? signed(((gain.low_after - gain.low_before) / gain.low_before) * 100, 1, "%") : "未测量"}</span></div>
        </div>
        ${p.spec ? `<div class="flex items-center gap-1.5 mt-space-xs font-label-sm text-label-sm text-on-surface-variant" title="完整配置在“赛前优化”页面">${icon("computer", "text-[14px] text-tertiary")}<span class="truncate">${esc(p.spec)}</span></div>` : ""}
        <div class="flex flex-col gap-1 mt-space-xs font-body-sm text-body-sm text-on-surface-variant">${checks || `<div class="text-outline">正在读取电脑配置…</div>`}</div>
      </div>
      <div class="w-full pt-space-xs">
        <div class="flex justify-between items-center font-label-sm text-label-sm text-outline-variant mb-1"><span>FRAME TIME（最近一次测量）</span>
          <span class="text-secondary">${p.frame_p99 ? "99% 帧耗时 " + num(p.frame_p99, 2) + " ms" : ""}</span></div>
        ${sparkline(p.frametimes)}
        <button class="btn-quiet w-full mt-space-sm" data-action="goto" data-view="optimize">进入赛前优化与体检 ${icon("arrow_forward", "text-[16px]")}</button>
      </div>
    </div>`;
  }

  function midCard(m) {
    m = m || {};
    const r = (state && state.recorder) || {};
    return `
    <div class="panel p-space-md flex flex-col justify-between gap-space-md">
      <div class="flex flex-col gap-space-xs">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-space-xs"><span class="tag-label text-tertiary">[STAGE 02 // 赛中]</span><span class="w-1.5 h-1.5 rounded-full bg-tertiary"></span></div>
          <span class="chip bg-surface-container text-tertiary">${r.recording ? "录制中" : r.encoder ? "待机准备完毕" : r.encoder_busy ? "检测编码器" : "未就绪"}</span>
        </div>
        <div class="font-headline-md text-headline-md text-on-surface mt-1">低占用自动录制</div>
        <div class="font-label-md text-label-md text-tertiary">${esc(r.encoder ? r.encoder.toUpperCase() + " 显卡硬件编码" : "显卡硬件编码（自动选择）")}</div>
        <div class="flex flex-col gap-space-xs mt-space-xs">
          <div class="tile p-space-xs flex items-center justify-between"><span class="font-body-sm text-body-sm text-on-surface-variant">录制规格</span><span class="font-label-md text-label-md text-on-surface">${esc(m.spec || "—")}</span></div>
          <div class="tile p-space-xs flex items-center justify-between"><span class="font-body-sm text-body-sm text-on-surface-variant">崩溃保护</span><span class="font-label-md text-label-md text-secondary">分段 MKV · 边录边写</span></div>
          <div class="tile p-space-xs flex items-center justify-between"><span class="font-body-sm text-body-sm text-on-surface-variant">空间策略</span><span class="font-label-md text-label-md text-primary font-bold">原片保留 ${esc(m.retention_label || "—")} · 片段永久</span></div>
        </div>
        <div class="font-body-sm text-body-sm text-on-surface-variant mt-space-xs">只在游戏窗口位于前台时录制，切到桌面 3 秒后自动暂停，不会录到聊天和桌面。</div>
      </div>
      <div class="w-full pt-space-xs flex flex-col gap-1">
        <div class="flex justify-between font-label-sm text-label-sm text-outline-variant"><span>SEGMENT ${esc(m.segment_label || "60 秒")}${m.live ? " · 录完一段就分析" : ""}</span><span class="text-on-surface" id="mid-seg">${r.recording ? "本段 " + esc((r.seg_time || "").split(".")[0]) : "未在录制"}</span></div>
        <div class="w-full h-1.5 bg-surface-container-highest rounded-full overflow-hidden"><div class="h-full bg-gradient-to-r from-tertiary to-secondary rounded-full" style="width:${r.recording ? Math.min(100, (r.seg_progress || 0) * 100) : 0}%"></div></div>
      </div>
    </div>`;
  }

  function postCard(p) {
    p = p || {};
    return `
    <div class="panel p-space-md flex flex-col justify-between gap-space-md">
      <div class="flex flex-col gap-space-xs">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-space-xs"><span class="tag-label text-secondary">[STAGE 03 // 赛后]</span><span class="w-1.5 h-1.5 rounded-full bg-secondary"></span></div>
          <span class="chip ${p.ai_enabled ? "bg-secondary/15 text-secondary" : "bg-surface-container-highest text-outline"}">${p.ai_enabled ? "AI 诊断已开启" : "AI 诊断未开启"}</span>
        </div>
        <div class="font-headline-md text-headline-md text-on-surface mt-1">阵亡与对枪复盘</div>
        <div class="font-body-sm text-body-sm text-on-surface-variant">每局结束自动找阵亡、切片段、给死因；死因你确认后才用于建议</div>
        <div class="grid grid-cols-2 gap-space-xs mt-space-xs">
          <div class="tile p-space-sm flex flex-col"><span class="font-label-sm text-label-sm text-on-surface-variant">最近 7 天对局</span>
            <span class="font-headline-lg text-headline-lg text-primary font-bold">${num(p.sessions_week)} <span class="font-body-sm text-body-sm text-on-surface-variant">局</span></span></div>
          <div class="tile p-space-sm flex flex-col"><span class="font-label-sm text-label-sm text-on-surface-variant">已标注阵亡</span>
            <span class="font-headline-lg text-headline-lg text-tertiary font-bold">${num(p.deaths)} <span class="font-body-sm text-body-sm text-on-surface-variant">次</span></span></div>
        </div>
        <div class="tile p-space-sm flex items-center justify-between mt-space-xs">
          <span class="flex items-center gap-space-xs font-body-md text-body-md text-on-surface">${icon("psychology", "text-secondary text-[18px]")}对枪胜率（已标注）</span>
          <span class="font-label-md text-label-md text-secondary">${p.duel && (p.duel.wins + p.duel.losses) ? pct(p.duel.rate) + ` · ${p.duel.wins}胜 ${p.duel.losses}负` : "暂无数据"}</span>
        </div>
        ${p.unconfirmed ? `<div class="tile p-space-sm flex items-center justify-between"><span class="font-body-sm text-body-sm text-on-surface-variant">AI 判断待你确认</span><span class="font-label-md text-label-md text-primary">${p.unconfirmed} 条</span></div>` : ""}
      </div>
      <button class="btn-quiet w-full" data-action="goto" data-view="matches">进入对局录像与击杀分析 ${icon("arrow_forward", "text-[16px]")}</button>
    </div>`;
  }

  function debriefHtml(d) {
    const recent = d.recent;
    const recs = d.recommendations || [];
    const code = d.gunsmith_top;
    const head = recent ? `
      <div class="flex flex-col lg:flex-row lg:items-end justify-between gap-space-md">
        <div class="flex flex-col gap-space-xs">
          <span class="font-label-md text-label-md text-on-surface-variant">[TACTICAL DEBRIEF // RECENT MATCH] · ${esc(recent.label || recent.name)}</span>
          <span class="font-headline-xl text-headline-xl text-on-surface">${esc(recent.title)}</span>
          <div class="flex items-center gap-space-xs flex-wrap">
            ${recent.result ? `<span class="chip ${recent.result === "extracted" || recent.result === "win" ? "bg-secondary/15 text-secondary" : "bg-error/20 text-error"}">${esc(RESULTS[recent.result] || recent.result)}</span>` : `<span class="chip bg-surface-container-highest text-outline">结果未标注</span>`}
            <span class="chip bg-surface-container text-on-surface-variant">${when(recent.started_at)} · 时长 ${durCN(recent.duration)}</span>
            <span class="chip bg-surface-container text-on-surface-variant">阵亡 ${recent.deaths} 次</span>
          </div>
        </div>
        <div class="flex flex-col items-start lg:items-end gap-space-xs">
          <div class="flex gap-space-xs flex-wrap">
            <span class="tile px-space-sm py-1 font-label-md text-label-md text-on-surface-variant">对枪样本：<span class="text-primary">${recent.duel_total}</span> 次</span>
            <span class="tile px-space-sm py-1 font-label-md text-label-md text-on-surface-variant">本局对枪胜率：<span class="text-secondary">${recent.duel_total ? pct(recent.duel_rate) : "—"}</span></span>
          </div>
          <button class="btn-primary" data-action="open-session" data-name="${esc(recent.name)}">${icon("play_circle", "text-[20px]")}去复盘这一局</button>
        </div>
      </div>` : `
      <div class="empty">${icon("sports_esports", "text-[36px] text-outline")}<div class="font-headline-sm text-headline-sm text-on-surface">还没有录像</div>
      <div>打开总开关后正常打一局，关掉游戏，这里就会出现最近一局的复盘。</div></div>`;
    const diag = d.diagnosis ? `
      <div class="w-full bg-surface-container rounded p-space-md flex items-center justify-between gap-space-md">
        <div class="flex items-center gap-space-md">
          <div class="w-10 h-10 rounded bg-error-container/30 flex items-center justify-center">${icon("troubleshoot", "text-error text-[22px]")}</div>
          <div class="flex flex-col"><span class="font-headline-sm text-headline-sm text-error">死因诊断结论</span>
          <span class="font-body-lg text-body-lg text-on-surface">${esc(d.diagnosis.text)}</span></div>
        </div>
        <span class="tile px-space-sm py-1 font-label-md text-label-md text-on-surface-variant shrink-0">样本 <span class="text-secondary">${d.diagnosis.n}</span> 次阵亡</span>
      </div>` : "";
    const recHtml = recs.length ? recs.map((r, i) => recCard(r, i)).join("") : `
      <div class="tile p-space-sm flex items-center gap-space-sm">${icon("timeline", "text-[20px] text-outline")}
      <span class="font-body-sm text-body-sm text-on-surface-variant">累计 ${esc(d.min_deaths || 5)} 次已确认死因后，这里会按多局数据给出长期改动（例如灵敏度具体数值），采纳后 3 局自动复测。本局的方案在上面，不用等。</span></div>`;
    const verified = (d.verified || []).map((v) => `
      <div class="tile p-space-md flex items-center gap-space-md">
        <div class="w-10 h-10 rounded-full ${v.status === "kept" ? "bg-secondary-container/20" : "bg-error-container/30"} flex items-center justify-center">${icon(v.status === "kept" ? "check_circle" : "undo", (v.status === "kept" ? "text-secondary" : "text-error") + " text-[22px]")}</div>
        <div class="flex flex-col flex-1"><span class="font-headline-sm text-headline-sm ${v.status === "kept" ? "text-secondary" : "text-error"}">${v.status === "kept" ? "上次建议复测有效" : "上次建议复测无效"}</span>
        <span class="font-body-md text-body-md text-on-surface-variant">${esc(v.text)}</span></div></div>`).join("");
    return `
    <section class="panel p-space-lg flex flex-col gap-space-lg">
      ${head}${diag}
      <div class="grid grid-cols-1 xl:grid-cols-[1.4fr_1fr] gap-space-lg">
        <div class="flex flex-col gap-space-md">
          <div class="flex items-center justify-between"><span class="flex items-center gap-space-xs font-headline-md text-headline-md text-primary">${icon("build_circle", "text-[22px]")}本局修正方案</span>
          <span class="font-label-sm text-label-sm text-on-surface-variant">MATCH // 打完一局就给</span></div>
          ${recent ? matchAdviceHtml(d.match_advice, recent.name) : ""}
          <div class="flex items-center justify-between pt-space-xs"><span class="flex items-center gap-space-xs font-headline-sm text-headline-sm text-on-surface">${icon("timeline", "text-[18px]")}长期方案</span>
          <span class="font-label-sm text-label-sm text-on-surface-variant">AUTO-VERIFY · 多局数据 · 3 局后复测</span></div>
          ${recHtml}${verified}
        </div>
        <div class="flex flex-col gap-space-md">
          <div class="flex items-center justify-between"><span class="flex items-center gap-space-xs font-headline-md text-headline-md text-primary">${icon("handyman", "text-[22px]")}对症改枪码推荐</span>
          <span class="font-label-sm text-label-sm text-tertiary" data-action="goto" data-view="gunsmith">查看全部 ›</span></div>
          ${code ? codeCard(code, true) : `<div class="tile p-space-lg empty">${icon("inventory_2", "text-[28px] text-outline")}<div>还没有抓取改枪码</div><button class="btn-ghost" data-action="gs-refresh">现在抓取社区改枪码</button></div>`}
        </div>
      </div>
    </section>`;
  }

  // 本局方案：按这一局的阵亡和死因（AI 判断的也用）马上给出，外加 AI 教练的 3 条点评
  function matchAdviceHtml(ma, sessionName) {
    if (!ma) return "";
    const coach = (ma.coach || "").split("\n").filter((x) => x.trim());
    const coachHtml = coach.length ? `
      <div class="tile p-space-md flex flex-col gap-space-xs">
        <span class="flex items-center gap-space-xs font-headline-sm text-headline-sm text-on-surface">${icon("record_voice_over", "text-primary text-[18px]")}AI 教练点评</span>
        ${coach.map((c, i) => `<div class="flex items-start gap-space-xs font-body-md text-body-md text-on-surface-variant"><span class="text-primary font-semibold">${i + 1}.</span><span>${esc(c)}</span></div>`).join("")}
        <span class="font-label-sm text-label-sm text-outline">根据这一局识别出的数据写的，仅供参考</span>
      </div>` : "";
    const open = sessionName ? `<button class="btn-ghost !py-1 font-label-md text-label-md shrink-0" data-action="open-session" data-name="${esc(sessionName)}">去点死因</button>` : "";
    let body = "";
    if ((ma.items || []).length) {
      body = ma.items.map((r, i) => recCard(r, i)).join("");
      if (ma.uncaused) body += `<div class="font-label-sm text-label-sm text-on-surface-variant">还有 ${ma.uncaused} 次阵亡没判断死因，点了之后方案会更准。</div>`;
    } else if (!ma.deaths) {
      body = `<div class="tile p-space-md flex items-center gap-space-sm">${icon("verified", "text-secondary text-[22px]")}
        <span class="font-body-md text-body-md text-on-surface-variant">这一局没有发现阵亡。如果实际阵亡了，在回放里按 D 标记并点一下死因，本局方案马上出来。</span></div>`;
    } else if (ma.uncaused) {
      body = `<div class="tile p-space-md flex items-center justify-between gap-space-sm flex-wrap">
        <span class="flex items-center gap-space-sm font-body-md text-body-md text-on-surface-variant">${icon("help", "text-tertiary text-[22px]")}这一局阵亡 ${ma.deaths} 次，还没判断死因。开了 AI 看图会自动判断；也可以自己选中阵亡点一下死因，方案马上出来。</span>${open}</div>`;
    } else {
      body = `<div class="tile p-space-md font-body-md text-body-md text-on-surface-variant">这一局的死因选的是“其他”，没有对应的固定改法，看下面的 AI 教练点评。</div>`;
    }
    return body + coachHtml;
  }

  function recCard(r, i) {
    const badge = { match: ["本局方案", "bg-secondary/15 text-secondary"], active: ["待应用", "bg-primary-container/15 text-primary"], adopted: ["复测中", "bg-tertiary/15 text-tertiary"],
      observing: ["观察中", "bg-surface-container-highest text-on-surface-variant"] }[r.status] || ["", ""];
    return `
    <div class="tile p-space-md flex flex-col gap-space-sm">
      <div class="flex items-center justify-between gap-space-sm flex-wrap">
        <div class="flex items-center gap-space-sm"><span class="font-label-md text-label-md px-1.5 py-0.5 rounded bg-primary-container text-on-primary-container font-bold">${String(i + 1).padStart(2, "0")}</span>
        <span class="font-headline-sm text-headline-sm text-on-surface">${esc(r.title)}</span></div>
        <div class="flex items-center gap-space-xs">${r.change_text ? `<span class="chip bg-surface-container-high text-tertiary">${esc(r.change_text)}</span>` : ""}<span class="chip ${badge[1]}">${badge[0]}</span></div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-space-sm">
        <div class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-outline">【数据证据】</span><span class="font-body-md text-body-md text-on-surface-variant">${esc(r.evidence)}</span></div>
        <div class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-outline">【具体改法】</span><span class="font-body-md text-body-md text-primary-fixed-dim">${esc(r.how)}</span></div>
      </div>
      <div class="flex items-center justify-between gap-space-sm flex-wrap pt-space-xs border-t border-surface-container-highest">
        <span class="flex items-center gap-1 font-label-sm text-label-sm text-on-surface-variant">${icon("rule", "text-[14px]")}闭环验证：${esc(r.verify)}</span>
        ${r.status === "active" ? `<button class="btn-ok !py-1 font-label-md text-label-md" data-action="rec-adopt" data-id="${r.id}">我已在游戏里改好</button>` :
          r.status === "adopted" ? `<span class="font-label-sm text-label-sm text-tertiary">已应用 ${esc(r.progress || "")}</span>` : ""}
      </div>
    </div>`;
  }

  function codeCard(c, compact = false) {
    const tags = (c.tags || []).map((t) => `<span class="chip bg-surface-container-high text-on-surface-variant">${esc(t)}</span>`).join("");
    return `
    <div class="tile p-space-md flex flex-col gap-space-sm">
      <div class="flex items-start justify-between gap-space-sm">
        <div class="flex flex-col gap-1 min-w-0">
          <span class="font-label-sm text-label-sm text-on-surface-variant">MATCH // ${esc(c.match_reason || "社区热度")}</span>
          <span class="font-headline-lg text-headline-lg text-on-surface truncate">${esc(c.weapon)}</span>
          <span class="font-label-md text-label-md text-primary">${esc(c.mode)} · ${esc((c.uses || []).join(" / ") || "通用")}${c.price_label ? ` · <span class="text-tertiary">${esc(c.price_label)}</span>` : ""}</span>
        </div>
        <div class="flex flex-col items-end shrink-0"><span class="flex items-center gap-1 font-headline-lg text-headline-lg text-secondary">${icon("thumb_up", "text-[20px]")}${num(c.score)}</span>
        <span class="font-label-sm text-label-sm text-on-surface-variant">综合分</span></div>
      </div>
      ${c.cover ? `<div class="relative rounded overflow-hidden bg-surface-container-lowest h-[150px]"><img src="${esc(c.cover)}" class="w-full h-full object-cover opacity-90" referrerpolicy="no-referrer" onerror="this.parentNode.remove()"/>
        <span class="absolute left-space-sm bottom-space-sm chip bg-black/60 text-on-surface">${esc(c.source_title || "").slice(0, 34)}</span></div>` : ""}
      <div class="flex flex-wrap gap-space-xs">${tags}</div>
      <div class="grid grid-cols-3 gap-space-xs">
        <div class="bg-surface-container-low rounded p-space-xs"><div class="font-label-sm text-label-sm text-outline">发布</div><div class="font-label-md text-label-md text-on-surface">${esc(c.age_label || "—")}</div></div>
        <div class="bg-surface-container-low rounded p-space-xs"><div class="font-label-sm text-label-sm text-outline">播放 / 收藏</div><div class="font-label-md text-label-md text-on-surface">${esc(c.popularity || "—")}</div></div>
        <div class="bg-surface-container-low rounded p-space-xs"><div class="font-label-sm text-label-sm text-outline">失效反馈</div><div class="font-label-md text-label-md ${c.invalid ? "text-error" : "text-secondary"}">${num(c.invalid)} 条</div></div>
      </div>
      <div class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">GUNSMITH CODE</span>
        <div class="flex items-center gap-space-xs bg-surface-container-lowest rounded p-1 pl-space-sm">
          <span class="font-label-md text-label-md text-primary flex-1 truncate select-text">${esc(c.code)}</span>
          <button class="btn-primary !py-1 font-label-md text-label-md" data-action="copy" data-text="${esc(c.code)}">${icon("content_copy", "text-[14px]")}一键复制</button>
        </div></div>
      <div class="flex items-center justify-between gap-space-sm flex-wrap font-label-sm text-label-sm text-on-surface-variant">
        <span>来源：<a class="text-tertiary cursor-pointer" data-action="open-url" data-url="${esc(c.url)}">${esc(c.author || "社区")}</a> · 社区方案，未经本软件实测</span>
        ${compact ? "" : `<span class="flex items-center gap-space-xs">
          <button class="btn-quiet !py-0.5 !px-space-xs" data-action="code-fb" data-code="${esc(c.code)}" data-ok="1">${icon("thumb_up", "text-[14px]")}导入成功 ${num(c.fb_ok)}</button>
          <button class="btn-quiet !py-0.5 !px-space-xs" data-action="code-fb" data-code="${esc(c.code)}" data-ok="0">${icon("thumb_down", "text-[14px]")}导入失效 ${num(c.fb_fail)}</button>
          <button class="btn-quiet !py-0.5 !px-space-xs ${c.adopted ? "text-secondary" : ""}" data-action="code-adopt" data-code="${esc(c.code)}">${icon(c.adopted ? "check" : "swap_horiz", "text-[14px]")}${c.adopted ? "正在使用" : "我换上了"}</button></span>`}
      </div>
    </div>`;
  }

  // =====================================================================
  // 对局录像与击杀分析
  // =====================================================================
  const mstate = { mode: "", map: "", result: "", days: 7, session: null, event: null, clip: null };

  VIEWS.matches = async () => {
    const d = await call("matches", { mode: mstate.mode, map: mstate.map, result: mstate.result, days: mstate.days });
    pageData.matches = d;
    if (!mstate.session && d.list.length) mstate.session = d.list[0].name;
    const detail = mstate.session ? await call("session_detail", { name: mstate.session }) : null;
    pageData.detail = detail;
    if (detail && mstate.event && !detail.events.find((e) => e.id === mstate.event)) mstate.event = null;
    if (detail && mstate.event === null) { const firstDeath = detail.events.find((e) => e.kind === "death"); mstate.event = firstDeath ? firstDeath.id : 0; }
    const k = d.kpis || {};
    const seg = (key, val, label) => `<button class="${mstate[key] === val ? "on" : ""}" data-action="m-filter" data-key="${key}" data-val="${esc(val)}">${label}</button>`;
    return `
    <section class="panel p-space-md flex flex-col gap-space-md">
      <div class="flex items-center gap-space-sm flex-wrap">
        <div class="seg">${seg("mode", "", "全部模式")}${seg("mode", "extraction", "烽火地带")}${seg("mode", "warfare", "全面战场")}</div>
        <select class="field !w-auto" data-action="m-select" data-key="map"><option value="">地图：全部</option>${(d.maps || []).map((m) => `<option ${m === mstate.map ? "selected" : ""}>${esc(m)}</option>`).join("")}</select>
        <select class="field !w-auto" data-action="m-select" data-key="result"><option value="">结果：全部</option>${Object.entries(RESULTS).filter(([v]) => v).map(([v, l]) => `<option value="${v}" ${v === mstate.result ? "selected" : ""}>${l}</option>`).join("")}</select>
        <div class="seg">${[7, 30, 0].map((n) => `<button class="${mstate.days === n ? "on" : ""}" data-action="m-days" data-val="${n}">${n ? "最近" + n + "天" : "全部"}</button>`).join("")}</div>
        <span class="font-label-md text-label-md text-on-surface-variant">${d.list.length} 局</span>
        <div class="flex-1"></div>
        <button class="btn-primary" data-action="import-video">${icon("upload", "text-[18px]")}导入外部录像</button>
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-5 gap-space-sm">
        ${kpi("总复盘对局", num(k.sessions), "MATCHES")}
        ${kpi("已标注阵亡", num(k.deaths), "K.O.", "text-error")}
        ${kpi("对枪胜率（已标注）", k.duel_total ? pct(k.duel_rate) : "—", k.duel_total ? `${k.wins}W / ${k.losses}L` : "暂无数据", "text-secondary")}
        ${kpi("主要死因", k.top_cause ? esc(CAUSES[k.top_cause] || k.top_cause) : "—", k.top_cause ? `占 ${pct(k.top_cause_share, 0)}` : "待标注", "text-primary")}
        ${kpi("AI 判断待确认", num(k.unconfirmed), "条", "text-tertiary")}
      </div>
    </section>
    <section class="grid grid-cols-1 xl:grid-cols-[minmax(320px,0.9fr)_minmax(0,1.6fr)] gap-space-md items-start">
      <div class="flex flex-col gap-space-sm min-w-0">
        <div class="flex items-center justify-between px-space-xs"><span class="font-headline-sm text-headline-sm text-on-surface"><span class="font-label-sm text-label-sm text-primary">[ARC_LOG // 01]</span> 对局索引与阵亡标记</span>
        <span class="font-label-sm text-label-sm text-on-surface-variant">按时间倒序</span></div>
        ${d.list.length ? d.list.map(sessionItem).join("") : `<div class="panel p-space-xl empty">${icon("videocam_off", "text-[32px] text-outline")}<div>这个筛选条件下没有录像</div></div>`}
        <div class="font-label-sm text-label-sm text-on-surface-variant px-space-xs">原片保留 ${esc(d.retention_label)}；已生成的阵亡片段和标注会一直保留。</div>
      </div>
      <div class="flex flex-col gap-space-md min-w-0" id="m-detail">${detail ? detailHtml(detail) : `<div class="panel p-space-xl empty">${icon("smart_display", "text-[36px] text-outline")}<div>选择左侧的一场录像开始复盘</div></div>`}</div>
    </section>`;
  };

  const kpi = (label, value, unit, color = "text-on-surface") => `
    <div class="tile p-space-sm flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">${label}</span>
    <span class="font-headline-lg text-headline-lg ${color} font-bold">${value} <span class="font-label-md text-label-md text-on-surface-variant font-normal">${unit}</span></span></div>`;

  function sessionItem(s) {
    const active = s.name === mstate.session;
    const res = s.result ? `<span class="chip ${s.result === "extracted" || s.result === "win" ? "bg-secondary/15 text-secondary" : "bg-error/20 text-error"}">${esc(RESULTS[s.result])}</span>` : "";
    const deaths = active ? (s.deaths || []).map((e) => `
      <div class="rounded p-space-sm flex flex-col gap-1 cursor-pointer ${e.id === mstate.event ? "bg-surface-container-high border-l-2 border-primary-container" : "bg-surface-container hover:bg-surface-container-high"}" data-action="pick-event" data-id="${e.id}">
        <div class="flex items-center justify-between gap-space-xs">
          <span class="font-headline-sm text-headline-sm text-on-surface"><span class="font-label-md text-label-md text-primary">[${mmss(e.t)}]</span> ${e.killer_weapon ? "被 " + esc(e.killer_weapon) + " 击杀" : "阵亡"}</span>
          <span class="flex items-center gap-1 font-label-sm text-label-sm ${e.clip ? "text-secondary" : "text-on-surface-variant"}">${icon(e.clip ? "play_circle" : "movie", "text-[14px]")}${e.clip ? "片段就绪" : "未生成片段"}</span>
        </div>
        <div class="flex items-center gap-space-xs flex-wrap font-body-sm text-body-sm text-on-surface-variant">
          ${e.cause ? `<span class="chip bg-error/15 text-error">${esc(CAUSES[e.cause] || e.cause)}</span>` : `<span class="chip bg-surface-container-highest text-outline">死因未标</span>`}
          ${e.source === "ai" && !e.confirmed ? `<span class="chip bg-tertiary/15 text-tertiary">AI 判断 · 待确认</span>` : ""}
          ${e.source === "auto" && !e.confirmed ? `<span class="chip bg-tertiary/15 text-tertiary">自动识别 · 待选死因</span>` : ""}
          ${e.distance_m ? `<span>交战距离 ${num(e.distance_m)}m</span>` : ""}
        </div></div>`).join("") : "";
    return `
    <div class="panel p-space-md flex flex-col gap-space-sm ${active ? "ring-1 ring-primary-container/40" : "cursor-pointer hover:bg-surface-container"}" ${active ? "" : `data-action="pick-session" data-name="${esc(s.name)}"`}>
      <div class="flex items-center justify-between gap-space-sm">
        <div class="flex items-center gap-space-xs min-w-0">${res}<span class="font-headline-md text-headline-md text-on-surface truncate">${esc(s.title)}</span>${s.imported ? `<span class="chip bg-surface-container-highest text-on-surface-variant">导入</span>` : ""}</div>
        <span class="font-label-md text-label-md text-on-surface-variant shrink-0">${when(s.started_at)}</span>
      </div>
      <div class="flex items-center gap-space-md font-body-md text-body-md text-on-surface-variant">
        <span class="flex items-center gap-1">${icon("timer", "text-[16px]")}${durCN(s.duration)}</span>
        <span class="flex items-center gap-1">${icon("skull", "text-[16px]")}阵亡 ${s.death_count}</span>
        <span class="flex items-center gap-1">${icon("swords", "text-[16px]")}${s.duel.w}胜 ${s.duel.l}负</span>
        <span class="flex-1"></span><span class="font-label-sm text-label-sm">${bytes(s.size)}</span>
      </div>
      ${active && deaths ? `<div class="font-label-sm text-label-sm text-on-surface-variant pt-space-xs">阵亡标记（${s.death_count}）</div>${deaths}` : ""}
      ${active && !s.death_count ? `<div class="font-body-sm text-body-sm text-on-surface-variant">还没有阵亡标记：开启 AI 看图后，每局结束会自动找；也可以在回放里按 D 手动标记。</div>` : ""}
    </div>`;
  }

  function detailHtml(dt) {
    const ev = dt.events.find((e) => e.id === mstate.event) || null;
    const clip = ev ? (ev.clip || null) : (dt.last_clip || null);
    const markers = clip ? dt.events.filter((e) => e.t >= clip.t_start - 0.01 && e.t <= clip.t_end + 0.01).map((e) => {
      const left = ((e.t - clip.t_start) / Math.max(0.1, clip.t_end - clip.t_start)) * 100;
      const c = e.kind === "death" ? "bg-error" : e.kind === "duel_win" ? "bg-secondary" : "bg-primary";
      return `<span class="absolute -top-1 w-2.5 h-2.5 rounded-full ${c} ring-2 ring-surface-container-lowest" style="left:calc(${left.toFixed(2)}% - 5px)" title="${esc(e.kind === "death" ? "阵亡" : e.kind === "duel_win" ? "对枪赢" : "对枪输")}"></span>`;
    }).join("") : "";
    const jobs = (state && state.jobs || []).filter((j) => j.session === dt.name && j.status === "running");
    const meta = dt.meta || {};
    const sel = (key, options) => `<select class="field !w-auto" data-action="meta-set" data-key="${key}">${Object.entries(options).map(([v, l]) => `<option value="${v}" ${meta[key] === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;
    return `
    <div class="panel p-space-md flex flex-col gap-space-sm">
      <div class="flex items-center justify-between gap-space-sm flex-wrap">
        <div class="flex items-center gap-space-xs font-label-md text-label-md">
          <span class="w-2 h-2 rounded-full ${clip ? "bg-error" : "bg-outline"}"></span>
          <span class="text-error font-bold">${ev && ev.kind === "death" ? "DEATH" : "VOD"} REPLAY</span><span class="text-outline-variant">|</span>
          <span class="text-on-surface-variant">${esc(dt.label || dt.name)}</span><span class="text-outline-variant">|</span>
          <span class="text-tertiary">${esc(dt.spec || "")}</span>
        </div>
        <div class="flex items-center gap-space-xs">
          ${sel("mode", MODES)}
          <input class="field !w-32" placeholder="地图（可选）" value="${esc(meta.map || "")}" data-action="meta-map"/>
          ${sel("result", RESULTS)}
        </div>
      </div>
      <div class="relative rounded overflow-hidden bg-surface-container-lowest aspect-video flex items-center justify-center" id="player-box">
        ${clip ? `<video id="player" class="w-full h-full object-contain bg-black" src="${esc(clip.src)}" preload="auto"></video>
          ${ev && ev.ai && ev.ai.headline ? `<div class="absolute right-space-md top-space-md max-w-[60%] rounded px-space-sm py-1 bg-surface-container-lowest/80 border border-primary-container/40"><div class="font-headline-sm text-headline-sm text-primary">${esc(ev.ai.headline)}</div><div class="font-label-sm text-label-sm text-on-surface-variant">AI 判断，仅供参考</div></div>` : ""}`
          : `<div class="empty">${icon("movie", "text-[36px] text-outline")}<div>${ev ? "这次阵亡还没有生成片段" : "点下方时间轴上的任意位置，生成该时刻前后 15 秒的片段"}</div>
            ${ev ? `<button class="btn-primary" data-action="make-clip" data-t="${ev.t}" data-event="${ev.id}">${icon("content_cut", "text-[18px]")}生成阵亡前 15 秒片段</button>` : ""}</div>`}
        ${jobs.length ? `<div class="absolute left-space-md top-space-md chip bg-black/60 text-primary" id="job-badge">${icon("progress_activity", "text-[14px] animate-spin")}${esc(jobs[0].label)} ${jobs[0].progress ? Math.round(jobs[0].progress * 100) + "%" : ""}</div>` : ""}
      </div>
      <div class="flex flex-col gap-space-xs">
        <div class="relative w-full h-1.5 bg-surface-container-highest rounded-full cursor-pointer" id="scrub">${markers}<div id="scrub-fill" class="h-full bg-primary-container rounded-full" style="width:0%"></div></div>
        <div class="flex items-center justify-between gap-space-sm flex-wrap">
          <div class="flex items-center gap-space-xs">
            <button class="btn-quiet !p-1.5" data-action="pl-toggle" ${clip ? "" : "disabled"}>${icon("play_arrow", "text-[20px]")}</button>
            <button class="btn-quiet !p-1.5" data-action="pl-seek" data-d="-1" ${clip ? "" : "disabled"}>${icon("replay", "text-[18px]")}</button>
            <button class="btn-quiet !p-1.5" data-action="pl-seek" data-d="1" ${clip ? "" : "disabled"}>${icon("forward_media", "text-[18px]")}</button>
            <span class="font-label-lg text-label-lg text-primary" id="pl-time">${clip ? "0.00s / " + num(clip.t_end - clip.t_start, 2) + "s" : "—"}</span>
            <div class="seg ml-space-xs">${[0.25, 0.5, 1].map((r) => `<button class="${r === 1 ? "on" : ""}" data-action="pl-rate" data-r="${r}">${r}x</button>`).join("")}</div>
          </div>
          <div class="flex items-center gap-space-xs">
            <span class="font-label-sm text-label-sm text-on-surface-variant">在当前画面标记：</span>
            <button class="btn-quiet !py-1 font-label-md text-label-md text-error" data-action="mark" data-kind="death" ${clip ? "" : "disabled"}>${icon("skull", "text-[16px]")}阵亡 D</button>
            <button class="btn-quiet !py-1 font-label-md text-label-md text-secondary" data-action="mark" data-kind="duel_win" ${clip ? "" : "disabled"}>${icon("military_tech", "text-[16px]")}对枪赢 W</button>
            <button class="btn-quiet !py-1 font-label-md text-label-md text-primary" data-action="mark" data-kind="duel_loss" ${clip ? "" : "disabled"}>${icon("heart_broken", "text-[16px]")}对枪输 L</button>
          </div>
        </div>
      </div>
      <div class="flex flex-col gap-space-xs pt-space-xs">
        <div class="flex items-center justify-between"><span class="font-label-sm text-label-sm text-on-surface-variant">这一局的时间轴（${mmss(dt.total_duration)}）· 点击缩略图生成该时刻片段</span>
          <div class="flex items-center gap-space-xs">
            <button class="btn-ghost !py-1 font-label-md text-label-md" data-action="ai-scan" ${dt.ai_ready ? "" : "disabled"} title="${dt.ai_ready ? "重新让 AI 找这一局的阵亡并判断死因" : "先在设置里接入 AI 平台并开启 AI 看图"}">${icon("travel_explore", "text-[16px]")}AI 重新分析</button>
            ${dt.match_id && !dt.open ? `<button class="btn-quiet !py-1 font-label-md text-label-md" data-action="export-match" title="把这一局保存成一个完整的录像文件（不重新压缩，不会被自动清理）">${icon("save", "text-[16px]")}保存这一局</button>` : ""}
            <button class="btn-quiet !py-1 font-label-md text-label-md" data-action="open-folder" data-path="${esc(dt.folder)}">${icon("folder_open", "text-[16px]")}原片文件夹</button>
          </div></div>
        <div class="flex gap-1 overflow-x-auto pb-1" id="thumbs">${(dt.thumbs || []).length ? dt.thumbs.map((th) => `
          <div class="shrink-0 w-[112px] cursor-pointer group" data-action="make-clip" data-t="${th.t + 6}">
            <div class="relative rounded overflow-hidden bg-surface-container-lowest h-[63px] ring-1 ring-transparent group-hover:ring-primary-container">
              <img src="${esc(th.src)}" class="w-full h-full object-cover" loading="lazy"/>
              ${dt.events.filter((e) => e.t >= th.t && e.t < th.t + dt.thumb_step).map((e) => `<span class="absolute right-1 top-1 w-2 h-2 rounded-full ${e.kind === "death" ? "bg-error" : e.kind === "duel_win" ? "bg-secondary" : "bg-primary"}"></span>`).join("")}
            </div><div class="font-label-sm text-label-sm text-on-surface-variant mt-0.5">${mmss(th.t)}</div></div>`).join("")
          : `<div class="w-full tile p-space-md flex items-center justify-between"><span class="font-body-sm text-body-sm text-on-surface-variant">${dt.thumbs_pending ? "正在生成缩略图…" : "还没有生成缩略图"}</span>
             ${dt.thumbs_pending ? "" : `<button class="btn-ghost !py-1" data-action="make-thumbs">生成时间轴缩略图</button>`}</div>`}</div>
      </div>
    </div>
    ${ev ? diagnosisHtml(ev, dt) : ""}
    ${dt.match_advice ? `<div class="panel p-space-md flex flex-col gap-space-md">
      <div class="flex items-center justify-between gap-space-sm flex-wrap"><span class="flex items-center gap-space-xs font-headline-md text-headline-md text-on-surface">${icon("build_circle", "text-primary text-[22px]")}本局诊断与方案</span>
      <span class="font-label-sm text-label-sm text-on-surface-variant">${ev ? "" : "选中左侧的一次阵亡可以看截图诊断、改死因"}</span></div>
      ${matchAdviceHtml(dt.match_advice, "")}</div>` : (ev ? "" : `<div class="panel p-space-lg empty">${icon("psychology_alt", "text-[30px] text-outline")}<div>选中左侧的一次阵亡，查看死因诊断和建议</div></div>`)}`;
  }

  function diagnosisHtml(ev, dt) {
    const ai = ev.ai || null;
    const causeChips = Object.entries(CAUSES).map(([k, l]) => `<button class="chip cursor-pointer ${ev.cause === k ? "bg-primary-container text-on-primary-container font-semibold" : "bg-surface-container-high text-on-surface-variant hover:text-on-surface"}" data-action="set-cause" data-id="${ev.id}" data-cause="${k}">${l}</button>`).join("");
    const cards = ai ? `
      <div class="grid grid-cols-1 lg:grid-cols-3 gap-space-sm">
        ${aiCard("my_location", "交火过程", ai.process, ai.process_tag)}
        ${aiCard("shield", "伤害与装备", ai.gear, ai.gear_tag)}
        ${aiCard("hearing", "听声与视野", ai.awareness, ai.awareness_tag)}
      </div>
      ${ai.advice ? `<div class="tile p-space-sm flex items-start gap-space-sm">${icon("lightbulb", "text-primary text-[18px]")}<div class="flex flex-col"><span class="font-label-sm text-label-sm text-outline">AI 给出的改进方向</span><span class="font-body-md text-body-md text-on-surface">${esc(ai.advice)}</span></div></div>` : ""}` : `
      <div class="tile p-space-md flex items-center justify-between gap-space-sm flex-wrap">
        <span class="font-body-md text-body-md text-on-surface-variant">${dt.ai_ready ? "AI 会看这次阵亡前 6 秒的几张截图，给出死因判断（没有片段会顺便生成）。" : "先在设置里接入 AI 平台并开启“AI 看图辅助分析”。"}</span>
        <button class="btn-primary" data-action="ai-diagnose" data-id="${ev.id}" ${dt.ai_ready ? "" : "disabled"}>${icon("neurology", "text-[18px]")}AI 诊断这次阵亡</button></div>`;
    return `
    <div class="panel p-space-md flex flex-col gap-space-md">
      <div class="flex items-center justify-between gap-space-sm flex-wrap">
        <span class="font-headline-md text-headline-md text-on-surface"><span class="font-label-sm text-label-sm text-primary">[SYS_EVAL // DIAGNOSTICS]</span> 阵亡归因 · ${mmss(ev.t)}</span>
        <div class="flex items-center gap-space-xs">
          ${ai ? `<span class="font-label-sm text-label-sm text-on-surface-variant">AI 自评把握 <span class="text-secondary">${pct(ai.confidence, 0)}</span> · ${esc(ai.model || "")}</span>` : ""}
          <button class="btn-quiet !py-0.5 !px-space-xs font-label-sm text-label-sm" data-action="del-event" data-id="${ev.id}">${icon("delete", "text-[14px]")}删除标记</button>
        </div>
      </div>
      ${cards}
      <div class="rounded border border-dashed border-surface-container-highest p-space-sm flex flex-col gap-space-xs">
        <div class="flex items-center gap-space-xs font-body-md text-body-md text-on-surface">${icon("rate_review", "text-primary text-[18px]")}
          ${ev.cause ? `当前判定：<span class="text-primary font-semibold">${esc(CAUSES[ev.cause] || ev.cause)}</span>${ev.confirmed ? `<span class="chip bg-secondary/15 text-secondary">已确认</span>` : `<span class="chip bg-tertiary/15 text-tertiary">${ev.source === "ai" ? "AI 判断，待确认" : ev.source === "auto" ? "自动识别，待确认" : "未确认"}</span>`}` : "还没有判定死因，请选择一个："}</div>
        <div class="flex flex-wrap gap-space-xs">${causeChips}
          ${ev.cause && !ev.confirmed ? `<button class="chip cursor-pointer bg-secondary-container text-on-secondary-container font-semibold" data-action="confirm-event" data-id="${ev.id}">确认标注</button>` : ""}</div>
        <div class="font-label-sm text-label-sm text-on-surface-variant">只有确认过的死因才计入统计和建议；AI 判断不准就点正确的那一项。</div>
      </div>
    </div>`;
  }
  const aiCard = (ic, title, text, tag) => `
    <div class="tile p-space-sm flex flex-col gap-space-xs">
      <div class="flex items-center justify-between gap-space-xs"><span class="flex items-center gap-1 font-headline-sm text-headline-sm text-on-surface">${icon(ic, "text-primary text-[18px]")}${title}</span>
      ${tag ? `<span class="chip bg-error/15 text-error">${esc(tag)}</span>` : ""}</div>
      <span class="font-body-md text-body-md text-on-surface-variant">${esc(text || "画面里看不出")}</span></div>`;

  afterRender.matches = () => {
    const v = $("#player");
    if (!v) return;
    const dt = pageData.detail;
    const ev = dt && dt.events.find((e) => e.id === mstate.event);
    const clip = ev ? ev.clip : dt.last_clip;
    if (!clip) return;
    const len = () => (v.duration && isFinite(v.duration) ? v.duration : clip.t_end - clip.t_start);
    v.addEventListener("timeupdate", () => {
      $("#pl-time").textContent = v.currentTime.toFixed(2) + "s / " + len().toFixed(2) + "s";
      $("#scrub-fill").style.width = Math.min(100, (v.currentTime / len()) * 100) + "%";
    });
    $("#scrub").addEventListener("click", (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      v.currentTime = ((e.clientX - r.left) / r.width) * len();
    });
    v.addEventListener("loadedmetadata", () => { if (ev && ev.kind === "death" && clip) v.currentTime = Math.max(0, ev.t - clip.t_start - 4); });
  };
  function updateJobBadges() {
    const dt = pageData.detail;
    if (!dt) return;
    const jobs = (state.jobs || []).filter((j) => j.session === dt.name && j.status === "running");
    const badge = $("#job-badge");
    if (badge && jobs.length) badge.innerHTML = `${icon("progress_activity", "text-[14px] animate-spin")}${esc(jobs[0].label)} ${jobs[0].progress ? Math.round(jobs[0].progress * 100) + "%" : ""}`;
  }

  // =====================================================================
  // 改枪码推荐
  // =====================================================================
  const gstate = { weapon: "", mode: "", use: "" };
  VIEWS.gunsmith = async () => {
    const d = await call("gunsmith", gstate);
    pageData.gunsmith = d;
    const p = d.profile || {};
    const crawl = d.crawl || {};
    return `
    <section class="flex flex-col gap-space-xs">
      <div class="flex items-center gap-space-xs font-label-md text-label-md"><span class="px-space-xs py-0.5 rounded bg-primary-container/10 text-primary">MODULE // ARSENAL TELEMETRY</span><span class="text-on-surface-variant">// 社区数据 · 本机打分</span></div>
      <div class="flex items-end justify-between gap-space-md flex-wrap">
        <span class="font-headline-xl text-headline-xl text-on-surface">社区改枪码推荐与战绩匹配</span>
        <span class="tile px-space-sm py-1 flex items-center gap-space-xs font-label-md text-label-md text-on-surface-variant">${icon("verified", "text-secondary text-[16px]")}
          ${crawl.last ? `上次抓取 ${when(crawl.last)} · 收录 <span class="text-secondary">${crawl.count}</span> 个改枪码 · 来自 ${crawl.videos} 个视频` : "还没有抓取"}
          <button class="btn-quiet !py-0.5 !px-space-xs ml-space-xs" data-action="gs-refresh" ${crawl.running ? "disabled" : ""}>${icon("refresh", "text-[14px]")}${crawl.running ? "抓取中…" : "重新抓取"}</button></span>
      </div>
    </section>
    <section class="panel p-space-md flex items-start gap-space-md border-l-2 border-primary-container !rounded-none">
      <div class="w-10 h-10 rounded bg-surface-container flex items-center justify-center shrink-0">${icon("shield", "text-primary text-[22px]")}</div>
      <div class="flex flex-col gap-1"><span class="font-headline-sm text-headline-sm text-primary">来源与可信度说明</span>
      <span class="font-body-md text-body-md text-on-surface-variant">改枪码从 B 站公开视频的简介里抓取，标明原作者和链接；按发布时间、热度和失效反馈打分。本软件不生成改枪码、不破解编码、不会往游戏里自动粘贴。社区方案未经本软件实测，赛季更新后可能失效。</span></div>
    </section>
    <section class="panel p-space-md flex flex-col gap-space-md">
      <div class="flex items-center justify-between flex-wrap gap-space-sm">
        <span class="flex items-center gap-space-xs font-headline-md text-headline-md text-on-surface"><span class="w-2.5 h-2.5 bg-primary-container"></span>根据你最近的复盘数据${p.sample_n ? `（${p.sample_n} 次已确认阵亡）` : ""}</span>
        <div class="flex items-center gap-space-xs font-label-md text-label-md text-on-surface-variant">价格区间：
          <div class="seg">${[{ label: "", count: null }].concat(d.budgets || []).map((b) => `<button class="${(d.budget || "") === b.label ? "on" : ""}" data-action="gs-budget" data-val="${b.label}">${b.label || "不限"}${b.count != null ? ` <span class="opacity-60">${b.count}</span>` : ""}</button>`).join("")}</div></div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-space-sm">
        ${profileTile("straighten", "交火距离偏好", p.distance_label || "未知", p.distance_note || "AI 诊断阵亡后才能估算距离", "text-tertiary")}
        ${profileTile("trending_up", "主要短板", p.issue_label || "未知", p.issue_note || "确认几次死因后显示", "text-error")}
        ${profileTile("payments", "价格区间", d.budget || "不限", "单位：哈夫币。价格从视频简介里读，没写的归到“价格未标注”", "text-primary")}
        ${profileTile("auto_fix_high", "推荐方向", p.direction || "按社区热度", p.direction_note || "数据不足时按综合分排序", "text-secondary")}
      </div>
    </section>
    <section class="flex flex-col gap-space-md">
      <div class="flex items-center justify-between flex-wrap gap-space-sm">
        <span class="font-headline-lg text-headline-lg text-on-surface"><span class="font-label-sm text-label-sm text-primary">[TACTICAL_RECOMMENDATIONS]</span> 推荐方案（TOP 3）</span>
        <div class="flex items-center gap-space-xs">
          <select class="field !w-auto" data-action="gs-filter" data-key="mode"><option value="">模式：全部</option>${["烽火地带", "全面战场"].map((m) => `<option ${gstate.mode === m ? "selected" : ""}>${m}</option>`).join("")}</select>
          <select class="field !w-auto" data-action="gs-filter" data-key="weapon"><option value="">枪械：全部</option>${(d.weapons || []).map((w) => `<option ${gstate.weapon === w ? "selected" : ""}>${esc(w)}</option>`).join("")}</select>
          <select class="field !w-auto" data-action="gs-filter" data-key="use"><option value="">用途：全部</option>${(d.uses || []).map((u) => `<option ${gstate.use === u ? "selected" : ""}>${esc(u)}</option>`).join("")}</select>
        </div>
      </div>
      ${d.top.length ? `<div class="grid grid-cols-1 xl:grid-cols-3 gap-space-md">${d.top.map((c, i) => `<div class="panel p-0 overflow-hidden border-t-2 ${["border-primary-container", "border-tertiary", "border-secondary"][i]}">${codeCard(c)}</div>`).join("")}</div>`
        : `<div class="panel p-space-xl empty">${icon("inventory_2", "text-[32px] text-outline")}<div>${crawl.last ? "没有符合筛选条件的改枪码" : "还没有抓取社区改枪码"}</div>${crawl.last ? "" : `<button class="btn-primary" data-action="gs-refresh">现在抓取</button>`}</div>`}
    </section>
    ${priceGroupsHtml(d)}
    <section class="panel p-space-md flex flex-col gap-space-sm">
      <span class="font-headline-md text-headline-md text-on-surface"><span class="font-label-sm text-label-sm text-primary">[EFFICACY_TELEMETRY]</span> 方案效果追踪</span>
      ${(d.efficacy || []).length ? d.efficacy.map((e) => `
        <div class="tile p-space-sm grid grid-cols-1 md:grid-cols-4 gap-space-sm items-center">
          <span class="font-body-md text-body-md text-on-surface">${esc(e.weapon)} <span class="font-label-sm text-label-sm text-on-surface-variant">自 ${when(e.adopted_at)}</span></span>
          <span class="font-label-md text-label-md text-on-surface-variant">换装前对枪胜率 <span class="text-on-surface">${e.before_n ? pct(e.before_rate) : "—"}</span>（${e.before_n} 次）</span>
          <span class="font-label-md text-label-md text-on-surface-variant">换装后 <span class="text-secondary">${e.after_n ? pct(e.after_rate) : "—"}</span>（${e.after_n} 次）</span>
          <span class="font-label-md text-label-md ${e.verdict_ok ? "text-secondary" : "text-on-surface-variant"}">${esc(e.verdict)}</span>
        </div>`).join("") : `<div class="font-body-md text-body-md text-on-surface-variant">点改枪码卡片上的“我换上了”，软件会比较换装前后你自己的对枪胜率（需要你在回放里标注对枪胜负）。多人汇总的效果统计需要服务器，充值版上线后提供。</div>`}
    </section>`;
  };
  function priceGroupsHtml(d) {
    const groups = (d.groups || []).filter((g) => g.active && g.count);
    if (!groups.length) return "";
    return `<section class="panel p-space-md flex flex-col gap-space-md">
      <div class="flex items-center justify-between flex-wrap gap-space-sm">
        <span class="flex items-center gap-space-xs font-headline-md text-headline-md text-on-surface">${icon("payments", "text-primary text-[22px]")}按价格区间</span>
        <span class="font-label-sm text-label-sm text-on-surface-variant">每个区间按综合分排序，最多显示 12 个</span></div>
      ${groups.map((g) => `
      <div class="flex flex-col gap-space-xs">
        <div class="flex items-center gap-space-sm"><span class="chip ${g.label === "价格未标注" ? "bg-surface-container-highest text-outline" : "bg-primary-container/15 text-primary"}">${esc(g.label)}</span>
          <span class="font-label-sm text-label-sm text-on-surface-variant">${g.count} 个${g.count > g.items.length ? `，显示前 ${g.items.length} 个` : ""}</span></div>
        <div class="flex flex-col divide-y divide-surface-container-highest">${g.items.map((c) => `
          <div class="flex items-center gap-space-md py-space-xs">
            <span class="w-10 font-label-md text-label-md text-secondary">${num(c.score)}</span>
            <span class="w-32 font-body-md text-body-md text-on-surface truncate">${esc(c.weapon)}</span>
            <span class="w-20 font-label-md text-label-md ${c.price_label ? "text-tertiary" : "text-outline"}">${esc(c.price_label || "—")}</span>
            <span class="w-20 font-label-sm text-label-sm text-on-surface-variant">${esc(c.mode)}</span>
            <span class="flex-1 font-label-md text-label-md text-primary truncate select-text">${esc(c.code)}</span>
            <span class="w-28 font-label-sm text-label-sm text-on-surface-variant truncate">${esc(c.author || "")}</span>
            <span class="w-16 font-label-sm text-label-sm text-on-surface-variant">${esc(c.age_label || "")}</span>
            <button class="btn-quiet !py-0.5 !px-space-xs" data-action="copy" data-text="${esc(c.code)}">${icon("content_copy", "text-[14px]")}</button>
          </div>`).join("")}</div>
      </div>`).join("")}
    </section>`;
  }
  const profileTile = (ic, label, value, note, color) => `
    <div class="tile p-space-md flex flex-col gap-space-xs">
      <div class="flex items-center justify-between font-label-sm text-label-sm text-on-surface-variant"><span>${label}</span>${icon(ic, color + " text-[18px]")}</div>
      <span class="font-headline-md text-headline-md ${color}">${esc(value)}</span>
      <span class="font-body-sm text-body-sm text-on-surface-variant">${esc(note)}</span></div>`;

  // =====================================================================
  // 赛前优化与体检
  // =====================================================================
  VIEWS.optimize = async () => {
    const d = await call("optimizer");
    pageData.optimize = d;
    const m = d.measure || {};
    const b = m.before, a = m.after;
    const cmp = (label, x, y, unit, better) => {
      const has = x && y;
      const delta = has ? (better === "high" ? (y - x) / x : (x - y) / x) * 100 : null;
      return `<div class="tile p-space-md flex flex-col gap-space-sm">
        <div class="flex items-center justify-between font-label-sm text-label-sm text-on-surface-variant"><span>${label}</span>
          ${has ? `<span class="chip ${delta >= 0 ? "bg-secondary/15 text-secondary" : "bg-error/15 text-error"}">${signed(better === "high" ? delta : -delta, 1, "%")}</span>` : ""}</div>
        <div class="flex items-baseline gap-space-xs"><span class="font-headline-lg text-headline-lg text-on-surface-variant">${x ? num(x, unit === "ms" ? 1 : 0) : "—"}</span>
        ${icon("arrow_forward", "text-[16px] text-outline")}<span class="font-headline-xl text-headline-xl ${y ? "text-primary" : "text-outline"}">${y ? num(y, unit === "ms" ? 1 : 0) : "—"}</span><span class="font-label-md text-label-md text-on-surface-variant">${unit}</span></div></div>`;
    };
    const items = d.items || [];
    return `
    <section class="panel p-space-lg flex flex-col gap-space-md">
      <div class="flex items-start justify-between gap-space-md flex-wrap">
        <div class="flex flex-col gap-space-xs">
          <div class="flex items-center gap-space-sm font-label-md text-label-md"><span class="px-space-xs py-0.5 rounded bg-primary-container/10 text-primary">SYS_DIAG // STAGE_ALPHA</span><span class="text-secondary">● ${d.scanned_at ? "硬件状态 " + when(d.scanned_at) + " 已扫描" : "正在扫描硬件状态"}</span></div>
          <span class="font-headline-xl text-headline-xl text-on-surface">赛前性能检查与靶场体检</span>
          <span class="font-body-lg text-body-lg text-on-surface-variant">改动前先备份 · 效果以实测帧率为准 · 不改系统安全设置 · 不安装驱动 · <span class="text-secondary">所有改动一键还原</span></span>
        </div>
        <div class="flex items-center gap-space-sm">
          <button class="btn-ghost" data-action="opt-restore">${icon("history", "text-[18px]")}一键恢复原始系统配置</button>
          <button class="btn-primary" data-action="opt-rescan">${icon("sync", "text-[18px]")}重新扫描硬件状态</button>
        </div>
      </div>
      <div class="grid grid-cols-2 xl:grid-cols-4 gap-space-sm">
        ${kpi("硬件检查通过", `${d.pass}/${d.total}`, d.total ? Math.round((d.pass / d.total) * 100) + "%" : "", "text-primary")}
        ${kpi("录制方式", esc(state && state.recorder.encoder ? state.recorder.encoder.toUpperCase() : "检测中"), "桌面复制 · 不注入", "text-secondary")}
        ${kpi("实测 1% LOW 变化", b && a ? signed(((a.low1 - b.low1) / b.low1) * 100, 1, "%") : "未测量", "优化前后对比", "text-secondary")}
        ${kpi("驱动与内存访问", "无", "不装驱动 · 不读内存", "text-tertiary")}
      </div>
      <div class="tile p-space-sm flex items-center justify-between gap-space-sm flex-wrap">
        <span class="flex items-center gap-space-xs font-body-md text-body-md text-on-surface">${icon("bolt", "text-primary text-[18px]")}自动处理下方能自动处理的项：刷新率、游戏模式、Xbox 录制在游戏没运行时调整；电源计划只在打游戏期间切到高性能（都可一键还原）</span>
        <label class="switch"><input type="checkbox" data-action="opt-auto" ${d.auto ? "checked" : ""}/><span class="track"></span><span class="knob"></span></label>
      </div>
    </section>
    ${specsHtml(d)}
    <section class="grid grid-cols-1 xl:grid-cols-[1.7fr_1fr] gap-space-md">
      <div class="panel p-space-md flex flex-col gap-space-md">
        <div class="flex items-start justify-between gap-space-sm flex-wrap">
          <div class="flex flex-col"><span class="flex items-center gap-space-xs font-headline-md text-headline-md text-on-surface">${icon("monitoring", "text-primary text-[22px]")}PresentMon 真实帧率对比</span>
          <span class="font-label-sm text-label-sm text-on-surface-variant">Windows 事件追踪（ETW）采样 · 不读游戏内存 · 需要你在弹出的系统授权窗口点“是”</span></div>
          <div class="flex items-center gap-space-sm font-label-sm text-label-sm"><span class="flex items-center gap-1"><span class="w-3 h-1.5 rounded bg-outline"></span>优化前</span><span class="flex items-center gap-1"><span class="w-3 h-1.5 rounded bg-primary-fixed-dim"></span>优化后</span></div>
        </div>
        <div class="grid grid-cols-1 md:grid-cols-3 gap-space-sm">
          ${cmp("平均帧率", b && b.avg, a && a.avg, "FPS", "high")}
          ${cmp("1% LOW 帧率", b && b.low1, a && a.low1, "FPS", "high")}
          ${cmp("99% 帧耗时", b && b.p99, a && a.p99, "ms", "low")}
        </div>
        <div class="rounded bg-surface-container-lowest p-space-md flex flex-col gap-space-xs">
          <div class="flex justify-between font-label-sm text-label-sm text-on-surface-variant"><span>FRAME PACING TIMELINE（逐帧耗时）</span><span class="text-secondary">${a ? "优化后 " + when(a.created) : b ? "优化前 " + when(b.created) : ""}</span></div>
          ${frameChart(b && b.series, a && a.series)}
        </div>
        <div class="flex items-center justify-between gap-space-sm flex-wrap">
          <span class="font-body-sm text-body-sm text-on-surface-variant">${d.presentmon ? (state && state.game.running ? "游戏正在运行：点“开始测量”并在授权窗口点“是”，10 秒内切回游戏正常打 60 秒；前后两次最好同一张图、同一段路线。" : "先打开游戏进入对局，再开始测量。") : "没有找到 PresentMon，请重新运行 setup.bat 下载。"}</span>
          <div class="flex items-center gap-space-xs">
            <div class="seg">${["before", "after"].map((k) => `<button class="${(d.next_label || "before") === k ? "on" : ""}" data-action="opt-label" data-val="${k}">${k === "before" ? "记为优化前" : "记为优化后"}</button>`).join("")}</div>
            <button class="btn-primary" data-action="opt-measure" ${d.presentmon && state && state.game.running && !d.measuring ? "" : "disabled"}>${icon("timer", "text-[18px]")}${d.measuring ? "测量中…" : "开始测量 60 秒"}</button>
          </div>
        </div>
      </div>
      <div class="panel p-space-md flex flex-col gap-space-md border border-tertiary/20">
        <div class="flex items-center justify-between"><span class="font-label-md text-label-md text-tertiary">MODE: FIRING_RANGE</span>${icon("my_location", "text-tertiary text-[24px]")}</div>
        <span class="font-headline-lg text-headline-lg text-on-surface">3 分钟靶场体检</span>
        <span class="font-body-md text-body-md text-on-surface-variant">进入训练靶场后，按固定流程打 3 组定靶和移动靶，软件根据录像计算开镜定位误差、压枪上飘幅度，给出灵敏度校准建议。</span>
        <div class="tile p-space-md flex flex-col gap-space-xs font-label-md text-label-md">
          <div class="flex justify-between"><span class="text-on-surface-variant">当前灵敏度设置</span><span class="text-on-surface">${esc(d.profile_text || "未填写")}</span></div>
          <div class="flex justify-between"><span class="text-on-surface-variant">屏幕距离系数</span><span class="text-primary">${esc(d.mdv || "—")}</span></div>
        </div>
        <div class="tile p-space-sm flex items-start gap-space-xs font-body-sm text-body-sm text-on-surface-variant">${icon("info", "text-[16px] text-tertiary")}
          <span>这一项需要先用你的录像样本训练识别（认出靶场画面、测准星偏移），下一版开放。现在可以先到设置里填好你的灵敏度，用于“修正方案”的具体数值。</span></div>
        <button class="btn-ghost" data-action="goto" data-view="settings" data-anchor="profile">${icon("edit", "text-[18px]")}填写我的灵敏度设置</button>
      </div>
    </section>
    <section class="panel p-space-md flex flex-col gap-space-md">
      <div class="flex items-center justify-between flex-wrap gap-space-sm"><span class="flex items-center gap-space-xs font-headline-md text-headline-md text-on-surface">${icon("fact_check", "text-primary text-[22px]")}硬件核心指标与优化项</span>
      <span class="font-label-sm text-label-sm text-on-surface-variant">数据来自系统接口 · 不依赖任何驱动</span></div>
      <div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-space-sm">${items.map(optItem).join("") || `<div class="empty">正在读取…</div>`}</div>
    </section>`;
  };
  function specsHtml(d) {
    const groups = d.specs || [];
    const text = groups.map((g) => `【${g.group}】\n` + g.rows.map((r) => `${r.k}：${r.v}`).join("\n")).join("\n");
    return `
    <section class="panel p-space-md flex flex-col gap-space-md">
      <div class="flex items-center justify-between flex-wrap gap-space-sm">
        <div class="flex flex-col"><span class="flex items-center gap-space-xs font-headline-md text-headline-md text-on-surface">${icon("computer", "text-primary text-[22px]")}电脑完整配置</span>
        <span class="font-label-sm text-label-sm text-on-surface-variant">${esc(d.spec_summary || "")}${d.spec_summary ? " · " : ""}来自 Windows 系统信息，只读 · 不含序列号和电脑名</span></div>
        ${groups.length ? `<button class="btn-ghost !py-1 font-label-md text-label-md" data-action="copy" data-text="${esc(text)}">${icon("content_copy", "text-[16px]")}复制完整配置</button>` : ""}
      </div>
      ${groups.length ? `<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-space-sm">${groups.map((g) => `
        <div class="tile p-space-md flex flex-col gap-space-xs">
          <span class="flex items-center gap-space-xs font-headline-sm text-headline-sm text-on-surface">${icon(g.icon || "memory", "text-tertiary text-[18px]")}${esc(g.group)}</span>
          ${g.rows.map((r) => `<div class="flex flex-col"><span class="font-label-sm text-label-sm text-outline">${esc(r.k)}</span><span class="font-body-md text-body-md text-on-surface break-words">${esc(r.v)}</span></div>`).join("")}
        </div>`).join("")}</div>` : `<div class="tile p-space-md empty">${d.scanned_at ? "这台电脑读不到配置信息（只支持 Windows 10/11）" : "正在读取电脑配置，大约需要 10 秒…"}</div>`}
    </section>`;
  }

  function optItem(it) {
    return `
    <div class="tile p-space-md flex flex-col gap-space-sm justify-between">
      <div class="flex flex-col gap-space-xs">
        <div class="flex items-center justify-between gap-space-xs"><span class="flex items-center gap-space-xs font-headline-md text-headline-md text-on-surface">${icon(it.icon || "memory", "text-tertiary text-[20px]")}${esc(it.title)}</span>${statusChip(it.status)}</div>
        <span class="font-body-md text-body-md text-on-surface-variant">${esc(it.detail)}</span>
      </div>
      <div class="flex items-center justify-between gap-space-xs font-label-sm text-label-sm pt-space-xs border-t border-surface-container-highest">
        <span class="text-on-surface-variant">${esc(it.footer || "")}</span>
        ${it.can_apply ? `<button class="btn-quiet !py-0.5 !px-space-xs text-primary" data-action="opt-apply" data-key="${esc(it.key)}">${esc(it.apply_label || "立即优化")}</button>` :
          it.applied ? `<button class="btn-quiet !py-0.5 !px-space-xs" data-action="opt-restore-one" data-key="${esc(it.key)}">还原这一项</button>` :
          it.link ? `<span class="text-tertiary cursor-pointer underline" data-action="open-url" data-url="${esc(it.link)}">${esc(it.link_label || "打开")}</span>` :
          `<span class="text-outline">${esc(it.footer_right || "")}</span>`}
      </div>
    </div>`;
  }

  // =====================================================================
  // 设置
  // =====================================================================
  VIEWS.settings = async () => {
    const s = await call("settings");
    pageData.settings = s;
    const c = s.config;
    const prov = (s.ai.providers || []).find((x) => x.id === s.ai.provider) || { label: "AI", vision: [], text: [] };
    const retention = [[1, "1天"], [3, "3天（默认）"], [7, "7天"], [0, "永不删除"]];
    const prof = c.profile || {};
    const pf = (key, label, step = "0.01") => `<label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">${label}</span>
      <input class="field" type="number" step="${step}" data-action="profile" data-key="${key}" value="${prof[key] ?? ""}" placeholder="未填"/></label>`;
    return `
    <section class="flex flex-col gap-space-xs">
      <div class="flex items-center gap-space-xs font-label-lg text-label-lg text-primary tracking-[0.2em]"><span class="w-1.5 h-1.5 rounded-full bg-primary"></span>CONTROL PANEL // COMPLIANCE &amp; PROTOCOLS</div>
      <div class="flex items-end justify-between gap-space-md flex-wrap">
        <span class="font-headline-xl text-headline-xl text-on-surface">系统设置与安全说明 <span class="font-label-md text-label-md text-on-surface-variant">BUILD ${esc(s.build)}</span></span>
        <span class="tile px-space-sm py-1 flex items-center gap-space-xs font-label-md text-label-md text-secondary">${icon("verified_user", "text-[16px]")}不装驱动 · 不读内存 · 不注入</span>
      </div>
    </section>
    <section class="panel p-space-lg flex flex-col gap-space-md">
      <div class="flex items-start justify-between gap-space-md flex-wrap">
        <div class="flex items-start gap-space-md">
          <div class="w-12 h-12 rounded bg-surface-container flex items-center justify-center shrink-0">${icon("shield_lock", "text-secondary text-[28px]", true)}</div>
          <div class="flex flex-col gap-space-xs"><span class="font-headline-md text-headline-md text-on-surface">安全边界与透明说明</span>
          <span class="font-body-md text-body-md text-on-surface-variant max-w-[760px]">本软件只使用 Windows 公开的截屏、性能和系统设置接口，不修改、不读取任何游戏内存。它不是腾讯官方软件，也没有经过 ACE 反作弊的认证；下面列出的是它实际做和不做的事，测试版的源码就在安装文件夹里，可以自行查看。</span></div>
        </div>
        <div class="flex items-center gap-space-xs">
          <button class="btn-quiet" data-action="open-folder" data-path="${esc(s.app_root)}">${icon("code", "text-[18px]")}打开源码文件夹</button>
          <span class="tile px-space-sm py-1.5 font-label-md text-label-md text-on-surface-variant select-text">${icon("fingerprint", "text-[16px] align-middle")} SHA256: ${esc(s.fingerprint)}</span>
        </div>
      </div>
      <div class="grid grid-cols-2 lg:grid-cols-5 gap-space-sm">
        ${fact("0 内存读取", "不打开游戏进程句柄")}${fact("0 驱动", "不装任何内核驱动")}${fact("0 键鼠模拟", "不发送任何输入")}${fact("系统接口", "桌面复制截屏 + 系统设置")}${fact("对局中静默", "不弹窗、不叠加画面")}
      </div>
    </section>
    <section class="grid grid-cols-1 xl:grid-cols-[1.6fr_1fr] gap-space-md items-start">
      <div class="flex flex-col gap-space-md">
        <div class="panel p-space-md flex flex-col gap-space-md" id="sec-storage">
          ${sectionTitle("01 // REPLAY_STORAGE", "录像存储与清理策略", `<span class="font-label-sm text-label-sm text-on-surface-variant">可用空间 ${bytes(s.storage.free_bytes)}</span>`)}
          <div class="tile p-space-md flex flex-col gap-space-sm">
            <span class="font-headline-sm text-headline-sm text-on-surface">录像保存路径</span>
            <div class="flex items-center gap-space-xs"><div class="field flex items-center gap-space-xs !py-space-sm">${icon("folder", "text-[16px] text-on-surface-variant")}<span class="truncate select-text">${esc(s.storage.path)}</span></div>
            <button class="btn-ghost shrink-0" data-action="pick-folder">${icon("drive_file_move", "text-[18px]")}更改路径</button></div>
            <span class="font-label-sm text-label-sm text-on-surface-variant">建议放在固态硬盘上，1 小时 1080p 录像约 9 GB。</span>
          </div>
          <div class="tile p-space-md flex items-center justify-between gap-space-md flex-wrap">
            <div class="flex flex-col gap-1 max-w-[520px]"><span class="font-headline-sm text-headline-sm text-on-surface">原始录像保留天数</span>
            <span class="font-body-md text-body-md text-on-surface-variant">超过天数的原片自动删除；已生成的<span class="text-primary">阵亡片段、缩略图和标注永久保留</span>。</span></div>
            <div class="seg">${retention.map(([v, l]) => `<button class="${c.retention_days === v ? "on" : ""}" data-action="set" data-key="retention_days" data-val="${v}">${l}</button>`).join("")}</div>
          </div>
          <div class="grid grid-cols-1 md:grid-cols-2 gap-space-sm">
            <div class="tile p-space-md flex flex-col gap-space-sm">
              <div class="flex items-center justify-between"><span class="font-headline-sm text-headline-sm text-on-surface">录制规格与硬件编码</span><span class="font-label-sm text-label-sm text-secondary">${esc(s.encoder || "自动")}</span></div>
              <div class="grid grid-cols-3 gap-space-xs">
                <label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">帧率</span><select class="field" data-action="set-select" data-key="framerate">${[60, 30].map((v) => `<option value="${v}" ${c.framerate === v ? "selected" : ""}>${v} FPS</option>`).join("")}</select></label>
                <label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">码率</span><select class="field" data-action="set-select" data-key="bitrate_mbps">${[[0, "自动"], [12, "12 Mbps"], [20, "20 Mbps"], [30, "30 Mbps"], [45, "45 Mbps"]].map(([v, l]) => `<option value="${v}" ${c.bitrate_mbps === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
                <label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">编码器</span><select class="field" data-action="set-select" data-key="encoder">${["auto", ...s.encoders].map((v) => `<option value="${v}" ${c.encoder === v ? "selected" : ""}>${v === "auto" ? "自动" : v}</option>`).join("")}</select></label>
              </div>
              <div class="flex items-center justify-between"><span class="font-label-sm text-label-sm text-on-surface-variant">录制显示器</span>
                <select class="field !w-auto" data-action="set-select" data-key="monitor_index">${[[-1, "自动（游戏所在显示器）"], [0, "显示器 1"], [1, "显示器 2"], [2, "显示器 3"]].map(([v, l]) => `<option value="${v}" ${c.monitor_index === v ? "selected" : ""}>${l}</option>`).join("")}</select></div>
              <button class="btn-quiet" data-action="reprobe">${icon("sync", "text-[16px]")}重新检测编码器</button>
            </div>
            <div class="tile p-space-md flex flex-col gap-space-sm">
              <div class="flex items-center justify-between"><span class="font-headline-sm text-headline-sm text-on-surface">麦克风与游戏音频录制</span>
              <label class="switch opacity-40"><input type="checkbox" disabled/><span class="track"></span><span class="knob"></span></label></div>
              <span class="font-body-md text-body-md text-on-surface-variant">测试版只录画面，不录任何声音。录游戏声音（用于听声分析）在下一版作为可选项加入，默认关闭，麦克风始终不录。</span>
            </div>
          </div>
        </div>

        <div class="panel p-space-md flex flex-col gap-space-md" id="sec-ai">
          ${sectionTitle("02 // AI_ENGINE", "AI 模型接入", `<span class="font-label-sm text-label-sm ${s.ai.configured ? "text-secondary" : "text-outline"}">${s.ai.configured ? esc(prov.label) + " · 密钥" + esc(s.ai.key_mask) : "未填写密钥"}</span>`)}
          <div class="tile p-space-md flex flex-col gap-space-sm">
            <div class="flex items-center justify-between gap-space-sm flex-wrap">
              <span class="font-headline-sm text-headline-sm text-on-surface">用哪家的模型</span>
              <span class="font-label-sm text-label-sm text-tertiary cursor-pointer underline" data-action="open-url" data-url="${esc(prov.console)}">去 ${esc(prov.label)} 控制台申请 API Key</span>
            </div>
            <div class="seg flex-wrap">${(s.ai.providers || []).map((x) => `<button class="${x.id === s.ai.provider ? "on" : ""}" data-action="set-provider" data-val="${esc(x.id)}">${esc(x.label)}${x.has_key ? " ✓" : ""}</button>`).join("")}</div>
            <span class="font-label-sm text-label-sm text-on-surface-variant">${esc(prov.note || "")}。每家的密钥分开保存，切换平台不会丢。</span>
            <div class="flex items-center gap-space-xs">
              <input class="field" type="password" id="ai-key" placeholder="粘贴 ${esc(prov.label)} 的 API Key，只保存在本机 Windows 凭据管理器" autocomplete="off"/>
              <button class="btn-primary shrink-0" data-action="ai-save-key">保存密钥</button>
              <button class="btn-quiet shrink-0" data-action="ai-del-key">删除</button>
            </div>
            <span class="font-label-sm text-label-sm text-on-surface-variant">密钥只发往下面的接口地址，不写进日志和诊断包。不要把密钥发给任何人。</span>
            <div class="grid grid-cols-1 md:grid-cols-3 gap-space-xs">
              <label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">接口地址</span><input class="field" data-action="set-input" data-key="ai_base_url" value="${esc(c.ai_base_url)}"/></label>
              <label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">文字模型（写本局点评用）</span><input class="field" list="dl-text" data-action="set-input" data-key="ai_text_model" value="${esc(c.ai_text_model)}"/></label>
              <label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">看图模型（推荐 ${esc((prov.vision || [""])[0])}）</span><input class="field" list="dl-vision" data-action="set-input" data-key="ai_vision_model" value="${esc(c.ai_vision_model)}"/></label>
            </div>
            <datalist id="dl-text">${Array.from(new Set([...(prov.text || []), ...(s.ai.models || [])])).map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
            <datalist id="dl-vision">${Array.from(new Set([...(prov.vision || []), ...(s.ai.models || [])])).map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
            ${s.ai.vision_status ? `<div class="font-label-md text-label-md ${s.ai.vision_status.startsWith("看图自检通过") ? "text-secondary" : "text-primary"}">${icon("visibility", "text-[14px] align-middle")} ${esc(s.ai.vision_status)}</div>` : ""}
            <span class="font-label-sm text-label-sm text-on-surface-variant">软件会发两张纯色测试图，模型说对颜色才算真的能看图；看不到图的模型会自动换成这家推荐的看图模型。</span>
            <div class="flex items-center gap-space-xs flex-wrap">
              <button class="btn-ghost" data-action="ai-test" data-kind="models">${icon("lan", "text-[16px]")}测试连接</button>
              <button class="btn-ghost" data-action="ai-test" data-kind="vision">${icon("visibility", "text-[16px]")}重新看图自检</button>
              <button class="btn-ghost" data-action="ai-test" data-kind="text">${icon("chat", "text-[16px]")}文字测试</button>
              <button class="btn-ghost" data-action="ai-test" data-kind="image-file">${icon("image", "text-[16px]")}看图测试：选截图</button>
              <button class="btn-ghost" data-action="ai-test" data-kind="image-recording">${icon("movie", "text-[16px]")}看图测试：最近录像</button>
            </div>
            <pre id="ai-out" class="bg-surface-container-lowest rounded p-space-sm font-label-md text-label-md text-on-surface-variant whitespace-pre-wrap min-h-[64px] select-text">${esc(s.ai.last_output || "测试结果显示在这里")}</pre>
          </div>
          <div class="tile p-space-md flex items-center justify-between gap-space-md flex-wrap">
            <div class="flex flex-col gap-1"><span class="font-headline-sm text-headline-sm text-on-surface">每日用量上限</span>
            <span class="font-body-md text-body-md text-on-surface-variant">今天已用 <span class="text-primary">${num(s.ai.used_today)}</span> tokens；超过上限后当天不再调用 AI，避免意外扣费。</span></div>
            <select class="field !w-auto" data-action="set-select" data-key="ai_daily_token_limit">${[[200000, "20 万"], [500000, "50 万"], [1000000, "100 万"], [3000000, "300 万"]].map(([v, l]) => `<option value="${v}" ${c.ai_daily_token_limit === v ? "selected" : ""}>${l} tokens</option>`).join("")}</select>
          </div>
        </div>

        <div class="panel p-space-md flex flex-col gap-space-md" id="sec-privacy">
          ${sectionTitle("03 // PRIVACY_AUTHORIZATION", "隐私与数据（由你控制）", `<span class="font-label-sm text-label-sm text-on-surface-variant">录像不出本机</span>`)}
          <div class="tile p-space-md flex items-center gap-space-md">
            <div class="w-10 h-10 rounded bg-surface-container-high flex items-center justify-center shrink-0">${icon("visibility", "text-tertiary text-[22px]")}</div>
            <div class="flex flex-col flex-1"><span class="font-headline-sm text-headline-sm text-on-surface">AI 看图辅助分析</span>
            <span class="font-body-md text-body-md text-on-surface-variant">开启后，会把每隔 3 秒截一张、9 张拼成一张的缩小截图发给上面选择的 AI 平台，用来找阵亡、判断死因（左上角的账号 UID 水印会先涂黑）。不开启时，按局切分用 Windows 自带的文字识别在本机完成。</span></div>
            <label class="switch ok"><input type="checkbox" data-action="set-check" data-key="ai_vision_enabled" ${c.ai_vision_enabled ? "checked" : ""}/><span class="track"></span><span class="knob"></span></label>
          </div>
          <div class="tile p-space-md flex items-center gap-space-md">
            <div class="w-10 h-10 rounded bg-surface-container-high flex items-center justify-center shrink-0">${icon("bolt", "text-tertiary text-[22px]")}</div>
            <div class="flex flex-col flex-1"><span class="font-headline-sm text-headline-sm text-on-surface">每局结束马上分析</span>
            <span class="font-body-md text-body-md text-on-surface-variant">打游戏时每录完 1 分钟就在后台整理（最低优先级，每分钟大约 2～3 次 AI 请求），一局结束几分钟内出结果。关掉后改成退出游戏再统一分析。</span></div>
            <label class="switch ok"><input type="checkbox" data-action="set-check" data-key="live_analyze" ${c.live_analyze ? "checked" : ""}/><span class="track"></span><span class="knob"></span></label>
          </div>
          <div class="tile p-space-md flex items-center gap-space-md opacity-60">
            <div class="w-10 h-10 rounded bg-surface-container-high flex items-center justify-center shrink-0">${icon("query_stats", "text-tertiary text-[22px]")}</div>
            <div class="flex flex-col flex-1"><span class="font-headline-sm text-headline-sm text-on-surface">参与匿名数据汇总 <span class="font-label-sm text-label-sm text-outline">[未开放]</span></span>
            <span class="font-body-md text-body-md text-on-surface-variant">需要服务器，充值版上线后才开放；届时只上传数字指标，不上传录像和账号。</span></div>
            <label class="switch"><input type="checkbox" disabled/><span class="track"></span><span class="knob"></span></label>
          </div>
          <div class="tile p-space-md flex items-center gap-space-md flex-wrap">
            <div class="w-10 h-10 rounded bg-surface-container-high flex items-center justify-center shrink-0">${icon("database", "text-tertiary text-[22px]")}</div>
            <div class="flex flex-col flex-1"><span class="font-headline-sm text-headline-sm text-on-surface">本地数据：SQLite 数据库</span>
            <span class="font-label-sm text-label-sm text-on-surface-variant">${num(s.db.events)} 条标注 · ${num(s.db.codes)} 个改枪码 · ${num(s.db.measurements)} 次帧率测量 · 占用 ${bytes(s.db.size)}</span></div>
            <button class="btn-ghost" data-action="export-data">${icon("download", "text-[18px]")}导出本地数据</button>
            <button class="btn-quiet" data-action="reset-data">${icon("delete_sweep", "text-[18px]")}重置数据库</button>
          </div>
        </div>

        <div class="panel p-space-md flex flex-col gap-space-md" id="sec-game">
          ${sectionTitle("04 // GAME_DETECTION", "游戏识别")}
          <div class="tile p-space-md grid grid-cols-1 md:grid-cols-2 gap-space-sm">
            <label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">窗口标题关键词（逗号分隔）</span><input class="field" data-action="set-list" data-key="game_title_keywords" value="${esc((c.game_title_keywords || []).join("，"))}"/></label>
            <label class="flex flex-col gap-1"><span class="font-label-sm text-label-sm text-on-surface-variant">游戏进程名（填了就只认这个进程）</span><input class="field" data-action="set-list" data-key="game_process_names" value="${esc((c.game_process_names || []).join("，"))}" placeholder="留空则按标题识别"/></label>
          </div>
          <div class="flex items-center justify-between gap-space-sm flex-wrap"><span class="font-body-sm text-body-sm text-on-surface-variant">启动器也被当成游戏、或者认不出游戏时：先打开游戏，再从当前窗口里选。</span>
          <button class="btn-ghost" data-action="pick-window">${icon("ads_click", "text-[18px]")}从当前窗口中选择</button></div>
        </div>

        <div class="panel p-space-md flex flex-col gap-space-md" id="sec-profile">
          ${sectionTitle("05 // MY_SENSITIVITY", "我的游戏灵敏度设置", `<span class="font-label-sm text-label-sm text-on-surface-variant">用于给出具体数值的修正建议</span>`)}
          <div class="tile p-space-md grid grid-cols-2 md:grid-cols-4 gap-space-sm">
            ${pf("dpi", "鼠标 DPI", "50")}${pf("mouse", "鼠标灵敏度")}${pf("vertical", "垂直灵敏度")}${pf("horizontal", "水平灵敏度")}
            ${pf("ads_boost", "举枪灵敏度加成")}${pf("ads_vertical", "举枪瞄准垂直灵敏度")}${pf("ads_horizontal", "举枪瞄准水平灵敏度")}${pf("fov", "默认 FOV", "1")}
          </div>
          <span class="font-label-sm text-label-sm text-on-surface-variant">在游戏 设置 → 键盘&amp;鼠标 → 鼠标灵敏度 里照着填。不填也能用，只是建议里不会写出“从多少改到多少”。</span>
        </div>
      </div>

      <div class="flex flex-col gap-space-md">
        <div class="panel p-space-md flex flex-col gap-space-md" id="sec-restore">
          <div class="flex items-center gap-space-sm"><span class="font-label-sm text-label-sm text-error px-space-xs py-0.5 rounded bg-error-container/30">06 // EMERGENCY</span><span class="font-headline-md text-headline-md text-on-surface">系统一键还原</span></div>
          <span class="font-body-md text-body-md text-on-surface-variant">如果优化后遇到游戏异常，可以在这里撤销本软件做过的所有系统改动。</span>
          <div class="tile p-space-md flex flex-col gap-space-sm">
            <div class="flex items-center justify-between"><div class="flex flex-col"><span class="font-headline-sm text-headline-sm text-on-surface">系统优化还原</span>
            <span class="font-label-sm text-label-sm text-on-surface-variant">${s.restore.count ? "已修改：" + esc(s.restore.items.join("、")) : "目前没有任何改动"}</span></div>${icon("tune", "text-on-surface-variant text-[22px]")}</div>
            <button class="btn-ghost w-full" data-action="opt-restore" ${s.restore.count ? "" : "disabled"}>${icon("restart_alt", "text-[18px]")}一键还原系统默认设置</button>
          </div>
          <div class="tile p-space-md flex flex-col gap-space-sm">
            <div class="flex items-center justify-between"><div class="flex flex-col"><span class="font-headline-sm text-headline-sm text-on-surface">游戏画面配置</span>
            <span class="font-label-sm text-label-sm text-on-surface-variant">测试版不会修改游戏配置文件</span></div>${icon("history", "text-on-surface-variant text-[22px]")}</div>
          </div>
        </div>
        <div class="panel p-space-md flex flex-col gap-space-sm" id="sec-audit">
          <div class="flex items-center justify-between"><span class="font-headline-md text-headline-md text-on-surface">实际运行方式</span>${icon("gavel", "text-secondary text-[22px]")}</div>
          ${[["录制方式", "系统桌面复制（DDA），裁剪到游戏窗口"], ["编码", s.encoder || "自动选择显卡硬件编码"], ["帧率测量", "PresentMon（ETW，可选，需授权）"], ["游戏识别", "窗口标题 / 系统进程列表"], ["AI 调用", s.ai.configured ? prov.label + "（你自己的密钥）" : "未接入"], ["界面", "本地网页界面（不联网加载）"]]
            .map(([k, v]) => `<div class="flex items-center justify-between gap-space-sm py-1 border-b border-surface-container-highest last:border-0"><span class="font-body-md text-body-md text-on-surface-variant">${k}</span><span class="font-label-md text-label-md text-on-surface text-right">${esc(v)}</span></div>`).join("")}
          <div class="flex items-center gap-space-xs pt-space-xs">
            <button class="btn-quiet flex-1" data-action="export-diag">${icon("bug_report", "text-[18px]")}导出诊断包</button>
            <button class="btn-quiet flex-1" data-action="open-logs">${icon("description", "text-[18px]")}打开日志</button>
          </div>
        </div>
      </div>
    </section>`;
  };
  const fact = (a, b) => `<div class="flex items-center gap-space-sm">${icon("check_circle", "text-secondary text-[20px]")}<div class="flex flex-col"><span class="font-label-md text-label-md text-on-surface">${a}</span><span class="font-label-sm text-label-sm text-on-surface-variant">${b}</span></div></div>`;
  const modelOptions = (models, current) => {
    const list = Array.from(new Set([...(models || []), ...(current ? [current] : [])]));
    if (!list.length) return `<option value="">先点“测试连接”读取模型</option>`;
    return list.map((m) => `<option ${m === current ? "selected" : ""}>${esc(m)}</option>`).join("");
  };

  // =====================================================================
  // 交互
  // =====================================================================
  const actions = {
    "goto": (el) => goto(el.dataset.view, el.dataset.anchor),
    "modal-bg": (el, e) => { if (!e.target.closest("[data-stop]")) closeModal(); },
    "close-modal": () => closeModal(),
    "open-session": (el) => { mstate.session = el.dataset.name; mstate.event = null; goto("matches"); },
    "pick-session": (el) => { mstate.session = el.dataset.name; mstate.event = null; mstate.clip = null; renderView(true); },
    "pick-event": (el) => { mstate.event = Number(el.dataset.id); renderView(true); },
    "m-filter": (el) => { mstate[el.dataset.key] = el.dataset.val; mstate.session = null; renderView(); },
    "m-days": (el) => { mstate.days = Number(el.dataset.val); mstate.session = null; renderView(); },
    "import-video": async () => { const r = await call("import_video"); if (r && r.name) { mstate.session = r.name; toast("已导入，可以开始复盘", "ok"); renderView(); } },
    "make-clip": async (el) => {
      const eventId = el.dataset.event ? Number(el.dataset.event) : null;
      if (!eventId) mstate.event = 0;
      await call("make_clip", { name: mstate.session, t: Number(el.dataset.t), event_id: eventId });
      toast("正在生成片段，约需 10 秒", "info");
      renderView(true);
    },
    "make-thumbs": async () => { await call("make_thumbs", { name: mstate.session }); toast("正在生成缩略图", "info"); renderView(true); },
    "export-match": async () => { await call("export_match", { name: mstate.session }); toast("正在保存这一局的完整录像，几秒钟就好", "info"); },
    "ai-scan": async () => { await call("ai_scan", { name: mstate.session }); toast("AI 正在找阵亡画面，完成后会自动标记", "info"); },
    "ai-diagnose": async (el) => { await call("ai_diagnose", { id: Number(el.dataset.id) }); toast("AI 正在诊断这次阵亡", "info"); },
    "mark": async (el) => {
      const v = $("#player");
      const dt = pageData.detail;
      const ev = dt.events.find((e) => e.id === mstate.event);
      const clip = ev ? ev.clip : dt.last_clip;
      if (!v || !clip) return;
      const t = clip.t_start + v.currentTime;
      const r = await call("add_event", { name: mstate.session, t, kind: el.dataset.kind });
      if (el.dataset.kind === "death") mstate.event = r.id;
      toast("已标记 " + mmss(t), "ok");
      renderView(true);
    },
    "set-cause": async (el) => { await call("update_event", { id: Number(el.dataset.id), cause: el.dataset.cause, confirmed: true }); renderView(true); },
    "confirm-event": async (el) => { await call("update_event", { id: Number(el.dataset.id), confirmed: true }); toast("已确认", "ok"); renderView(true); },
    "del-event": async (el) => { await call("delete_event", { id: Number(el.dataset.id) }); mstate.event = null; renderView(true); },
    "pl-toggle": () => { const v = $("#player"); if (v) v.paused ? v.play() : v.pause(); },
    "pl-seek": (el) => { const v = $("#player"); if (v) v.currentTime = Math.max(0, v.currentTime + Number(el.dataset.d)); },
    "pl-rate": (el) => { const v = $("#player"); if (v) v.playbackRate = Number(el.dataset.r); $$("[data-action=pl-rate]").forEach((b) => b.classList.toggle("on", b === el)); },
    "open-folder": (el) => call("open_path", { path: el.dataset.path }),
    "open-logs": () => call("open_path", { kind: "logs" }),
    "open-url": (el) => call("open_url", { url: el.dataset.url }),
    "copy": async (el) => { await call("copy_text", { text: el.dataset.text }); toast(el.dataset.text.length > 60 ? "已复制到剪贴板" : "已复制：" + el.dataset.text, "ok"); },
    "gs-refresh": async () => { await call("gunsmith_refresh"); toast("开始抓取社区改枪码，约 1 分钟", "info"); },
    "gs-budget": async (el) => { await call("save_settings", { gunsmith_budget: el.dataset.val }); renderView(true); },
    "code-fb": async (el) => { await call("code_feedback", { code: el.dataset.code, ok: el.dataset.ok === "1" }); toast("已记录反馈", "ok"); renderView(true); },
    "code-adopt": async (el) => { await call("adopt_code", { code: el.dataset.code }); toast("已记录换装时间，之后会对比你的对枪胜率", "ok"); renderView(true); },
    "rec-adopt": async (el) => { await call("adopt_recommendation", { id: Number(el.dataset.id) }); toast("已记录，接下来 3 局自动复测", "ok"); renderView(true); },
    "opt-restore": async () => { const r = await call("optimize_restore"); toast(r.message, "ok"); renderView(true); },
    "opt-restore-one": async (el) => { const r = await call("optimize_restore", { key: el.dataset.key }); toast(r.message, "ok"); renderView(true); },
    "opt-apply": async (el) => { const r = await call("optimize_apply", { key: el.dataset.key }); toast(r.message, r.ok ? "ok" : "error"); renderView(true); },
    "opt-rescan": async () => { await call("rescan_hw"); toast("正在重新扫描", "info"); },
    "opt-measure": async () => { await call("measure_fps", { seconds: 60 }); toast("请在系统授权窗口点“是”，然后 10 秒内切回游戏，正常打 60 秒", "info"); renderView(true); },
    "opt-label": async (el) => { await call("save_settings", { next_measure_label: el.dataset.val }); renderView(true); },
    "reprobe": async () => { await call("reprobe_encoder"); toast("正在重新检测编码器", "info"); },
    "pick-folder": async () => { const r = await call("pick_folder"); if (r && r.path) { toast("录像保存位置已更改", "ok"); renderView(true); } },
    "pick-window": async () => {
      const list = await call("list_windows");
      modal(`<div class="flex items-center justify-between"><span class="font-headline-md text-headline-md text-on-surface">选择游戏窗口</span><button class="btn-quiet !p-1" data-action="close-modal">${icon("close")}</button></div>
        <span class="font-body-md text-body-md text-on-surface-variant">先打开游戏，再选中游戏本体的窗口：</span>
        <div class="flex flex-col gap-space-xs max-h-[360px] overflow-y-auto">${list.length ? list.map((w) => `<button class="tile p-space-sm text-left hover:bg-surface-container-high flex items-center justify-between gap-space-sm" data-action="choose-window" data-exe="${esc(w.exe)}">
          <span class="font-label-md text-label-md text-primary">${esc(w.exe || "未知进程")}</span><span class="font-body-sm text-body-sm text-on-surface-variant truncate">${esc(w.title)}</span><span class="font-label-sm text-label-sm text-outline">${w.width}×${w.height}</span></button>`).join("") : `<div class="empty">没有找到窗口（只在 Windows 上可用）</div>`}</div>`);
    },
    "choose-window": async (el) => { await call("add_game_process", { exe: el.dataset.exe }); closeModal(); toast("已记住游戏进程：" + el.dataset.exe, "ok"); renderView(true); },
    "ai-save-key": async () => {
      const k = $("#ai-key").value.trim();
      if (!k) return toast("请先粘贴 API Key", "error");
      await call("set_api_key", { key: k });
      $("#ai-key").value = "";
      toast("密钥已保存到本机凭据管理器", "ok");
      renderView(true);
    },
    "set-provider": async (el) => { await call("save_settings", { ai_provider: el.dataset.val }); toast("已切换 AI 平台", "ok"); renderView(true); },
    "ai-del-key": async () => { await call("delete_api_key"); toast("密钥已删除", "ok"); renderView(true); },
    "ai-test": async (el) => {
      const out = $("#ai-out");
      out.textContent = "正在测试…";
      try { const r = await api.call("ai_test", { kind: el.dataset.kind }); out.textContent = r.text; if (el.dataset.kind === "models" || el.dataset.kind === "vision") renderView(true); }
      catch (e) { out.textContent = "失败：" + e.message; }
    },
    "export-data": async () => { const r = await call("export_data"); toast("已导出到桌面：" + r.file, "ok"); },
    "reset-data": async () => {
      modal(`<span class="font-headline-md text-headline-md text-on-surface">重置本地数据库？</span>
        <span class="font-body-md text-body-md text-on-surface-variant">会删除所有标注、AI 判断、阵亡片段、改枪码缓存和帧率测量记录；原始录像文件不会删除，系统优化的备份也会保留（仍可一键还原）。这个操作无法撤销。</span>
        <div class="flex justify-end gap-space-xs"><button class="btn-quiet" data-action="close-modal">取消</button><button class="btn btn bg-error-container text-on-error-container" data-action="reset-confirm">确认重置</button></div>`);
    },
    "reset-confirm": async () => { await call("reset_data"); closeModal(); toast("本地数据已重置", "ok"); renderView(); },
    "export-diag": async () => { const r = await call("export_diagnostics"); toast("诊断包已保存到桌面：" + r.file, "ok"); },
  };
  const changeActions = {
    "set": null,
    "set-select": async (el) => { const v = el.value; const n = Number(v); await call("save_settings", { [el.dataset.key]: v !== "" && !Number.isNaN(n) && /^-?\d+(\.\d+)?$/.test(v) ? n : v }); toast("已保存", "ok"); },
    "set-input": async (el) => { await call("save_settings", { [el.dataset.key]: el.value.trim() }); toast("已保存", "ok"); },
    "set-list": async (el) => { await call("save_settings", { [el.dataset.key]: el.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean) }); toast("已保存", "ok"); },
    "set-check": async (el) => { await call("save_settings", { [el.dataset.key]: el.checked }); toast(el.checked ? "已开启" : "已关闭", "ok"); },
    "profile": async (el) => { await call("save_profile", { [el.dataset.key]: el.value === "" ? null : Number(el.value) }); toast("已保存", "ok"); },
    "opt-auto": async (el) => { await call("save_settings", { auto_optimize: el.checked }); toast(el.checked ? "已开启赛前自动优化" : "已关闭赛前自动优化", "ok"); },
    "m-select": (el) => { mstate[el.dataset.key] = el.value; mstate.session = null; renderView(); },
    "meta-set": async (el) => { await call("set_session_meta", { name: mstate.session, [el.dataset.key]: el.value }); toast("已保存", "ok"); renderView(true); },
    "meta-map": async (el) => { await call("set_session_meta", { name: mstate.session, map: el.value.trim() }); toast("已保存", "ok"); },
    "gs-filter": (el) => { gstate[el.dataset.key] = el.value; renderView(true); },
    "toggle-auto": async (el) => { await call("set_auto_record", { on: el.checked }); toast(el.checked ? "自动录制已开启" : "自动录制已关闭", "ok"); },
  };

  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-action]");
    if (!el) return;
    const name = el.dataset.action;
    if (el.tagName === "INPUT" || el.tagName === "SELECT") return;  // 输入框和开关走 change 事件
    if (el.hasAttribute("disabled")) return;
    if (name === "set") { call("save_settings", { [el.dataset.key]: Number(el.dataset.val) }).then(() => { toast("已保存", "ok"); renderView(true); }); return; }
    actions[name] && actions[name](el, e);
  });
  document.addEventListener("change", (e) => {
    const el = e.target.closest("[data-action]");
    if (!el || el.tagName === "BUTTON") return;
    const fn = changeActions[el.dataset.action];
    fn && fn(el, e);
  });
  document.addEventListener("keydown", (e) => {
    if (view !== "matches" || ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) return;
    const map = { d: "death", w: "duel_win", l: "duel_loss" };
    const kind = map[e.key.toLowerCase()];
    if (kind) { const b = $(`[data-action=mark][data-kind=${kind}]`); b && !b.disabled && b.click(); }
    if (e.key === " ") { e.preventDefault(); actions["pl-toggle"](); }
  });

  // ---------- 首次使用 ----------
  async function firstRun() {
    if (!state || !state.first_run) return;
    modal(`<span class="font-headline-lg text-headline-lg text-on-surface">欢迎使用三角洲复盘助手</span>
      <span class="font-body-md text-body-md text-on-surface-variant">打开总开关后，软件会在游戏启动时自动录制、关闭后整理录像。下面两项需要你同意一次，之后不再询问（都可以在设置里改）：</span>
      <label class="tile p-space-md flex items-start gap-space-sm cursor-pointer"><input type="checkbox" id="fr-opt" checked class="mt-1"/><span class="flex flex-col"><span class="font-headline-sm text-headline-sm text-on-surface">赛前自动优化</span>
        <span class="font-body-sm text-body-sm text-on-surface-variant">刷新率调到最高、打开游戏模式、关闭 Xbox 后台录制；打游戏期间电源计划切到高性能，退出游戏后改回。每项先备份，全部可一键还原。</span></span></label>
      <label class="tile p-space-md flex items-start gap-space-sm cursor-pointer"><input type="checkbox" id="fr-ai" class="mt-1"/><span class="flex flex-col"><span class="font-headline-sm text-headline-sm text-on-surface">AI 看图辅助分析</span>
        <span class="font-body-sm text-body-sm text-on-surface-variant">需要你自己的 AI 平台 API Key（DeepSeek、通义千问、豆包、智谱任选一家）。开启后，会把缩小的截图发给这家平台，用来找阵亡、判断死因。</span></span></label>
      <div class="flex justify-end"><button class="btn-primary" data-action="first-run-done">开始使用</button></div>`);
  }
  actions["first-run-done"] = async () => {
    await call("finish_first_run", { auto_optimize: $("#fr-opt").checked, ai_vision_enabled: $("#fr-ai").checked });
    closeModal();
    renderView(true);
  };

  // ---------- 启动 ----------
  async function boot() {
    try { api = await connect(); }
    catch (e) {
      $("#view").innerHTML = `<div class="panel p-space-xl empty">${icon("link_off", "text-[36px] text-error")}<div class="text-on-surface">${esc(e.message)}</div><div>请关闭后重新打开软件；如果反复出现，请导出诊断包。</div></div>`;
      return;
    }
    await refreshState();
    const start = location.hash.slice(1);
    goto(VIEWS[start] ? start : "overview");
    window.addEventListener("hashchange", () => { const v = location.hash.slice(1); if (VIEWS[v] && v !== view) goto(v); });
    firstRun();
    setInterval(refreshState, 1000);
  }
  boot();
})();
