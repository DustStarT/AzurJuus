<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import { api } from "../../shared/api";
import type { Run, ToolCall, Agent, RunEvent } from "../../shared/types";
import Icon from "../../shared/Icon.vue";
import Avatar from "../../shared/Avatar.vue";
const props = defineProps<{ run?: Run; runs: Run[]; agents: Agent[] }>();
const emit = defineEmits<{ close: []; select: [id: string]; changed: [] }>();
const calls = ref<ToolCall[]>([]),
  error = ref(""),
  steer = ref(""),
  acknowledge = ref(false),
  tab = ref("progress"),
  busy = ref(false);
const outputs = ref<Record<string, string>>({});
const transcripts = ref<{seq:number;actorId:string;text:string}[]>([]);
const statuses: Record<string, string> = {
  queued: "排队中",
  running: "正在执行",
  waiting_approval: "等待确认",
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
  calls.value.filter((c) => c.status === "waiting_approval"),
);
const currentRuns = computed(() => props.runs.filter((r) => r.mode !== 'chat'));
let timer = 0;
async function load() {
  if (props.run) {
    try {
      calls.value = (
        await api<{ calls: ToolCall[] }>(`/api/runs/${props.run.id}`)
      ).calls;
      transcripts.value = (await api<{records:typeof transcripts.value}>(`/api/runs/${props.run.id}/transcripts`)).records;
    } catch (e) {
      error.value = (e as Error).message;
    }
  }
}
function event(e: Event) {
  const detail = (e as CustomEvent<RunEvent>).detail;
  if (detail?.runId !== props.run?.id) return;
  if (detail.type === "tool.output") {
    const id = String(detail.payload.callId);
    outputs.value[id] = (
      (outputs.value[id] || "") + String(detail.payload.text || "")
    ).slice(-24000);
    return;
  }
  if (!timer)
    timer = window.setTimeout(() => {
      timer = 0;
      void load();
    }, 300);
}
watch(
  () => props.run?.id,
  () => {
    calls.value = [];
    outputs.value = {};
    acknowledge.value = false;
    void load();
  },
);
onMounted(() => {
  void load();
  window.addEventListener("azur-run-event", event);
});
onUnmounted(() => {
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
  try {
    await api(`/api/runs/${props.run?.id}/approvals/${call.id}`, { decision });
    await load();
    emit("changed");
  } catch (e) {
    error.value = (e as Error).message;
  }
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
          <section v-if="approvals.length" class="approval-section">
            <h3><Icon name="shield" />需要你的确认</h3>
            <article
              v-for="call in approvals"
              :key="call.id"
              class="approval-card"
            >
              <strong>{{ call.name }}</strong>
              <p>{{ call.result?.reason }}</p>
              <pre>{{ pretty(call.args) }}</pre>
              <pre
                v-if="outputs[call.id] && call.status === 'running'"
                aria-live="off"
                >{{ outputs[call.id] }}</pre
              >
              <div class="button-row">
                <button class="soft-button" @click="resolve(call, 'rejected')">
                  拒绝</button
                ><button
                  class="primary-button"
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
