"""Public edition: the local management console opens without credentials."""
from __future__ import annotations
import ipaddress
from bootstrap import _env


def _is_loopback(host: str) -> bool:
    if host.lower() in {"localhost", "ip6-localhost"}:
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_loopback or bool(
            isinstance(address, ipaddress.IPv6Address)
            and address.ipv4_mapped and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        return False


def _check_local_bind() -> None:
    """This edition runs on the user's own machine, with no management password."""
    if not _is_loopback(_env("HOST") or "127.0.0.1"):
        raise SystemExit("本地免登录版仅支持 127.0.0.1 / localhost / ::1 监听地址")

