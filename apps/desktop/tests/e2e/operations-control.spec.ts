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
  readonly afterStepUpGrant?: (snapshot: OperationsSnapshot) => void;
  readonly authenticatedDevice?: boolean;
  readonly snapshotRpcError?: {
    readonly status: number;
    readonly body: Record<string, unknown>;
  };
}

interface CapturedAuthLifecycle {
  tokenRequests: number;
  userRequests: number;
  logoutScopes: Array<string | null>;
  logoutFailuresRemaining: number;
}

test.describe("operations RPC safety boundary", () => {
  for (const viewport of [
    { name: "desktop", width: 1280, height: 820 },
    { name: "mobile", width: 390, height: 844 }
  ]) {
    test(`${viewport.name} runs strict grant-bound RPCs without implying LIVE or worker completion`, async ({ page }) => {
      const captured = emptyCapturedRpc();
      const operationsSnapshot = makeCurrentOperationsSnapshot();
      operationsSnapshot.runtime_health.execution_enabled = true;
      await page.setViewportSize(viewport);
      await mockControlPlaneRealtime(page);
      await mockOperationsRpc(page, captured, { operationsSnapshot });
      await page.goto("/?page=control");
      await expect(page).toHaveTitle("KR Auto Trading Lab");
      await expect(page.getByRole("heading", { name: "현재 실행 상태" })).toBeVisible();
      await expect(page.getByText("LIVE 잠금", { exact: true })).toBeVisible();
      await expect(page.getByText("검토 승인 · Worker 대기")).toBeVisible();
      await expect(page.getByText(/Worker 적용 확인 없음/)).toBeVisible();
      await expect(page.getByRole("button", { name: /실주문 허용 활성화/ })).toHaveCount(0);
      const dataBasis = page.locator("dd").filter({ hasText: "실시간 신호" });
      await expect(dataBasis).toBeVisible();
      await expect(dataBasis).not.toContainText("확인 불가");
      const mountedDrawers = page.locator('dialog[data-variant="drawer"]');
      await expect(mountedDrawers).toHaveCount(0);

      const pauseButton = page.getByRole("button", { name: "모의거래 일시정지", exact: true });
      await expect(pauseButton).toBeEnabled();
      await pauseButton.focus();
      await pauseButton.press("Enter");
      const pauseDialog = page.getByRole("dialog", { name: "모의거래 일시정지" });
      await expect(pauseDialog).toBeVisible();
      const cancelPause = pauseDialog.getByRole("button", { name: "취소" });
      const confirmPause = pauseDialog.getByRole("button", { name: "요청 생성" });
      await expect(cancelPause).toBeFocused();
      await page.keyboard.press("Shift+Tab");
      await expect(confirmPause).toBeFocused();
      await page.keyboard.press("Tab");
      await expect(cancelPause).toBeFocused();
      await page.keyboard.press("Escape");
      await expect(pauseDialog).toBeVisible();
      await cancelPause.press("Enter");
      await expect(pauseDialog).toHaveCount(0);
      await expect(pauseButton).toBeFocused();
      expect(captured.grants).toHaveLength(0);

      await pauseButton.press("Enter");
      await confirmNativeDialog(page, "모의거래 일시정지", "요청 생성");
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
        "Worker 적용 확인 전에는 완료가 아닙니다"
      );

      await page.getByRole("button", { name: "승인 검토" }).click();
      const approvalDrawer = page.getByRole("dialog", { name: "승인 상세" });
      await expect(approvalDrawer).toBeVisible();
      await expect(mountedDrawers).toHaveCount(1);
      const approveButton = page.getByRole("button", { name: "승인", exact: true });
      await expect(approveButton).toBeEnabled();
      await approveButton.press("Enter");
      await confirmNativeDialog(page, "운영 요청 승인", "승인 전송");
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

      await approvalDrawer.getByRole("button", { name: "상세 닫기" }).click();
      await expect(approvalDrawer).toHaveCount(0);
      await expect(mountedDrawers).toHaveCount(0);
      await page.getByRole("button", { name: "사고 확인" }).click();
      const incidentDrawer = page.getByRole("dialog", { name: "사고 상세" });
      await expect(incidentDrawer).toBeVisible();
      const incidentButton = page.getByRole("button", { name: "확인 접수" });
      await expect(incidentButton).toBeEnabled();
      await incidentButton.press("Enter");
      await confirmNativeDialog(page, "사고 확인 접수", "사고 확인 접수");
      await expect.poll(() => captured.incidents.length).toBe(1);
      expect(rpcArgument(captured.incidents[0], "action_payload")).toMatchObject({
        schema_version: 1,
        action: "acknowledge"
      });
      await incidentDrawer.getByRole("button", { name: "상세 닫기" }).click();
      await expect(incidentDrawer).toHaveCount(0);

      const accessibility = await new AxeBuilder({ page }).analyze();
      const blockingViolations = accessibility.violations.filter(
        (violation) => violation.impact === "critical" || violation.impact === "serious"
      );
      expect(blockingViolations).toEqual([]);
      if (viewport.name === "mobile") {
        await expect(page.getByRole("navigation", { name: "주 탐색" })).toBeVisible();
        await expect(page.getByRole("button", { name: "계정·보안" })).toBeVisible();
        const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(horizontalOverflow).toBeLessThanOrEqual(1);
      }
    });
  }

  test("SUBSCRIBED without a server invalidation remains read-only", async ({ page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page, { sendSignal: false });
    await mockOperationsRpc(page, captured);
    await page.goto("/?page=control");

    await expect(page.getByText(/실시간 연결 · 마지막 신호 확인 불가/)).toBeVisible();
    await expect(page.getByRole("button", { name: /모의거래 재개 요청/ })).toBeDisabled();
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
  });

  test("offline actions are not transmitted or replayed after reconnect", async ({ context, page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page);
    const operationsSnapshot = makeCurrentOperationsSnapshot();
    operationsSnapshot.runtime_health.execution_enabled = true;
    await mockOperationsRpc(page, captured, { operationsSnapshot });
    await page.goto("/?page=control");
    const pauseButton = page.getByRole("button", { name: "모의거래 일시정지", exact: true });
    await expect(pauseButton).toBeEnabled();

    await context.setOffline(true);
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    await expect(pauseButton).toBeDisabled();
    await expect(page.getByRole("alert").filter({ hasText: "오프라인 작업은 전송되지 않으며" })).toBeVisible();
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);

    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await expect(pauseButton).toBeEnabled();
    await page.waitForTimeout(250);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
  });

  test("an open confirmation is invalidated when the device goes offline", async ({ context, page }) => {
    const captured = emptyCapturedRpc();
    const operationsSnapshot = makeCurrentOperationsSnapshot();
    operationsSnapshot.runtime_health.execution_enabled = true;
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, { operationsSnapshot });
    await page.goto("/?page=control");

    await page.getByRole("button", { name: "모의거래 일시정지", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "모의거래 일시정지" });
    await expect(dialog).toBeVisible();
    await context.setOffline(true);
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    await expect(dialog.getByRole("button", { name: "요청 생성" })).toBeDisabled();
    await expect(dialog).toContainText(/기기 연결|최신 전체 상태/);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
    await dialog.getByRole("button", { name: "취소" }).click();

    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await page.waitForTimeout(250);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
  });

  test("a post-grant state change discards the grant and sends no command", async ({ page }) => {
    const captured = emptyCapturedRpc();
    const operationsSnapshot = makeCurrentOperationsSnapshot();
    operationsSnapshot.runtime_health.execution_enabled = true;
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, {
      operationsSnapshot,
      afterStepUpGrant: (snapshot) => {
        snapshot.runtime_health.state_version += 1;
      }
    });
    await page.goto("/?page=control");

    await page.getByRole("button", { name: "모의거래 일시정지", exact: true }).click();
    await confirmNativeDialog(page, "모의거래 일시정지", "요청 생성");
    await expect.poll(() => captured.grants.length).toBe(1);
    await expect(page.locator('[role="status"][aria-live="polite"]')).toContainText("상태가 변경됨");
    expect(captured.commands).toHaveLength(0);
  });

  test("worker offline blocks commands while a current incident acknowledgement remains available", async ({ page }) => {
    const captured = emptyCapturedRpc();
    const operationsSnapshot = makeCurrentOperationsSnapshot();
    operationsSnapshot.runtime_health.execution_enabled = true;
    operationsSnapshot.runtime_health.overall_state = "offline";
    const worker = operationsSnapshot.runtime_health.components.find((component) => component.component === "worker");
    if (!worker) {
      throw new Error("worker fixture is required");
    }
    worker.state = "offline";
    worker.detail_code = "heartbeat_missing";
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, { operationsSnapshot });
    await page.goto("/?page=control");

    await expect(page.getByRole("button", { name: "모의거래 일시정지", exact: true })).toBeDisabled();
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
    await page.getByRole("button", { name: "사고 확인", exact: true }).click();
    const incidentDrawer = page.getByRole("dialog", { name: "사고 상세" });
    await expect(incidentDrawer).toBeVisible();
    await expect(incidentDrawer.getByRole("button", { name: "확인 접수" })).toBeEnabled();
    await incidentDrawer.getByRole("button", { name: "확인 접수" }).click();
    await confirmNativeDialog(page, "사고 확인 접수", "사고 확인 접수");
    await expect.poll(() => captured.incidents.length).toBe(1);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
  });

  test("back and forward navigation preserve the URL, selected page, and heading focus", async ({ page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured);
    await page.goto("/?page=control");

    await page.getByRole("button", { name: "계정·보안", exact: true }).click();
    await expect(page).toHaveURL(/\?page=settings$/);
    const settingsHeading = page.getByRole("heading", { name: "계정·보안", level: 1 });
    await expect(settingsHeading).toBeFocused();
    await expect(page.getByRole("heading", { name: "operator@example.test", level: 2 })).toBeVisible();

    await page.goBack();
    await expect(page).toHaveURL(/\?page=control$/);
    await expect(page.getByRole("heading", { name: "운영 제어", level: 1 })).toBeFocused();

    await page.goForward();
    await expect(page).toHaveURL(/\?page=settings$/);
    await expect(page.getByRole("heading", { name: "계정·보안", level: 1 })).toBeFocused();
  });

  test("a disconnected device is routed to one-time connection without operations errors", async ({ page }) => {
    const captured = emptyCapturedRpc();
    await mockOperationsRpc(page, captured, { authenticatedDevice: false });
    await page.goto("/?page=control");

    await expect(page).toHaveURL(/\?page=settings$/);
    await expect(page.getByRole("heading", { name: "이 기기는 아직 연결되지 않았습니다" })).toBeVisible();
    await expect(page.getByText(/운영 데이터 확인 실패/)).toHaveCount(0);
    await expect(page.getByText(/로그인/)).toHaveCount(0);
    await page.getByRole("button", { name: "이 기기 연결", exact: true }).click();
    const deviceConnectionDrawer = page.getByRole("dialog", { name: "이 기기 연결" });
    await expect(deviceConnectionDrawer).toBeVisible();
    await expect(deviceConnectionDrawer.getByLabel("운영 계정 이메일")).toBeVisible();
    await expect(deviceConnectionDrawer.getByLabel("비밀번호")).toBeVisible();
    await deviceConnectionDrawer.getByRole("button", { name: "기기 연결 닫기" }).click();
    await expect(deviceConnectionDrawer).toHaveCount(0);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
  });

  test("PGRST106 explains the backend setup gate and sends no mutation", async ({ page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page, { sendSignal: false });
    await mockOperationsRpc(page, captured, {
      snapshotRpcError: {
        status: 406,
        body: {
          code: "PGRST106",
          hint: "Only the following schemas are exposed: public, graphql_public",
          message: "Invalid schema: api"
        }
      }
    });

    await page.goto("/?page=control");

    await expect(page.getByText("이 기기 연결됨", { exact: true })).toBeVisible();
    const setupGateLabels = page.getByText("운영 API 준비 상태 확인 필요", { exact: true });
    await expect(setupGateLabels).toHaveCount(2);
    await expect(setupGateLabels.first()).toBeVisible();
    await expect(page.getByText("운영 권한 확인 대기", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "모의거래 일시정지" })).toHaveCount(0);
    await page.getByText("연결 상세", { exact: true }).click();
    await expect(page.getByText(/Data API 노출 설정과 스키마 캐시/)).toBeVisible();
    await expect(page.locator('[role="alert"]')).toHaveCount(1);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
    expect(captured.reviews).toHaveLength(0);
    expect(captured.incidents).toHaveLength(0);
  });

  test("one-time device connection restores locally and clears this device after revoke failure", async ({ page }) => {
    const captured = emptyCapturedRpc();
    const capturedAuth: CapturedAuthLifecycle = {
      tokenRequests: 0,
      userRequests: 0,
      logoutScopes: [],
      logoutFailuresRemaining: 1
    };
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, { authenticatedDevice: false });
    await mockAuthLifecycle(page, capturedAuth);
    await page.goto("/?page=settings");

    await page.getByRole("button", { name: "이 기기 연결", exact: true }).click();
    const connectionDrawer = page.getByRole("dialog", { name: "이 기기 연결" });
    await connectionDrawer.getByLabel("운영 계정 이메일").fill("operator@example.test");
    await connectionDrawer.getByLabel("비밀번호").fill("test-only-password");
    await connectionDrawer.getByRole("button", { name: "기기 연결", exact: true }).click();
    await expect(page.getByRole("heading", { name: "operator@example.test", level: 2 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "operator@example.test", level: 2 })).toBeFocused();
    await expect.poll(() => capturedAuth.tokenRequests).toBe(1);
    await expect.poll(() => capturedAuth.userRequests).toBeGreaterThan(0);
    expect(await page.evaluate(() => localStorage.getItem("sb-e2e-auth-token") !== null)).toBe(true);

    const tokenRequestsBeforeReload = capturedAuth.tokenRequests;
    await page.reload();
    await expect(page.getByRole("heading", { name: "operator@example.test", level: 2 })).toBeVisible();
    expect(capturedAuth.tokenRequests).toBe(tokenRequestsBeforeReload);

    await page.getByText("연결 정보 보기", { exact: true }).click();
    const disconnectButton = page.getByRole("button", { name: "이 기기 연결 해제", exact: true });
    await disconnectButton.click();
    const disconnectDialog = page.getByRole("dialog", { name: "이 기기 연결을 해제할까요?" });
    await expect(disconnectDialog.getByRole("button", { name: "취소" })).toBeFocused();
    await disconnectDialog.getByRole("button", { name: "연결 해제", exact: true }).click();
    await expect(page.getByRole("heading", { name: "이 기기는 아직 연결되지 않았습니다" })).toBeVisible();
    await expect(page.getByRole("button", { name: "이 기기 연결", exact: true })).toBeFocused();
    expect(capturedAuth.logoutScopes).toEqual(["local"]);
    expect(await page.evaluate(() => localStorage.getItem("sb-e2e-auth-token"))).toBeNull();
  });

  test("a settings chunk failure renders a Korean fail-closed boundary", async ({ page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured);
    await page.route(/\/src\/pages\/SettingsPage\.tsx(?:\?.*)?$/, (route) => route.abort());
    await page.goto("/?page=settings");

    await expect(page.getByRole("alert").filter({ hasText: "계정·보안 화면을 안전하게 열지 못했습니다" })).toBeVisible();
    await expect(page.getByRole("button", { name: "앱 새로고침" })).toBeVisible();
  });

  test("1440 and 960 layouts keep the safety rail readable without serious accessibility violations", async ({ page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured);
    await page.goto("/?page=control");

    for (const viewport of [
      { width: 1440, height: 900, columns: 6 },
      { width: 960, height: 640, columns: 3 }
    ]) {
      await page.setViewportSize(viewport);
      const rail = page.getByRole("region", { name: "운영 상태 레일" }).locator("dl");
      await expect(rail).toBeVisible();
      const columnCount = await rail.evaluate((element) =>
        getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/).length
      );
      expect(columnCount).toBe(viewport.columns);
      const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(horizontalOverflow).toBeLessThanOrEqual(1);
      const accessibility = await new AxeBuilder({ page }).analyze();
      expect(accessibility.violations.filter(
        (violation) => violation.impact === "critical" || violation.impact === "serious"
      )).toEqual([]);
    }
  });

  test("reduced motion, reduced transparency, and forced colors use opaque non-animated surfaces", async ({ context, page }) => {
    const captured = emptyCapturedRpc();
    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setEmulatedMedia", {
      media: "screen",
      features: [
        { name: "prefers-reduced-motion", value: "reduce" },
        { name: "prefers-reduced-transparency", value: "reduce" },
        { name: "forced-colors", value: "active" }
      ]
    });
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured);
    await page.goto("/?page=control");

    const fallback = await page.evaluate(() => {
      const header = document.querySelector(".app-header-glass");
      const button = document.querySelector("nav button");
      if (!(header instanceof HTMLElement) || !(button instanceof HTMLElement)) {
        throw new Error("fallback fixture elements are required");
      }
      const headerStyle = getComputedStyle(header);
      const buttonStyle = getComputedStyle(button);
      return {
        reducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
        reducedTransparency: matchMedia("(prefers-reduced-transparency: reduce)").matches,
        forcedColors: matchMedia("(forced-colors: active)").matches,
        backdropFilter: headerStyle.backdropFilter,
        webkitBackdropFilter: headerStyle.getPropertyValue("-webkit-backdrop-filter"),
        transitionDuration: buttonStyle.transitionDuration
      };
    });
    expect(fallback).toMatchObject({
      reducedMotion: true,
      reducedTransparency: true,
      forcedColors: true
    });
    expect([fallback.backdropFilter, fallback.webkitBackdropFilter]).not.toContain(expect.stringMatching(/blur/i));
    expect(Number.parseFloat(fallback.transitionDuration)).toBeLessThanOrEqual(0.00001);
    const accessibility = await new AxeBuilder({ page }).analyze();
    expect(accessibility.violations.filter(
      (violation) => violation.impact === "critical" || violation.impact === "serious"
    )).toEqual([]);
  });

  test("a 200 percent equivalent CSS viewport has no horizontal overflow", async ({ context, page }) => {
    const captured = emptyCapturedRpc();
    const cdp = await context.newCDPSession(page);
    await cdp.send("Emulation.setDeviceMetricsOverride", {
      width: 720,
      height: 450,
      deviceScaleFactor: 2,
      mobile: false,
      screenWidth: 1440,
      screenHeight: 900
    });
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured);
    await page.goto("/?page=control");

    const dimensions = await page.evaluate(() => ({
      width: window.innerWidth,
      dpr: window.devicePixelRatio,
      overflow: document.documentElement.scrollWidth - window.innerWidth
    }));
    expect(dimensions).toMatchObject({ width: 720, dpr: 2 });
    expect(dimensions.overflow).toBeLessThanOrEqual(1);
  });

  test("keeps repeated navigation responsive while rejecting sustained or severe long tasks", async ({ page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured);
    await page.goto("/?page=control");
    const supported = await page.evaluate(() => PerformanceObserver.supportedEntryTypes.includes("longtask"));
    expect(supported).toBe(true);
    const rounds: number[][] = [];

    for (let round = 0; round < 3; round += 1) {
      await page.evaluate(() => {
        const runtime = globalThis as typeof globalThis & {
          __uiLongTasks?: number[];
          __uiLongTaskObserver?: PerformanceObserver;
        };
        runtime.__uiLongTaskObserver?.disconnect();
        runtime.__uiLongTasks = [];
        runtime.__uiLongTaskObserver = new PerformanceObserver((list) => {
          runtime.__uiLongTasks?.push(...list.getEntries().map((entry) => entry.duration));
        });
        runtime.__uiLongTaskObserver.observe({ type: "longtask", buffered: false });
      });

      await page.evaluate(() => window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" }));
      await page.evaluate(() => window.scrollTo({ top: 0, behavior: "instant" }));
      await page.getByRole("button", { name: /전체 보기/ }).click();
      const queueDrawer = page.getByRole("dialog", { name: "지금 확인할 항목 전체" });
      await expect(queueDrawer).toBeVisible();
      await queueDrawer.getByRole("button", { name: "상세 닫기" }).click();
      await expect(queueDrawer).toBeHidden();
      await page.getByRole("button", { name: "계정·보안", exact: true }).click();
      await expect(page.getByRole("heading", { name: "계정·보안", level: 1 })).toBeFocused();
      await page.goBack();
      await expect(page.getByRole("heading", { name: "운영 제어", level: 1 })).toBeFocused();
      await page.evaluate(
        () =>
          new Promise<void>((resolve) => {
            requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
          })
      );

      const longTasks = await page.evaluate(() => {
        const runtime = globalThis as typeof globalThis & {
          __uiLongTasks?: number[];
          __uiLongTaskObserver?: PerformanceObserver;
        };
        const pendingEntries = runtime.__uiLongTaskObserver?.takeRecords() ?? [];
        runtime.__uiLongTasks?.push(...pendingEntries.map((entry) => entry.duration));
        runtime.__uiLongTaskObserver?.disconnect();
        return runtime.__uiLongTasks ?? [];
      });
      rounds.push(longTasks);
    }

    const sortedRoundMaxes = rounds
      .map((longTasks) => Math.max(0, ...longTasks))
      .sort((left, right) => left - right);
    const evidence = JSON.stringify({ rounds, sortedRoundMaxes });
    expect(sortedRoundMaxes[1], evidence).toBeLessThanOrEqual(50);
    expect(sortedRoundMaxes[2], evidence).toBeLessThan(200);
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

    await openReconciliationDrawer(page);
    const requestButton = page.getByRole("button", { name: "추가 본인 확인 후 조정 요청" });
    await expect(requestButton).toBeDisabled();
    const evidenceSha = "d".repeat(64);
    await page.getByLabel("증거 자료 위치").fill(`urn:sha256:${evidenceSha}`);
    await page.getByLabel("증거 SHA-256").fill(evidenceSha);
    await page.getByLabel("증거 수집 시각").fill(new Date(Date.now() - 60_000).toISOString());
    await page.getByLabel("확정된 최종 주문 상태").selectOption("canceled");
    await page.getByLabel("누락 체결 목록 (엄격한 JSON 배열)").fill("[]");
    await expect(requestButton).toBeEnabled();

    await requestButton.click();
    await confirmNativeDialog(page, "회계 조정 요청 확인", "요청 전송");
    await expect
      .poll(
        () => ({
          unknownGrants: captured.unknownGrants.length,
          unknownRequests: captured.unknownRequests.length
        }),
        {
          message: "unknown-resolution request RPCs should complete before the UI receipt",
          timeout: 15_000
        }
      )
      .toEqual({ unknownGrants: 1, unknownRequests: 1 });
    await expect(page.locator('[role="status"][aria-live="polite"]')).toContainText(
      "독립 승인, Worker 적용, 회계 반영 전에는 완료가 아닙니다",
      { timeout: 15_000 }
    );
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
    await expect(page.getByText("Worker 적용·회계 반영 확인")).toHaveCount(0);

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

    await openReconciliationDrawer(page);
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

    await openReconciliationDrawer(page);
    const approveButton = page.getByRole("button", { name: "증거 승인" });
    await expect(approveButton).toBeEnabled();
    await approveButton.click();
    await confirmNativeDialog(page, "회계 조정 승인 확인", "승인 전송");
    await expect.poll(() => captured.unknownGrants.length).toBe(1);
    await expect.poll(() => captured.unknownReviews.length).toBe(1);
    expect(captured.reviews).toHaveLength(0);
    expect(rpcArgument(captured.unknownGrants[0], "request_payload")).toMatchObject({
      schema_version: 2,
      bound_action: "review",
      bound_command_type: "close_unknown_execution",
      command_payload: { decision: "approve", reviewer_role: "risk_approver" }
    });
    await expect(page.getByText("승인됨 · Worker 적용 확인 대기")).toBeVisible();
    const timeline = page.getByRole("list", { name: "조정 요청·승인·Worker 적용·회계 반영 타임라인" });
    await expect(timeline.getByText(/Worker 적용 확인/)).toBeVisible();
    await expect(timeline.getByText("미확인 · 대기").first()).toBeVisible();
    await expect(page.getByText("Worker 적용·회계 반영 확인")).toHaveCount(0);
    await expect(page.locator('[role="status"][aria-live="polite"]')).toContainText(
      "Worker 적용과 최신 회계 반영을 계속 확인하세요"
    );
  });

  test("offline unknown-resolution request is never transmitted or replayed", async ({ context, page }) => {
    const captured = emptyCapturedRpc();
    await mockControlPlaneRealtime(page);
    await mockOperationsRpc(page, captured, {
      unknownSnapshot: makeCurrentUnknownResolutionSnapshot(makeUnknownResolutionSnapshot())
    });
    await page.goto("/?page=control");

    await openReconciliationDrawer(page);
    const evidenceSha = "d".repeat(64);
    await page.getByLabel("증거 자료 위치").fill(`urn:sha256:${evidenceSha}`);
    await page.getByLabel("증거 SHA-256").fill(evidenceSha);
    await page.getByLabel("증거 수집 시각").fill(new Date(Date.now() - 60_000).toISOString());
    await page.getByLabel("확정된 최종 주문 상태").selectOption("canceled");
    await page.getByLabel("누락 체결 목록 (엄격한 JSON 배열)").fill("[]");
    const requestButton = page.getByRole("button", { name: "추가 본인 확인 후 조정 요청" });
    await expect(requestButton).toBeEnabled();

    await context.setOffline(true);
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    await expect(requestButton).toBeDisabled();
    await expect(page.getByRole("alert").filter({ hasText: "오프라인 작업은 전송되지 않으며" })).toBeVisible();
    expect(captured.unknownGrants).toHaveLength(0);
    expect(captured.unknownRequests).toHaveLength(0);

    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));
    await expect(requestButton).toBeEnabled();
    await page.waitForTimeout(250);
    expect(captured.unknownGrants).toHaveLength(0);
    expect(captured.unknownRequests).toHaveLength(0);
  });

  test("session expiry closes volatile drawers and leaves no deferred mutation to replay", async ({ context, page }) => {
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

    await openReconciliationDrawer(page);
    const evidenceInput = page.getByLabel("증거 자료 위치");
    await evidenceInput.fill(`urn:sha256:${"d".repeat(64)}`);
    await expect(evidenceInput).toHaveValue(/urn:sha256:/);

    await context.setOffline(true);
    await page.evaluate(() => window.dispatchEvent(new Event("offline")));
    operationsSnapshot.access.signed_in = false;
    operationsSnapshot.access.actor = null;
    operationsSnapshot.access.session_state = "expired";
    operationsSnapshot.access.assurance_level = "aal1";
    operationsSnapshot.access.active_step_up_grants = [];
    operationsSnapshot.access.permissions = [];
    operationsSnapshot.runtime_health.overall_state = "session_expired";
    await context.setOffline(false);
    await page.evaluate(() => window.dispatchEvent(new Event("online")));

    await expect(page.locator('dialog[data-variant="drawer"]')).toHaveCount(0);
    await expect(evidenceInput).toHaveCount(0);
    expect(captured.grants).toHaveLength(0);
    expect(captured.commands).toHaveLength(0);
    expect(captured.unknownGrants).toHaveLength(0);
    expect(captured.unknownRequests).toHaveLength(0);
    expect(await page.evaluate(() => localStorage.getItem("sb-e2e-auth-token"))).toBeNull();

    await page.getByRole("button", { name: "계정·보안", exact: true }).click();
    await expect(page.getByRole("heading", { name: "이 기기는 아직 연결되지 않았습니다" })).toBeVisible();
  });
});

