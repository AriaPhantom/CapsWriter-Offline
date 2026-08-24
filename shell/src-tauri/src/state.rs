//! 外壳共享状态与后端探测
//!
//! 进程探测这里刻意避开了旧 GUI 的写法。旧实现对全部 ~570 个进程取
//! `cmdline`，并对每个参数做 `Path.resolve()`（触发文件系统调用），
//! 实测单轮 3.2s，而刷新周期只有 2.5s —— 后台线程永不空闲。
//!
//! 这里改为：先用进程名粗筛（只有 python/pythonw/start_* 才是候选），
//! 命中后才读该进程的命令行。候选通常十来个，单轮降到毫秒级。

use crate::protocol::{RecognitionInfo, StatusSnapshot};
use parking_lot::Mutex;
use std::collections::HashSet;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use sysinfo::{ProcessRefreshKind, ProcessesToUpdate, System, UpdateKind};

/// ASR server 监听端口
pub const ASR_PORT: u16 = 6016;

/// 只有这些进程名才可能是 CapsWriter 后端，用于粗筛
const CANDIDATE_NAMES: &[&str] = &[
    "python.exe",
    "pythonw.exe",
    "python",
    "start_server.exe",
    "start_client.exe",
];

pub struct AppState {
    inner: Mutex<Inner>,
    root: PathBuf,
    sys: Mutex<System>,
}

struct Inner {
    recording: bool,
    open_toasts: HashSet<String>,
    /// 最近一个通知气泡的预计消失时刻。
    /// 通知没有 id、也没有前端回调，所以用「忙到什么时候」来表达，
    /// 否则看门狗会在气泡还在屏幕上时把浮层隐藏掉。
    notify_busy_until: Option<Instant>,
    last_recognition: Option<RecognitionInfo>,
    last_heartbeat: Option<Instant>,
    heartbeat_pid: u32,
    /// 缓存的快照，避免 Dashboard 高频拉取时反复扫进程
    cached: Option<(Instant, StatusSnapshot)>,
}

/// 快照缓存有效期。Dashboard 每秒拉一次也只会真正扫 2 次/秒以下。
const SNAPSHOT_TTL: Duration = Duration::from_millis(700);

/// Python 侧心跳超时：超过这个时间没收到就认为客户端已退出
const HEARTBEAT_TIMEOUT: Duration = Duration::from_secs(6);

impl AppState {
    pub fn new(root: PathBuf) -> Self {
        Self {
            inner: Mutex::new(Inner {
                recording: false,
                open_toasts: HashSet::new(),
                notify_busy_until: None,
                last_recognition: None,
                last_heartbeat: None,
                heartbeat_pid: 0,
                cached: None,
            }),
            root,
            sys: Mutex::new(System::new()),
        }
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn set_recording(&self, active: bool) {
        self.inner.lock().recording = active;
    }

    pub fn is_recording(&self) -> bool {
        self.inner.lock().recording
    }

    pub fn note_toast_open(&self, id: &str) {
        self.inner.lock().open_toasts.insert(id.to_string());
    }

    pub fn note_toast_closed(&self, id: &str) {
        self.inner.lock().open_toasts.remove(id);
    }

    pub fn has_open_toasts(&self) -> bool {
        !self.inner.lock().open_toasts.is_empty()
    }

    /// 记录一个通知气泡将占用浮层多久（含淡出与少量余量）
    pub fn note_notify(&self, duration_ms: u64) {
        let hold = Duration::from_millis(duration_ms.max(600) + 700);
        let until = Instant::now() + hold;
        let mut g = self.inner.lock();
        // 多条气泡并存时取最晚的那个
        if g.notify_busy_until.map(|t| until > t).unwrap_or(true) {
            g.notify_busy_until = Some(until);
        }
    }

    /// 浮层当前是否还有可见内容（toast / 录音指示 / 通知气泡）
    ///
    /// 看门狗与 `hide_overlay_if_idle` 都以此为准。任何新增的
    /// 「会让浮层出现内容」的事件都必须体现在这里。
    pub fn overlay_busy(&self) -> bool {
        let g = self.inner.lock();
        if g.recording || !g.open_toasts.is_empty() {
            return true;
        }
        g.notify_busy_until
            .map(|t| Instant::now() < t)
            .unwrap_or(false)
    }

    pub fn note_heartbeat(&self, pid: u32) {
        let mut g = self.inner.lock();
        g.last_heartbeat = Some(Instant::now());
        if pid != 0 {
            g.heartbeat_pid = pid;
        }
    }

    /// 心跳是否来自本安装目录的客户端。
    ///
    /// 老客户端不带 `root` 字段（空串）时按兼容处理予以接受；
    /// 带了就必须与本 root 一致，避免同机另一份安装把状态点亮。
    pub fn heartbeat_belongs_here(&self, root: &str) -> bool {
        if root.is_empty() {
            return true;
        }
        let a = Path::new(root)
            .canonicalize()
            .unwrap_or_else(|_| PathBuf::from(root));
        let b = self
            .root
            .canonicalize()
            .unwrap_or_else(|_| self.root.clone());
        a.to_string_lossy().to_ascii_lowercase() == b.to_string_lossy().to_ascii_lowercase()
    }

    /// Python 客户端是否在线（基于心跳，比扫进程表可靠且便宜）
    pub fn client_alive_by_heartbeat(&self) -> bool {
        self.inner
            .lock()
            .last_heartbeat
            .map(|t| t.elapsed() < HEARTBEAT_TIMEOUT)
            .unwrap_or(false)
    }

    pub fn set_last_recognition(
        &self,
        text: String,
        original: String,
        latency: f64,
        hotwords: Vec<String>,
    ) {
        let at = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0);
        let mut g = self.inner.lock();
        g.last_recognition = Some(RecognitionInfo {
            text,
            original,
            latency,
            hotwords,
            at,
        });
        // 识别结果变了，让缓存立刻失效，Dashboard 下一拉就能看到
        g.cached = None;
    }

