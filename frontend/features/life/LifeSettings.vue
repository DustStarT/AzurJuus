<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import { api } from '../../shared/api';
type Config = {paused:boolean;hourlyCalls:number;quietStart:number;quietEnd:number;proactive:boolean;actorDmHourly:number;dmHourly:number;groupHourly:number;momentsEnabled:boolean;momentsActorDailyPosts:number;momentsDailyPosts:number;momentsActorDailyInteractions:number;momentsDailyInteractions:number};
type Stage = {kind:string;calls:number;failures:number;tokens:number};
type Control = {enabled:boolean;settings:Config;callsLastHour:number;backgroundCallsLastHour:number;failuresLastHour:number;tokensLastHour:number;usageKnownCalls:number;backgroundFailuresLastHour:number;backgroundTokensLastHour:number;backgroundStages:Stage[];stagesLastHour:Stage[];activeCalls:number;peakConcurrentCalls:number;decisionQueueWaitSeconds:number;emptyResponsesLastHour:number;expiredDecisionsLastHour:number;staleGroupDecisionsLastHour:number};
const props = defineProps<{developerMode?: boolean}>();
const stageNames:Record<string,string>={profile:'人物资料编译',understand:'理解',decide:'决策',express:'表达',reflect:'反思',research:'资料调查',moments_post:'动态表达',moments_comment:'动态评论','skill-trial':'技能验证','method-review':'方法复核'};
const control = ref<Control>(), error = ref(''), saved = ref(false), busy = ref(false);
const machineTime=ref(''); let timer:ReturnType<typeof setInterval>|undefined;
async function clock() { try { const value=await api<{local:string;timezone:string}>('/api/time'); machineTime.value=new Date(value.local).toLocaleString('zh-CN',{dateStyle:'medium',timeStyle:'short'})+' · 本机时间'; } catch {} }
async function load() { try { control.value = await api<Control>('/api/life'); } catch(e) { error.value = (e as Error).message; } }
async function refreshUsage() {
  try {
    const value=await api<Control>('/api/life');
    control.value={...value,settings:control.value?.settings ?? value.settings};
  } catch(e) { error.value=(e as Error).message; }
}
async function save() {
  busy.value = true; error.value = ''; saved.value = false;
  try { control.value = await api<Control>('/api/life', control.value!.settings); saved.value = true; }
  catch(e) { error.value = (e as Error).message; } finally { busy.value = false; }
}
onMounted(()=>{load();clock();timer=setInterval(clock,30000);});
onUnmounted(()=>clearInterval(timer));
</script>
<template>
  <section class="settings-page life-settings" aria-label="自主生活设置">
    <h3>自主生活</h3>
    <p class="page-meta">{{machineTime || '正在同步本机时间…'}}</p>
    <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    <form v-if="control" class="settings-page-form" @submit.prevent="save">
      <div class="usage-overview" aria-label="最近一小时用量"><div><strong>{{control.callsLastHour}}</strong><span>自主调用</span></div><div><strong>{{control.failuresLastHour}}</strong><span>调用失败</span></div><div><strong>{{control.tokensLastHour}}</strong><span>已报告 token</span></div></div>
      <section class="settings-section"><h4>运行与额度</h4>
        <p v-if="!control.enabled" class="field-hint">自主生活未启用。</p>
        <label class="setting-switch"><span>暂停后台生活与反思</span><input type="checkbox" v-model="control.settings.paused"></label>
        <label>每小时自主调用上限<input type="number" min="1" max="1000" v-model.number="control.settings.hourlyCalls" required></label>
        <p class="field-hint">此额度用于自主活动；用户发起的请求不受影响。</p>
      </section>
      <section v-if="props.developerMode" class="settings-section"><h4>免打扰</h4><div class="form-grid">
        <label>开始时刻<input type="number" min="0" max="23" v-model.number="control.settings.quietStart" required></label>
        <label>结束时刻<input type="number" min="0" max="23" v-model.number="control.settings.quietEnd" required></label>
      </div><p class="field-hint">按本机时间，起止相同则关闭免打扰。</p></section>
      <details v-if="props.developerMode" class="settings-section"><summary>主动联系</summary>
        <label class="setting-switch"><span>允许主动私聊与群聊</span><input type="checkbox" v-model="control.settings.proactive"></label>
        <div class="form-grid"><label>每人每小时私信<input type="number" min="0" max="10" v-model.number="control.settings.actorDmHourly" required></label>
        <label>全港每小时私信<input type="number" min="0" max="30" v-model.number="control.settings.dmHourly" required></label>
        <label>全港每小时群聊话题<input type="number" min="0" max="10" v-model.number="control.settings.groupHourly" required></label></div>
      </details>
      <details v-if="props.developerMode" class="settings-section"><summary>动态发布与互动</summary>
        <label class="setting-switch"><span>允许自主发布与回应动态</span><input type="checkbox" v-model="control.settings.momentsEnabled"></label>
        <div class="form-grid"><label>每人每天发布上限<input type="number" min="0" max="10" v-model.number="control.settings.momentsActorDailyPosts" required></label>
        <label>全港每天发布上限<input type="number" min="0" max="30" v-model.number="control.settings.momentsDailyPosts" required></label>
        <label>每人每天互动上限<input type="number" min="0" max="30" v-model.number="control.settings.momentsActorDailyInteractions" required></label>
        <label>全港每天互动上限<input type="number" min="0" max="100" v-model.number="control.settings.momentsDailyInteractions" required></label></div>
      </details>
      <div class="section-actions"><span v-if="saved" class="save-notice" role="status">已保存</span><button class="primary-button" :disabled="busy">{{busy?'保存中…':'保存自主生活设置'}}</button></div>
      <details v-if="props.developerMode" class="settings-section usage-details">
        <summary>过去一小时后台调用明细</summary>
        <button type="button" class="text-button" @click="refreshUsage">刷新调用统计</button>
        <p class="muted">后台共 {{ control.backgroundCallsLastHour }} 次，其中自主活动等计入额度 {{ control.callsLastHour }} 次；后台失败 {{ control.backgroundFailuresLastHour }} 次，已报告 {{ control.backgroundTokensLastHour }} token。</p>
        <ul v-if="control.backgroundStages?.length">
          <li v-for="stage in control.backgroundStages" :key="stage.kind">{{ stageNames[stage.kind] || stage.kind }}：{{ stage.calls }} 次，失败 {{ stage.failures }} 次，已报告 {{ stage.tokens }} token</li>
        </ul>
        <p v-else class="muted">这一小时没有后台模型调用。</p>
        <p class="muted">当前模型调用 {{ control.activeCalls }} 项；本次启动最高并发 {{ control.peakConcurrentCalls }} 项。过去一小时决策排队 {{ control.decisionQueueWaitSeconds.toFixed(1) }} 秒、空正文 {{ control.emptyResponsesLastHour }} 次、过期或中断判断 {{ control.expiredDecisionsLastHour }} 次，其中群聊频道版本过期 {{ control.staleGroupDecisionsLastHour }} 次。</p>
        <details><summary>全部模型阶段用量</summary><ul><li v-for="stage in control.stagesLastHour" :key="stage.kind">{{ stageNames[stage.kind] || stage.kind }}：{{ stage.calls }} 次，已报告 {{ stage.tokens }} token</li></ul></details>
      </details>
    </form>
  </section>
</template>
