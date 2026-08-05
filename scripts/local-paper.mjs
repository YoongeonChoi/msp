import { createHmac, createHash, randomBytes, randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createClient } from "@supabase/supabase-js";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(SCRIPT_DIR, "..");
if (process.platform !== "win32" || !process.env.LOCALAPPDATA) {
  throw new Error(
    "Local PAPER bootstrap requires Windows LOCALAPPDATA for protected state.",
  );
}
const LOCAL_STATE_DIR = resolve(
  process.env.LOCALAPPDATA,
  "kr-auto-trading-lab",
);
const CREDENTIALS_PATH = resolve(
  LOCAL_STATE_DIR,
  "local-paper-credentials.json",
);
const OPENING_EVIDENCE_PATH = resolve(
  LOCAL_STATE_DIR,
  "account-opening-evidence.json",
);
const WORKER_ENV_PATH = resolve(ROOT, "apps", "worker", ".env");
// Vite loads .env.<mode>.local after the generic .env.local file. Keeping the
// local PAPER endpoint in this mode-specific file prevents an existing hosted
// developer configuration from winning when the local flavor is launched.
const DESKTOP_ENV_PATH = resolve(
  ROOT,
  "apps",
  "desktop",
  ".env.local-paper.local",
);
const DB_CONTAINER = "supabase_db_kr-auto-trading-lab";
const SUPABASE_CLI_VERSION = "2.109.1";

const command = process.argv[2] ?? "provision";

function info(message) {
  process.stdout.write(`${message}\n`);
}

function fail(message, cause) {
  const error = new Error(message);
  if (cause !== undefined) {
    error.cause = cause;
  }
  throw error;
}

function run(file, args, options = {}) {
  return execFileSync(file, args, {
    cwd: ROOT,
    encoding: "utf8",
    maxBuffer: 16 * 1024 * 1024,
    ...options,
  });
}

let currentUserSid = null;

function getCurrentUserSid() {
  if (currentUserSid !== null) {
    return currentUserSid;
  }
  const output = run("whoami.exe", ["/user", "/fo", "csv", "/nh"], {
    stdio: ["ignore", "pipe", "pipe"],
  });
  const match = /"(S-1(?:-\d+)+)"/u.exec(output);
  if (!match) {
    fail(
      "Could not resolve the current Windows user SID for local secret ACLs.",
    );
  }
  currentUserSid = match[1];
  return currentUserSid;
}

function restrictAcl(path, { directory = false } = {}) {
  const permission = directory ? "(OI)(CI)F" : "F";
  try {
    run(
      "icacls.exe",
      [
        path,
        "/inheritance:r",
        "/grant:r",
        `*${getCurrentUserSid()}:${permission}`,
        `*S-1-5-18:${permission}`,
        `*S-1-5-32-544:${permission}`,
        "/q",
      ],
      { stdio: ["ignore", "pipe", "pipe"] },
    );
  } catch (error) {
    fail(`Could not restrict Windows ACLs for ${path}.`, error);
  }
}

function ensureProtectedStateDirectory() {
  mkdirSync(LOCAL_STATE_DIR, { recursive: true });
  restrictAcl(LOCAL_STATE_DIR, { directory: true });
  for (const protectedPath of [CREDENTIALS_PATH, OPENING_EVIDENCE_PATH]) {
    if (existsSync(protectedPath)) {
      restrictAcl(protectedPath);
    }
  }
}

