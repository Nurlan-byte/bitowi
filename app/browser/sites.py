"""Site profiles: how to get a logged-in browser state for a host."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from app.config import Settings


@dataclass(frozen=True)
class FormLogin:
    login_path: str
    user: str
    password: str
    user_selector: str
    password_selector: str
    submit_selector: str


@dataclass(frozen=True)
class SiteProfile:
    host: str
    kind: Literal["form", "state"]
    auth_file: Path
    form: FormLogin | None = None


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def auth_file(settings: Settings, host: str) -> Path:
    return settings.auth_dir / (host.replace(":", "_") + ".json")


def find_auth_file(settings: Settings, host: str) -> Path:
    """The owner's state.json for the host or a parent domain (www.coursera.org → coursera.org);
    none saved — the host's own path."""
    exact = auth_file(settings, host)
    labels = host.split(".")
    if ":" in host or labels[-1].isdigit():
        return exact
    for i in range(len(labels) - 1):
        path = auth_file(settings, ".".join(labels[i:]))
        if path.exists():
            return path
    return exact


def profile_for(url: str, settings: Settings) -> SiteProfile:
    host = host_of(url)
    path = auth_file(settings, host)
    if host == settings.demo_moodle_host.lower():
        # демо Moodle сбрасывается каждый час — входим по форме на каждую задачу
        return SiteProfile(host, "form", path, FormLogin(
            "/login/index.php",
            settings.demo_moodle_user,
            settings.demo_moodle_password,
            "#username",
            "#password",
            "#loginbtn",
        ))
    if host in (settings.fake_lms_host.lower(), settings.fake_lms_host.lower().replace("127.0.0.1", "localhost")):
        return SiteProfile(host, "form", path, FormLogin(
            "/login",
            settings.fake_lms_user,
            settings.fake_lms_password,
            "input[name=username]",
            "input[name=password]",
            "button[type=submit]",
        ))
    # остальные — state.json, который владелец делает сам (SSO, MFA)
    return SiteProfile(host, "state", find_auth_file(settings, host))
