//! 外壳 IPC 协议
//!
//! Python 客户端通过本机 TCP（`127.0.0.1:6020`）向外壳推送 UI 事件，
//! 每条消息是一行 JSON（NDJSON），行尾 `\n`。选择 TCP 而非 UDP 是因为
//! 流式文本必须有序且不可丢包。
//!
//! 反方向（外壳 -> Python）复用已有的 UDP 控制端口，不新增机制。

use serde::{Deserialize, Serialize};

/// Python -> 外壳 的事件
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum InboundEvent {
    /// 开始一次浮层会话（对应原 ToastMessage 的创建）
    ToastOpen {
        id: String,
        #[serde(default)]
        style: ToastStyle,
        /// 是否流式；false 表示一次性显示完整文本
        #[serde(default)]
        streaming: bool,
        #[serde(default)]
        text: String,
    },
    /// 追加流式增量。注意这里传的是**增量**而非全量，
    /// 与原实现每次传全量再 diff 不同，避免 O(n²) 的字符串拼接与重排。
    ToastAppend { id: String, delta: String },
    /// 流式结束，进入 Markdown 定稿渲染并开始自动关闭倒计时
    ToastFinish { id: String },
    /// 立即关闭
    ToastClose { id: String },

    /// 录音状态指示（用于浮层的听写状态动画）
    RecordingState {
        active: bool,
        #[serde(default)]
        elapsed: f64,
    },

    /// 一次识别完成后的摘要，用于 Dashboard 的「最近识别」
    Recognition {
        text: String,
        #[serde(default)]
        original: String,
        #[serde(default)]
        latency: f64,
        #[serde(default)]
        hotwords: Vec<String>,
    },

    /// 通用提示气泡（替代原 `toast()` 简单调用）
    Notify {
        message: String,
        #[serde(default)]
        level: NotifyLevel,
        #[serde(default = "default_duration")]
        duration: u64,
    },
    // 注意：新增事件时若会让浮层产生可见内容，
    // 必须同步更新 AppState 的「忙」判定，否则看门狗会把浮层收掉。

    /// Python 侧存活心跳，用于 Dashboard 显示客户端在线状态。
    /// `root` 是客户端自己的安装目录，用于确认它属于本外壳所管的那一份
    /// （同机可能存在多份 CapsWriter 安装）。
    Heartbeat {
        #[serde(default)]
        pid: u32,
        #[serde(default)]
        root: String,
    },
}

fn default_duration() -> u64 {
    3000
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, Default, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum NotifyLevel {
    #[default]
    Info,
    Success,
    Warn,
    Error,
}

/// 浮层样式，字段与 `LLM/*.py` 里的 `toast_*` 配置一一对应，
/// 这样角色配置无需改动即可透传过来。
#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ToastStyle {
    #[serde(default = "d_width")]
    pub width: f64,
    #[serde(default)]
    pub height: u32,
    #[serde(default = "d_font_family")]
    pub font_family: String,
    #[serde(default = "d_font_size")]
    pub font_size: u32,
    #[serde(default = "d_fg")]
    pub fg: String,
    #[serde(default = "d_bg")]
    pub bg: String,
    #[serde(default = "d_duration")]
    pub duration: u64,
    #[serde(default)]
    pub editable: bool,
    #[serde(default = "d_true")]
    pub markdown: bool,
    /// 角色名，显示在浮层标题栏
    #[serde(default)]
    pub role: String,
}

fn d_width() -> f64 {
    0.5
}
fn d_font_family() -> String {
    "Microsoft YaHei UI".into()
}
fn d_font_size() -> u32 {
    14
}
fn d_fg() -> String {
    "#ffffff".into()
}
fn d_bg() -> String {
    "#075077".into()
}
fn d_duration() -> u64 {
    3000
}
fn d_true() -> bool {
    true
}

impl Default for ToastStyle {
    fn default() -> Self {
        Self {
            width: d_width(),
            height: 0,
            font_family: d_font_family(),
            font_size: d_font_size(),
            fg: d_fg(),
            bg: d_bg(),
            duration: d_duration(),
            editable: false,
            markdown: true,
            role: String::new(),
        }
    }
}

/// 外壳 -> 前端 的事件载荷（通过 Tauri emit 下发到 WebView）
#[derive(Debug, Clone, Serialize)]
#[serde(tag = "kind", rename_all = "camelCase")]
pub enum UiEvent {
    #[serde(rename_all = "camelCase")]
    ToastOpen {
        id: String,
        style: ToastStyle,
        streaming: bool,
        text: String,
    },
    /// 合并后的增量：外壳按帧聚合多个 append 再下发一次，
    /// 这是把 O(n²) 重排降为定频单次重排的关键。
    #[serde(rename_all = "camelCase")]
    ToastDelta { id: String, delta: String },
    #[serde(rename_all = "camelCase")]
    ToastFinish { id: String },
    #[serde(rename_all = "camelCase")]
    ToastClose { id: String },
    #[serde(rename_all = "camelCase")]
    RecordingState { active: bool, elapsed: f64 },
    #[serde(rename_all = "camelCase")]
    Notify {
        message: String,
        level: NotifyLevel,
        duration: u64,
    },
}

/// Dashboard 通过 `invoke` 拉取的后端状态快照
#[derive(Debug, Clone, Serialize, Default)]
#[serde(rename_all = "camelCase")]
pub struct StatusSnapshot {
    pub server_running: bool,
    pub client_running: bool,
    pub port_open: bool,
    pub server_pids: Vec<u32>,
    pub client_pids: Vec<u32>,
    /// 父进程已消失、但仍在运行的 multiprocessing 子进程。
    ///
    /// 刻意与 `server_pids` 分开：`server_running` 是从 `server_pids`
    /// 是否为空推出来的，把孤儿混进去会让 Dashboard 在只剩残留时
    /// 错误地显示「server 正在运行」。这些 PID 只用于 `stop_all` 清理。
    pub orphan_pids: Vec<u32>,
    pub active_model: String,
    pub last_recognition: Option<RecognitionInfo>,
    /// 采集这份快照本身花了多少毫秒，便于自查性能回退
    pub probe_ms: u64,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct RecognitionInfo {
    pub text: String,
    pub original: String,
    pub latency: f64,
    pub hotwords: Vec<String>,
    /// Unix 毫秒时间戳
    pub at: u64,
}