function loadSupabaseStatus() {
  const npx = process.platform === "win32" ? "cmd.exe" : "npx";
  const npxArgs =
    process.platform === "win32"
      ? [
          "/d",
          "/s",
          "/c",
          `npx.cmd --yes supabase@${SUPABASE_CLI_VERSION} status -o env`,
        ]
      : ["--yes", `supabase@${SUPABASE_CLI_VERSION}`, "status", "-o", "env"];
  let output;
  try {
    output = run(npx, npxArgs, {
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    // Supabase CLI returns a non-zero status when an optional local service is
    // degraded (for example, the Windows analytics collector), while still
    // returning a complete and usable local API/DB environment on stdout.
    const partialOutput = error?.stdout?.toString?.() ?? "";
    if (!partialOutput.includes("API_URL=")) {
      fail(
        "Local Supabase is not running. Run scripts/bootstrap-local-paper.ps1 first.",
        error,
      );
    }
    output = partialOutput;
  }

  const values = new Map();
  for (const rawLine of output.split(/\r?\n/u)) {
    const line = rawLine.trim();
    const match = /^([A-Z_]+)=(.*)$/u.exec(line);
    if (!match) {
      continue;
    }
    let value = match[2].trim();
    if (value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1);
    }
    values.set(match[1], value);
  }

  const apiUrl = values.get("API_URL");
  const publishableKey =
    values.get("PUBLISHABLE_KEY") ?? values.get("ANON_KEY");
  const secretKey = values.get("SECRET_KEY") ?? values.get("SERVICE_ROLE_KEY");
  if (!apiUrl || !publishableKey || !secretKey) {
    fail(
      "Supabase status is missing API_URL, publishable key, or worker secret.",
    );
  }
  return { apiUrl, publishableKey, secretKey };
}

function readState() {
  if (!existsSync(CREDENTIALS_PATH)) {
    return null;
  }
  const parsed = JSON.parse(readFileSync(CREDENTIALS_PATH, "utf8"));
  if (
    parsed.schemaVersion !== 1 ||
    parsed.projectId !== "kr-auto-trading-lab"
  ) {
    fail(`Unsupported local credential state at ${CREDENTIALS_PATH}.`);
  }
  return parsed;
}

function writeState(state) {
  ensureProtectedStateDirectory();
  const temporary = `${CREDENTIALS_PATH}.${process.pid}.tmp`;
  writeFileSync(temporary, `${JSON.stringify(state, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  restrictAcl(temporary);
  renameSync(temporary, CREDENTIALS_PATH);
  restrictAcl(CREDENTIALS_PATH);
}

function newState() {
  return {
    schemaVersion: 1,
    projectId: "kr-auto-trading-lab",
    createdAt: new Date().toISOString(),
    workerId: randomUUID(),
    operator: {
      id: randomUUID(),
      email: "operator.local@example.test",
      password: `Aa1!${randomBytes(24).toString("base64url")}`,
      factorId: null,
      totpSecret: null,
      totpUri: null,
    },
    risk: {
      id: randomUUID(),
      email: "risk.local@example.test",
      password: `Aa1!${randomBytes(24).toString("base64url")}`,
      factorId: null,
      totpSecret: null,
      totpUri: null,
    },
    openingEvidenceId: null,
  };
}

function makeClient(url, key) {
  return createClient(url, key, {
    auth: {
      persistSession: false,
      autoRefreshToken: false,
      detectSessionInUrl: false,
    },
  });
}

async function listAllUsers(adminClient) {
  const users = [];
  for (let page = 1; ; page += 1) {
    const { data, error } = await adminClient.auth.admin.listUsers({
      page,
      perPage: 100,
    });
    if (error) {
      fail("Could not list local Supabase Auth users.", error);
    }
    users.push(...data.users);
    if (data.users.length < 100) {
      return users;
    }
  }
}

async function ensureAuthUser(
  adminClient,
  account,
  preexistingUsers,
  stateWasPresent,
) {
  const byEmail = preexistingUsers.find(
    (user) => user.email?.toLowerCase() === account.email,
  );
  const byId = preexistingUsers.find((user) => user.id === account.id);
  if (byEmail && byEmail.id !== account.id) {
    fail(
      `Auth user ${account.email} exists with an unexpected id; refusing account adoption.`,
    );
  }
  if (byId && byId.email?.toLowerCase() !== account.email) {
    fail(
      `Auth user ${account.id} exists with an unexpected email; refusing account adoption.`,
    );
  }
  if (byEmail || byId) {
    if (!stateWasPresent) {
      fail(
        `Auth user ${account.email} exists but the protected credential state is missing.`,
      );
    }
    return;
  }

  const { error } = await adminClient.auth.admin.createUser({
    id: account.id,
    email: account.email,
    password: account.password,
    email_confirm: true,
  });
  if (error) {
    fail(`Could not create local Auth user ${account.email}.`, error);
  }
}

function decodeBase32(value) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const normalized = value
    .toUpperCase()
    .replace(/=+$/u, "")
    .replace(/\s+/gu, "");
  let bits = "";
  for (const character of normalized) {
    const index = alphabet.indexOf(character);
    if (index < 0) {
      fail("TOTP secret contains an invalid Base32 character.");
    }
    bits += index.toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let offset = 0; offset + 8 <= bits.length; offset += 8) {
    bytes.push(Number.parseInt(bits.slice(offset, offset + 8), 2));
  }
  return Buffer.from(bytes);
}

function totpCode(secret, at = Date.now()) {
  const counter = BigInt(Math.floor(at / 30_000));
  const input = Buffer.alloc(8);
  input.writeBigUInt64BE(counter);
  const digest = createHmac("sha1", decodeBase32(secret))
    .update(input)
    .digest();
  const offset = digest[digest.length - 1] & 0x0f;
  const value =
    (((digest[offset] & 0x7f) << 24) |
      (digest[offset + 1] << 16) |
      (digest[offset + 2] << 8) |
      digest[offset + 3]) %
    1_000_000;
  return value.toString().padStart(6, "0");
}

function validateTotpEnrollment(totp, expectedSecret) {
  const uri = new URL(totp.uri);
  const secret = uri.searchParams.get("secret");
  const algorithm = uri.searchParams.get("algorithm") ?? "SHA1";
  const digits = uri.searchParams.get("digits") ?? "6";
  const period = uri.searchParams.get("period") ?? "30";
  if (
    uri.protocol !== "otpauth:" ||
    uri.hostname !== "totp" ||
    secret !== expectedSecret ||
    algorithm.toUpperCase() !== "SHA1" ||
    digits !== "6" ||
    period !== "30"
  ) {
    fail("Supabase returned an unexpected TOTP enrollment contract.");
  }
}

async function signInAndVerifyMfa(url, publishableKey, account, saveState) {
  const client = makeClient(url, publishableKey);
  const signIn = await client.auth.signInWithPassword({
    email: account.email,
    password: account.password,
  });
  if (signIn.error || !signIn.data.session) {
    fail(`Could not sign in local Auth user ${account.email}.`, signIn.error);
  }

  const factorsResult = await client.auth.mfa.listFactors();
  if (factorsResult.error) {
    fail(
      `Could not list MFA factors for ${account.email}.`,
      factorsResult.error,
    );
  }
  const totpFactors = factorsResult.data.totp ?? [];
  let factor = account.factorId
    ? totpFactors.find((candidate) => candidate.id === account.factorId)
    : undefined;

  if (account.factorId && (!account.totpSecret || !factor)) {
    fail(
      `Stored MFA state for ${account.email} no longer matches Supabase Auth.`,
    );
  }
  if (
    !account.factorId &&
    totpFactors.some((candidate) => candidate.status === "verified")
  ) {
    fail(
      `Verified MFA exists for ${account.email}, but its protected TOTP state is missing.`,
    );
  }

  if (!factor) {
    const enrollment = await client.auth.mfa.enroll({
      factorType: "totp",
      friendlyName: account.email.startsWith("operator")
        ? "Local PAPER Operator"
        : "Local PAPER Risk Approver",
      issuer: "KR Auto Trading Lab Local PAPER",
    });
    if (enrollment.error || !enrollment.data.totp) {
      fail(`Could not enroll TOTP for ${account.email}.`, enrollment.error);
    }
    validateTotpEnrollment(enrollment.data.totp, enrollment.data.totp.secret);
    account.factorId = enrollment.data.id;
    account.totpSecret = enrollment.data.totp.secret;
    account.totpUri = enrollment.data.totp.uri;
    saveState();
    factor = { id: enrollment.data.id, status: "unverified" };
  }

  const challenge = await client.auth.mfa.challenge({ factorId: factor.id });
  if (challenge.error || !challenge.data.id) {
    fail(
      `Could not create an MFA challenge for ${account.email}.`,
      challenge.error,
    );
  }
  const verification = await client.auth.mfa.verify({
    factorId: factor.id,
    challengeId: challenge.data.id,
    code: totpCode(account.totpSecret),
  });
  if (verification.error) {
    fail(`Could not verify TOTP for ${account.email}.`, verification.error);
  }

  const assurance = await client.auth.mfa.getAuthenticatorAssuranceLevel();
  if (
    assurance.error ||
    assurance.data.currentLevel !== "aal2" ||
    assurance.data.nextLevel !== "aal2"
  ) {
    fail(`AAL2 was not established for ${account.email}.`, assurance.error);
  }
  assertFreshAal2Session(await currentSession(client), account.id);
  return client;
}

async function currentSession(client) {
  const { data, error } = await client.auth.getSession();
  if (error || !data.session) {
    fail("An authenticated Supabase session is required.", error);
  }
  return data.session;
}

function decodeJwtPayload(token) {
  const parts = token.split(".");
  if (parts.length !== 3) {
    fail("Supabase returned a malformed access token.");
  }
  return JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8"));
}

function assertFreshAal2Session(session, expectedUserId) {
  const payload = decodeJwtPayload(session.access_token);
  const now = Math.floor(Date.now() / 1000);
  const totp = Array.isArray(payload.amr)
    ? payload.amr.find((entry) => entry?.method === "totp")
    : undefined;
  if (
    payload.sub !== expectedUserId ||
    payload.role !== "authenticated" ||
    payload.aal !== "aal2" ||
    typeof payload.session_id !== "string" ||
    !totp ||
    typeof totp.timestamp !== "number" ||
    now - totp.timestamp > 300 ||
    totp.timestamp > now + 30
  ) {
    fail("Supabase session did not satisfy the recent AAL2/TOTP contract.");
  }
  return payload;
}

function sqlString(value) {
  return `'${String(value).replaceAll("'", "''")}'`;
}

function psql(sql, { capture = true } = {}) {
  return run(
    "docker",
    [
      "exec",
      "-i",
      DB_CONTAINER,
      "psql",
      "-X",
      "-U",
      "supabase_admin",
      "-d",
      "postgres",
      "-v",
      "ON_ERROR_STOP=1",
      "-At",
    ],
    {
      input: sql,
      stdio: ["pipe", capture ? "pipe" : "ignore", "pipe"],
    },
  );
}

function ensureRoles(state) {
  psql(
    `begin;
insert into private.role_assignments (user_id, role, reason)
values
  (${sqlString(state.operator.id)}::uuid, 'operator', 'local_paper_bootstrap'),
  (${sqlString(state.operator.id)}::uuid, 'release_manager', 'local_paper_bootstrap'),
  (${sqlString(state.risk.id)}::uuid, 'risk_approver', 'local_paper_bootstrap'),
  (${sqlString(state.risk.id)}::uuid, 'release_manager', 'local_paper_bootstrap')
on conflict (user_id, role) where revoked_at is null do nothing;
commit;
`,
    { capture: false },
  );
}

function writeOpeningEvidenceArtifact(state, releaseSha) {
  const evidence = {
    schema_version: 1,
    environment: "paper",
    account_id: "paper-primary",
    opening_capital_krw: 10_000_000,
    release_sha: releaseSha,
    operator_user_id: state.operator.id,
    risk_approver_user_id: state.risk.id,
    production_order_network_requests: 0,
    live_order_execution_enabled: false,
    captured_at: new Date().toISOString(),
  };
  ensureProtectedStateDirectory();
  writeFileSync(
    OPENING_EVIDENCE_PATH,
    `${JSON.stringify(evidence, null, 2)}\n`,
    {
      encoding: "utf8",
      mode: 0o600,
    },
  );
  restrictAcl(OPENING_EVIDENCE_PATH);
  const bytes = readFileSync(OPENING_EVIDENCE_PATH);
  return {
    capturedAt: evidence.captured_at,
    sha256: createHash("sha256").update(bytes).digest("hex"),
  };
}

async function ensureOpeningEvidence(state, operatorClient, releaseSha) {
  if (state.openingEvidenceId) {
    const exists = psql(
      `select exists(select 1 from private.control_evidence where id=${sqlString(state.openingEvidenceId)}::uuid and evidence_type='account_opening');`,
    ).trim();
    if (exists === "t") {
      return;
    }
    state.openingEvidenceId = null;
  }

  const session = await currentSession(operatorClient);
  const claims = assertFreshAal2Session(session, state.operator.id);
  const artifact = writeOpeningEvidenceArtifact(state, releaseSha);
  const output = psql(`begin;
set local request.jwt.claims = ${sqlString(JSON.stringify(claims))};
select evidence_id::text
from private.register_control_evidence(
  'account_opening',
  'paper',
  ${sqlString(`urn:sha256:${artifact.sha256}`)},
  ${sqlString(artifact.sha256)},
  ${sqlString(artifact.capturedAt)}::timestamptz,
  jsonb_build_object(
    'schema_version', 1,
    'account_id', 'paper-primary',
    'release_sha', ${sqlString(releaseSha)},
    'local_only', true,
    'production_order_network_requests', 0
  )
);
commit;
`);
  const evidenceId = output.match(
    /[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/iu,
  )?.[0];
  if (!evidenceId) {
    fail("Local account-opening evidence was not registered.");
  }
  state.openingEvidenceId = evidenceId;
}

function parseUniqueEnvironment(content) {
  const values = new Map();
  for (const rawLine of content.split(/\r?\n/u)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) {
      continue;
    }
    const separator = line.indexOf("=");
    if (separator < 1) {
      return null;
    }
    const key = line.slice(0, separator).trim();
    if (!/^[A-Z][A-Z0-9_]*$/u.test(key) || values.has(key)) {
      return null;
    }
    values.set(key, line.slice(separator + 1));
  }
  return values;
}

function isManagedLocalEnvironment(content, kind) {
  const values = parseUniqueEnvironment(content);
  if (values === null) {
    return false;
  }
  if (kind === "worker") {
    return (
      values.get("ENV") === "local" &&
      values.get("SUPABASE_URL") === "http://127.0.0.1:54321" &&
      values.get("MOCK_PROVIDERS") === "true" &&
      values.get("BOT_DEFAULT_MODE") === "paper" &&
      values.get("LIVE_ORDER_EXECUTION_ENABLED") === "false" &&
      values.get("TOSS_ORDER_ENDPOINT_ENABLED") === "false" &&
      values.get("TOSS_ORDER_CAPABLE_CREDENTIALS") === "false" &&
      values.get("EXECUTION_V2_ENVIRONMENT") === "paper"
    );
  }
  return (
    values.get("VITE_SUPABASE_URL") === "http://127.0.0.1:54321" &&
    /^sb_publishable_[A-Za-z0-9_-]+$/u.test(
      values.get("VITE_SUPABASE_PUBLISHABLE_KEY") ?? "",
    )
  );
}

function writeEnvFile(path, content, kind) {
  if (existsSync(path)) {
    const existing = readFileSync(path, "utf8");
    if (existing !== content && !isManagedLocalEnvironment(existing, kind)) {
      fail(
        `Refusing to overwrite non-local ${kind} env at ${path}. Move it explicitly and rerun bootstrap.`,
      );
    }
    restrictAcl(path);
  }
  writeFileSync(path, content, { encoding: "utf8", mode: 0o600 });
  restrictAcl(path);
}

function writeRuntimeEnvironment(status, state, releaseSha) {
  const workerEnv = `ENV=local
TZ=Asia/Seoul
MOCK_PROVIDERS=true
USE_SUPABASE_REPOSITORY=true
RUN_ONCE=false
BOT_DEFAULT_MODE=paper
LOOP_INTERVAL_SEC=30
HEARTBEAT_INTERVAL_SEC=30
MAX_CONCURRENT_API_CALLS=5
SUPABASE_URL=${status.apiUrl}
SUPABASE_SECRET_KEY=${status.secretKey}
TOSS_CLIENT_ID=
TOSS_CLIENT_SECRET=
TOSS_ACCOUNT_ID=
TOSS_CREDENTIAL_SCOPE=read_only
TOSS_ORDER_CAPABLE_CREDENTIALS=false
KR_CALENDAR_COLLECTION_ASSESSMENT_ENABLED=false
KR_CALENDAR_COLLECTION_MANUAL_EXECUTION_ENABLED=false
KR_CALENDAR_COLLECTION_HOLDER_ID=
LIVE_ORDER_EXECUTION_ENABLED=false
TOSS_ORDER_ENDPOINT_ENABLED=false
EXECUTION_V2_ENABLED=true
EXECUTION_V2_ENVIRONMENT=paper
EXECUTION_V2_WORKER_API_ENABLED=true
EXECUTION_V2_PAPER_RESUME_INPUT_ENABLED=false
EXECUTION_V2_PAPER_SOURCE_INPUT_ENABLED=false
EXECUTION_V2_WORKER_ID=${state.workerId}
EXECUTION_V2_ACCOUNT_ID=paper-primary
WORKER_LEASE_TTL_SEC=30
WORKER_LEASE_RENEW_INTERVAL_SEC=10
OPERATIONS_COMMAND_INTERVAL_SEC=2
OPERATIONS_EXECUTION_INTERVAL_SEC=5
OPERATIONS_SETTLEMENT_INTERVAL_SEC=10
OPERATIONS_RECONCILIATION_INTERVAL_SEC=10
OPERATIONS_OUTBOX_INTERVAL_SEC=1
OPERATIONS_HEARTBEAT_INTERVAL_SEC=5
DEAD_MAN_ACCOUNT_ID=paper-primary
DEAD_MAN_ALERT_WEBHOOK_URL=
DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID=
DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=
DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID=
DEAD_MAN_ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64=
DEAD_MAN_INTERVAL_SEC=10
DEAD_MAN_REQUEST_TIMEOUT_SEC=5
OPENDART_API_KEY=
KRX_API_KEY=
NAVER_CLIENT_ID=
NAVER_CLIENT_SECRET=
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5.5
ALERT_WEBHOOK_URL=
ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_ID=
ALERT_WEBHOOK_RECEIVER_ACK_CURRENT_KEY_B64=
ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_ID=
ALERT_WEBHOOK_RECEIVER_ACK_PREVIOUS_KEY_B64=
ALERT_WEBHOOK_TIMEOUT_SEC=5
ALERT_DRILL_MAX_LATENCY_MS=2000
LIVE_SYSTEM_ORDER_COUNT_SCOPE_ACCEPTED=false
LOCAL_PAPER_RELEASE_SHA=${releaseSha}
`;
  const desktopEnv = `VITE_SUPABASE_URL=${status.apiUrl}
VITE_SUPABASE_PUBLISHABLE_KEY=${status.publishableKey}
`;
  writeEnvFile(WORKER_ENV_PATH, workerEnv, "worker");
  writeEnvFile(DESKTOP_ENV_PATH, desktopEnv, "desktop");
}

async function validateAccess(
  client,
  expectedUserId,
  requiredRole,
  requiredPermission,
) {
  const session = await currentSession(client);
  assertFreshAal2Session(session, expectedUserId);
  const result = await client
    .schema("api")
    .rpc("get_desktop_operations_snapshot_v1");
  if (result.error || !result.data?.access) {
    fail(
      `Could not load the ${requiredRole} operations snapshot.`,
      result.error,
    );
  }
  const actor = result.data.access.actor;
  if (
    actor?.actor_id !== expectedUserId ||
    !actor.roles?.includes(requiredRole) ||
    actor.roles?.includes("platform_admin") ||
    !result.data.access.permissions?.includes(requiredPermission) ||
    result.data.access.assurance_level !== "aal2"
  ) {
    fail(
      `The ${requiredRole} access profile does not satisfy the local PAPER contract.`,
    );
  }
}

async function rpc(client, schema, functionName, parameters = undefined) {
  const result = await client.schema(schema).rpc(functionName, parameters);
  if (result.error) {
    fail(`${schema}.${functionName} failed.`, result.error);
  }
  return result.data;
}

async function freshHumanClients(status, state) {
  ensureRoles(state);
  const persist = () => writeState(state);
  const operatorClient = await signInAndVerifyMfa(
    status.apiUrl,
    status.publishableKey,
    state.operator,
    persist,
  );
  const riskClient = await signInAndVerifyMfa(
    status.apiUrl,
    status.publishableKey,
    state.risk,
    persist,
  );
  return { operatorClient, riskClient };
}

function accountOpeningState() {
  const output = psql(`select concat_ws('|',
  account.state,
  coalesce(account.opening_journal_entry_id::text, ''),
  coalesce((select count(*)::text from private.accounting_transactions transaction
    where transaction.id=account.opening_journal_entry_id), '0'),
  coalesce((select count(*)::text from private.accounting_postings posting
    where posting.journal_entry_id=account.opening_journal_entry_id), '0'),
  coalesce(balance.settled_cash_krw::bigint::text, '')
)
from private.trading_accounts account
left join private.cash_balance_projection balance using (account_id)
where account.account_id='paper-primary';
`).trim();
  const [state, journalId, transactionCount, postingCount, settledCash] =
    output.split("|");
  return { state, journalId, transactionCount, postingCount, settledCash };
}

function openingCommandState(commandId) {
  const output = psql(`select concat_ws('|', state, revision::text)
from private.operation_commands
where id=${sqlString(commandId)}::uuid and command_type='account_opening';
`).trim();
  if (!output) {
    return null;
  }
  const [state, revision] = output.split("|");
  return { state, revision: Number.parseInt(revision, 10) };
}

async function requestAndReviewAccountOpening() {
  const status = loadSupabaseStatus();
  const state = readState();
  if (!state) {
    fail(
      "Protected local credentials do not exist. Run the provision command first.",
    );
  }
  const releaseSha = run("git", ["rev-parse", "HEAD"]).trim();
  const current = accountOpeningState();
  if (current.state === "open") {
    info("PASS paper-primary is already open");
    return;
  }
  if (current.state !== "pending_open") {
    fail(
      `paper-primary has an unexpected opening state: ${current.state || "missing"}.`,
    );
  }

  const { operatorClient, riskClient } = await freshHumanClients(status, state);
  await ensureOpeningEvidence(state, operatorClient, releaseSha);
  writeState(state);

  if (!state.openingCommandId) {
    state.openingCommandId = randomUUID();
    state.openingIdempotencyKey = randomUUID();
    writeState(state);
  }
  let command = openingCommandState(state.openingCommandId);
  if (!command) {
    const requestedAt = new Date();
    const draft = {
      schema_version: 1,
      request_id: state.openingCommandId,
      environment: "paper",
      account_id: "paper-primary",
      opening_capital_krw: 10_000_000,
      idempotency_key: state.openingIdempotencyKey,
      requested_at: requestedAt.toISOString(),
      expires_at: new Date(requestedAt.getTime() + 60 * 60_000).toISOString(),
      command_type: "account_opening",
      evidence_id: state.openingEvidenceId,
      reason_code: "approved_account_opening",
    };
    const grant = await rpc(
      operatorClient,
      "api",
      "issue_account_opening_step_up_grant_v1",
      {
        request_payload: {
          schema_version: 1,
          bound_action: "request",
          bound_command_type: "account_opening",
          command_payload: draft,
        },
      },
    );
    const receipt = await rpc(
      operatorClient,
      "api",
      "request_account_opening_v1",
      {
        request_payload: { ...draft, ...grant },
      },
    );
    if (
      receipt.command_id !== state.openingCommandId ||
      receipt.state !== "requested"
    ) {
      fail(
        "Account-opening request receipt did not match the submitted command.",
      );
    }
    command = openingCommandState(state.openingCommandId);
  }

  if (command?.state === "requested") {
    const reviewDraft = {
      schema_version: 1,
      review_id: randomUUID(),
      command_id: state.openingCommandId,
      command_type: "account_opening",
      reviewer_role: "risk_approver",
      decision: "approve",
      reason_code: "policy_satisfied",
      expected_receipt_revision: command.revision,
      reviewed_at: new Date().toISOString(),
    };
    const grant = await rpc(
      riskClient,
      "api",
      "issue_account_opening_step_up_grant_v1",
      {
        request_payload: {
          schema_version: 1,
          bound_action: "review",
          bound_command_type: "account_opening",
          command_payload: reviewDraft,
        },
      },
    );
    const receipt = await rpc(riskClient, "api", "review_account_opening_v1", {
      review_payload: { ...reviewDraft, ...grant },
    });
    if (
      receipt.command_id !== state.openingCommandId ||
      receipt.state !== "approved"
    ) {
      fail(
        "Account-opening review receipt did not confirm distinct-user approval.",
      );
    }
    command = openingCommandState(state.openingCommandId);
  }

  if (command?.state !== "approved" && command?.state !== "applied") {
    fail(
      `Account-opening command is not ready for Worker application: ${command?.state ?? "missing"}.`,
    );
  }
  info("PASS operator requested account opening with recent AAL2");
  info("PASS distinct risk approver approved account opening with recent AAL2");
}

async function waitForAccountOpening() {
  const state = readState();
  const current = accountOpeningState();
  if (
    current.state === "open" &&
    current.journalId &&
    current.transactionCount === "1" &&
    current.postingCount === "2" &&
    current.settledCash === "10000000"
  ) {
    info("PASS paper-primary opening postcondition already holds");
    return;
  }
  if (!state?.openingCommandId) {
    fail(
      "No local account-opening command has been recorded and the opening postcondition does not hold.",
    );
  }
  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const command = openingCommandState(state.openingCommandId);
    const account = accountOpeningState();
    if (
      command?.state === "applied" &&
      account.state === "open" &&
      account.journalId &&
      account.transactionCount === "1" &&
      account.postingCount === "2" &&
      account.settledCash === "10000000"
    ) {
      info("PASS Worker ACK applied the account-opening command");
      info(
        "PASS opening journal and 10,000,000 KRW cash postcondition verified",
      );
      return;
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 1_000));
  }
  fail(
    "Worker did not apply account opening and satisfy the journal postcondition in time.",
  );
}

