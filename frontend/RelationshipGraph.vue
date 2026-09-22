<script setup lang="ts">
import {ref,onMounted,onUnmounted,computed} from 'vue';
import {api} from './api';
type Node={id:string;name:string;faction:string;research:{status?:string;progress?:number;message?:string;error?:string;sourceErrors?:unknown[]}};
type Edge={from:string;to:string;summary:string;background:{text:string;source?:string;quote?:string;origin?:string}[]};
type Batch={id:string;total:number;done:number;progress:number};
const nodes=ref<Node[]>([]),edges=ref<Edge[]>([]),batch=ref<Batch|null>(null),selected=ref(''),error=ref(''),source=ref(''),refreshing=ref(false);
const emit=defineEmits<{select:[string]}>();
async function load(){try{const data=await api<{nodes:Node[];edges:Edge[];batch:Batch|null}>('/api/relationships/graph');nodes.value=data.nodes;edges.value=data.edges;batch.value=data.batch;}catch(e){error.value=(e as Error).message;}}
let timer:number|undefined;
onMounted(()=>{load();timer=window.setInterval(()=>{if(nodes.value.some(n=>['pending','fetching','analyzing','saving'].includes(n.research.status||'')))load();},900)});
onUnmounted(()=>window.clearInterval(timer));
const placed=computed(()=>nodes.value.map((n,i)=>({...n,x:300+225*Math.cos(i/nodes.value.length*2*Math.PI-Math.PI/2),y:180+135*Math.sin(i/nodes.value.length*2*Math.PI-Math.PI/2)})));
const point=(id:string)=>placed.value.find(n=>n.id===id);
function endpoint(edge:Edge){const a=point(edge.from)!,b=point(edge.to)!;const distance=Math.hypot(b.x-a.x,b.y-a.y);return {x:b.x-(b.x-a.x)*25/distance,y:b.y-(b.y-a.y)*25/distance};}
const statuses:Record<string,string>={pending:'等待后台研究',completed:'研究完成',partial:'部分资料未完成，可重试',failed:'研究失败，可重试'};
const current=computed(()=>nodes.value.find(n=>n.id===selected.value));
const visualEdges=computed(()=>{const seen=new Set<string>();return edges.value.filter(e=>{const key=[e.from,e.to].sort().join('|');if(seen.has(key))return false;seen.add(key);return true;});});
const currentEdges=computed(()=>edges.value.filter(e=>e.from===selected.value));
function pick(id:string){selected.value=id;source.value='';error.value='';emit('select',id);}
async function research(){try{await api(`/api/actors/${selected.value}/research-relationships`,{sourceUrls:source.value.trim()?source.value.trim().split('\n'):[]});await load();}catch(e){error.value=(e as Error).message;}}
async function refreshAll(){if(refreshing.value || (batch.value && batch.value.done<batch.value.total))return;refreshing.value=true;error.value='';try{await api('/api/relationships/refresh-all',{});await load();}catch(e){error.value=(e as Error).message;}finally{refreshing.value=false;}}
</script>
<template><section aria-label="人物关系网络"><h3>关系网络</h3><p>仅展示原作与研究资料中有出处的关系。后续聊天、合作和个人判断保留在人物记忆中。</p><button class="text-button" @click="load">刷新关系图</button><button class="soft-button" :disabled="refreshing || !!batch && batch.done<batch.total" @click="refreshAll">{{refreshing?'正在排队…':batch && batch.done<batch.total?'正在检索…':'全部重新检索'}}</button><p v-if="batch" class="research-progress" role="status">批量研究 {{batch.done}} / {{batch.total}} 人 · {{batch.progress}}%<progress :value="batch.progress" max="100"/></p><p v-if="error" role="alert">{{error}}</p>
<svg viewBox="0 0 600 360" role="group" aria-label="可点击人物关系图">
 <defs><marker id="relationship-arrow" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto"><path d="M0,0 L6,3 L0,6" fill="#6aa9bc"/></marker></defs>
 <line v-for="edge in visualEdges.filter(e=>point(e.from)&&point(e.to))" :key="edge.from+edge.to" :x1="point(edge.from)!.x" :y1="point(edge.from)!.y" :x2="endpoint(edge).x" :y2="endpoint(edge).y" stroke="#9bc0cf" :stroke-dasharray="edge.background.some(b=>b.source)?undefined:'5 4'" :opacity="!selected||edge.from===selected||edge.to===selected?1:.18"/>
 <g v-for="node in placed" :key="node.id" role="button" tabindex="0" :aria-label="'查看'+node.name+'的关系'" @click="pick(node.id)" @keydown.enter="pick(node.id)" @keydown.space.prevent="pick(node.id)"><circle :cx="node.x" :cy="node.y" r="22" :fill="selected===node.id?'#6abbd0':'#edf8fb'" stroke="#5fabc1"/><text :x="node.x" :y="node.y+37" text-anchor="middle">{{node.name}}</text><text :x="node.x" :y="node.y+5" text-anchor="middle">{{node.name.slice(0,1)}}</text></g>
</svg>
<div v-if="current"><strong>{{current.name}} · {{current.faction}}</strong><div class="research-progress" v-if="current.research.status"><progress :value="current.research.progress||0" max="100"/><span>{{current.research.progress||0}}% · {{current.research.message||statuses[current.research.status||'']}}</span></div><p v-if="current.research.error" role="alert">{{current.research.error}}</p><p v-if="current.research.sourceErrors?.length">有 {{current.research.sourceErrors.length}} 个来源暂时未读到，其他来源仍会继续处理。</p><label>补充官方剧情或 Wiki 链接（可选，最多六行）<textarea v-model="source" rows="3"/></label><button class="soft-button" @click="research">更新关系资料</button>
 <div class="relationship-list"><article v-for="edge in currentEdges" :key="edge.from+edge.to"><strong>{{point(edge.to)?.name}}</strong><p>{{edge.summary}}</p><details v-if="edge.background.some(b=>b.source)"><summary>资料出处</summary><div v-for="(item,i) in edge.background.filter(b=>b.source)" :key="i"><blockquote v-if="item.quote">{{item.quote}}</blockquote><a :href="item.source" target="_blank" rel="noreferrer">查看出处</a></div></details></article><p v-if="!currentEdges.length">暂无需要单独展示的关系。</p></div>
</div></section></template>
<style scoped>svg{width:100%;min-height:220px}g[role=button]{cursor:pointer}g:focus circle{stroke-width:4}text{font-size:13px;fill:#35596b}p,small{font-size:13px}textarea{width:100%}.research-progress{display:grid;gap:5px;margin:10px 0}.research-progress progress{width:100%;accent-color:#63bdd2}.relationship-list article{padding:10px 0;border-bottom:1px solid #d9e6ed}.relationship-list p{margin:5px 0;line-height:1.55}details{padding:4px 0}blockquote{margin:6px 0;border-left:2px solid #8cc8d7;padding-left:12px;color:#607887}</style>
