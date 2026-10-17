<script setup lang="ts">
import { ref } from 'vue';
import { api } from '../../shared/api';
const props=defineProps<{messageId:string}>();
type Source={sourceId:string;title:string;kind:string;text:string;data:Record<string,unknown>};
const sources=ref<Source[]>(), error=ref('');
const groups=ref<{id:string;title:string;kind:string;archived?:boolean}[]>([]), target=ref(''), note=ref('');
async function load(){if(sources.value)return;try{
  const [result,workspace]=await Promise.all([api<{sources:Source[]}>(`/api/context/messages/${props.messageId}/sources`),
    api<{workspace:{data:{conversations:typeof groups.value}}}>('/api/workspace/load')]);
  sources.value=result.sources;groups.value=workspace.workspace.data.conversations.filter(r=>r.kind==='group' && !r.archived);
}
catch(e){error.value=(e as Error).message;}}
async function share(sourceId:string,revoke=false){if(!target.value)return;error.value='';note.value='';
try{await api(`/api/context/sources/${sourceId}/share`,{conversationId:target.value,revoke});
  note.value=revoke?'已撤销此片段的分享。':'此片段已允许在所选群聊中引用；其他成员不会被标记为亲自阅读。';}
catch(e){error.value=(e as Error).message;}}
</script>
<template><details class="context-sources" @toggle="($event.target as HTMLDetailsElement).open && load()">
  <summary>查看回答来源</summary><p v-if="error">{{error}}</p>
  <p v-if="sources && !sources.length">此消息没有记录外部来源。</p>
  <label v-if="sources?.some(s=>s.kind==='document') && groups.length">分享目标群聊<select v-model="target" aria-label="分享目标群聊"><option value="">请选择</option><option v-for="group in groups" :key="group.id" :value="group.id">{{group.title}}</option></select></label>
  <article v-for="source in sources" :key="source.sourceId"><strong>{{source.title}}</strong>
    <small>{{source.kind==='wiki'?'原作背景':'读取时的历史证据'}} · {{JSON.stringify(source.data.location || {})}}</small>
    <small v-if="source.kind==='wiki'">{{source.data.stale?'旧缓存 / 时间未确认':'已有缓存'}} · {{typeof source.data.fetchedAt==='number'?new Date(source.data.fetchedAt*1000).toLocaleString():'抓取时间未知'}}</small>
    <pre>{{source.text}}</pre>
    <template v-if="source.kind==='document' && groups.length"><button type="button" class="text-button" :disabled="!target" @click="share(source.sourceId)">分享此片段</button><button type="button" class="text-button" :disabled="!target" @click="share(source.sourceId,true)">撤销此片段分享</button></template>
  </article><p v-if="note" role="status">{{note}}</p>
</details></template>
<style scoped>.context-sources{font-size:12px;max-width:36em;color:#587c90}.context-sources summary{cursor:pointer}small{display:block}pre{white-space:pre-wrap;max-height:200px;overflow:auto}</style>
