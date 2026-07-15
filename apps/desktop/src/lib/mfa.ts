import { hasSupabaseConfig, supabase } from "./supabaseClient";

export type AuthenticatorAssuranceLevel = "aal1" | "aal2" | null;

export interface MfaFactorSummary {
  readonly id: string;
  readonly friendlyName: string | null;
  readonly status: "verified" | "unverified";
  readonly createdAt: string;
}

export interface MfaStatus {
  readonly signedIn: boolean;
  readonly currentLevel: AuthenticatorAssuranceLevel;
  readonly nextLevel: AuthenticatorAssuranceLevel;
  readonly verifiedTotpFactors: readonly MfaFactorSummary[];
  readonly unverifiedTotpFactors: readonly MfaFactorSummary[];
}

export interface EphemeralTotpEnrollment {
  readonly factorId: string;
  readonly qrCodeDataUrl: string;
}

export interface MfaDataApi {
  readonly fetchStatus: () => Promise<MfaStatus>;
  readonly enrollTotp: () => Promise<EphemeralTotpEnrollment>;
  readonly verifyTotp: (input: { readonly factorId: string; readonly code: string }) => Promise<void>;
}

export class MfaOperationError extends Error {
  readonly operation: "status" | "enroll" | "challenge" | "verify";

  constructor(operation: MfaOperationError["operation"], message: string) {
    super(message);
    this.name = "MfaOperationError";
    this.operation = operation;
  }
}

interface MfaClient {
  readonly auth: {
    readonly getUser: () => Promise<{
      readonly data: { readonly user: { readonly id: string } | null };
      readonly error: unknown;
    }>;
    readonly mfa: {
      readonly listFactors: () => Promise<{
        readonly data: {
          readonly all: readonly RawMfaFactor[];
          readonly totp: readonly RawMfaFactor[];
        } | null;
        readonly error: unknown;
      }>;
      readonly getAuthenticatorAssuranceLevel: () => Promise<{
        readonly data: {
          readonly currentLevel: string | null;
          readonly nextLevel: string | null;
        } | null;
        readonly error: unknown;
      }>;
      readonly enroll: (input: {
        readonly factorType: "totp";
        readonly friendlyName: string;
        readonly issuer: string;
      }) => Promise<{
        readonly data: {
          readonly id: string;
          readonly type: string;
          readonly totp?: {
            readonly qr_code: string;
            readonly secret: string;
            readonly uri: string;
          };
        } | null;
        readonly error: unknown;
      }>;
      readonly challenge: (input: { readonly factorId: string }) => Promise<{
        readonly data: { readonly id: string; readonly type: string; readonly expires_at: number } | null;
        readonly error: unknown;
      }>;
      readonly verify: (input: {
        readonly factorId: string;
        readonly challengeId: string;
        readonly code: string;
      }) => Promise<{ readonly data: unknown; readonly error: unknown }>;
    };
  };
}

interface RawMfaFactor {
  readonly id: string;
  readonly friendly_name?: string;
  readonly factor_type: string;
  readonly status: string;
  readonly created_at: string;
}

