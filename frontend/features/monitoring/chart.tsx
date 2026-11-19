"use client";

import { useEffect, useRef, useState } from "react";

export type Series = { labels: Record<string, string>; samples: [number, number | null][] };
export type TimeRange = { start: number; end: number; step: number };

const colors = ["#38bdf8", "#4ade80", "#fbbf24", "#c084fc", "#fb7185", "#2dd4bf", "#fb923c", "#a5b4fc"];
const clock = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

function seriesName(labels: Series["labels"]) {
  if (labels.mountpoint) return `${labels.node || labels.instance} ${labels.mountpoint}`;
  if (labels.device) return `${labels.node || labels.instance} ${labels.device}`;
  if (labels.pod) return `${labels.namespace}/${labels.pod}`;
  return labels.node || labels.instance || "Unlabeled series";
}

export function TimeSeriesChart({ title, series, range, format, minimumCeiling = 0 }: {
  title: string; series: Series[]; range: TimeRange; format: (value: number) => string; minimumCeiling?: number;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const { start, end, step } = range;
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const draw = () => {
      const ctx = element.getContext("2d");
      if (!ctx) return;
      const width = element.clientWidth;
      const height = element.clientHeight;
      const ratio = window.devicePixelRatio || 1;
      element.width = width * ratio;
      element.height = height * ratio;
      ctx.scale(ratio, ratio);
      if (width < 120 || height < 50) return;
      const plot = { left: 59, right: width - 9, top: 10, bottom: height - 25 };
      const ceiling = Math.max(minimumCeiling, ...series.flatMap((row) => row.samples.map(([, v]) => v ?? 0)), 0.0001);
      const x = (ts: number) => plot.left + (ts - start) / (end - start) * (plot.right - plot.left);
      const y = (value: number) => plot.bottom - Math.max(0, value) / ceiling * (plot.bottom - plot.top);
      const foreground = getComputedStyle(element).color;
      ctx.font = "11px system-ui";
      ctx.fillStyle = foreground;
      ctx.strokeStyle = foreground;
      for (const fraction of [0, 0.25, 0.5, 0.75, 1]) {
        const value = ceiling * fraction;
        ctx.globalAlpha = 0.23;
        ctx.beginPath();
        ctx.moveTo(plot.left, y(value));
        ctx.lineTo(plot.right, y(value));
        ctx.stroke();
        ctx.globalAlpha = 1;
        ctx.textAlign = "right";
        ctx.fillText(format(value), plot.left - 6, y(value) + 4);
      }
      for (const fraction of [0, 0.5, 1]) {
        const ts = start + fraction * (end - start);
        ctx.textAlign = fraction === 0 ? "left" : fraction === 1 ? "right" : "center";
        ctx.fillText(clock.format(ts * 1000), x(ts), height - 6);
      }
      series.forEach((row, index) => {
        const color = colors[index % colors.length];
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        let previous: number | null = null;
        const isolated: [number, number][] = [];
        row.samples.forEach(([timestamp, value], position) => {
          if (value === null) {
            previous = null;
            return;
          }
          if (previous === null || timestamp - previous > step * 1.5) {
            ctx.moveTo(x(timestamp), y(value));
            const next = row.samples[position + 1];
            if (!next || next[1] === null || next[0] - timestamp > step * 1.5) {
              isolated.push([x(timestamp), y(value)]);
            }
          } else {
            ctx.lineTo(x(timestamp), y(value));
          }
          previous = timestamp;
        });
        ctx.stroke();
        for (const [pointX, pointY] of isolated) {
          ctx.beginPath();
          ctx.arc(pointX, pointY, 2, 0, Math.PI * 2);
          ctx.fill();
        }
      });
      if (cursor !== null) {
        ctx.strokeStyle = foreground;
        ctx.globalAlpha = 0.55;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x(cursor), plot.top);
        ctx.lineTo(x(cursor), plot.bottom);
        ctx.stroke();
      }
    };
    const observer = new ResizeObserver(draw);
    observer.observe(element);
    draw();
    return () => observer.disconnect();
  }, [cursor, end, format, minimumCeiling, series, start, step]);

  return (
    <>
      <canvas
        ref={canvas}
        role="img"
        aria-label={`${title} time series`}
        className="block h-48 w-full text-muted-foreground"
        onPointerLeave={() => setCursor(null)}
        onPointerMove={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          const fraction = (event.clientX - bounds.left - 59) / (bounds.width - 68);
          setCursor(fraction < 0 || fraction > 1 ? null : start + Math.round(fraction * (end - start) / step) * step);
        }}
      />
      <p className="text-xs text-muted-foreground">{cursor === null ? "Latest samples" : clock.format(cursor * 1000)}</p>
      <ul className="divide-y text-sm" aria-label={`${title} values`}>
        {series.map((row, index) => {
          const sample = cursor === null ? row.samples.at(-1) : row.samples.find(([ts]) => ts === cursor);
          return (
            <li className="flex items-center gap-2 py-1.5" key={JSON.stringify(row.labels)}>
              <span className="size-2 shrink-0 rounded-full" style={{ background: colors[index % colors.length] }} aria-hidden />
              <span className="min-w-0 flex-1 truncate" title={seriesName(row.labels)}>{seriesName(row.labels)}</span>
              <span className="shrink-0 tabular-nums text-muted-foreground">{sample?.[1] != null ? format(sample[1]) : "—"}</span>
            </li>
          );
        })}
      </ul>
    </>
  );
}
