import { useEffect, useState } from "react";

export function useOnlineStatus(override?: boolean): boolean {
  const [isOnline, setIsOnline] = useState(() => override ?? readNavigatorOnline());

  useEffect(() => {
    if (override !== undefined) {
      setIsOnline(override);
      return;
    }

    const update = () => setIsOnline(readNavigatorOnline());
    window.addEventListener("online", update);
    window.addEventListener("offline", update);
    update();
    return () => {
      window.removeEventListener("online", update);
      window.removeEventListener("offline", update);
    };
  }, [override]);

  return isOnline;
}

function readNavigatorOnline(): boolean {
  return typeof navigator === "undefined" ? true : navigator.onLine;
}