async function exercisePauseCommand() {
  const status = loadSupabaseStatus();
  const state = readState();
  if (!state) {
    fail("Protected local credentials do not exist.");
  }
  const account = accountOpeningState();
  if (account.state !== "open") {
    fail("paper-primary must be open before exercising a control command.");
  }
  const { operatorClient, riskClient } = await freshHumanClients(status, state);
  const snapshot = await rpc(
    operatorClient,
    "api",
    "get_desktop_operations_snapshot_v1",
  );
  const now = new Date();
  const draft = {
    schema_version: 1,
    request_id: randomUUID(),
    environment: "paper",
    idempotency_key: randomUUID(),
    expected_state_version: snapshot.runtime_health.state_version,
    requested_at: now.toISOString(),
    expires_at: new Date(now.getTime() + 5 * 60_000).toISOString(),
    command_type: "pause_paper",
    reason_code: "operator_pause",
  };
  const requestGrant = await rpc(
    operatorClient,
    "api",
    "issue_step_up_grant_v1",
    {
      request_payload: {
        schema_version: 1,
        bound_action: "request",
        bound_command_type: "pause_paper",
        command_payload: draft,
      },
    },
  );
  const requested = await rpc(
    operatorClient,
    "api",
    "request_operation_command_v1",
    {
      request_payload: { ...draft, ...requestGrant },
    },
  );
  if (
    requested.command_id !== draft.request_id ||
    requested.state !== "requested"
  ) {
    fail("Pause command request receipt mismatch.");
  }
  const reviewDraft = {
    schema_version: 1,
    review_id: randomUUID(),
    command_id: requested.command_id,
    command_type: "pause_paper",
    reviewer_role: "risk_approver",
    decision: "approve",
    reason_code: "policy_satisfied",
    expected_receipt_revision: requested.control_plane_receipt.revision,
    reviewed_at: new Date().toISOString(),
  };
  const reviewGrant = await rpc(riskClient, "api", "issue_step_up_grant_v1", {
    request_payload: {
      schema_version: 1,
      bound_action: "review",
      bound_command_type: "pause_paper",
      command_payload: reviewDraft,
    },
  });
  const reviewed = await rpc(riskClient, "api", "review_operation_command_v1", {
    review_payload: { ...reviewDraft, ...reviewGrant },
  });
  if (
    reviewed.command_id !== requested.command_id ||
    reviewed.state !== "approved"
  ) {
    fail("Pause command review receipt mismatch.");
  }

  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    const result =
      psql(`select concat_ws('|', command.state, control.execution_enabled::text,
  coalesce(control.last_command_id::text, ''))
from private.operation_commands command
join private.execution_controls control on control.account_id='paper-primary'
where command.id=${sqlString(requested.command_id)}::uuid;
`).trim();
    if (result === `applied|false|${requested.command_id}`) {
      state.pauseCommandId = requested.command_id;
      writeState(state);
      info(
        "PASS operator requested pause_paper and distinct risk approver approved it",
      );
      info(
        "PASS Worker ACK applied pause_paper and execution_enabled=false postcondition holds",
      );
      return;
    }
    await new Promise((resolvePromise) => setTimeout(resolvePromise, 1_000));
  }
  fail(
    "Worker did not ACK pause_paper with the expected postcondition in time.",
  );
}

