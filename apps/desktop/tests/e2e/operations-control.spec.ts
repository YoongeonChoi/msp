import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

import type {
  OperationsSnapshot,
  UnknownResolutionSnapshotV2
} from "../../src/lib/operationsContracts";
import { makeOperationsSnapshot } from "../operationsFixture";
import {
  makeRequestedUnknownResolutionSnapshot,
  makeUnknownResolutionSnapshot
} from "../unknownResolutionFixture";

interface CapturedRpc {
  readonly grants: Array<Record<string, unknown>>;
  readonly commands: Array<Record<string, unknown>>;
  readonly reviews: Array<Record<string, unknown>>;
  readonly incidents: Array<Record<string, unknown>>;
  readonly unknownGrants: Array<Record<string, unknown>>;
  readonly unknownRequests: Array<Record<string, unknown>>;
  readonly unknownReviews: Array<Record<string, unknown>>;
}

interface MockOperationsOptions {
  readonly operationsSnapshot?: OperationsSnapshot;
  readonly unknownSnapshot?: UnknownResolutionSnapshotV2;
  readonly applyUnknownReviewProjection?: boolean;
}

test.describe("operations RPC safety boundary", () => {
  for (const viewport of [
    { name: "desktop", width: 1280, height: 820 },
    { name: "mobile", width: 390, height: 844 }
  ]) {
    test(`${viewport.name} runs strict grant-bound RPCs without implying LIVE or worker completion`, async ({ page }) => {
      const captured = emptyCapturedRpc();
      await page.setViewportSize(viewport);
      await mockControlPlaneRealtime(page);
      await mockOperationsRpc(page, captured);
      await page.goto("/?page=control");
      await expect(page).toHaveTitle("KR Auto Trading Lab");
      await expect(page.getByRole("heading", { name: "안전 명령 센터" })).toBeVisible();
      await expect(page.getByText("LIVE 금지").first()).toBeVisible();
      await expect(page.getByText("승인됨 · 적용 전")).toBeVisible();
      await expect(page.getByText(/Worker ACK 없음/)).toBeVisible();
      await expect(page.getByRole("button", { name: /실주문 허용 활성화/ })).toHaveCount(0);
      const realtimeStatus = page.getByText(/^Realtime:/);
      await expect(realtimeStatus).toContainText("연결 · 서버 신호");
      await expect(realtimeStatus).not.toContainText("대기");

      const pauseButton = page.getByRole("button", { name: /PAPER 일시정지/ });
      await expect(pauseButton).toBeEnabled();
      await pauseButton.focus();
      const dismissedDialog = handleNextDialog(page, "dismiss");
      await pauseButton.press("Enter");
      await dismissedDialog;
      await expect(pauseButton).toBeFocused();
      expect(captured.grants).toHaveLength(0);

      const acceptedDialog = handleNextDialog(page, "accept");
      await pauseButton.press("Enter");
      await acceptedDialog;
      await expect.poll(() => captured.grants.length).toBe(1);
      await expect.poll(() => captured.commands.length).toBe(1);
      expect(rpcArgument(captured.grants[0], "request_payload")).toMatchObject({
        schema_version: 1,
        bound_action: "request",
        bound_command_type: "pause_paper",
        command_payload: { schema_version: 1, command_type: "pause_paper" }
      });
      const submittedCommand = rpcArgument(captured.commands[0], "request_payload");
      const requestDraft = rpcArgument(captured.grants[0], "request_payload").command_payload as Record<string, unknown>;
      expect(submittedCommand).toMatchObject({
        schema_version: 1,
        command_type: "pause_paper",
        bound_action: "request",
        bound_command_type: "pause_paper"
      });
      expect(submittedCommand.request_id).toBe(requestDraft.request_id);
      expect(submittedCommand.idempotency_key).toBe(requestDraft.idempotency_key);
      expect(submittedCommand.command_hash).toBe("c".repeat(64));
      await expect(page.locator('[role="status"][aria-live="polite"]')).toContainText(
        "Worker ACK가 오기 전에는 적용 완료가 아닙니다"
      );

      const approveButton = page.getByRole("button", { name: "승인", exact: true });
      await expect(approveButton).toBeEnabled();
      const approveDialog = handleNextDialog(page, "accept");
      await approveButton.press("Enter");
      await approveDialog;
      await expect.poll(() => captured.reviews.length).toBe(1);
      await expect.poll(() => captured.grants.length).toBe(2);
      const reviewGrantEnvelope = rpcArgument(captured.grants[1], "request_payload");
      expect(reviewGrantEnvelope).toMatchObject({
        bound_action: "review",
        bound_command_type: "resume_paper",
        command_payload: { command_type: "resume_paper", decision: "approve" }
      });
      const reviewDraft = reviewGrantEnvelope.command_payload as Record<string, unknown>;
      const submittedReview = rpcArgument(captured.reviews[0], "review_payload");
      expect(submittedReview.review_id).toBe(reviewDraft.review_id);
      expect(submittedReview.command_hash).toBe("d".repeat(64));
      expect(submittedReview.command_hash).not.toBe(snapshotCommandHash());

      const incidentButton = page.getByRole("button", { name: "확인 접수" });
      await expect(incidentButton).toBeEnabled();
      await incidentButton.press("Enter");
      await expect.poll(() => captured.incidents.length).toBe(1);
      expect(rpcArgument(captured.incidents[0], "action_payload")).toMatchObject({
        schema_version: 1,
        action: "acknowledge"
      });

      const accessibility = await new AxeBuilder({ page }).analyze();
      const blockingViolations = accessibility.violations.filter(
        (violation) => violation.impact === "critical" || violation.impact === "serious"
      );
      expect(blockingViolations).toEqual([]);
      if (viewport.name === "mobile") {
        await expect(page.getByRole("combobox", { name: "페이지 선택" })).toBeVisible();
      }
    });
  }

  test("SUBSCRIBED without a server invalidation remains read-only", async ({ page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page, { sendSignal: false });
    await mockOperationsRpc(page, captured);
    await page.goto("/?page=control");

    await expect(page.getByText("Realtime: 연결 · 서버 신호 대기")).toBeVisible();
    await expect(page.getByRole("button", { name: /PAPER 일시정지/ })).toBeDisabled();
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
  });

  test("offline actions are not transmitted or replayed after reconnect", async ({ context, page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured);
    await page.goto("/?page=control");
    const pauseButton = page.getByRole("button", { name: /PAPER 일시정지/ });
    await expect(pauseButton).toBeEnabled();

    await context.setOffline(true);
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    await expect(pauseButton).toBeDisabled();
    await expect(page.locator('[role="alert"][aria-live="assertive"]')).toContainText("오프라인 작업은 전송되지 않으며");
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);

    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await expect(pauseButton).toBeEnabled();
    await page.waitForTimeout(250);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
  });

  test("operator submits explicit unknown evidence only through dedicated V2 RPCs", async ({ page }) => {
    const captured = emptyCapturedRpc();
    const operationsSnapshot = makeCurrentOperationsSnapshot();
    if (operationsSnapshot.access.actor === null) {
      throw new Error("fixture actor is required");
    }
    operationsSnapshot.access.actor.roles = ["operator"];
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, {
      operationsSnapshot,
      unknownSnapshot: makeCurrentUnknownResolutionSnapshot(makeUnknownResolutionSnapshot())
    });
    await page.goto("/?page=control");

    const requestButton = page.getByRole("button", { name: "AAL2 step-up 후 조정 요청" });
    await expect(requestButton).toBeDisabled();
    const evidenceSha = "d".repeat(64);
    await page.getByLabel("Evidence artifact URI").fill(`urn:sha256:${evidenceSha}`);
    await page.getByLabel("Evidence SHA-256").fill(evidenceSha);
    await page.getByLabel("Evidence captured at").fill(new Date(Date.now() - 60_000).toISOString());
    await page.getByLabel("확정 terminal 상태").selectOption("canceled");
    await page.getByLabel("누락 체결 manifest (strict JSON array)").fill("[]");
    await expect(requestButton).toBeEnabled();

    const acceptedDialog = handleNextDialog(page, "accept");
    await requestButton.click();
    await acceptedDialog;
    await expect.poll(() => captured.unknownGrants.length).toBe(1);
    await expect.poll(() => captured.unknownRequests.length).toBe(1);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
    expect(rpcArgument(captured.unknownGrants[0], "request_payload")).toMatchObject({
      schema_version: 2,
      bound_action: "request",
      bound_command_type: "close_unknown_execution",
      command_payload: {
        schema_version: 2,
        terminal_status: "canceled",
        missing_fills: []
      }
    });
    expect(rpcArgument(captured.unknownRequests[0], "request_payload")).toMatchObject({
      schema_version: 2,
      bound_action: "request",
      bound_command_type: "close_unknown_execution",
      terminal_status: "canceled",
      missing_fills: []
    });
    await expect(page.locator('[role="status"][aria-live="polite"]')).toContainText(
      "독립 승인과 Worker ACK 전에는 회계 조정 완료가 아닙니다"
    );
    await expect(page.getByText("Worker 적용·회계 확인")).toHaveCount(0);

    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(accessibility.violations.filter(
      (violation) => violation.impact === "critical" || violation.impact === "serious"
    )).toEqual([]);
  });

  test("maker cannot review the same unknown-resolution request", async ({ page }) => {
    const captured = emptyCapturedRpc();
    const unknownSnapshot = makeCurrentUnknownResolutionSnapshot(makeRequestedUnknownResolutionSnapshot());
    const operationsSnapshot = makeCurrentOperationsSnapshot();
    if (operationsSnapshot.access.actor === null || unknownSnapshot.cases[0].request === null) {
      throw new Error("requested fixture actor is required");
    }
    operationsSnapshot.access.actor = {
      actor_id: unknownSnapshot.cases[0].request.requested_by.actor_id,
      display_name: "요청자 겸 승인 역할 사용자",
      roles: ["operator", "risk_approver"]
    };
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, { operationsSnapshot, unknownSnapshot });
    await page.goto("/?page=control");

    await expect(page.getByText("본인이 요청한 회계 조정은 승인하거나 거절할 수 없습니다.")).toBeVisible();
    await expect(page.getByRole("button", { name: "증거 승인" })).toBeDisabled();
    await expect(page.getByRole("button", { name: "증거 거절" })).toBeDisabled();
    expect(captured.unknownGrants).toHaveLength(0);
    expect(captured.unknownReviews).toHaveLength(0);
  });

  test("independent approval remains incomplete until Worker ACK and accounting postcondition", async ({ page }) => {
    const captured = emptyCapturedRpc();
    const unknownSnapshot = makeCurrentUnknownResolutionSnapshot(makeRequestedUnknownResolutionSnapshot());
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, {
      unknownSnapshot,
      applyUnknownReviewProjection: true
    });
    await page.goto("/?page=control");

    const approveButton = page.getByRole("button", { name: "증거 승인" });
    await expect(approveButton).toBeEnabled();
    const acceptedDialog = handleNextDialog(page, "accept");
    await approveButton.click();
    await acceptedDialog;
    await expect.poll(() => captured.unknownGrants.length).toBe(1);
    await expect.poll(() => captured.unknownReviews.length).toBe(1);
    expect(captured.reviews).toHaveLength(0);
    expect(rpcArgument(captured.unknownGrants[0], "request_payload")).toMatchObject({
      schema_version: 2,
      bound_action: "review",
      bound_command_type: "close_unknown_execution",
      command_payload: { decision: "approve", reviewer_role: "risk_approver" }
    });
    await expect(page.getByText("승인됨 · Worker ACK 대기")).toBeVisible();
    const timeline = page.getByRole("list", { name: "조정 요청·승인·Worker ACK·postcondition 타임라인" });
    await expect(timeline.getByText(/3\. Worker ACK/)).toBeVisible();
    await expect(timeline.getByText("미확인 · 대기").first()).toBeVisible();
    await expect(page.getByText("Worker 적용·회계 확인")).toHaveCount(0);
    await expect(page.locator('[role="status"][aria-live="polite"]')).toContainText(
      "Worker claim/application과 회계 postcondition을 계속 확인하세요"
    );
  });

  test("offline unknown-resolution request is never transmitted or replayed", async ({ context, page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, {
      unknownSnapshot: makeCurrentUnknownResolutionSnapshot(makeUnknownResolutionSnapshot())
    });
    await page.goto("/?page=control");

    const evidenceSha = "d".repeat(64);
    await page.getByLabel("Evidence artifact URI").fill(`urn:sha256:${evidenceSha}`);
    await page.getByLabel("Evidence SHA-256").fill(evidenceSha);
    await page.getByLabel("Evidence captured at").fill(new Date(Date.now() - 60_000).toISOString());
    await page.getByLabel("확정 terminal 상태").selectOption("canceled");
    await page.getByLabel("누락 체결 manifest (strict JSON array)").fill("[]");
    const requestButton = page.getByRole("button", { name: "AAL2 step-up 후 조정 요청" });
    await expect(requestButton).toBeEnabled();

    await context.setOffline(true);
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    await expect(requestButton).toBeDisabled();
    await expect(page.locator('[role="alert"][aria-live="assertive"]')).toContainText(
      "오프라인 작업은 전송되지 않으며"
    );
    expect(captured.unknownGrants).toHaveLength(0);
    expect(captured.unknownRequests).toHaveLength(0);

    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await expect(requestButton).toBeEnabled();
    await page.waitForTimeout(250);
    expect(captured.unknownGrants).toHaveLength(0);
    expect(captured.unknownRequests).toHaveLength(0);
  });
});

