"""Brainstem Agent entry point. The `fixture` command runs bounded synthetic fixtures and never starts Grail; other commands are the cell CLI."""

import argparse
import json
from pathlib import Path
import tempfile

from .harness import HarnessError, OfflineHarness
from .policy import GrantAuthority, GrantDenied, fixture_namespace
from .state import StateError, Store


def run_fixture() -> dict:
    calls = 0
    observed_artifact = None

    def transport(request, context):
        nonlocal calls, observed_artifact
        calls += 1
        observed_artifact = context.tools.write(
            context, name="briefing.txt", text="Synthetic briefing: accessibility comes first.\n",
            request_key="briefing",
        )
        return {
            "response": "Saved the synthetic briefing.",
            "agent_logs": "fixture.write completed",
            "session_id": request["session_id"],
            "model": "fixture-not-a-model",
        }

    with tempfile.TemporaryDirectory(prefix="brainstem-agent-fixture-") as temporary:
        directory = Path(temporary).resolve()
        workspace = directory / "workspace"
        workspace.mkdir(mode=0o700)
        database = directory / "state.sqlite3"
        with Store(database) as store:
            harness = OfflineHarness(store, GrantAuthority(store), workspace, transport)
            first = harness.run(
                "fixture-owner", "Write the synthetic briefing.",
                workspace="fixture-workspace", idempotency_key="fixture-turn",
            )
            replay = harness.run(
                "fixture-owner", "Changed text must replay the original terminal result.",
                workspace="fixture-workspace", idempotency_key="fixture-turn",
            )
            artifact_exists = (workspace / "briefing.txt").is_file()
        with Store(database) as store:
            restored = store.reserve_chat(
                fixture_namespace("fixture-owner", "fixture-workspace"),
                "Replay after reopen.", idempotency_key="fixture-turn",
            )
            history = store.history(
                fixture_namespace("fixture-owner", "fixture-workspace"),
                first.response["session_id"],
            )
        checks = {
            "one_fixture_dispatch": calls == 1,
            "artifact_written": artifact_exists,
            "changed_input_replayed": replay.replayed and replay.response == first.response,
            "terminal_result_survived_reopen": restored.response == first.response,
            "successful_history_retained": len(history) == 2,
            "exact_public_response_keys": set(first.response) == {
                "response", "agent_logs", "session_id",
            },
        }
        if not all(checks.values()):
            raise HarnessError("Offline fixture checks failed.")
        return {
            "schema": "brainstem-agent/offline-evidence-v1",
            "stage": "offline-contract-verified",
            "real_grail_executed": False,
            "native_rapp_activated": False,
            "sandbox_qualified": False,
            "checks": checks,
            "artifact": {**observed_artifact, "retained": False},
            "limitations": [
                "Explicitly injected, trusted fixture code; not a real model.",
                "Temporary fixture files are removed after this run.",
                "No OS/network isolation, live worker, or provider qualification.",
                "No native RAPP frame or production execution receipt is emitted.",
                "Full M0 capability gates remain unqualified.",
            ],
        }


def main() -> int:
    import sys

    if sys.argv[1:2] != ["fixture"]:
        from .cli import main as cell_main

        return cell_main(sys.argv[1:], redirect=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["fixture"])
    parser.add_argument("--mode", choices=["synthetic", "live"], default="synthetic")
    parser.add_argument("--output", type=Path, help="Create a new JSON evidence file; never overwrite.")
    arguments = parser.parse_args()
    if arguments.mode != "synthetic":
        parser.error("Live execution is gated and is not implemented by this offline harness.")
    try:
        report = run_fixture()
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        if arguments.output is not None:
            with arguments.output.open("x", encoding="utf-8") as target:
                target.write(encoded)
        print(encoded, end="")
    except (OSError, StateError, GrantDenied, HarnessError) as error:
        parser.exit(1, f"Offline fixture failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
