<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { api } from './api';
import MindPanel from './MindPanel.vue';
const props = defineProps<{ actorId: string; name: string }>();
const emit = defineEmits<{close: []; inspect: [id: string]}>();
type Skill = {id:string;name:string;summary:string;enabled:boolean;sourceRunId?:string;validationCount:number;origin:string;lifecycle:string;revision:number;trialFamily?:string;trialError?:string;trialEvidence:{case:number;variant:string;passed:boolean;seconds:number}[];publicReview?:{status:string;reason:string};publicProposalId?:string};
const data = ref<{skills:Skill[];records:{id:string;names:string[];summary:string;verified:boolean;runId?:string}[]}>();
const error = ref('');
const tab = ref('methods');
const lifecycle:Record<string,string> = {candidate:'待验证',queued:'等待试用',testing:'隔离试用中',active:'可选用',paused:'已停用',trial_failed:'试用未通过'};
let active = true, timer = 0;
async function load() { try { const next = await api<typeof data.value>(`/api/actors/${props.actorId}/methods`); if (active) data.value = next; } catch(e) { error.value = (e as Error).message; } }
function changed(event:Event) { const detail=(event as CustomEvent).detail; if (detail?.type === 'skill.changed' && detail.payload.actorId === props.actorId) { clearTimeout(timer); timer=window.setTimeout(load,200); } }
async function control(id:string, action:string) {
  try { await api(`/api/actors/${props.actorId}/methods/${id}/${action}`, {}); data.value = await api(`/api/actors/${props.actorId}/methods`); }
  catch(e) { error.value = (e as Error).message; }
}
onMounted(() => { load(); window.addEventListener('azur-run-event',changed); });
onUnmounted(() => { active=false; clearTimeout(timer); window.removeEventListener('azur-run-event',changed); });
</script>
<template>
  <div class="drawer-scrim" @click.self="emit('close')">
    <aside class="work-drawer" role="dialog" aria-label="方法与成长">
      <header class="drawer-header"><h2>{{ name }} · 方法与成长</h2><button class="text-button" @click="emit('close')">关闭</button></header>
      <div class="drawer-content">
        <nav class="drawer-tabs"><button @click="tab = 'methods'">方法与成长</button><button @click="tab = 'mind'">经历与关系</button></nav>
        <p v-if="error" class="error-message">{{ error }}</p>
        <MindPanel v-if="tab === 'mind'" :actor-id="actorId" />
        <section v-else>
        <p class="muted">方法影响处理事情的习惯，人物性格影响表达；工具权限单独管理。</p>
        <article v-for="skill in data?.skills || []" :key="skill.id" class="assignment">
          <strong>{{ skill.name }}</strong><small> · {{ lifecycle[skill.lifecycle] || (skill.enabled ? '可选用' : '候选') }} · 版本 {{ skill.revision }}</small>
          <p>{{ skill.summary }}</p>
          <button v-if="skill.sourceRunId" class="text-button" @click="emit('inspect',skill.sourceRunId)">查看启发来源</button>
          <p v-if="!skill.enabled" class="muted">已验证试用 {{ skill.validationCount }} 次。观察记录不代表已经学会。</p>
          <p v-if="skill.trialError" class="error-message">{{ skill.trialError }}</p>
          <button v-if="!skill.enabled && skill.trialFamily" class="text-button" :disabled="['queued','testing'].includes(skill.lifecycle)" @click="control(skill.id,'trial')">安排隔离试用</button>
          <button v-if="!skill.enabled && (skill.origin.startsWith('builtin') || skill.validationCount === 3)" class="text-button" @click="control(skill.id,'enable')">重新启用</button>
          <button v-if="skill.enabled" class="text-button" @click="control(skill.id,'disable')">停用</button>
          <button class="text-button" @click="control(skill.id,'rollback')">回滚</button>
          <details v-if="skill.trialEvidence?.length"><summary>隔离验证证据</summary><p v-for="item in skill.trialEvidence" :key="item.case + item.variant">场景 {{ item.case + 1 }} · {{ item.variant === 'baseline' ? '原方法' : '新方法' }}：{{ item.passed ? '通过' : '未通过' }}，{{ item.seconds }} 秒</p></details>
          <button v-if="skill.enabled && skill.validationCount === 3 && !skill.publicProposalId" class="text-button" @click="control(skill.id,'publish')">申请公有共享</button>
          <p v-if="skill.publicReview" class="muted">秘书复核：{{ ({queued:'等待复核',running:'复核中',approved:'通过',rejected:'未通过',failed:'失败'} as Record<string,string>)[skill.publicReview.status] }} {{ skill.publicReview.reason }}</p>
          <button v-if="skill.publicReview?.status === 'approved' && !skill.publicProposalId" class="primary-button" @click="control(skill.id,'confirm_publish')">确认共享此方法</button>
          <button v-if="skill.publicProposalId" class="text-button" @click="control(skill.id,'withdraw_public')">撤回公有共享</button>
        </article>
        <h3>最近的方法使用</h3>
        <article v-for="record in data?.records || []" :key="record.id" class="assignment">
          <strong>{{ record.names.join(' · ') }}</strong>
          <p>{{ record.summary }}</p><small>{{ record.verified ? '任务验收通过' : '对话或未验证记录' }}</small>
          <button v-if="record.runId" class="text-button" @click="emit('inspect',record.runId)">查看工作记录</button>
        </article>
        </section>
      </div>
    </aside>
  </div>
</template>
