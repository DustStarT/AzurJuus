import { ref } from "vue";

export interface BackdropPosition {
  x: number;
  y: number;
  zoom: number;
}

const storageKey = "azur-chat-background-v1";
const defaults: BackdropPosition = { x: 50, y: 50, zoom: 100 };

function bounded(value: unknown, fallback: number, min: number, max: number) {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.min(max, Math.max(min, value))
    : fallback;
}

function normalize(value: Partial<BackdropPosition>): BackdropPosition {
  return {
    x: bounded(value.x, 50, 0, 100),
    y: bounded(value.y, 50, 0, 100),
    zoom: bounded(value.zoom, 100, 100, 200),
  };
}

function readPositions(): Record<string, BackdropPosition> {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(storageKey) || "{}");
    if (!saved || typeof saved !== "object" || Array.isArray(saved)) return {};
    return Object.fromEntries(
      Object.entries(saved)
        .filter(
          ([, value]) =>
            value && typeof value === "object" && !Array.isArray(value),
        )
        .map(([id, value]) => [id, normalize(value)]),
    );
  } catch {
    return {};
  }
}

const positions = ref(readPositions());

export function backdropPosition(actorId?: string): BackdropPosition {
  return actorId ? positions.value[actorId] || defaults : defaults;
}

export function setBackdropPosition(actorId: string, value: BackdropPosition) {
  // Preview remains usable if browser storage is unavailable; the editor reports it.
  positions.value = { ...positions.value, [actorId]: normalize(value) };
  localStorage.setItem(storageKey, JSON.stringify(positions.value));
}

export function resetBackdropPosition(actorId: string) {
  const next = { ...positions.value };
  delete next[actorId];
  positions.value = next;
  localStorage.setItem(storageKey, JSON.stringify(next));
}
