/**
 * Dashboard 前端
 *
 * 与旧 GUI 的差别：
 * - 状态轮询交给 Rust（`get_status` 内部有 700ms TTL 缓存 + 进程名粗筛），
 *   前端只管渲染。旧实现每 2.5s 在 Python 里扫全表进程 3.2s，永远追不上自己。
 * - 日志只在**当前页可见时**拉取，且只取尾部 N 行；内容未变就不碰 DOM。
 * - 窗口不可见时完全停止轮询（`visibilitychange`），托盘常驻时零开销。
 */

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import "./theme.css";
import "./dashboard.css";

interface RecognitionInfo {
  text: string;
  original: string;
  latency: number;
  hotwords: string[];
  at: number;
}

interface StatusSnapshot {
  serverRunning: boolean;
  clientRunning: boolean;
  portOpen: boolean;
  serverPids: number[];
  clientPids: number[];
  activeModel: string;
  lastRecognition: RecognitionInfo | null;
  probeMs: number;
}

const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const win = getCurrentWindow();

// 把前端错误回传到外壳 stderr（WebView console 在这里不可见）
function uiLog(level: string, message: string) {
  void invoke("ui_log", { level, message }).catch(() => {});
}
window.addEventListener("error", (e) => {
  uiLog("error", `dashboard: ${e.message} @ ${e.filename}:${e.lineno}`);
});
window.addEventListener("unhandledrejection", (e) => {
  uiLog("error", `dashboard unhandled: ${String(e.reason)}`);
});

const MODEL_LABELS: Record<string, string> = {
  qwen_asr: "Qwen3-ASR-1.7B",
  qwen_asr_0_6b: "Qwen3-ASR-0.6B",
  fun_asr_nano: "Fun-ASR-Nano",
  sensevoice: "SenseVoice",
  paraformer: "Paraformer",
};

// ============================================================
// 页面切换
// ============================================================

const PAGE_TITLES: Record<string, string> = {
  home: "总览",
  client: "客户端日志",
  server: "服务端日志",
};

let currentPage = "home";

function showPage(id: string) {
  currentPage = id;
  document.querySelectorAll<HTMLElement>(".page").forEach((p) => {
    p.classList.toggle("on", p.dataset.page === id);
  });
  document.querySelectorAll<HTMLElement>(".nav-item").forEach((b) => {
    b.classList.toggle("on", b.dataset.page === id);
  });
  $("page-title").textContent = PAGE_TITLES[id] ?? id;
  // 切到日志页立刻拉一次并滚到底，不等下个轮询周期
  if (id === "client" || id === "server") void refreshLog(id, true);
}

document.querySelectorAll<HTMLElement>(".nav-item").forEach((b) => {
  b.addEventListener("click", () => showPage(b.dataset.page!));
});

// ============================================================
// 窗口控制
// ============================================================

$("btn-min").addEventListener("click", () => void win.minimize());
$("btn-close").addEventListener("click", () => void invoke("hide_dashboard"));
$("btn-tray").addEventListener("click", () => void invoke("hide_dashboard"));

document.querySelectorAll<HTMLElement>("[data-open]").forEach((b) => {
  b.addEventListener("click", () => {
    void invoke("open_path", { target: b.dataset.open });
  });
});

// ============================================================
// 面板内提示
// ============================================================

