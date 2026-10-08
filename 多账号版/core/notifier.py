"""漏发邮件通知（M4 多账号改造）。

多账号串行发送结束后，由调用方把 orchestrator.run_all 的汇总交给 :func:`notify`：
仅在出现漏发 / 异常时发一封信汇总全部账号（全账号成功默认不打扰，可由
NOTIFY_ON_SUCCESS 开启简报）。发送失败只记日志、绝不影响发送主流程。

只用标准库 smtplib + email（云端 Python 3.9，不增加依赖）。SMTP 参数全部走环境
变量（部署时写入服务器 .env，不入库）：

    SMTP_HOST        SMTP 服务器，如 smtp.qq.com / smtp.163.com
    SMTP_PORT        端口，SSL 默认 465，STARTTLS 常用 587
    SMTP_USER        发件邮箱
    SMTP_PASS        发件邮箱「授权码」（不是登录密码）
    SMTP_FROM        发件人，缺省取 SMTP_USER
    SMTP_TO          收件人，多个用英文逗号分隔
    SMTP_SECURITY    ssl（默认）/ starttls / none
    NOTIFY_ON_SUCCESS  1/true 时全成功也发简报，默认关闭
"""

from __future__ import annotations

import html as html_lib
import logging
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, formatdate

logger = logging.getLogger("douyin-cloud-streak")

# 失败六分类（顺序即邮件中的展示顺序）。
CATEGORIES = [
    ("unknown", "发送结果待人工确认"),
    ("login_expired", "登录态失效"),
    ("rate_limited", "限流 / 安全验证"),
    ("conversation_not_found", "找不到会话"),
    ("send_failed", "发送失败"),
    ("skipped", "跳过 / 未执行"),
    ("executor_error", "执行器异常 / 超时"),
]
CAT_LABEL = dict(CATEGORIES)

ACCOUNT_STATUS_LABEL = {
    "unknown": "发送结果待人工确认",
    "manual_required": "等待人工处理",
    "ok": "全部成功",
    "partial": "部分成功",
    "failed": "全部失败",
    "logged_out": "登录态失效",
    "rate_limited": "触发限流",
    "timeout": "执行超时",
    "executor_error": "执行器异常",
    "no_state": "无登录态",
    "breaker_skipped": "熔断跳过",
    "empty": "未勾选好友",
    "pending": "未执行",
}

_TRUE = {"1", "true", "yes", "on"}


def load_config(env: dict | None = None) -> dict | None:
    """从环境读取 SMTP 配置；缺关键字段时返回 None（表示不发信）。"""
    e = env if env is not None else os.environ
    host = (e.get("SMTP_HOST") or "").strip()
    user = (e.get("SMTP_USER") or "").strip()
    password = e.get("SMTP_PASS") or ""
    to_raw = (e.get("SMTP_TO") or "").strip()
    if not (host and user and password and to_raw):
        return None
    security = (e.get("SMTP_SECURITY") or "ssl").strip().lower()
    if security not in {"ssl", "starttls", "none"}:
        security = "ssl"
    default_port = 465 if security == "ssl" else 587
    try:
        port = int((e.get("SMTP_PORT") or default_port))
    except (TypeError, ValueError):
        port = default_port
    recipients = [x.strip() for x in to_raw.replace(";", ",").split(",") if x.strip()]
    return {
        "host": host, "port": port, "user": user, "password": password,
        "from": (e.get("SMTP_FROM") or user).strip() or user,
        "to": recipients, "security": security,
        "notify_on_success": (e.get("NOTIFY_ON_SUCCESS") or "").strip().lower() in _TRUE,
    }


