
import asyncio
import sys 
import os 
from multiprocessing import Process, Manager
import queue
from util.server.server_cosmic import Cosmic, console
from util.server.server_init_recognizer import init_recognizer
from util.server.state import get_state
from util.server.server_check_model import check_model
from util.common.lifecycle import lifecycle
from util.tools.kill_on_close_job import assign_process_to_job
from . import logger


def start_recognizer_process():
    """启动识别子进程并等待模型加载完成"""
    
    check_model()

    state = get_state()
    # 持有 Manager 本身，而不只是它产出的 list：清理时要显式 shutdown()，
    # 否则那个 Manager 进程只能靠 atexit 回收（硬杀父进程时不会执行）。
    manager = Manager()
    state.sockets_id_manager = manager
    Cosmic.sockets_id = manager.list()

    # 取 stdin 的 fd 传给子进程（子进程用它重开 stdin 以响应 Ctrl+C）。
    # 用 pythonw.exe / 无控制台方式启动时 sys.stdin 是 None，
    # 重定向到管道时 fileno() 也可能失败 —— 这两种情况传 None，
    # 子进程会跳过重开 stdin。不能让它在这里抛异常：
    # 那会使整个 server 启动失败，而 stdin 只是为了键盘中断。
    stdin_fn = None
    try:
        if sys.stdin is not None:
            stdin_fn = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError) as e:
        logger.info(f"无法获取 stdin fd（无控制台环境），子进程将跳过 stdin: {e}")
    # daemon=True 处理正常退出路径（解释器退出时 multiprocessing 会回收它）。
    # 注意它**不**足以防止孤儿：那套回收跑在父进程的 atexit 里，而
    # TerminateProcess（taskkill /F、任务管理器结束任务、崩溃）会跳过 atexit。
    # 硬杀场景由下面的 Job Object 兜底。
    recognize_process = Process(target=init_recognizer,
                                args=(Cosmic.queue_in,
                                      Cosmic.queue_out,
                                      Cosmic.sockets_id,
                                      stdin_fn),
                                daemon=True)
    recognize_process.start()

    # 内核级兜底：把子进程加入 KILL_ON_JOB_CLOSE 的 job。
    # 父进程一死，内核关闭其最后一个 job 句柄，job 内进程立即被终止，
    # 不经过任何用户态代码 —— 这是硬杀场景下唯一可靠的机制。
    #
    # 识别子进程加载 sherpa-onnx 后提交约 4GB（OpenBLAS 按核心数预留线程栈，
    # 24 核机器上尤其明显），泄漏一次的代价很高，值得这层保护。
    if not assign_process_to_job(recognize_process.pid):
        logger.warning(
            "无法将识别子进程加入 Job Object；父进程被强制结束时可能残留子进程"
        )
    # Manager 进程同样纳入：它约占 900MB 提交，以前也会一起泄漏。
    manager_proc = getattr(manager, '_process', None)
    if manager_proc is not None and manager_proc.pid:
        if not assign_process_to_job(manager_proc.pid):
            logger.warning("无法将 Manager 进程加入 Job Object")
    state.recognize_process = recognize_process
    logger.info("识别子进程已启动")

    # 轮询等待模型加载，同时响应退出请求
    import errno
    while not lifecycle.is_shutting_down:
        try:
            Cosmic.queue_out.get(timeout=0.1)
            break
        except queue.Empty:
            if recognize_process.is_alive():
                continue
            else:
                break
        except (InterruptedError, OSError) as e:
            # 处理被信号中断的情况 (Errno 4 Interrupted function call)
            # 这通常发生在 Anti-Shake 触发时 (第一次 Ctrl+C)
            if isinstance(e, InterruptedError) or e.errno == errno.EINTR:
                continue
            raise

    # 检查子进程是否存活
    if not recognize_process.is_alive():
        logger.error("识别子进程意外退出（可能是因为模型文件缺失或加载失败）")
        # 退出码不为0，说明可能出错
        if recognize_process.exitcode != 0:
            logger.error(f"子进程退出码: {recognize_process.exitcode}")
        
        # 主动抛出异常或直接退出
        lifecycle.request_shutdown()

    if lifecycle.is_shutting_down:
        logger.warning("在加载模型时收到退出请求")
        recognize_process.terminate()
        # 不再抛出异常，而是优雅返回，由外层 lifecycle 状态决定流程
        return recognize_process

    logger.info("模型加载完成，开始服务")
    console.rule('[green3]开始服务')
    console.line()
    return recognize_process
