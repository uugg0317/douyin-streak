"""单账号 data/ → 多账号 data/accounts/<id>/ 迁移脚本（M4）。

用法：
    python migrate_to_accounts.py --dry-run            # 只打印计划，不写任何文件
    python migrate_to_accounts.py --apply              # 先整目录备份，再迁移，可回滚
    python migrate_to_accounts.py --apply --main-id main --main-name 主号

迁移内容（账号私有数据）：config.json、state.json、ledger.json、ledger.json.bak、
runtime.json、logs/ 目录。
**不迁移**：accounts/、accounts.json、server.pid、.dsh_write_test、.gitkeep、
backups/、*.tmp.*，以及其它未识别文件（会在报告中列出，默认留在原地，多为全局文件，
如 IP 白名单）。

安全保证：
- 默认 dry-run；apply 前会把整个 data/ 复制到同级 data.bak-<时间戳>，回滚即还原；
- 目标账号目录非空或注册表已存在该账号时中止，不覆盖任何凭证（除非 --force）；
- 仅迁移，不删除源目录之外的东西，也不触碰线上服务。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import datetime
from pathlib import Path

from core import accounts as A

# 账号私有、随账号迁移的文件 / 目录（存在才迁）。
MIGRATE_ITEMS = [
    A.CONFIG_NAME,
    A.STATE_NAME,
    A.LEDGER_NAME,
    "ledger.json.bak",
    A.RUNTIME_NAME,
    A.LOG_DIR_NAME,
]
# 明确保留在 data/ 根、不随账号迁移的运行期 / 全局项。
KEEP_AT_ROOT = {
    "accounts", "accounts.json", "server.pid", ".dsh_write_test",
    ".gitkeep", "backups",
}


def _data_root() -> Path:
    # 账号根是 <data>/accounts，其父目录即单账号时代的数据根。
    return A.ACCOUNTS_ROOT.parent


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _state_cookie_count(path: Path) -> int:
    data = _read_json(path)
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        return len(data["cookies"])
    return 0


def _ledger_count(path: Path) -> int:
    data = _read_json(path)
    return len(data) if isinstance(data, list) else 0


def _plan(data_root: Path, main_id: str) -> tuple[list[Path], list[Path]]:
    migrate, unknown = [], []
    for item in sorted(data_root.iterdir()):
        if item.name in KEEP_AT_ROOT or item.name.startswith("accounts.json.tmp."):
            continue
        if item.name in MIGRATE_ITEMS:
            migrate.append(item)
        else:
            unknown.append(item)
    return migrate, unknown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单账号数据迁移到多账号目录")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="真正执行")
    mode.add_argument("--dry-run", action="store_true", help="只打印计划（默认）")
    parser.add_argument("--main-id", default="main", help="主账号 id（默认 main）")
    parser.add_argument("--main-name", default="主号", help="主账号显示名")
    parser.add_argument("--force", action="store_true", help="目标已存在时强制（慎用）")
    args = parser.parse_args(argv)

    if not A.valid_account_id(args.main_id):
        print(f"[中止] --main-id 非法：{args.main_id!r}")
        return 2

    data_root = _data_root()
    if not data_root.exists():
        print(f"[中止] 数据目录不存在：{data_root}")
        return 2

    migrate, unknown = _plan(data_root, args.main_id)
    target_dir = A.account_dir(args.main_id)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = data_root.parent / f"{data_root.name}.bak-{ts}"

    existing = A.get_account(args.main_id)
    target_nonempty = target_dir.exists() and any(target_dir.iterdir())

    print("=" * 64)
    print(f"数据根目录 : {data_root}")
    print(f"目标账号   : {args.main_id}（{args.main_name}）→ {target_dir}")
    print(f"整目录备份 : {backup_dir}")
    print(f"模式       : {'APPLY（真实执行）' if args.apply else 'DRY-RUN（不写文件）'}")
    print("-" * 64)
    print("将迁移的账号私有数据：")
    for item in migrate:
        kind = "目录" if item.is_dir() else "文件"
        extra = ""
        if item.name == A.STATE_NAME:
            extra = f"（cookies={_state_cookie_count(item)}）"
        elif item.name == A.LEDGER_NAME:
            extra = f"（台账 {_ledger_count(item)} 条）"
        print(f"  - [{kind}] {item.name}{extra}")
    if unknown:
        print("保留在原地、不迁移（未识别 / 全局项）：")
        for item in unknown:
            print(f"  ~ {item.name}")
    print("-" * 64)

    # 幂等 / 覆盖保护。
    if existing or target_nonempty:
        msg = f"目标账号 {args.main_id} 已存在或目录非空"
        if not args.force:
            print(f"[中止] {msg}，为避免覆盖凭证请先确认；如确需重做请加 --force。")
            return 2
        print(f"[警告] {msg}，已指定 --force，将继续。")

    if not args.apply:
        print("DRY-RUN 完成，未做任何改动。确认无误后加 --apply 执行。")
        return 0

    # 1) 整目录备份（在移动之前，保证可完整回滚）。
    if backup_dir.exists():
        print(f"[中止] 备份目录已存在：{backup_dir}")
        return 2
    print(f"[1/4] 备份整个数据目录 → {backup_dir.name} ...")
    shutil.copytree(data_root, backup_dir)

    # 2) 注册账号并创建目录。
    print("[2/4] 写入注册表 accounts.json ...")
    A.add_account(args.main_id, args.main_name, enabled=True, order=0,
                  note="由单账号迁移生成", exist_ok=args.force)

    # 3) 逐项迁移。
    print("[3/4] 迁移账号私有数据 ...")
    for item in migrate:
        dst = target_dir / item.name
        if dst.exists():
            print(f"  跳过（目标已存在）：{item.name}")
            continue
        shutil.move(str(item), str(dst))
        print(f"  移动：{item.name}")

    # 4) 校验。
    print("[4/4] 校验 ...")
    cookies = _state_cookie_count(target_dir / A.STATE_NAME)
    ledger_n = _ledger_count(target_dir / A.LEDGER_NAME)
    print(f"  目标 state cookies = {cookies}；ledger 条数 = {ledger_n}")
    if cookies == 0:
        print("[警告] 目标未检测到有效登录态，请检查备份与迁移结果。")
    print("迁移完成。回滚方式：停服后用备份目录覆盖 data/，并删除 data/accounts.json。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
