/* 仅用于在浏览器里预览排版（地址带 ?mock=1 时才启用）。真实软件不会加载这里的示例数据。
   所有数据都是虚构的示例，不来自任何真实玩家。 */
(() => {
  const now = Date.now() / 1000;
  const thumbs = Array.from({ length: 24 }, (_, i) => ({ t: i * 10, src: "img/logo.svg" }));
  const events = [
    { id: 1, kind: "death", t: 755, cause: "recoil", source: "ai", confirmed: false, killer_weapon: "M4A1", distance_m: 45,
      clip: null, ai: { headline: "第 6 发开始准星上飘", process: "全自动连射第 6 发后准星持续上飘，没有及时下压。", process_tag: "失误",
        gear: "对方先命中 3 枪，你命中 2 枪。", gear_tag: "", awareness: "敌人从正面出现，你先看到了对方。", awareness_tag: "",
        advice: "中距离改点射，或把举枪瞄准垂直灵敏度调高 0.05。", confidence: 0.72, model: "deepseek-flash" } },
    { id: 2, kind: "death", t: 1458, cause: "unseen", source: "manual", confirmed: true, killer_weapon: "", distance_m: null, clip: null, ai: null },
    { id: 3, kind: "duel_win", t: 512, cause: "", source: "manual", confirmed: true },
    { id: 4, kind: "duel_loss", t: 755, cause: "", source: "manual", confirmed: true },
  ];
  const session = (name, title, label, ago, dur, deaths, w, l, result, mode) => ({
    name, title, label, started_at: now - ago, duration: dur, death_count: deaths, duel: { w, l }, size: dur * 2.6e6,
    result, mode, imported: false,
    deaths: name === "20261022_194210#12" ? events.filter((e) => e.kind === "death") : [],
  });
  const list = [
    session("20261022_194210#12", "长弓溪谷 · 烽火地带", "10-22 19:42 · 第 2 局", 1800, 1720, 2, 1, 2, "failed", "extraction"),
    session("20261022_194210#11", "零号大坝 · 烽火地带", "10-22 19:42 · 第 1 局", 4200, 1260, 0, 3, 0, "extracted", "extraction"),
    session("20261021_223012#9", "航天基地 · 烽火地带", "10-21 22:30 · 第 1 局", 90000, 900, 1, 0, 1, "failed", "extraction"),
  ];
  const code = (c, weapon, mode, uses, tags, score, reason, age, pop, invalid, author, title, budget) => ({
    code: c, weapon, mode, uses, tags, score, match_reason: reason, age_label: age, popularity: pop, invalid, author,
    url: "https://www.bilibili.com/", source_title: title, cover: "", fb_ok: 0, fb_fail: 0, adopted: false,
    budget_wan: budget, price_label: budget == null ? "" : `约 ${budget} 万`,
    bucket: budget == null ? "价格未标注" : budget < 15 ? "低于15万" : budget < 30 ? "15万~30万" : budget < 60 ? "30万~60万" : "60万以上",
  });
  const codes = [
    code("KC17突击步枪-烽火地带-6JLV0F406JIA63S9GM6T5", "KC17突击步枪", "烽火地带", ["中距离"], ["压枪"], 92, "补齐压枪短板", "6 天前", "94.8万 / 1.2万", 0, "示例UP主A", "KC17 中距离压枪方案", 24),
    code("M4A1突击步枪-烽火地带-8HNX2P712KMQA4Z7R2D1", "M4A1突击步枪", "烽火地带", ["近距离"], ["高机动", "近战"], 86, "适合中距离交战", "12 天前", "61.2万 / 1.5万", 1, "示例UP主B", "M4A1 开镜快的改法", 38),
    code("PKM通用机枪-全面战场-3KDQ9L510JFA5ZK1Y8R0", "PKM通用机枪", "全面战场", ["中距离"], ["压枪", "大容量"], 80, "社区热度", "20 天前", "28.4万 / 6.2千", 0, "示例UP主C", "PKM 全面战场", null),
    code("AK-12突击步枪-烽火地带-6H5RLPK0A2GV48FMPKMA1", "AK-12突击步枪", "烽火地带", ["中距离"], ["压枪", "性价比"], 77, "社区热度", "3 天前", "12.1万 / 3.4千", 0, "示例UP主D", "平民 AK-12", 11),
    code("Vector冲锋枪-烽火地带-6H5RLPK0A2GV48FMPKMB2", "Vector冲锋枪", "烽火地带", ["近距离"], ["近战", "高机动"], 74, "社区热度", "9 天前", "40.2万 / 9.1千", 0, "示例UP主E", "Vector 贴脸", 13.5),
    code("SR-25精确射手步枪-烽火地带-6H5RLPK0A2GV48FMPKMC3", "SR-25精确射手步枪", "烽火地带", ["远距离"], ["远距离"], 71, "社区热度", "15 天前", "9.8万 / 2.2千", 0, "示例UP主F", "SR-25 满改", 72),
    code("M7战斗步枪-烽火地带-6H5RLPK0A2GV48FMPKMD4", "M7战斗步枪", "烽火地带", ["中距离"], ["压枪", "满改"], 69, "社区热度", "18 天前", "22.5万 / 5.0千", 0, "示例UP主G", "M7 毕业", 55),
  ];
  const buckets = ["低于15万", "15万~30万", "30万~60万", "60万以上", "价格未标注"];
  const groups = buckets.map((b) => { const items = codes.filter((c) => c.bucket === b); return { label: b, count: items.length, items, active: true }; });
  const matchAdvice = {
    label: "10-22 19:42 · 第 2 局", deaths: 2, uncaused: 0,
    coach: "中距离对枪改 3～5 发点射，别一梭子扫到底\n转点前停 1 秒听脚步，确认方向再出掩体\n第二次阵亡前在空地停留太久，先找掩体再打药",
    items: [
      { id: "m12-recoil", title: "举枪瞄准垂直灵敏度上调 0.05", change_text: "当前 1.10 → 建议 1.15（+0.05）", status: "match",
        evidence: "这一局阵亡 2 次，其中 1 次是“压枪散了”（AI 判断，待你确认）。",
        how: "设置 → 键盘&鼠标 → 鼠标灵敏度 → 举枪瞄准垂直灵敏度改为 1.15（常用范围 0.9～1.2）。改完去靶场 25 米压一梭子，子弹不往上飘就对了。",
        verify: "下一局继续自动分析；同一个问题累计 5 次确认后进入长期方案，并自动复测效果。" },
      { id: "m12-unseen", title: "先听后动：用耳机和脚步声找人", change_text: "音频 + 转点习惯", status: "match",
        evidence: "这一局阵亡 2 次，其中 1 次是“没先看到人”（已确认）。",
        how: "戴耳机玩，在 设置 → 音频 里把输出设成耳机。转点前停 1 秒听脚步；没有信息时不要站在空地上死架一个点。",
        verify: "下一局继续自动分析；同一个问题累计 5 次确认后进入长期方案，并自动复测效果。" }],
  };
  const specs = [
    { group: "处理器", icon: "memory", rows: [{ k: "型号", v: "Intel Core i5-13600KF" }, { k: "核心 / 线程", v: "14 核 20 线程" }, { k: "基础频率", v: "3.5 GHz" }, { k: "缓存", v: "L2 20 MB · L3 24 MB" }] },
    { group: "显卡", icon: "developer_board", rows: [{ k: "型号", v: "NVIDIA GeForce RTX 4060 Ti（独立显卡，显存 8 GB）" }, { k: "驱动", v: "32.0.15.6614 · 2026-09-10" }] },
    { group: "内存", icon: "memory_alt", rows: [{ k: "总容量", v: "32 GB DDR5 · 2 根 · 双通道" }, { k: "运行频率", v: "6000 MT/s" }, { k: "插槽", v: "已用 2 / 共 4 个" }] },
    { group: "主板", icon: "developer_board", rows: [{ k: "型号", v: "示例主板 B760M" }, { k: "BIOS", v: "1.20（2026-03-01）" }] },
    { group: "硬盘", icon: "hard_drive", rows: [{ k: "示例 NVMe SSD 1TB", v: "固态硬盘 · NVMe · 932 GB" }, { k: "D 盘", v: "可用 382 GB / 共 931 GB" }] },
    { group: "显示器", icon: "desktop_windows", rows: [{ k: "显示器 1（主屏）", v: "2560×1440 · 当前 165 Hz · 最高 165 Hz" }] },
    { group: "声音设备", icon: "headphones", rows: [{ k: "设备 1", v: "Realtek Audio" }] },
    { group: "系统", icon: "settings", rows: [{ k: "系统", v: "Microsoft Windows 11 专业版（版本 10.0.26200，64 位）" }, { k: "电脑类型", v: "台式机" }, { k: "电源计划", v: "高性能" }] },
  ];
  const providers = [
    { id: "deepseek", label: "DeepSeek", base_url: "https://api.deepseek.com", vision: ["deepseek-flash"], text: ["deepseek-v4-pro", "deepseek-flash"],
      console: "https://platform.deepseek.com/api_keys", note: "能看图的是 deepseek-flash；deepseek-v4-pro 只能处理文字", has_key: true },
    { id: "qwen", label: "阿里云百炼（通义千问）", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", vision: ["qwen3-vl-plus"], text: ["qwen3.7-plus"],
      console: "https://bailian.console.aliyun.com/", note: "看图推荐 qwen3-vl-plus", has_key: false },
    { id: "doubao", label: "火山方舟（豆包）", base_url: "https://ark.cn-beijing.volces.com/api/v3", vision: ["doubao-seed-2-1-pro-260915"], text: ["doubao-seed-2-1-pro-260915"],
      console: "https://console.volcengine.com/ark", note: "模型名以方舟控制台“开通管理”里显示的为准", has_key: false },
    { id: "zhipu", label: "智谱（GLM）", base_url: "https://open.bigmodel.cn/api/paas/v4", vision: ["glm-4.5v"], text: ["glm-4.5v"],
      console: "https://open.bigmodel.cn/usercenter/apikeys", note: "看图用 glm-4.5v", has_key: false },
  ];
  const data = {
    state: () => ({
      version: "0.4.0", build: "2026.10.23", first_run: false, auto_record: true,
      game: { running: true, foreground: true, exe: "DeltaForceClient-Win64-Shipping.exe", title: "三角洲行动", width: 2560, height: 1440 },
      recorder: { status: "录制中（2560×1440，h264_nvenc）", recording: true, encoder: "h264_nvenc", encoder_busy: false, fps: "60", drop: "0",
        seg_time: "00:00:41.3", seg_progress: 0.69, capture: "DDA 桌面复制 · 裁剪到游戏窗口", ffmpeg_ok: true, error: "", output_changes: 2, failures: 0 },
      storage: { used_bytes: 4.2e9, free_bytes: 382e9, path: "D:\\DeltaReplay\\Recordings" },
      gpu: { name: "NVIDIA GeForce RTX 4060 Ti", temp_c: 58, vram_used_mb: 3900, vram_total_mb: 8192 },
      ai: { configured: true, provider: "deepseek", text_model: "deepseek-v4-pro", vision_model: "deepseek-flash", enabled: true, vision_ok: true, error: "" },
      jobs: [],
    }),
    dashboard: () => ({
      pre: { applied: 3, auto: true, frame_p99: 9.8, frametimes: [7.1, 7.0, 7.4, 6.9, 7.2, 8.3, 7.0, 6.8, 7.1, 7.3, 6.9, 7.0],
        fps_gain: { avg_before: 142, avg_after: 156, low_before: 81, low_after: 96 },
        spec: "Intel Core i5-13600KF · RTX 4060 Ti 8GB · 32GB DDR5 双通道 · 2560×1440 165Hz",
        checks: [{ label: "显示器 165Hz 刷新率", status: "applied", value: "已改回最高" }, { label: "Windows 游戏模式", status: "ok", value: "已开启" },
          { label: "电源计划：高性能", status: "applied", value: "已切换" }, { label: "内存 DDR5 6000 MT/s", status: "ok", value: "已开 XMP" },
          { label: "Xbox 后台录制", status: "applied", value: "已关闭" }] },
      mid: { spec: "原生分辨率 · 60FPS · 自动码率", segment_label: "60 秒", live: true, retention_label: "3 天" },
      post: { sessions_week: 3, deaths: 3, ai_enabled: true, unconfirmed: 1, duel: { wins: 4, losses: 3, rate: 4 / 7 } },
      recent: { name: "20261022_194210#12", label: "10-22 19:42 · 第 2 局", title: "长弓溪谷 · 烽火地带", result: "failed", started_at: now - 1800, duration: 1720, deaths: 2, duel_total: 3, duel_rate: 1 / 3 },
      match_advice: matchAdvice,
      diagnosis: { text: "最近 6 次已确认的阵亡里，4 次是“压枪散了”，集中在 20~50 米的对枪。", n: 6 },
      min_deaths: 5,
      recommendations: [
        { id: 1, title: "举枪瞄准垂直灵敏度上调 0.05", change_text: "当前 1.05 → 建议 1.10（+0.05）", status: "adopted", progress: "（第 1/3 局）",
          evidence: "最近 6 次已确认阵亡中 4 次（67%）是“压枪散了”。", how: "设置 → 键盘&鼠标 → 鼠标灵敏度 → 举枪瞄准垂直灵敏度改为 1.10（上限 1.2）。",
          verify: "再打 3 局后对比“压枪散了”的占比，没有下降就提示改回。" }],
      verified: [{ status: "kept", text: "鼠标灵敏度下调 5% 后，中距离“甩过头”的阵亡从 4 次降到 1 次，已保留。" }],
      gunsmith_top: codes[0],
    }),
    matches: () => ({ list, maps: ["长弓溪谷", "零号大坝", "航天基地"], retention_label: "3 天",
      kpis: { sessions: 3, deaths: 3, wins: 4, losses: 3, duel_total: 7, duel_rate: 4 / 7, top_cause: "recoil", top_cause_share: 0.5, unconfirmed: 1 } }),
    session_detail: ({ name }) => ({ name, label: "10-22 19:42 · 第 2 局", match_id: 12, open: false, folder: "D:\\DeltaReplay\\Recordings\\20261022_194210", spec: "2560×1440 · 60FPS · NVENC", total_duration: 1720,
      thumbs, thumb_step: 10, thumbs_pending: false, events, ai_ready: true, last_clip: null,
      meta: { mode: "extraction", map: "长弓溪谷", result: "failed" }, match_advice: matchAdvice }),
    gunsmith: () => ({ top: codes.slice(0, 3), rest: codes.slice(3), groups,
      budgets: groups.filter((g) => g.label !== "价格未标注").map((g) => ({ label: g.label, count: g.count })),
      weapons: codes.map((c) => c.weapon), uses: ["近距离", "中距离", "远距离"], budget: "",
      crawl: { last: now - 3600, count: 37, videos: 21, running: false },
      profile: { sample_n: 6, distance_label: "20~50 米", distance_note: "AI 读出的 5 次交战距离里占 60%", issue_label: "压枪散了", issue_note: "6 次已确认阵亡中 4 次（67%）",
        direction: "优先压枪稳定", direction_note: "后坐力低、枪身稳的改法加分" },
      efficacy: [] }),
    optimizer: () => ({ pass: 5, total: 7, scanned_at: now - 120, auto: true, presentmon: true, measuring: false, next_label: "after",
      profile_text: "鼠标 1.18 · 垂直/水平 5.00/5.00 · 举枪 0.80/1.10/0.80", mdv: "",
      measure: { before: { avg: 142, low1: 81, p99: 12.3, created: now - 86400, series: [7.2, 7.4, 9.8, 7.1, 13.2, 7.0, 7.3, 11.9, 7.1, 7.2] },
        after: { avg: 156, low1: 96, p99: 10.4, created: now - 600, series: [6.5, 6.6, 7.0, 6.4, 8.1, 6.5, 6.6, 7.2, 6.5, 6.4] } },
      specs, spec_summary: "Intel Core i5-13600KF · RTX 4060 Ti 8GB · 32GB DDR5 双通道 · 2560×1440 165Hz",
      items: [
        { key: "refresh", title: "显示器刷新率", icon: "desktop_windows", status: "applied", detail: "显示器 1 已从 60Hz 改回最高 165Hz。", footer: "2560×1440 · 165Hz", applied: true },
        { key: "power", title: "系统电源计划", icon: "bolt", status: "applied", detail: "打游戏期间切换到“高性能”，关掉游戏后改回“平衡”。", footer: "原计划：平衡", applied: true },
        { key: "gamemode", title: "Windows 游戏模式", icon: "sports_esports", status: "ok", detail: "已开启。", footer: "AutoGameModeEnabled = 1" },
        { key: "driver", title: "显卡驱动版本", icon: "memory", status: "info", detail: "NVIDIA 驱动发布于 5 个月前，可以更新。", footer: "RTX 4060 Ti · 8 GB", link: "https://www.nvidia.cn/geforce/drivers/", link_label: "官方下载" },
        { key: "xmp", title: "内存 XMP / EXPO", icon: "developer_board", status: "ok", detail: "运行在 6000 MT/s，双通道。", footer: "32 GB DDR5 · 2 根" },
        { key: "background", title: "后台高占用进程", icon: "hourglass_top", status: "info", detail: "浏览器占用 CPU 18%，建议进对局前关掉不用的标签页。", footer: "只提示，不会强制关闭" },
        { key: "gamebar", title: "Xbox 后台录制", icon: "videocam_off", status: "applied", detail: "已关闭，避免与本软件重复录制。", footer: "HistoricalCaptureEnabled = 0", applied: true },
      ] }),
    settings: () => ({ build: "2026.10.23", fingerprint: "7f4a91c0…e129", app_root: "D:\\fupan-assistant", encoder: "h264_nvenc", encoders: ["h264_nvenc", "hevc_nvenc", "h264_amf", "h264_qsv", "h264_mf"],
      storage: { path: "D:\\DeltaReplay\\Recordings", free_bytes: 382e9 },
      config: { retention_days: 3, framerate: 60, bitrate_mbps: 0, encoder: "auto", monitor_index: -1, ai_provider: "deepseek", ai_base_url: "https://api.deepseek.com",
        ai_text_model: "deepseek-v4-pro", ai_vision_model: "deepseek-flash", live_analyze: true, ai_daily_token_limit: 1000000, ai_vision_enabled: true,
        game_title_keywords: ["三角洲行动", "Delta Force"], game_process_names: [], profile: { dpi: 800, mouse: 1.18, vertical: 5, horizontal: 5, ads_boost: 0.8, ads_vertical: 1.1, ads_horizontal: 0.8, fov: 90 } },
      ai: { configured: true, key_mask: "已保存（尾号 abcd）", providers, provider: "deepseek", models: ["deepseek-flash", "deepseek-v4-pro"], used_today: 18240, last_output: "",
        vision_status: "看图自检通过：deepseek-flash 能看到图片" },
      db: { events: 4, codes: 37, measurements: 2, size: 286720 },
      restore: { count: 3, items: ["显示器刷新率", "电源计划", "Xbox 后台录制"] } }),
  };
  window.MockBackend = {
    call: async (method, args = {}) => {
      await new Promise((r) => setTimeout(r, 60));
      if (data[method]) return data[method](args);
      if (method === "ai_test") return { text: "（预览模式）" };
      if (method === "optimize_restore" || method === "optimize_apply") return { ok: true, message: "（预览模式）" };
      if (method === "list_windows") return [{ exe: "DeltaForceClient-Win64-Shipping.exe", title: "三角洲行动", width: 2560, height: 1440 }];
      if (method === "export_data" || method === "export_diagnostics") return { file: "preview.zip" };
      if (method === "add_event") return { id: 99 };
      return {};
    },
  };
})();
