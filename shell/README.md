# CapsWriter 现代化外壳（Tauri + WebView2）

替代 Tkinter 的 UI 层：Toast 浮层 + Dashboard 面板 + 系统托盘。
Python 侧只负责 ASR / 热词 / LLM，UI 全部交给外壳。

## 为什么换

旧 Tk 实现有三个**算法层面**的性能缺陷，换语言本身并不解决，必须换掉调用模式：

| 位置 | 问题 | 实测 |
| --- | --- | --- |
| `toast_text.py` 流式更新 | 每个 chunk 都 `update_idletasks()` + `count(displaylines)`，两者皆 O(n) → 整体 O(n²) | 1860 字累计 **14.7s**，UI 落后 LLM 8.2s |
| `capswriter_gui.pyw` 状态轮询 | `process_iter(['cmdline'])` 扫全部 ~570 进程，每参数还 `Path.resolve()` | 单轮 **3.2s**，而刷新周期 2.5s |
| `toast_manager.py` 队列轮询 | `after(100ms)` 轮询消息队列 | 首帧白等 **50~110ms** |

外壳对应的做法：

- 增量追加文本节点，Markdown 只在收尾解析一次；高度自适应交给 CSS，无 JS 测量
- Rust 侧按 16ms（60fps）聚合增量再下发，生产端速度与渲染次数解耦
- 进程探测先按**进程名**粗筛，命中才读 cmdline；端口用 `connect_timeout` 直连探测
- 浮层预创建并隐藏，显示即 `show()`；TCP 可读即处理，无轮询

实测结果（同一段 1860 字，40 tok/s）：

```
UI 累计开销   24.7 ms   (Tk: 14724 ms → 快 596x)
墙钟          13.50s    理论 13.50s   附加 +0 ms
丢弃事件      0
```

## 构建

需要 Rust、Node、MSVC 生成工具、WebView2 Runtime（Win11 自带）。

```bash
cd shell
npm install
npm run tauri build          # 出 exe + NSIS 安装包
npx tauri build --no-bundle  # 只要 exe，构建更快
```

产物：`shell/src-tauri/target/release/capswriter-shell.exe`（约 3.6 MB，常驻内存 ~25 MB）

**注意**：不要用裸 `cargo build`。前端资源由 Tauri CLI 负责构建并嵌入，
`cargo build` 出来的二进制会指向开发服务器地址，运行后是一片空白。

开发模式（前端热更新）：

```bash
npm run tauri dev
```

## 运行

```
启动外壳.cmd
```

外壳常驻系统托盘。左键点图标开/关面板，右键出菜单（启动/重启/停止后端、退出）。
带 `--show` 参数启动会直接打开面板。

## 与 Python 的关系

外壳是**纯增强**，不是依赖：

- 外壳在线 → 浮层/通知走 WebView2
- 外壳没装、没启动、崩了 → 自动回退到原来的 Tkinter，行为与历史版本一致

开关在 `config_client.py`：

```python
use_shell_ui = True   # False 则始终使用 Tkinter
```

代码上由 `util/ui/toast_adapter.py` 统一决策，调用方只用 `ToastSession`，
不必关心底层是哪套实现。

## 通信

Python → 外壳：本机 TCP `127.0.0.1:6020`，每条消息一行 JSON（NDJSON）。
选 TCP 而非 UDP 是因为流式文本必须有序且不可丢包。

事件：`toast_open` / `toast_append`（发**增量**）/ `toast_finish` / `toast_close` /
`recording_state` / `recognition` / `notify` / `heartbeat`。

发送端（`util/ui/shell_bridge.py`）的要点：

- 全部异步入队，**绝不阻塞识别链路**
- 一次 `sendall` 合并最多 256 条，否则 LLM 高速吐字时 syscall 次数会让队列溢出丢字
- 文本类事件不可丢（丢了就是内容缺失）；状态类事件（心跳/录音/识别摘要）是快照，可丢最旧的
- 心跳带上安装目录，供外壳区分同机的多份 CapsWriter

端口分配：`6016` ASR server、`6018` UDP 控制、`6019` 旧 GUI 控制、`6020` 外壳桥、`6021` vite dev。

## 结构

```
shell/
├── index.html          Dashboard
├── overlay.html        Toast 浮层
├── src/
│   ├── theme.css       设计令牌
│   ├── overlay.ts      浮层逻辑（性能关键路径）
│   ├── overlay.css
│   ├── dashboard.ts    面板逻辑
│   └── dashboard.css
└── src-tauri/
    ├── src/
    │   ├── main.rs       入口、命令、托盘
    │   ├── bridge.rs     TCP 桥 + 60fps 帧聚合
    │   ├── protocol.rs   IPC 协议
    │   ├── state.rs      共享状态 + 进程/端口探测
    │   ├── window.rs     窗口管理 + 浮层看门狗
    │   └── backend.rs    Python 进程启停
    └── capabilities/     Tauri 2 ACL 权限声明
```

## 日志查看

真实日志会长到 10MB / 9 万行（实测某台机器 logs 目录累计 906MB、203 个文件），
所以读取策略是：

- **只从文件末尾读固定字节**（按行均长估算，64KB~1MB），不把整文件读进内存。
  9.8MB 文件实测后端读取 0ms；整文件读入是 23ms 且随文件增长线性变差。
- 后端按 mtime 短路：文件没变化就返回 `None`，前端不重绘。
- 一次传整段文本而非 300 条字符串，避免逐条过 IPC 序列化。
- 前端用一次 `innerHTML` 写入（行文本已转义），不逐行 `createElement`。
  300 行渲染 1ms。
- 日志只在对应页面打开时刷新；面板隐藏时完全停止轮询。

选哪个文件：优先按**文件名里的日期**取最大，日期相同再比 mtime。
不单纯用 mtime，因为备份/同步/复制会改写它，可能让旧日期的文件看起来更新。

调试时可以用 `--page=client` 直接打开日志页。

## 排障

前端错误会回传到外壳 stderr（`ui_log` 命令），启动器把它重定向到 `logs/shell.err.log`。
WebView 的 console 在这里看不到，出问题先看这个文件。

两个容易踩的坑，都已在代码里处理：

- **Tauri 2 ACL**：`event.listen` 等命令必须在 `capabilities/default.json` 里显式授权，
  否则前端 `invoke` 全部被拒且只在 WebView console 里报错，表现为「窗口在但毫无反应」。
- **CSP**：IPC 需要 `ipc: http://ipc.localhost`，漏了同样会让所有 `invoke` 失败。
