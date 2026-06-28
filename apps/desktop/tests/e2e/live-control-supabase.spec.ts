import { expect, test, type Page, type Request, type Route } from "@playwright/test";

const reviewerUserId = "00000000-0000-4000-8000-000000000002";
const requesterUserId = "00000000-0000-4000-8000-000000000001";
const pendingCommandId = "cmd-pending";
const nowIso = "2026-06-28T00:00:00.000Z";

interface CapturedRequests {
  readonly liveRequests: Array<Record<string, unknown>>;
  readonly reviews: Array<Record<string, unknown>>;
  readonly botSettingsUpdates: Array<Record<string, unknown>>;
}

test.describe("live control Supabase network fixture", () => {
  for (const viewport of [
    { name: "desktop", width: 1280, height: 720 },
    { name: "mobile", width: 390, height: 844 }
  ]) {
    test(`${viewport.name} blocks live activation without fresh approval and sends gated payloads`, async ({
      page
    }) => {
      const captured: CapturedRequests = {
        liveRequests: [],
        reviews: [],
        botSettingsUpdates: []
      };
      await page.setViewportSize(viewport);
      await installSupabaseSession(page);
      await mockSupabaseApi(page, captured);

      const errors: string[] = [];
      page.on("dialog", async (dialog) => {
        await dialog.accept();
      });
      page.on("console", (message) => {
        if (message.type() === "error" || message.type() === "warning") {
          errors.push(message.text());
        }
      });
      page.on("pageerror", (error) => errors.push(error.message));

      await page.goto("/?page=control");
      await expect(page).toHaveTitle("KR Auto Trading Lab");
      await expect(page.getByText("Live 승인 게이트")).toBeVisible();
      await expect(page.getByText("fresh 승인")).toBeVisible();
      await expect(page.getByRole("button", { name: "실주문 허용 활성화" })).toBeDisabled();

      await page.getByLabel("실주문 확인 문구").fill("실주문 위험을 이해했습니다");
      await expect(page.getByRole("button", { name: "실주문 허용 활성화" })).toBeDisabled();

      await page.getByLabel("risk report id").fill("risk-2026-06-28");
      await page.getByLabel("release version").fill("release-2");
      await page.getByLabel("만료 분").fill("45");
      await page.getByRole("button", { name: "Live 승인 요청" }).click();
      await expect.poll(() => captured.liveRequests.length).toBe(1);
      expect(captured.liveRequests[0]).toMatchObject({
        command_type: "request_live_enable",
        requested_by: reviewerUserId,
        status: "pending"
      });
      expect(captured.liveRequests[0].payload).toMatchObject({
        provider_contract_version: "toss-openapi-1.1.5",
        risk_report_id: "risk-2026-06-28",
        release_version: "release-2"
      });

      await page.getByRole("button", { name: "승인", exact: true }).click();
      await expect.poll(() => captured.reviews.length).toBe(1);
      expect(captured.reviews[0]).toMatchObject({
        status: "accepted",
        reviewed_by: reviewerUserId
      });

      await expect(page.getByText("있음")).toBeVisible();
      await page.getByRole("button", { name: "실주문 허용 활성화" }).click();
      await expect.poll(() => captured.botSettingsUpdates.length).toBe(1);
      expect(captured.botSettingsUpdates[0]).toMatchObject({
        enabled: true,
        mode: "live",
        live_order_allowed: true
      });
      expect(errors).toEqual([]);
    });
  }
});

async function installSupabaseSession(page: Page): Promise<void> {
  await page.addInitScript(
    ({ userId }) => {
      const issuedAt = Math.floor(Date.now() / 1000);
      window.localStorage.setItem(
        "sb-e2e-auth-token",
        JSON.stringify({
          access_token: "e2e-access-token",
          refresh_token: "e2e-refresh-token",
          token_type: "bearer",
          expires_in: 3600,
          expires_at: issuedAt + 3600,
          user: {
            id: userId,
            aud: "authenticated",
            role: "authenticated",
            email: "reviewer@example.invalid",
            email_confirmed_at: new Date(issuedAt * 1000).toISOString(),
            confirmed_at: new Date(issuedAt * 1000).toISOString(),
            app_metadata: { provider: "email", providers: ["email"] },
            user_metadata: {},
            identities: [],
            created_at: new Date(issuedAt * 1000).toISOString(),
            updated_at: new Date(issuedAt * 1000).toISOString(),
            is_anonymous: false
          }
        })
      );
    },
    { userId: reviewerUserId }
  );
}