def _classify_detail(status: str, reason: str | None, from_skipped: bool = False) -> str:
    """把账号状态 + 单条原因归入六分类之一。"""
    if status in {"logged_out", "no_state"}:
        return "login_expired"
    if status == "rate_limited":
        return "rate_limited"
    if status in {"executor_error", "timeout"}:
        return "executor_error"
    if status in {"empty", "breaker_skipped"} or from_skipped:
        return "skipped"
    r = reason or ""
    if any(k in r for k in ("登录", "掉线", "扫码", "登录态", "未登录")):
        return "login_expired"
    if any(k in r for k in ("频繁", "验证", "限流", "人机", "风控", "安全")):
        return "rate_limited"
    if any(k in r for k in ("找不到", "会话", "搜索", "联系人", "定位")):
        return "conversation_not_found"
    if any(k in r for k in ("跳过", "预算", "补发", "首条")):
        return "skipped"
    return "send_failed"


def _account_lines(item: dict) -> list[tuple[str, str, str]]:
    """返回某账号的 (分类key, 对象名, 原因) 明细；系统级问题对象名用 _system。"""
    lines: list[tuple[str, str, str]] = []
    status = item.get("status", "pending")

    # 账号级、没有好友明细的状态，补一条系统级说明。
    system_only = status in {"logged_out", "no_state", "rate_limited", "timeout",
                             "executor_error", "empty", "breaker_skipped", "failed", "manual_required", "unknown"}
    for d in item.get("unknown_detail") or []:
        lines.append(("unknown", d.get("name") or "_system", d.get("reason") or "结果未知，请人工核对；不要直接补发"))
    for d in item.get("failed_detail") or []:
        name = d.get("name") or "_system"
        reason = d.get("reason") or ""
        lines.append((_classify_detail(status, reason), name, reason))
    for d in item.get("skipped_detail") or []:
        name = d.get("name") or "_system"
        reason = d.get("reason") or ""
        lines.append((_classify_detail(status, reason, from_skipped=True), name, reason))

    if not lines and system_only:
        reason = item.get("error") or ACCOUNT_STATUS_LABEL.get(status, status)
        lines.append((_classify_detail(status, reason), "_system", reason))
    return lines


def should_notify(summary: dict, cfg: dict | None) -> bool:
    if not cfg:
        return False
    if summary.get("has_miss"):
        return True
    return bool(cfg.get("notify_on_success"))