async function mockControlPlaneRealtime(
  page: Page,
  { sendSignal = true }: { readonly sendSignal?: boolean } = {}
): Promise<void> {
  let signalVersion = 0;
  await page.routeWebSocket(/e2e\.supabase\.test\/realtime\/v1\/websocket/, (socket) => {
    socket.onMessage((message) => {
      const [joinRef, ref, topic, event, payload] = JSON.parse(String(message)) as readonly [
        string | null,
        string | null,
        string,
        string,
        Record<string, unknown>
      ];
      if (event === "phx_join") {
        const config = payload.config as { readonly postgres_changes?: readonly Record<string, unknown>[] };
        const binding = config.postgres_changes?.[0] ?? {};
        socket.send(JSON.stringify([
          joinRef,
          ref,
          topic,
          "phx_reply",
          { status: "ok", response: { postgres_changes: [{ id: 1, ...binding }] } }
        ]));
        if (sendSignal) {
          signalVersion += 1;
          const signalFrame = JSON.stringify([
            joinRef,
            null,
            topic,
            "postgres_changes",
            {
              ids: [1],
              data: {
                schema: "api",
                table: "control_plane_signal",
                commit_timestamp: new Date().toISOString(),
                type: "UPDATE",
                errors: null,
                columns: [
                  { name: "id", type: "text" },
                  { name: "signal_version", type: "int8" },
                  { name: "signaled_at", type: "timestamptz" },
                  { name: "signal_kind", type: "text" }
                ],
                record: {
                  id: "singleton",
                  signal_version: signalVersion,
                  signaled_at: new Date().toISOString(),
                  signal_kind: "snapshot_invalidated"
                },
                old_record: { id: "singleton" }
              }
            }
          ]);
          setTimeout(() => socket.send(signalFrame), 25);
        }
        return;
      }
      if (event === "heartbeat") {
        socket.send(JSON.stringify([joinRef, ref, topic, "phx_reply", { status: "ok", response: {} }]));
      }
    });
  });
}