async function provision() {
  const status = loadSupabaseStatus();
  const releaseSha = run("git", ["rev-parse", "HEAD"]).trim();
  if (!/^[0-9a-f]{40}$/u.test(releaseSha)) {
    fail("Current Git HEAD is not a canonical release SHA.");
  }

  ensureProtectedStateDirectory();
  const existingState = readState();
  const state = existingState ?? newState();
  if (state.operator.id === state.risk.id) {
    fail("Operator and risk approver must be distinct Auth users.");
  }
  writeState(state);

  const adminClient = makeClient(status.apiUrl, status.secretKey);
  const users = await listAllUsers(adminClient);
  await ensureAuthUser(
    adminClient,
    state.operator,
    users,
    existingState !== null,
  );
  await ensureAuthUser(adminClient, state.risk, users, existingState !== null);
  ensureRoles(state);

  const persist = () => writeState(state);
  const operatorClient = await signInAndVerifyMfa(
    status.apiUrl,
    status.publishableKey,
    state.operator,
    persist,
  );
  const riskClient = await signInAndVerifyMfa(
    status.apiUrl,
    status.publishableKey,
    state.risk,
    persist,
  );
  if (
    state.operator.factorId === state.risk.factorId ||
    state.operator.totpSecret === state.risk.totpSecret
  ) {
    fail("Operator and risk approver must have distinct TOTP factors.");
  }

  await validateAccess(
    operatorClient,
    state.operator.id,
    "operator",
    "request_command",
  );
  await validateAccess(
    riskClient,
    state.risk.id,
    "risk_approver",
    "review_command",
  );
  await ensureOpeningEvidence(state, operatorClient, releaseSha);
  writeRuntimeEnvironment(status, state, releaseSha);
  writeState(state);

  info("PASS local Auth users are distinct and login-capable");
  info("PASS both local users have verified TOTP and fresh AAL2 sessions");
  info(
    "PASS operator/risk_approver/release_manager roles are active without platform_admin",
  );
  info(
    "PASS account-opening evidence is registered through the guarded private function",
  );
  info("PASS Worker and Desktop local PAPER environment files are written");
  info(`Credential file: ${CREDENTIALS_PATH}`);
}

