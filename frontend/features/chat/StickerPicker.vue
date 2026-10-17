<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import { api } from '../../shared/api';
import Icon from '../../shared/Icon.vue';
const props = defineProps<{open:boolean}>();
const emit = defineEmits<{ select: [string]; 'update:open':[boolean] }>();
const query = ref(''), error = ref(''), busy = ref(false);
const entries = ref<{label:string;animated:boolean}[]>([]);
const failed = ref(new Set<string>());
const filtered = computed(() => entries.value.filter(e => e.label.includes(query.value.trim())));
async function load(refresh = false) {
  busy.value = true; error.value = '';
  try {
    if (refresh) await api('/api/stickers/refresh', {});
    entries.value = (await api<{stickers:typeof entries.value}>('/api/stickers')).stickers;
    failed.value.clear();
  } catch (e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
watch(() => props.open, value => { if (value && !entries.value.length && !busy.value) void load(); });
</script>
<template>
  <div class="sticker-picker">
    <button type="button" class="composer-tool-button" aria-label="表情包" title="表情包" :aria-expanded="open" @click="emit('update:open',!open)"><Icon name="heart" :size="17" /></button>
    <section v-if="open" aria-label="选择表情包" class="composer-tool-panel">
      <header><input v-model="query" aria-label="搜索表情" placeholder="搜索表情"><button type="button" class="text-button" :disabled="busy" @click="load(true)">更新</button><button type="button" class="icon-button" aria-label="关闭表情包" @click="emit('update:open',false)"><Icon name="close" :size="16" /></button></header>
      <p v-if="error" role="alert">{{error}}</p><p v-if="busy" role="status">正在加载…</p>
      <div class="sticker-grid"><button v-for="entry in filtered" :key="entry.label" type="button" :title="entry.label" :aria-label="entry.label" @click="emit('select',entry.label);emit('update:open',false)">
        <img v-if="!failed.has(entry.label)" :src="'/api/stickers/'+encodeURIComponent(entry.label)+'?still=true'" :alt="entry.label" loading="lazy" @error="failed.add(entry.label)"><span>{{entry.label}}</span>
      </button></div><p v-if="!busy && !filtered.length">没有匹配的表情。</p>
    </section>
  </div>
</template>
<style scoped>
section{width:min(400px,100%);max-width:100%;max-height:min(350px,50vh);overflow:auto;padding:12px}header{display:flex;align-items:center;gap:8px}input{min-width:0;flex:1}.sticker-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-top:10px}.sticker-grid button{display:grid;justify-items:center;padding:4px;border-radius:var(--radius-control);cursor:pointer}.sticker-grid button:hover{background:var(--selected)}.sticker-grid img{width:100%;max-width:64px;height:64px;object-fit:contain}.sticker-grid span{font-size:12px;overflow-wrap:anywhere}p{font-size:13px}
</style>
