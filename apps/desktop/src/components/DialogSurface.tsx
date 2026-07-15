import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  type RefObject
} from "react";
import { X } from "lucide-react";
import clsx from "clsx";

export type DialogCloseReason = "cancel" | "escape" | "backdrop" | "close-button";

type DialogVariant = "confirm" | "drawer";

export interface DialogSurfaceProps {
  readonly open: boolean;
  readonly variant?: DialogVariant;
  readonly ariaLabelledBy: string;
  readonly ariaDescribedBy?: string;
  readonly initialFocusRef?: RefObject<HTMLElement>;
  readonly allowEscapeClose?: boolean;
  readonly allowBackdropClose?: boolean;
  readonly restoreFocus?: boolean;
  readonly onRequestClose: (reason: DialogCloseReason) => void;
  readonly children: ReactNode;
  readonly className?: string;
  readonly panelClassName?: string;
}

/**
 * Controlled native dialog foundation. Callers own the open state so a close
 * request can be revalidated before the dialog is actually removed.
 */
export function DialogSurface({
  open,
  variant = "confirm",
  ariaLabelledBy,
  ariaDescribedBy,
  initialFocusRef,
  allowEscapeClose = false,
  allowBackdropClose = false,
  restoreFocus = true,
  onRequestClose,
  children,
  className,
  panelClassName
}: DialogSurfaceProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const backdropPointerDownRef = useRef(false);
  const scheduledFocusRef = useRef<number | null>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) {
      return;
    }

    if (open) {
      if (!dialog.open) {
        const activeElement = document.activeElement;
        openerRef.current = activeElement instanceof HTMLElement ? activeElement : null;

        if (typeof dialog.showModal === "function") {
          dialog.showModal();
        } else {
          // JSDOM and older WebViews may not expose showModal. Keeping the
          // fallback here makes the controlled semantics testable and fail-safe.
          dialog.setAttribute("open", "");
        }
      }

      const focusInitialTarget = () => {
        const target = initialFocusRef?.current ?? firstFocusableElement(dialog) ?? dialog;
        target.focus({ preventScroll: true });
      };

      scheduledFocusRef.current = scheduleAfterPaint(focusInitialTarget);
      return () => {
        cancelScheduledAfterPaint(scheduledFocusRef.current);
        scheduledFocusRef.current = null;
      };
    }

    if (dialog.open) {
      if (typeof dialog.close === "function") {
        dialog.close();
      } else {
        dialog.removeAttribute("open");
      }
    }

    if (restoreFocus) {
      const opener = openerRef.current;
      if (opener?.isConnected) {
        opener.focus({ preventScroll: true });
      }
    }
    openerRef.current = null;
  }, [initialFocusRef, open, restoreFocus]);

  useEffect(
    () => () => {
      cancelScheduledAfterPaint(scheduledFocusRef.current);
      const opener = openerRef.current;
      if (restoreFocus && opener?.isConnected) {
        opener.focus({ preventScroll: true });
      }
    },
    [restoreFocus]
  );

  const handleCancel = useCallback(
    (event: React.SyntheticEvent<HTMLDialogElement>) => {
      event.preventDefault();
      if (allowEscapeClose) {
        onRequestClose("escape");
      }
    },
    [allowEscapeClose, onRequestClose]
  );

  const handlePointerDown = useCallback((event: ReactPointerEvent<HTMLDialogElement>) => {
    backdropPointerDownRef.current = event.target === event.currentTarget;
  }, []);

  const handlePointerUp = useCallback(
    (event: ReactPointerEvent<HTMLDialogElement>) => {
      const isBackdropClick = backdropPointerDownRef.current && event.target === event.currentTarget;
      backdropPointerDownRef.current = false;
      if (isBackdropClick && allowBackdropClose) {
        onRequestClose("backdrop");
      }
    },
    [allowBackdropClose, onRequestClose]
  );

  const handleKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDialogElement>) => {
    if (event.key !== "Tab") {
      return;
    }
    // A nested confirm dialog lives inside its parent drawer in the React tree.
    // Stop its Tab event here so the parent drawer does not run a second trap.
    event.stopPropagation();

    const focusableElements = getFocusableElements(event.currentTarget);
    if (focusableElements.length === 0) {
      event.preventDefault();
      event.currentTarget.focus({ preventScroll: true });
      return;
    }

    const first = focusableElements[0];
    const last = focusableElements[focusableElements.length - 1];
    const activeElement = document.activeElement;

    if (event.shiftKey && (activeElement === first || activeElement === event.currentTarget)) {
      event.preventDefault();
      last.focus({ preventScroll: true });
      return;
    }

    if (!event.shiftKey && activeElement === last) {
      event.preventDefault();
      first.focus({ preventScroll: true });
    }
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className={clsx("dialog-surface", className)}
      data-variant={variant}
      aria-labelledby={ariaLabelledBy}
      aria-describedby={ariaDescribedBy}
      aria-modal="true"
      tabIndex={-1}
      onCancel={handleCancel}
      onPointerDown={handlePointerDown}
      onPointerUp={handlePointerUp}
      onKeyDown={handleKeyDown}
    >
      <div className={clsx("dialog-surface__panel", panelClassName)}>{children}</div>
    </dialog>
  );
}

