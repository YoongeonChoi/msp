import { z } from "zod";

export const schemaVersionV1Schema = z.literal(1);
export const schemaVersionV2Schema = z.literal(2);
export const operationEnvironmentSchema = z.enum(["paper", "contract_test"]);
export const gitShaSchema = z.string().regex(/^[0-9a-f]{40}([0-9a-f]{24})?$/);
export const sha256Schema = z.string().regex(/^[0-9a-f]{64}$/);
export const operationCommandTypeSchema = z.enum([
  "emergency_stop",
  "pause_paper",
  "resume_paper",
  "activate_paper_strategy",
  "start_contract_test",
  "apply_risk_policy_version"
]);
const stepUpCommandTypeSchema = z.enum([
  "pause_paper",
  "resume_paper",
  "activate_paper_strategy",
  "start_contract_test",
  "apply_risk_policy_version"
]);

const uuidSchema = z.string().uuid();
const isoTimestampSchema = z.string().datetime({ offset: true });
const nullableTimestampSchema = isoTimestampSchema.nullable();
const nonEmptyTextSchema = z.string().trim().min(1).max(500);
const versionRefSchema = z.string().trim().min(1).max(160);
const koreanStockSymbolSchema = z.string().regex(/^\d{6}$/);

export const operationsRoleSchema = z.enum([
  "platform_admin",
  "operator",
  "risk_approver",
  "strategy_reviewer",
  "auditor",
  "release_manager",
  "viewer"
]);

export const operationsPermissionSchema = z.enum([
  "request_command",
  "review_command",
  "acknowledge_incident",
  "resolve_incident",
  "view_audit",
  "view_reconciliation"
]);

export const actorRefSchema = z
  .object({
    actor_id: uuidSchema,
    display_name: z.string().trim().min(1).max(120),
    roles: z.array(operationsRoleSchema).min(1)
  })
  .strict();

const stepUpGrantBindingFields = {
  step_up_grant_id: uuidSchema,
  command_hash: sha256Schema,
  step_up_grant_issued_at: isoTimestampSchema,
  step_up_grant_expires_at: isoTimestampSchema,
  step_up_grant_one_time: z.literal(true),
  step_up_grant_consumed_at: z.null(),
  bound_action: z.enum(["request", "review"]),
  bound_command_type: stepUpCommandTypeSchema
} as const;

export const stepUpGrantBindingSchema = z
  .object(stepUpGrantBindingFields)
  .strict()
  .superRefine((value, context) => {
    const issuedAt = Date.parse(value.step_up_grant_issued_at);
    const expiresAt = Date.parse(value.step_up_grant_expires_at);
    if (expiresAt <= issuedAt || expiresAt - issuedAt > 5 * 60_000) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["step_up_grant_expires_at"],
        message: "step-up grant lifetime must be greater than zero and no longer than five minutes"
      });
    }
  });

function validateStepUpBinding(
  value: z.infer<typeof stepUpGrantBindingSchema>,
  context: z.RefinementCtx
): void {
  const issuedAt = Date.parse(value.step_up_grant_issued_at);
  const expiresAt = Date.parse(value.step_up_grant_expires_at);
  if (expiresAt <= issuedAt || expiresAt - issuedAt > 5 * 60_000) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["step_up_grant_expires_at"],
      message: "step-up grant lifetime must be greater than zero and no longer than five minutes"
    });
  }
}

export const accessContextSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    signed_in: z.boolean(),
    actor: actorRefSchema.nullable(),
    session_state: z.enum(["active", "expired"]),
    assurance_level: z.enum(["aal1", "aal2"]),
    active_step_up_grants: z.array(stepUpGrantBindingSchema),
    permissions: z.array(operationsPermissionSchema)
  })
  .strict()
  .superRefine((value, context) => {
    if (value.signed_in !== (value.actor !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["actor"],
        message: "signed_in and actor must agree"
      });
    }
    if (
      !value.signed_in &&
      (value.session_state !== "expired" || value.permissions.length > 0 || value.active_step_up_grants.length > 0)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["permissions"],
        message: "signed-out access must be expired and permissionless"
      });
    }
    if (
      value.active_step_up_grants.length > 0 &&
      (value.session_state !== "active" || value.assurance_level !== "aal2")
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["active_step_up_grants"],
        message: "active step-up grants require an active AAL2 session"
      });
    }
  });

const accessStepUpGrantBindingFields = {
  step_up_grant_id: uuidSchema,
  change_hash: sha256Schema,
  step_up_grant_issued_at: isoTimestampSchema,
  step_up_grant_expires_at: isoTimestampSchema,
  step_up_grant_one_time: z.literal(true),
  step_up_grant_consumed_at: z.null(),
  bound_action: z.enum(["request", "review"])
} as const;

export const accessChangeRequestDraftSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    request_id: uuidSchema,
    subject_user_id: uuidSchema,
    requested_role: operationsRoleSchema,
    change_type: z.enum(["grant", "revoke"]),
    evidence_id: uuidSchema,
    reason_code: z.enum(["role_required", "duty_separation", "access_removal"]),
    requested_at: isoTimestampSchema,
    expires_at: isoTimestampSchema
  })
  .strict()
  .superRefine((value, context) => {
    const duration = Date.parse(value.expires_at) - Date.parse(value.requested_at);
    if (duration <= 0 || duration > 24 * 60 * 60_000) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["expires_at"],
        message: "access change request lifetime must be greater than zero and no longer than 24 hours"
      });
    }
  });

export const accessChangeReviewDraftSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    review_id: uuidSchema,
    request_id: uuidSchema,
    decision: z.enum(["approve", "reject"]),
    reason_code: z.enum(["policy_satisfied", "evidence_incomplete", "separation_of_duties"]),
    expected_state: z.literal("requested"),
    reviewed_at: isoTimestampSchema
  })
  .strict();

export const accessStepUpGrantDraftRequestSchema = z
  .union([
    z
      .object({
        schema_version: schemaVersionV1Schema,
        bound_action: z.literal("request"),
        access_change_payload: accessChangeRequestDraftSchema
      })
      .strict(),
    z
      .object({
        schema_version: schemaVersionV1Schema,
        bound_action: z.literal("review"),
        access_change_payload: accessChangeReviewDraftSchema
      })
      .strict()
  ]);

export const accessStepUpGrantIssueResponseSchema = z
  .object({ schema_version: schemaVersionV1Schema, ...accessStepUpGrantBindingFields })
  .strict()
  .superRefine((value, context) => {
    const issuedAt = Date.parse(value.step_up_grant_issued_at);
    const expiresAt = Date.parse(value.step_up_grant_expires_at);
    if (expiresAt <= issuedAt || expiresAt - issuedAt > 5 * 60_000) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["step_up_grant_expires_at"],
        message: "access step-up grant lifetime must be greater than zero and no longer than five minutes"
      });
    }
  });

export const accessChangeRequestSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    request_id: uuidSchema,
    subject_user_id: uuidSchema,
    requested_role: operationsRoleSchema,
    change_type: z.enum(["grant", "revoke"]),
    evidence_id: uuidSchema,
    reason_code: z.enum(["role_required", "duty_separation", "access_removal"]),
    requested_at: isoTimestampSchema,
    expires_at: isoTimestampSchema,
    ...accessStepUpGrantBindingFields
  })
  .strict()
  .superRefine((value, context) => {
    const duration = Date.parse(value.expires_at) - Date.parse(value.requested_at);
    if (duration <= 0 || duration > 24 * 60 * 60_000) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["expires_at"], message: "request lifetime invalid" });
    }
    if (value.bound_action !== "request") {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["bound_action"], message: "request grant required" });
    }
  });

export const accessChangeReviewSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    review_id: uuidSchema,
    request_id: uuidSchema,
    decision: z.enum(["approve", "reject"]),
    reason_code: z.enum(["policy_satisfied", "evidence_incomplete", "separation_of_duties"]),
    expected_state: z.literal("requested"),
    reviewed_at: isoTimestampSchema,
    ...accessStepUpGrantBindingFields
  })
  .strict()
  .superRefine((value, context) => {
    if (value.bound_action !== "review") {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["bound_action"], message: "review grant required" });
    }
  });

