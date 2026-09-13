from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import requests

from ..config import Config
from ..model import ProviderResult, SourceAttempt, now_utc

log = logging.getLogger(__name__)

_SESSION: requests.Session | None = None


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.trust_env = True
        _SESSION = s
    return _SESSION


class Provider(ABC):
    id: str = "?"
    name: str = "?"

    def __init__(self, config: Config):
        self.config = config
        self.settings = config.provider(self.id)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled", True))

    @abstractmethod
    def detect(self) -> bool:
        """Is this product installed on the machine? Decides panel placement."""

    @abstractmethod
    def collect(self, result: ProviderResult) -> None:
        """Walk the fallback chain, filling in result.windows / result.attempts."""

    def fetch(self) -> ProviderResult:
        result = ProviderResult(provider_id=self.id, name=self.name, fetched_at=now_utc())
        try:
            result.installed = self.detect()
        except Exception as exc:                                # noqa: BLE001
            log.debug("%s detect raised", self.id, exc_info=True)
            result.installed = True
            result.attempts.append(SourceAttempt("detect install", False, str(exc)))
        try:
            self.collect(result)
        except Exception as exc:                                # noqa: BLE001
            log.exception("%s collection failed", self.id)
            result.attempts.append(SourceAttempt("collect", False, repr(exc)))
        if not result.ok and not result.status:
            if not result.installed:
                result.status = "not detected on this machine"
            else:
                failed = [a for a in result.attempts if not a.ok]
                result.status = failed[-1].detail if failed else "no usable quota source"
        return result


def build_providers(config: Config) -> list[Provider]:
    from .antigravity import AntigravityProvider
    from .claude import ClaudeProvider
    from .codex import CodexProvider

    providers = [ClaudeProvider(config), CodexProvider(config), AntigravityProvider(config)]
    return [p for p in providers if p.enabled]