export type ConfirmTone = "primary" | "danger" | "neutral";

export interface ConfirmDialogProps {
  readonly open: boolean;
  readonly title: string;
  readonly description?: ReactNode;
  readonly children?: ReactNode;
  readonly confirmLabel?: string;
  readonly cancelLabel?: string;
  readonly pendingLabel?: string;
  readonly tone?: ConfirmTone;
  readonly confirmDisabled?: boolean;
  readonly pending?: boolean;
  readonly error?: ReactNode;
  readonly onConfirm: () => void | Promise<void>;
  readonly onCancel: () => void;
}

/**
 * Explicit confirmation dialog. Escape and backdrop never submit or dismiss,
 * and the safe cancel action receives initial focus.
 */
export function ConfirmDialog({
  open,
  title,
  description,
  children,
  confirmLabel = "확인",
  cancelLabel = "취소",
  pendingLabel = "처리 중",
  tone = "primary",
  confirmDisabled = false,
  pending = false,
  error,
  onConfirm,
  onCancel
}: ConfirmDialogProps) {
  const titleId = useStableDomId("confirm-title");
  const descriptionId = useStableDomId("confirm-description");
  const cancelButtonRef = useRef<HTMLButtonElement>(null);
  const submittingRef = useRef(false);
  const [internallyPending, setInternallyPending] = useState(false);
  const isPending = pending || internallyPending;

  useEffect(() => {
    if (!open) {
      submittingRef.current = false;
      setInternallyPending(false);
    }
  }, [open]);

  const handleConfirm = useCallback(async () => {
    if (isPending || confirmDisabled || submittingRef.current) {
      return;
    }

    submittingRef.current = true;
    setInternallyPending(true);
    try {
      await onConfirm();
    } finally {
      submittingRef.current = false;
      setInternallyPending(false);
    }
  }, [confirmDisabled, isPending, onConfirm]);

  return (
    <DialogSurface
      open={open}
      variant="confirm"
      ariaLabelledBy={titleId}
      ariaDescribedBy={description ? descriptionId : undefined}
      initialFocusRef={cancelButtonRef}
      allowEscapeClose={false}
      allowBackdropClose={false}
      onRequestClose={() => undefined}
    >
      <header className="dialog-surface__header">
        <div>
          <h2 id={titleId} className="dialog-surface__title">
            {title}
          </h2>
          {description ? (
            <div id={descriptionId} className="dialog-surface__description">
              {description}
            </div>
          ) : null}
        </div>
      </header>
      {children ? <div className="dialog-surface__body">{children}</div> : null}
      {error ? (
        <div className="dialog-surface__error" role="alert" aria-live="assertive">
          {error}
        </div>
      ) : null}
      <footer className="dialog-surface__footer">
        <button
          ref={cancelButtonRef}
          type="button"
          className="dialog-surface__button dialog-surface__button--neutral"
          disabled={isPending}
          onClick={onCancel}
        >
          {cancelLabel}
        </button>
        <button
          type="button"
          className={clsx("dialog-surface__button", confirmToneClasses[tone])}
          disabled={isPending || confirmDisabled}
          aria-busy={isPending}
          onClick={handleConfirm}
        >
          {isPending ? pendingLabel : confirmLabel}
        </button>
      </footer>
    </DialogSurface>
  );
}