export const accessChangeReceiptSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    request_id: uuidSchema,
    subject_user_id: uuidSchema,
    requested_role: operationsRoleSchema,
    change_type: z.enum(["grant", "revoke"]),
    state: z.enum(["requested", "applied", "rejected", "expired", "canceled"]),
    requested_by: actorRefSchema,
    reviewed_by: actorRefSchema.nullable(),
    requested_at: isoTimestampSchema,
    reviewed_at: nullableTimestampSchema,
    applied_at: nullableTimestampSchema,
    expires_at: isoTimestampSchema,
    reason_code: z.enum([
      "role_required",
      "duty_separation",
      "access_removal",
      "policy_satisfied",
      "evidence_incomplete",
      "separation_of_duties"
    ])
  })
  .strict()
  .superRefine((value, context) => {
    if (Date.parse(value.expires_at) <= Date.parse(value.requested_at)) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["expires_at"], message: "receipt expiry must follow request" });
    }
    const reviewed = value.reviewed_by !== null && value.reviewed_at !== null;
    if (["requested", "expired", "canceled"].includes(value.state) && (reviewed || value.applied_at !== null)) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["reviewed_by"], message: "unreviewed access change cannot include review evidence" });
    }
    if (["applied", "rejected"].includes(value.state) && !reviewed) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["reviewed_by"], message: "terminal access change requires review evidence" });
    }
    if ((value.state === "applied") !== (value.applied_at !== null)) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["applied_at"], message: "only applied access changes contain applied_at" });
    }
    if (value.reviewed_by?.actor_id === value.requested_by.actor_id) {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["reviewed_by"], message: "access changes require maker-checker separation" });
    }
  });

export const runtimeHealthStateSchema = z.enum([
  "fresh",
  "degraded",
  "stale",
  "offline",
  "session_expired",
  "contract_error"
]);

export const runtimeComponentHealthSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    component: z.enum(["control_plane", "realtime", "worker", "market_data", "contract_test"]),
    state: runtimeHealthStateSchema,
    observed_at: isoTimestampSchema,
    detail_code: z.string().trim().min(1).max(120)
  })
  .strict();

export const freshnessPolicySchema = z
  .object({
    snapshot_max_age_seconds: z.number().int().positive().max(3600),
    worker_heartbeat_max_age_seconds: z.number().int().positive().max(3600),
    realtime_max_age_seconds: z.number().int().positive().max(3600)
  })
  .strict();

export const runtimeHealthSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    environment: operationEnvironmentSchema,
    live_permitted: z.literal(false),
    execution_enabled: z.boolean(),
    overall_state: runtimeHealthStateSchema,
    as_of: isoTimestampSchema,
    state_version: z.number().int().nonnegative(),
    realtime_connected: z.boolean(),
    realtime_last_seen_at: nullableTimestampSchema,
    active_strategy_version_id: uuidSchema.nullable(),
    active_risk_policy_version_id: uuidSchema.nullable(),
    execution_policy_version: versionRefSchema.nullable(),
    execution_policy_sha256: sha256Schema.nullable(),
    risk_policy_sha256: sha256Schema.nullable(),
    provider_contract_version: versionRefSchema.nullable(),
    provider_openapi_sha256: sha256Schema.nullable(),
    worker_release_sha: gitShaSchema.nullable(),
    worker_heartbeat_at: nullableTimestampSchema,
    components: z.array(runtimeComponentHealthSchema).min(1),
    freshness_policy: freshnessPolicySchema
  })
  .strict();

export const qualificationGateResultSchema = z
  .object({
    status: z.enum(["pass", "fail", "expired", "not_evaluated"]),
    checked_at: isoTimestampSchema,
    evidence_ref: versionRefSchema.nullable()
  })
  .strict();

export const qualificationSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    qualification_id: uuidSchema,
    environment: operationEnvironmentSchema,
    status: z.enum(["qualified", "blocked", "expired"]),
    release_sha: gitShaSchema,
    ledger_checkpoint: versionRefSchema,
    dataset_version: versionRefSchema,
    strategy_version_id: uuidSchema,
    risk_policy_version_id: uuidSchema,
    valid_from: isoTimestampSchema,
    valid_until: isoTimestampSchema,
    g1: qualificationGateResultSchema,
    g2: qualificationGateResultSchema
  })
  .strict()
  .superRefine((value, context) => {
    if (Date.parse(value.valid_until) <= Date.parse(value.valid_from)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["valid_until"],
        message: "valid_until must be later than valid_from"
      });
    }
    if (value.status === "qualified" && (value.g1.status !== "pass" || value.g2.status !== "pass")) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["status"],
        message: "qualified status requires both gates to pass"
      });
    }
  });

const commandRequestBase = {
  schema_version: schemaVersionV1Schema,
  request_id: uuidSchema,
  environment: operationEnvironmentSchema,
  idempotency_key: uuidSchema,
  expected_state_version: z.number().int().nonnegative(),
  requested_at: isoTimestampSchema,
  expires_at: isoTimestampSchema
} as const;

export const pausePaperCommandDraftSchema = z
  .object({
    ...commandRequestBase,
    command_type: z.literal("pause_paper"),
    reason_code: z.enum(["operator_pause", "maintenance_window", "risk_review"])
  })
  .strict();

export const qualifiedCommandDraftSchema = z
  .object({
    ...commandRequestBase,
    command_type: z.enum([
      "resume_paper",
      "activate_paper_strategy",
      "start_contract_test",
      "apply_risk_policy_version"
    ]),
    qualification_id: uuidSchema,
    strategy_version_id: uuidSchema,
    risk_policy_version_id: uuidSchema,
    release_sha: gitShaSchema,
    ledger_checkpoint: versionRefSchema,
    reason_code: z.enum(["qualified_resume", "approved_change", "boundary_verification"])
  })
  .strict();

export const stepUpCommandDraftSchema = z
  .union([pausePaperCommandDraftSchema, qualifiedCommandDraftSchema])
  .superRefine((value, context) => {
    if (Date.parse(value.expires_at) <= Date.parse(value.requested_at)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["expires_at"],
        message: "expires_at must be later than requested_at"
      });
    }
  });

export const emergencyStopCommandRequestSchema = z
  .object({
    ...commandRequestBase,
    command_type: z.literal("emergency_stop"),
    reason_code: z.enum(["operator_safety_stop", "incident_response"]),
    assurance_level: z.literal("aal2"),
    actor_role: z.literal("operator"),
    single_actor: z.literal(true)
  })
  .strict();

export const pausePaperCommandRequestSchema = z
  .object({
    ...commandRequestBase,
    ...stepUpGrantBindingFields,
    command_type: z.literal("pause_paper"),
    reason_code: z.enum(["operator_pause", "maintenance_window", "risk_review"])
  })
  .strict();

export const qualifiedCommandRequestSchema = z
  .object({
    ...commandRequestBase,
    ...stepUpGrantBindingFields,
    command_type: z.enum([
      "resume_paper",
      "activate_paper_strategy",
      "start_contract_test",
      "apply_risk_policy_version"
    ]),
    qualification_id: uuidSchema,
    strategy_version_id: uuidSchema,
    risk_policy_version_id: uuidSchema,
    release_sha: gitShaSchema,
    ledger_checkpoint: versionRefSchema,
    reason_code: z.enum(["qualified_resume", "approved_change", "boundary_verification"])
  })
  .strict();

export const operationCommandRequestSchema = z
  .union([emergencyStopCommandRequestSchema, pausePaperCommandRequestSchema, qualifiedCommandRequestSchema])
  .superRefine((value, context) => {
    if (Date.parse(value.expires_at) <= Date.parse(value.requested_at)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["expires_at"],
        message: "expires_at must be later than requested_at"
      });
    }
    if (value.command_type !== "emergency_stop") {
      validateStepUpBinding(value, context);
      if (value.bound_action !== "request" || value.bound_command_type !== value.command_type) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["step_up_grant_id"],
          message: "step-up grant must be bound to this command request"
        });
      }
    }
  });

