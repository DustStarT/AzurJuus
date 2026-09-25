<script setup lang="ts">
import { ref, watch } from 'vue';
import { api } from '../../shared/api';
const props = defineProps<{ conversationId: string }>();
const emit = defineEmits<{ changed: [] }>();
const config = ref({ muted: false, allowInvites: true });
const members = ref<{ id: string; name: string; faction: string }[]>([]);
const directory=ref<typeof members.value>([]),adding=ref('');
const busy = ref(false), error = ref(''), enabled = ref(false);
const social = ref({ paused: false, hourlyCalls: 30 });
const callsLastHour = ref(0);
watch(() => props.conversationId, async cid => {
  error.value = '';
  try {
    const [room, state] = await Promise.all([
      api<{ config: typeof config.value; members: typeof members.value; directory:typeof members.value }>(`/api/conversations/${encodeURIComponent(cid)}/social`),
      api<{ enabled: boolean; settings: typeof social.value; callsLastHour: number }>('/api/social/state'),
    ]);
    if (cid !== props.conversationId) return;
    config.value = room.config; members.value = room.members; enabled.value = state.enabled;
    directory.value=room.directory;
    social.value = state.settings; callsLastHour.value = state.callsLastHour;
  } catch (e) { error.value = (e as Error).message; }
}, { immediate: true });
async function configureGlobal(payload: Partial<typeof social.value>) {
  busy.value = true; error.value = '';
  try {
    const state = await api<{ settings: typeof social.value; callsLastHour: number }>('/api/social/settings', payload);
    social.value = state.settings; callsLastHour.value = state.callsLastHour;
  } catch (e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
async function configure(key: 'muted' | 'allowInvites', value: boolean) {
  busy.value = true; error.value = '';
  try {
    await api(`/api/conversations/${encodeURIComponent(props.conversationId)}/social`, { [key]: value });
    config.value = { ...config.value, [key]: value }; emit('changed');
  } catch (e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
async function remove(id: string) {
  busy.value = true; error.value = '';
  try {
    await api(`/api/conversations/${encodeURIComponent(props.conversationId)}/members/${encodeURIComponent(id)}/remove`, {});
    members.value = members.value.filter(m => m.id !== id); emit('changed');
  } catch (e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
async function add(){busy.value=true;error.value='';try{
 await api(`/api/conversations/${encodeURIComponent(props.conversationId)}/members/${encodeURIComponent(adding.value)}/add`,{});
 members.value.push(directory.value.find(a=>a.id===adding.value)!);adding.value='';emit('changed');
}catch(e){error.value=(e as Error).message;}finally{busy.value=false;}}
</script>

<template>
  <section class="social-controls" aria-label="频道社交设置">
    <label>添加群成员<select v-model="adding" :disabled="busy"><option value="">选择人物</option><option v-for="a in directory.filter(a=>!members.some(m=>m.id===a.id))" :key="a.id" :value="a.id">{{a.name}}</option></select><button class="text-button" :disabled="busy||!adding" @click="add">加入</button></label>
    <p v-if="!enabled" class="muted">自主社交尚未启用；当前使用原有群聊方式。</p>
    <label><input type="checkbox" :checked="config.muted" :disabled="busy || !enabled"
      @change="configure('muted', ($event.target as HTMLInputElement).checked)" />暂停本频道自主交流</label>
    <label><input type="checkbox" :checked="config.allowInvites" :disabled="busy || !enabled"
      @change="configure('allowInvites', ($event.target as HTMLInputElement).checked)" />允许人物邀请同伴</label>
    <details><summary>全局主动交流</summary>
      <label><input type="checkbox" :checked="social.paused" :disabled="busy || !enabled"
        @change="configureGlobal({ paused: ($event.target as HTMLInputElement).checked })" />暂停后台社交与反思</label>
      <label>每小时自主后台调用上限<input type="number" min="1" max="1000" :value="social.hourlyCalls" :disabled="busy || !enabled"
        aria-label="自主后台每小时调用上限" @change="configureGlobal({ hourlyCalls: Number(($event.target as HTMLInputElement).value) })" /></label>
      <p class="muted">近一小时自主后台调用 {{ callsLastHour }} 次；资料调查不计入。重启不重置额度。</p>
    </details>
    <details><summary>移出成员</summary>
      <div v-for="member in members" :key="member.id" class="social-member">
        <span>{{ member.name }} <small>{{ member.faction }}</small></span>
        <button type="button" class="text-button" :disabled="busy" :aria-label="`移出${member.name}`" @click="remove(member.id)">移出</button>
      </div>
    </details>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>

<style scoped>
.social-controls { border-top: 1px solid var(--line, #dce6ed); margin-top: 12px; padding-top: 12px; display: grid; gap: 10px; font-size: 13px; }
label { display: flex; align-items: center; gap: 8px; }
summary { cursor: pointer; padding-block: 4px; }
.social-member { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-block: 5px; }
small { opacity: .65; }
input[type=number] { width: 64px; padding: 4px; }
details label { margin-block: 8px; }
p { margin: 0; }
</style>