interface DrawerSurfaceCommonProps {
  readonly open: boolean;
  readonly title: string;
  readonly description?: ReactNode;
  readonly children: ReactNode;
  readonly footer?: ReactNode;
  readonly closeLabel?: string;
  readonly onRequestClose: (reason: DialogCloseReason) => void;
}

interface ReadOnlyDrawerSurfaceProps extends DrawerSurfaceCommonProps {
  readonly readOnly: true;
  readonly dirty?: never;
  readonly confirmDiscard?: never;
}

interface FormDrawerSurfaceProps extends DrawerSurfaceCommonProps {
  readonly readOnly?: false;
  readonly dirty?: boolean;
  readonly confirmDiscard?: (reason: DialogCloseReason) => boolean | Promise<boolean>;
}

export type DrawerSurfaceProps = ReadOnlyDrawerSurfaceProps | FormDrawerSurfaceProps;

/**
 * Right-side sheet. Only read-only sheets permit Escape/backdrop dismissal.
 * Form sheets require the explicit close button, with dirty changes protected
 * by the caller-provided discard confirmation.
 */
export function DrawerSurface(props: DrawerSurfaceProps) {
  const {
    open,
    title,
    description,
    children,
    footer,
    closeLabel = "상세 닫기",
    onRequestClose
  } = props;
  const titleId = useStableDomId("drawer-title");
  const descriptionId = useStableDomId("drawer-description");
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [autoDirty, setAutoDirty] = useState(false);
  const [discardReason, setDiscardReason] = useState<DialogCloseReason | null>(null);
  const isReadOnly = props.readOnly === true;
  const dirty = !isReadOnly && (props.dirty === true || autoDirty);
  const confirmDiscard = !isReadOnly ? props.confirmDiscard : undefined;
  const requestGuardedClose = useDirtyCloseGuard({
    dirty,
    onRequestClose,
    confirmDiscard: confirmDiscard ?? (dirty ? (reason) => {
      setDiscardReason(reason);
      return false;
    } : undefined)
  });

  useEffect(() => {
    if (!open) {
      setAutoDirty(false);
      setDiscardReason(null);
    }
  }, [open]);

  const handleSurfaceClose = useCallback(
    (reason: DialogCloseReason) => {
      if (isReadOnly) {
        onRequestClose(reason);
      }
    },
    [isReadOnly, onRequestClose]
  );

  return (
    <DialogSurface
      open={open}
      variant="drawer"
      ariaLabelledBy={titleId}
      ariaDescribedBy={description ? descriptionId : undefined}
      initialFocusRef={closeButtonRef}
      allowEscapeClose={isReadOnly}
      allowBackdropClose={isReadOnly}
      onRequestClose={handleSurfaceClose}
    >
      <header className="dialog-surface__header dialog-surface__header--drawer">
        <div className="dialog-surface__heading-copy">
          <h2 id={titleId} className="dialog-surface__title">
            {title}
          </h2>
          {description ? (
            <div id={descriptionId} className="dialog-surface__description">
              {description}
            </div>
          ) : null}
        </div>
        <button
          ref={closeButtonRef}
          type="button"
          className="dialog-surface__icon-button"
          aria-label={closeLabel}
          onClick={() => void requestGuardedClose("close-button")}
        >
          <X size={20} aria-hidden="true" />
        </button>
      </header>
      <div
        className="dialog-surface__body dialog-surface__body--drawer"
        onChangeCapture={() => {
          if (!isReadOnly) setAutoDirty(true);
        }}
        onInputCapture={() => {
          if (!isReadOnly) setAutoDirty(true);
        }}
      >
        {children}
      </div>
      {footer ? <footer className="dialog-surface__footer dialog-surface__footer--drawer">{footer}</footer> : null}
      <ConfirmDialog
        open={discardReason !== null}
        title="입력 내용 삭제"
        description="저장하거나 제출하지 않은 입력이 있습니다. 닫으면 현재 입력은 복구할 수 없습니다."
        confirmLabel="입력 삭제하고 닫기"
        cancelLabel="계속 작성"
        tone="danger"
        onConfirm={() => {
          const reason = discardReason;
          setDiscardReason(null);
          setAutoDirty(false);
          if (reason !== null) onRequestClose(reason);
        }}
        onCancel={() => setDiscardReason(null)}
      />
    </DialogSurface>
  );
}

