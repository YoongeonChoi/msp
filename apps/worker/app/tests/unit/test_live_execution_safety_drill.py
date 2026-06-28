import pytest

from app.tools.run_live_execution_safety_drill_once import (
    format_live_execution_safety_drill_result,
    main,
    run_live_execution_safety_drill,
)


async def test_live_execution_safety_drill_exercises_local_invariants() -> None:
    result = await run_live_execution_safety_drill()

    assert result.missing_evidence_blocked == 1
    assert result.pre_broker_manual_check == 1
    assert result.provider_result_recorded == 1
    assert result.duplicate_blocked == 1
    assert result.broker_calls == 1
    assert format_live_execution_safety_drill_result(result) == (
        "FINAL=PASS live_execution_safety_drill missing_evidence_blocked=1 "
        "pre_broker_manual_check=1 provider_result_recorded=1 duplicate_blocked=1 "
        "broker_calls=1"
    )


def test_live_execution_safety_drill_cli_prints_single_pass_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 0

    output_lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert output_lines == [
        "FINAL=PASS live_execution_safety_drill missing_evidence_blocked=1 "
        "pre_broker_manual_check=1 provider_result_recorded=1 duplicate_blocked=1 "
        "broker_calls=1"
    ]