async function mockOperationsRpc(
  page: Page,
  captured: CapturedRpc,
  options: MockOperationsOptions = {}
): Promise<void> {
  const snapshot = options.operationsSnapshot ?? makeCurrentOperationsSnapshot();
  const unknownSnapshot = options.unknownSnapshot ?? makeCurrentUnknownResolutionSnapshot(makeUnknownResolutionSnapshot());
  await page.route("https://e2e.supabase.test/**", async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") {
      await fulfillPreflight(route);
      return;
    }
    const path = new URL(request.url()).pathname;
    if (path === "/rest/v1/rpc/get_desktop_operations_snapshot_v1") {
      await fulfillJson(route, snapshot);
      return;
    }
    if (path === "/rest/v1/rpc/get_unknown_resolution_cases_v2") {
      await fulfillJson(route, unknownSnapshot);
      return;
    }
    const body = (await request.postDataJSON()) as Record<string, unknown>;
    if (path === "/rest/v1/rpc/issue_unknown_resolution_step_up_v2") {
      captured.unknownGrants.push(body);
      const envelope = rpcArgument(body, "request_payload");
      const commandPayload = envelope.command_payload as Record<string, unknown>;
      const actionAt = String(commandPayload.requested_at ?? commandPayload.reviewed_at);
      await fulfillJson(route, {
        schema_version: 2,
        step_up_grant_id: envelope.bound_action === "request"
          ? "51515151-5151-4151-8151-515151515151"
          : "52525252-5252-4252-8252-525252525252",
        command_hash: envelope.bound_action === "request" ? "8".repeat(64) : "9".repeat(64),
        step_up_grant_issued_at: actionAt,
        step_up_grant_expires_at: new Date(Date.parse(actionAt) + 4 * 60_000).toISOString(),
        step_up_grant_one_time: true,
        step_up_grant_consumed_at: null,
        bound_action: envelope.bound_action,
        bound_command_type: envelope.bound_command_type
      });
      return;
    }
    if (path === "/rest/v1/rpc/request_unknown_resolution_v2") {
      captured.unknownRequests.push(body);
      const input = rpcArgument(body, "request_payload");
      await fulfillJson(route, unknownRequestReceiptFor(input));
      return;
    }
    if (path === "/rest/v1/rpc/review_unknown_resolution_v2") {
      captured.unknownReviews.push(body);
      const input = rpcArgument(body, "review_payload");
      if (options.applyUnknownReviewProjection) {
        applyUnknownReviewProjection(unknownSnapshot, snapshot, input);
      }
      await fulfillJson(route, unknownReviewReceiptFor(input, unknownSnapshot));
      return;
    }
    if (path === "/rest/v1/rpc/issue_step_up_grant_v1") {
      captured.grants.push(body);
      const envelope = rpcArgument(body, "request_payload");
      const commandPayload = envelope.command_payload as Record<string, unknown>;
      const actionAt = String(commandPayload.requested_at ?? commandPayload.reviewed_at);
      await fulfillJson(route, {
        schema_version: 1,
        step_up_grant_id: captured.grants.length === 1
          ? "41414141-4141-4141-8141-414141414141"
          : "42424242-4242-4242-8242-424242424242",
        command_hash: captured.grants.length === 1 ? "c".repeat(64) : "d".repeat(64),
        step_up_grant_issued_at: actionAt,
        step_up_grant_expires_at: new Date(Date.parse(actionAt) + 4 * 60_000).toISOString(),
        step_up_grant_one_time: true,
        step_up_grant_consumed_at: null,
        bound_action: envelope.bound_action,
        bound_command_type: envelope.bound_command_type
      });
      return;
    }
    if (path === "/rest/v1/rpc/request_operation_command_v1") {
      captured.commands.push(body);
      await fulfillJson(route, commandReceiptFor(rpcArgument(body, "request_payload"), snapshot));
      return;
    }
    if (path === "/rest/v1/rpc/review_operation_command_v1") {
      captured.reviews.push(body);
      await fulfillJson(route, reviewReceiptFor(rpcArgument(body, "review_payload"), snapshot));
      return;
    }
    if (path === "/rest/v1/rpc/act_on_operation_incident_v1") {
      captured.incidents.push(body);
      const action = rpcArgument(body, "action_payload");
      await fulfillJson(route, {
        schema_version: 1,
        incident_id: action.incident_id,
        status: action.action === "acknowledge" ? "acknowledged" : "resolved",
        action_id: action.action_id,
        acted_at: action.acted_at
      });
      return;
    }
    await route.fulfill({ status: 404, body: "unmocked" });
  });
}