export const commandReviewRequestSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    review_id: uuidSchema,
    command_id: uuidSchema,
    command_type: stepUpCommandTypeSchema,
    ...stepUpGrantBindingFields,
    reviewer_role: z.enum(["risk_approver", "strategy_reviewer", "release_manager"]),
    decision: z.enum(["approve", "reject"]),
    reason_code: z.enum(["policy_satisfied", "evidence_incomplete", "risk_rejected", "separation_of_duties"]),
    expected_receipt_revision: z.number().int().nonnegative(),
    reviewed_at: isoTimestampSchema
  })
  .strict()
  .superRefine((value, context) => {
    validateStepUpBinding(value, context);
    if (value.bound_action !== "review" || value.bound_command_type !== value.command_type) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["step_up_grant_id"],
        message: "step-up grant must be bound to this command review"
      });
    }
  });

export const commandReviewDraftSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    review_id: uuidSchema,
    command_id: uuidSchema,
    command_type: stepUpCommandTypeSchema,
    reviewer_role: z.enum(["risk_approver", "strategy_reviewer", "release_manager"]),
    decision: z.enum(["approve", "reject"]),
    reason_code: z.enum(["policy_satisfied", "evidence_incomplete", "risk_rejected", "separation_of_duties"]),
    expected_receipt_revision: z.number().int().nonnegative(),
    reviewed_at: isoTimestampSchema
  })
  .strict();

export const stepUpGrantDraftRequestSchema = z
  .union([
    z
      .object({
        schema_version: schemaVersionV1Schema,
        bound_action: z.literal("request"),
        bound_command_type: stepUpCommandTypeSchema,
        command_payload: stepUpCommandDraftSchema
      })
      .strict(),
    z
      .object({
        schema_version: schemaVersionV1Schema,
        bound_action: z.literal("review"),
        bound_command_type: stepUpCommandTypeSchema,
        command_payload: commandReviewDraftSchema
      })
      .strict()
  ])
  .superRefine((value, context) => {
    if (value.bound_command_type !== value.command_payload.command_type) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["bound_command_type"],
        message: "step-up binding must match the strict draft command type"
      });
    }
  });

export const stepUpGrantIssueResponseSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    ...stepUpGrantBindingFields
  })
  .strict()
  .superRefine((value, context) => {
    const issuedAt = Date.parse(value.step_up_grant_issued_at);
    const expiresAt = Date.parse(value.step_up_grant_expires_at);
    if (expiresAt <= issuedAt || expiresAt - issuedAt > 5 * 60_000) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["step_up_grant_expires_at"],
        message: "step-up grant lifetime must be greater than zero and no longer than five minutes"
      });
    }
  });

const approvalActorRefSchema = actorRefSchema.superRefine((value, context) => {
  if (value.roles.includes("platform_admin")) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["roles"],
      message: "platform_admin is not a trading approval role"
    });
  }
});

export const commandReviewSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    review_id: uuidSchema,
    command_id: uuidSchema,
    command_type: stepUpCommandTypeSchema,
    reviewer: approvalActorRefSchema,
    reviewer_role: z.enum(["risk_approver", "strategy_reviewer", "release_manager"]),
    step_up_grant_id: uuidSchema,
    command_hash: sha256Schema,
    decision: z.enum(["approved", "rejected"]),
    reason_code: z.enum(["policy_satisfied", "evidence_incomplete", "risk_rejected", "separation_of_duties"]),
    reviewed_at: isoTimestampSchema,
    receipt_revision: z.number().int().nonnegative()
  })
  .strict()
  .superRefine((value, context) => {
    if (!value.reviewer.roles.includes(value.reviewer_role)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["reviewer_role"],
        message: "reviewer must hold the recorded approval role"
      });
    }
  });

export const controlPlaneReceiptSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    receipt_id: uuidSchema,
    command_id: uuidSchema,
    state: z.enum(["requested", "approved", "rejected", "expired", "canceled"]),
    revision: z.number().int().nonnegative(),
    persisted_at: isoTimestampSchema,
    approved_at: nullableTimestampSchema,
    approved_by: approvalActorRefSchema.nullable()
  })
  .strict()
  .superRefine((value, context) => {
    const hasApprovalTime = value.approved_at !== null;
    const hasApprover = value.approved_by !== null;
    if (hasApprovalTime !== hasApprover) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["approved_by"],
        message: "approved_at and approved_by must be present together"
      });
    }
    if (value.state === "approved" && (!hasApprovalTime || !hasApprover)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["approved_by"],
        message: "approved receipt requires approval evidence"
      });
    }
    if (value.state !== "approved" && (hasApprovalTime || hasApprover)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["approved_by"],
        message: "non-approved receipt must not contain approval evidence"
      });
    }
  });

const workerAckBase = {
  schema_version: schemaVersionV1Schema,
  ack_id: uuidSchema,
  command_id: uuidSchema,
  worker_instance_id: uuidSchema,
  worker_release_sha: gitShaSchema,
  claimed_at: isoTimestampSchema
} as const;

export const workerCommandAckSchema = z.discriminatedUnion("state", [
  z
    .object({
      ...workerAckBase,
      state: z.literal("claimed"),
      applied_at: z.null(),
      post_state_version: z.null(),
      failure_code: z.null()
    })
    .strict(),
  z
    .object({
      ...workerAckBase,
      state: z.literal("applied"),
      applied_at: isoTimestampSchema,
      post_state_version: z.number().int().nonnegative(),
      failure_code: z.null()
    })
    .strict(),
  z
    .object({
      ...workerAckBase,
      state: z.literal("failed"),
      applied_at: isoTimestampSchema,
      post_state_version: z.number().int().nonnegative().nullable(),
      failure_code: z.string().trim().min(1).max(120)
    })
    .strict()
]);

export const operationCommandStateSchema = z.enum([
  "requested",
  "approved",
  "claimed",
  "applied",
  "rejected",
  "failed",
  "expired",
  "canceled"
]);