def render(summary: dict) -> tuple[str, str, str]:
    """返回 (subject, text_body, html_body)。"""
    accounts = summary.get("accounts") or []
    totals = summary.get("totals") or {}
    miss_n = totals.get("miss_accounts", 0)
    dry_run = summary.get("dry_run")
    mode_word = "演练" if dry_run else "真实发送"
    when = summary.get("at") or ""

    # 分类计数（跨账号）。
    cat_count = {k: 0 for k, _ in CATEGORIES}
    for item in accounts:
        for cat, _name, _reason in _account_lines(item):
            cat_count[cat] += 1

    if summary.get("has_miss"):
        subject = f"【抖音续火花】漏发报告 {when[:10]}（{miss_n} 个账号异常）"
    else:
        subject = f"【抖音续火花】今日全部发送成功 {when[:10]}"
    if dry_run:
        subject = "[演练] " + subject

    # ── 纯文本 ──
    L = []
    L.append("抖音自动续火花 · " + ("漏发报告" if summary.get("has_miss") else "发送简报"))
    L.append(f"时间：{when}    模式：{mode_word}")
    L.append(
        f"账号总数：{len(accounts)}，异常账号：{miss_n}，"
        f"成功 {totals.get('ok', 0)} 人，失败 {totals.get('failed', 0)} 人，"
        f"跳过 {totals.get('skipped', 0)} 人，待确认 {totals.get('unknown', 0)} 人"
    )
    if cat_count and summary.get("has_miss"):
        cats = "，".join(f"{label} {cat_count[k]} 项" for k, label in CATEGORIES if cat_count[k])
        if cats:
            L.append("问题分类：" + cats)
    L.append("")
    for item in accounts:
        status = item.get("status", "pending")
        head = (
            f"【{item.get('name') or item.get('id')}】"
            f"{ACCOUNT_STATUS_LABEL.get(status, status)}"
            f"（成功 {item.get('ok_count', 0)} / 失败 {item.get('failed_count', 0)}"
            f" / 跳过 {item.get('skipped_count', 0)}，用时 {item.get('duration_sec', 0)}s）"
        )
        L.append(head)
        lines = _account_lines(item)
        if lines:
            for cat, name, reason in lines:
                who = "系统" if name == "_system" else name
                L.append(f"  - [{CAT_LABEL[cat]}] {who}：{reason or '无说明'}")
        elif status == "ok":
            L.append("  - 无异常")
        if item.get("error") and status not in {"logged_out", "no_state", "rate_limited",
                                                "timeout", "executor_error", "empty",
                                                "breaker_skipped", "failed"}:
            L.append(f"  - 备注：{item['error']}")
        L.append("")
    L.append("本邮件在出现漏发 / 异常时自动发送；全账号成功默认不打扰。")
    text_body = "\n".join(L)

    # ── HTML（简洁白底，兼容邮件客户端）──
    def esc(x) -> str:
        return html_lib.escape(str(x if x is not None else ""))
    H = ["<html><body style='font-family:Arial,\"Microsoft YaHei\",sans-serif;"
         "font-size:14px;color:#222;line-height:1.7;'>"]
    H.append(f"<h2 style='margin-bottom:4px;'>抖音自动续火花 · "
             f"{'漏发报告' if summary.get('has_miss') else '发送简报'}</h2>")
    H.append(f"<p style='color:#666;margin:4px 0;'>{esc(when)} ｜ 模式：{mode_word}</p>")
    H.append(f"<p>账号总数 <b>{len(accounts)}</b>，异常账号 <b style='color:#c0392b;'>{miss_n}</b>，"
             f"成功 <b>{totals.get('ok', 0)}</b>，失败 <b>{totals.get('failed', 0)}</b>，"
             f"跳过 <b>{totals.get('skipped', 0)}</b>，待确认 <b>{totals.get('unknown', 0)}</b></p>")
    H.append("<table style='border-collapse:collapse;width:100%;margin-top:8px;'>")
    for item in accounts:
        status = item.get("status", "pending")
        color = "#c0392b" if status != "ok" else "#27ae60"
        H.append("<tr><td style='border:1px solid #ddd;padding:8px;vertical-align:top;'>")
        H.append(f"<b>{esc(item.get('name') or item.get('id'))}</b>"
                 f" <span style='color:{color};'>{esc(ACCOUNT_STATUS_LABEL.get(status, status))}</span>"
                 f"<br><span style='color:#666;font-size:12px;'>"
                 f"成功 {item.get('ok_count', 0)} / 失败 {item.get('failed_count', 0)}"
                 f" / 跳过 {item.get('skipped_count', 0)} · {item.get('duration_sec', 0)}s</span>")
        lines = _account_lines(item)
        if lines:
            H.append("<ul style='margin:6px 0;padding-left:18px;'>")
            for cat, name, reason in lines:
                who = "系统" if name == "_system" else name
                H.append(f"<li>[{esc(CAT_LABEL[cat])}] <b>{esc(who)}</b>：{esc(reason or '无说明')}</li>")
            H.append("</ul>")
        elif status == "ok":
            H.append("<div style='color:#27ae60;'>无异常</div>")
        H.append("</td></tr>")
    H.append("</table>")
    H.append("<p style='color:#999;font-size:12px;margin-top:12px;'>"
             "本邮件在出现漏发 / 异常时自动发送；全账号成功默认不打扰。</p>")
    H.append("</body></html>")
    html_body = "".join(H)
    return subject, text_body, html_body


def send_email(cfg: dict, subject: str, text: str, html: str) -> tuple[bool, str | None]:
    """实际发送；返回 (是否成功, 错误信息)。不抛异常。"""
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr(("抖音续火花", cfg["from"]))
        msg["To"] = ", ".join(cfg["to"])
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")

        if cfg["security"] == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=30) as s:
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
                if cfg["security"] == "starttls":
                    ctx = ssl.create_default_context()
                    s.starttls(context=ctx)
                s.login(cfg["user"], cfg["password"])
                s.send_message(msg)
        logger.info("漏发邮件已发送至 %s", ",".join(cfg["to"]))
        return True, None
    except Exception as e:  # best-effort：通知失败不影响主流程
        err = f"{type(e).__name__}: {e}"
        logger.error("漏发邮件发送失败：%s", err)
        return False, err


