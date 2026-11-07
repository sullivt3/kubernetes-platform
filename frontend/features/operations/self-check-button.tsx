"use client";

import { Stethoscope } from "lucide-react";
import { useRouter } from "next/navigation";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { ApiError } from "@/lib/api/client";
import { useStartSelfCheck } from "@/lib/api/hooks";

/** Starts the real platform self-check workflow and opens its live view. */
export function SelfCheckButton({ variant = "default" }: { variant?: "default" | "outline" }) {
  const router = useRouter();
  const start = useStartSelfCheck();
  return (
    <Button
      variant={variant}
      size="sm"
      disabled={start.isPending}
      onClick={() =>
        start.mutate(undefined, {
          onSuccess: (op) => router.push(`/operations/${op.id}`),
          onError: (error) =>
            toast.error("Could not start self-check", {
              description: error instanceof ApiError ? error.message : String(error),
            }),
        })
      }
    >
      <Stethoscope />
      {start.isPending ? "Starting…" : "Run platform self-check"}
    </Button>
  );
}
