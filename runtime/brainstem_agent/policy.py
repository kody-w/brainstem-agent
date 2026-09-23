"""Grant authority: synthetic fixtures (M0) and single-owner local grants (Cell v1).

``local`` mode is authority for the owner's own host, not multi-user
authentication. Local grant handles are stored only as SHA-256 digests and are
bound to one worker generation, explicit capabilities and an expiry.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re
import secrets
import threading
import time
from typing import Callable

from .state import Store

_LOCAL_TTL = 3600


class GrantDenied(ValueError):
    """An operation has no current matching synthetic grant."""


def _identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 256
        and value == value.strip()
        and not any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
            for char in value
        )
    )


def _timestamp(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 10**15
        and math.isfinite(value)
    )


def fixture_namespace(owner: str, workspace: str) -> str:
    """Private operational partition key, not an identity or authority grant."""
    if not _identifier(owner) or not _identifier(workspace):
        raise GrantDenied("Owner and workspace must be bounded identifiers.")
    pair = json.dumps([owner, workspace], ensure_ascii=False, separators=(",", ":"))
    return "fixture:" + hashlib.sha256(pair.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RunBinding:
    owner: str
    workspace: str
    session_id: str
    turn_id: str
    worker_id: str
    generation: str
    capabilities: tuple[str, ...]

    @property
    def storage_namespace(self) -> str:
        return fixture_namespace(self.owner, self.workspace)

    def __post_init__(self) -> None:
        for field in (
            self.owner, self.workspace, self.session_id, self.turn_id,
            self.worker_id, self.generation,
        ):
            if not _identifier(field):
                raise GrantDenied("Every run binding requires a bounded identifier.")
        if (
            not isinstance(self.capabilities, tuple)
            or not self.capabilities
            or len(self.capabilities) > 32
            or any(
                not isinstance(capability, str)
                or re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", capability) is None
                for capability in self.capabilities
            )
            or len(set(self.capabilities)) != len(self.capabilities)
        ):
            raise GrantDenied("Capabilities must be a bounded, explicit tuple.")


class GrantAuthority:
    def __init__(
        self, store: Store, *, clock: Callable[[], float] = time.time,
        mode: str = "synthetic",
    ) -> None:
        if mode not in ("synthetic", "local"):
            raise GrantDenied("Unknown grant authority mode.")
        self.mode = mode
        self.store = store
        self.clock = clock
        self._clock_floor = 0.0
        # Helpers resolve grants on parallel broker threads: read and advance the floor
        # atomically, or two concurrent readings look like a clock that went backwards.
        self._clock_lock = threading.Lock()

    def _now(self) -> float:
        with self._clock_lock:
            now = self.clock()
            if not _timestamp(now) or now < self._clock_floor:
                raise GrantDenied("Invalid or regressed fixture clock.")
            self._clock_floor = float(now)
            return float(now)

    def issue(self, binding: RunBinding, *, ttl: float = 60) -> str:
        if not isinstance(binding, RunBinding):
            raise GrantDenied("A validated run binding is required.")
        limit = _LOCAL_TTL if self.mode == "local" else 300
        if not _timestamp(ttl) or not 0 < ttl <= limit:
            raise GrantDenied(f"Grant TTL must be between 0 and {limit} seconds.")
        now = self._now()
        token = secrets.token_urlsafe(32)
        document = asdict(binding)
        document["capabilities"] = list(binding.capabilities)
        document.update({
            "authority": self.mode,
            "issued_at": now,
            "expires_at": now + ttl,
        })
        self.store.store_grant(self._grant_id(token), document)
        return token

    def _grant_id(self, token: str) -> str:
        if self.mode == "local":
            return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()
        return token

    def resolve(self, token: str, *, worker_id: str, generation: str) -> RunBinding:
        """Return the trusted binding for a local grant presented by one worker generation."""
        if self.mode != "local":
            raise GrantDenied("Only local authority resolves worker grants.")
        if not _identifier(token) or len(token) > 512:
            raise GrantDenied("Invalid grant handle.")
        grant_id = self._grant_id(token)
        document = self.store.get_grant(grant_id)
        if document is None:
            raise GrantDenied("Unknown grant.")
        if document.get("authority") != "local" or document.get("revoked") is not False:
            raise GrantDenied("Grant is revoked or has an unsupported authority.")
        now = self._now()
        issued = document.get("issued_at")
        expires = document.get("expires_at")
        if (
            not _timestamp(issued) or not _timestamp(expires)
            or expires <= issued or expires - issued > _LOCAL_TTL or now < issued
        ):
            raise GrantDenied("Grant clock binding is invalid.")
        if now >= expires:
            self.store.revoke_grant(grant_id)
            raise GrantDenied("Grant expired.")
        if document.get("worker_id") != worker_id or document.get("generation") != generation:
            raise GrantDenied("Grant belongs to another worker generation.")
        try:
            return RunBinding(
                owner=document["owner"], workspace=document["workspace"],
                session_id=document["session_id"], turn_id=document["turn_id"],
                worker_id=document["worker_id"], generation=document["generation"],
                capabilities=tuple(document["capabilities"]),
            )
        except (KeyError, TypeError) as error:
            raise GrantDenied("Stored grant is malformed.") from error

    def authorize(
        self, token: str, binding: RunBinding, capability: str,
    ) -> None:
        if not _identifier(token) or not isinstance(binding, RunBinding):
            raise GrantDenied("Invalid grant handle or expected binding.")
        document = self.store.get_grant(token)
        if document is None:
            raise GrantDenied("Unknown grant.")
        if document.get("authority") != "synthetic" or document.get("revoked") is not False:
            raise GrantDenied("Grant is revoked or has an unsupported authority.")
        now = self._now()
        issued = document.get("issued_at")
        expires = document.get("expires_at")
        if (
            not _timestamp(issued) or not _timestamp(expires)
            or expires <= issued or expires - issued > 300 or now < issued
        ):
            raise GrantDenied("Grant clock binding is invalid.")
        if now >= expires:
            self.store.revoke_grant(token)
            raise GrantDenied("Grant expired.")
        expected = asdict(binding)
        expected["capabilities"] = list(binding.capabilities)
        if any(document.get(key) != value for key, value in expected.items()):
            raise GrantDenied("Grant does not match this worker generation and run.")
        if capability not in binding.capabilities:
            raise GrantDenied("Capability was not granted.")

    def revoke(self, token: str) -> None:
        self.store.revoke_grant(self._grant_id(token))
