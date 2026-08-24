// 不弹控制台窗口（release 下生效）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod backend;
mod bridge;
mod protocol;
mod state;
mod window;

use protocol::StatusSnapshot;
use state::AppState;
use std::path::PathBuf;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, State};

// ============================================================
// Tauri 命令（前端 invoke 调用）
// ============================================================

#[tauri::command]
fn get_status(state: State<AppState>) -> StatusSnapshot {
    state.snapshot()
}

#[tauri::command]
fn start_backend(state: State<AppState>) -> Result<String, String> {
    backend::start_all(&state).map_err(|e| e.to_string())
}

#[tauri::command]
fn stop_backend(state: State<AppState>) -> Result<String, String> {
    backend::stop_all(&state).map_err(|e| e.to_string())
}

#[tauri::command]
fn restart_backend(state: State<AppState>) -> Result<String, String> {
    backend::restart_all(&state).map_err(|e| e.to_string())
}

#[tauri::command]
fn switch_model(state: State<AppState>, model: String) -> Result<String, String> {
    backend::switch_model(&state, &model).map_err(|e| e.to_string())
}

/// 日志尾部读取结果
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
struct LogTail {
    /// 整段文本（已用 \n 连接）。传一个大字符串而不是 Vec<String>，
    /// 避免 300 条独立字符串各自过一遍 IPC 序列化。
    text: String,
    /// 日志文件名，供前端显示
    file: String,
    /// 文件总字节数
    size: u64,
    /// 文件修改时间（Unix 毫秒），前端据此判断是否需要重绘
    mtime: u64,
    /// 本次读取耗时（毫秒），便于自查性能回退
    took_ms: u64,
}

/// 读取日志尾部。
///
/// 关键点：**只从文件末尾读固定字节**，不把整个文件读进内存。
/// 真实日志会长到 10MB / 9 万行（用户机器上 logs 目录累计 906MB），
/// 整文件读入需要 23ms 且随文件增长线性变差；只读尾部 512KB 是 1ms 且恒定。
///
/// `known_mtime` 是前端已持有的修改时间；未变化时返回 `None`，
/// 省掉一次读取与整块重绘。
#[tauri::command]
fn read_log_tail(
    state: State<AppState>,
    which: String,
    max_lines: usize,
    known_mtime: Option<u64>,
) -> Result<Option<LogTail>, String> {
    use std::io::{Read, Seek, SeekFrom};

    let t0 = std::time::Instant::now();
    let dir = state.root().join("logs");
    let prefix = if which == "server" { "server" } else { "client" };

    // 找当前应该看的日志文件。只看 `<prefix>_YYYYMMDD.log`
    // （排除 .log.1 之类轮转备份），优先按**文件名里的日期**取最大，
    // 日期相同再比 mtime。
    //
    // 不单纯用 mtime 是因为：备份/同步/复制都会改写 mtime，
    // 一个旧日期的文件可能反而有更新的 mtime，导致面板显示错误的那一天。
    // 文件名里的日期才是权威。
    let mut best: Option<(String, std::time::SystemTime, PathBuf)> = None;
    let entries = std::fs::read_dir(&dir).map_err(|e| format!("读取 logs 目录失败: {e}"))?;
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if !name.starts_with(prefix) || !name.ends_with(".log") {
            continue;
        }
        // 提取名字中的日期串（`client_20260823.log` -> `20260823`），
        // 没有日期的（如 `client.log`）用空串，排在有日期的之后
        let day = name
            .strip_prefix(prefix)
            .and_then(|s| s.strip_suffix(".log"))
            .map(|s| s.trim_start_matches('_'))
            .filter(|s| s.len() == 8 && s.chars().all(|c| c.is_ascii_digit()))
            .unwrap_or("")
            .to_string();

        let Ok(meta) = entry.metadata() else { continue };
        let mtime = meta.modified().unwrap_or(std::time::UNIX_EPOCH);

        let better = match &best {
            None => true,
            Some((bday, bmtime, _)) => (&day, &mtime) > (bday, bmtime),
        };
        if better {
            best = Some((day, mtime, entry.path()));
        }
    }
    let best = best.map(|(_, mtime, path)| (mtime, path));

    let Some((mtime, path)) = best else {
        return Ok(Some(LogTail {
            text: format!("暂无 {prefix} 日志"),
            file: String::new(),
            size: 0,
            mtime: 0,
            took_ms: t0.elapsed().as_millis() as u64,
        }));
    };

    let mtime_ms = mtime
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0);

    // 文件没变化就不重复读取，前端也不用重绘
    if known_mtime == Some(mtime_ms) {
        return Ok(None);
    }

    let mut file = std::fs::File::open(&path).map_err(|e| format!("打开日志失败: {e}"))?;
    let size = file
        .metadata()
        .map(|m| m.len())
        .map_err(|e| format!("读取日志属性失败: {e}"))?;

    // 按每行平均 ~110 字节估算需要的字节数，再留足余量。
    // 上限 1MB：即便全是超长行也够 300 行用。
    let want = ((max_lines as u64 + 8) * 400).clamp(64 * 1024, 1024 * 1024);
    let want = want.min(size);
    let from_start = size <= want;

    let mut buf = vec![0u8; want as usize];
    file.seek(SeekFrom::Start(size - want))
        .map_err(|e| format!("定位日志失败: {e}"))?;
    file.read_exact(&mut buf)
        .map_err(|e| format!("读取日志失败: {e}"))?;

    // 日志可能含非 UTF-8 字节，或从中途切断多字节字符，有损解码即可
    let chunk = String::from_utf8_lossy(&buf);
    let mut lines: Vec<&str> = chunk.lines().collect();
    // 不是从头读的话，第一行可能被截断，丢掉它
    if !from_start && lines.len() > 1 {
        lines.remove(0);
    }
    let start = lines.len().saturating_sub(max_lines);
    let text = lines[start..].join("\n");

    Ok(Some(LogTail {
        text,
        file: path
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default(),
        size,
        mtime: mtime_ms,
        took_ms: t0.elapsed().as_millis() as u64,
    }))
}

