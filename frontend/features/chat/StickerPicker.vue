<script setup lang="ts">
import { computed, ref } from 'vue';
import { api } from '../../shared/api';
import Icon from '../../shared/Icon.vue';
const emit = defineEmits<{ select: [string] }>();
const open = ref(false), query = ref(''), error = ref(''), busy = ref(false);
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
function toggle() { open.value = !open.value; if (open.value && !entries.value.length) void load(); }
</script>
<template>
  <div class="sticker-picker">
    <button type="button" class="composer-tool-button" aria-label="表情包" title="表情包" :aria-expanded="open" @click="toggle"><Icon name="heart" :size="17" /></button>
    <section v-if="open" aria-label="选择表情包">
      <header><input v-model="query" aria-label="搜索表情" placeholder="搜索表情名称"><button type="button" :disabled="busy" @click="load(true)">更新目录</button><button type="button" @click="open=false">关闭</button></header>
      <p v-if="error" role="alert">{{error}}</p><p v-if="busy" role="status">正在加载…</p>
      <div class="sticker-grid"><button v-for="entry in filtered" :key="entry.label" type="button" :title="entry.label" :aria-label="entry.label" @click="emit('select',entry.label);open=false">
        <img v-if="!failed.has(entry.label)" :src="'/api/stickers/'+encodeURIComponent(entry.label)+'?still=true'" :alt="entry.label" loading="lazy" @error="failed.add(entry.label)"><span>{{entry.label}}</span>
      </button></div><p v-if="!busy && !filtered.length">没有匹配的表情。</p>
    </section>
  </div>
</template>
<style scoped>
.sticker-picker{position:relative}section{position:absolute;bottom:calc(100% + 6px);left:0;width:min(420px,75vw);max-height:350px;overflow:auto;padding:12px;background:#f8fcfe;border:1px solid #c8dfe8;border-radius:10px;z-index:15;box-shadow:0 10px 30px #42698224}header{display:flex;gap:8px}input{min-width:0;flex:1}.sticker-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin-top:10px}.sticker-grid button{display:grid;justify-items:center;padding:4px;border:0;background:transparent;cursor:pointer}.sticker-grid img{width:64px;height:64px;object-fit:contain}.sticker-grid span{font-size:12px;overflow-wrap:anywhere}p{font-size:13px}
</style>