export const operationCommandReceiptSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    command_id: uuidSchema,
    command_type: operationCommandTypeSchema,
    environment: operationEnvironmentSchema,
    state: operationCommandStateSchema,
    requested_by: actorRefSchema,
    requested_at: isoTimestampSchema,
    expires_at: isoTimestampSchema,
    qualification_id: uuidSchema.nullable(),
    strategy_version_id: uuidSchema.nullable(),
    risk_policy_version_id: uuidSchema.nullable(),
    release_sha: gitShaSchema.nullable(),
    ledger_checkpoint: versionRefSchema.nullable(),
    command_hash: sha256Schema.nullable(),
    control_plane_receipt: controlPlaneReceiptSchema,
    worker_ack: workerCommandAckSchema.nullable()
  })
  .strict()
  .superRefine((value, context) => {
    if (value.control_plane_receipt.command_id !== value.command_id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["control_plane_receipt", "command_id"],
        message: "control-plane receipt command_id mismatch"
      });
    }
    if (value.worker_ack !== null && value.worker_ack.command_id !== value.command_id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["worker_ack", "command_id"],
        message: "worker acknowledgement command_id mismatch"
      });
    }
    if (Date.parse(value.expires_at) <= Date.parse(value.requested_at)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["expires_at"],
        message: "command receipt expiry must follow its request time"
      });
    }
    if (Date.parse(value.control_plane_receipt.persisted_at) < Date.parse(value.requested_at)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["control_plane_receipt", "persisted_at"],
        message: "control-plane receipt cannot predate the command request"
      });
    }
    const qualificationRequired = !["emergency_stop", "pause_paper"].includes(value.command_type);
    if (qualificationRequired !== (value.qualification_id !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["qualification_id"],
        message: "qualification requirement does not match command type"
      });
    }
    const targetPins = [
      value.strategy_version_id,
      value.risk_policy_version_id,
      value.release_sha,
      value.ledger_checkpoint
    ];
    if (targetPins.some((pin) => (qualificationRequired ? pin === null : pin !== null))) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["strategy_version_id"],
        message: "qualified command target pins must be complete and control-only commands must not contain them"
      });
    }
    const hashRequired = value.command_type !== "emergency_stop";
    if (hashRequired !== (value.command_hash !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["command_hash"],
        message: "command hash requirement does not match command type"
      });
    }
    if (
      (["requested", "approved", "rejected", "expired", "canceled"] as const).includes(
        value.state as "requested" | "approved" | "rejected" | "expired" | "canceled"
      ) &&
      value.worker_ack !== null
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["worker_ack"],
        message: "control-plane-only states must not contain a worker acknowledgement"
      });
    }
    if (value.state === "claimed" && value.worker_ack?.state !== "claimed") {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["worker_ack"], message: "claimed state requires claimed ACK" });
    }
    if (value.state === "applied" && value.worker_ack?.state !== "applied") {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["worker_ack"], message: "applied state requires applied ACK" });
    }
    if (value.state === "failed" && value.worker_ack?.state !== "failed") {
      context.addIssue({ code: z.ZodIssueCode.custom, path: ["worker_ack"], message: "failed state requires failed ACK" });
    }
    const expectedControlState = (["approved", "claimed", "applied", "failed"] as const).includes(
      value.state as "approved" | "claimed" | "applied" | "failed"
    )
      ? "approved"
      : value.state;
    if (value.control_plane_receipt.state !== expectedControlState) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["control_plane_receipt", "state"],
        message: "control-plane receipt state does not match command lifecycle"
      });
    }
    if (
      value.command_type !== "emergency_stop" &&
      value.control_plane_receipt.approved_by?.actor_id === value.requested_by.actor_id
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["control_plane_receipt", "approved_by"],
        message: "non-emergency commands require maker-checker separation"
      });
    }
    if (value.worker_ack !== null && Date.parse(value.worker_ack.claimed_at) < Date.parse(value.requested_at)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["worker_ack", "claimed_at"],
        message: "worker claim cannot predate the command request"
      });
    }
    if (
      value.worker_ack?.applied_at !== null &&
      value.worker_ack?.applied_at !== undefined &&
      Date.parse(value.worker_ack.applied_at) < Date.parse(value.worker_ack.claimed_at)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["worker_ack", "applied_at"],
        message: "worker application cannot predate its claim"
      });
    }
  });

export const orderReadModelSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    order_id: uuidSchema,
    command_id: uuidSchema.nullable(),
    environment: operationEnvironmentSchema,
    symbol: koreanStockSymbolSchema,
    side: z.enum(["buy", "sell"]),
    order_type: z.literal("limit"),
    requested_quantity: z.number().int().positive(),
    filled_quantity: z.number().int().nonnegative(),
    requested_price_krw: z.number().positive(),
    average_fill_price_krw: z.number().positive().nullable(),
    status: z.enum([
      "proposed",
      "paper_simulated",
      "contract_simulated",
      "partial_filled",
      "filled",
      "canceled",
      "expired",
      "rejected",
      "failed",
      "reconciliation_required"
    ]),
    strategy_version_id: uuidSchema,
    risk_policy_version_id: uuidSchema,
    idempotency_key: sha256Schema,
    created_at: isoTimestampSchema,
    updated_at: isoTimestampSchema
  })
  .strict()
  .superRefine((value, context) => {
    if (value.filled_quantity > value.requested_quantity) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["filled_quantity"],
        message: "filled quantity cannot exceed requested quantity"
      });
    }
  });

export const positionReadModelSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    position_id: uuidSchema,
    environment: operationEnvironmentSchema,
    symbol: koreanStockSymbolSchema,
    quantity: z.number().int().nonnegative(),
    average_price_krw: z.number().nonnegative(),
    market_price_krw: z.number().nonnegative().nullable(),
    market_value_krw: z.number().nonnegative().nullable(),
    unrealized_pnl_krw: z.number().nullable(),
    market_data_status: z.enum(["available", "unavailable", "stale"]),
    market_data_source: versionRefSchema.nullable(),
    market_data_as_of: nullableTimestampSchema,
    as_of: isoTimestampSchema
  })
  .strict()
  .superRefine((value, context) => {
    const values = [value.market_price_krw, value.market_value_krw, value.unrealized_pnl_krw];
    const completeValuation = values.every((item) => item !== null);
    if (value.market_data_status === "available") {
      if (!completeValuation || value.market_data_source === null || value.market_data_as_of === null) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["market_data_status"],
          message: "available market data requires complete sourced valuation"
        });
      }
    } else if (values.some((item) => item !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["market_price_krw"],
        message: "unavailable or stale market data must not expose an unverified valuation"
      });
    }
  });

export const incidentSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    incident_id: uuidSchema,
    severity: z.enum(["sev1", "sev2", "sev3", "sev4"]),
    status: z.enum(["open", "acknowledged", "mitigating", "resolved"]),
    kind: z.enum([
      "stale_data",
      "worker_offline",
      "command_timeout",
      "contract_error",
      "reconciliation_required",
      "access_violation"
    ]),
    title: z.string().trim().min(1).max(160),
    summary: nonEmptyTextSchema,
    detected_at: isoTimestampSchema,
    ack_due_at: nullableTimestampSchema,
    escalation_status: z.enum(["not_required", "pending", "canceled", "escalated"]),
    acknowledged_at: nullableTimestampSchema,
    resolved_at: nullableTimestampSchema,
    owner: actorRefSchema.nullable(),
    evidence_refs: z.array(versionRefSchema)
  })
  .strict();

export const incidentActionRequestSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    action_id: uuidSchema,
    incident_id: uuidSchema,
    action: z.enum(["acknowledge", "resolve"]),
    reason_code: z.enum(["operator_acknowledged", "mitigation_verified"]),
    expected_status: z.enum(["open", "acknowledged", "mitigating"]),
    acted_at: isoTimestampSchema
  })
  .strict();

export const incidentActionReceiptSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    incident_id: uuidSchema,
    status: z.enum(["acknowledged", "resolved"]),
    action_id: uuidSchema,
    acted_at: isoTimestampSchema
  })
  .strict();

export const auditEventSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    audit_id: uuidSchema,
    occurred_at: isoTimestampSchema,
    actor: actorRefSchema.nullable(),
    action: z.enum([
      "command_requested",
      "command_reviewed",
      "command_claimed",
      "command_applied",
      "command_failed",
      "incident_acknowledged",
      "incident_resolved",
      "reconciliation_opened",
      "reconciliation_resolved",
      "access_denied"
    ]),
    resource_type: z.enum(["command", "incident", "order", "position", "qualification", "session"]),
    resource_id: uuidSchema,
    outcome: z.enum(["success", "denied", "failed"]),
    reason_code: z.string().trim().min(1).max(120),
    correlation_id: uuidSchema
  })
  .strict();

export const reconciliationCaseSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    case_id: uuidSchema,
    order_id: uuidSchema,
    environment: operationEnvironmentSchema,
    status: z.enum(["open", "investigating", "resolved", "rejected"]),
    reason_code: z.enum(["ack_timeout", "ambiguous_order_state", "fill_mismatch", "operator_escalation"]),
    opened_at: isoTimestampSchema,
    updated_at: isoTimestampSchema,
    owner: actorRefSchema.nullable(),
    evidence_refs: z.array(versionRefSchema).min(1),
    resolution_code: z.enum(["matched", "canceled", "manual_follow_up", "not_applicable"]).nullable()
  })
  .strict();

const safeNonnegativeIntegerSchema = z
  .number()
  .int()
  .nonnegative()
  .max(Number.MAX_SAFE_INTEGER);
const safePositiveIntegerSchema = z
  .number()
  .int()
  .positive()
  .max(Number.MAX_SAFE_INTEGER);
const isoDateSchema = z
  .string()
  .regex(/^\d{4}-\d{2}-\d{2}$/)
  .refine((value) => {
    const parsed = new Date(`${value}T00:00:00.000Z`);
    return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
  }, "date must be a real ISO calendar date");