#[tauri::command]
fn open_path(state: State<AppState>, target: String) -> Result<(), String> {
    let path = match target.as_str() {
        "logs" => state.root().join("logs"),
        "root" => state.root().to_path_buf(),
        other => state.root().join(other),
    };
    opener_reveal(&path).map_err(|e| e.to_string())
}

fn opener_reveal(path: &std::path::Path) -> anyhow::Result<()> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        std::process::Command::new("explorer")
            .arg(path)
            .creation_flags(0x0800_0000)
            .spawn()?;
        return Ok(());
    }
    #[cfg(not(windows))]
    {
        std::process::Command::new("xdg-open").arg(path).spawn()?;
        Ok(())
    }
}

/// 浮层告知后端：现在需要/不需要接管鼠标
#[tauri::command]
fn set_overlay_interactive(app: AppHandle, interactive: bool) {
    window::set_overlay_interactive(&app, interactive);
}

/// 浮层内容已全部消散，可以隐藏整个浮层窗口
#[tauri::command]
fn overlay_idle(app: AppHandle) {
    window::hide_overlay_if_idle(&app);
}

/// 浮层通知后端某个 toast 已在前端关闭（倒计时到点或用户手动关）
#[tauri::command]
fn note_toast_closed(state: State<AppState>, id: String) {
    state.note_toast_closed(&id);
}

#[tauri::command]
fn show_dashboard(app: AppHandle) -> Result<(), String> {
    window::show_dashboard(&app).map_err(|e| e.to_string())
}

#[tauri::command]
fn hide_dashboard(app: AppHandle) {
    window::hide_dashboard(&app);
}

#[tauri::command]
fn quit_shell(app: AppHandle) {
    app.exit(0);
}

