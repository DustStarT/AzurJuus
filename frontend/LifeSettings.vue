<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { api } from './api';
type Config = {paused:boolean;hourlyCalls:number;quietStart:number;quietEnd:number;proactive:boolean;actorDmHourly:number;dmHourly:number;groupHourly:number};
type Control = {enabled:boolean;settings:Config;callsLastHour:number;failuresLastHour:number;tokensLastHour:number;usageKnownCalls:number};
const control = ref<Control>(), error = ref(''), saved = ref(false), busy = ref(false);
async function load() { try { control.value = await api<Control>('/api/life'); } catch(e) { error.value = (e as Error).message; } }
async function save() {
  busy.value = true; error.value = ''; saved.value = false;
  try { control.value = await api<Control>('/api/life', control.value!.settings); saved.value = true; }
  catch(e) { error.value = (e as Error).message; } finally { busy.value = false; }
}
onMounted(load);
</script>
<template>
  <section aria-label="自主生活设置">
    <h3>自主生活</h3>
    <p class="muted">应用运行时，人物可以推进个人目标、休息和与同伴交流。关闭应用后保留进度，不补造离线互动。模拟练习不会授予真实工具能力。</p>
    <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    <form v-if="control" @submit.prevent="save">
      <p v-if="!control.enabled" class="muted">当前使用旧引擎；新生活记录保留，自主生活调度已停止。</p>
      <label class="check-label"><input type="checkbox" v-model="control.settings.paused">暂停所有后台生活与反思</label>
      <label>每小时后台模型调用上限<input type="number" min="1" max="1000" v-model.number="control.settings.hourlyCalls" required></label>
      <p class="field-hint">理解、决策、表达分别计次；失败与重试也计入。前台请求优先，额度用尽后保留待处理计划。</p>
      <label class="check-label"><input type="checkbox" v-model="control.settings.proactive">允许主动私聊与群聊</label>
      <fieldset><legend>免打扰（本机时间）</legend>
        <label>开始时刻<input type="number" min="0" max="23" v-model.number="control.settings.quietStart" required></label>
        <label>结束时刻<input type="number" min="0" max="23" v-model.number="control.settings.quietEnd" required></label>
        <p class="field-hint">默认 23:00–08:00。起止相同表示关闭免打扰。期间不积压待补发的闲聊。</p>
      </fieldset>
      <details><summary>主动联系频率</summary>
        <label>每人每小时主动私信<input type="number" min="0" max="10" v-model.number="control.settings.actorDmHourly" required></label>
        <label>全港每小时主动私信<input type="number" min="0" max="30" v-model.number="control.settings.dmHourly" required></label>
        <label>全港每小时主动群聊话题<input type="number" min="0" max="10" v-model.number="control.settings.groupHourly" required></label>
        <p class="field-hint">每个主动群聊话题最多六次发言，可以提前结束。回复已有对话不算再次主动联系。</p>
      </details>
      <p class="muted">过去一小时后台调用 {{ control.callsLastHour }} 次；全部调用失败 {{ control.failuresLastHour }} 次。模型已报告 {{ control.tokensLastHour }} token（{{ control.usageKnownCalls }} 次有用量报告）。</p>
      <button class="primary-button" :disabled="busy">{{ busy ? '保存中…' : '保存自主生活设置' }}</button>
      <span v-if="saved" role="status"> 已保存</span>
    </form>
  </section>
</template>
