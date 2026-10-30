import hashlib
import hmac
import ipaddress

from .config import get_settings


def hmac_hex(value: str, salt: str | None = None) -> str:
    salt = salt or get_settings().signal_hash_salt or "dev-salt"
    return hmac.new(salt.encode(), value.encode(), hashlib.sha256).hexdigest()


def ip_bucket(ip: str) -> str:
    """Hash of the /24 (IPv4) or /48 (IPv6) network; raw IPs are never stored."""
    addr = ipaddress.ip_address(ip)
    net = ipaddress.ip_network(f"{ip}/{24 if addr.version == 4 else 48}", strict=False)
    return hmac_hex(str(net))


def ua_family(ua: str) -> str:
    u = ua.lower()
    for key, name in (("edg", "edge"), ("firefox", "firefox"), ("chrome", "chrome"), ("safari", "safari")):
        if key in u:
            return name
    return "other"
