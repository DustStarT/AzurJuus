import { createApp } from "vue";
import App from "./App.vue";
import "./theme.css";
// Retire old browser copies of backend credentials without discarding preferences.
for (const key of [
  "azurjuus-ui-settings-v1",
  "azurjuus-local-state-v1",
  "azurjuus-ui-session-v1",
]) {
  const raw = localStorage.getItem(key);
  if (!raw) continue;
  try {
    const clean = (value: unknown): unknown =>
      Array.isArray(value)
        ? value.map(clean)
        : value && typeof value === "object"
          ? Object.fromEntries(
              Object.entries(value)
                .filter(
                  ([name]) => !/(api_?key|password|secret|token)$/i.test(name),
                )
                .map(([name, item]) => [name, clean(item)]),
            )
          : value;
    localStorage.setItem(key, JSON.stringify(clean(JSON.parse(raw))));
  } catch {
    localStorage.removeItem(key);
  }
}
createApp(App).mount("#app");
