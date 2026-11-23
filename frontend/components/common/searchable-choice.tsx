"use client";
import { Check, ChevronsUpDown } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Command, CommandEmpty, CommandInput, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

export function SearchableChoice({ label, value, options, change }: { label: string; value: string; options: { value: string; label: string }[]; change: (value: string) => void }) {
  const [open, setOpen] = useState(false);
  return <div className="space-y-1 text-sm"><span>{label}</span><Popover open={open} onOpenChange={setOpen}><PopoverTrigger asChild><Button type="button" role="combobox" aria-label={label} aria-expanded={open} variant="outline" className="w-full justify-between"><span className="truncate">{options.find((o) => o.value === value)?.label ?? (value || `Select ${label.toLowerCase()}`)}</span><ChevronsUpDown className="size-4 shrink-0" /></Button></PopoverTrigger><PopoverContent className="w-[var(--radix-popover-trigger-width)] p-0" align="start"><Command><CommandInput aria-label={`Search ${label.toLowerCase()}`} placeholder={`Search ${label.toLowerCase()}…`} /><CommandList><CommandEmpty>No matching options</CommandEmpty>{options.map((option) => <CommandItem key={option.value} value={`${option.label} ${option.value}`} onSelect={() => { change(option.value); setOpen(false); }}>{option.label}{value === option.value && <Check className="ml-auto size-4" />}</CommandItem>)}</CommandList></Command></PopoverContent></Popover></div>;
}