function authSession(userId: string): Record<string, unknown> {
  const issuedAt = Math.floor(Date.now() / 1000);
  return {
    access_token: "e2e-access-token",
    refresh_token: "e2e-refresh-token",
    token_type: "bearer",
    expires_in: 3600,
    expires_at: issuedAt + 3600,
    user: {
      id: userId,
      aud: "authenticated",
      role: "authenticated",
      email: "reviewer@example.invalid",
      email_confirmed_at: new Date(issuedAt * 1000).toISOString(),
      confirmed_at: new Date(issuedAt * 1000).toISOString(),
      app_metadata: { provider: "email", providers: ["email"] },
      user_metadata: {},
      identities: [],
      created_at: new Date(issuedAt * 1000).toISOString(),
      updated_at: new Date(issuedAt * 1000).toISOString(),
      is_anonymous: false
    }
  };
}

async function mockSupabaseApi(page: Page, captured: CapturedRequests): Promise<void> {
  let liveCommands = [pendingCommand()];
  await page.route("https://e2e.supabase.test/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/auth/v1/user") {
      await fulfillJson(route, {
        id: reviewerUserId,
        aud: "authenticated",
        role: "authenticated",
        email: "reviewer@example.invalid",
        app_metadata: {},
        user_metadata: {}
      });
      return;
    }
    if (url.pathname === "/auth/v1/token") {
      await fulfillJson(route, authSession(reviewerUserId));
      return;
    }
    if (url.pathname === "/rest/v1/user_roles" && request.method() === "GET") {
      await fulfillJson(route, [{ role: "admin" }]);
      return;
    }
    if (url.pathname !== "/rest/v1/bot_settings" && url.pathname !== "/rest/v1/manual_commands") {
      if (url.pathname.startsWith("/rest/v1/") && request.method() === "GET") {
        await fulfillJson(route, []);
        return;
      }
      await route.fulfill({ status: 404, body: "unmocked" });
      return;
    }
    await handleRestRequest(route, request, url, captured, {
      getLiveCommands: () => liveCommands,
      setLiveCommands: (nextRows) => {
        liveCommands = nextRows;
      }
    });
  });
}

async function handleRestRequest(
  route: Route,
  request: Request,
  url: URL,
  captured: CapturedRequests,
  commands: {
    readonly getLiveCommands: () => Array<Record<string, unknown>>;
    readonly setLiveCommands: (rows: Array<Record<string, unknown>>) => void;
  }
): Promise<void> {
  if (url.pathname === "/rest/v1/bot_settings") {
    if (request.method() === "GET") {
      await fulfillJson(route, [botSettings()]);
      return;
    }
    if (request.method() === "PATCH") {
      captured.botSettingsUpdates.push(await request.postDataJSON());
      commands.setLiveCommands(commands.getLiveCommands().map((row) => (
        row.status === "accepted"
          ? { ...row, status: "applied", applied_at: new Date().toISOString() }
          : row
      )));
      await fulfillJson(route, []);
      return;
    }
  }
  if (url.pathname === "/rest/v1/manual_commands") {
    if (request.method() === "GET") {
      await fulfillJson(route, commands.getLiveCommands());
      return;
    }
    if (request.method() === "POST") {
      const payload = await request.postDataJSON();
      captured.liveRequests.push(payload);
      await fulfillJson(route, []);
      return;
    }
    if (request.method() === "PATCH") {
      const payload = await request.postDataJSON();
      captured.reviews.push(payload);
      commands.setLiveCommands(commands.getLiveCommands().map((row) => (
        row.id === pendingCommandId
          ? {
              ...row,
              status: payload.status,
              reviewed_by: payload.reviewed_by,
              reviewed_at: payload.reviewed_at
            }
          : row
      )));
      await fulfillJson(route, [{ id: pendingCommandId }]);
      return;
    }
  }
  await route.fulfill({ status: 405, body: "unmocked method" });
}

async function fulfillJson(route: Route, body: unknown): Promise<void> {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify(body)
  });
}

function botSettings(): Record<string, unknown> {
  return {
    id: "singleton",
    enabled: false,
    mode: "paper",
    live_order_allowed: false,
    max_order_amount_krw: 100000,
    max_daily_loss_pct: 0.02,
    max_daily_order_count: 10,
    max_position_pct: 0.1,
    max_sector_pct: 0.3,
    loop_interval_sec: 30,
    updated_at: nowIso
  };
}

function pendingCommand(): Record<string, unknown> {
  return {
    id: pendingCommandId,
    command_type: "request_live_enable",
    status: "pending",
    payload: {
      provider_contract_version: "toss-openapi-1.1.5",
      risk_report_id: "risk-2026-06-28",
      release_version: "release-1"
    },
    requested_by: requesterUserId,
    reviewed_by: null,
    rejection_reason: null,
    expires_at: new Date(Date.now() + 45 * 60_000).toISOString(),
    reviewed_at: null,
    created_at: nowIso,
    applied_at: null
  };
}