function makeCurrentOperationsSnapshot(): OperationsSnapshot {
  const snapshot = makeOperationsSnapshot();
  const now = new Date();
  snapshot.generated_at = now.toISOString();
  snapshot.runtime_health.as_of = now.toISOString();
  snapshot.runtime_health.worker_heartbeat_at = new Date(now.getTime() - 5_000).toISOString();
  snapshot.runtime_health.realtime_last_seen_at = now.toISOString();
  for (const component of snapshot.runtime_health.components) {
    component.observed_at = now.toISOString();
  }
  if (snapshot.qualification) {
    snapshot.qualification.valid_from = new Date(now.getTime() - 60 * 60_000).toISOString();
    snapshot.qualification.valid_until = new Date(now.getTime() + 60 * 60_000).toISOString();
  }
  return snapshot;
}

function makeCurrentUnknownResolutionSnapshot(
  base: UnknownResolutionSnapshotV2
): UnknownResolutionSnapshotV2 {
  const snapshot = structuredClone(base);
  const now = new Date();
  snapshot.generated_at = now.toISOString();
  for (const item of snapshot.cases) {
    item.detected_at = new Date(now.getTime() - 5 * 60_000).toISOString();
    item.reconciliation_updated_at = item.detected_at;
    item.unknown_observation.observed_at = item.detected_at;
    if (item.request !== null) {
      item.request.requested_at = new Date(now.getTime() - 2 * 60_000).toISOString();
      item.request.evidence_captured_at = new Date(now.getTime() - 3 * 60_000).toISOString();
      item.request.expires_at = new Date(now.getTime() + 30 * 60_000).toISOString();
    }
  }
  return snapshot;
}