/// 前端把日志/错误回传到外壳 stderr。
/// WebView 的 console 在无头环境下看不到，出问题时这是唯一的可观测入口。
#[tauri::command]
fn ui_log(level: String, message: String) {
    eprintln!("[ui/{level}] {message}");
}

// ============================================================
// 入口
// ============================================================

fn resolve_root() -> PathBuf {
    // 打包后 exe 位于 <root>/shell/… 或 <root>/，开发时 cwd 是 src-tauri。
    // 逐级上溯找到含 config_server.py 的目录，作为项目根。
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        let mut p = exe.parent().map(|p| p.to_path_buf());
        while let Some(dir) = p {
            candidates.push(dir.clone());
            p = dir.parent().map(|p| p.to_path_buf());
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        let mut p = Some(cwd);
        while let Some(dir) = p {
            candidates.push(dir.clone());
            p = dir.parent().map(|p| p.to_path_buf());
        }
    }
    for c in &candidates {
        if c.join("config_server.py").exists() && c.join("start_client.py").exists() {
            return c.clone();
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."))
}

fn main() {
    let root = resolve_root();
    eprintln!("[shell] 项目根目录: {}", root.display());

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(AppState::new(root))
        .invoke_handler(tauri::generate_handler![
            get_status,
            start_backend,
            stop_backend,
            restart_backend,
            switch_model,
            read_log_tail,
            open_path,
            set_overlay_interactive,
            overlay_idle,
            note_toast_closed,
            show_dashboard,
            hide_dashboard,
            quit_shell,
            ui_log,
        ])
        .setup(|app| {
            let handle = app.handle().clone();

            // 浮层预创建（隐藏），保证首次显示零建窗延迟
            window::build_overlay(&handle)?;

            // 托盘
            let open_i = MenuItem::with_id(app, "open", "打开面板", true, None::<&str>)?;
            let start_i = MenuItem::with_id(app, "start", "启动后端", true, None::<&str>)?;
            let restart_i = MenuItem::with_id(app, "restart", "重启后端", true, None::<&str>)?;
            let stop_i = MenuItem::with_id(app, "stop", "停止后端", true, None::<&str>)?;
            let sep = PredefinedMenuItem::separator(app)?;
            let quit_i = MenuItem::with_id(app, "quit", "退出外壳", true, None::<&str>)?;
            let menu = Menu::with_items(
                app,
                &[&open_i, &sep, &start_i, &restart_i, &stop_i, &sep, &quit_i],
            )?;

            TrayIconBuilder::with_id("main-tray")
                .icon(app.default_window_icon().unwrap().clone())
                .tooltip("CapsWriter Offline")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| {
                    let state = app.state::<AppState>();
                    match event.id().as_ref() {
                        "open" => {
                            let _ = window::show_dashboard(app);
                        }
                        "start" => {
                            let _ = backend::start_all(&state);
                        }
                        "restart" => {
                            let _ = backend::restart_all(&state);
                        }
                        "stop" => {
                            let _ = backend::stop_all(&state);
                        }
                        "quit" => app.exit(0),
                        _ => {}
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    // 左键单击切换面板显示
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(win) = app.get_webview_window(window::DASHBOARD_LABEL) {
                            if win.is_visible().unwrap_or(false) {
                                let _ = win.hide();
                                return;
                            }
                        }
                        let _ = window::show_dashboard(app);
                    }
                })
                .build(app)?;

            // 浮层看门狗：兜住「空闲但仍可见」的异常态
            window::spawn_overlay_watchdog(handle.clone());

            // 带 --show 启动时直接打开面板（首次运行/从启动器打开时用）
            if std::env::args().any(|a| a == "--show") {
                let _ = window::show_dashboard(&handle);
            }

            // 启动 Python 桥
            bridge::spawn(handle);

            Ok(())
        })
        .on_window_event(|win, event| {
            // 关闭 Dashboard 只隐藏，外壳继续常驻托盘
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if win.label() == window::DASHBOARD_LABEL {
                    api.prevent_close();
                    let _ = win.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("外壳启动失败");
}
