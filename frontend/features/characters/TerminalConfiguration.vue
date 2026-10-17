<script setup lang="ts">
import { ref,onMounted,computed } from 'vue';
import { api } from '../../shared/api';
import MindPanel from '../mind/MindPanel.vue';
import RelationshipGraph from './RelationshipGraph.vue';
import Avatar from '../../shared/Avatar.vue';
import type { Agent } from '../../shared/types';
type Member={id:string;name:string;faction:string;socialOnly:boolean;systemPrompt:string;avatarUrl?:string};
type Entry={id:string;text:string;source?:string;origin:string;canonicalText?:string};
const emit=defineEmits<{changed:[]}>();
const props=defineProps<{section?:string}>();
const members=ref<Member[]>([]),world=ref<Entry[]>([]),selected=ref(''),error=ref(''),notice=ref('');
const currentMember=computed(()=>members.value.find(m=>m.id===selected.value));
const settings=ref({maxTaskMembers:12,secretaryAgentId:'',worldOverrides:{} as Record<string,string>});
const editing=ref(''),text=ref('');
const loading=ref(true);
const portrait=(member:Member):Agent=>({...member,handle:member.id,initials:member.name.slice(0,1),favorite:false,capabilities:[]});
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
 <section class="settings-page terminal-settings" aria-label="人物关系与任务配置">
 <h3>{{props.section==='profiles'?'角色资料与心智':props.section==='tasks'?'任务参与':'关系与世界观'}}</h3>
 <p v-if="error" class="error-message" role="alert">{{error}}</p><p v-if="notice" class="save-notice" role="status">{{notice}}</p>
 <p v-if="loading" class="muted" role="status">正在加载…</p>
 <template v-if="props.section==='tasks'">
 <section class="settings-section"><h4>协作安排</h4><div class="form-grid">
   <label>任务秘书<select v-model="settings.secretaryAgentId" :disabled="loading"><option v-for="member in members.filter(m=>!m.socialOnly)" :key="member.id" :value="member.id">{{member.name}}</option></select></label>
   <label>单次任务成员上限<input type="number" min="1" max="24" :disabled="loading" v-model.number="settings.maxTaskMembers"></label>
 </div><div class="section-actions"><button class="primary-button" :disabled="loading" @click="save">保存任务设置</button></div></section>
 <section class="settings-section"><div class="section-heading"><h4>允许参与工具任务</h4><span class="muted">{{members.length}} 位成员</span></div>
 <div class="member-permissions"><label v-for="member in members" :key="member.id" class="member-permission">
   <Avatar :agent="portrait(member)"/><span>{{member.name}}</span><input type="checkbox" :checked="!member.socialOnly" :disabled="loading" @change="permission(member,($event.target as HTMLInputElement).checked)">
 </label></div></section>
 </template>
 <template v-if="props.section==='profiles'">
 <label>查看角色<select v-model="selected"><option v-for="member in members" :key="member.id" :value="member.id">{{member.name}} · {{member.faction}}</option></select></label>
 <article v-for="member in currentMember?[currentMember]:[]" :key="member.id" class="profile-settings">
  <div class="profile-heading"><Avatar :agent="portrait(member)"/><div><h4>{{member.name}}</h4><span class="muted">{{member.faction}}</span></div></div>
  <section class="settings-section"><h4>角色资料</h4><div class="form-grid"><label>显示称呼<input v-model="member.name" maxlength="80"></label><label>阵营<input v-model="member.faction" maxlength="80"></label></div>
    <label>头像地址<input v-model="member.avatarUrl" placeholder="图片 URL"></label>
    <label>人物背景与表达设定<textarea v-model="member.systemPrompt" rows="8"/></label>
    <div class="section-actions"><button class="primary-button" @click="profile(member)">保存人物资料</button></div>
  </section>
  <details class="settings-section"><summary>心智、记忆与关系判断</summary><MindPanel :key="member.id" :actor-id="member.id"/></details>
 </article>
 </template>
 <template v-if="!props.section || props.section==='world'">
 <RelationshipGraph />
 <details class="settings-section"><summary>世界观与原作背景</summary>
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
<style scoped>.profile-settings{display:grid;gap:16px}.profile-heading{display:flex;align-items:center;gap:12px;padding:4px 0}.profile-heading h4{font-size:17px;margin:0 0 3px}.member-permissions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.member-permission{display:flex;align-items:center;gap:9px;padding:10px;border:1px solid var(--line);border-radius:var(--radius-control);background:var(--paper);cursor:pointer}.member-permission input{margin-left:auto}.member-permission .avatar{width:32px;height:32px;border-width:2px;font-size:13px}.member-permission:has(input:checked){border-color:#b6d5e8;background:#f0f7fc}.member-permission span:not(.avatar){overflow:hidden;text-overflow:ellipsis;white-space:nowrap}@media(max-width:600px){.member-permissions{grid-template-columns:1fr}}</style>
