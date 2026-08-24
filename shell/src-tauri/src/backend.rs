//! 后端进程控制：启动/停止/重启 Python 的 server 与 client
//!
//! 与旧 GUI 的差别：启动时用 `CREATE_NO_WINDOW` 直接隐藏控制台，
//! 不再需要「先启动、再 EnumWindows 找窗口去 ShowWindow(SW_HIDE)」
//! 那套竞态做法（那会闪一下黑窗，且偶发抓不到）。

use crate::state::AppState;
use anyhow::{anyhow, Result};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// 解析要用的 Python 解释器。优先项目自带的 runtime，其次 PATH。
fn resolve_python(root: &Path) -> PathBuf {
    // 常见的随包 Python 位置
    let candidates = [
        root.join("runtime").join("python.exe"),
        root.join("python").join("python.exe"),
    ];
    for c in candidates {
        if c.exists() {
            return c;
        }
    }
    // 回退：让系统在 PATH 里找。pythonw 不带控制台，更适合后台常驻。
    PathBuf::from("python.exe")
}

fn spawn_script(root: &Path, script: &str) -> Result<u32> {
    let script_path = root.join(script);
    if !script_path.exists() {
        return Err(anyhow!("找不到 {script}"));
    }
    let py = resolve_python(root);

    let mut cmd = Command::new(&py);
    cmd.arg(&script_path)
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let child = cmd.spawn().map_err(|e| anyhow!("启动 {script} 失败: {e}"))?;
    Ok(child.id())
}

pub fn start_server(state: &AppState) -> Result<String> {
    let snap = state.snapshot();
    if snap.server_running {
        return Ok("Server 已在运行".into());
    }
    let pid = spawn_script(state.root(), "start_server.py")?;
    Ok(format!("Server 已启动 (pid {pid})"))
}

pub fn start_client(state: &AppState) -> Result<String> {
    let snap = state.snapshot();
    if snap.client_running {
        return Ok("Client 已在运行".into());
    }
    let pid = spawn_script(state.root(), "start_client.py")?;
    Ok(format!("Client 已启动 (pid {pid})"))
}

/// 启动全部：先 server，等端口就绪再 client（client 启动即要连 ws）
pub fn start_all(state: &AppState) -> Result<String> {
    let mut msgs = Vec::new();
    msgs.push(start_server(state)?);

    // 等 ASR 端口就绪，最多 20 秒（模型加载可能较慢）
    for _ in 0..80 {
        if crate::state::probe_asr_port() {
            break;
        }
        std::thread::sleep(Duration::from_millis(250));
    }

    msgs.push(start_client(state)?);
    Ok(msgs.join("；"))
}

#[cfg(windows)]
fn kill_tree(pid: u32) -> Result<()> {
    let mut cmd = Command::new("taskkill");
    cmd.args(["/PID", &pid.to_string(), "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    cmd.status().map_err(|e| anyhow!("taskkill 失败: {e}"))?;
    Ok(())
}

#[cfg(not(windows))]
fn kill_tree(pid: u32) -> Result<()> {
    Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status()
        .map_err(|e| anyhow!("kill 失败: {e}"))?;
    Ok(())
}

pub fn stop_all(state: &AppState) -> Result<String> {
    let snap = state.snapshot();
    let mut n = 0;
    // 先停 client 再停 server，顺序反了会让 client 报连接断开
    for pid in snap.client_pids.iter().chain(snap.server_pids.iter()) {
        if kill_tree(*pid).is_ok() {
            n += 1;
        }
    }
    if n == 0 {
        Ok("没有正在运行的后端进程".into())
    } else {
        Ok(format!("已停止 {n} 个进程"))
    }
}

pub fn restart_all(state: &AppState) -> Result<String> {
    stop_all(state)?;
    // 给端口释放留出时间，否则新 server 绑定会失败
    std::thread::sleep(Duration::from_millis(900));
    start_all(state)
}

/// 切换 ASR 模型：改写 config_server.py 的 model_type 再重启后端
pub fn switch_model(state: &AppState, model: &str) -> Result<String> {
    const ALLOWED: &[&str] = &[
        "qwen_asr",
        "qwen_asr_0_6b",
        "fun_asr_nano",
        "sensevoice",
        "paraformer",
    ];
    if !ALLOWED.contains(&model) {
        return Err(anyhow!("未知模型: {model}"));
    }

    let path = state.root().join("config_server.py");
    let content = std::fs::read_to_string(&path)
        .map_err(|e| anyhow!("读取 config_server.py 失败: {e}"))?;

    let mut replaced = false;
    let mut out = String::with_capacity(content.len() + 32);
    for line in content.lines() {
        let trimmed = line.trim_start();
        if !replaced && trimmed.starts_with("model_type") && trimmed.contains('=') {
            let indent_len = line.len() - trimmed.len();
            let indent = &line[..indent_len];
            // 保留行尾注释
            let comment = line.find('#').map(|i| &line[i..]).unwrap_or("");
            if comment.is_empty() {
                out.push_str(&format!("{indent}model_type = '{model}'"));
            } else {
                out.push_str(&format!("{indent}model_type = '{model}'    {comment}"));
            }
            out.push('\n');
            replaced = true;
        } else {
            out.push_str(line);
            out.push('\n');
        }
    }

    if !replaced {
        return Err(anyhow!("config_server.py 中未找到 model_type 行"));
    }

    std::fs::write(&path, out).map_err(|e| anyhow!("写入 config_server.py 失败: {e}"))?;
    let msg = restart_all(state)?;
    Ok(format!("已切换到 {model}；{msg}"))
}
