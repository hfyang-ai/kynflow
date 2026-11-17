"use client";

import { AlertTriangle, X } from "lucide-react";

export function ErrorBanner({
  message,
  onDismiss,
  className = "",
}: {
  message: string | null;
  onDismiss?: () => void;
  className?: string;
}) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className={`flex items-start gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-xs leading-5 text-rose-700 ${className}`}
    >
      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span className="min-w-0 flex-1 break-words">{message}</span>
      {onDismiss && (
        <button
          type="button"
          onClick={onDismiss}
          aria-label="关闭提示"
          className="shrink-0 rounded p-0.5 text-rose-500 transition hover:bg-rose-100"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  );
}