const providerOrderIdSchema = z.string().trim().min(1).max(200);
const evidenceArtifactUriSchema = z
  .string()
  .trim()
  .min(1)
  .max(500)
  .refine(
    (value) =>
      (/^https:\/\/[^/?#\s]+\//.test(value) && !/[?#]/.test(value)) ||
      /^urn:sha256:[0-9a-f]{64}$/.test(value),
    "evidence artifact URI must be an HTTPS path without query/fragment or a SHA-256 URN"
  );

const unknownResolutionMissingFillFields = {
  schema_version: schemaVersionV2Schema,
  fill_sequence: z.number().int().positive().max(100),
  provider_order_id: providerOrderIdSchema,
  provider_execution_id: z.string().trim().min(1).max(200),
  quantity: safePositiveIntegerSchema,
  price_krw: safePositiveIntegerSchema,
  commission_krw: safeNonnegativeIntegerSchema,
  tax_krw: safeNonnegativeIntegerSchema,
  filled_at: isoTimestampSchema,
  settlement_date: isoDateSchema,
  evidence_sha256: sha256Schema
} as const;

export const unknownResolutionMissingFillV2Schema = z
  .object(unknownResolutionMissingFillFields)
  .strict();

export const unknownResolutionProposedFillV2Schema = z
  .object({
    ...unknownResolutionMissingFillFields,
    proposal_sha256: sha256Schema
  })
  .strict();

const unknownResolutionRequestDraftFields = {
  schema_version: schemaVersionV2Schema,
  request_id: uuidSchema,
  environment: operationEnvironmentSchema,
  idempotency_key: uuidSchema,
  command_type: z.literal("close_unknown_execution"),
  break_id: uuidSchema,
  intent_id: uuidSchema,
  unknown_observation_id: uuidSchema,
  provider_order_id: providerOrderIdSchema,
  evidence_artifact_uri: evidenceArtifactUriSchema,
  evidence_sha256: sha256Schema,
  evidence_captured_at: isoTimestampSchema,
  reason_code: z.literal("accounting_closure_requested"),
  expected_break_state: z.literal("open"),
  expected_break_revision: safeNonnegativeIntegerSchema,
  expected_reconciliation_state: z.literal("manual"),
  expected_cash_projection_version: safeNonnegativeIntegerSchema,
  expected_position_projection_version: safeNonnegativeIntegerSchema.nullable(),
  expected_reservation_event_sequence: safePositiveIntegerSchema,
  expected_control_epoch: safePositiveIntegerSchema,
  terminal_status: z.enum(["filled", "canceled", "expired", "rejected"]),
  missing_fills: z.array(unknownResolutionMissingFillV2Schema).max(100),
  requested_at: isoTimestampSchema,
  expires_at: isoTimestampSchema
} as const;

function validateUnknownResolutionRequestDraft(
  value: z.infer<z.ZodObject<typeof unknownResolutionRequestDraftFields>>,
  context: z.RefinementCtx
): void {
  const requestedAt = Date.parse(value.requested_at);
  const expiresAt = Date.parse(value.expires_at);
  const capturedAt = Date.parse(value.evidence_captured_at);
  if (expiresAt <= requestedAt || expiresAt - requestedAt > 24 * 60 * 60_000) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["expires_at"],
      message: "unknown-resolution request lifetime must be greater than zero and no longer than 24 hours"
    });
  }
  if (capturedAt > requestedAt || requestedAt - capturedAt > 30 * 24 * 60 * 60_000) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["evidence_captured_at"],
      message: "evidence must be captured no later than the request and within 30 days"
    });
  }
  if (
    value.evidence_artifact_uri.startsWith("urn:sha256:") &&
    value.evidence_artifact_uri !== `urn:sha256:${value.evidence_sha256}`
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["evidence_artifact_uri"],
      message: "SHA-256 evidence URN must bind the submitted evidence digest"
    });
  }
  value.missing_fills.forEach((fill, index) => {
    if (fill.fill_sequence !== index + 1) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["missing_fills", index, "fill_sequence"],
        message: "missing-fill sequence must be contiguous and start at one"
      });
    }
    if (fill.provider_order_id !== value.provider_order_id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["missing_fills", index, "provider_order_id"],
        message: "missing fill must retain the unknown provider order identity"
      });
    }
  });
}

export const unknownResolutionRequestDraftV2Schema = z
  .object(unknownResolutionRequestDraftFields)
  .strict()
  .superRefine(validateUnknownResolutionRequestDraft);

const unknownResolutionReviewDraftFields = {
  schema_version: schemaVersionV2Schema,
  review_id: uuidSchema,
  command_id: uuidSchema,
  command_type: z.literal("close_unknown_execution"),
  reviewer_role: z.literal("risk_approver"),
  decision: z.enum(["approve", "reject"]),
  reason_code: z.enum([
    "evidence_sufficient",
    "evidence_incomplete",
    "accounting_adjustment_required"
  ]),
  expected_receipt_revision: safeNonnegativeIntegerSchema,
  expected_break_revision: safeNonnegativeIntegerSchema,
  request_digest_sha256: sha256Schema,
  evidence_sha256: sha256Schema,
  reviewed_at: isoTimestampSchema
} as const;

function validateUnknownResolutionReviewDraft(
  value: z.infer<z.ZodObject<typeof unknownResolutionReviewDraftFields>>,
  context: z.RefinementCtx
): void {
  const reasonValid = value.decision === "approve"
    ? value.reason_code === "evidence_sufficient"
    : value.reason_code === "evidence_incomplete" ||
      value.reason_code === "accounting_adjustment_required";
  if (!reasonValid) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["reason_code"],
      message: "review reason does not match its decision"
    });
  }
}

export const unknownResolutionReviewDraftV2Schema = z
  .object(unknownResolutionReviewDraftFields)
  .strict()
  .superRefine(validateUnknownResolutionReviewDraft);

const unknownResolutionStepUpBindingFields = {
  step_up_grant_id: uuidSchema,
  command_hash: sha256Schema,
  step_up_grant_issued_at: isoTimestampSchema,
  step_up_grant_expires_at: isoTimestampSchema,
  step_up_grant_one_time: z.literal(true),
  step_up_grant_consumed_at: z.null(),
  bound_action: z.enum(["request", "review"]),
  bound_command_type: z.literal("close_unknown_execution")
} as const;

function validateUnknownResolutionStepUpLifetime(
  value: { readonly step_up_grant_issued_at: string; readonly step_up_grant_expires_at: string },
  context: z.RefinementCtx
): void {
  const issuedAt = Date.parse(value.step_up_grant_issued_at);
  const expiresAt = Date.parse(value.step_up_grant_expires_at);
  if (expiresAt <= issuedAt || expiresAt - issuedAt > 5 * 60_000) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["step_up_grant_expires_at"],
      message: "unknown-resolution step-up lifetime must be greater than zero and no longer than five minutes"
    });
  }
}

export const unknownResolutionStepUpGrantDraftRequestV2Schema = z.union([
  z
    .object({
      schema_version: schemaVersionV2Schema,
      bound_action: z.literal("request"),
      bound_command_type: z.literal("close_unknown_execution"),
      command_payload: unknownResolutionRequestDraftV2Schema
    })
    .strict(),
  z
    .object({
      schema_version: schemaVersionV2Schema,
      bound_action: z.literal("review"),
      bound_command_type: z.literal("close_unknown_execution"),
      command_payload: unknownResolutionReviewDraftV2Schema
    })
    .strict()
]);

export const unknownResolutionStepUpGrantV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    ...unknownResolutionStepUpBindingFields
  })
  .strict()
  .superRefine(validateUnknownResolutionStepUpLifetime);

export const unknownResolutionRequestV2Schema = z
  .object({
    ...unknownResolutionRequestDraftFields,
    ...unknownResolutionStepUpBindingFields,
    bound_action: z.literal("request")
  })
  .strict()
  .superRefine((value, context) => {
    validateUnknownResolutionRequestDraft(value, context);
    validateUnknownResolutionStepUpLifetime(value, context);
  });

