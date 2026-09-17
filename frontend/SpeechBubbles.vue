<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { enqueueSpeech, liveSpeechIds } from './speechQueue';
const props = defineProps<{ text: string; streaming?: boolean; single?: boolean; self?: boolean; messageId?:string; conversationId?:string }>();
const emit = defineEmits<{ reveal: [] }>();
// History is immediate; live output commits complete sentences without final flush.
const fresh = !!props.messageId && liveSpeechIds.delete(props.messageId);
const queued = fresh && props.text.length <= 180 && !props.single;
const live = ref(!!props.streaming || queued);
let release: (()=>void) | undefined;
let waiting = queued;
const shown = ref<{ text: string; bubble: number }[]>([]);
let timer: ReturnType<typeof setTimeout> | undefined;
function sentences() {
  if (props.single) return [{ text: props.text, bubble: 0 }];
  const result: { text: string; bubble: number }[] = [];
  let start = 0, bubble = 0, fenced = false;
  for (let i = 0; i < props.text.length; i++) {
    if (props.text.slice(i, i + 3) === '```') { fenced = !fenced; i += 2; continue; }
    if (fenced) continue;
    const pause = props.text.slice(i, i + 2) === '\n\n';
    if (pause || /[。！？!?]/.test(props.text[i]!)) {
      const end = i + (pause ? 2 : 1);
      const text = props.text.slice(start, end);
      if (text.trim()) result.push({ text, bubble });
      if (pause) { bubble = Math.min(3, bubble + 1); i++; }
      start = end;
    }
  }
  if (!props.streaming && start < props.text.length) result.push({ text: props.text.slice(start), bubble });
  return result;
}
const readyParts = computed(sentences);
function advance() {
  if (waiting) return;
  timer = undefined;
  const ready = readyParts.value;
  if (!live.value || props.single) { shown.value = ready; return; }
  if (shown.value.length < ready.length) {
    const next = ready[shown.value.length]!;
    shown.value.push(next);
    emit('reveal');
    const pace = localStorage.getItem('azur-speech-pace') || 'natural';
    timer = setTimeout(advance, pace === 'instant' ? 0 : Math.min(pace === 'calm' ? 1400 : 1000, Math.max(450, next.text.length * (pace === 'calm' ? 35 : 24))));
  } else if (!props.streaming) {
    release?.();
  }
}
watch(() => [props.text, props.streaming], () => {
  if (props.streaming) live.value = true;
  if (!timer) advance();
}, { immediate: true });
if (queued) release = enqueueSpeech(props.conversationId || 'chat', done => { release = done; waiting = false; advance(); });
function revealAll() { clearTimeout(timer); waiting = false; shown.value = readyParts.value; release?.(); emit('reveal'); }
const bubbles = computed(() => {
  const result: { id: number; parts: { id: number; text: string }[] }[] = [];
  shown.value.forEach((part, id) => {
    let group = result.find(g => g.id === part.bubble);
    if (!group) { group = { id: part.bubble, parts: [] }; result.push(group); }
    group.parts.push({ id, text: part.text });
  });
  return result;
});
onBeforeUnmount(() => { clearTimeout(timer); release?.(); });
</script>
<template>
  <div class="speech-stack" :class="{ 'speech-stack--self': self }" :aria-busy="streaming || shown.length < readyParts.length || undefined">
    <div v-for="bubble in bubbles" :key="bubble.id" class="message-bubble" :class="{ 'speech-enter': live }">
      <span v-for="part in bubble.parts" :key="part.id" :class="{ 'sentence-enter': live }">{{ part.text }}</span>
    </div>
    <span v-if="streaming && !bubbles.length" class="speech-wait" aria-label="正在组织回复">···</span>
    <button v-if="!streaming && shown.length < readyParts.length" class="text-button" @click="revealAll">立即显示</button>
  </div>
</template>
<style scoped>
.speech-stack { display: flex; flex-direction: column; align-items: flex-start; gap: 9px; }
.speech-stack .message-bubble { max-width: min(100%, 36em); overflow-wrap: anywhere; }
.speech-stack--self { align-items: flex-end; }
.speech-enter { animation: speech-in 180ms ease-out both; transform-origin: left bottom; }
.sentence-enter { animation: sentence-in 180ms ease-out both; }
.speech-wait { padding: 8px 16px; color: #7199aa; }
@keyframes speech-in { from { opacity: 0; transform: translateY(6px) scale(.98); } to { opacity: 1; transform: none; } }
@keyframes sentence-in { from { opacity: 0; } to { opacity: 1; } }
:global(.reduced-motion) .speech-enter, :global(.reduced-motion) .sentence-enter { animation: none; }
@media (prefers-reduced-motion: reduce) { .speech-enter, .sentence-enter { animation: none; } }
</style>
