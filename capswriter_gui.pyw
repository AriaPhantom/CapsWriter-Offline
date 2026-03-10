from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import ctypes
import re
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from tkinter import StringVar, Tk, ttk
from tkinter import scrolledtext

import psutil
import pystray
from PIL import Image, ImageDraw


ROOT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = ROOT_DIR / "assets"
ICON_PATH = ASSETS_DIR / "icon.ico"
LOG_DIR = ROOT_DIR / "logs"
PORT = 6016
CONTROL_PORT = 6019
CREATE_NO_WINDOW = 0x08000000


@dataclass(frozen=True)
class LaunchTarget:
    command: tuple[str, ...]
    match_mode: str
    match_path: Path


class Theme:
    BG = "#0B0F1A"         # Deepest Blue-Black
    SIDEBAR = "#151B2B"    # Dark Sidebar
    PANEL = "#1E2538"      # Panel background
    CARD = "#1E2538"       # Card background
    CARD_HOVER = "#2A344D"
    TEXT = "#F1F5F9"       # Clean white
    MUTED = "#94A3B8"      # Slate 400
    PRIMARY = "#3B82F6"    # Modern Blue
    SUCCESS = "#10B981"    # Emerald
    ERROR = "#EF4444"      # Red
    BORDER = "#2E3A52"     # Subtle border


