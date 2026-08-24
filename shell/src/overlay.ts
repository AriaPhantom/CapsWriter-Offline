/**
 * Toast 浮层前端
 *
 * 性能约定（对应旧 Tk 实现的 O(n²) 缺陷）：
 *
 * - 流式期间**只做文本追加**：`textNode.appendData(delta)`。浏览器对
 *   纯文本节点追加是增量重排，不会像 Tk 那样每次从头数行数
 *   （旧实现 `count('1.0','end','displaylines')` 是 O(n)，每 chunk 一次 → O(n²)）。
 * - **Markdown 只在收尾解析一次**，流式期间不解析。中途解析既昂贵又会
 *   因为语法不完整而闪烁。
 * - 高度自适应交给 CSS（`max-height` + `overflow-y:auto`），
 *   不做任何 JS 测量与窗口 geometry 计算。
 * - 倒计时进度条用 `transform: scaleX()`，纯合成器动画，不占主线程。
 */

import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { marked } from "marked";
import "./theme.css";
import "./overlay.css";

marked.setOptions({ gfm: true, breaks: true });

// 把前端错误回传到外壳 stderr。WebView 的 console 在这里看不到，
// 没有这条通道时前端一旦抛错就完全静默。
function uiLog(level: string, message: string) {
  void invoke("ui_log", { level, message }).catch(() => {
    /* 连 invoke 都不通时无处可报，只能放弃 */
  });
}

window.addEventListener("error", (e) => {
  uiLog("error", `${e.message} @ ${e.filename}:${e.lineno}`);
});
window.addEventListener("unhandledrejection", (e) => {
  uiLog("error", `unhandled rejection: ${String(e.reason)}`);
});

uiLog("info", "overlay 脚本已加载");

type NotifyLevel = "info" | "success" | "warn" | "error";

interface ToastStyle {
  width: number;
  height: number;
  font_family: string;
  font_size: number;
  fg: string;
  bg: string;
  duration: number;
  editable: boolean;
  markdown: boolean;
  role: string;
}

type UiEvent =
  | { kind: "toastOpen"; id: string; style: ToastStyle; streaming: boolean; text: string }
  | { kind: "toastDelta"; id: string; delta: string }
  | { kind: "toastFinish"; id: string }
  | { kind: "toastClose"; id: string }
  | { kind: "recordingState"; active: boolean; elapsed: number }
  | { kind: "notify"; message: string; level: NotifyLevel; duration: number };

const stack = document.getElementById("stack") as HTMLDivElement;
const rec = document.getElementById("rec") as HTMLDivElement;
const recTime = document.getElementById("rec-time") as HTMLSpanElement;
const notes = document.getElementById("notes") as HTMLDivElement;

// ============================================================
// Toast 实例
// ============================================================

class Toast {
  readonly id: string;
  readonly el: HTMLDivElement;
  private body: HTMLDivElement;
  private textNode: Text;
  private caret: HTMLSpanElement | null = null;
  private progress: HTMLDivElement;
  private style: ToastStyle;
  private raw = "";
  private timer: number | null = null;
  private hovered = false;
  private done = false;

  constructor(id: string, style: ToastStyle, streaming: boolean, initial: string) {
    this.id = id;
    this.style = style;
    this.raw = initial;

    const el = document.createElement("div");
    el.className = "toast";
    el.style.setProperty("--toast-bg", style.bg);
    el.style.setProperty("--toast-fg", style.fg);
    el.style.setProperty("--toast-fs", `${style.font_size}px`);
    if (style.font_family) {
      el.style.setProperty("--toast-ff", `"${style.font_family}", var(--font-ui)`);
    }

    // 标题栏
    const head = document.createElement("div");
    head.className = "toast-head";
    if (style.role) {
      const role = document.createElement("span");
      role.className = "toast-role";
      role.textContent = style.role;
      head.appendChild(role);
    }
    if (streaming) {
      const sp = document.createElement("div");
      sp.className = "toast-spinner";
      head.appendChild(sp);
    }
    const close = document.createElement("button");
    close.className = "toast-close";
    close.textContent = "✕";
    close.title = "关闭 (Esc)";
    close.addEventListener("click", () => this.dismiss());
    head.appendChild(close);

    // 正文
    const body = document.createElement("div");
    body.className = "toast-body";
    this.textNode = document.createTextNode(initial);
    body.appendChild(this.textNode);
    if (streaming) {
      this.caret = document.createElement("span");
      this.caret.className = "caret";
      body.appendChild(this.caret);
    }

    const progress = document.createElement("div");
    progress.className = "toast-progress";
    progress.style.transform = "scaleX(0)";

    el.append(head, body, progress);
    this.el = el;
    this.body = body;
    this.progress = progress;

    // 悬停暂停倒计时（沿用旧行为）
    el.addEventListener("mouseenter", () => {
      this.hovered = true;
      this.clearTimer();
      this.progress.style.transition = "none";
      this.progress.style.transform = "scaleX(1)";
      setInteractive(true);
    });
    el.addEventListener("mouseleave", () => {
      this.hovered = false;
      setInteractive(false);
      if (this.done) this.startCountdown();
    });
    // 滚轮在正文内滚动，不冒泡到系统
    body.addEventListener("wheel", (e) => e.stopPropagation(), { passive: true });

    stack.appendChild(el);
    applyWidth(style.width);

    // 非流式：直接定稿
    if (!streaming) {
      this.finish();
    }
  }