export interface DirtyCloseGuardOptions {
  readonly dirty: boolean;
  readonly onRequestClose: (reason: DialogCloseReason) => void;
  readonly confirmDiscard?: (reason: DialogCloseReason) => boolean | Promise<boolean>;
}

/**
 * Returns a fail-closed close request. When a form is dirty and no discard
 * confirmation is provided, the request is intentionally ignored.
 */
export function useDirtyCloseGuard({
  dirty,
  onRequestClose,
  confirmDiscard
}: DirtyCloseGuardOptions): (reason: DialogCloseReason) => Promise<boolean> {
  const checkingRef = useRef(false);

  return useCallback(
    async (reason: DialogCloseReason) => {
      if (checkingRef.current) {
        return false;
      }

      if (!dirty) {
        onRequestClose(reason);
        return true;
      }

      if (!confirmDiscard) {
        return false;
      }

      checkingRef.current = true;
      try {
        const shouldDiscard = await confirmDiscard(reason);
        if (!shouldDiscard) {
          return false;
        }
        onRequestClose(reason);
        return true;
      } finally {
        checkingRef.current = false;
      }
    },
    [confirmDiscard, dirty, onRequestClose]
  );
}

const confirmToneClasses: Record<ConfirmTone, string> = {
  primary: "dialog-surface__button--primary",
  danger: "dialog-surface__button--danger",
  neutral: "dialog-surface__button--neutral"
};

const focusableSelector = [
  "a[href]",
  "area[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "iframe",
  "object",
  "embed",
  "[contenteditable='true']",
  "[tabindex]:not([tabindex='-1'])"
].join(",");

function getFocusableElements(root: HTMLElement): HTMLElement[] {
  return [...root.querySelectorAll<HTMLElement>(focusableSelector)].filter(
    (element) =>
      element.closest("dialog:not([open])") === null &&
      element.closest("[hidden], [inert], [aria-hidden='true']") === null
  );
}

function firstFocusableElement(root: HTMLElement): HTMLElement | null {
  return getFocusableElements(root)[0] ?? null;
}

function scheduleAfterPaint(callback: () => void): number {
  if (typeof window.requestAnimationFrame === "function") {
    return window.requestAnimationFrame(callback);
  }
  return window.setTimeout(callback, 0);
}

function cancelScheduledAfterPaint(handle: number | null): void {
  if (handle === null) {
    return;
  }
  if (typeof window.cancelAnimationFrame === "function") {
    window.cancelAnimationFrame(handle);
    return;
  }
  window.clearTimeout(handle);
}

function useStableDomId(prefix: string): string {
  const reactId = useId().replace(/:/g, "");
  return `${prefix}-${reactId}`;
}
