"""Explicitly synthetic execution fixtures with durable admission and outcomes."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import threading
from typing import Callable, Mapping, Protocol

from .adapter import build_core_request, normalize_core_response
from .policy import GrantAuthority, RunBinding, fixture_namespace
from .state import Store


class HarnessError(RuntimeError):
    """The offline fixture could not complete."""


class TurnUnavailable(HarnessError):
    """An existing turn cannot be redispatched."""


@dataclass(frozen=True)
class TurnContext:
    binding: RunBinding
    grant: str
    tools: "FixtureTools"


@dataclass(frozen=True)
class RunOutcome:
    turn_id: str
    response: dict
    replayed: bool
    stage: str = "synthetic-fixture"


class FixtureTransport(Protocol):
    def __call__(self, request: dict, context: TurnContext) -> Mapping: ...


class FixtureTools:
    def __init__(
        self, store: Store, authority: GrantAuthority, root: Path, *,
        after_effect: Callable[[str], None] | None = None,
    ) -> None:
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise HarnessError("The offline filesystem fixture requires a POSIX host.")
        root = Path(root)
        if root.is_symlink() or not root.is_dir():
            raise HarnessError("Fixture workspace must be an existing non-symlink directory.")
        self.root = root.resolve()
        self.store = store
        self.authority = authority
        self.after_effect = after_effect

    def write(
        self, context: TurnContext, *, name: str, text: str, request_key: str,
    ) -> dict:
        self.authority.authorize(context.grant, context.binding, "fixture.write")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", name) is None:
            raise HarnessError("Fixture artifact name must be one bounded filename.")
        if not isinstance(text, str) or len(text.encode("utf-8")) > 64_000:
            raise HarnessError("Fixture artifact must be text of at most 64000 bytes.")
        if not isinstance(request_key, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", request_key) is None:
            raise HarnessError("Fixture request key is invalid.")
        binding = context.binding
        namespace = binding.storage_namespace
        request = {
            "workspace": binding.workspace,
            "session_id": binding.session_id,
            "turn_id": binding.turn_id,
            "operation": "fixture.write",
            "name": name,
            "text": text,
        }
        job = self.store.admit_job(
            namespace, f"{binding.turn_id}:{request_key}", request,
        )
        if not job.created:
            if job.state == "succeeded":
                return dict(job.result)
            if job.state != "accepted":
                raise TurnUnavailable("A started or terminal fixture effect cannot be replayed.")
        self.store.transition_job(namespace, job.job_id, "accepted", "running")
        effect_started = False
        try:
            self.authority.authorize(context.grant, binding, "fixture.write")
            payload = text.encode("utf-8")
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=directory,
                )
                effect_started = True
                with os.fdopen(descriptor, "wb") as artifact:
                    artifact.write(payload)
                    artifact.flush()
                    os.fsync(artifact.fileno())
                os.fsync(directory)
            finally:
                os.close(directory)
            if self.after_effect is not None:
                self.after_effect(job.job_id)
            result = {
                "artifact": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "stage": "synthetic-fixture",
            }
            self.store.transition_job(
                namespace, job.job_id, "running", "succeeded", result,
            )
            return result
        except Exception:
            self.store.transition_job(
                namespace, job.job_id, "running",
                "uncertain" if effect_started else "failed",
            )
            raise


class OfflineHarness:
    def __init__(
        self, store: Store, authority: GrantAuthority, root: Path,
        transport: FixtureTransport, *, worker_id: str = "fixture-worker",
        generation: str | None = None, mode: str = "synthetic",
        after_effect: Callable[[str], None] | None = None,
    ) -> None:
        if mode != "synthetic":
            raise HarnessError("Live Grail execution is not implemented or qualified.")
        if not callable(transport):
            raise HarnessError("An explicitly injected fixture transport is required.")
        self.store = store
        self.authority = authority
        self.transport = transport
        self.worker_id = worker_id
        self.generation = generation if generation is not None else secrets.token_hex(16)
        self.tools = FixtureTools(store, authority, root, after_effect=after_effect)
        self._worker_lock = threading.Lock()

    def run(
        self, owner: str, user_input: str, *, workspace: str,
        session_id: str | None = None, idempotency_key: str | None = None,
    ) -> RunOutcome:
        build_core_request(user_input, session_id or "fixture-preflight", [])
        namespace = fixture_namespace(owner, workspace)
        if not self._worker_lock.acquire(blocking=False):
            raise TurnUnavailable("This fixture worker already has an active run.")
        grant = None
        try:
            reservation = self.store.reserve_chat(
                namespace, user_input, session_id, idempotency_key,
            )
            if not reservation.created:
                if reservation.state == "succeeded":
                    return RunOutcome(
                        reservation.turn_id, dict(reservation.response), replayed=True,
                    )
                raise TurnUnavailable(
                    f"Retained turn is {reservation.state}; inference was not redispatched."
                )
            self.store.mark_chat_running(namespace, reservation.turn_id)
            dispatched = False
            try:
                binding = RunBinding(
                    owner, workspace, reservation.session_id, reservation.turn_id,
                    self.worker_id, self.generation, ("fixture.write",),
                )
                grant = self.authority.issue(binding)
                request = build_core_request(
                    user_input, reservation.session_id,
                    self.store.history(namespace, reservation.session_id),
                )
                context = TurnContext(binding, grant, self.tools)
                dispatched = True
                payload = self.transport(request, context)
                response = normalize_core_response(payload, reservation.session_id)
                self.store.finish_chat(namespace, reservation.turn_id, "succeeded", response)
                return RunOutcome(reservation.turn_id, response, replayed=False)
            except Exception as error:
                self.store.finish_chat(
                    namespace, reservation.turn_id,
                    "uncertain" if dispatched else "failed",
                )
                raise HarnessError(
                    "Fixture turn did not complete; inspect retained state before retrying."
                ) from error
        finally:
            try:
                if grant is not None:
                    self.authority.revoke(grant)
            finally:
                self._worker_lock.release()
