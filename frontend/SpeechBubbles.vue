<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{ text: string; streaming?: boolean; single?: boolean }>();
// Boundaries depend only on the preceding text, so an incoming suffix cannot
// reshuffle already rendered bubbles. Blank lines are the actor's own pauses.
const parts = computed(() => {
  if (props.single) return [props.text];
  const result: string[] = [];
  let start = 0, fenced = false;
  for (let i = 0; i < props.text.length; i++) {
    if (props.text.slice(i, i + 3) === '```') { fenced = !fenced; i += 2; continue; }
    if (fenced) continue;
    const pause = props.text.slice(i, i + 2) === '\n\n';
    if (pause && result.length < 3) {
      const end = i + (pause ? 2 : 1);
      result.push(props.text.slice(start, end));
      start = end;
      if (pause) i++;
    }
  }
  if (start < props.text.length) result.push(props.text.slice(start));
  return result.length ? result : [''];
});
</script>
<template>
  <div class="speech-stack">
    <div v-for="(part, index) in parts" :key="index" class="message-bubble" :class="{ streaming: streaming && index === parts.length - 1 }">{{ part.replace(/\n\n$/, '') }}<span v-if="streaming && index === parts.length - 1" class="stream-caret"></span></div>
  </div>
</template>
<style scoped>
.speech-stack { display: flex; flex-direction: column; align-items: flex-start; gap: 9px; }
.speech-stack .message-bubble { max-width: 100%; overflow-wrap: anywhere; }
</style>
