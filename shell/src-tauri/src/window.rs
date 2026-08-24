//! 窗口管理：Dashboard 主窗 + Toast 浮层
//!
//! 浮层的关键设计：
//! - **预创建**：启动即建好并隐藏。显示时只是 `show()`，没有建窗开销，
//!   也没有旧实现 100ms 队列轮询的首帧延迟。
//! - **全屏透明 + 点击穿透**：浮层铺满工作区，背景透明，默认忽略鼠标事件，
//!   所以它悬在所有窗口之上却不挡任何操作。只有当浮层内容需要交互
//!   （可编辑 / 悬停暂停倒计时）时，前端才请求临时接管鼠标。
//! - **不进任务栏、不抢焦点**：`skip_taskbar` + `focused(false)`，
//!   打字时焦点始终留在目标程序里。

use crate::state::AppState;
use tauri::{AppHandle, Emitter, Manager, WebviewUrl, WebviewWindowBuilder};

pub const DASHBOARD_LABEL: &str = "dashboard";
pub const OVERLAY_LABEL: &str = "overlay";

/// 创建 Toast 浮层窗口（启动时调用一次）
pub fn build_overlay(app: &AppHandle) -> tauri::Result<()> {
    if app.get_webview_window(OVERLAY_LABEL).is_some() {
        return Ok(());
    }

    let win = WebviewWindowBuilder::new(
        app,
        OVERLAY_LABEL,
        WebviewUrl::App("overlay.html".into()),
    )
    .title("CapsWriter Overlay")
    .transparent(true)
    .decorations(false)
    .always_on_top(true)
    .skip_taskbar(true)
    .resizable(false)
    .focused(false)
    .visible(false)
    .shadow(false)
    .build()?;

    // 铺满主显示器
    align_to_primary_monitor(&win);

    // 默认穿透，鼠标事件交给下层窗口
    let _ = win.set_ignore_cursor_events(true);

    Ok(())
}

/// 把浮层对齐到主显示器整屏（含任务栏区域）。
///
/// 用整个 `monitor.size()` 而不是工作区：浮层是点击穿透的，盖住任务栏
/// 不影响操作，而 CSS 里 `bottom: 8vh` 的换算需要以整屏高度为基准。
///
/// **必须可重复调用**：见 `ensure_overlay_visible` 的说明。
fn align_to_primary_monitor(win: &tauri::WebviewWindow) {
    let Ok(Some(monitor)) = win.primary_monitor() else {
        return;
    };
    let size = *monitor.size();
    let pos = *monitor.position();

    // 尺寸/位置已经正确时就不再下发，避免每次录音都白白触发一次
    // SetWindowPos 引起的重排。
    let cur_pos = win.outer_position().ok();
    let cur_size = win.outer_size().ok();
    let aligned = cur_pos.map(|p| p.x == pos.x && p.y == pos.y).unwrap_or(false)
        && cur_size
            .map(|s| s.width == size.width && s.height == size.height)
            .unwrap_or(false);
    if aligned {
        return;
    }

    let _ = win.set_position(tauri::PhysicalPosition::new(pos.x, pos.y));
    let _ = win.set_size(tauri::PhysicalSize::new(size.width, size.height));
}

/// 确保浮层可见（幂等，热路径上调用）
///
/// 这里每次都重新校正几何与置顶，而不是只在建窗时做一次。原因是
/// 「建窗那一刻」的显示环境未必可信，而错误状态不会自己恢复：
///
/// - **开机自启**：外壳随登录启动时，DWM / 显示器拓扑 / DPI 上下文可能
///   都还在收敛。此时 `primary_monitor()` 拿到的尺寸是过渡值，浮层会被
///   定在错误的矩形上（实测开机后为 `(-13,-13) 3866x2186`，而正确值是
///   `(0,0) 3840x2160`），窗口虽然 `visible=true` 却一个像素都不合成，
///   于是「按住说话时底部横幅不见了」。
/// - **显示器变化**：插拔外显、改分辨率、改缩放之后，原来的矩形同样失效。
///
/// 校正很便宜（几何未变时直接返回），换来的是浮层不会因为一次
/// 时序不巧就永久失效 —— 用户端的症状是功能整体消失，代价不对等。
pub fn ensure_overlay_visible(app: &AppHandle) {
    if let Some(win) = app.get_webview_window(OVERLAY_LABEL) {
        // 顺序要紧：先摆正几何再 show，避免先显示出一个错位的空窗
        align_to_primary_monitor(&win);
        if !win.is_visible().unwrap_or(false) {
            let _ = win.show();
        }
        // 重新置顶：某些全屏程序会抢占 topmost
        let _ = win.set_always_on_top(true);
    }
}

