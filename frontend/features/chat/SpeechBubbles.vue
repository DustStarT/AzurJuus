<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";
import { enqueueSpeech, liveSpeechIds } from './speechQueue';
const props = defineProps<{ text: string; streaming?: boolean; single?: boolean; self?: boolean; messageId?:string; conversationId?:string }>();
const emit = defineEmits<{ reveal: [] }>();
const stickers:Record<string,string>={赞同:'👍',疑惑:'🤔',开心:'😄',困倦:'😴'};
const failedStickers = reactive(new Set<string>());
function pieces(text:string){return text.split(/(\[表情:[^\]\n]{1,32}\])/g).filter(Boolean).map(t=>({text:t,sticker:t.match(/^\[表情:(.+)\]$/)?.[1]}));}
// History is immediate; live output commits complete sentences without final flush.
const fresh = !!props.messageId && liveSpeechIds.delete(props.messageId);
const queued = fresh && props.text.length <= 180 && !props.single;
const live = ref(!!props.streaming || queued);
let release: (()=>void) | undefined;
let waiting = queued;
const shown = ref<{ text: string; bubble: number }[]>([]);
let timer: ReturnType<typeof setTimeout> | undefined;
let wasStreaming = !!props.streaming;
function sentences() {
  if (props.single) return [{ text: props.text, bubble: 0 }];
  if (!wasStreaming && !props.streaming) return props.text.split(/\n\n+/).filter(s => s.trim()).map((text,bubble) => ({text,bubble}));
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
    timer = setTimeout(advance, pace === 'instant' ? 0 : Math.min(1200, Math.max(450, next.text.length * (pace === 'calm' ? 35 : 24))));
  } else if (!props.streaming) {
    release?.();
  }
}
watch(() => [props.text, props.streaming], () => {
  if (props.streaming) { wasStreaming = true; live.value = true; }
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
      <span v-for="part in bubble.parts" :key="part.id" :class="{ 'sentence-enter': live }"><template v-for="(piece,index) in pieces(part.text)" :key="index"><span v-if="piece.sticker==='标枪疑惑'" class="official-sticker"><img class="animated" src="/resources/ui/javelin.gif" alt="标枪疑惑"><img class="still" src="/resources/ui/javelin.png" alt="标枪疑惑"></span><span v-else-if="piece.sticker && stickers[piece.sticker]" class="sticker" role="img" :aria-label="piece.sticker">{{stickers[piece.sticker]}}</span><span v-else-if="piece.sticker && failedStickers.has(piece.sticker)" class="sticker-fallback">{{piece.sticker}}</span><span v-else-if="piece.sticker" class="official-sticker"><img class="animated wiki-sticker" :src="'/api/stickers/'+encodeURIComponent(piece.sticker)" :alt="piece.sticker" loading="lazy" @error="failedStickers.add(piece.sticker)"><img class="still wiki-sticker" :src="'/api/stickers/'+encodeURIComponent(piece.sticker)+'?still=true'" :alt="piece.sticker" loading="lazy" @error="failedStickers.add(piece.sticker)"></span><template v-else>{{piece.text}}</template></template></span>
    </div>
    <span v-if="streaming && !bubbles.length" class="speech-wait" aria-label="正在组织回复">···</span>
    <button v-if="!streaming && shown.length < readyParts.length" class="text-button" @click="revealAll">立即显示</button>
  </div>
</template>
<style scoped>
.speech-stack { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
.sticker { display:inline-block;font-size:48px;line-height:1.3;padding:4px 12px; }
.official-sticker { display:inline-flex; vertical-align:middle; }.official-sticker img{width:110px;height:110px;object-fit:contain}.official-sticker .still{display:none}:global(.reduced-motion) .official-sticker .animated{display:none}:global(.reduced-motion) .official-sticker .still{display:block}
.wiki-sticker { display:inline-block; width:110px; height:110px; object-fit:contain; vertical-align:middle; }
.sticker-fallback { display:inline-block; padding:4px 8px; color:#587c90; }
.speech-stack .message-bubble { max-width: min(100%, 36em); overflow-wrap: anywhere; }
.speech-stack--self { align-items: flex-end; }
.speech-enter { animation: speech-in var(--duration-message) ease-out both; transform-origin: left bottom; }
.sentence-enter { animation: sentence-in var(--duration-message) ease-out both; }
.speech-wait { padding: 8px 16px; color: #7199aa; }
@keyframes speech-in { from { opacity: 0; transform: translateY(6px) scale(.98); } to { opacity: 1; transform: none; } }
@keyframes sentence-in { from { opacity: 0; } to { opacity: 1; } }
:global(.reduced-motion) .speech-enter, :global(.reduced-motion) .sentence-enter { animation: none; }
@media (prefers-reduced-motion: reduce) { .speech-enter, .sentence-enter { animation: none; } }
@media (prefers-reduced-motion: reduce) { .official-sticker .animated { display:none; } .official-sticker .still { display:block; } }
</style>
