from __future__ import annotations

import pytest

from blackjack_ai.benchmark.harness import BenchmarkHarness


class _StalledWaitApiClient:
    def __init__(self) -> None:
        self.poll_count = 0

    def get_round(self, round_id: str) -> dict[str, object]:
        self.poll_count += 1
        return {
            "round_id": round_id,
            "phase": "dealer_turn",
            "action_count": 2,
            "next_request": {"type": "wait", "reason": "dealer_turn"},
        }


def test_harness_raises_for_unsupported_request_types() -> None:
    harness = BenchmarkHarness(_StalledWaitApiClient())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Unsupported next_request type"):
        harness._play_round(  # noqa: SLF001 - focused validation of fail-fast benchmark behavior
            {
                "round_id": "round-unsupported",
                "phase": "dealer_turn",
                "action_count": 0,
                "next_request": {"type": "mystery"},
            },
            {},
            1,
        )


def test_harness_raises_when_wait_state_never_progresses() -> None:
    api_client = _StalledWaitApiClient()
    harness = BenchmarkHarness(api_client)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="did not progress"):
        harness._play_round(  # noqa: SLF001 - focused validation of fail-fast benchmark behavior
            {
                "round_id": "round-stalled",
                "phase": "dealer_turn",
                "action_count": 2,
                "next_request": {"type": "wait", "reason": "dealer_turn"},
            },
            {},
            1,
        )

    assert api_client.poll_count == 5