/// 浮层内容全部消失后隐藏，避免一个空的全屏透明窗常驻
pub fn hide_overlay_if_idle(app: &AppHandle) {
    let state = app.state::<AppState>();
    if state.overlay_busy() {
        return;
    }
    if let Some(win) = app.get_webview_window(OVERLAY_LABEL) {
        let _ = win.hide();
        // 隐藏时恢复穿透，防止下次显示时残留接管状态
        let _ = win.set_ignore_cursor_events(true);
    }
}

/// 浮层看门狗
///
/// 浮层是全屏置顶窗口，一旦因为丢事件而卡在可见状态，用户会觉得
/// 「屏幕上糊了一层东西」。前端的 `overlay_idle` 是唯一的隐藏触发点，
/// 任何一次丢失（前端异常、WebView 重载、进程竞态）都会让它永久可见。
///
/// 所以这里加一个低频自检：状态上已经空闲、但窗口还可见，就补一次隐藏。
/// 1 秒一次、只读两个布尔值，开销可忽略。
pub fn spawn_overlay_watchdog(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let mut ticker = tokio::time::interval(std::time::Duration::from_secs(1));
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        // 需要连续两次判定空闲才隐藏，避免和「刚 open 还没渲染」撞车
        let mut idle_streak = 0u8;
        loop {
            ticker.tick().await;
            let state = app.state::<AppState>();
            let idle = !state.overlay_busy();
            if !idle {
                idle_streak = 0;
                continue;
            }
            idle_streak = idle_streak.saturating_add(1);
            if idle_streak < 2 {
                continue;
            }
            if let Some(win) = app.get_webview_window(OVERLAY_LABEL) {
                if win.is_visible().unwrap_or(false) {
                    let _ = win.hide();
                    let _ = win.set_ignore_cursor_events(true);
                }
            }
        }
    });
}

/// 前端请求接管/释放鼠标。浮层里有可交互内容时接管，否则穿透。
pub fn set_overlay_interactive(app: &AppHandle, interactive: bool) {
    if let Some(win) = app.get_webview_window(OVERLAY_LABEL) {
        let _ = win.set_ignore_cursor_events(!interactive);
    }
}

/// 显示并聚焦 Dashboard；不存在则按需创建（懒加载，省内存）
pub fn show_dashboard(app: &AppHandle) -> tauri::Result<()> {
    if let Some(win) = app.get_webview_window(DASHBOARD_LABEL) {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
        // 明确告知前端「你现在可见了，去刷新」。
        // 不依赖 visibilitychange —— 它在 hide/show 时不保证触发，
        // 漏一次就会让面板永久停在旧数据上。
        let _ = win.emit("dashboard-shown", ());
        return Ok(());
    }

    // 支持 --page=<id> 直接打开指定页（便于验证日志页等）
    let page = std::env::args()
        .find_map(|a| a.strip_prefix("--page=").map(|s| s.to_string()))
        .filter(|s| s.chars().all(|c| c.is_ascii_alphanumeric() || c == '_'));
    let url = match page {
        Some(p) => format!("index.html?page={p}"),
        None => "index.html".to_string(),
    };

    let win = WebviewWindowBuilder::new(app, DASHBOARD_LABEL, WebviewUrl::App(url.into()))
    .title("CapsWriter Offline")
    .inner_size(1040.0, 700.0)
    .min_inner_size(880.0, 560.0)
    .decorations(false)
    .transparent(true)
    .resizable(true)
    .center()
    .visible(true)
    .build()?;

    let _ = win.set_focus();
    Ok(())
}

pub fn hide_dashboard(app: &AppHandle) {
    if let Some(win) = app.get_webview_window(DASHBOARD_LABEL) {
        let _ = win.hide();
    }
}