export const unknownResolutionReviewV2Schema = z
  .object({
    ...unknownResolutionReviewDraftFields,
    ...unknownResolutionStepUpBindingFields,
    bound_action: z.literal("review")
  })
  .strict()
  .superRefine((value, context) => {
    validateUnknownResolutionReviewDraft(value, context);
    validateUnknownResolutionStepUpLifetime(value, context);
  });

export const unknownResolutionReceiptV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    command_id: uuidSchema,
    break_id: uuidSchema,
    intent_id: uuidSchema,
    state: z.enum(["requested", "approved", "claimed", "applied", "rejected", "failed", "expired", "canceled"]),
    receipt_revision: safeNonnegativeIntegerSchema,
    break_revision: safeNonnegativeIntegerSchema,
    request_digest_sha256: sha256Schema,
    review_digest_sha256: sha256Schema.nullable(),
    terminal_status: z.enum(["filled", "canceled", "expired", "rejected"]),
    claim_token: uuidSchema.nullable(),
    work_revision: safeNonnegativeIntegerSchema.nullable(),
    application_id: uuidSchema.nullable(),
    application_sha256: sha256Schema.nullable(),
    accounting_mutation_allowed: z.boolean(),
    resolution_complete: z.boolean(),
    inserted: z.boolean()
  })
  .strict()
  .superRefine((value, context) => {
    const mutationAllowed = ["approved", "claimed", "applied"].includes(value.state);
    if (value.accounting_mutation_allowed !== mutationAllowed) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["accounting_mutation_allowed"],
        message: "accounting mutation permission must match the reviewed command state"
      });
    }
    const applied = value.state === "applied";
    if (
      value.resolution_complete !== applied ||
      applied !== (value.application_id !== null && value.application_sha256 !== null)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["resolution_complete"],
        message: "only an applied command with an application receipt is complete"
      });
    }
    if (["requested", "rejected", "failed", "expired", "canceled"].includes(value.state)) {
      if (value.claim_token !== null || value.work_revision !== null || value.application_id !== null) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["claim_token"],
          message: "unapproved command cannot expose a worker receipt"
        });
      }
    }
    if (value.state === "requested" && value.review_digest_sha256 !== null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["review_digest_sha256"],
        message: "requested command cannot contain a review digest"
      });
    }
    if (["approved", "claimed", "applied", "rejected"].includes(value.state) && value.review_digest_sha256 === null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["review_digest_sha256"],
        message: "reviewed command requires a review digest"
      });
    }
    if (["approved", "claimed", "applied"].includes(value.state) && value.work_revision === null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["work_revision"],
        message: "approved command requires a worker work receipt"
      });
    }
    if (["claimed", "applied"].includes(value.state) && value.claim_token === null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["claim_token"],
        message: "claimed or applied command requires a claim token"
      });
    }
  });

export const unknownResolutionProviderIdentityV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    broker: z.enum(["internal_paper", "local_contract_simulator"]),
    provider_order_id: providerOrderIdSchema,
    provider_execution_id: z.string().trim().min(1).max(200).nullable(),
    provider_binding_sha256: sha256Schema,
    provider_contract_version: versionRefSchema.nullable(),
    provider_openapi_sha256: sha256Schema.nullable()
  })
  .strict();

export const unknownExecutionObservationV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    observation_id: uuidSchema,
    sequence: safePositiveIntegerSchema,
    event_type: z.literal("unknown_requires_manual_check"),
    observed_at: isoTimestampSchema,
    cumulative_quantity: safeNonnegativeIntegerSchema,
    cumulative_gross_krw: safeNonnegativeIntegerSchema,
    cumulative_commission_krw: safeNonnegativeIntegerSchema,
    cumulative_tax_krw: safeNonnegativeIntegerSchema,
    reason_code: z.string().trim().min(1).max(160).nullable(),
    observation_sha256: sha256Schema,
    provider_observation_sha256: sha256Schema
  })
  .strict();

export const unknownResolutionRequestContextV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    request_id: uuidSchema,
    state: z.enum(["requested", "approved", "claimed", "applied", "rejected", "failed", "expired", "canceled"]),
    receipt_revision: safeNonnegativeIntegerSchema,
    requested_by: actorRefSchema,
    requested_at: isoTimestampSchema,
    expires_at: isoTimestampSchema,
    terminal_status: z.enum(["filled", "canceled", "expired", "rejected"]),
    evidence_artifact_uri: evidenceArtifactUriSchema,
    evidence_sha256: sha256Schema,
    evidence_captured_at: isoTimestampSchema,
    request_digest_sha256: sha256Schema,
    expected_break_revision: safeNonnegativeIntegerSchema,
    expected_cash_projection_version: safeNonnegativeIntegerSchema,
    expected_position_projection_version: safeNonnegativeIntegerSchema.nullable(),
    expected_reservation_event_sequence: safePositiveIntegerSchema,
    expected_control_epoch: safePositiveIntegerSchema,
    missing_fills: z.array(unknownResolutionProposedFillV2Schema).max(100)
  })
  .strict()
  .superRefine((value, context) => {
    const requestedAt = Date.parse(value.requested_at);
    const expiresAt = Date.parse(value.expires_at);
    const capturedAt = Date.parse(value.evidence_captured_at);
    if (expiresAt <= requestedAt || expiresAt - requestedAt > 24 * 60 * 60_000) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["expires_at"],
        message: "stored request expiry must follow the request by no more than 24 hours"
      });
    }
    if (capturedAt > requestedAt || requestedAt - capturedAt > 30 * 24 * 60 * 60_000) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["evidence_captured_at"],
        message: "stored evidence time must precede the request by no more than 30 days"
      });
    }
    if (
      value.evidence_artifact_uri.startsWith("urn:sha256:") &&
      value.evidence_artifact_uri !== `urn:sha256:${value.evidence_sha256}`
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["evidence_artifact_uri"],
        message: "stored evidence URN must bind the immutable evidence digest"
      });
    }
  });

export const unknownResolutionReviewContextV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    review_id: uuidSchema,
    decision: z.enum(["approved", "rejected"]),
    reason_code: z.enum(["evidence_sufficient", "evidence_incomplete", "accounting_adjustment_required"]),
    reviewed_by: actorRefSchema,
    reviewed_at: isoTimestampSchema,
    request_digest_sha256: sha256Schema,
    review_digest_sha256: sha256Schema,
    evidence_sha256: sha256Schema
  })
  .strict();

export const unknownResolutionWorkReceiptV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    state: z.enum(["approved", "claimed", "applied"]),
    work_revision: safeNonnegativeIntegerSchema,
    claim_token: uuidSchema.nullable(),
    claimed_at: nullableTimestampSchema,
    claim_expires_at: nullableTimestampSchema,
    applied_at: nullableTimestampSchema,
    worker_release_sha: gitShaSchema.nullable(),
    fencing_token: safePositiveIntegerSchema.nullable()
  })
  .strict()
  .superRefine((value, context) => {
    const claimed = value.state === "claimed" || value.state === "applied";
    const claimFields = [
      value.claim_token,
      value.claimed_at,
      value.claim_expires_at,
      value.worker_release_sha,
      value.fencing_token
    ];
    if (claimed !== claimFields.every((item) => item !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["claim_token"],
        message: "claim evidence must be complete only after worker claim"
      });
    }
    if ((value.state === "applied") !== (value.applied_at !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["applied_at"],
        message: "only applied work contains applied_at"
      });
    }
    if (claimed && value.claimed_at !== null && value.claim_expires_at !== null) {
      if (Date.parse(value.claim_expires_at) <= Date.parse(value.claimed_at)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["claim_expires_at"],
          message: "worker claim expiry must follow claim time"
        });
      }
      if (value.applied_at !== null && Date.parse(value.applied_at) < Date.parse(value.claimed_at)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["applied_at"],
          message: "worker application cannot predate its claim"
        });
      }
    }
  });

