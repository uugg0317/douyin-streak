"""单账号发送执行器（M4 多账号改造）。

每个账号在**独立子进程**里运行本模块，由编排器（core.orchestrator）串行拉起：

    python -m core.worker --account acc2 [--dry-run] [--only 张三,李四]
    python -m core.worker --data-dir /opt/douyin-streak/data/accounts/acc2

隔离原理：进程启动后、导入任何 core 业务模块**之前**，先把环境变量
``DATA_DIR`` 指向该账号的数据目录，并置 ``SPARKKEEPER_NO_ROOT_STATE_FALLBACK=1``
防止跨账号兜底拷贝登录态。这样 core.config / runtime / ledger / automation
在导入时固化的模块级路径全部指向当前账号，automation 的发送逻辑无需改动。

设计约束：
- 不启动 FastAPI、不注册 APScheduler、不抢单实例 PID 锁（那些属于 Web 主进程）；
- 不做 45 分钟自动补发（子进程跑完即退，补发无法跨进程存活）；好友级失败次日
  全量重跑覆盖，漏发由编排器汇总后邮件告警；
- 默认关闭 60 秒发送预算（SEND_BUDGET_SECONDS=0），否则小号 70 人会在 60 秒处
  被截断成 skipped 且无人补发；按各号 config 的 gap 完整发送；
- 浏览器冷启动、跑完即关（禁用常驻），退出前关闭 Playwright 工作线程，保证子进程不挂住。

结果协议：stdout 最后一行输出 ``WORKER_RESULT <json>``（其余为日志），编排器据此解析；
完整结果同时由 runtime.record_run 写入该账号 runtime.json。

退出码：0=正常跑完（含好友级失败/掉线/限流，见 JSON 标志）；2=参数错误；
3=账号目录或登录态缺失；1=执行器未捕获异常。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
from contextlib import ExitStack
from pathlib import Path

# 这一行之前禁止导入任何 core.* 业务模块（DATA_DIR 必须先就位）。
_BASE_DIR = Path(__file__).resolve().parent.parent
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9_-]{3,20}$")
RESULT_PREFIX = "WORKER_RESULT "

logger = logging.getLogger("douyin-cloud-streak")


def _emit(payload: dict) -> None:
    """结果行打印到 stdout，flush 确保父进程能读到。"""
    sys.stdout.write(RESULT_PREFIX + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _resolve_data_dir(args) -> Path:
    if args.data_dir:
        d = Path(args.data_dir).expanduser().resolve()
    else:
        if not args.account or not _ACCOUNT_ID_RE.match(args.account):
            print(f"[worker] 账号 id 非法或未提供：{args.account!r}", file=sys.stderr)
            sys.exit(2)
        env_root = os.environ.get("SPARKKEEPER_ACCOUNTS_ROOT", "").strip()
        root = Path(env_root).resolve() if env_root else (_BASE_DIR / "data" / "accounts")
        d = (root / args.account).resolve()
        # 防穿越：解析后必须仍在账号根之下。
        if root not in d.parents:
            print(f"[worker] 账号目录越界：{d}", file=sys.stderr)
            sys.exit(2)
    if not d.is_dir():
        print(f"[worker] 账号数据目录不存在：{d}", file=sys.stderr)
        sys.exit(3)
    return d


def _run_fetch_contacts(account_id, automation, runtime, envelope, started) -> int:
    """采集私信好友名单并合并进该号台账（不发送任何消息）。"""
    from core import ledger
    runtime.set_running(True)
    exit_code = 0
    try:
        data = automation.fetch_chat_contacts() or {}
        runtime.record_contacts(data)
        names = data.get("names") or []
        total = len(names)
        if names:
            st = ledger.merge_consumer_contacts(names)
            total = st.get("total", len(names))
            logger.info("[%s] 好友名单采集合并：本次 %s，台账共 %s", account_id, len(names), total)
        if data.get("error"):
            exit_code = 1
            envelope.update(status="error", error=str(data.get("error")))
        else:
            envelope.update(status="ok", total=total,
                            result={"mode": "fetch-contacts", "names_count": len(names),
                                    "ledger_total": total, "error": data.get("error")})
        envelope.update(finished_at=automation._now(),
                        duration_sec=round(time.time() - started, 1))
    except Exception as e:
        exit_code = 1
        envelope.update(status="error", finished_at=automation._now(),
                        duration_sec=round(time.time() - started, 1),
                        error=f"{type(e).__name__}: {e}")
        logger.error("[%s] 好友采集异常：%s\n%s", account_id, e, traceback.format_exc())
    finally:
        try:
            runtime.set_running(False)
        except Exception:
            pass
        try:
            automation._pw_executor.shutdown(wait=True)
        except Exception:
            pass
        _emit(envelope)
    return exit_code


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="单账号发送执行器")
    parser.add_argument("--account", help="账号 id（与 --data-dir 二选一）")
    parser.add_argument("--data-dir", help="账号数据目录绝对路径（优先于 --account）")
    parser.add_argument("--mode", default="send", choices=["send", "fetch-contacts"],
                        help="send=发送续火花；fetch-contacts=采集私信好友名单（不发送）")
    parser.add_argument("--dry-run", action="store_true", help="只演练不真实发送")
    parser.add_argument("--only", default="", help="只发给指定好友，逗号分隔显示名")
    parser.add_argument("--budget", default="0",
                        help="单轮发送预算秒数；默认 0=不限（按配置间隔完整发完，勿截断小号）")
    return parser.parse_args(argv)


def _execute(args, data_dir: Path) -> int:
    account_id = args.account or data_dir.name

    # ★ 关键：在导入 core 业务模块前固化进程级环境。
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ.pop("STATE_FILE_PATH", None)
    os.environ["SPARKKEEPER_NO_ROOT_STATE_FALLBACK"] = "1"
    os.environ["KEEP_BROWSER_ALWAYS"] = "false"
    os.environ["SEND_BUDGET_SECONDS"] = str(args.budget)

    # 延迟导入：以下模块在 import 时即按 DATA_DIR 固化路径。
    from core import automation, config, runtime

    runtime.setup_logging()
    logger.info("worker 启动：account=%s data_dir=%s dry_run=%s",
                account_id, data_dir, args.dry_run)

    only_names = [s.strip() for s in args.only.split(",") if s.strip()] or None

    started = time.time()
    envelope = {
        "account_id": account_id,
        "data_dir": str(data_dir),
        "status": "error",
        "dry_run": bool(args.dry_run),
        "at": None, "finished_at": None, "duration_sec": 0,
        "total": 0, "ok_count": 0, "failed_count": 0, "skipped_count": 0, "unknown_count": 0,
        "logged_out": False, "rate_limited": False,
        "error": None, "result": None,
    }

    # 凭证预检：多账号隔离模式下只认本账号目录的 state.json。
    if not config.get_valid_state_path():
        envelope.update(status="no_state", finished_at=automation._now())
        envelope["error"] = "账号目录缺少有效登录态 state.json"
        logger.error("[%s] %s", account_id, envelope["error"])
        _emit(envelope)
        return 3

    if args.mode == "fetch-contacts":
        return _run_fetch_contacts(account_id, automation, runtime, envelope, started)

    runtime.set_running(True)
    exit_code = 0
    try:
        result = automation.run_send(dry_run=args.dry_run, only_names=only_names)
        # 写入该账号 runtime.json（history / session_status / 连续失败熔断计数）。
        runtime.record_run(result)
        ok = result.get("ok") or []
        failed = result.get("failed") or []
        skipped = result.get("skipped") or []
        unknown = result.get("unknown") or []
        envelope.update(
            status="ok",
            at=result.get("at"),
            finished_at=automation._now(),
            duration_sec=round(time.time() - started, 1),
            total=int(result.get("total", len(ok) + len(failed)) or 0),
            ok_count=len(ok),
            failed_count=len(failed),
            skipped_count=len(skipped),
            unknown_count=len(unknown),
            manual_required=bool(unknown or result.get("security_verification") or result.get("safety_verification")),
            security_verification=bool(result.get("security_verification") or result.get("safety_verification")),
            logged_out=bool(result.get("logged_out")),
            rate_limited=bool(result.get("rate_limited")),
            result=result,
        )
        logger.info(
            "[%s] worker 完成：成功 %s 失败 %s 跳过 %s 掉线=%s 限流=%s 用时 %.1fs",
            account_id, len(ok), len(failed), len(skipped),
            envelope["logged_out"], envelope["rate_limited"], envelope["duration_sec"],
        )
    except Exception as e:  # 执行器级异常：捕获后正常收尾，交编排器记为该号失败
        exit_code = 1
        envelope.update(
            status="error",
            finished_at=automation._now(),
            duration_sec=round(time.time() - started, 1),
            error=f"{type(e).__name__}: {e}",
        )
        logger.error("[%s] worker 执行异常：%s\n%s", account_id, e, traceback.format_exc())
    finally:
        try:
            runtime.set_running(False)
        except Exception:
            pass
        # 显式关闭专用 Playwright 工作线程，避免子进程在 atexit join 时挂住。
        try:
            automation._pw_executor.shutdown(wait=True)
        except Exception:
            pass
        _emit(envelope)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_dir = _resolve_data_dir(args)
    # This primitive has no config or browser imports. Hold the OS lock until the
    # worker closes its browser, so a surviving worker stays visible after a
    # controller crash or restart.
    from core.storage import file_lock
    with ExitStack() as locks:
        try:
            locks.enter_context(file_lock(data_dir / ".worker.guard", timeout=0.1))
        except TimeoutError:
            _emit({"account_id": args.account or data_dir.name, "status": "busy",
                   "error": "账号执行器仍在运行，本次未启动", "result": None})
            return 4
        return _execute(args, data_dir)


if __name__ == "__main__":
    sys.exit(main())
