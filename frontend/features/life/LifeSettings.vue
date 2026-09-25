<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue';
import { api } from '../../shared/api';
type Config = {paused:boolean;hourlyCalls:number;quietStart:number;quietEnd:number;proactive:boolean;actorDmHourly:number;dmHourly:number;groupHourly:number;momentsEnabled:boolean;momentsActorDailyPosts:number;momentsDailyPosts:number;momentsActorDailyInteractions:number;momentsDailyInteractions:number};
type Stage = {kind:string;calls:number;failures:number;tokens:number};
type Control = {enabled:boolean;settings:Config;callsLastHour:number;backgroundCallsLastHour:number;failuresLastHour:number;tokensLastHour:number;usageKnownCalls:number;backgroundFailuresLastHour:number;backgroundTokensLastHour:number;backgroundStages:Stage[];stagesLastHour:Stage[];activeCalls:number;peakConcurrentCalls:number;decisionQueueWaitSeconds:number;emptyResponsesLastHour:number;expiredDecisionsLastHour:number;staleGroupDecisionsLastHour:number};
const stageNames:Record<string,string>={profile:'人物资料编译',understand:'理解',decide:'决策',express:'表达',reflect:'反思',research:'资料调查',moments_post:'动态表达',moments_comment:'动态评论','skill-trial':'技能验证','method-review':'方法复核'};
const control = ref<Control>(), error = ref(''), saved = ref(false), busy = ref(false);
const machineTime=ref(''); let timer:ReturnType<typeof setInterval>|undefined;
async function clock() { try { const value=await api<{local:string;timezone:string}>('/api/time'); machineTime.value=value.local+' '+value.timezone; } catch {} }
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
  <section aria-label="自主生活设置">
    <h3>自主生活</h3>
    <p class="muted">本机时间：{{machineTime || '正在同步'}}（每30秒同步，不调用模型）</p>
    <p class="muted">应用运行时，人物可以推进个人目标、休息和与同伴交流。关闭应用后保留进度，不补造离线互动。模拟练习不会授予真实工具能力。</p>
    <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    <form v-if="control" @submit.prevent="save">
      <details>
        <summary>过去一小时后台调用明细</summary>
        <button type="button" @click="refreshUsage">刷新调用统计</button>
        <p class="muted">后台共 {{ control.backgroundCallsLastHour }} 次，其中自主活动等计入额度 {{ control.callsLastHour }} 次；后台失败 {{ control.backgroundFailuresLastHour }} 次，已报告 {{ control.backgroundTokensLastHour }} token。</p>
        <ul v-if="control.backgroundStages?.length">
          <li v-for="stage in control.backgroundStages" :key="stage.kind">{{ stageNames[stage.kind] || stage.kind }}：{{ stage.calls }} 次，失败 {{ stage.failures }} 次，已报告 {{ stage.tokens }} token</li>
        </ul>
        <p v-else class="muted">这一小时没有后台模型调用。</p>
        <p class="muted">当前模型调用 {{ control.activeCalls }} 项；本次启动最高并发 {{ control.peakConcurrentCalls }} 项。过去一小时决策排队 {{ control.decisionQueueWaitSeconds.toFixed(1) }} 秒、空正文 {{ control.emptyResponsesLastHour }} 次、过期或中断判断 {{ control.expiredDecisionsLastHour }} 次，其中群聊频道版本过期 {{ control.staleGroupDecisionsLastHour }} 次。</p>
        <details><summary>全部模型阶段用量</summary><ul><li v-for="stage in control.stagesLastHour" :key="stage.kind">{{ stageNames[stage.kind] || stage.kind }}：{{ stage.calls }} 次，已报告 {{ stage.tokens }} token</li></ul></details>
      </details>
      <p v-if="!control.enabled" class="muted">当前使用旧引擎；新生活记录保留，自主生活调度已停止。</p>
      <label class="check-label"><input type="checkbox" v-model="control.settings.paused">暂停所有后台生活与反思</label>
      <label>每小时自主后台模型调用上限<input type="number" min="1" max="1000" v-model.number="control.settings.hourlyCalls" required></label>
      <p class="field-hint">自主生活、反思、主动交流和自动技能试用计入额度；各阶段及失败重试分别计次。用户请求与资料调查不占此额度。前台请求优先，额度用尽后保留自主计划。</p>
      <p class="field-hint">等待或失败后逐步延长重新考虑间隔；收到新经历或目标变化时可提前唤醒。活动尚未到期不反复询问模型。</p>
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
      <details><summary>朋友圈自主行为</summary>
        <label class="check-label"><input type="checkbox" v-model="control.settings.momentsEnabled">允许人物自主发布与回应动态</label>
        <label>每人每天最多发布<input type="number" min="0" max="10" v-model.number="control.settings.momentsActorDailyPosts" required></label>
        <label>全港每天最多发布<input type="number" min="0" max="30" v-model.number="control.settings.momentsDailyPosts" required></label>
        <label>每人每天最多互动<input type="number" min="0" max="30" v-model.number="control.settings.momentsActorDailyInteractions" required></label>
        <label>全港每天最多互动<input type="number" min="0" max="100" v-model.number="control.settings.momentsDailyInteractions" required></label>
        <p class="field-hint">仅运行时考虑新活动、动态及到期想法；共同活动公开前分别征得参与者同意。仍遵守全局暂停、免打扰和后台模型额度。</p>
      </details>
      <p class="muted">过去一小时计入自主额度 {{ control.callsLastHour }} 次；全部模型调用失败 {{ control.failuresLastHour }} 次。模型已报告 {{ control.tokensLastHour }} token（{{ control.usageKnownCalls }} 次有用量报告）。</p>
      <button class="primary-button" :disabled="busy">{{ busy ? '保存中…' : '保存自主生活设置' }}</button>
      <span v-if="saved" role="status"> 已保存</span>
    </form>
  </section>
</template>
