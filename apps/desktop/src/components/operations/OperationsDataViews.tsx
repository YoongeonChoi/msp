import { useMemo, useState } from "react";
import {
  Activity,
  CircleDollarSign,
  Clock3,
  LockKeyhole,
  Radio,
  ShieldCheck,
  WalletCards
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type { OperationsSnapshot } from "../../lib/operationsContracts";
import { formatKrw, formatKst } from "../../lib/formatters";
import { operationStateLabel } from "../../lib/presentation";
import { runtimeStateLabel } from "./StaleDataBoundary";
import { EmptyState, Panel, Pill, SectionTitle, type Tone } from "../ui";

const orderStatusLabels: Record<OperationsSnapshot["orders"][number]["status"], string> = {
  proposed: "제안됨",
  paper_simulated: "모의 체결",
  contract_simulated: "계약 테스트",
  partial_filled: "일부 체결",
  filled: "체결 완료",
  canceled: "취소",
  expired: "만료",
  rejected: "거절",
  failed: "실패",
  reconciliation_required: "대사 필요"
};

const auditActionLabels: Record<OperationsSnapshot["audit_events"][number]["action"], string> = {
  command_requested: "명령 요청",
  command_reviewed: "명령 검토",
  command_claimed: "Worker 인수",
  command_applied: "명령 적용",
  command_failed: "명령 실패",
  incident_acknowledged: "사고 확인",
  incident_resolved: "사고 해결",
  reconciliation_opened: "대사 시작",
  reconciliation_resolved: "대사 해결",
  access_denied: "접근 거부"
};

const outcomeLabels: Record<OperationsSnapshot["audit_events"][number]["outcome"], string> = {
  success: "성공",
  denied: "거부",
  failed: "실패"
};

function environmentLabel(environment: OperationsSnapshot["runtime_health"]["environment"]): string {
  return environment === "paper" ? "PAPER" : "CONTRACT TEST";
}

function statusTone(value: string): Tone {
  if (["success", "filled", "paper_simulated", "contract_simulated", "applied", "fresh", "available"].includes(value)) {
    return "safe";
  }
  if (["failed", "rejected", "denied", "contract_error", "offline"].includes(value)) {
    return "danger";
  }
  if (["reconciliation_required", "stale", "degraded", "expired", "partial_filled"].includes(value)) {
    return "warning";
  }
  return "neutral";
}

export function OperationsSummaryStrip({
  snapshot,
  mutationsAllowed,
  attentionCount
}: {
  readonly snapshot: OperationsSnapshot;
  readonly mutationsAllowed: boolean;
  readonly attentionCount: number;
}) {
  const worker = snapshot.runtime_health.components.find((component) => component.component === "worker");
  const metrics = [
    {
      label: "운영 환경",
      value: snapshot.runtime_health.live_permitted ? "계약 위반" : environmentLabel(snapshot.runtime_health.environment),
      detail: "실주문 경로 없음",
      icon: LockKeyhole,
      tone: snapshot.runtime_health.live_permitted ? "danger" : "safe"
    },
    {
      label: "실행 상태",
      value: snapshot.runtime_health.execution_enabled ? "실행 중" : "중지",
      detail: runtimeStateLabel(snapshot.runtime_health.overall_state),
      icon: Activity,
      tone: snapshot.runtime_health.overall_state === "fresh" ? "safe" : "warning"
    },
    {
      label: "Worker",
      value: worker ? runtimeStateLabel(worker.state) : "확인 불가",
      detail: snapshot.runtime_health.worker_release_sha?.slice(0, 10) ?? "배포 버전 없음",
      icon: Radio,
      tone: worker?.state === "fresh" ? "safe" : "warning"
    },
    {
      label: "거래 명령",
      value: mutationsAllowed ? "요청 가능" : "차단",
      detail: attentionCount > 0 ? `확인 대기 ${attentionCount}건` : "확인 대기 없음",
      icon: ShieldCheck,
      tone: mutationsAllowed ? "safe" : "warning"
    }
  ] as const;

  return (
    <section className="operations-summary" aria-label="운영 요약">
      <Panel className="operations-identity-card">
        <p className="operations-identity-card__label">OPERATING POSTURE</p>
        <h2>{environmentLabel(snapshot.runtime_health.environment)}</h2>
        <dl>
          <div><dt>상태 버전</dt><dd>r{snapshot.runtime_health.state_version}</dd></div>
          <div><dt>기준 시각</dt><dd>{formatKst(snapshot.runtime_health.as_of)}</dd></div>
          <div><dt>활성 전략</dt><dd>{snapshot.runtime_health.active_strategy_version_id?.slice(0, 8) ?? "없음"}</dd></div>
        </dl>
      </Panel>

      <Panel className="operations-kpi-panel">
        <h2 className="sr-only">핵심 안전 상태</h2>
        <div className="operations-kpi-grid">
          {metrics.map(({ label, value, detail, icon: Icon, tone }) => (
            <article className="operations-kpi" key={label}>
              <span className={`operations-kpi__icon is-${tone}`} aria-hidden="true"><Icon /></span>
              <div>
                <p>{label}</p>
                <strong>{value}</strong>
                <span>{detail}</span>
              </div>
            </article>
          ))}
        </div>
      </Panel>
    </section>
  );
}

export function PortfolioView({ snapshot }: { readonly snapshot: OperationsSnapshot }) {
  const totalMarketValue = snapshot.positions.reduce((total, position) => (
    position.market_data_status === "available" ? total + (position.market_value_krw ?? 0) : total
  ), 0);
  const totalUnrealized = snapshot.positions.reduce((total, position) => (
    position.market_data_status === "available" ? total + (position.unrealized_pnl_krw ?? 0) : total
  ), 0);
  const reconciliationCount = snapshot.orders.filter((order) => order.status === "reconciliation_required").length;

  return (
    <div className="operations-page-stack">
      <section className="compact-metrics" aria-label="주문과 보유 요약">
        <MetricCell icon={WalletCards} label="주문" value={`${snapshot.orders.length}건`} detail={`대사 필요 ${reconciliationCount}건`} />
        <MetricCell icon={CircleDollarSign} label="보유 종목" value={`${snapshot.positions.length}개`} detail="검증된 read model" />
        <MetricCell icon={Activity} label="평가 금액" value={formatKrw(totalMarketValue)} detail="최신 시세 확인분만 합산" />
        <MetricCell icon={ShieldCheck} label="미실현 손익" value={formatKrw(totalUnrealized)} detail="stale·unavailable 제외" tone={totalUnrealized >= 0 ? "safe" : "danger"} />
      </section>

      <Panel className="data-panel">
        <SectionTitle title="주문 원장" detail={<Pill tone="neutral">읽기 전용</Pill>} />
        {snapshot.orders.length === 0 ? (
          <EmptyState title="표시할 주문이 없습니다" detail="모의거래 또는 계약 테스트 주문이 생성되면 이곳에 표시됩니다." />
        ) : (
          <div className="data-table-scroll" tabIndex={0} aria-label="주문 원장 가로 스크롤">
            <table className="data-table">
              <thead><tr><th>종목</th><th>구분</th><th>수량</th><th>지정가</th><th>평균 체결가</th><th>상태</th><th>환경</th><th>갱신 시각</th></tr></thead>
              <tbody>{snapshot.orders.map((order) => (
                <tr key={order.order_id}>
                  <td><strong>{order.symbol}</strong><small>{order.order_id.slice(0, 8)}</small></td>
                  <td>{order.side === "buy" ? "매수" : "매도"}</td>
                  <td>{order.filled_quantity.toLocaleString("ko-KR")} / {order.requested_quantity.toLocaleString("ko-KR")}</td>
                  <td>{formatKrw(order.requested_price_krw)}</td>
                  <td>{formatKrw(order.average_fill_price_krw)}</td>
                  <td><Pill tone={statusTone(order.status)}>{orderStatusLabels[order.status]}</Pill></td>
                  <td>{environmentLabel(order.environment)}</td>
                  <td>{formatKst(order.updated_at)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel className="data-panel">
        <SectionTitle title="보유 현황" detail={<Pill tone="neutral">검증된 평가만 표시</Pill>} />
        {snapshot.positions.length === 0 ? (
          <EmptyState title="표시할 보유 수량이 없습니다" detail="검증된 포지션 read model이 생기면 이곳에 표시됩니다." />
        ) : (
          <div className="data-table-scroll" tabIndex={0} aria-label="보유 현황 가로 스크롤">
            <table className="data-table">
              <thead><tr><th>종목</th><th>수량</th><th>평균가</th><th>시장가</th><th>평가액</th><th>미실현 손익</th><th>시장 데이터</th><th>기준 시각</th></tr></thead>
              <tbody>{snapshot.positions.map((position) => {
                const valuationAvailable = position.market_data_status === "available";
                return (
                  <tr key={position.position_id}>
                    <td><strong>{position.symbol}</strong><small>{position.position_id.slice(0, 8)}</small></td>
                    <td>{position.quantity.toLocaleString("ko-KR")}주</td>
                    <td>{formatKrw(position.average_price_krw)}</td>
                    <td>{valuationAvailable ? formatKrw(position.market_price_krw) : "검증 불가"}</td>
                    <td>{valuationAvailable ? formatKrw(position.market_value_krw) : "검증 불가"}</td>
                    <td className={valuationAvailable && (position.unrealized_pnl_krw ?? 0) >= 0 ? "text-success" : valuationAvailable ? "text-danger" : undefined}>{valuationAvailable ? formatKrw(position.unrealized_pnl_krw) : "검증 불가"}</td>
                    <td><Pill tone={statusTone(position.market_data_status)}>{position.market_data_status === "available" ? "정상" : position.market_data_status === "stale" ? "지연" : "없음"}</Pill></td>
                    <td>{formatKst(position.market_data_as_of ?? position.as_of)}</td>
                  </tr>
                );
              })}</tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

export function RecordsView({ snapshot }: { readonly snapshot: OperationsSnapshot }) {
  const [auditOutcome, setAuditOutcome] = useState<"all" | "success" | "denied" | "failed">("all");
  const filteredAuditEvents = useMemo(
    () => auditOutcome === "all" ? snapshot.audit_events : snapshot.audit_events.filter((event) => event.outcome === auditOutcome),
    [auditOutcome, snapshot.audit_events]
  );

  return (
    <div className="operations-page-stack">
      <section className="operations-records-grid" aria-label="운영 기록 요약">
        <Panel className="record-ledger-panel">
          <SectionTitle title="명령 기록" detail={<Pill tone="neutral">{snapshot.commands.length}건</Pill>} />
          {snapshot.commands.length === 0 ? <EmptyState title="명령 기록이 없습니다" detail="운영 명령이 접수되면 시간 순서로 표시됩니다." /> : (
            <ol className="record-ledger-list">
              {snapshot.commands.map((command) => (
                <li key={command.command_id}>
                  <span className="record-ledger-list__marker" aria-hidden="true" />
                  <div><strong>{command.command_type}</strong><small>{command.command_id}</small></div>
                  <div><Pill tone={statusTone(command.state)}>{operationStateLabel(command.state)}</Pill><small>{formatKst(command.requested_at)}</small></div>
                </li>
              ))}
            </ol>
          )}
        </Panel>

        <Panel className="record-ledger-panel">
          <SectionTitle title="검토 기록" detail={<Pill tone="neutral">{snapshot.reviews.length}건</Pill>} />
          {snapshot.reviews.length === 0 ? <EmptyState title="검토 기록이 없습니다" detail="독립 검토가 완료되면 검토자와 결정이 표시됩니다." /> : (
            <ol className="record-ledger-list">
              {snapshot.reviews.map((review) => (
                <li key={review.review_id}>
                  <span className="record-ledger-list__marker" aria-hidden="true" />
                  <div><strong>{review.decision === "approved" ? "승인" : "거절"}</strong><small>{review.reviewer.display_name}</small></div>
                  <div><Pill tone={review.decision === "approved" ? "safe" : "danger"}>{review.reason_code}</Pill><small>{formatKst(review.reviewed_at)}</small></div>
                </li>
              ))}
            </ol>
          )}
        </Panel>
      </section>

      <Panel className="data-panel">
        <SectionTitle
          title="감사 이벤트"
          detail={(
            <label className="inline-filter">
              <span>결과</span>
              <select aria-label="감사 결과 필터" value={auditOutcome} onChange={(event) => setAuditOutcome(event.target.value as typeof auditOutcome)}>
                <option value="all">전체</option><option value="success">성공</option><option value="denied">거부</option><option value="failed">실패</option>
              </select>
            </label>
          )}
        />
        {filteredAuditEvents.length === 0 ? <EmptyState title="조건에 맞는 감사 이벤트가 없습니다" detail="필터를 변경하거나 새로운 감사 기록을 기다리세요." /> : (
          <div className="data-table-scroll" tabIndex={0} aria-label="감사 이벤트 가로 스크롤">
            <table className="data-table">
              <thead><tr><th>발생 시각</th><th>행동</th><th>사용자</th><th>자원</th><th>결과</th><th>사유</th><th>상관 ID</th></tr></thead>
              <tbody>{filteredAuditEvents.map((event) => (
                <tr key={event.audit_id}>
                  <td>{formatKst(event.occurred_at)}</td>
                  <td>{auditActionLabels[event.action]}</td>
                  <td>{event.actor?.display_name ?? "시스템"}</td>
                  <td><strong>{event.resource_type}</strong><small>{event.resource_id.slice(0, 8)}</small></td>
                  <td><Pill tone={statusTone(event.outcome)}>{outcomeLabels[event.outcome]}</Pill></td>
                  <td>{event.reason_code}</td>
                  <td className="font-mono">{event.correlation_id.slice(0, 12)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

export function RuntimeHealthView({ snapshot }: { readonly snapshot: OperationsSnapshot }) {
  const health = snapshot.runtime_health;
  return (
    <div className="operations-page-stack">
      <section className="compact-metrics" aria-label="런타임 요약">
        <MetricCell icon={Activity} label="전체 운영" value={runtimeStateLabel(health.overall_state)} detail={`상태 r${health.state_version}`} tone={statusTone(health.overall_state)} />
        <MetricCell icon={Radio} label="실시간 신호" value={health.realtime_connected ? "연결" : "연결 끊김"} detail={formatKst(health.realtime_last_seen_at)} tone={health.realtime_connected ? "safe" : "warning"} />
        <MetricCell icon={Clock3} label="Worker heartbeat" value={formatKst(health.worker_heartbeat_at)} detail={health.worker_release_sha?.slice(0, 12) ?? "배포 버전 없음"} tone={health.worker_heartbeat_at ? "safe" : "warning"} />
        <MetricCell icon={LockKeyhole} label="LIVE" value={health.live_permitted ? "계약 위반" : "영구 금지"} detail={environmentLabel(health.environment)} tone={health.live_permitted ? "danger" : "safe"} />
      </section>

      <Panel className="data-panel">
        <SectionTitle title="컴포넌트 상태" detail={<Pill tone={statusTone(health.overall_state)}>{runtimeStateLabel(health.overall_state)}</Pill>} />
        <div className="runtime-component-grid">
          {health.components.map((component) => (
            <article key={component.component}>
              <span className={`runtime-component-grid__icon is-${statusTone(component.state)}`}><Activity aria-hidden="true" /></span>
              <div><strong>{component.component}</strong><small>{component.detail_code}</small></div>
              <div><Pill tone={statusTone(component.state)}>{runtimeStateLabel(component.state)}</Pill><small>{formatKst(component.observed_at)}</small></div>
            </article>
          ))}
        </div>
      </Panel>

      <section className="operations-records-grid">
        <Panel className="data-panel">
          <SectionTitle title="신선도 정책" />
          <dl className="description-list">
            <div><dt>Snapshot 최대 지연</dt><dd>{health.freshness_policy.snapshot_max_age_seconds}초</dd></div>
            <div><dt>Worker heartbeat 최대 지연</dt><dd>{health.freshness_policy.worker_heartbeat_max_age_seconds}초</dd></div>
            <div><dt>Realtime 최대 지연</dt><dd>{health.freshness_policy.realtime_max_age_seconds}초</dd></div>
            <div><dt>최종 기준 시각</dt><dd>{formatKst(health.as_of)}</dd></div>
          </dl>
        </Panel>
        <Panel className="data-panel">
          <SectionTitle title="고정된 운영 계약" />
          <dl className="description-list">
            <div><dt>제공자 계약</dt><dd>{health.provider_contract_version ?? "확인 불가"}</dd></div>
            <div><dt>실행 정책</dt><dd>{health.execution_policy_version ?? "확인 불가"}</dd></div>
            <div><dt>전략 버전</dt><dd>{health.active_strategy_version_id?.slice(0, 12) ?? "없음"}</dd></div>
            <div><dt>위험 정책</dt><dd>{health.active_risk_policy_version_id?.slice(0, 12) ?? "없음"}</dd></div>
          </dl>
        </Panel>
      </section>
    </div>
  );
}

function MetricCell({
  icon: Icon,
  label,
  value,
  detail,
  tone = "neutral"
}: {
  readonly icon: LucideIcon;
  readonly label: string;
  readonly value: string;
  readonly detail: string;
  readonly tone?: Tone;
}) {
  return (
    <article className="compact-metric">
      <span className={`compact-metric__icon is-${tone}`} aria-hidden="true"><Icon /></span>
      <div><p>{label}</p><strong>{value}</strong><span>{detail}</span></div>
    </article>
  );
}
