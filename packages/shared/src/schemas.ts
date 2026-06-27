import { z } from "zod";

export const tradingModeSchema = z.enum(["paper", "live"]);
export const orderStatusSchema = z.enum([
  "proposed",
  "paper",
  "blocked",
  "sent",
  "filled",
  "rejected",
  "failed",
  "unknown_requires_manual_check"
]);

export const botSettingsSchema = z.object({
  enabled: z.boolean().default(false),
  mode: tradingModeSchema.default("paper"),
  live_order_allowed: z.boolean().default(false),
  max_order_amount_krw: z.number().int().positive().max(100000000).default(100000),
  max_daily_loss_pct: z.number().positive().max(0.2).default(0.02),
  max_daily_order_count: z.number().int().positive().max(1000).default(10),
  max_position_pct: z.number().positive().max(1).default(0.1),
  max_sector_pct: z.number().positive().max(1).default(0.3)
});

export type BotSettings = z.infer<typeof botSettingsSchema>;