function commandReceiptFor(input: Record<string, unknown>, snapshot: OperationsSnapshot) {
  const emergency = input.command_type === "emergency_stop";
  return {
    schema_version: 1,
    command_id: input.request_id,
    command_type: input.command_type,
    environment: input.environment,
    state: emergency ? "approved" : "requested",
    requested_by: snapshot.access.actor,
    requested_at: input.requested_at,
    expires_at: input.expires_at,
    qualification_id: input.qualification_id ?? null,
    strategy_version_id: input.strategy_version_id ?? null,
    risk_policy_version_id: input.risk_policy_version_id ?? null,
    release_sha: input.release_sha ?? null,
    ledger_checkpoint: input.ledger_checkpoint ?? null,
    command_hash: emergency ? null : input.command_hash,
    control_plane_receipt: {
      schema_version: 1,
      receipt_id: input.request_id,
      command_id: input.request_id,
      state: emergency ? "approved" : "requested",
      revision: emergency ? 1 : 0,
      persisted_at: input.requested_at,
      approved_at: emergency ? input.requested_at : null,
      approved_by: emergency ? snapshot.access.actor : null
    },
    worker_ack: null
  };
}

function reviewReceiptFor(input: Record<string, unknown>, snapshot: OperationsSnapshot) {
  const original = structuredClone(snapshot.pending_reviews[0]);
  const approved = input.decision === "approve";
  original.state = approved ? "approved" : "rejected";
  original.control_plane_receipt.state = approved ? "approved" : "rejected";
  original.control_plane_receipt.revision = Number(input.expected_receipt_revision) + 1;
  original.control_plane_receipt.persisted_at = String(input.reviewed_at);
  original.control_plane_receipt.approved_at = approved ? String(input.reviewed_at) : null;
  original.control_plane_receipt.approved_by = approved ? snapshot.access.actor : null;
  return original;
}