export const unknownResolutionApplicationReceiptV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    application_id: uuidSchema,
    terminal_observation_id: uuidSchema,
    terminal_status: z.enum(["filled", "canceled", "expired", "rejected"]),
    final_cumulative_quantity: safeNonnegativeIntegerSchema,
    final_cumulative_gross_krw: safeNonnegativeIntegerSchema,
    final_cumulative_commission_krw: safeNonnegativeIntegerSchema,
    final_cumulative_tax_krw: safeNonnegativeIntegerSchema,
    command_revision: safeNonnegativeIntegerSchema,
    work_revision: safeNonnegativeIntegerSchema,
    applied_at: isoTimestampSchema,
    application_sha256: sha256Schema
  })
  .strict();

export const unknownResolutionCaseContextV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    break_id: uuidSchema,
    intent_id: uuidSchema,
    environment: operationEnvironmentSchema,
    symbol: koreanStockSymbolSchema,
    side: z.enum(["buy", "sell"]),
    requested_quantity: safePositiveIntegerSchema,
    limit_price_krw: safePositiveIntegerSchema,
    break_state: z.enum(["open", "resolution_requested", "resolved"]),
    break_revision: safeNonnegativeIntegerSchema,
    break_reason_code: z.string().trim().min(1).max(160),
    detected_at: isoTimestampSchema,
    resolved_at: nullableTimestampSchema,
    reconciliation_state: z.enum(["pending", "leased", "complete", "manual"]),
    reconciliation_updated_at: isoTimestampSchema,
    cash_projection_version: safeNonnegativeIntegerSchema,
    position_projection_version: safeNonnegativeIntegerSchema.nullable(),
    reservation_event_sequence: safePositiveIntegerSchema,
    control_epoch: safePositiveIntegerSchema,
    provider_identity: unknownResolutionProviderIdentityV2Schema,
    unknown_observation: unknownExecutionObservationV2Schema,
    request: unknownResolutionRequestContextV2Schema.nullable(),
    review: unknownResolutionReviewContextV2Schema.nullable(),
    work_receipt: unknownResolutionWorkReceiptV2Schema.nullable(),
    application_receipt: unknownResolutionApplicationReceiptV2Schema.nullable(),
    postcondition: z
      .object({
        schema_version: schemaVersionV2Schema,
        resolution_complete: z.boolean(),
        accounting_application_recorded: z.boolean()
      })
      .strict()
  })
  .strict()
  .superRefine((value, context) => {
    const paperIdentity = value.environment === "paper" &&
      value.provider_identity.broker === "internal_paper" &&
      value.provider_identity.provider_contract_version === null &&
      value.provider_identity.provider_openapi_sha256 === null;
    const contractIdentity = value.environment === "contract_test" &&
      value.provider_identity.broker === "local_contract_simulator" &&
      value.provider_identity.provider_contract_version !== null &&
      value.provider_identity.provider_openapi_sha256 !== null;
    if (!paperIdentity && !contractIdentity) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["provider_identity", "broker"],
        message: "environment and provider contract identity must agree"
      });
    }
    if (value.unknown_observation.cumulative_quantity > value.requested_quantity) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["unknown_observation", "cumulative_quantity"],
        message: "unknown cumulative quantity cannot exceed the requested quantity"
      });
    }
    if (value.request === null && (value.review !== null || value.work_receipt !== null || value.application_receipt !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["request"],
        message: "review and worker receipts require a request"
      });
    }
    if (value.request === null && value.break_state !== "open") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["break_state"],
        message: "a break without a V2 request must remain open"
      });
    }
    if (value.request !== null) {
      value.request.missing_fills.forEach((fill, index) => {
        if (fill.fill_sequence !== index + 1 || fill.provider_order_id !== value.provider_identity.provider_order_id) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            path: ["request", "missing_fills", index],
            message: "stored fill manifest must be ordered and preserve provider identity"
          });
        }
      });
      if (value.review !== null && (
        value.review.request_digest_sha256 !== value.request.request_digest_sha256 ||
        value.review.evidence_sha256 !== value.request.evidence_sha256
      )) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["review"],
          message: "review must bind the immutable request and evidence digests"
        });
      }
      if (
        value.review !== null &&
        Date.parse(value.review.reviewed_at) < Date.parse(value.request.requested_at)
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["review", "reviewed_at"],
          message: "review cannot predate its request"
        });
      }
      if (
        value.application_receipt !== null &&
        value.review !== null &&
        Date.parse(value.application_receipt.applied_at) < Date.parse(value.review.reviewed_at)
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["application_receipt", "applied_at"],
          message: "application cannot predate independent review"
        });
      }
      const state = value.request.state;
      const lifecycleMatches = state === "requested"
        ? value.break_state === "resolution_requested" &&
          value.review === null && value.work_receipt === null && value.application_receipt === null
        : state === "rejected"
          ? value.break_state === "open" &&
            value.review?.decision === "rejected" &&
            value.work_receipt === null && value.application_receipt === null
          : state === "approved"
            ? value.break_state === "resolution_requested" &&
              value.review?.decision === "approved" &&
              value.work_receipt?.state === "approved" &&
              value.application_receipt === null
            : state === "claimed"
              ? value.break_state === "resolution_requested" &&
                value.review?.decision === "approved" &&
                value.work_receipt?.state === "claimed" &&
                value.application_receipt === null
              : state === "applied"
                ? value.break_state === "resolved" &&
                  value.review?.decision === "approved" &&
                  value.work_receipt?.state === "applied" &&
                  value.application_receipt !== null
                : value.break_state === "open" &&
                  value.work_receipt === null && value.application_receipt === null;
      if (!lifecycleMatches) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["request", "state"],
          message: "request state must match break, review, worker, and application receipts"
        });
      }
    }
    const completed = value.break_state === "resolved" &&
      value.reconciliation_state === "complete" &&
      value.request?.state === "applied" &&
      value.review?.decision === "approved" &&
      value.work_receipt?.state === "applied" &&
      value.application_receipt !== null;
    if (
      value.postcondition.resolution_complete !== completed ||
      value.postcondition.accounting_application_recorded !== (value.application_receipt !== null)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["postcondition"],
        message: "resolution postcondition must agree with every immutable receipt"
      });
    }
  });

export const unknownResolutionSnapshotV2Schema = z
  .object({
    schema_version: schemaVersionV2Schema,
    generated_at: isoTimestampSchema,
    cases: z.array(unknownResolutionCaseContextV2Schema).max(100)
  })
  .strict();

export const operationsSnapshotSchema = z
  .object({
    schema_version: schemaVersionV1Schema,
    generated_at: isoTimestampSchema,
    runtime_health: runtimeHealthSchema,
    access: accessContextSchema,
    access_changes: z.array(accessChangeReceiptSchema),
    qualification: qualificationSchema.nullable(),
    commands: z.array(operationCommandReceiptSchema),
    pending_reviews: z.array(operationCommandReceiptSchema),
    reviews: z.array(commandReviewSchema),
    incidents: z.array(incidentSchema),
    orders: z.array(orderReadModelSchema),
    positions: z.array(positionReadModelSchema),
    audit_events: z.array(auditEventSchema),
    reconciliation_cases: z.array(reconciliationCaseSchema)
  })
  .strict();

export class DataContractError extends Error {
  readonly source: string;
  readonly issues: readonly { readonly code: string; readonly path: string }[];

  constructor(source: string, error: z.ZodError) {
    super(`데이터 계약 검증에 실패했습니다: ${source}`);
    this.name = "DataContractError";
    this.source = source;
    this.issues = error.issues.map((issue) => ({
      code: issue.code,
      path: issue.path.map(String).join(".")
    }));
  }
}

export function parseDataContract<TSchema extends z.ZodTypeAny>(
  schema: TSchema,
  value: unknown,
  source: string
): z.infer<TSchema> {
  const result = schema.safeParse(value);
  if (!result.success) {
    throw new DataContractError(source, result.error);
  }
  return result.data;
}