  /** 流式追加。热路径，必须保持 O(delta) */
  append(delta: string) {
    if (this.done) return;
    this.raw += delta;
    this.textNode.appendData(delta);
    // 贴着底部时自动跟随；用户手动上滚后就不再打断
    const nearBottom =
      this.body.scrollHeight - this.body.scrollTop - this.body.clientHeight < 48;
    if (nearBottom) this.body.scrollTop = this.body.scrollHeight;
  }

  /** 流式结束：一次性 Markdown 定稿 + 启动倒计时 */
  finish() {
    if (this.done) return;
    this.done = true;

    this.caret?.remove();
    this.caret = null;
    this.el.querySelector(".toast-spinner")?.remove();

    if (this.style.markdown && this.raw.trim()) {
      try {
        const html = marked.parse(this.raw) as string;
        this.body.textContent = "";
        this.body.className = "toast-body md";
        // marked 输出的是我们自己的 LLM 文本渲染结果，
        // 且 CSP 禁止内联脚本执行，这里用 innerHTML 是可接受的
        this.body.innerHTML = html;
      } catch {
        // 解析失败就保留纯文本，不让一次异常吞掉整段内容
      }
    }

    if (this.style.editable) {
      this.body.classList.add("editable");
      this.body.contentEditable = "true";
      this.body.spellcheck = false;
    }

    if (!this.hovered) this.startCountdown();
  }

  private startCountdown() {
    this.clearTimer();
    const ms = this.style.duration > 0 ? this.style.duration : 3000;
    // 进度条从满到空，纯 transform 动画
    this.progress.style.transition = "none";
    this.progress.style.transform = "scaleX(1)";
    // 强制一帧，让 transition 生效
    requestAnimationFrame(() => {
      this.progress.style.transition = `transform ${ms}ms linear`;
      this.progress.style.transform = "scaleX(0)";
    });
    this.timer = window.setTimeout(() => this.dismiss(), ms);
  }

  private clearTimer() {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  dismiss() {
    this.clearTimer();
    this.el.classList.add("leaving");
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      this.el.remove();
      toasts.delete(this.id);
      // 必须等后端记账完成再判空闲，否则 overlay_idle 可能先到，
      // 此时 Rust 侧仍认为有 toast 打开，浮层就不会隐藏。
      void invoke("note_toast_closed", { id: this.id }).then(maybeIdle, maybeIdle);
    };
    this.el.addEventListener("animationend", done, { once: true });
    // 兜底：动画事件没来也要清掉
    setTimeout(done, 320);
  }
}

const toasts = new Map<string, Toast>();

function applyWidth(w: number) {
  // 0-1 视为屏幕比例，>1 视为像素（与旧配置语义一致）
  const css = w > 0 && w <= 1 ? `${w * 100}vw` : `${w}px`;
  stack.style.setProperty("--toast-w", css);
}

// ============================================================
// 鼠标接管：只有需要交互时才让浮层吃鼠标事件
// ============================================================

let interactiveRefs = 0;
let interactiveState = false;

function setInteractive(on: boolean) {
  interactiveRefs = Math.max(0, interactiveRefs + (on ? 1 : -1));
  const want = interactiveRefs > 0;
  if (want !== interactiveState) {
    interactiveState = want;
    void invoke("set_overlay_interactive", { interactive: want });
  }
}

function maybeIdle() {
  if (toasts.size === 0 && !recActive && notes.childElementCount === 0) {
    void invoke("overlay_idle");
  }
}

// ============================================================
// 录音指示
// ============================================================

let recActive = false;
let recStart = 0;
let recRaf = 0;

function tickRec() {
  if (!recActive) return;
  const s = (performance.now() - recStart) / 1000;
  recTime.textContent = `${s.toFixed(1)}s`;
  recRaf = requestAnimationFrame(tickRec);
}

function setRecording(active: boolean, elapsed: number) {
  recActive = active;
  if (active) {
    recStart = performance.now() - elapsed * 1000;
    rec.classList.add("on");
    cancelAnimationFrame(recRaf);
    tickRec();
  } else {
    rec.classList.remove("on");
    cancelAnimationFrame(recRaf);
    maybeIdle();
  }
}

// ============================================================
// 通知气泡
// ============================================================

function notify(message: string, level: NotifyLevel, duration: number) {
  const el = document.createElement("div");
  el.className = `note ${level}`;
  const icon = document.createElement("span");
  icon.className = "note-icon";
  const txt = document.createElement("span");
  txt.textContent = message;
  el.append(icon, txt);
  notes.appendChild(el);

  setTimeout(() => {
    el.classList.add("leaving");
    const done = () => {
      el.remove();
      maybeIdle();
    };
    el.addEventListener("animationend", done, { once: true });
    setTimeout(done, 260);
  }, Math.max(600, duration));
}

// ============================================================
// 事件分发
// ============================================================

void listen<UiEvent>("ui-event", ({ payload }) => {
  switch (payload.kind) {
    case "toastOpen": {
      // 同 id 重开先清旧的
      toasts.get(payload.id)?.dismiss();
      const t = new Toast(payload.id, payload.style, payload.streaming, payload.text);
      toasts.set(payload.id, t);
      break;
    }
    case "toastDelta":
      toasts.get(payload.id)?.append(payload.delta);
      break;
    case "toastFinish":
      toasts.get(payload.id)?.finish();
      break;
    case "toastClose":
      toasts.get(payload.id)?.dismiss();
      break;
    case "recordingState":
      setRecording(payload.active, payload.elapsed);
      break;
    case "notify":
      notify(payload.message, payload.level, payload.duration);
      break;
  }
});

// Esc 关闭最上面的 toast（与旧实现一致）
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && toasts.size > 0) {
    const last = [...toasts.values()].pop();
    last?.dismiss();
  }
});
