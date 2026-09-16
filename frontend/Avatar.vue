<script setup lang="ts">
import { ref, watch } from "vue";
import type { Agent } from "./types";
const props = defineProps<{ agent?: Agent; group?: boolean }>();
const failed = ref(false);
watch(
  () => props.agent?.avatarUrl,
  () => (failed.value = false),
);
</script>
<template>
  <span class="avatar" :class="{ 'avatar--group': group }"
    ><img
      v-if="agent?.avatarUrl && !failed"
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
