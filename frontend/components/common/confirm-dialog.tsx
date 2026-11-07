"use client";

import { useState, type ReactNode } from "react";

import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

/**
 * Confirmation for destructive or disruptive actions. When `confirmText` is set the
 * user must type it exactly (the backend independently enforces the same token).
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  confirmText,
  destructive = true,
  pending = false,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: ReactNode;
  confirmLabel: string;
  confirmText?: string;
  destructive?: boolean;
  pending?: boolean;
  onConfirm: () => void;
}) {
  const [typed, setTyped] = useState("");
  const matches = !confirmText || typed === confirmText;

  return (
    <AlertDialog
      open={open}
      onOpenChange={(next) => {
        if (!next) setTyped("");
        onOpenChange(next);
      }}
    >
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription asChild>
            <div className="space-y-2">{description}</div>
          </AlertDialogDescription>
        </AlertDialogHeader>
        {confirmText && (
          <div className="space-y-2">
            <Label htmlFor="confirm-text" className="text-xs text-muted-foreground">
              Type <span className="font-mono font-semibold text-foreground">{confirmText}</span> to confirm
            </Label>
            <Input
              id="confirm-text"
              value={typed}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setTyped(e.target.value)}
            />
          </div>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={pending}>Cancel</AlertDialogCancel>
          <Button
            variant={destructive ? "destructive" : "default"}
            disabled={!matches || pending}
            onClick={onConfirm}
          >
            {pending ? "Working…" : confirmLabel}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
