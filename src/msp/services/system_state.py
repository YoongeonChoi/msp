from __future__ import annotations

import sqlite3

from msp.db import row_to_dict
from msp.domain.enums import KillMode, SystemMode
from msp.exceptions import ConflictError, IdempotencyError, SafetyError
from msp.services.audit import append_audit, append_outbox
from msp.services.ids import new_id
from msp.settings import Settings
from msp.time import utc_now_iso

TRADING_MODES = {SystemMode.PAPER.value, SystemMode.ARMED.value, SystemMode.LIVE.value}
ORDER_GENERATION_MODES = {
    SystemMode.PAPER.value,
    SystemMode.SHADOW.value,
    SystemMode.ARMED.value,
    SystemMode.LIVE.value,
}


def get_system_state(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM system_state WHERE id = 1").fetchone()
    if row is None:
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO system_state (id, mode, state_version, trading_enabled, updated_at)
            VALUES (1, 'READ_ONLY', 1, 0, ?)
            """,
            (now,),
        )
        row = conn.execute("SELECT * FROM system_state WHERE id = 1").fetchone()
    return row_to_dict(row) or {}


def _trading_enabled(mode: str) -> int:
    return 1 if mode in TRADING_MODES else 0


def _require_idempotency(idempotency_key: str | None) -> str:
    if not idempotency_key:
        raise IdempotencyError("Idempotency-Key is required for state changes")
    return idempotency_key


def transition_system_state(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    command_type: str,
    target_mode: SystemMode,
    reason: str,
    expected_state_version: int | None,
    idempotency_key: str | None,
    actor: str,
    extra_payload: dict | None = None,
) -> dict:
    idempotency_key = _require_idempotency(idempotency_key)
    existing = conn.execute(
        "SELECT * FROM commands WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        return get_system_state(conn)

    if target_mode == SystemMode.LIVE and not settings.allow_live_mode:
        raise SafetyError("LIVE mode is disabled. Set MSP_ALLOW_LIVE_MODE=true only after go-live gates pass.")

    current = get_system_state(conn)
    if expected_state_version is not None and expected_state_version != current["state_version"]:
        raise ConflictError(
            f"state_version mismatch: expected {expected_state_version}, current {current['state_version']}"
        )

    next_version = int(current["state_version"]) + 1
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE system_state
        SET mode = ?, state_version = ?, trading_enabled = ?, updated_at = ?
        WHERE id = 1
        """,
        (target_mode.value, next_version, _trading_enabled(target_mode.value), now),
    )
    conn.execute(
        """
        INSERT INTO commands
            (id, idempotency_key, command_type, target_mode, reason, expected_state_version, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'APPLIED', ?)
        """,
        (
            new_id("cmd"),
            idempotency_key,
            command_type,
            target_mode.value,
            reason,
            expected_state_version,
            now,
        ),
    )
    payload = {
        "from_mode": current["mode"],
        "to_mode": target_mode.value,
        "state_version": next_version,
        "reason": reason,
        **(extra_payload or {}),
    }
    append_audit(conn, f"system.{command_type}", actor, payload, idempotency_key)
    append_outbox(conn, "system_state", "1", f"system.{command_type}", payload)
    return get_system_state(conn)


def command_arm(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    target_mode: SystemMode,
    reason: str,
    expected_state_version: int | None,
    idempotency_key: str | None,
    actor: str,
) -> dict:
    if target_mode in {SystemMode.BOOTING, SystemMode.HALTED, SystemMode.KILLED}:
        raise SafetyError("arm target must be READ_ONLY, PAPER, SHADOW, ARMED, or LIVE")
    current = get_system_state(conn)
    if current["mode"] in {SystemMode.HALTED.value, SystemMode.KILLED.value}:
        raise SafetyError("unlock is required before arming from HALTED or KILLED")
    return transition_system_state(
        conn,
        settings=settings,
        command_type="arm",
        target_mode=target_mode,
        reason=reason,
        expected_state_version=expected_state_version,
        idempotency_key=idempotency_key,
        actor=actor,
    )


def command_halt(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    reason: str,
    expected_state_version: int | None,
    idempotency_key: str | None,
    actor: str,
) -> dict:
    return transition_system_state(
        conn,
        settings=settings,
        command_type="halt",
        target_mode=SystemMode.HALTED,
        reason=reason,
        expected_state_version=expected_state_version,
        idempotency_key=idempotency_key,
        actor=actor,
    )


def command_kill(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    mode: KillMode,
    reason: str,
    expected_state_version: int | None,
    idempotency_key: str | None,
    actor: str,
) -> dict:
    mode = KillMode(mode)
    if mode == KillMode.LIQUIDATE_POSITIONS:
        raise SafetyError("LIQUIDATE_POSITIONS is intentionally not wired as a default kill action")
    state = transition_system_state(
        conn,
        settings=settings,
        command_type="kill",
        target_mode=SystemMode.KILLED,
        reason=reason,
        expected_state_version=expected_state_version,
        idempotency_key=idempotency_key,
        actor=actor,
        extra_payload={"kill_mode": mode.value},
    )
    if mode == KillMode.CANCEL_OPEN_ORDERS:
        now = utc_now_iso()
        conn.execute(
            """
            UPDATE order_intents
            SET status = 'CANCELED', updated_at = ?, reject_reason = 'canceled by kill command'
            WHERE status IN ('DRAFT', 'RISK_APPROVED', 'READY', 'SUBMITTING', 'UNKNOWN')
            """,
            (now,),
        )
    return state


def command_unlock(
    conn: sqlite3.Connection,
    *,
    settings: Settings,
    reason: str,
    expected_state_version: int | None,
    idempotency_key: str | None,
    actor: str,
    confirmation_phrase: str | None,
) -> dict:
    current = get_system_state(conn)
    if current["mode"] not in {SystemMode.HALTED.value, SystemMode.KILLED.value}:
        raise SafetyError("unlock is only allowed from HALTED or KILLED")
    if confirmation_phrase != "I_ACCEPT_MANUAL_RECOVERY":
        raise SafetyError("unlock requires confirmation_phrase=I_ACCEPT_MANUAL_RECOVERY")
    return transition_system_state(
        conn,
        settings=settings,
        command_type="unlock",
        target_mode=SystemMode.READ_ONLY,
        reason=reason,
        expected_state_version=expected_state_version,
        idempotency_key=idempotency_key,
        actor=actor,
    )


def force_halt(conn: sqlite3.Connection, *, actor: str, reason: str) -> dict:
    current = get_system_state(conn)
    if current["mode"] == SystemMode.HALTED.value:
        return current
    now = utc_now_iso()
    next_version = int(current["state_version"]) + 1
    conn.execute(
        """
        UPDATE system_state
        SET mode = 'HALTED', state_version = ?, trading_enabled = 0, updated_at = ?
        WHERE id = 1
        """,
        (next_version, now),
    )
    payload = {"from_mode": current["mode"], "to_mode": "HALTED", "reason": reason}
    append_audit(conn, "system.force_halt", actor, payload)
    append_outbox(conn, "system_state", "1", "system.force_halt", payload)
    return get_system_state(conn)
