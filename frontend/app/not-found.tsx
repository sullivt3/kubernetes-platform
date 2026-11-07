import Link from "next/link";

import { EmptyState } from "@/components/common/page";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <EmptyState
      className="mt-12"
      title="Page not found"
      description="The page you requested does not exist."
      action={
        <Button asChild variant="outline" size="sm">
          <Link href="/">Back to overview</Link>
        </Button>
      }
    />
  );
}
