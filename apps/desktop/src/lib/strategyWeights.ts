export const strategyWeightFields = [
  { key: "technical", label: "기술적 분석" },
  { key: "fundamental", label: "펀더멘털" },
  { key: "market_sector", label: "시장·섹터" },
  { key: "news_event", label: "뉴스·이벤트" },
  { key: "portfolio", label: "포트폴리오" }
] as const;

export type StrategyWeightKey = (typeof strategyWeightFields)[number]["key"];

export interface StrategyWeightValues {
  readonly technical: number;
  readonly fundamental: number;
  readonly market_sector: number;
  readonly news_event: number;
  readonly portfolio: number;
}

export interface StrategyWeightForm {
  readonly technical: string;
  readonly fundamental: string;
  readonly market_sector: string;
  readonly news_event: string;
  readonly portfolio: string;
}

export type StrategyWeightValidation =
  | {
      readonly ok: true;
      readonly values: StrategyWeightValues;
      readonly sum: number;
      readonly message: null;
    }
  | {
      readonly ok: false;
      readonly values: null;
      readonly sum: number | null;
      readonly message: string;
    };

export const strategyWeightSumTolerance = 1e-6;

export function strategyWeightFormFromRecord(value: Record<string, unknown>): StrategyWeightForm {
  return {
    technical: numericText(value.technical),
    fundamental: numericText(value.fundamental),
    market_sector: numericText(value.market_sector),
    news_event: numericText(value.news_event),
    portfolio: numericText(value.portfolio)
  };
}

export function validateStrategyWeightForm(form: StrategyWeightForm): StrategyWeightValidation {
  const invalidField = strategyWeightFields.find(({ key }) => {
    const input = form[key].trim();
    const value = Number(input);
    return input.length === 0 || !Number.isFinite(value) || value < 0 || value > 1;
  });
  if (invalidField) {
    return {
      ok: false,
      values: null,
      sum: null,
      message: `${invalidField.label} 가중치는 0에서 1 사이의 숫자여야 합니다.`
    };
  }

  const values: StrategyWeightValues = {
    technical: Number(form.technical),
    fundamental: Number(form.fundamental),
    market_sector: Number(form.market_sector),
    news_event: Number(form.news_event),
    portfolio: Number(form.portfolio)
  };
  const sum = strategyWeightFields.reduce((total, { key }) => total + values[key], 0);
  if (Math.abs(sum - 1) > strategyWeightSumTolerance) {
    return {
      ok: false,
      values: null,
      sum,
      message: "가중치 합계는 1이어야 합니다."
    };
  }
  return { ok: true, values, sum, message: null };
}

function numericText(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "";
}
