//! Python <-> 外壳 的本机 TCP 桥
//!
//! 设计要点（对应原 Tk 实现的三个性能缺陷）：
//!
//! 1. **帧聚合**：`ToastAppend` 不直接转发。增量先进入 per-toast 缓冲，
//!    由一个 60fps 的聚合任务合并后一次性 emit。LLM 吐 40 tok/s 时，
//!    原实现是 40 次全量重排，这里最多 60 次 emit 且每次只带增量文本，
//!    实际因为聚合通常远少于 40 次。
//! 2. **零轮询**：原实现用 `after(100ms)` 轮询消息队列，首帧白等 50~110ms。
//!    这里 TCP 可读即处理，浮层窗口预创建并隐藏，显示只是 `show()`。
//! 3. **不阻塞**：解析、聚合都在 tokio 线程，WebView 渲染在 UI 线程，
//!    两者通过 emit 解耦，不存在原实现里工作线程直接调 Tk 的竞态。

use crate::protocol::{InboundEvent, UiEvent};
use crate::state::AppState;
use anyhow::Result;
use parking_lot::Mutex;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::{TcpListener, TcpStream};

/// Python 客户端连接的端口。选 6020 是为了和既有端口错开：
/// 6016 ASR server / 6018 UDP control / 6019 旧 GUI control / 6021 vite dev
pub const BRIDGE_PORT: u16 = 6020;

/// 聚合周期。16ms ≈ 60fps，肉眼已是连续流动，
/// 再快只是徒增 IPC 次数而看不出差别。
const COALESCE_INTERVAL: Duration = Duration::from_millis(16);

/// 待下发的增量缓冲：toast id -> 累积的增量文本
type PendingDeltas = Arc<Mutex<HashMap<String, String>>>;

pub fn spawn(app: AppHandle) {
    let pending: PendingDeltas = Arc::new(Mutex::new(HashMap::new()));

    // 聚合下发任务
    {
        let app = app.clone();
        let pending = pending.clone();
        tauri::async_runtime::spawn(async move {
            let mut ticker = tokio::time::interval(COALESCE_INTERVAL);
            ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            loop {
                ticker.tick().await;
                // 快速换出缓冲，避免持锁期间做 emit
                let batch: Vec<(String, String)> = {
                    let mut guard = pending.lock();
                    if guard.is_empty() {
                        continue;
                    }
                    guard.drain().collect()
                };
                for (id, delta) in batch {
                    if delta.is_empty() {
                        continue;
                    }
                    let _ = app.emit("ui-event", UiEvent::ToastDelta { id, delta });
                }
            }
        });
    }

    // TCP 监听任务
    tauri::async_runtime::spawn(async move {
        loop {
            match TcpListener::bind(("127.0.0.1", BRIDGE_PORT)).await {
                Ok(listener) => {
                    log_info(&format!("桥已监听 127.0.0.1:{BRIDGE_PORT}"));
                    loop {
                        match listener.accept().await {
                            Ok((stream, _)) => {
                                let app = app.clone();
                                let pending = pending.clone();
                                tauri::async_runtime::spawn(async move {
                                    if let Err(e) = handle_client(stream, app, pending).await {
                                        log_info(&format!("桥连接结束: {e}"));
                                    }
                                });
                            }
                            Err(e) => {
                                log_info(&format!("accept 失败: {e}"));
                                tokio::time::sleep(Duration::from_millis(500)).await;
                            }
                        }
                    }
                }
                Err(e) => {
                    // 端口被占用（比如另一个外壳实例）时不断重试，
                    // 这样先启外壳还是先启 Python 都无所谓。
                    log_info(&format!("绑定 {BRIDGE_PORT} 失败，2s 后重试: {e}"));
                    tokio::time::sleep(Duration::from_secs(2)).await;
                }
            }
        }
    });
}

async fn handle_client(
    stream: TcpStream,
    app: AppHandle,
    pending: PendingDeltas,
) -> Result<()> {
    stream.set_nodelay(true)?;
    let mut lines = BufReader::new(stream).lines();

    while let Some(line) = lines.next_line().await? {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        match serde_json::from_str::<InboundEvent>(line) {
            Ok(ev) => dispatch(ev, &app, &pending),
            Err(e) => log_info(&format!("无法解析事件: {e} / 原文: {line}")),
        }
    }
    Ok(())
}

fn dispatch(ev: InboundEvent, app: &AppHandle, pending: &PendingDeltas) {
    let state = app.state::<AppState>();

    match ev {
        InboundEvent::ToastOpen {
            id,
            style,
            streaming,
            text,
        } => {
            state.note_toast_open(&id);
            crate::window::ensure_overlay_visible(app);
            let _ = app.emit(
                "ui-event",
                UiEvent::ToastOpen {
                    id,
                    style,
                    streaming,
                    text,
                },
            );
        }

        InboundEvent::ToastAppend { id, delta } => {
            // 只入缓冲，不 emit —— 聚合任务负责下发
            let mut guard = pending.lock();
            guard.entry(id).or_default().push_str(&delta);
        }

        InboundEvent::ToastFinish { id } => {
            // 关键顺序：先把该 toast 尚未下发的增量冲刷掉，
            // 再发 finish，否则前端可能在收到尾部文本前就定稿。
            flush_one(&id, app, pending);
            let _ = app.emit("ui-event", UiEvent::ToastFinish { id });
        }

        InboundEvent::ToastClose { id } => {
            pending.lock().remove(&id);
            state.note_toast_closed(&id);
            let _ = app.emit("ui-event", UiEvent::ToastClose { id });
        }

        InboundEvent::RecordingState { active, elapsed } => {
            state.set_recording(active);
            crate::window::ensure_overlay_visible(app);
            let _ = app.emit("ui-event", UiEvent::RecordingState { active, elapsed });
        }

        InboundEvent::Recognition {
            text,
            original,
            latency,
            hotwords,
        } => {
            state.set_last_recognition(text, original, latency, hotwords);
        }

        InboundEvent::Notify {
            message,
            level,
            duration,
        } => {
            // 先登记占用时长，再显示。顺序反了的话看门狗可能在
            // 登记之前就判定空闲并把浮层收掉。
            state.note_notify(duration);
            crate::window::ensure_overlay_visible(app);
            let _ = app.emit(
                "ui-event",
                UiEvent::Notify {
                    message,
                    level,
                    duration,
                },
            );
        }

        InboundEvent::Heartbeat { pid, root } => {
            // 只接受来自本安装目录的心跳。别处的 CapsWriter 客户端
            // 也能连上这个端口，但不该让它把本外壳的状态点亮。
            if state.heartbeat_belongs_here(&root) {
                state.note_heartbeat(pid);
            }
        }
    }
}

fn flush_one(id: &str, app: &AppHandle, pending: &PendingDeltas) {
    let delta = pending.lock().remove(id);
    if let Some(delta) = delta {
        if !delta.is_empty() {
            let _ = app.emit(
                "ui-event",
                UiEvent::ToastDelta {
                    id: id.to_string(),
                    delta,
                },
            );
        }
    }
}

fn log_info(msg: &str) {
    // 外壳自身的日志走 stderr，由启动器重定向到 logs/shell.log
    eprintln!("[bridge] {msg}");
}
