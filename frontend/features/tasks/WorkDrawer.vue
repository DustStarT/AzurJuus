<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch, inject } from "vue";
import { api } from "../../shared/api";
import { kernelRuntimeKey } from "../../shared/kernel-runtime";
import type { WorkflowDetail } from "../../shared/kernel-runtime";
import type { Run, ToolCall, Agent, RunEvent, HelpRequest } from "../../shared/types";
import Icon from "../../shared/Icon.vue";
import Avatar from "../../shared/Avatar.vue";
const props = defineProps<{ run?: Run; runs: Run[]; agents: Agent[]; connectedAgentIds: string[] }>();
const emit = defineEmits<{ close: []; select: [id: string]; changed: [] }>();
const kernel = inject(kernelRuntimeKey);
if (!kernel) throw new Error("Task drawer requires the workspace plugin runtime");
const projected = computed(() => props.run ? kernel.state.value[props.run.id] : undefined);
const calls = computed(() => Object.values(projected.value?.calls ?? {}));
const error = ref(""),
  steer = ref(""),
  acknowledge = ref(false),
  tab = ref("progress"),
  busy = ref(false),
  helperAssignmentId = ref(""),
  helperActorId = ref(""),
  helperBrief = ref(""),
  helperReason = ref("");
const outputs = computed(() => projected.value?.outputs ?? {});
const transcripts = ref<{seq:number;actorId:string;text:string}[]>([]);
const statuses: Record<string, string> = {
  queued: "排队中",
  running: "正在执行",
  waiting_approval: "等待确认",
  waiting_secretary: "秘书复核中",
  paused: "已暂停",
  failed: "执行失败",
  completed: "已完成",
  cancelled: "已取消",
  pending: "待执行",
  needs_revision: "待修订",
  submitted: "待复核",
  uncertain: "需要核验",
  approved: "已批准",
  rejected: "已拒绝",
  reconciled: "已核验",
};
const approvals = computed(() =>
  calls.value.filter((c) => ['waiting_secretary','waiting_approval'].includes(c.status)),
);
const currentRuns = computed(() => props.runs.filter((r) => r.mode !== 'chat'));
const helpRequests = computed(() => props.run?.helpRequests || []);
const stageReviews = computed(() => {
  const latest=new Map<string,NonNullable<Run['stageReviews']>[string]>();
  Object.values(props.run?.stageReviews || {}).forEach(r => latest.set(r.key,r));
  return [...latest.values()].filter(r => ['secretary_reviewing','waiting_user','rejected','interrupted','stale'].includes(r.status));
});
const reviewNotes = ref<Record<string,string>>({});
async function decideStage(id: string, decision: string) {
  if (!props.run || busy.value) return;
  busy.value = true; error.value = '';
  try {
    await api(`/api/runs/${props.run.id}/reviews/${id}`, { decision, note: reviewNotes.value[id] || '' });
    delete reviewNotes.value[id]; await load(); emit('changed');
  } catch (e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
const helperAssignments = computed(() => (props.run?.assignments || []).filter((item) => !['completed', 'submitted'].includes(item.status)));
const helperCandidates = computed(() => {
  const assigned = new Set((props.run?.assignments || []).map((item) => item.actorId));
  if (props.run?.actorId) assigned.add(props.run.actorId);
  const connected = new Set(props.connectedAgentIds);
  return props.agents.filter((agent) => connected.has(agent.id) && !assigned.has(agent.id));
});
let timer = 0;
let loadGeneration = 0;
async function load() {
  const runId = props.run?.id;
  const generation = ++loadGeneration;
  if (runId) {
    try {
      const [detail, transcript] = await Promise.all([
        api<WorkflowDetail>(`/api/runs/${runId}`),
        api<{records:typeof transcripts.value}>(`/api/runs/${runId}/transcripts`),
      ]);
      if (generation !== loadGeneration || props.run?.id !== runId) return;
      kernel!.hydrateDetail(detail);
      transcripts.value = transcript.records;
      error.value = "";
    } catch (e) {
      if (generation !== loadGeneration) return;
      error.value = (e as Error).message;
    }
  }
}
function event(e: Event) {
  const detail = (e as CustomEvent<RunEvent>).detail;
  if (detail?.runId !== props.run?.id) return;
  // Calls, approval status and output are already projected by the plugin.
  if (!detail.type.startsWith("execution.message.") && !["run.updated", "run.created"].includes(detail.type)) return;
  if (!timer)
    timer = window.setTimeout(() => {
      timer = 0;
      void load();
    }, 300);
}
watch(
  () => props.run?.id,
  () => {
    clearTimeout(timer);
    timer = 0;
    transcripts.value = [];
    error.value = "";
    acknowledge.value = false;
    helperAssignmentId.value = "";
    helperActorId.value = "";
    helperBrief.value = "";
    helperReason.value = "";
    void load();
  },
);
onMounted(() => {
  void load();
  window.addEventListener("azur-run-event", event);
});
onUnmounted(() => {
  ++loadGeneration;
  clearTimeout(timer);
  window.removeEventListener("azur-run-event", event);
});
async function control(action: string) {
  if (!props.run || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await api(`/api/runs/${props.run.id}/control`, {
      action,
      text: steer.value,
      acknowledgeEffects: acknowledge.value,
    });
    if (action === "steer") steer.value = "";
    emit("changed");
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function resolve(call: ToolCall, decision: string) {
  const runId = props.run?.id;
  if (!runId || busy.value) return;
  busy.value = true;
  try {
    const detail = await api<WorkflowDetail>(`/api/runs/${runId}/approvals/${call.id}`, { decision });
    kernel!.hydrateDetail(detail);
    emit("changed");
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function requestHelp() {
  if (!props.run || !helperAssignmentId.value || !helperActorId.value || !helperBrief.value.trim() || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const assignment = props.run.assignments.find((item) => item.id === helperAssignmentId.value);
    await api('/api/workflows/help-request', {
      runId: props.run.id,
      assignmentId: helperAssignmentId.value,
      requesterId: assignment?.actorId || props.run.actorId,
      brief: helperBrief.value.trim(),
      reason: helperReason.value.trim(),
      candidateIds: [helperActorId.value],
    });
    helperBrief.value = "";
    helperReason.value = "";
    await load();
    emit('changed');
  } catch (e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
async function resolveHelp(request: HelpRequest, decision: string) {
  if (!props.run || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await api(`/api/workflows/help-request/${props.run.id}/${request.id}`, {
      decision,
      actorId: decision === 'approved' ? (request.candidateIds[0] || helperActorId.value) : undefined,
    });
    await load();
    emit('changed');
  } catch (e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
const pretty = (v: unknown) => JSON.stringify(v, null, 2);
async function removeRecords(all = false) {
  const description = all ? "清空已完成、失败和已停止的工作记录？" : "删除这条工作记录？暂停的任务将停止。";
  if (!window.confirm(description + "聊天消息和实际文件将保留。")) return;
  busy.value = true;
  try {
    await api(all ? "/api/runs/clear" : `/api/runs/${props.run?.id}/remove`, {});
    emit("select", "");
    emit("changed");
  } catch (e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
</script>
<template>
  <div class="drawer-scrim" @click.self="emit('close')">
    <aside
      class="work-drawer"
      role="dialog"
      aria-modal="true"
      aria-label="任务详情"
    >
      <header class="drawer-header">
        <div>
          <span class="eyebrow">OPERATIONS / 港区协作</span>
          <h2>工作记录<span class="small-dot"></span></h2>
        </div>
        <button
          class="icon-button"
          aria-label="关闭任务详情"
          @click="emit('close')"
        >
          <Icon name="close" />
        </button>
      </header>
      <div class="run-picker">
        <button class="text-button" :disabled="busy" @click="removeRecords(true)">清空已结束记录</button>
        <select
          aria-label="选择任务"
          :value="run?.id || ''"
          @change="emit('select', ($event.target as HTMLSelectElement).value)"
        >
          <option value="" disabled>选择一项任务</option>
          <option v-for="r in currentRuns" :key="r.id" :value="r.id">
            {{ r.prompt.slice(0, 45) }} · {{ statuses[r.status] }}
          </option>
        </select>
      </div>
      <div v-if="!run" class="empty-state">
        <Icon name="activity" :size="44" />
        <h3>从一句委托开始</h3>
        <p>切换到任务模式，把想完成的事情交给港区伙伴。</p>
      </div>
      <template v-else>
        <div class="run-overview">
          <span class="status-pill" :class="'status--' + run.status">{{
            statuses[run.status] || run.status
          }}</span>
          <h3>{{ run.prompt }}</h3>
          <p v-for="[phase, stage] in Object.entries(['running', 'paused', 'failed', 'waiting_approval'].includes(run.status) ? run.runtimeStages || {} : {})" :key="phase" class="muted">
            {{ agents.find(a => a.id === stage.actorId)?.name }} · {{ ['paused', 'failed'].includes(run.status) ? '停止前：' : '' }}{{ stage.label }}
          </p>
          <p v-if="run.error" class="error-message">
            <Icon name="alert" />{{ run.error }}
          </p>
          <p v-if="run.expressionError" class="error-message">{{ run.expressionError }}。执行状态和产物不受影响。</p>
          <div class="run-controls">
            <button v-if="['completed', 'failed', 'cancelled', 'paused'].includes(run.status)" class="text-button danger" :disabled="busy" @click="removeRecords()">删除记录</button>
            <button
              v-if="
                ['running', 'queued', 'waiting_approval'].includes(run.status)
              "
              class="soft-button"
              :disabled="busy"
              @click="control('pause')"
            >
              <Icon name="pause" />暂停</button
            ><button
              v-if="['paused', 'failed'].includes(run.status)"
              class="primary-button"
              :disabled="busy"
              @click="control('resume')"
            >
              <Icon name="play" />继续任务</button
            ><button
              v-if="!['completed', 'cancelled'].includes(run.status)"
              class="text-button danger"
              @click="control('cancel')"
            >
              <Icon name="stop" />停止
            </button>
          </div>
          <label
            v-if="['paused', 'failed'].includes(run.status)"
            class="check-label"
            ><input
              type="checkbox"
              v-model="acknowledge"
            />我已核验上次操作产生的文件或页面变化</label
          >
        </div>
        <nav class="drawer-tabs">
          <button
            v-for="item in [
              { id: 'progress', label: '进度' },
              { id: 'tools', label: '执行记录' },
              { id: 'artifacts', label: '交付成果' },
            ]"
            :key="item.id"
            :class="{ active: tab === item.id }"
            @click="tab = item.id"
          >
            {{ item.label }}
          </button>
        </nav>
        <div class="drawer-content">
          <p v-if="error" class="error-message" role="alert">{{ error }}</p>
          <section v-if="stageReviews.length" class="approval-section">
            <h3><Icon name="shield" />阶段验收</h3>
            <article v-for="review in stageReviews" :key="review.id" class="approval-card">
              <strong>{{ review.title }}</strong>
              <p>{{ review.status === 'secretary_reviewing' ? '秘书正在复核' : review.status === 'waiting_user' ? '秘书已通过，等待你的验收' : '审核未完成，请修订或继续任务' }}</p>
              <p v-if="review.secretary">{{ agents.find(a => a.id === review.secretary?.actorId)?.name || '秘书' }}：{{ review.secretary.reason }}</p>
              <p v-if="review.material.result">{{ review.material.result.summary }}</p>
              <ul v-if="review.material.assignments"><li v-for="item in review.material.assignments" :key="item.id">{{ agents.find(a => a.id === item.actorId)?.name }}：{{ item.brief }}</li></ul>
              <p v-for="(artifact,index) in review.material.artifacts || []" :key="artifact.path"><a :href="`/api/runs/${run.id}/reviews/${review.id}/artifacts/${index}`" download>{{ artifact.path }}</a> · {{ artifact.bytes }} 字节</p>
              <label v-if="review.status === 'waiting_user'">验收意见<textarea v-model="reviewNotes[review.id]" maxlength="2000" placeholder="需要修改的地方或补充要求"></textarea></label>
              <div v-if="review.status === 'waiting_user'" class="button-row">
                <button class="soft-button" :disabled="busy" @click="decideStage(review.id,'rejected')">要求修订</button>
                <button class="primary-button" :disabled="busy" @click="decideStage(review.id,'approved')">验收通过，继续</button>
              </div>
            </article>
          </section>
          <section v-if="approvals.length" class="approval-section">
            <h3><Icon name="shield" />操作复核与确认</h3>
            <article
              v-for="call in approvals"
              :key="call.id"
              class="approval-card"
            >
              <strong>{{ call.name }}</strong>
              <p v-if="call.status === 'waiting_secretary'">秘书正在核验必要性与副作用，尚无执行权限。</p>
              <p>{{ call.result?.reason }}</p>
              <p v-if="call.result?.secretaryReview">秘书复核：{{ (call.result.secretaryReview as { reason?: string }).reason }}</p>
              <pre>{{ pretty(call.args) }}</pre>
              <pre
                v-if="outputs[call.id] && call.status === 'running'"
                aria-live="off"
                >{{ outputs[call.id] }}</pre
              >
              <div v-if="call.status === 'waiting_approval'" class="button-row">
                <button class="soft-button" :disabled="busy" @click="resolve(call, 'rejected')">
                  拒绝</button
                ><button
                  class="primary-button"
                  :disabled="busy"
                  @click="resolve(call, 'approved')"
                >
                  <Icon name="check" />批准操作
                </button>
              </div>
            </article>
          </section>
          <template v-if="tab === 'progress'"
            ><p v-for="candidate in run.learningCandidates || []" :key="candidate.skillId" class="quiet-note">
              {{ candidate.name }}：已记录协作启发，待试用验证。
            </p><div v-if="!run.assignments.length" class="quiet-note">
              {{
                run.status === "running"
                  ? "正在整理任务与分工…"
                  : "等待任务开始"
              }}
            </div>
            <article
              v-for="(assignment, index) in run.assignments"
              :key="assignment.id"
              class="assignment"
            >
              <div class="assignment-line">
                <span class="step-number">{{
                  String(index + 1).padStart(2, "0")
                }}</span
                ><Avatar
                  :agent="agents.find((a) => a.id === assignment.actorId)"
                />
                <div>
                  <strong>{{
                    agents.find((a) => a.id === assignment.actorId)?.name
                  }}</strong
                  ><small>{{
                    statuses[assignment.status] || assignment.status
                  }}</small>
                </div>
                <Icon v-if="assignment.status === 'completed'" name="check" />
              </div>
              <p>{{ assignment.brief }}</p>
              <p v-if="run.methods?.[assignment.id]" class="quiet-note">采用方法：{{ run.methods[assignment.id].names.join('、') }}</p>
              <div v-if="assignment.dependsOn.length" class="quiet-note">
                依赖 {{ assignment.dependsOn.join("、") }}
              </div>
              <details v-if="assignment.result">
                <summary>查看交接成果</summary>
                <p>{{ assignment.result.summary }}</p>
              </details>
            </article>
            <section v-if="run.status !== 'completed' && helperAssignments.length" class="help-panel">
              <div class="section-heading">
                <span><Icon name="users" /> 外援协作</span>
                <small>计划修订 {{ run.planRevision || 0 }}</small>
              </div>
              <form class="help-form" @submit.prevent="requestHelp">
                <select v-model="helperAssignmentId" aria-label="选择需要外援的子任务">
                  <option value="">选择需要外援的子任务</option>
                  <option v-for="assignment in helperAssignments" :key="assignment.id" :value="assignment.id">
                    {{ assignment.brief.slice(0, 42) }}
                  </option>
                </select>
                <select v-model="helperActorId" aria-label="选择外援候选人">
                  <option value="">选择外援候选人</option>
                  <option v-for="agent in helperCandidates" :key="agent.id" :value="agent.id">{{ agent.name }}</option>
                </select>
                <input v-model="helperBrief" required placeholder="说明需要补位的工作" aria-label="外援工作说明" />
                <input v-model="helperReason" placeholder="原因（可选）" aria-label="申请外援原因" />
                <button class="soft-button" :disabled="busy || !helperAssignmentId || !helperActorId || !helperBrief.trim()">申请外援</button>
              </form>
              <article v-for="request in helpRequests" :key="request.id" class="help-request" :class="`help-request--${request.status}`">
                <div><strong>{{ request.brief }}</strong><small>{{ request.status }}</small></div>
                <p v-if="request.reason">{{ request.reason }}</p>
                <p v-if="request.handoff?.length" class="quiet-note">已携带 {{ request.handoff.length }} 个依赖产物</p>
                <div v-if="request.status === 'pending'" class="button-row">
                  <button class="text-button danger" :disabled="busy" @click="resolveHelp(request, 'rejected')">拒绝</button>
                  <button class="primary-button" :disabled="busy || !request.candidateIds.length" @click="resolveHelp(request, 'approved')">批准并加入计划</button>
                </div>
              </article>
            </section>
            <div v-if="run.result" class="result-summary">
              <Icon name="check" />
              <p>{{ run.result.summary }}</p>
            </div></template
          >
          <div v-if="tab === 'tools' && transcripts.length">
            <details v-for="record in transcripts" :key="record.seq" class="tool-record">
              <summary>{{ agents.find(a => a.id === record.actorId)?.name || '成员' }} · 工作原文</summary>
              <pre>{{ record.text }}</pre>
            </details>
          </div>
          <template v-if="tab === 'tools'"
            ><div v-if="!calls.length" class="quiet-note">
              工具执行后会在这里留下真实记录。
            </div>
            <details v-for="call in calls" :key="call.id" class="tool-record">
              <summary>
                <Icon
                  :name="call.name === 'command' ? 'activity' : 'file'"
                /><span>{{ call.name }}</span
                ><small :class="'status--' + call.status">{{
                  statuses[call.status] || call.status
                }}</small>
              </summary>
              <pre>{{ pretty(call.args) }}</pre>
              <pre v-if="call.result">{{
                pretty(call.result).slice(0, 12000)
              }}</pre>
              <small class="muted">{{ call.id }}</small>
              <a
                v-if="call.name === 'command' && call.result?.logId"
                class="text-button"
                :href="`/api/runs/${run.id}/calls/${call.id}/log`"
                download
                >下载完整输出</a
              >
            </details></template
          >
          <template v-if="tab === 'artifacts'"
            ><p v-if="!run.artifacts?.length" class="quiet-note">
              验收通过的文件会出现在这里。
            </p>
            <a
              v-for="(artifact, index) in run.artifacts"
              :key="artifact.path"
              class="artifact-card"
              :href="`/api/runs/${run.id}/artifacts/${index}`"
              download
              ><Icon name="file" :size="29" />
              <div>
                <strong>{{ artifact.path }}</strong
                ><small
                  >{{ (artifact.bytes / 1024).toFixed(1) }} KB · 已核验</small
                >
              </div>
              <Icon name="download" /></a
          ></template>
        </div>
        <form
          v-if="!['completed', 'cancelled'].includes(run.status)"
          class="steer-form"
          @submit.prevent="control('steer')"
        >
          <input
            v-model="steer"
            placeholder="补充要求，或调整任务方向…"
            aria-label="补充任务要求"
          /><button
            class="icon-button"
            :disabled="!steer.trim()"
            aria-label="发送补充要求"
          >
            <Icon name="send" />
          </button>
        </form>
      </template>
    </aside>
  </div>
</template>
