"""Network policy of the course browser.

- Private and local addresses (loopback, LAN, link-local, Docker, CGNAT, IPv6 ULA) are closed for
  everything: page navigation, subresources, fetch/XHR, WebSocket. The filtering proxy checks the IP
  a host resolves to (DNS rebinding); here — literal hosts and schemes, for early refusals and notes.
- Exceptions: the course origin itself (the owner gave that link, e.g. a LAN LMS) and COURSE_PRIVATE_ALLOW.
- Other public domains are allowed; the tab's moves to them are counted and limited by the runner.
"""

import ipaddress
import re
from urllib.parse import urlparse

BLOCKED_SCHEMES = ("file:", "chrome:", "chrome-extension:", "devtools:", "chrome-search:", "view-source:")
_EXTRA_PRIVATE = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("0.0.0.0/8")]
# вторые уровни, на которых сайт — три последние метки (astanait.edu.kz, example.co.uk)
_SECOND_LEVEL = {"edu", "ac", "co", "com", "org", "gov", "net", "gob", "mil"}
_DOMAINS_RE = re.compile(r"^\s*домены\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)


def is_private_ip(value: str) -> bool:
    """Loopback, private, link-local, unspecified, reserved, multicast, CGNAT; IPv4 inside IPv6 too."""
    try:
        ip = ipaddress.ip_address(value.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_reserved
        or ip.is_multicast or any(ip in net for net in _EXTRA_PRIVATE)
    )


def is_local_name(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host == "localhost" or host.endswith(".localhost")


def host_port(url: str) -> tuple[str, int]:
    p = urlparse(url)
    default = {"https": 443, "wss": 443}.get(p.scheme, 80)
    return (p.hostname or "").lower(), p.port or default


def site_of(host: str) -> str:
    """Registrable part of a host, approximately: www.coursera.org → coursera.org, lms.astanait.edu.kz → astanait.edu.kz."""
    host = host.lower().rstrip(".")
    if not host or is_private_ip(host) or ":" in host:
        return host
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def parse_domains(text: str) -> list[str]:
    """«a.com, b.org» → [a.com, b.org] (lowercase, without scheme and path)."""
    out = []
    for item in re.split(r"[,;\s]+", text):
        item = item.strip().lower()
        if not item:
            continue
        item = (urlparse(item).hostname or item) if "//" in item else item.split("/", 1)[0]
        out.append(item.strip("."))
    return list(dict.fromkeys(out))


def split_course_input(text: str) -> tuple[str, str, list[str]]:
    """«url | домены: a.com, b.org | указания» (parts in any order) → (url, указания, домены)."""
    url, *rest = [p.strip() for p in text.split("|")]
    instructions, domains = [], []
    for part in rest:
        m = _DOMAINS_RE.match(part)
        if m:
            domains += parse_domains(m.group(1))
        elif part:
            instructions.append(part)
    return url, " | ".join(instructions), domains


class NetPolicy:
    def __init__(self, course_url: str, private_allow: str = "", domains: list[str] | None = None) -> None:
        self.course_host, self.course_port = host_port(course_url)
        self.course_site = site_of(self.course_host)
        # исключения из запрета приватных адресов: хост:порт
        self.exempt = {f"{self.course_host}:{self.course_port}"}
        for item in re.split(r"[,;\s]+", private_allow):
            if item.strip():
                self.exempt.add(item.strip().lower())
        self.domains = [d.lower() for d in (domains or [])]

    def exempt_endpoint(self, host: str, port: int) -> bool:
        return f"{host.lower()}:{port}" in self.exempt

    def literal_block(self, url: str) -> str:
        """Why the URL is closed without resolving DNS, or ''."""
        low = url.lower()
        if low.startswith(BLOCKED_SCHEMES):
            return "схема " + low.split(":", 1)[0]
        if not low.startswith(("http://", "https://", "ws://", "wss://")):
            return ""
        host, port = host_port(url)
        if self.exempt_endpoint(host, port):
            return ""
        if is_local_name(host) or is_private_ip(host):
            return "локальный адрес"
        return ""

    def endpoint_block(self, host: str, port: int, ips: list[str]) -> str:
        """Proxy check after DNS: any private IP closes the host (unless it is an exception)."""
        if self.exempt_endpoint(host, port):
            return ""
        if is_local_name(host) or is_private_ip(host):
            return "локальный адрес"
        if not ips:
            return "имя не резолвится"
        if any(is_private_ip(ip) for ip in ips):
            return "имя указывает на локальный адрес"
        return ""

    def is_course(self, url: str) -> bool:
        host, _ = host_port(url)
        return bool(host) and (host == self.course_host or site_of(host) == self.course_site)

    def navigation_allowed(self, url: str) -> bool:
        """«домены:» limits where the tab may go (resources are not limited by it)."""
        if not self.domains or self.is_course(url):
            return True
        host, _ = host_port(url)
        return any(host == d or host.endswith("." + d) for d in self.domains)
