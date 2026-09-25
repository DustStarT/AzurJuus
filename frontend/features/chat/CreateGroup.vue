<script setup lang="ts">
import {ref} from 'vue';
import Icon from '../../shared/Icon.vue';
import {api} from '../../shared/api';
import type {Agent} from '../../shared/types';
defineProps<{agents:Agent[]}>();
const emit=defineEmits<{created:[id:string]}>();
const open=ref(false),title=ref(''),members=ref<string[]>([]),busy=ref(false),error=ref('');
async function create(){busy.value=true;error.value='';try{
 const result=await api<{conversationId:string}>('/api/conversations/groups',{title:title.value,memberIds:members.value});
 open.value=false;title.value='';members.value=[];emit('created',result.conversationId);
}catch(e){error.value=(e as Error).message;}finally{busy.value=false;}}
</script>
<template>
<button class="icon-button" @click="open=true" aria-label="新建群聊" title="新建群聊"><Icon name="plus" :size="19" /></button>
<Teleport to="body"><div v-if="open" class="modal-scrim" @click.self="!busy&&(open=false)">
 <form class="group-create" role="dialog" aria-modal="true" aria-label="新建群聊" @submit.prevent="create">
 <header><h2>新建群聊</h2><button type="button" class="text-button" :disabled="busy" @click="open=false">取消</button></header>
 <label>群名称<input v-model="title" maxlength="40" required autofocus></label>
 <fieldset><legend>选择成员</legend><label v-for="a in agents" :key="a.id"><input type="checkbox" v-model="members" :value="a.id">{{a.name}}</label></fieldset>
 <p v-if="error" role="alert">{{error}}</p><button class="primary-button" :disabled="busy||!members.length||!title.trim()">{{busy?'正在创建…':'创建群聊'}}</button>
 </form></div></Teleport>
</template>
<style scoped>.group-create{width:min(440px,90vw);max-height:80vh;overflow:auto;padding:24px;background:var(--surface);border-radius:var(--radius-panel)}.group-create header{display:flex;justify-content:space-between;align-items:center}fieldset{border:0;padding:16px 0;display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:flex;gap:8px;align-items:center}input[type=checkbox]{width:auto}</style>
