"""配置读写。配置保存在 data/config.json，由网页端编辑。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import os
import tempfile
import time

BASE_DIR = Path(__file__).resolve().parent.parent
# 支持多账号：通过环境变量 DATA_DIR 指定数据目录，默认 data/
# 例如 set DATA_DIR=accounts/账号1  即可隔离每个账号的数据
#
# 桌面版（PyInstaller onedir）也会用到这个变量：打包后 BASE_DIR 会落到
# _internal/，数据必须留在 exe 旁边而不是埋进 _internal，否则用户找不到。
# 见 desktop/runtime_hook_data_dir.py。
_data_dir_env = os.environ.get("DATA_DIR", "").strip()
DATA_DIR = Path(_data_dir_env).resolve() if _data_dir_env else (BASE_DIR / "data")
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
ROOT_STATE_PATH = BASE_DIR / "state.json"


def _atomic_replace(tmp: Path, dst: Path) -> None:
    """把已 fsync 的临时文件原子替换到 dst，并针对 Windows 做重试；失败时保留原文件。

    Windows 上 os.replace 偶发抛 [WinError 5] 拒绝访问：实时杀毒、搜索索引、同步盘
    或其它进程在新文件落盘瞬间短暂独占目标/临时文件，rename 即失败。这是瞬时锁，
    按指数退避重试通常即可成功。Linux/macOS 的 rename 即使目标被读打开也能成功，
    几乎不触发，重试同样无害。
    """
    last_error = None
    for attempt in range(6):
        try:
            os.replace(str(tmp), str(dst))
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.05 * (2 ** attempt))
    raise last_error


def _atomic_write(path: Path, data, binary: bool) -> None:
    """原子写通用实现：先建父目录，写唯一临时文件并 fsync，再重试式原子替换。

    临时文件名带 pid + 随机后缀（mkstemp），避免同进程残留 .tmp 被锁时互相冲突；
    mkstemp 建出的文件权限为 0600（仅所有者可读写），对含登录 Cookie 的 state.json
    反而更安全，服务以同一用户运行，读取不受影响。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp.", suffix="")
    tmp = Path(tmp_name)
    try:
        if binary:
            ctx = os.fdopen(fd, "wb")
        else:
            ctx = os.fdopen(fd, "w", encoding="utf-8")
        with ctx as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _atomic_replace(tmp, path)
    finally:
        for _ in range(3):  # 清理临时文件；被短暂占用时重试几次
            try:
                if tmp.exists():
                    tmp.unlink()
                break
            except OSError:
                time.sleep(0.05)


def atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：外部读者只会看到旧版或完整新版，不会读到半截 JSON。

    state.json 是完整登录 Cookie、ledger.json 是好友台账，直接 write_text 若中途
    被杀/断电会截断成半截，导致登录态失效（被迫重新扫码）或台账被读空（一整晚不发
    却显示正常完成）。Windows 替换阶段的 [WinError 5] 瞬时锁由 _atomic_replace
    重试处理；持续失败时抛错并保留原文件。
    """
    _atomic_write(path, text, binary=False)


def atomic_write_bytes(path: Path, raw: bytes) -> None:
    """原子写字节：见 atomic_write_text。"""
    _atomic_write(path, raw, binary=True)


def _state_looks_valid(p: Path) -> bool:
    """state.json 是否真能用：能解析为 dict 且 cookies 是非空数组。

    旧校验只看 `st_size > 30`，一个被截断成 10KB 的半截 JSON 也能通过，随后在
    Playwright storage_state 加载时抛异常，表现为发送持续失败却不知道原因。
    """
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and isinstance(data.get("cookies"), list) and bool(data["cookies"])


# 安全模式：通过环境变量 STATE_FILE_PATH 指定 state.json 路径（如 /dev/shm/state.json）
# 用于将解密后的登录态放在 tmpfs 内存中，磁盘上不留明文
_EXTERNAL_STATE = os.environ.get("STATE_FILE_PATH", "").strip()

# 多账号 worker/编排器置 1：禁止在账号目录缺 state 时从根目录兜底拷贝 state.json，
# 否则某个账号没接入凭证时，会把别的账号（或旧单账号）登录态拷进来造成「串号」。
_NO_ROOT_STATE_FALLBACK = os.environ.get("SPARKKEEPER_NO_ROOT_STATE_FALLBACK", "").strip() in {
    "1", "true", "TRUE", "yes", "on",
}


def get_valid_state_path() -> Path | None:
    """返回一个「真能用」的登录态文件路径；找不到则回退拷贝 ROOT_STATE_PATH。"""
    if _EXTERNAL_STATE:
        p = Path(_EXTERNAL_STATE)
        if p.exists() and _state_looks_valid(p):
            return p
    if STATE_PATH.exists() and _state_looks_valid(STATE_PATH):
        return STATE_PATH
    if _NO_ROOT_STATE_FALLBACK:
        # 多账号隔离模式：只认本账号 DATA_DIR 下的登录态，绝不跨账号/从根目录兜底拷贝。
        return None
    if ROOT_STATE_PATH.exists() and _state_looks_valid(ROOT_STATE_PATH):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copy2(ROOT_STATE_PATH, STATE_PATH)
        except Exception:
            pass
        return STATE_PATH
    return None

DEFAULT_CONFIG = {
    "schedule_time": "21:00",   # 每天发送时间 HH:MM（服务器时区 Asia/Shanghai）
    "jitter_minutes": 30,       # 时间抖动窗口：实际在 [schedule_time, schedule_time+30min] 内随机开始
    "send_gap_min": 6,          # 相邻两个好友之间的最小间隔（秒）
    "send_gap_max": 12,         # 相邻两个好友之间的最大间隔（秒）
    "max_friends_per_run": 20,  # 每次最多发送的好友数（0 表示不限制）
    "friends": [],              # 好友列表：聊天列表里显示的备注 / 昵称 / 抖音号
    "messages": ["🔥 续火花", "今天也要开心哦 🔥", "晚上好 🔥"],
    # creator 页抖音号采集（P1）：
    "creator_user_detail_path": "aweme/v1/creator/im/user_detail/",  # user_detail 接口路径前缀（接口变动只改这里）
    "creator_max_scrolls": 80,  # 单次采集最大滚动轮数
    # 通道 B / 调度（P2）：
    "auto_run_enabled": True,  # 自动运行总开关：关闭后定时任务不发送（手动「立即续火花」不受影响）
    "allow_first_message": False,  # 允许对无会话好友发送首条消息（通道 B，高风险，默认关闭）
    "first_message_daily_limit": 1,  # 通道 B 单日上限
    "schedule_harvest_day": "mon",  # 周级 creator 采集：mon/tue/.../sun 或空字符串关闭，默认周一 03:00
}

_lock = threading.Lock()


def load_config(path: Path | None = None) -> dict:
    p = path or CONFIG_PATH
    cfg = dict(DEFAULT_CONFIG)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def load_persisted_config(path: Path | None = None) -> dict:
    """只读取磁盘上真实持久化过的键值（不含 DEFAULT_CONFIG 兜底）。

    用途：判断某项「服务器上到底有没有存过」。load_config() 会把
    DEFAULT_CONFIG 合并进来，因此永远无法区分「存过」与「没存过」。
    """
    p = path or CONFIG_PATH
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict | None, path: Path | None = None, *, lock_gap: bool = True) -> dict:
    # Hold the lock across reading, merging and writing to prevent lost partial updates.
    with _lock:
        return _save_config_unlocked(cfg, path, lock_gap=lock_gap)


def _save_config_unlocked(cfg: dict | None, path: Path | None = None, *, lock_gap: bool = True) -> dict:
    p = path or CONFIG_PATH
    if p.exists():
        try:
            persisted = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("现有配置无法读取，已保留原文件；请先修复配置后重试") from exc
        if not isinstance(persisted, dict):
            raise ValueError("现有配置必须是 JSON 对象，已保留原文件")
    else:
        persisted = {}
    merged = dict(DEFAULT_CONFIG)
    merged.update(persisted)
    if cfg:
        merged.update(cfg)

    merged["friends"] = [str(x).strip() for x in merged.get("friends", []) if str(x).strip()]
    # 发送文案永久固定：始终以服务器磁盘上已保存的 messages 为准，
    # 忽略网页或直接调接口传入的任何 messages 修改，保证发送内容不被改动
    fixed_messages = persisted.get("messages") or ["[续火花吧]"]
    merged["messages"] = [str(x) for x in fixed_messages if str(x).strip()]

    # 定时与发送节奏永久锁定：这些项已在前端禁用，且直接决定「0 点准时、
    # 1 分钟内发完」。前端页面可能带着默认值（21:00 / 30 / 6 / 12）整体回存，
    # 这里强制以服务器磁盘上已保存的值为准，杜绝被默认值覆盖。
    # 仅当磁盘上确实没有配置（首次部署）时，才使用安全默认值 00:00 / 0 / 1 / 2。
    #
    # 注意：判断「磁盘上有没有」必须用 load_persisted_config()。
    # 旧实现用的是 load_config()，而它必然含有 DEFAULT_CONFIG 的全部键，
    # 于是 else 分支永远不可达 —— 首次部署会落到 DEFAULT_CONFIG 的
    # 21:00 / 30 / 6 / 12，与「0 点准时发」的设计意图正好相反。
    #
    # gap 取值说明（2026-09-18 调整 2/4 → 1/2）：
    # 单条发送动作实测约 1.2 秒，gap 均值 1.5 秒 → 单人约 2.7 秒，
    # 10 人端到端约 31 秒，1 分钟目标留有充足余量（即便个别好友走慢路径）。
    # 注意 gap 是「好友之间」的间隔，不是发送频率上限，1~2 秒仍在拟人范围。
    # 定时时间与抖动始终以磁盘为准（多账号由编排器统一 00:00，worker 不读调度）。
    for _key, _safe_default in {"schedule_time": "00:00", "jitter_minutes": 0}.items():
        merged[_key] = persisted.get(_key, _safe_default)
    # 发送间隔：单账号锁定（防前端默认值覆盖）；多账号按号保存传 lock_gap=False，
    # 允许各号设置自己的节奏（如小号 2~3 秒），随后仍统一走整数与大小关系校验。
    if lock_gap:
        for _key, _safe_default in {"send_gap_min": 1, "send_gap_max": 2}.items():
            merged[_key] = persisted.get(_key, _safe_default)

    schedule = str(merged.get("schedule_time", "00:00"))
    try:
        hh, mm = schedule.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
        merged["schedule_time"] = f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        raise ValueError("schedule_time 必须是 HH:MM 格式")

    for key in ("jitter_minutes", "send_gap_min", "send_gap_max", "max_friends_per_run", "creator_max_scrolls", "first_message_daily_limit"):
        try:
            merged[key] = max(0, int(merged.get(key, DEFAULT_CONFIG[key])))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]
    merged["auto_run_enabled"] = bool(merged.get("auto_run_enabled"))
    merged["allow_first_message"] = bool(merged.get("allow_first_message"))
    day = str(merged.get("schedule_harvest_day") or "").strip().lower()
    merged["schedule_harvest_day"] = day if day in {"mon", "tue", "wed", "thu", "fri", "sat", "sun", "off"} else "off"

    atomic_write_text(p, json.dumps(merged, ensure_ascii=False, indent=2))
    return merged
