from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_bootstrap_creates_a_reviewed_opening_command_after_provisioning() -> None:
    script = _text("scripts/bootstrap-local-paper.ps1")

    provision = script.index('"provision")')
    open_account = script.index('"open-account")')

    assert provision < open_account


def test_launcher_waits_for_worker_ack_before_starting_the_desktop() -> None:
    script = _text("scripts/start-local-paper.ps1")

    lease = script.index("Wait-WorkerLease -HolderId $configuredWorkerId")
    opening = script.index('Invoke-NodeHelper -Command "verify-opening"')
    desktop = script.index('if ($NoDesktop)')

    assert lease < opening < desktop


def test_verify_flow_opens_account_before_asserting_the_opening_postcondition() -> None:
    script = _text("scripts/verify-local-paper.ps1")

    open_account = script.index('Invoke-NodeHelper -Command "open-account"')
    verify_opening = script.index('Invoke-NodeHelper -Command "verify-opening"')
    opening_postcondition = script.index("$opening = Invoke-PsqlScalar")
    exercise_command = script.index('Invoke-NodeHelper -Command "exercise-command"')

    assert open_account < verify_opening < opening_postcondition < exercise_command


def test_opening_wait_is_idempotent_only_for_the_complete_journal_postcondition() -> None:
    helper = _text("scripts/local-paper.mjs")
    start = helper.index("async function waitForAccountOpening()")
    end = helper.index("async function exercisePauseCommand()")
    opening_wait = helper[start:end]

    for expected in (
        'current.state === "open"',
        'current.transactionCount === "1"',
        'current.postingCount === "2"',
        'current.settledCash === "10000000"',
    ):
        assert expected in opening_wait
