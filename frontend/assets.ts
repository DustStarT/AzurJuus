import type { Agent } from "./types";

// Reuse decoded images across keyed components. QWebEngine supplies the disk HTTP cache.
const images = new Map<string, HTMLImageElement>();
export function preloadAssets(agents: Agent[]) {
  const urls = [
    ...agents.map((a) => a.avatarUrl),
    ...agents.slice(0, 3).map((a) => a.illustrationUrl),
  ];
  for (const url of urls) {
    if (!url || images.has(url)) continue;
    const image = new Image();
    image.decoding = "async";
    image.src = url;
    images.set(url, image);
    void image.decode().catch(() => {});
    if (images.size > 64) images.delete(images.keys().next().value!);
  }
}
