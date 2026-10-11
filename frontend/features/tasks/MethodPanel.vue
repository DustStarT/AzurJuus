<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { api } from '../../shared/api';
import MindPanel from '../mind/MindPanel.vue';
const props = defineProps<{ actorId: string; name: string }>();
const emit = defineEmits<{close: []; inspect: [id: string]}>();
type Skill = {id:string;name:string;summary:string;enabled:boolean;sourceRunId?:string;validationCount:number;origin:string;lifecycle:string;revision:number;trialFamily?:string;trialError?:string;trialEvidence:{case:number;variant:string;passed:boolean;seconds:number}[];trialSummary?:{candidateRate:number;baselineRate:number;stable:boolean};publicProposalId?:string;trialSpec?:unknown[];secretaryReview?:{approved:boolean;reason:string}};
type SkillProposal = {id:string;actorId:string;baseSkillName:string;title:string;summary:string;promptPatch:string;status:string;decisionNote?:string;extra?:{secretaryReview?:{status:string;note?:string};userReview?:{status:string;note?:string};rollback?:{note?:string}}};
const data = ref<{skills:Skill[];records:{id:string;names:string[];summary:string;verified:boolean;runId?:string}[]}>();
const proposals = ref<SkillProposal[]>([]);
const error = ref('');
const tab = ref('methods');
const proposalBusy = ref(false);
const lifecycle:Record<string,string> = {candidate:'待验证',queued:'等待试用',testing:'隔离试用中',active:'可选用',paused:'已停用',trial_failed:'试用未通过',awaiting_trial_confirmation:'等待确认试用方案',proposal_rejected:'秘书未通过方案'};
const evolutionBusy = ref(false);
async function evolve(skill: Skill) {
  const record=data.value?.records.find(r => r.verified && r.runId && r.names.includes(skill.name));
  if (!record?.runId) { error.value='先完成一次使用该方法的真实任务，才有可核验的改进来源。'; return; }
  if (evolutionBusy.value) return;
  evolutionBusy.value=true; error.value='';
  try { await api(`/api/actors/${props.actorId}/methods/evolve`,{sourceRunId:record.runId,basedOn:[skill.id]}); await load(); }
  catch(e) { error.value=(e as Error).message; }
  finally { evolutionBusy.value=false; }
}
const proposalStatus:Record<string,string> = {pending:'等待审核',approved:'已共享',rejected:'未通过',rolled_back:'已回滚'};
const reviewStatus:Record<string,string> = {queued:'等待秘书复核',running:'秘书复核中',approved:'秘书已通过',rejected:'秘书未通过',failed:'复核失败'};
let active = true, timer = 0;
async function load() { try { const [next, shared] = await Promise.all([
  api<typeof data.value>(`/api/actors/${props.actorId}/methods`),
  api<{proposals:SkillProposal[]}>('/api/skills/proposals'),
]); if (active) { data.value = next; proposals.value = shared.proposals.filter(item => item.actorId === props.actorId); } } catch(e) { error.value = (e as Error).message; } }
function changed(event:Event) { const detail=(event as CustomEvent).detail; if (detail?.type === 'skill.proposal.changed' || (detail?.type === 'skill.changed' && detail.payload.actorId === props.actorId)) { clearTimeout(timer); timer=window.setTimeout(load,200); } }
async function control(id:string, action:string) {
  try { await api(`/api/actors/${props.actorId}/methods/${id}/${action}`, {}); await load(); }
  catch(e) { error.value = (e as Error).message; }
}
async function decideProposal(id:string, decision:'approved'|'rejected') {
  if (proposalBusy.value) return;
  proposalBusy.value = true; error.value = '';
  try { await api(`/api/skills/proposals/${id}/review`, {decision}); await load(); }
  catch(e) { error.value = (e as Error).message; }
  finally { proposalBusy.value = false; }
}
async function rollbackProposal(id:string) {
  if (proposalBusy.value) return;
  proposalBusy.value = true; error.value = '';
  try { await api(`/api/skills/proposals/${id}/rollback`, {}); await load(); }
  catch(e) { error.value = (e as Error).message; }
  finally { proposalBusy.value = false; }
}
async function retryReview(id:string) {
  try { await api(`/api/skills/proposals/${id}/review-request`, {}); await load(); }
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
        <nav class="drawer-tabs"><button :class="{active: tab === 'methods'}" @click="tab = 'methods'">方法与成长</button><button :class="{active: tab === 'mind'}" @click="tab = 'mind'">经历与关系</button></nav>
        <p v-if="error" class="error-message">{{ error }}</p>
        <MindPanel v-if="tab === 'mind'" :actor-id="actorId" />
        <section v-else>
        <p class="muted">方法影响处理事情的习惯，人物性格影响表达；工具权限单独管理。</p>
        <article v-for="skill in data?.skills || []" :key="skill.id" class="assignment">
          <strong>{{ skill.name }}</strong><small> · {{ lifecycle[skill.lifecycle] || (skill.enabled ? '可选用' : '候选') }} · 版本 {{ skill.revision }}</small>
          <p>{{ skill.summary }}</p>
          <button v-if="skill.enabled" class="text-button" :disabled="evolutionBusy" @click="evolve(skill)">{{ evolutionBusy ? '正在提炼并审核' : '从已验收任务改进方法' }}</button>
          <p v-if="skill.secretaryReview" class="quiet-note">秘书：{{ skill.secretaryReview.reason }}</p>
          <details v-if="skill.trialSpec"><summary>三场景输入与验收方案</summary><pre>{{ JSON.stringify(skill.trialSpec,null,2) }}</pre></details>
          <button v-if="skill.lifecycle === 'awaiting_trial_confirmation'" class="primary-button" @click="control(skill.id,'confirm_trial')">确认方案，安排隔离试用</button>
          <button v-if="skill.sourceRunId" class="text-button" @click="emit('inspect',skill.sourceRunId)">查看启发来源</button>
          <p v-if="!skill.enabled" class="muted">已验证试用 {{ skill.validationCount }} 次。观察记录不代表已经学会。</p>
          <p v-if="skill.trialError" class="error-message">{{ skill.trialError }}</p>
          <button v-if="!skill.enabled && skill.trialFamily && !['awaiting_trial_confirmation','proposal_rejected'].includes(skill.lifecycle)" class="text-button" :disabled="['queued','testing'].includes(skill.lifecycle)" @click="control(skill.id,'trial')">安排隔离试用</button>
          <button v-if="!skill.enabled && (skill.origin.startsWith('builtin') || skill.validationCount === 3)" class="text-button" @click="control(skill.id,'enable')">重新启用</button>
          <button v-if="skill.enabled" class="text-button" @click="control(skill.id,'disable')">停用</button>
          <button class="text-button" @click="control(skill.id,'rollback')">回滚</button>
          <details v-if="skill.trialEvidence?.length"><summary>隔离验证证据</summary><p v-for="item in skill.trialEvidence" :key="item.case + item.variant">场景 {{ item.case + 1 }} · {{ item.variant === 'baseline' ? '原方法' : '新方法' }}：{{ item.passed ? '通过' : '未通过' }}，{{ item.seconds }} 秒</p></details>
          <p v-if="skill.trialSummary" class="quiet-note">候选稳定率 {{ Math.round(skill.trialSummary.candidateRate * 100) }}%，基线稳定率 {{ Math.round(skill.trialSummary.baselineRate * 100) }}%。</p>
          <button v-if="skill.enabled && skill.validationCount === 3 && !skill.publicProposalId" class="text-button" @click="control(skill.id,'publish')">申请公有共享</button>
          <button v-if="skill.publicProposalId" class="text-button" @click="control(skill.id,'withdraw_public')">撤回公有共享</button>
        </article>
        <h3 v-if="proposals.length">公有方法提案</h3>
        <article v-for="proposal in proposals" :key="proposal.id" class="assignment">
          <strong>{{ proposal.title }}</strong><small> · {{ proposal.baseSkillName }} · {{ proposalStatus[proposal.status] || proposal.status }}</small>
          <p>{{ proposal.summary }}</p>
          <details><summary>查看提案差异</summary><p>在 {{ proposal.baseSkillName }} 上增加以下方法：</p><pre>{{ proposal.promptPatch }}</pre></details>
          <p v-if="proposal.extra?.secretaryReview" class="quiet-note">{{ reviewStatus[proposal.extra.secretaryReview.status] || proposal.extra.secretaryReview.status }} · {{ proposal.extra.secretaryReview.note }}</p>
          <p v-if="proposal.decisionNote" class="quiet-note">{{ proposal.decisionNote }}</p>
          <div v-if="proposal.status === 'pending'" class="button-row">
            <button class="text-button danger" :disabled="proposalBusy" @click="decideProposal(proposal.id,'rejected')">拒绝共享</button>
            <button v-if="proposal.extra?.secretaryReview?.status === 'failed'" class="text-button" @click="retryReview(proposal.id)">重新复核</button>
            <button v-if="proposal.extra?.secretaryReview?.status === 'approved'" class="primary-button" :disabled="proposalBusy" @click="decideProposal(proposal.id,'approved')">确认共享</button>
          </div>
          <button v-if="proposal.status === 'approved'" class="text-button danger" :disabled="proposalBusy" @click="rollbackProposal(proposal.id)">回滚公有版本</button>
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
