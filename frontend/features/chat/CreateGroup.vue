<script setup lang="ts">
import {ref,onMounted,onUnmounted,nextTick} from 'vue';
import Icon from '../../shared/Icon.vue';
import {api} from '../../shared/api';
import type {Agent} from '../../shared/types';
import Avatar from '../../shared/Avatar.vue';
defineProps<{agents:Agent[]}>();
const emit=defineEmits<{created:[id:string]}>();
const open=ref(false),title=ref(''),members=ref<string[]>([]),busy=ref(false),error=ref('');
const titleField=ref<HTMLInputElement>(),trigger=ref<HTMLButtonElement>();
async function show(){open.value=true;await nextTick();titleField.value?.focus();}
function close(){if(!busy.value){open.value=false;trigger.value?.focus();}}
function key(event:KeyboardEvent){if(event.key==='Escape'&&open.value)close();}
onMounted(()=>document.addEventListener('keydown',key));
onUnmounted(()=>document.removeEventListener('keydown',key));
async function create(){busy.value=true;error.value='';try{
 const result=await api<{conversationId:string}>('/api/conversations/groups',{title:title.value,memberIds:members.value});
 open.value=false;title.value='';members.value=[];emit('created',result.conversationId);
}catch(e){error.value=(e as Error).message;}finally{busy.value=false;}}
</script>
<template>
<button ref="trigger" class="icon-button" @click="show" aria-label="新建群聊" title="新建群聊"><Icon name="plus" :size="19" /></button>
<Teleport to="body"><div v-if="open" class="modal-scrim" @click.self="close">
 <form class="group-create" role="dialog" aria-modal="true" aria-label="新建群聊" @submit.prevent="create">
 <header><h2>新建群聊</h2><button type="button" class="icon-button" aria-label="关闭新建群聊" :disabled="busy" @click="close"><Icon name="close"/></button></header>
 <label>群名称<input ref="titleField" v-model="title" maxlength="40" required placeholder="给这个群起个名字"></label>
 <fieldset><legend>选择成员</legend><label v-for="a in agents" :key="a.id"><Avatar :agent="a"/><span>{{a.name}}</span><input type="checkbox" v-model="members" :value="a.id"></label></fieldset>
 <p v-if="error" role="alert">{{error}}</p><button class="primary-button" :disabled="busy||!members.length||!title.trim()">{{busy?'正在创建…':'创建群聊'}}</button>
 </form></div></Teleport>
</template>
<style scoped>.group-create{width:min(440px,90vw);max-height:80vh;overflow:auto;padding:24px;background:#f6f9fc;border-radius:var(--radius-panel)}.group-create header{display:flex;justify-content:space-between;align-items:center}fieldset{border:0;padding:16px 0;display:grid;grid-template-columns:1fr 1fr;gap:10px}fieldset label{display:flex;gap:8px;align-items:center;cursor:pointer}fieldset label:has(input:checked){background:#e4f1fb;outline:1px solid #aacfe5}fieldset .avatar{width:28px;height:28px;border-width:2px;font-size:12px}fieldset input{margin-left:auto}input[type=checkbox]{width:auto}</style>