async function mockControlPlaneRealtime(
  page: Page,
  { sendSignal = true }: { readonly sendSignal?: boolean } = {}
): Promise<void> {
  let signalVersion = 0;
  await page.routeWebSocket(/e2e\.supabase\.co\/realtime\/v1\/websocket/, (socket) => {
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
  const authFixture = options.authenticatedDevice === false ? null : makeMockAuthFixture();
  if (authFixture !== null) {
    await page.addInitScript(
      ({ storageKey, session }) => localStorage.setItem(storageKey, JSON.stringify(session)),
      { storageKey: "sb-e2e-auth-token", session: authFixture.session }
    );
  }
  await page.route("https://e2e.supabase.co/**", async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") {
      await fulfillPreflight(route);
      return;
    }
    const path = new URL(request.url()).pathname;
    if (path === "/auth/v1/user" && authFixture !== null) {
      await fulfillJson(route, authFixture.user);
      return;
    }
    if (path === "/rest/v1/rpc/get_desktop_operations_snapshot_v1") {
      if (options.snapshotRpcError) {
        await route.fulfill({
          status: options.snapshotRpcError.status,
          contentType: "application/json",
          headers: corsHeaders,
          body: JSON.stringify(options.snapshotRpcError.body)
        });
        return;
      }
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
      options.afterStepUpGrant?.(snapshot);
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

async function mockAuthLifecycle(page: Page, captured: CapturedAuthLifecycle): Promise<void> {
  const { accessToken, nowSeconds, user } = makeMockAuthFixture();

  await page.route("https://e2e.supabase.co/auth/v1/**", async (route) => {
    const request = route.request();
    if (request.method() === "OPTIONS") {
      await fulfillPreflight(route);
      return;
    }
    const url = new URL(request.url());
    if (url.pathname === "/auth/v1/token") {
      captured.tokenRequests += 1;
      await fulfillJson(route, {
        access_token: accessToken,
        token_type: "bearer",
        expires_in: 3600,
        expires_at: nowSeconds + 3600,
        refresh_token: "e2e-refresh-token",
        user
      });
      return;
    }
    if (url.pathname === "/auth/v1/user") {
      captured.userRequests += 1;
      await fulfillJson(route, user);
      return;
    }
    if (url.pathname === "/auth/v1/logout") {
      captured.logoutScopes.push(url.searchParams.get("scope"));
      if (captured.logoutFailuresRemaining > 0) {
        captured.logoutFailuresRemaining -= 1;
        await route.fulfill({
          status: 503,
          contentType: "application/json",
          headers: corsHeaders,
          body: JSON.stringify({ message: "synthetic revoke failure" })
        });
        return;
      }
      await fulfillJson(route, {});
      return;
    }
    await route.fallback();
  });
}

function makeMockAuthFixture() {
  const nowSeconds = Math.floor(Date.now() / 1000);
  const user = {
    id: "71717171-7171-4171-8171-717171717171",
    aud: "authenticated",
    role: "authenticated",
    email: "operator@example.test",
    email_confirmed_at: new Date().toISOString(),
    phone: "",
    confirmed_at: new Date().toISOString(),
    last_sign_in_at: new Date().toISOString(),
    app_metadata: { provider: "email", providers: ["email"] },
    user_metadata: {},
    identities: [],
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    is_anonymous: false
  };
  const accessToken = makeUnsignedJwt({
    sub: user.id,
    aud: "authenticated",
    role: "authenticated",
    email: user.email,
    aal: "aal1",
    session_id: "72727272-7272-4272-8272-727272727272",
    iat: nowSeconds,
    exp: nowSeconds + 3600
  });
  return {
    nowSeconds,
    user,
    accessToken,
    session: {
      access_token: accessToken,
      token_type: "bearer",
      expires_in: 3600,
      expires_at: nowSeconds + 3600,
      refresh_token: "e2e-refresh-token",
      user
    }
  };
}

function makeUnsignedJwt(payload: Record<string, unknown>): string {
  const encode = (value: Record<string, unknown>) => Buffer
    .from(JSON.stringify(value))
    .toString("base64url");
  return `${encode({ alg: "HS256", typ: "JWT" })}.${encode(payload)}.e2e-signature`;
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

async function confirmNativeDialog(page: Page, title: string, confirmLabel: string): Promise<void> {
  const dialog = page.getByRole("dialog", { name: title });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: confirmLabel, exact: true }).press("Enter");
  await expect(dialog).toHaveCount(0);
}

async function openReconciliationDrawer(page: Page): Promise<void> {
  await page.getByRole("button", { name: "대사 상세" }).first().click();
  await expect(page.getByRole("dialog", { name: "수동 대사 상세" })).toBeVisible();
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