class BackendManager:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.backend_python = self._resolve_backend_python()
        self.server_target = self._resolve_target("start_server")
        self.client_target = self._resolve_target("start_client")

    def _resolve_backend_python(self) -> Path:
        current = Path(sys.executable).resolve()
        python_exe = current.with_name("python.exe")
        if current.name.lower() == "pythonw.exe" and python_exe.exists():
            return python_exe
        return current

    def _resolve_target(self, stem: str) -> LaunchTarget:
        exe_path = (self.root_dir / f"{stem}.exe").resolve()
        if exe_path.exists():
            return LaunchTarget(
                command=(str(exe_path),),
                match_mode="exe",
                match_path=exe_path,
            )

        script_path = (self.root_dir / f"{stem}.py").resolve()
        if script_path.exists():
            return LaunchTarget(
                command=(str(self.backend_python), str(script_path)),
                match_mode="script",
                match_path=script_path,
            )

        raise FileNotFoundError(f"未找到 {stem}.exe 或 {stem}.py")

    def _matching_processes(self, target: LaunchTarget) -> list[psutil.Process]:
        matches: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
            try:
                if target.match_mode == "exe":
                    exe = proc.info.get("exe")
                    if exe and Path(exe).resolve() == target.match_path:
                        matches.append(proc)
                    continue

                cmdline = proc.info.get("cmdline") or []
                resolved_args: list[str] = []
                for arg in cmdline[1:]:
                    try:
                        resolved_args.append(str(Path(arg).resolve()))
                    except (OSError, RuntimeError):
                        resolved_args.append(os.path.normcase(arg))
                if str(target.match_path) in resolved_args:
                    matches.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, FileNotFoundError, OSError):
                continue
        return matches

    def server_processes(self) -> list[psutil.Process]:
        return self._matching_processes(self.server_target)

    def client_processes(self) -> list[psutil.Process]:
        return self._matching_processes(self.client_target)

    def is_port_open(self, host: str = "127.0.0.1", port: int = PORT) -> bool:
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if not conn.laddr:
                    continue
                if conn.laddr.port != port:
                    continue
                if conn.status != psutil.CONN_LISTEN:
                    continue
                if host not in ("0.0.0.0", "127.0.0.1"):
                    if conn.laddr.ip not in (host, "0.0.0.0"):
                        continue
                return True
        except Exception:
            return False
        return False

    def _spawn_hidden(self, target: LaunchTarget) -> None:
        subprocess.Popen(
            list(target.command),
            cwd=str(self.root_dir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )

    def _backend_family_pids(self) -> set[int]:
        pids: set[int] = set()
        roots = self.server_processes() + self.client_processes()
        for proc in roots:
            try:
                pids.add(proc.pid)
                for child in proc.children(recursive=True):
                    pids.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return pids

    def hide_backend_windows(self) -> None:
        target_pids = self._backend_family_pids()
        if not target_pids:
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        enum_windows = user32.EnumWindows
        show_window = user32.ShowWindow
        is_window_visible = user32.IsWindowVisible
        get_window_thread_process_id = user32.GetWindowThreadProcessId
        SW_HIDE = 0

        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            get_window_thread_process_id(hwnd, ctypes.byref(pid))
            if pid.value in target_pids and is_window_visible(hwnd):
                show_window(hwnd, SW_HIDE)
            return True

        enum_windows(callback_type(callback), 0)

    def start_all(self) -> None:
        if not self.server_processes():
            self._spawn_hidden(self.server_target)
        for _ in range(40):
            if self.is_port_open():
                break
            time.sleep(0.25)
        if not self.client_processes():
            self._spawn_hidden(self.client_target)
        time.sleep(1.0)
        self.hide_backend_windows()

    def stop_all(self) -> None:
        procs: dict[int, psutil.Process] = {}
        for proc in self.client_processes() + self.server_processes():
            try:
                procs[proc.pid] = proc
                for child in proc.children(recursive=True):
                    procs[child.pid] = child
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for proc in procs.values():
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        deadline = time.time() + 5
        while time.time() < deadline:
            if not self.server_processes() and not self.client_processes():
                break
            time.sleep(0.2)

    def restart_all(self) -> None:
        self.stop_all()
        time.sleep(1.0)
        self.start_all()


class CapsWriterGUI:
    def __init__(
        self,
        start_minimized: bool = False,
        smoke_test: bool = False,
        backend_action: str = "start",
    ) -> None:
        self.manager = BackendManager(ROOT_DIR)
        self.start_minimized = start_minimized
        self.smoke_test = smoke_test
        self.backend_action = backend_action

        self.root = Tk()
        self.root.title("CapsWriter Offline Control Center")
        self.root.geometry("1040x720")
        self.root.minsize(920, 620)
        self.root.configure(bg=Theme.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.bind("<Unmap>", self._on_unmap)

        try:
            if ICON_PATH.exists():
                self.root.iconbitmap(str(ICON_PATH))
        except Exception:
            pass

        self.status_backend = StringVar(value="检测中…")
        self.status_port = StringVar(value="检测中…")
        self.status_client = StringVar(value="检测中…")
        self.last_result = StringVar(value="尚无识别结果")
        self.last_error = StringVar(value="系统状态正常")
        self.activity = StringVar(value="准备就绪")

        self.client_log_choice = StringVar(value="")
        self.server_log_choice = StringVar(value="")
        self.client_log_combo: ttk.Combobox | None = None
        self.server_log_combo: ttk.Combobox | None = None
        self.client_text: scrolledtext.ScrolledText | None = None
        self.server_text: scrolledtext.ScrolledText | None = None
        self.last_log_state: dict[str, tuple[str, float]] = {"client": ("", 0.0), "server": ("", 0.0)}
        self.log_auto_follow: dict[str, bool] = {"client": True, "server": True}

        self.tray_icon: pystray.Icon | None = None
        self.tray_thread: threading.Thread | None = None
        self.control_stop = threading.Event()
        self.control_thread: threading.Thread | None = None
        self.refresh_job: str | None = None
        
        self.sidebar_buttons: list[ttk.Button] = []
        self.current_page = "overview"

        self._build_styles()
        self._build_ui()
        self._populate_log_choices(initial=True)
        self._start_control_server()
        self._run_initial_backend_action()
        self._schedule_refresh()

        if self.smoke_test:
            self.root.after(3000, self._finish_smoke_test)
        elif self.start_minimized:
            self.root.after(700, self.hide_to_tray)

    def _build_styles(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        
        # Base Frames
        style.configure("Caps.TFrame", background=Theme.BG)
        style.configure("Sidebar.TFrame", background=Theme.SIDEBAR)
        style.configure("Main.TFrame", background=Theme.BG)
        style.configure("Card.TFrame", background=Theme.CARD, borderwidth=1, relief="flat")
        style.configure("Separator.TFrame", background=Theme.BORDER)

        # Labels
        style.configure("Caps.TLabel", background=Theme.BG, foreground=Theme.TEXT)
        style.configure("Sidebar.TLabel", background=Theme.SIDEBAR, foreground=Theme.TEXT)
        style.configure("Muted.TLabel", background=Theme.BG, foreground=Theme.MUTED)
        style.configure("CardTitle.TLabel", background=Theme.CARD, foreground=Theme.MUTED)
        style.configure("CardValue.TLabel", background=Theme.CARD, foreground=Theme.TEXT)
        style.configure("SidebarMuted.TLabel", background=Theme.SIDEBAR, foreground=Theme.MUTED)
        
        # Buttons
        style.configure("Caps.TButton", background=Theme.CARD_HOVER, foreground=Theme.TEXT, borderwidth=0, padding=(12, 6))
        style.map("Caps.TButton", background=[("active", Theme.PRIMARY)])
        
        style.configure("Primary.TButton", background=Theme.PRIMARY, foreground="white", borderwidth=0, padding=(16, 8))
        style.map("Primary.TButton", background=[("active", "#2563EB")])
        
        style.configure("Sidebar.TButton", background=Theme.SIDEBAR, foreground=Theme.MUTED, borderwidth=0, anchor="w", padding=(20, 12), font=("Segoe UI", 10))
        style.map("Sidebar.TButton", 
                  background=[("active", Theme.CARD_HOVER), ("selected", Theme.CARD_HOVER)],
                  foreground=[("active", Theme.TEXT), ("selected", Theme.PRIMARY)])

        style.configure("SidebarAction.TButton", background=Theme.SIDEBAR, foreground=Theme.MUTED, borderwidth=0, anchor="w", padding=(20, 6), font=("Segoe UI", 9))
        style.map("SidebarAction.TButton", background=[("active", Theme.CARD_HOVER)], foreground=[("active", Theme.TEXT)])

        # Combobox
        style.configure("Caps.TCombobox", fieldbackground=Theme.CARD_HOVER, background=Theme.CARD_HOVER, foreground=Theme.TEXT, borderwidth=0)

    def _build_ui(self) -> None:
        # 1. Main Layout: Sidebar + Content
        self.sidebar = ttk.Frame(self.root, style="Sidebar.TFrame", width=210)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        self.main_container = ttk.Frame(self.root, style="Main.TFrame")
        self.main_container.pack(side="right", fill="both", expand=True)

        # 2. Sidebar Content
        # Logo Area
        logo_area = ttk.Frame(self.sidebar, style="Sidebar.TFrame", padding=(18, 24))
        logo_area.pack(fill="x")
        ttk.Label(logo_area, text="CapsWriter", style="Sidebar.TLabel", font=("Segoe UI Semibold", 18)).pack(anchor="w")
        ttk.Label(logo_area, text="OFFLINE", style="SidebarMuted.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(2, 0))

        # Nav Buttons
        nav_area = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        nav_area.pack(fill="x", pady=20)
        
        self.btn_overview = self._nav_button(nav_area, "Dashboard", "overview")
        self.btn_client_logs = self._nav_button(nav_area, "Client Logs", "client_logs")
        self.btn_server_logs = self._nav_button(nav_area, "Server Logs", "server_logs")

        # Sidebar Bottom Actions
        bottom_area = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        bottom_area.pack(side="bottom", fill="x", pady=20)
        
        ttk.Frame(bottom_area, style="Separator.TFrame", height=1).pack(fill="x", pady=10)
        self._sidebar_action(bottom_area, "Logs Directory", lambda: os.startfile(str(LOG_DIR))).pack(fill="x")
        self._sidebar_action(bottom_area, "Install Directory", lambda: os.startfile(str(ROOT_DIR))).pack(fill="x")
        self._sidebar_action(bottom_area, "Minimize to Tray", self.hide_to_tray).pack(fill="x")

        # 3. Main Pages
        self.pages = {}
        self.pages["overview"] = self._create_overview_page()
        self.pages["client_logs"] = self._create_logs_page("client")
        self.pages["server_logs"] = self._create_logs_page("server")
        
        self._show_page("overview")

    def _nav_button(self, parent, text: str, page_id: str):
        btn = ttk.Button(parent, text=f"  ●  {text}", style="Sidebar.TButton", command=lambda: self._show_page(page_id))
        btn.pack(fill="x")
        self.sidebar_buttons.append(btn)
        return btn

    def _sidebar_action(self, parent, text: str, command):
        return ttk.Button(parent, text=f"  →  {text}", style="SidebarAction.TButton", command=command)

    def _show_page(self, page_id: str):
        for p in self.pages.values():
            p.pack_forget()
        self.pages[page_id].pack(fill="both", expand=True)
        self.current_page = page_id
        
        # Update button states (pseudo-selection)
        for btn in self.sidebar_buttons:
            btn.state(["!selected"])
        if page_id == "overview": self.btn_overview.state(["selected"])
        elif page_id == "client_logs": self.btn_client_logs.state(["selected"])
        elif page_id == "server_logs": self.btn_server_logs.state(["selected"])

    def _create_overview_page(self) -> ttk.Frame:
        page = ttk.Frame(self.main_container, style="Main.TFrame", padding=22)
        
        # Header
        header = ttk.Frame(page, style="Main.TFrame")
        header.pack(fill="x", pady=(0, 20))
        ttk.Label(header, text="Dashboard Overview", style="Caps.TLabel", font=("Segoe UI Semibold", 20)).pack(side="left")
        
        actions = ttk.Frame(header, style="Main.TFrame")
        actions.pack(side="right")
        ttk.Button(actions, text="Start Backend", style="Primary.TButton", command=lambda: self._run_async("Starting Services...", self.manager.start_all)).pack(side="left", padx=5)
        ttk.Button(actions, text="Restart", style="Caps.TButton", command=lambda: self._run_async("Restarting Services...", self.manager.restart_all)).pack(side="left", padx=5)
        ttk.Button(actions, text="Stop", style="Caps.TButton", command=lambda: self._run_async("Stopping Services...", self.manager.stop_all)).pack(side="left", padx=5)

        # Status Grid
        status_row = ttk.Frame(page, style="Main.TFrame")
        status_row.pack(fill="x", pady=(0, 18))
        status_row.columnconfigure((0, 1, 2), weight=1, uniform="status")
        
        self._status_card(status_row, "Service Status", self.status_backend, 0)
        self._status_card(status_row, "ASR Server (6016)", self.status_port, 1)
        self._status_card(status_row, "Active Client", self.status_client, 2)

        # Result Area
        result_card = ttk.Frame(page, style="Card.TFrame", padding=18)
        result_card.pack(fill="both", expand=True)
        
        ttk.Label(result_card, text="LATEST RECOGNITION", style="CardTitle.TLabel", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        res_label = ttk.Label(result_card, textvariable=self.last_result, style="CardValue.TLabel", font=("Segoe UI Semibold", 15), wraplength=680, justify="left")
        res_label.pack(anchor="w", pady=(12, 22), fill="x")
        
        ttk.Frame(result_card, style="Separator.TFrame", height=1).pack(fill="x", pady=(0, 20))
        
        err_row = ttk.Frame(result_card, style="Card.TFrame")
        err_row.pack(fill="x")
        ttk.Label(err_row, text="SYSTEM STATUS", style="CardTitle.TLabel", font=("Segoe UI", 8, "bold")).pack(side="left")
        ttk.Label(err_row, textvariable=self.last_error, style="CardValue.TLabel", font=("Consolas", 9)).pack(side="left", padx=(12, 0))

        # Footer Status
        footer = ttk.Frame(page, style="Main.TFrame")
        footer.pack(fill="x", pady=(20, 0))
        ttk.Label(footer, textvariable=self.activity, style="Muted.TLabel", font=("Segoe UI", 9)).pack(side="left")
        
        return page

    def _create_logs_page(self, prefix: str) -> ttk.Frame:
        page = ttk.Frame(self.main_container, style="Main.TFrame", padding=22)
        
        header = ttk.Frame(page, style="Main.TFrame")
        header.pack(fill="x", pady=(0, 16))
        title = "Client Process Logs" if prefix == "client" else "Server Process Logs"
        ttk.Label(header, text=title, style="Caps.TLabel", font=("Segoe UI Semibold", 20)).pack(side="left")
        
        controls = ttk.Frame(page, style="Main.TFrame")
        controls.pack(fill="x", pady=(0, 12))
        
        choice = self.client_log_choice if prefix == "client" else self.server_log_choice
        combo = ttk.Combobox(controls, textvariable=choice, state="readonly", width=45, style="Caps.TCombobox")
        combo.pack(side="left", padx=(0, 10))
        combo.bind("<<ComboboxSelected>>", lambda _event, p=prefix: self._on_log_selected(p))
        if prefix == "client":
            self.client_log_combo = combo
        else:
            self.server_log_combo = combo
            
        ttk.Button(controls, text="Refresh", style="Caps.TButton", command=lambda p=prefix: self._refresh_log_text(p, force=True)).pack(side="left", padx=5)
        ttk.Button(controls, text="Open File", style="Caps.TButton", command=lambda p=prefix: self._open_selected_log(p)).pack(side="left", padx=5)

        text_container = ttk.Frame(page, style="Card.TFrame", padding=1)
        text_container.pack(fill="both", expand=True)
        
        text = scrolledtext.ScrolledText(text_container, wrap="word", bg=Theme.BG, fg=Theme.TEXT, 
                                        insertbackground=Theme.TEXT, relief="flat", borderwidth=0, 
                                        font=("Consolas", 10), padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.configure(state="disabled")
        
        if prefix == "client": self.client_text = text
        else: self.server_text = text
        
        return page

    def _status_card(self, parent, title: str, variable: StringVar, column: int):
        frame = ttk.Frame(parent, style="Card.TFrame", padding=(16, 14))
        frame.grid(row=0, column=column, sticky="nsew", padx=5 if column == 1 else (0, 5) if column == 0 else (5, 0))
        
        ttk.Label(frame, text=title.upper(), style="CardTitle.TLabel", font=("Segoe UI", 8, "bold")).pack(anchor="w")
        val_label = ttk.Label(frame, textvariable=variable, style="CardValue.TLabel", font=("Segoe UI Semibold", 15))
        val_label.pack(anchor="w", pady=(8, 0))
        return frame

    @staticmethod
    def _log_day_token(file_name: str) -> str:
        match = re.match(r"^(?:client|server)_(\d{8})\.log$", file_name)
        return match.group(1) if match else ""

    def _should_roll_to_latest_day(self, current_name: str, latest_name: str) -> bool:
        current_day = self._log_day_token(current_name)
        latest_day = self._log_day_token(latest_name)
        return bool(current_day and latest_day and latest_day > current_day)

    def _on_log_selected(self, prefix: str) -> None:
        variable = self.client_log_choice if prefix == "client" else self.server_log_choice
        selected = variable.get()
        latest = self._latest_log(prefix)
        self.log_auto_follow[prefix] = bool(latest and selected == latest.name)
        self._refresh_log_text(prefix, force=True)

    def _schedule_refresh(self) -> None:
        if self.control_stop.is_set():
            return
        self._refresh_status()
        self._populate_log_choices()
        self._refresh_log_text("client")
        self._refresh_log_text("server")
        self.refresh_job = self.root.after(1500, self._schedule_refresh)

    def _refresh_status(self) -> None:
        server_count = len(self.manager.server_processes())
        client_count = len(self.manager.client_processes())
        port_open = self.manager.is_port_open()
        
        if server_count and client_count and port_open:
            self.status_backend.set("Running")
        elif server_count or client_count:
            self.status_backend.set("Partial")
        else:
            self.status_backend.set("Stopped")
            
        self.status_port.set("Listening" if port_open else "Offline")
        self.status_client.set(f"Active ({client_count})" if client_count else "Inactive")
        
        latest_result = self._extract_last_result()
        if latest_result:
            self.last_result.set(latest_result)
        
        err = self._extract_last_error()
        self.last_error.set(err if err else "System Status: Normal")

    def _populate_log_choices(self, initial: bool = False) -> None:
        for prefix, combo, variable in (("client", self.client_log_combo, self.client_log_choice), ("server", self.server_log_combo, self.server_log_choice)):
            if combo is None:
                continue
            files = sorted(LOG_DIR.glob(f"{prefix}_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            names = [f.name for f in files]
            combo["values"] = names

            if not names:
                variable.set("")
                self.log_auto_follow[prefix] = True
                continue

            current = variable.get()
            latest = names[0]

            if not current or current not in names:
                variable.set(latest)
                self.log_auto_follow[prefix] = True
            elif self.log_auto_follow[prefix]:
                if current != latest:
                    variable.set(latest)
            elif self._should_roll_to_latest_day(current, latest):
                variable.set(latest)
                self.log_auto_follow[prefix] = True
            elif initial:
                variable.set(current)

    def _selected_log_path(self, prefix: str) -> Path | None:
        name = self.client_log_choice.get() if prefix == "client" else self.server_log_choice.get()
        if not name:
            return None
        path = LOG_DIR / name
        return path if path.exists() else None

    def _refresh_log_text(self, prefix: str, force: bool = False) -> None:
        text_widget = self.client_text if prefix == "client" else self.server_text
        if text_widget is None:
            return
        path = self._selected_log_path(prefix)
        if path is None:
            return
        mtime = path.stat().st_mtime
        if not force and self.last_log_state[prefix] == (str(path), mtime):
            return
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                tail = "".join(deque(fh, maxlen=400))
        except OSError as exc:
            tail = f"Failed to read logs: {exc}"
        text_widget.configure(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", tail)
        text_widget.see("end")
        text_widget.configure(state="disabled")
        self.last_log_state[prefix] = (str(path), mtime)

    def _extract_last_result(self) -> str:
        for prefix in ("client", "server"):
            path = self._selected_log_path(prefix) or self._latest_log(prefix)
            if not path:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                for marker in ("收到最终识别结果:", "麦克风识别结果:"):
                    if marker in line:
                        return line.split(marker, 1)[1].strip()
        return ""

    def _extract_last_error(self) -> str:
        for prefix in ("client", "server"):
            path = self._selected_log_path(prefix) or self._latest_log(prefix)
            if not path:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if " - ERROR - " in line:
                    # Clean up error line for display
                    return line.split(" - ERROR - ", 1)[1].strip()
        return ""

    def _latest_log(self, prefix: str) -> Path | None:
        files = sorted(LOG_DIR.glob(f"{prefix}_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None

    def _open_selected_log(self, prefix: str) -> None:
        path = self._selected_log_path(prefix)
        if path:
            os.startfile(str(path))

    def _run_async(self, activity_text: str, func) -> None:
        self.activity.set(activity_text)

        def worker() -> None:
            try:
                func()
                self.root.after(0, lambda: self.activity.set(f"{activity_text} Done"))
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: self.activity.set(f"Operation Failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _run_initial_backend_action(self) -> None:
        actions = {
            "start": ("Starting Backend...", self.manager.start_all),
            "restart": ("Restarting Backend...", self.manager.restart_all),
            "stop": ("Stopping Backend...", self.manager.stop_all),
        }
        activity_text, func = actions.get(self.backend_action, actions["start"])
        self._run_async(activity_text, func)


    @staticmethod
    def _log_day_token(file_name: str) -> str:
        match = re.match(r"^(?:client|server)_(\d{8})\.log$", file_name)
        return match.group(1) if match else ""

    def _should_roll_to_latest_day(self, current_name: str, latest_name: str) -> bool:
        current_day = self._log_day_token(current_name)
        latest_day = self._log_day_token(latest_name)
        return bool(current_day and latest_day and latest_day > current_day)

    def _on_log_selected(self, prefix: str) -> None:
        variable = self.client_log_choice if prefix == "client" else self.server_log_choice
        selected = variable.get()
        latest = self._latest_log(prefix)
        self.log_auto_follow[prefix] = bool(latest and selected == latest.name)
        self._refresh_log_text(prefix, force=True)

    def _schedule_refresh(self) -> None:
        if self.control_stop.is_set():
            return
        self._refresh_status()
        self._populate_log_choices()
        self._refresh_log_text("client")
        self._refresh_log_text("server")
        self.refresh_job = self.root.after(1500, self._schedule_refresh)

    def _refresh_status(self) -> None:
        server_count = len(self.manager.server_processes())
        client_count = len(self.manager.client_processes())
        port_open = self.manager.is_port_open()
        if server_count and client_count and port_open:
            self.status_backend.set("已运行")
        elif server_count or client_count:
            self.status_backend.set("部分运行")
        else:
            self.status_backend.set("未运行")
        self.status_port.set("监听中" if port_open else "未监听")
        self.status_client.set(f"运行中（{client_count}）" if client_count else "未运行")
        latest_result = self._extract_last_result()
        if latest_result:
            self.last_result.set(latest_result)
        self.last_error.set(self._extract_last_error() or "尚无错误")

    def _populate_log_choices(self, initial: bool = False) -> None:
        for prefix, combo, variable in (("client", self.client_log_combo, self.client_log_choice), ("server", self.server_log_combo, self.server_log_choice)):
            if combo is None:
                continue
            files = sorted(LOG_DIR.glob(f"{prefix}_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
            names = [f.name for f in files]
            combo["values"] = names

            if not names:
                variable.set("")
                self.log_auto_follow[prefix] = True
                continue

            current = variable.get()
            latest = names[0]

            if not current or current not in names:
                variable.set(latest)
                self.log_auto_follow[prefix] = True
            elif self.log_auto_follow[prefix]:
                if current != latest:
                    variable.set(latest)
            elif self._should_roll_to_latest_day(current, latest):
                variable.set(latest)
                self.log_auto_follow[prefix] = True
            elif initial:
                variable.set(current)

    def _selected_log_path(self, prefix: str) -> Path | None:
        name = self.client_log_choice.get() if prefix == "client" else self.server_log_choice.get()
        if not name:
            return None
        path = LOG_DIR / name
        return path if path.exists() else None

    def _refresh_log_text(self, prefix: str, force: bool = False) -> None:
        text_widget = self.client_text if prefix == "client" else self.server_text
        if text_widget is None:
            return
        path = self._selected_log_path(prefix)
        if path is None:
            return
        mtime = path.stat().st_mtime
        if not force and self.last_log_state[prefix] == (str(path), mtime):
            return
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                tail = "".join(deque(fh, maxlen=400))
        except OSError as exc:
            tail = f"无法读取日志：{exc}"
        text_widget.configure(state="normal")
        text_widget.delete("1.0", "end")
        text_widget.insert("1.0", tail)
        text_widget.see("end")
        text_widget.configure(state="disabled")
        self.last_log_state[prefix] = (str(path), mtime)

    def _extract_last_result(self) -> str:
        for prefix in ("client", "server"):
            path = self._selected_log_path(prefix) or self._latest_log(prefix)
            if not path:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                for marker in ("收到最终识别结果:", "麦克风识别结果:"):
                    if marker in line:
                        return line.split(marker, 1)[1].strip()
        return ""

    def _extract_last_error(self) -> str:
        for prefix in ("client", "server"):
            path = self._selected_log_path(prefix) or self._latest_log(prefix)
            if not path:
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if " - ERROR - " in line:
                    return line.strip()
        return ""

    def _latest_log(self, prefix: str) -> Path | None:
        files = sorted(LOG_DIR.glob(f"{prefix}_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        return files[0] if files else None

    def _open_selected_log(self, prefix: str) -> None:
        path = self._selected_log_path(prefix)
        if path:
            os.startfile(str(path))

    def _run_async(self, activity_text: str, func) -> None:
        self.activity.set(activity_text)

        def worker() -> None:
            try:
                func()
                self.root.after(0, lambda: self.activity.set(f"{activity_text} 完成"))
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda: self.activity.set(f"操作失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _run_initial_backend_action(self) -> None:
        actions = {
            "start": ("正在启动后台…", self.manager.start_all),
            "restart": ("正在重启后台…", self.manager.restart_all),
            "stop": ("正在停止后台…", self.manager.stop_all),
        }
        activity_text, func = actions.get(self.backend_action, actions["start"])
        self._run_async(activity_text, func)

    def _on_unmap(self, _event) -> None:
        try:
            if self.root.state() == "iconic":
                self.hide_to_tray()
        except Exception:
            pass

    def hide_to_tray(self) -> None:
        self._ensure_tray_icon()
        self.root.withdraw()
        self.activity.set("已最小化到托盘")

    def show_window(self) -> None:
        self.root.after(0, self._show_window_impl)

    def _show_window_impl(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.activity.set("面板已打开")

    def _ensure_tray_icon(self) -> None:
        if self.tray_icon is not None:
            return
        image = self._load_icon()
        menu = pystray.Menu(
            pystray.MenuItem("打开面板", lambda: self.show_window(), default=True),
            pystray.MenuItem("启动后台", lambda: self._run_async("正在启动后台…", self.manager.start_all)),
            pystray.MenuItem("重启后台", lambda: self._run_async("正在重启后台…", self.manager.restart_all)),
            pystray.MenuItem("停止后台", lambda: self._run_async("正在停止后台…", self.manager.stop_all)),
            pystray.MenuItem("打开日志目录", lambda: os.startfile(str(LOG_DIR))),
            pystray.MenuItem("退出 GUI（后台继续）", lambda: self.exit_gui(False)),
            pystray.MenuItem("退出全部", lambda: self.exit_gui(True)),
        )
        self.tray_icon = pystray.Icon("capswriter_gui", image, "CapsWriter Control Center", menu)
        self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
        self.tray_thread.start()

    def _start_control_server(self) -> None:
        def worker() -> None:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind(("127.0.0.1", CONTROL_PORT))
                server.listen(5)
                server.settimeout(0.5)
                while not self.control_stop.is_set():
                    try:
                        conn, _addr = server.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    with conn:
                        try:
                            command = conn.recv(64).decode("utf-8", errors="ignore").strip().upper()
                        except OSError:
                            command = ""
                    if command == "SHOW":
                        self.root.after(0, self._show_window_impl)
                    elif command == "START":
                        self.root.after(0, lambda: self._run_async("正在启动后台…", self.manager.start_all))
                    elif command == "STOP":
                        self.root.after(0, lambda: self._run_async("正在停止后台…", self.manager.stop_all))
                    elif command == "RESTART":
                        self.root.after(0, lambda: self._run_async("正在重启后台…", self.manager.restart_all))
                    elif command == "EXIT":
                        self.root.after(0, lambda: self.exit_gui(False))
            finally:
                try:
                    server.close()
                except OSError:
                    pass

        self.control_thread = threading.Thread(target=worker, daemon=True)
        self.control_thread.start()

    def _load_icon(self):
        try:
            if ICON_PATH.exists():
                return Image.open(ICON_PATH)
        except Exception:
            pass
        image = Image.new("RGBA", (64, 64), Theme.BG)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((8, 8, 56, 56), radius=12, fill=Theme.PRIMARY)
        draw.text((18, 18), "CW", fill="white")
        return image

    def exit_gui(self, stop_backend: bool) -> None:
        def finish() -> None:
            self.control_stop.set()
            if self.refresh_job is not None:
                try:
                    self.root.after_cancel(self.refresh_job)
                except Exception:
                    pass
                self.refresh_job = None
            if self.tray_icon is not None:
                try:
                    self.tray_icon.stop()
                except Exception:
                    pass
            self.root.destroy()

        if stop_backend:
            self._run_async("正在退出全部…", self.manager.stop_all)
            self.root.after(1200, finish)
        else:
            finish()

    def _finish_smoke_test(self) -> None:
        print("GUI_SMOKE_OK")
        print(f"SERVER_PORT_OPEN={self.manager.is_port_open()}")
        print(f"SERVER_PROC={len(self.manager.server_processes())}")
        print(f"CLIENT_PROC={len(self.manager.client_processes())}")
        self.exit_gui(False)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    args = set(sys.argv[1:])

    control_command = None
    backend_action = "start"
    if "--start-backend" in args:
        control_command = "START"
        backend_action = "start"
    elif "--restart-backend" in args:
        control_command = "RESTART"
        backend_action = "restart"
    elif "--stop-backend" in args:
        control_command = "STOP"
        backend_action = "stop"
    elif "--show" in args:
        control_command = "SHOW"

    if "--smoke-test" not in args:
        command = control_command or "SHOW"
        try:
            with socket.create_connection(("127.0.0.1", CONTROL_PORT), timeout=0.35) as sock:
                sock.sendall(command.encode("utf-8"))
            return
        except OSError:
            pass
    gui = CapsWriterGUI(
        start_minimized="--minimized" in args,
        smoke_test="--smoke-test" in args,
        backend_action=backend_action,
    )
    gui.run()


if __name__ == "__main__":
    main()
