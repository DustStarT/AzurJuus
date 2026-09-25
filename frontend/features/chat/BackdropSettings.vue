<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import type { Agent } from "../../shared/types";
import Icon from "../../shared/Icon.vue";
import {
  backdropPosition,
  resetBackdropPosition,
  setBackdropPosition,
} from "./backdropPreferences";
import type { BackdropPosition } from "./backdropPreferences";

const props = defineProps<{ actor: Agent }>();
const open = ref(false),
  error = ref("");
const root = ref<HTMLElement>(),
  trigger = ref<HTMLButtonElement>();
const position = computed(() => backdropPosition(props.actor.id));
const controls = [
  { key: "x", label: "水平位置", min: 0, max: 100, start: "左", end: "右" },
  { key: "y", label: "垂直位置", min: 0, max: 100, start: "上", end: "下" },
  {
    key: "zoom",
    label: "背景缩放",
    min: 100,
    max: 200,
    start: "铺满",
    end: "放大",
  },
] as const;
function change(key: keyof BackdropPosition, event: Event) {
  error.value = "";
  try {
    setBackdropPosition(props.actor.id, {
      ...position.value,
      [key]: Number((event.target as HTMLInputElement).value),
    });
  } catch {
    error.value = "已预览，但暂时无法保存背景设置。";
  }
}
function reset() {
  error.value = "";
  try {
    resetBackdropPosition(props.actor.id);
  } catch {
    error.value = "已恢复默认构图，但暂时无法保存。";
  }
}
function outside(event: PointerEvent) {
  if (event.target instanceof Node && !root.value?.contains(event.target))
    open.value = false;
}
function escape(event: KeyboardEvent) {
  if (event.key === "Escape" && open.value) {
    open.value = false;
    trigger.value?.focus();
  }
}
onMounted(() => {
  document.addEventListener("pointerdown", outside);
  document.addEventListener("keydown", escape);
});
onUnmounted(() => {
  document.removeEventListener("pointerdown", outside);
  document.removeEventListener("keydown", escape);
});
</script>

<template>
  <div ref="root" class="backdrop-controls">
    <button
      ref="trigger"
      type="button"
      class="icon-button"
      aria-label="调整背景"
      title="调整背景"
      :aria-expanded="open"
      @click="open = !open"
    >
      <Icon name="image" />
    </button>
    <section v-if="open" class="backdrop-settings" aria-label="聊天背景设置">
      <header>
        <strong>{{ actor.name }} · 聊天背景</strong
        ><button
          type="button"
          class="text-button"
          @click="
            open = false;
            trigger?.focus();
          "
        >
          完成
        </button>
      </header>
      <label v-for="control in controls" :key="control.key">
        <span
          >{{ control.label
          }}<output>{{ position[control.key] }}%</output></span
        >
        <input
          type="range"
          :aria-label="control.label"
          :min="control.min"
          :max="control.max"
          step="1"
          :value="position[control.key]"
          @input="change(control.key, $event)"
        />
        <small
          ><span>{{ control.start }}</span
          ><span>{{ control.end }}</span></small
        >
      </label>
      <p class="muted">
        沿图片可裁切的方向调整位置；想移动更多，可先放大。仅调整当前角色，自动保存到本机。
      </p>
      <button type="button" class="text-button" @click="reset">
        恢复默认构图
      </button>
      <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    </section>
  </div>
</template>

<style scoped>
.backdrop-controls {
  position: relative;
}
.backdrop-settings {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  width: min(300px, calc(100vw - 130px));
  max-height: calc(100dvh - 160px);
  overflow: auto;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: var(--radius-panel);
  background: var(--surface);
  box-shadow: var(--shadow);
  text-align: left;
}
.backdrop-settings header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.backdrop-settings label {
  display: block;
  margin: 10px 0 14px;
}
.backdrop-settings label > span,
.backdrop-settings small {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
}
.backdrop-settings output,
.backdrop-settings small {
  color: var(--muted);
  font-size: 10px;
}
.backdrop-settings input {
  display: block;
  width: 100%;
  margin: 8px 0;
  accent-color: var(--cyan);
}
.backdrop-settings .muted {
  font-size: 11px;
  margin: 12px 0 4px;
}
</style>
