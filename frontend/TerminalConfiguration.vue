<script setup lang="ts">
import { ref,onMounted,computed } from 'vue';
import { api } from './api';
import MindPanel from './MindPanel.vue';
import RelationshipGraph from './RelationshipGraph.vue';
type Member={id:string;name:string;faction:string;socialOnly:boolean;systemPrompt:string;avatarUrl?:string};
type Entry={id:string;text:string;source?:string;origin:string;canonicalText?:string};
const emit=defineEmits<{changed:[]}>();
const props=defineProps<{section?:string}>();
const members=ref<Member[]>([]),world=ref<Entry[]>([]),selected=ref(''),error=ref(''),notice=ref('');
const currentMember=computed(()=>members.value.find(m=>m.id===selected.value));
const settings=ref({maxTaskMembers:12,secretaryAgentId:'',worldOverrides:{} as Record<string,string>});
const editing=ref(''),text=ref('');
const loading=ref(true);
async function load(){loading.value=true;try{
 const data=await api<{members:Member[];world:Entry[];settings:typeof settings.value}>('/api/terminal/settings');
 members.value=data.members;world.value=data.world;settings.value=data.settings;
 if(!selected.value)selected.value=members.value[0]?.id||'';
}catch(e){error.value=(e as Error).message;}finally{loading.value=false;}}
async function save(){try{await api('/api/terminal/settings',settings.value);notice.value='设置已保存';await load();emit('changed');}catch(e){error.value=(e as Error).message;}}
async function permission(member:Member,enabled:boolean){try{await api(`/api/actors/${member.id}/task-permission`,{enabled});await load();emit('changed');}catch(e){error.value=(e as Error).message;}}
async function profile(member:Member){try{await api(`/api/actors/${member.id}/profile`,{name:member.name,faction:member.faction,avatarUrl:member.avatarUrl||''});await api('/api/agents/persona',{agentId:member.id,systemPrompt:member.systemPrompt});notice.value='人物资料已保存';emit('changed');}catch(e){error.value=(e as Error).message;}}
onMounted(load);
</script>
<template>
 <section aria-label="人物关系与任务配置">
 <h3>{{props.section==='profiles'?'角色资料与心智':props.section==='tasks'?'任务参与':'关系与世界观'}}</h3>
 <p v-if="error" role="alert">{{error}}</p><p role="status">{{notice}}</p>
 <template v-if="props.section==='tasks'">
 <p>已建档 {{members.length}} 人。任务参与人数由秘书按复杂度选择，工具执行仍最多两人同时进行。</p>
 <label>任务秘书<select v-model="settings.secretaryAgentId" :disabled="loading"><option v-for="member in members.filter(m=>!m.socialOnly)" :key="member.id" :value="member.id">{{member.name}}</option></select></label>
 <label>每个协作任务的成员上限<input type="number" min="1" max="24" :disabled="loading" v-model.number="settings.maxTaskMembers"></label>
 <button class="soft-button" :disabled="loading" @click="save">保存任务设置</button>
 <label v-for="member in members" :key="member.id"><input type="checkbox" :checked="!member.socialOnly" @change="permission(member,($event.target as HTMLInputElement).checked)">{{member.name}} · 允许参与工具任务</label>
 </template>
 <template v-if="props.section==='profiles'">
 <label>查看角色<select v-model="selected"><option v-for="member in members" :key="member.id" :value="member.id">{{member.name}} · {{member.faction}}</option></select></label>
 <article v-for="member in currentMember?[currentMember]:[]" :key="member.id" class="member-config">
  <p>人物背景用于塑造性格与判断。此处保存后立即生效。</p>
  <div v-if="selected===member.id">
   <details open><summary>角色卡</summary><label>显示称呼<input v-model="member.name" maxlength="80"></label><label>阵营<input v-model="member.faction" maxlength="80"></label><label>头像图片地址<input v-model="member.avatarUrl"></label><label>人物背景与表达设定<textarea v-model="member.systemPrompt" rows="8"/></label><button class="soft-button" @click="profile(member)">保存人物资料</button></details>
   <details><summary>心智、记忆与关系判断</summary><MindPanel :key="member.id" :actor-id="member.id"/></details>
  </div>
 </article>
 </template>
 <template v-if="!props.section || props.section==='world'">
 <RelationshipGraph />
 <details><summary>世界观与原作背景</summary><p>修改保存在用户配置中，恢复时使用原作基础条目；不会改写已发生的任务事实。</p>
 <details v-for="entry in world" :key="entry.id">
  <summary>{{entry.text.slice(0,48)}}</summary><p>{{entry.text}}</p>
  <a v-if="entry.source" :href="entry.source" target="_blank" rel="noreferrer">查看资料出处</a>
  <button class="text-button" @click="editing=entry.id;text=entry.text">编辑</button>
  <button v-if="entry.canonicalText!==undefined" class="text-button" @click="delete settings.worldOverrides[entry.id];save()">恢复基础设定</button>
  <div v-if="editing===entry.id"><textarea v-model="text" rows="4" maxlength="2000"/><button class="soft-button" @click="settings.worldOverrides[entry.id]=text;save();editing=''">保存背景</button></div>
 </details>
 </details>
 </template>
 </section>
</template>
<style scoped>.member-config{border-bottom:1px solid #d9e6ed;padding:12px 0}details{padding:8px 0}textarea{width:100%}label{display:block;margin:8px 0}</style>
