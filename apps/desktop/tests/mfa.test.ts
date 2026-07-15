import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { createMfaDataApi, MfaOperationError } from "../src/lib/mfa";

const calls: string[] = [];
const client = {
  auth: {
    getUser: async () => {
      calls.push("getUser");
      return { data: { user: { id: "11111111-1111-4111-8111-111111111111" } }, error: null };
    },
    mfa: {
      listFactors: async () => {
        calls.push("listFactors");
        return {
          data: {
            all: [
              {
                id: "factor-verified",
                friendly_name: "운영 인증 앱",
                factor_type: "totp",
                status: "verified",
                created_at: "2026-07-14T00:00:00.000Z"
              },
              {
                id: "factor-pending",
                factor_type: "totp",
                status: "unverified",
                created_at: "2026-07-14T00:01:00.000Z"
              }
            ],
            totp: [
              {
                id: "factor-verified",
                friendly_name: "운영 인증 앱",
                factor_type: "totp",
                status: "verified",
                created_at: "2026-07-14T00:00:00.000Z"
              }
            ]
          },
          error: null
        };
      },
      getAuthenticatorAssuranceLevel: async () => {
        calls.push("getAuthenticatorAssuranceLevel");
        return { data: { currentLevel: "aal1", nextLevel: "aal2" }, error: null };
      },
      enroll: async () => {
        calls.push("enroll");
        return {
          data: {
            id: "factor-new",
            type: "totp",
            totp: {
              qr_code: "data:image/svg+xml;utf-8,%3Csvg%3Eqr%3C/svg%3E",
              secret: "JBSWY3DPEHPK3PXP",
              uri: "otpauth://totp/KR%20Trading%20Lab?secret=JBSWY3DPEHPK3PXP&issuer=KR%20Auto%20Trading%20Lab"
            }
          },
          error: null
        };
      },
      challenge: async ({ factorId }: { readonly factorId: string }) => {
        calls.push(`challenge:${factorId}`);
        return { data: { id: "challenge-1", type: "totp", expires_at: 1_800_000_000 }, error: null };
      },
      verify: async ({ factorId, challengeId, code }: {
        readonly factorId: string;
        readonly challengeId: string;
        readonly code: string;
      }) => {
        calls.push(`verify:${factorId}:${challengeId}:${code}`);
        return { data: { access_token: "must-not-be-returned" }, error: null };
      }
    }
  }
};

const api = createMfaDataApi(client);
const status = await api.fetchStatus();
assert.equal(status.currentLevel, "aal1");
assert.equal(status.nextLevel, "aal2");
assert.equal(status.verifiedTotpFactors.length, 1);
assert.equal(status.unverifiedTotpFactors.length, 1);
assert.ok(calls.includes("listFactors"));
assert.ok(calls.includes("getAuthenticatorAssuranceLevel"));

const enrollment = await api.enrollTotp();
assert.deepEqual(enrollment, {
  factorId: "factor-new",
  qrCodeDataUrl: "data:image/svg+xml;utf-8,%3Csvg%3Eqr%3C/svg%3E"
});
assert.doesNotMatch(JSON.stringify(enrollment), /JBSWY3DPEHPK3PXP|otpauth/);

const unsafeEnrollmentApi = createMfaDataApi({
  ...client,
  auth: {
    ...client.auth,
    mfa: {
      ...client.auth.mfa,
      enroll: async () => ({
        data: {
          id: "factor-unsafe",
          type: "totp",
          totp: {
            qr_code: "data:image/svg+xml;utf-8,%3Csvg%3Eunsafe%3C/svg%3E",
            secret: "JBSWY3DPEHPK3PXP",
            uri: "https://malicious.invalid/?secret=JBSWY3DPEHPK3PXP"
          }
        },
        error: null
      })
    }
  }
});
await assert.rejects(
  () => unsafeEnrollmentApi.enrollTotp(),
  (error: unknown) => error instanceof MfaOperationError && error.operation === "enroll"
);

const callsBeforeInvalidCode = calls.length;
await assert.rejects(
  () => api.verifyTotp({ factorId: "factor-verified", code: "12ab" }),
  (error: unknown) => error instanceof MfaOperationError && error.operation === "verify"
);
assert.equal(calls.length, callsBeforeInvalidCode, "invalid code must fail before challenge creation");

const verificationResult = await api.verifyTotp({ factorId: "factor-verified", code: "123456" });
assert.equal(verificationResult, undefined, "session tokens must never escape the MFA adapter");
assert.ok(calls.includes("challenge:factor-verified"));
assert.ok(calls.includes("verify:factor-verified:challenge-1:123456"));

const testDir = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(resolve(testDir, "../src/lib/mfa.ts"), "utf8");
assert.doesNotMatch(source, /localStorage|sessionStorage|console\./);
assert.match(source, /mfa\.listFactors\(\)/);
assert.match(source, /mfa\.getAuthenticatorAssuranceLevel\(\)/);
assert.match(source, /mfa\.enroll\(/);
assert.match(source, /mfa\.challenge\(/);
assert.match(source, /mfa\.verify\(/);
assert.match(source, /parsed\.protocol === "otpauth:"/);

console.log("Supabase TOTP MFA adapter guards passed");
