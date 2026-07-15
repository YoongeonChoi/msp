import { useOperationsSnapshot } from "../lib/operationsSnapshotContext";
import { OperationsPage } from "./OperationsPage";
import type { OperationsPageProps } from "./OperationsPage";

export type ControlPageProps = Omit<OperationsPageProps, "snapshotSource">;

export function ControlPage(props: ControlPageProps) {
  const source = useOperationsSnapshot();
  return <OperationsPage {...props} snapshotSource={source} />;
}
