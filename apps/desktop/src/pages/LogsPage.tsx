import { useQuery } from "@tanstack/react-query";
import { fetchEngineEvents } from "../lib/supabaseData";
import { formatKst } from "../lib/formatters";
import { EmptyState, ErrorState, JsonSummary, LoadingState, Panel, Pill, SectionTitle } from "../components/ui";

export function LogsPage() {
  const events = useQuery({ queryKey: ["engine_events"], queryFn: () => fetchEngineEvents(100), refetchInterval: 60_000 });

  if (events.isLoading) {
    return <LoadingState label="engine_events를 불러오는 중" />;
  }
  if (events.error) {
    return <ErrorState message="engine_events를 읽지 못했습니다." />;
  }

  const rows = events.data ?? [];
  return (
    <Panel>
      <SectionTitle title="Engine Events" />
      {rows.length === 0 ? (
        <EmptyState title="로그 없음" detail="worker event가 저장되면 표시됩니다." />
      ) : (
        <div className="space-y-2">
          {rows.map((event) => (
            <div key={event.id} className="rounded-md border border-line p-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <Pill tone={event.level === "critical" || event.level === "error" ? "danger" : event.level === "warning" ? "warning" : "neutral"}>
                    {event.level}
                  </Pill>
                  <span className="text-sm font-medium text-ink">{event.component}</span>
                </div>
                <span className="text-xs text-muted">{formatKst(event.createdAt)}</span>
              </div>
              <p className="mt-2 text-sm text-ink">{event.message}</p>
              <div className="mt-1"><JsonSummary value={event.details} /></div>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
