export type DrawerKind = "command" | "approval" | "incident" | "reconciliation" | "mfa" | "access";

export interface DrawerState {
  readonly kind: DrawerKind;
  readonly entityId?: string;
}

export interface ConfirmAction<TKind extends string = string> {
  readonly kind: TKind;
  readonly entityId?: string;
  readonly label: string;
  readonly tone: "primary" | "danger" | "neutral";
}
