import type { Agent } from "./types";

// Reuse decoded images across keyed components. QWebEngine supplies the disk HTTP cache.
const images = new Map<string, HTMLImageElement>();
export function artworkUrl(source?: string) {
  if (!source) return '';
  try {
    const url = new URL(source);
    if (url.hostname === 'patchwiki.biligame.com' && url.pathname.includes('/thumb/')) {
      url.pathname = url.pathname.replace(/\/thumb\//, '/').replace(/\/\d+px-[^/]+$/, '');
      return url.href;
    }
  } catch { /* User-provided relative assets remain unchanged. */ }
  return source;
}
export function preloadAssets(agents: Agent[]) {
  const urls = [
    ...agents.map((a) => a.avatarUrl),
    ...agents.slice(0, 3).map((a) => artworkUrl(a.illustrationUrl)),
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