function note(msg: string, isErr = false) {
  const el = document.createElement("div");
  el.className = `dnote${isErr ? " err" : ""}`;
  el.textContent = msg;
  $("dash-notes").appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

function setFoot(msg: string) {
  $("foot-msg").textContent = msg;
}

// ============================================================
// 后端操作
// ============================================================

async function run(btn: HTMLButtonElement, cmd: string, args?: Record<string, unknown>) {
  btn.classList.add("busy");
  btn.disabled = true;
  setFoot("执行中…");
  try {
    const msg = await invoke<string>(cmd, args);
    note(msg);
    setFoot(msg);
  } catch (e) {
    const msg = String(e);
    note(msg, true);
    setFoot(`失败：${msg}`);
  } finally {
    btn.classList.remove("busy");
    btn.disabled = false;
    // 操作后立刻刷新一次状态
    void tick(true);
  }
}

$("btn-start").addEventListener("click", (e) =>
  run(e.currentTarget as HTMLButtonElement, "start_backend"),
);
$("btn-restart").addEventListener("click", (e) =>
  run(e.currentTarget as HTMLButtonElement, "restart_backend"),
);
$("btn-stop").addEventListener("click", (e) =>
  run(e.currentTarget as HTMLButtonElement, "stop_backend"),
);
$("btn-model").addEventListener("click", (e) => {
  const model = ($("sel-model") as HTMLSelectElement).value;
  run(e.currentTarget as HTMLButtonElement, "switch_model", { model });
});

// ============================================================
// 状态渲染
// ============================================================

function setPill(id: string, state: "ok" | "err" | "warn" | "off") {
  const el = $(id);
  el.className = `pill${state === "off" ? "" : " " + state}`;
}

let lastRecoAt = -1;
let modelInit = false;

function render(s: StatusSnapshot) {
  // 服务状态
  const svcOn = s.serverRunning && s.portOpen;
  setPill("p-svc", svcOn ? "ok" : s.serverRunning ? "warn" : "err");
  $("t-svc").textContent = svcOn
    ? "运行中"
    : s.serverRunning
      ? "启动中"
      : "未运行";

  setPill("p-port", s.portOpen ? "ok" : "err");
  $("t-port").textContent = s.portOpen ? "已监听" : "未监听";

  setPill("p-cli", s.clientRunning ? "ok" : "err");
  $("t-cli").textContent = s.clientRunning ? "已连接" : "未运行";

  // 端口通但进程没认出来 —— 说明探测逻辑漏了，如实说出来，
  // 不要让用户看着「未运行」却明明能用
  if (s.portOpen && !s.serverRunning) {
    $("t-svc").textContent = "运行中(外部)";
    setPill("p-svc", "ok");
  }

  $("live-dot").classList.toggle("live", svcOn && s.clientRunning);

  // 模型
  const label = MODEL_LABELS[s.activeModel] ?? s.activeModel ?? "未知";
  $("t-model").textContent = label;
  if (!modelInit && s.activeModel) {
    ($("sel-model") as HTMLSelectElement).value = s.activeModel;
    modelInit = true;
  }

  // 最近识别：仅在时间戳变化时更新 DOM
  const r = s.lastRecognition;
  if (r && r.at !== lastRecoAt) {
    lastRecoAt = r.at;
    const res = $("t-result");
    res.textContent = r.text;
    res.classList.remove("empty");
    $("m-lat").textContent = `${r.latency.toFixed(2)}s`;
    $("m-at").textContent = new Date(r.at).toLocaleTimeString("zh-CN");

    const tags = $("t-tags");
    tags.textContent = "";
    for (const h of r.hotwords.slice(0, 8)) {
      const t = document.createElement("span");
      t.className = "tag";
      t.textContent = h;
      tags.appendChild(t);
    }
  }

  // 带上采样时刻：万一轮询又停了，用户能一眼看出「这是几分钟前的数据」，
  // 而不是把过期快照当成当前状态
  const now = new Date();
  const hhmmss = now.toLocaleTimeString("zh-CN", { hour12: false });
  $("foot-probe").textContent = `${hhmmss} · probe ${s.probeMs}ms`;
}

// ============================================================
// 日志
// ============================================================

interface LogTail {
  text: string;
  file: string;
  size: number;
  mtime: number;
  tookMs: number;
}

/** 已知的 mtime，交给后端做「没变就别读」的判断 */
const logMtime: Record<string, number | undefined> = {};
/** 正在请求中，避免轮询叠加 */
const logBusy: Record<string, boolean> = {};

const LEVEL_RE = /\b(ERROR|CRITICAL|Traceback|WARNING|WARN|DEBUG|INFO)\b/;

function classify(line: string): string {
  const m = LEVEL_RE.exec(line);
  if (!m) return "";
  switch (m[1].toUpperCase()) {
    case "ERROR":
    case "CRITICAL":
    case "TRACEBACK":
      return "err";
    case "WARNING":
    case "WARN":
      return "warn";
    case "DEBUG":
      return "dbg";
    default:
      return "info";
  }
}

/**
 * 渲染日志。
 *
 * 用 innerHTML 一次性写入，而不是 createElement 逐行 append —— 300 行
 * 逐个建节点在 WebView 里明显更慢。行文本必须转义，否则日志里的
 * `<` 会被当成标签（日志内容不可信，里面有用户语音识别结果）。
 */
function renderLog(box: HTMLElement, text: string) {
  if (!text) {
    box.textContent = "（日志为空）";
    return;
  }
  const html = text
    .split("\n")
    .map((line) => {
      const cls = classify(line);
      const safe = line
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
      return `<div class="l${cls ? " " + cls : ""}">${safe || "&nbsp;"}</div>`;
    })
    .join("");
  box.innerHTML = html;
}

const loggedLogRender: Record<string, boolean> = {};

async function refreshLog(which: "client" | "server", force = false) {
  if (logBusy[which]) return;
  logBusy[which] = true;
  try {
    const tail = await invoke<LogTail | null>("read_log_tail", {
      which,
      maxLines: 300,
      knownMtime: force ? null : logMtime[which],
    });
    // null = 文件未变化，无需重绘
    if (!tail) return;
    logMtime[which] = tail.mtime;

    const box = $(`log-${which}`);
    const follow = ($(`follow-${which}`) as HTMLInputElement).checked;
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;

    const tRender = performance.now();
    renderLog(box, tail.text);

    const meta = $(`meta-${which}`);
    if (meta) {
      const mb = (tail.size / 1048576).toFixed(1);
      meta.textContent = tail.file
        ? `${tail.file} · ${mb} MB · 读取 ${tail.tookMs}ms`
        : "";
    }

    // 首次渲染各页时上报一次耗时，便于确认没有性能回退
    if (!loggedLogRender[which]) {
      loggedLogRender[which] = true;
      const lines = tail.text ? tail.text.split("\n").length : 0;
      uiLog(
        "info",
        `${which} 日志首次渲染: ${tail.file} ${(tail.size / 1048576).toFixed(1)}MB ` +
          `后端读取 ${tail.tookMs}ms, ${lines} 行, 渲染 ${Math.round(
            performance.now() - tRender,
          )}ms`,
      );
    }

    if (follow && (atBottom || force)) box.scrollTop = box.scrollHeight;
  } catch (e) {
    $(`log-${which}`).textContent = `读取日志失败：${e}`;
  } finally {
    logBusy[which] = false;
  }
}

document.querySelectorAll<HTMLElement>("[data-refresh]").forEach((b) => {
  b.addEventListener("click", () => {
    const w = b.dataset.refresh as "client" | "server";
    void refreshLog(w, true); // 强制重读并滚到底
  });
});

// ============================================================
// 轮询
// ============================================================

let timer: number | null = null;

let loggedFirstTick = false;
/** 上一次的状态指纹，仅用于在状态真正变化时留一行日志 */
let lastFingerprint = "";

async function tick(force = false) {
  if (document.hidden && !force) return;
  try {
    const s = await invoke<StatusSnapshot>("get_status");
    render(s);

    const fp = `${s.serverRunning}|${s.clientRunning}|${s.portOpen}`;
    if (!loggedFirstTick) {
      loggedFirstTick = true;
      lastFingerprint = fp;
      uiLog(
        "info",
        `dashboard 首次状态: server=${s.serverRunning} client=${s.clientRunning} ` +
          `port=${s.portOpen} model=${s.activeModel || "-"} probe=${s.probeMs}ms`,
      );
    } else if (fp !== lastFingerprint) {
      // 状态变化留痕：轮询是否真的在跑，看这里就知道
      lastFingerprint = fp;
      uiLog(
        "info",
        `状态变化: server=${s.serverRunning} client=${s.clientRunning} port=${s.portOpen}`,
      );
    }
  } catch (e) {
    setFoot(`状态读取失败：${e}`);
    uiLog("error", `get_status 失败: ${e}`);
  }
  // 日志只在对应页面打开时刷新，且后端用 mtime 短路，
  // 文件没变化时这一步几乎零成本
  if (currentPage === "client" || currentPage === "server") {
    void refreshLog(currentPage as "client" | "server");
  }
}

function startPolling() {
  if (timer !== null) return;
  void tick();
  timer = window.setInterval(() => void tick(), 1500);
}

function stopPolling() {
  if (timer !== null) {
    clearInterval(timer);
    timer = null;
  }
}

/**
 * 何时轮询、何时停。
 *
 * 只靠 `visibilitychange` 是不够的：Tauri 里 `win.hide()` / `show()`
 * 不保证成对触发它，一旦漏掉一次「变可见」，轮询就再也起不来，
 * 面板会永久停在旧数据上（表现为「服务未运行，但其实能用」）。
 *
 * 所以以 Tauri 的窗口事件为主、`visibilitychange` 为辅，
 * 并且每次回到前台都立刻强制刷新一次，不等下个周期。
 */
function resume() {
  startPolling();
  void tick(true);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopPolling();
  else resume();
});

// 后端在 show_dashboard() 里发出的权威信号
void listen("dashboard-shown", resume);

// 获得焦点也刷一次（用户点回面板时立刻看到最新状态）
void win.onFocusChanged(({ payload: focused }) => {
  if (focused) resume();
});

// 支持 ?page=client 直接打开某一页（调试与验证用）
const wantPage = new URLSearchParams(location.search).get("page");
if (wantPage && PAGE_TITLES[wantPage]) {
  showPage(wantPage);
}

startPolling();
