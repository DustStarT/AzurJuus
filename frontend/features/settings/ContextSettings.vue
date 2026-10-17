<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import { api } from '../../shared/api';
const props = defineProps<{ preset?: string; developerMode?: boolean }>();
const emit = defineEmits<{ 'update:preset': [value: 'compact'|'balanced'|'rich'] }>();
type Config = {provider:'local'|'lexical'|'cloud';localModelPath:string;baseUrl:string;model:string;
  modelVersion:string;dimensions:number;contextLimit:number;apiKeyConfigured?:boolean;
  chatTokenizerPath:string;chatTokenizerModel:string};
type Status = {sources:number;chunks:number;embeddedChunks:number;pendingJobs:number;backfillComplete:boolean;
  localModelReady:boolean;degraded:boolean;message:string};
const config=ref<Config>(), status=ref<Status>(), key=ref(''), error=ref(''), busy=ref(false), note=ref('');
let timer:ReturnType<typeof setInterval>|undefined;
async function refresh(){ status.value=await api<Status>('/api/context/status'); }
async function load(){ try { config.value=await api<Config>('/api/context/settings'); await refresh(); }
  catch(e){ error.value=(e as Error).message; } }
async function save(){ if(!config.value)return; busy.value=true;error.value='';note.value='';
  try { const {apiKeyConfigured:_,...values}=config.value;
    config.value=await api<Config>('/api/context/settings',{...values,apiKey:key.value});key.value='';
    note.value='检索配置已保存。';await refresh();
  }catch(e){error.value=(e as Error).message;}finally{busy.value=false;} }
async function action(path:string){busy.value=true;error.value='';note.value=path.includes('prepare')?'正在下载并校验本机模型…':'正在重建索引…';
  try {await api(path,{});await refresh();note.value='已完成，向量由后台继续建立。';}
  catch(e){error.value=(e as Error).message;note.value='';}finally{busy.value=false;} }
onMounted(async()=>{await load();timer=setInterval(()=>{void refresh().catch(()=>{});},5000);});
onUnmounted(()=>clearInterval(timer));
</script>
<template>
  <section class="settings-page context-settings">
    <h3>上下文与检索</h3>
    <label>角色表达与消耗
      <select aria-label="角色表达与消耗" :value="preset || 'balanced'" @change="emit('update:preset',($event.target as HTMLSelectElement).value as 'compact'|'balanced'|'rich')">
        <option value="compact">省流 · 4K 输入目标</option><option value="balanced">标准 · 8K 输入目标</option><option value="rich">完整 · 12K 输入目标</option>
      </select>
    </label>
    <p class="field-hint">表达档位随“保存设置”生效。</p>
    <template v-if="config && props.developerMode">
      <label>检索方式<select aria-label="检索方式" v-model="config.provider"><option value="local">本机混合检索</option><option value="lexical">仅词法检索</option><option value="cloud">云端 embedding 混合检索</option></select></label>
      <template v-if="config.provider==='local'">
        <label>本机模型目录<input v-model="config.localModelPath" placeholder="留空使用内置模型"></label>
        <button type="button" class="soft-button" :disabled="busy" @click="action('/api/context/model/prepare')">下载本机模型</button>
      </template>
      <template v-if="config.provider==='cloud'">
        <p class="field-hint">启用后，资料片段会发送到此接口；使用独立密钥。</p>
        <label>接口地址<input v-model="config.baseUrl" placeholder="https://example.com/v1"></label>
        <label>Embedding 模型<input v-model="config.model"></label>
        <label>模型版本<input v-model="config.modelVersion" placeholder="用于隔离向量缓存"></label>
        <label>向量维度<input v-model.number="config.dimensions" type="number" min="1" max="4096"></label>
        <label>独立密钥<input v-model="key" type="password" autocomplete="off" :placeholder="config.apiKeyConfigured?'已保存，留空保留':'尚未配置'"></label>
      </template>
      <label>聊天模型上下文上限（token）<input v-model.number="config.contextLimit" type="number" min="4096" max="2000000"></label>
      <details><summary>匹配的本地聊天 tokenizer（可选）</summary>
        <label>对应的聊天模型名<input v-model="config.chatTokenizerModel"></label>
        <label>tokenizer.json 路径<input v-model="config.chatTokenizerPath"></label>
        <p class="field-hint">未匹配时显示估算输入 token。</p>
      </details>
      <button type="button" class="primary-button" :disabled="busy" @click="save">保存检索配置</button>
    </template>
    <p v-else class="field-hint">检索使用当前默认配置。需要调整索引、模型或权限时，请先在“我的资料与数据”中启用开发者选项。</p>
    <div v-if="status" class="context-status" aria-live="polite">
      <p>{{status.message}}</p>
      <p>{{status.sources}} 个来源 · {{status.chunks}} 个片段 · {{status.embeddedChunks}} 个向量</p>
      <p>{{status.backfillComplete?'历史回填已完成':'正在回填有效历史'}} · {{status.pendingJobs}} 个待处理来源</p>
      <button v-if="props.developerMode" type="button" class="soft-button" :disabled="busy" @click="action('/api/context/reindex')">重建派生索引</button>
    </div>
    <p v-if="note" role="status">{{note}}</p><p v-if="error" role="alert">{{error}}</p>
  </section>
</template>
<style scoped>
.context-settings{display:grid;gap:12px}.context-settings label{display:grid;gap:6px}.context-settings>button{justify-self:start}.context-status{padding:12px;border:1px solid var(--line);border-radius:var(--radius-panel)}.context-status p{margin:4px 0}input,select{max-width:100%}
</style>