function unknownRequestReceiptFor(input: Record<string, unknown>) {
  return {
    schema_version: 2,
    command_id: input.request_id,
    break_id: input.break_id,
    intent_id: input.intent_id,
    state: "requested",
    receipt_revision: 0,
    break_revision: Number(input.expected_break_revision) + 1,
    request_digest_sha256: "e".repeat(64),
    review_digest_sha256: null,
    terminal_status: input.terminal_status,
    claim_token: null,
    work_revision: null,
    application_id: null,
    application_sha256: null,
    accounting_mutation_allowed: false,
    resolution_complete: false,
    inserted: true
  };
}

function unknownReviewReceiptFor(
  input: Record<string, unknown>,
  snapshot: UnknownResolutionSnapshotV2
) {
  const item = snapshot.cases[0];
  if (item.request === null) {
    throw new Error("requested unknown-resolution fixture is required");
  }
  const approved = input.decision === "approve";
  return {
    schema_version: 2,
    command_id: input.command_id,
    break_id: item.break_id,
    intent_id: item.intent_id,
    state: approved ? "approved" : "rejected",
    receipt_revision: Number(input.expected_receipt_revision) + 1,
    break_revision: Number(input.expected_break_revision) + 1,
    request_digest_sha256: input.request_digest_sha256,
    review_digest_sha256: "f".repeat(64),
    terminal_status: item.request.terminal_status,
    claim_token: null,
    work_revision: approved ? 0 : null,
    application_id: null,
    application_sha256: null,
    accounting_mutation_allowed: approved,
    resolution_complete: false,
    inserted: true
  };
}