def _merge_recipients(cfg: dict, extra) -> dict:
    """把账号级 notify_emails 并入全局 SMTP_TO，去重、保序、忽略空值。"""
    extras = extra or []
    if not extras:
        return cfg
    merged = list(cfg.get("to") or [])
    seen = {x.strip().lower() for x in merged if x.strip()}
    for raw in extras:
        addr = (raw or "").strip()
        key = addr.lower()
        if addr and key not in seen:
            seen.add(key)
            merged.append(addr)
    new_cfg = dict(cfg)
    new_cfg["to"] = merged
    return new_cfg


def notify(summary: dict, *, cfg: dict | None = None, extra_recipients=None,
           send_func=None) -> dict:
    """高层入口。返回 {action: sent|skipped|failed|disabled, reason, error}。

    extra_recipients：账号级收件人（各号 notify_emails 的并集），与全局 SMTP_TO
    合并去重后投递；仅当确实需要发信（有漏发或开启成功简报）时才合并。
    send_func 可注入用于测试，签名 (cfg, subject, text, html) -> (ok, err)。
    """
    cfg = cfg if cfg is not None else load_config()
    if not cfg:
        logger.info("未配置 SMTP（SMTP_HOST/USER/PASS/TO），跳过漏发邮件")
        return {"action": "disabled", "reason": "smtp_not_configured", "error": None}
    if not should_notify(summary, cfg):
        return {"action": "skipped", "reason": "all_success", "error": None}

    cfg = _merge_recipients(cfg, extra_recipients)

    subject, text, html = render(summary)
    send_func = send_func or send_email
    try:
        ok, err = send_func(cfg, subject, text, html)
    except Exception as e:  # 兜底：注入/实现异常也不抛出
        ok, err = False, f"{type(e).__name__}: {e}"
    return {"action": "sent" if ok else "failed", "reason": None, "error": err,
            "subject": subject}


def _sample_summary() -> dict:
    return {
        "at": "2026-09-21T00:05:00+0800", "dry_run": False, "forced": False,
        "accounts": [
            {"id": "main", "name": "主号", "status": "ok", "has_state": True,
             "ok_count": 10, "failed_count": 0, "skipped_count": 0,
             "logged_out": False, "rate_limited": False, "duration_sec": 32.5,
             "error": None, "failed_detail": [], "skipped_detail": []},
            {"id": "acc2", "name": "小号", "status": "partial", "has_state": True,
             "ok_count": 66, "failed_count": 3, "skipped_count": 1,
             "logged_out": False, "rate_limited": False, "duration_sec": 245.0,
             "error": None,
             "failed_detail": [
                 {"name": "张三", "reason": "找不到该好友会话，搜索也无结果"},
                 {"name": "李四", "reason": "发送后输入框未清空，判定未发出"},
                 {"name": "_system", "reason": "登录态失效（掉线）"}],
             "skipped_detail": [
                 {"name": "_system", "reason": "本轮预算已用尽，剩余 1 人留给补发"}]},
        ],
        "totals": {"ok": 76, "failed": 3, "skipped": 1,
                   "logged_out_accounts": 1, "rate_limited_accounts": 0,
                   "error_accounts": 0, "miss_accounts": 1},
        "duration_sec": 280.0, "has_miss": True,
    }


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--sample" in argv:
        subject, text, html = render(_sample_summary())
        print("Subject:", subject)
        print("-" * 60)
        print(text)
        return 0
    if "--config" in argv:
        cfg = load_config()
        if not cfg:
            print("SMTP 未配置（需要 SMTP_HOST/SMTP_USER/SMTP_PASS/SMTP_TO）")
            return 1
        shown = {k: ("***" if k == "password" and v else v) for k, v in cfg.items()}
        print(shown)
        return 0
    print("用法：python -m core.notifier --sample | --config")
    return 2


if __name__ == "__main__":
    sys.exit(main())