    /// 采集后端状态快照（带 TTL 缓存）
    pub fn snapshot(&self) -> StatusSnapshot {
        if let Some((at, snap)) = self.inner.lock().cached.clone() {
            if at.elapsed() < SNAPSHOT_TTL {
                return snap;
            }
        }

        let t0 = Instant::now();
        let (server_pids, client_pids) = self.scan_backend_processes();
        let port_open = probe_port(ASR_PORT);
        let active_model = self.read_active_model();
        let last_recognition = self.inner.lock().last_recognition.clone();

        let snap = StatusSnapshot {
            server_running: !server_pids.is_empty(),
            // 心跳优先：Python 在线一定说明客户端活着，
            // 即便进程名匹配因为打包方式变化而失效
            client_running: !client_pids.is_empty() || self.client_alive_by_heartbeat(),
            port_open,
            server_pids,
            client_pids,
            active_model,
            last_recognition,
            probe_ms: t0.elapsed().as_millis() as u64,
        };

        self.inner.lock().cached = Some((Instant::now(), snap.clone()));
        snap
    }

    /// 返回 (server_pids, client_pids)
    fn scan_backend_processes(&self) -> (Vec<u32>, Vec<u32>) {
        let mut sys = self.sys.lock();
        // 只刷新进程列表，且只要 cmd + exe，不要内存/CPU 等昂贵字段
        sys.refresh_processes_specifics(
            ProcessesToUpdate::All,
            true,
            ProcessRefreshKind::nothing()
                .with_cmd(UpdateKind::Always)
                .with_exe(UpdateKind::Always),
        );

        let mut server = Vec::new();
        let mut client = Vec::new();

        for (pid, proc_) in sys.processes() {
            // 粗筛：进程名不在候选里就直接跳过，不碰 cmdline
            let name = proc_.name().to_string_lossy().to_ascii_lowercase();
            if !CANDIDATE_NAMES.iter().any(|c| name == *c) {
                continue;
            }

            // 命中候选，才检查命令行
            let cmd_joined = proc_
                .cmd()
                .iter()
                .map(|s| s.to_string_lossy().to_ascii_lowercase())
                .collect::<Vec<_>>()
                .join(" ");

            let is_server = cmd_joined.contains("start_server")
                || cmd_joined.contains("core_server")
                || name == "start_server.exe";
            let is_client = cmd_joined.contains("start_client")
                || cmd_joined.contains("core_client")
                || name == "start_client.exe";

            if !is_server && !is_client {
                continue;
            }

            // 必须确属**本**安装目录。
            //
            // 这里绝不能用「目录名相同」之类的宽松匹配：同一台机器上很可能
            // 存在多份 CapsWriter-Offline（仓库 + 部署副本），仅比对末级目录名
            // 会把别的副本认成自己。那不只是状态显示错，`stop_all` 还会
            // taskkill 掉另一份正在使用的客户端。
            //
            // 判定只认两种确定性证据：
            //   1. 命令行里出现本 root 的完整路径（python 跑本目录的脚本）
            //   2. 进程 exe 就在本 root 之下（打包版 start_*.exe）
            let root_lossy = self.root.to_string_lossy().to_ascii_lowercase();
            let belongs = cmd_joined.contains(&root_lossy)
                || proc_
                    .exe()
                    .map(|p| p.starts_with(&self.root))
                    .unwrap_or(false);
            if !belongs {
                continue;
            }

            if is_server {
                server.push(pid.as_u32());
            }
            if is_client {
                client.push(pid.as_u32());
            }
        }

        (server, client)
    }

    /// 从 config_server.py 读当前 model_type
    fn read_active_model(&self) -> String {
        let path = self.root.join("config_server.py");
        let Ok(content) = std::fs::read_to_string(&path) else {
            return String::new();
        };
        for line in content.lines() {
            let t = line.trim_start();
            if let Some(rest) = t.strip_prefix("model_type") {
                let rest = rest.trim_start();
                if let Some(rest) = rest.strip_prefix('=') {
                    let rest = rest.trim();
                    let val = rest
                        .trim_start_matches(['\'', '"'])
                        .split(['\'', '"'])
                        .next()
                        .unwrap_or("");
                    if !val.is_empty() {
                        return val.to_string();
                    }
                }
            }
        }
        String::new()
    }
}

/// TCP 连通性探测。比 psutil 的 net_connections 枚举全表更轻，
/// 也不需要提权（旧实现在部分环境会撞 AccessDenied）。
pub fn probe_port(port: u16) -> bool {
    use std::net::{Ipv4Addr, SocketAddrV4, TcpStream};
    let addr = SocketAddrV4::new(Ipv4Addr::LOCALHOST, port);
    TcpStream::connect_timeout(&addr.into(), Duration::from_millis(120)).is_ok()
}

/// 探测 ASR server 端口是否就绪
pub fn probe_asr_port() -> bool {
    probe_port(ASR_PORT)
}