async function verifyIdentity() {
  const status = loadSupabaseStatus();
  const state = readState();
  if (!state) {
    fail(
      "Protected local credentials do not exist. Run the provision command first.",
    );
  }
  ensureRoles(state);
  const persist = () => writeState(state);
  const operatorClient = await signInAndVerifyMfa(
    status.apiUrl,
    status.publishableKey,
    state.operator,
    persist,
  );
  const riskClient = await signInAndVerifyMfa(
    status.apiUrl,
    status.publishableKey,
    state.risk,
    persist,
  );
  await validateAccess(
    operatorClient,
    state.operator.id,
    "operator",
    "request_command",
  );
  await validateAccess(
    riskClient,
    state.risk.id,
    "risk_approver",
    "review_command",
  );
  info(
    "PASS two-user Auth, role, TOTP, AAL2, and operations access verification",
  );
}

try {
  ensureProtectedStateDirectory();
  if (command === "provision") {
    await provision();
  } else if (command === "verify-identity") {
    await verifyIdentity();
  } else if (command === "open-account") {
    await requestAndReviewAccountOpening();
  } else if (command === "verify-opening") {
    await waitForAccountOpening();
  } else if (command === "exercise-command") {
    await exercisePauseCommand();
  } else {
    fail(`Unknown command: ${command}`);
  }
} catch (error) {
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(`FAIL local-paper ${command}: ${message}\n`);
  process.exitCode = 1;
}
