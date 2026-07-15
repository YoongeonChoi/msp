import { useEffect, useRef, type ReactNode } from "react";

let sharedObserver: IntersectionObserver | null = null;

function observer(): IntersectionObserver | null {
  if (typeof IntersectionObserver === "undefined") {
    return null;
  }
  sharedObserver ??= new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const element = entry.target as HTMLElement;
        element.classList.remove("is-pending");
        element.classList.add("is-visible");
        element.dataset.revealed = "true";
        sharedObserver?.unobserve(element);
      }
    },
    { threshold: 0.08, rootMargin: "0px 0px -24px" }
  );
  return sharedObserver;
}

export function ReadonlyReveal({ children, className = "" }: { readonly children: ReactNode; readonly className?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = ref.current;
    if (!element || typeof window.matchMedia !== "function") return;
    const preference = window.matchMedia("(min-width: 960px) and (prefers-reduced-motion: no-preference)");

    const apply = () => {
      sharedObserver?.unobserve(element);
      element.classList.remove("is-pending", "is-visible");
      if (!preference.matches || element.dataset.revealed === "true") return;
      const instance = observer();
      if (!instance) return;
      element.classList.add("is-pending");
      instance.observe(element);
    };

    apply();
    preference.addEventListener?.("change", apply);
    return () => {
      preference.removeEventListener?.("change", apply);
      sharedObserver?.unobserve(element);
    };
  }, []);

  return <div ref={ref} className={`readonly-reveal ${className}`}>{children}</div>;
}