function applyUnknownReviewProjection(
  snapshot: UnknownResolutionSnapshotV2,
  operationsSnapshot: OperationsSnapshot,
  input: Record<string, unknown>
): void {
  const item = snapshot.cases[0];
  if (item.request === null || operationsSnapshot.access.actor === null) {
    throw new Error("requested fixture and independent reviewer are required");
  }
  const reviewedAt = String(input.reviewed_at);
  const approved = input.decision === "approve";
  item.break_revision = Number(input.expected_break_revision) + 1;
  item.reconciliation_updated_at = reviewedAt;
  item.request.state = approved ? "approved" : "rejected";
  item.request.receipt_revision = Number(input.expected_receipt_revision) + 1;
  item.review = {
    schema_version: 2,
    review_id: String(input.review_id),
    decision: approved ? "approved" : "rejected",
    reason_code: approved ? "evidence_sufficient" : "evidence_incomplete",
    reviewed_by: structuredClone(operationsSnapshot.access.actor),
    reviewed_at: reviewedAt,
    request_digest_sha256: item.request.request_digest_sha256,
    review_digest_sha256: "f".repeat(64),
    evidence_sha256: item.request.evidence_sha256
  };
  if (approved) {
    item.work_receipt = {
      schema_version: 2,
      state: "approved",
      work_revision: 0,
      claim_token: null,
      claimed_at: null,
      claim_expires_at: null,
      applied_at: null,
      worker_release_sha: null,
      fencing_token: null
    };
  } else {
    item.break_state = "open";
    item.work_receipt = null;
  }
  snapshot.generated_at = reviewedAt;
}

function rpcArgument(body: Record<string, unknown>, name: string): Record<string, unknown> {
  const value = body[name];
  expect(value).toBeTruthy();
  return value as Record<string, unknown>;
}

function emptyCapturedRpc(): CapturedRpc {
  return {
    grants: [],
    commands: [],
    reviews: [],
    incidents: [],
    unknownGrants: [],
    unknownRequests: [],
    unknownReviews: []
  };
}

function snapshotCommandHash(): string {
  return "b".repeat(64);
}

function handleNextDialog(page: Page, action: "accept" | "dismiss"): Promise<void> {
  return new Promise((resolve) => {
    page.once("dialog", async (dialog) => {
      if (action === "accept") {
        await dialog.accept();
      } else {
        await dialog.dismiss();
      }
      resolve();
    });
  });
}

async function fulfillPreflight(route: Route): Promise<void> {
  await route.fulfill({
    status: 204,
    headers: corsHeaders
  });
}

async function fulfillJson(route: Route, body: unknown): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    headers: corsHeaders,
    body: JSON.stringify(body)
  });
}

const corsHeaders = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, apikey, accept-profile, content-profile, content-type, x-client-info",
  "access-control-allow-methods": "GET, POST, OPTIONS"
};