export const CommandRequestV1Schema = operationCommandRequestSchema;
export const CommandReviewV1Schema = commandReviewRequestSchema;
export const CommandReceiptV1Schema = operationCommandReceiptSchema;
export const RuntimeStatusV1Schema = runtimeHealthSchema;
export const ConnectionHealthV1Schema = runtimeComponentHealthSchema;
export const OrderSummaryV1Schema = orderReadModelSchema;
export const PositionSummaryV1Schema = positionReadModelSchema;
export const IncidentSummaryV1Schema = incidentSchema;
export const AuditSummaryV1Schema = auditEventSchema;
export const AccessProfileV1Schema = accessContextSchema;
export const QualificationSnapshotV1Schema = qualificationSchema;
export const OperationsSnapshotV1Schema = operationsSnapshotSchema;
export const UnknownResolutionRequestDraftV2Schema = unknownResolutionRequestDraftV2Schema;
export const UnknownResolutionReviewDraftV2Schema = unknownResolutionReviewDraftV2Schema;
export const UnknownResolutionStepUpRequestV2Schema = unknownResolutionStepUpGrantDraftRequestV2Schema;
export const UnknownResolutionStepUpReceiptV2Schema = unknownResolutionStepUpGrantV2Schema;
export const UnknownResolutionRequestV2Schema = unknownResolutionRequestV2Schema;
export const UnknownResolutionReviewV2Schema = unknownResolutionReviewV2Schema;
export const UnknownResolutionReceiptV2Schema = unknownResolutionReceiptV2Schema;
export const UnknownResolutionContextV2Schema = unknownResolutionCaseContextV2Schema;
export const UnknownResolutionSnapshotV2Schema = unknownResolutionSnapshotV2Schema;

export type CommandRequestV1 = z.infer<typeof CommandRequestV1Schema>;
export type CommandReviewV1 = z.infer<typeof CommandReviewV1Schema>;
export type CommandReceiptV1 = z.infer<typeof CommandReceiptV1Schema>;
export type RuntimeStatusV1 = z.infer<typeof RuntimeStatusV1Schema>;
export type ConnectionHealthV1 = z.infer<typeof ConnectionHealthV1Schema>;
export type OrderSummaryV1 = z.infer<typeof OrderSummaryV1Schema>;
export type PositionSummaryV1 = z.infer<typeof PositionSummaryV1Schema>;
export type IncidentSummaryV1 = z.infer<typeof IncidentSummaryV1Schema>;
export type AuditSummaryV1 = z.infer<typeof AuditSummaryV1Schema>;
export type AccessProfileV1 = z.infer<typeof AccessProfileV1Schema>;
export type QualificationSnapshotV1 = z.infer<typeof QualificationSnapshotV1Schema>;
export type OperationsSnapshotV1 = z.infer<typeof OperationsSnapshotV1Schema>;
export type UnknownResolutionRequestDraftV2 = z.infer<typeof UnknownResolutionRequestDraftV2Schema>;
export type UnknownResolutionReviewDraftV2 = z.infer<typeof UnknownResolutionReviewDraftV2Schema>;
export type UnknownResolutionStepUpRequestV2 = z.infer<typeof UnknownResolutionStepUpRequestV2Schema>;
export type UnknownResolutionStepUpReceiptV2 = z.infer<typeof UnknownResolutionStepUpReceiptV2Schema>;
export type UnknownResolutionRequestV2 = z.infer<typeof UnknownResolutionRequestV2Schema>;
export type UnknownResolutionReviewV2 = z.infer<typeof UnknownResolutionReviewV2Schema>;
export type UnknownResolutionReceiptV2 = z.infer<typeof UnknownResolutionReceiptV2Schema>;
export type UnknownResolutionContextV2 = z.infer<typeof UnknownResolutionContextV2Schema>;
export type UnknownResolutionSnapshotV2 = z.infer<typeof UnknownResolutionSnapshotV2Schema>;
export type UnknownResolutionMissingFillV2 = z.infer<typeof unknownResolutionMissingFillV2Schema>;

export type AccessContext = z.infer<typeof accessContextSchema>;
export type OperationsRole = z.infer<typeof operationsRoleSchema>;
export type AccessChangeRequestDraft = z.infer<typeof accessChangeRequestDraftSchema>;
export type AccessChangeReviewDraft = z.infer<typeof accessChangeReviewDraftSchema>;
export type AccessStepUpGrantDraftRequest = z.infer<typeof accessStepUpGrantDraftRequestSchema>;
export type AccessStepUpGrantIssueResponse = z.infer<typeof accessStepUpGrantIssueResponseSchema>;
export type AccessChangeRequest = z.infer<typeof accessChangeRequestSchema>;
export type AccessChangeReviewRequest = z.infer<typeof accessChangeReviewSchema>;
export type AccessChangeReceipt = z.infer<typeof accessChangeReceiptSchema>;
export type ActorRef = z.infer<typeof actorRefSchema>;
export type AuditEvent = z.infer<typeof auditEventSchema>;
export type CommandReview = z.infer<typeof commandReviewSchema>;
export type CommandReviewRequest = z.infer<typeof commandReviewRequestSchema>;
export type ControlPlaneReceipt = z.infer<typeof controlPlaneReceiptSchema>;
export type Incident = z.infer<typeof incidentSchema>;
export type IncidentActionRequest = z.infer<typeof incidentActionRequestSchema>;
export type IncidentActionReceipt = z.infer<typeof incidentActionReceiptSchema>;
export type OperationCommandReceipt = z.infer<typeof operationCommandReceiptSchema>;
export type OperationCommandRequest = z.infer<typeof operationCommandRequestSchema>;
export type OperationsSnapshot = z.infer<typeof operationsSnapshotSchema>;
export type Qualification = z.infer<typeof qualificationSchema>;
export type ReconciliationCase = z.infer<typeof reconciliationCaseSchema>;
export type RuntimeHealth = z.infer<typeof runtimeHealthSchema>;
export type WorkerCommandAck = z.infer<typeof workerCommandAckSchema>;
export type StepUpCommandDraft = z.infer<typeof stepUpCommandDraftSchema>;
export type CommandReviewDraft = z.infer<typeof commandReviewDraftSchema>;
export type StepUpGrantDraftRequest = z.infer<typeof stepUpGrantDraftRequestSchema>;
export type StepUpGrantIssueResponse = z.infer<typeof stepUpGrantIssueResponseSchema>;

export function commandDraftForHash(request: OperationCommandRequest): StepUpCommandDraft {
  if (request.command_type === "emergency_stop") {
    throw new Error("emergency_stop_does_not_use_step_up_grant");
  }
  if (request.command_type === "pause_paper") {
    return pausePaperCommandDraftSchema.parse({
      schema_version: request.schema_version,
      request_id: request.request_id,
      environment: request.environment,
      idempotency_key: request.idempotency_key,
      expected_state_version: request.expected_state_version,
      requested_at: request.requested_at,
      expires_at: request.expires_at,
      command_type: request.command_type,
      reason_code: request.reason_code
    });
  }
  return qualifiedCommandDraftSchema.parse({
    schema_version: request.schema_version,
    request_id: request.request_id,
    environment: request.environment,
    idempotency_key: request.idempotency_key,
    expected_state_version: request.expected_state_version,
    requested_at: request.requested_at,
    expires_at: request.expires_at,
    command_type: request.command_type,
    qualification_id: request.qualification_id,
    strategy_version_id: request.strategy_version_id,
    risk_policy_version_id: request.risk_policy_version_id,
    release_sha: request.release_sha,
    ledger_checkpoint: request.ledger_checkpoint,
    reason_code: request.reason_code
  });
}

export function reviewDraftForHash(review: CommandReviewRequest): CommandReviewDraft {
  return commandReviewDraftSchema.parse({
    schema_version: review.schema_version,
    review_id: review.review_id,
    command_id: review.command_id,
    command_type: review.command_type,
    reviewer_role: review.reviewer_role,
    decision: review.decision,
    reason_code: review.reason_code,
    expected_receipt_revision: review.expected_receipt_revision,
    reviewed_at: review.reviewed_at
  });
}
