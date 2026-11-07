const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
  ["second", 1],
];

export function formatRelative(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "—";
  const seconds = Math.round((new Date(iso).getTime() - now) / 1000);
  if (Math.abs(seconds) < 5) return "just now";
  for (const [unit, size] of UNITS) {
    if (Math.abs(seconds) >= size || unit === "second") {
      return RELATIVE.format(Math.round(seconds / size), unit);
    }
  }
  return "—";
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour12: false });
}

export function formatDuration(
  startIso: string | null | undefined,
  endIso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!startIso) return "—";
  const end = endIso ? new Date(endIso).getTime() : now;
  const totalSeconds = Math.max(0, Math.round((end - new Date(startIso).getTime()) / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return `${minutes}m ${seconds}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/** "ssh_private_key" -> "SSH private key" */
export function humanize(value: string): string {
  const ACRONYMS: Record<string, string> = {
    ssh: "SSH",
    aws: "AWS",
    gcp: "GCP",
    api: "API",
    cni: "CNI",
    ha: "HA",
    vsphere: "vSphere",
    kubeconfig: "kubeconfig",
  };
  const words = value.replace(/[._-]+/g, " ").split(" ").filter(Boolean);
  return words
    .map((word, index) => {
      const lower = word.toLowerCase();
      if (ACRONYMS[lower]) return ACRONYMS[lower];
      return index === 0 ? lower.charAt(0).toUpperCase() + lower.slice(1) : lower;
    })
    .join(" ");
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}
