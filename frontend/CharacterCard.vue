<script setup lang="ts">
import { ref, onMounted } from 'vue';
import { api } from './api';
const props = defineProps<{actorId:string}>();
const card = ref<{version:string;text:string;diff:string;hasOverride:boolean;latest:unknown}>();
const error = ref('');
async function load(action?:string) {
  try { card.value = await api(`/api/actors/${props.actorId}/character-card`, action ? {action} : undefined); }
  catch(e) { error.value = (e as Error).message; }
}
onMounted(() => load());
</script>
<template>
  <section class="character-card-editor">
    <h3>终端角色卡</h3>
    <p v-if="error" class="error-message">{{ error }}</p>
    <p>当前生效：{{ card?.version }}{{ card?.hasOverride ? ' · 使用你的人设修改' : '' }}</p>
    <details><summary>查看生效内容与更新差异</summary><pre>{{ card?.text }}</pre><pre>{{ card?.diff }}</pre></details>
    <template v-if="card?.latest">
      <button class="soft-button" @click="load('apply')">应用基础卡更新，保留我的覆盖</button>
      <details><summary>恢复基础版本</summary><p>移除当前自定义覆盖，旧人设备份仍保留。</p><button class="text-button" @click="load('restore')">恢复基础卡</button></details>
    </template>
  </section>
</template>
<style scoped>pre { white-space:pre-wrap; font-size:12px; max-height:260px; overflow:auto; } .character-card-editor { margin:20px 0; }</style>
