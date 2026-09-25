<script setup lang="ts">
import { ref, watch } from "vue";
import type { Agent } from "./types";
const props = defineProps<{ agent?: Agent; group?: boolean; hub?:boolean }>();
const failed = ref(false);
watch(
  () => props.agent?.avatarUrl,
  () => (failed.value = false),
);
</script>
<template>
  <span class="avatar" :class="{ 'avatar--group': group }"
    ><img
      v-if="hub"
      src="/resources/ui/port-anchor.svg"
      alt="港区船锚标志"
    /><img
      v-else-if="agent?.avatarUrl && !failed"
      :src="agent.avatarUrl"
      :alt="agent.name"
      loading="lazy"
      decoding="async"
      @error="failed = true"
    /><span v-else>{{
      group ? "❖" : agent?.initials || agent?.name?.slice(0, 1) || "港"
    }}</span></span
  >
</template>