export function createMfaDataApi(client: MfaClient): MfaDataApi {
  return {
    fetchStatus: async () => {
      const userResult = await client.auth.getUser();
      if (userResult.error || userResult.data.user === null) {
        return signedOutMfaStatus;
      }

      const [factorResult, assuranceResult] = await Promise.all([
        client.auth.mfa.listFactors(),
        client.auth.mfa.getAuthenticatorAssuranceLevel()
      ]);
      if (factorResult.error || factorResult.data === null || assuranceResult.error || assuranceResult.data === null) {
        throw new MfaOperationError("status", "TOTP 등록 상태와 2단계 인증을 확인하지 못했습니다.");
      }

      const verifiedTotpFactors = factorResult.data.totp
        .filter((factor) => factor.factor_type === "totp" && factor.status === "verified")
        .map(toFactorSummary);
      const unverifiedTotpFactors = factorResult.data.all
        .filter((factor) => factor.factor_type === "totp" && factor.status === "unverified")
        .map(toFactorSummary);
      return {
        signedIn: true,
        currentLevel: strictAssuranceLevel(assuranceResult.data.currentLevel),
        nextLevel: strictAssuranceLevel(assuranceResult.data.nextLevel),
        verifiedTotpFactors,
        unverifiedTotpFactors
      };
    },
    enrollTotp: async () => {
      const result = await client.auth.mfa.enroll({
        factorType: "totp",
        friendlyName: "KR Trading Lab Desktop",
        issuer: "KR Auto Trading Lab"
      });
      const qrCode = result.data?.totp?.qr_code;
      const totpUri = result.data?.totp?.uri;
      const totpSecret = result.data?.totp?.secret;
      if (
        result.error ||
        result.data === null ||
        result.data.type !== "totp" ||
        !result.data.id ||
        !qrCode ||
        !qrCode.startsWith("data:image/svg+xml;utf-8,") ||
        qrCode.length > 200_000 ||
        !isSafeTotpUri(totpUri, totpSecret)
      ) {
        throw new MfaOperationError("enroll", "TOTP 등록 QR을 안전하게 생성하지 못했습니다.");
      }

      // Deliberately return neither totp.secret nor totp.uri. The QR data is
      // held only in component memory until verification or unmount.
      return { factorId: result.data.id, qrCodeDataUrl: qrCode };
    },
    verifyTotp: async ({ factorId, code }) => {
      if (!factorId || !/^\d{6}$/.test(code)) {
        throw new MfaOperationError("verify", "인증 앱의 6자리 숫자 코드를 입력하세요.");
      }
      const challengeResult = await client.auth.mfa.challenge({ factorId });
      if (challengeResult.error || challengeResult.data === null || !challengeResult.data.id) {
        throw new MfaOperationError("challenge", "TOTP 검증 challenge를 만들지 못했습니다.");
      }
      const verifyResult = await client.auth.mfa.verify({
        factorId,
        challengeId: challengeResult.data.id,
        code
      });
      if (verifyResult.error) {
        throw new MfaOperationError("verify", "TOTP 코드 검증에 실패했습니다.");
      }
    }
  };
}

export const mfaDataApi: MfaDataApi = hasSupabaseConfig && supabase !== null
  ? createMfaDataApi(supabase as unknown as MfaClient)
  : {
      fetchStatus: async () => {
        throw new MfaOperationError("status", "Supabase Auth 설정이 없어 MFA 상태를 확인할 수 없습니다.");
      },
      enrollTotp: async () => {
        throw new MfaOperationError("enroll", "Supabase Auth 설정이 없어 TOTP를 등록할 수 없습니다.");
      },
      verifyTotp: async () => {
        throw new MfaOperationError("verify", "Supabase Auth 설정이 없어 TOTP를 검증할 수 없습니다.");
      }
    };

const signedOutMfaStatus: MfaStatus = {
  signedIn: false,
  currentLevel: null,
  nextLevel: null,
  verifiedTotpFactors: [],
  unverifiedTotpFactors: []
};

function strictAssuranceLevel(value: string | null): AuthenticatorAssuranceLevel {
  return value === "aal1" || value === "aal2" ? value : null;
}

function toFactorSummary(factor: RawMfaFactor): MfaFactorSummary {
  return {
    id: factor.id,
    friendlyName: factor.friendly_name?.trim() || null,
    status: factor.status === "verified" ? "verified" : "unverified",
    createdAt: factor.created_at
  };
}

function isSafeTotpUri(uri: string | undefined, secret: string | undefined): boolean {
  if (!uri || !secret || secret.length < 16 || secret.length > 256 || !/^[A-Z2-7]+=*$/i.test(secret)) {
    return false;
  }
  try {
    const parsed = new URL(uri);
    return (
      parsed.protocol === "otpauth:" &&
      parsed.hostname === "totp" &&
      parsed.username === "" &&
      parsed.password === "" &&
      parsed.hash === "" &&
      parsed.searchParams.get("secret") === secret
    );
  } catch {
    return false;
  }
}
