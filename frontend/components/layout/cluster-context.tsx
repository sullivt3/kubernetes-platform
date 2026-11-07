"use client";

import { useCallback, useSyncExternalStore } from "react";

const STORAGE_KEY = "kplatform.selectedCluster";
const CHANGE_EVENT = "kplatform:selected-cluster";

function subscribe(onChange: () => void) {
  window.addEventListener("storage", onChange); // other tabs
  window.addEventListener(CHANGE_EVENT, onChange); // this tab
  return () => {
    window.removeEventListener("storage", onChange);
    window.removeEventListener(CHANGE_EVENT, onChange);
  };
}

const getSnapshot = () => window.localStorage.getItem(STORAGE_KEY);
const getServerSnapshot = () => null;

/** The cluster that cluster-scoped pages (nodes, workloads...) operate on; persisted per browser. */
export function useSelectedCluster() {
  const clusterId = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const setClusterId = useCallback((id: string | null) => {
    if (id) window.localStorage.setItem(STORAGE_KEY, id);
    else window.localStorage.removeItem(STORAGE_KEY);
    window.dispatchEvent(new Event(CHANGE_EVENT));
  }, []);
  return { clusterId, setClusterId };
}
