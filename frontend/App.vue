<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from "vue";
import { api, useWorkspace } from "./shared/api";
import type { Agent, Conversation, Run } from "./shared/types";
import Icon from "./shared/Icon.vue";
import Avatar from "./shared/Avatar.vue";
import { artworkUrl } from './shared/assets';
import WorkDrawer from "./features/tasks/WorkDrawer.vue";
import SpeechBubbles from "./features/chat/SpeechBubbles.vue";
import { liveSpeechIds } from './features/chat/speechQueue';
import MethodPanel from "./features/tasks/MethodPanel.vue";
import SettingsPanel from "./features/settings/SettingsPanel.vue";
import AttachmentPicker from './features/chat/AttachmentPicker.vue';
import StickerPicker from './features/chat/StickerPicker.vue';
import SocialControls from "./features/chat/SocialControls.vue";
import ConversationList from './features/chat/ConversationList.vue';
import BackdropSettings from './features/chat/BackdropSettings.vue';
import { backdropPosition } from './features/chat/backdropPreferences';
import MomentsPage from './features/social/MomentsPage.vue';
const { workspace, runs, online, error, streams, refresh, start, stop } =
  useWorkspace();
const methodActor = ref<Agent>();
const attachmentIds = ref<string[]>([]);
const view = ref("chat"),
  activeId = ref(""),
  postId = ref(""),
  showSettings = ref(false),
  showWork = ref(false),
  showMembers = ref(false),
  selectedRunId = ref("");
const draft = ref(""),
  mode = ref("auto"),
  sending = ref(false),
  composing = ref(false),
  notice = ref("");
const modeMenu = ref<HTMLDetailsElement>();
function setMode(value: string) { mode.value = value; modeMenu.value?.removeAttribute('open'); }
watch(activeId, () => modeMenu.value?.removeAttribute('open'));
const pendingRequest = ref<{ fingerprint: string; id: string }>();
const routeLabels: Record<string, string> = { chat:'闲聊', followup:'结果追问', task:'任务',
  swarm:'协作', guidance:'任务引导', clarify:'澄清' };
const scroller = ref<HTMLElement>(),
  pageSize = ref(100),
  atBottom = ref(true),
  mobileChat = ref(false);
const data = computed(() => workspace.value?.data);
const userId = computed(() => data.value?.user.id || 'commander');
const agents = computed(() => data.value?.agents || []);
const mentionQuery = computed(() => draft.value.match(/(?:^|\s)@([^\s@]*)$/)?.[1]);
const mentionOptions = computed(() => composing.value || mentionQuery.value === undefined || conversation.value?.kind !== 'group'
  ? [] : agents.value.filter(a => a.name.includes(mentionQuery.value!)).slice(0, 8));
function insertMention(name: string) {
  draft.value = draft.value.replace(/@[^\s@]*$/, `@${name} `);
  nextTick(() => document.querySelector<HTMLTextAreaElement>('.composer textarea')?.focus());
}
const agentMap = computed(() =>
  Object.fromEntries(
    [...agents.value, ...(data.value ? [data.value.user] : [])].map((a) => [
      a.id,
      a,
    ]),
  ),
);
const conversation = computed(() =>
  data.value?.conversations.find((c) => c.id === activeId.value),
);
const conversations = computed(() =>
  (data.value?.conversations || []).filter(c => !c.archived),
);
const peer = computed(() =>
  conversation.value?.memberIds
    .map((id) => agentMap.value[id])
    .find((a) => a && a.id !== "commander"),
);
const backdropFailed = ref(false);
const backdropStyle = computed(() => {
  const { x, y, zoom } = backdropPosition(peer.value?.id);
  return { objectPosition: `${x}% ${y}%`, transform: `scale(${zoom / 100})`, transformOrigin: `${x}% ${y}%` };
});
const backdropUnavailable = ref(false);
watch(() => peer.value?.illustrationUrl, () => { backdropFailed.value = false; backdropUnavailable.value = false; });
const messages = computed(() => data.value?.messages[activeId.value] || []);
const visibleMessages = computed(() => {
  const rows = messages.value.slice(-pageSize.value).map(m => {
    const foreign = conversation.value?.kind === 'dm' && m.speakerId !== 'commander' && !conversation.value.memberIds.includes(m.speakerId);
    return { ...m, ...(foreign ? { type: 'task_notice', body: '历史协作回传已保留，请在工作记录中查看。' } : {}), streaming: false };
  });
  for (const [, stream] of activeStreams.value) {
    if (!stream.text) continue;
    const row = { id: stream.messageId, speakerId: stream.actorId, body: stream.text,
      type: "text", createdAt: stream.createdAt, streaming: !stream.done };
    const index = rows.findIndex(m => m.id === row.id);
    if (index < 0) rows.push(row);
    else rows[index] = row;
  }
  return rows;
});
const activeRuns = computed(() =>
  runs.value.filter((r) => r.conversationId === activeId.value),
);
const working = computed(() =>
  activeRuns.value.find((r) =>
    ["running", "queued", "waiting_approval"].includes(r.status),
  ),
);
const taskRuns = computed(() =>
  activeRuns.value.filter((r) => r.mode !== "chat"),
);
const selectedRun = computed(
  () =>
    runs.value.find((r) => r.id === selectedRunId.value && r.mode !== 'chat') || taskRuns.value[0],
);
const attention = computed(
  () =>
    runs.value.filter((r) =>
      ["waiting_approval", "failed", "paused"].includes(r.status),
    ).length,
);
const activeStreams = computed(() =>
  Object.entries(streams).filter(([key]) =>
    activeRuns.value.some((r) => key.startsWith(r.id + ":")),
  ),
);
const teamTask = computed(() => activeRuns.value.find(r => r.teamConversationId === activeId.value));
const guidingTask = computed(() => teamTask.value && !['completed','cancelled'].includes(teamTask.value.status));
function insertSticker(label:string) {
  draft.value = draft.value.replace(/\[表情:[^\]\n]*\]/g, '').trim();
  draft.value += (draft.value ? '\n\n' : '') + `[表情:${label}]`;
}
const statuses: Record<string, string> = {
  queued: "已接收",
  running: "正在执行",
  waiting_approval: "需要确认",
  paused: "已暂停",
  failed: "遇到问题",
  completed: "已完成",
  cancelled: "已停止",
};
const date = (value: string) =>
  new Date(value).toLocaleDateString("zh-CN", {
    month: "long",
    day: "numeric",
  });
const time = (value: string) =>
  new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
function openRun(run?: Run) {
  selectedRunId.value = run?.id || taskRuns.value[0]?.id || "";
  showWork.value = true;
}
async function removeGroup() {
  if (!conversation.value || !confirm('删除这个协作群？未完成任务会停止，文件与审计记录将保留。')) return;
  try {
    await api(`/api/conversations/${conversation.value.id}/remove`, {});
    await refresh();
    activeId.value = conversations.value.find(c => c.kind === 'dm')?.id || conversations.value[0]?.id || '';
    showMembers.value = false;
    await saveSession();
  } catch (e) { notice.value = (e as Error).message; }
}
async function selectConversation(c: Conversation) {
  liveSpeechIds.clear();
  activeId.value = c.id;
  mobileChat.value = true;
  pageSize.value = 100;
  draft.value = "";
  atBottom.value = true;
  showMembers.value = false;
  try {
    await api("/api/conversations/open", { conversationId: c.id });
  } catch {}
  await nextTick();
  scrollBottom();
  saveSession();
}
function scrollBottom() {
  if (scroller.value) scroller.value.scrollTop = scroller.value.scrollHeight;
}
async function speechRevealed() {
  if (atBottom.value) {
    await nextTick();
    scrollBottom();
  }
}
function onScroll() {
  const el = scroller.value;
  if (el)
    atBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
}
async function earlier() {
  const el = scroller.value;
  const height = el?.scrollHeight || 0;
  pageSize.value += 100;
  await nextTick();
  if (el) el.scrollTop += el.scrollHeight - height;
}
watch(
  () => [
    messages.value.length,
    JSON.stringify(activeStreams.value.map(([, s]) => s.text.length)),
  ],
  async () => {
    if (atBottom.value) {
      await nextTick();
      scrollBottom();
    }
  },
);
watch(workspace, (w) => {
  if (w && !activeId.value) {
    activeId.value =
      w.uiSession.activeConversationId || w.data.conversations[0]?.id || "";
    view.value = w.uiSession.view === "circle" ? "circle" : "chat";
    postId.value = w.uiSession.activePostId || "";
    void nextTick(scrollBottom);
  }
});
function saveSession() {
  void api("/api/workspace/save", {
    workspace: {
      uiSession: {
        view: view.value,
        activeConversationId: activeId.value,
        activePostId: postId.value,
      },
    },
  }).catch(() => {});
}
function switchView(next: string) {
  view.value = next;
  saveSession();
}
async function send() {
  if (
    !draft.value.trim() ||
    sending.value ||
    composing.value ||
    !conversation.value
  )
    return;
  const content = draft.value.trim();
  const fingerprint = JSON.stringify([activeId.value, content, attachmentIds.value, mode.value]);
  if (pendingRequest.value?.fingerprint !== fingerprint) pendingRequest.value = { fingerprint, id: crypto.randomUUID() };
  sending.value = true;
  notice.value = "";
  try {
    const result = await api<{ runId: string | null; conversationId: string; route?:{kind:string;status:string}; proposalId?:string }>(
      "/api/messages/send",
      {
        conversationId: activeId.value,
        content,
        attachments: attachmentIds.value,
        mentions: conversation.value.kind === 'group'
          ? agents.value.filter(a => content.includes(`@${a.name}`)).map(a => a.id) : [],
        mode: mode.value === "swarm" ? "swarm" : mode.value,
        collaborative: mode.value === "swarm",
        requestId: pendingRequest.value.id,
      },
    );
    pendingRequest.value = undefined;
    draft.value = "";
    attachmentIds.value = [];
    activeId.value = result.conversationId;
    if (result.runId && (['task','swarm','guidance'].includes(result.route?.kind || '')
      || mode.value === 'task' || mode.value === 'swarm')) selectedRunId.value = result.runId;
    await refresh();
    atBottom.value = true;
    await nextTick();
    scrollBottom();
  } catch (e) {
    notice.value = (e as Error).message;
  } finally {
    sending.value = false;
  }
}
async function decideProposal(requestId:string, action:'confirm'|'cancel') {
  try {
    const result = await api<{runId?:string;conversationId:string}>(`/api/messages/route/${encodeURIComponent(requestId)}/${action}`, {});
    if (result.runId) { selectedRunId.value = result.runId; activeId.value = result.conversationId; }
    await refresh();
  } catch(e) { notice.value = (e as Error).message; await refresh(); }
}
function keydown(e: KeyboardEvent) {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !composing.value && mentionOptions.value.length) {
    e.preventDefault();
    insertMention(mentionOptions.value[0].name);
    return;
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !composing.value) {
    e.preventDefault();
    void send();
  }
}
async function stopChat(id:string) {
  try { await api(`/api/runs/${id}/control`,{action:'cancel'});await refresh(); }
  catch(e) { notice.value=(e as Error).message; }
}
async function changeRole(id: string, role: string) {
  try {
    await api("/api/groups/roles", {
      conversationId: activeId.value,
      agentId: id,
      role,
    });
    await refresh();
  } catch (e) {
    notice.value = (e as Error).message;
  }
}
function globalKey(e: KeyboardEvent) {
  if (e.key === "Escape") {
    showSettings.value = false;
    showWork.value = false;
    showMembers.value = false;
  }
  if (e.key === "Tab") {
    const modal = document.querySelector<HTMLElement>('[aria-modal="true"]');
    if (!modal) return;
    const elements = Array.from(
      modal.querySelectorAll<HTMLElement>(
        "button:not(:disabled),input,textarea,select,a[href]",
      ),
    );
    const first = elements[0],
      last = elements.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  }
}
let focusBefore: Element | null = null;
watch(
  () => showSettings.value || showWork.value,
  async (open) => {
    if (open) {
      focusBefore = document.activeElement;
      await nextTick();
      document
        .querySelector<HTMLElement>('[aria-modal="true"] button')
        ?.focus();
    } else if (focusBefore instanceof HTMLElement) focusBefore.focus();
  },
);
onMounted(() => {
  document.documentElement.classList.toggle(
    "reduced-motion",
    localStorage.getItem("azur-reduced-motion") === "true",
  );
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i);
    if (key?.includes("settings")) {
      try {
        const v = JSON.parse(localStorage.getItem(key) || "{}");
        delete v.llmApiKey;
        delete v.toolApiKey;
        localStorage.setItem(key, JSON.stringify(v));
      } catch {}
    }
  }
  void start().catch(() => {});
  document.addEventListener("keydown", globalKey);
});
onUnmounted(() => {
  stop();
  document.removeEventListener("keydown", globalKey);
});
</script>
<template>
  <div class="juus-shell" :class="{ 'is-mobile-chat': mobileChat }">
    <aside class="rail">
      <a class="brand" href="#" @click.prevent="switchView('chat')"
        aria-label="JUUS 首页">JUUS<span>//</span></a>
      <nav class="rail-nav" aria-label="主导航">
        <button
          :class="{ active: view === 'chat' }"
          aria-label="聊天"
          @click="switchView('chat')"
        >
          <Icon name="chat" :size="30" /><span>通讯</span></button
        ><span class="rail-rule"></span
        ><button
          :class="{ active: view === 'circle' }"
          aria-label="朋友圈"
          @click="switchView('circle')"
        >
          <Icon name="circle" :size="33" /><span>动态</span>
        </button>
      </nav>
      <div class="rail-bottom">
        <button class="rail-work" aria-label="打开任务中心" @click="openRun()">
          <Icon name="activity" /><i v-if="attention">{{
            attention
          }}</i></button
        ><button
          class="rail-work"
          aria-label="打开设置"
          @click="showSettings = true"
        >
          <Icon name="settings" /></button
        ><span
          class="connection-dot"
          :class="{ connected: online }"
          :title="online ? '本地服务已连接' : '正在重新连接'"
        ></span>
      </div>
    </aside>
    <main class="workspace">
      <header class="topbar">
        <span class="wordmark">JUUSTAGRAM</span
        >
        <div class="topbar-status">
          <span class="status-dot" :class="{ connected: online }"></span
          >{{ online ? "本地已连接" : "重新连接中"
          }}<button
            class="icon-button"
            aria-label="查看任务"
            @click="openRun()"
          >
            <Icon name="activity" />
          </button>
        </div>
      </header>
      <div v-if="!workspace" class="loading-view">
        <div class="loading-emblem">JUUS<span>//</span></div>
        <h2>{{ error ? "暂时无法连接港区" : "正在连接港区…" }}</h2>
        <p>{{ error || "伙伴们的消息，马上就到。" }}</p>
        <button class="primary-button" @click="refresh().catch(() => {})">
          重新连接
        </button>
      </div>
      <template v-else>
        <div v-if="notice || error" class="notice-banner" role="alert">
          <Icon name="alert" /><span>{{ notice || error }}</span
          ><button
            class="icon-button"
            aria-label="关闭提示"
            @click="
              notice = '';
              error = '';
            "
          >
            <Icon name="close" />
          </button>
        </div>
        <section v-show="view === 'chat'" class="chat-layout">
          <ConversationList
            :snapshot="data!" :runs="runs" :agents="agents" :active-id="activeId" :user-id="userId"
            @select="selectConversation" @created="async id => { await refresh(); activeId=id; mobileChat=true; }"
          />
          <section class="chat-panel" v-if="conversation">
            <header class="chat-header">
              <button
                class="icon-button mobile-back"
                aria-label="返回会话列表"
                @click="mobileChat = false"
              >
                <Icon name="back" />
              </button>
              <div class="chat-heading">
                <h1>{{ conversation.title }}</h1>
                <p>
                  <span class="small-dot"></span
                  >{{
                    conversation.kind === "group"
                      ? `${conversation.memberIds.length} 位成员 · 协作频道`
                      : peer?.faction || "港区通讯"
                  }}
                </p>
              </div>
              <div class="chat-header-actions">
                <BackdropSettings v-if="conversation.kind === 'dm' && peer?.illustrationUrl" :key="peer.id" :actor="peer" />
                <button v-if="conversation.kind === 'group' && conversation.id !== 'port-hub'" class="text-button danger" @click="removeGroup">删除群聊</button>
                <button
                  class="icon-button"
                  aria-label="查看工作记录"
                  @click="openRun()"
                >
                  <Icon name="folder" /><span
                    v-if="taskRuns.some((r) => r.status === 'waiting_approval')"
                    class="notification-dot"
                  ></span></button
                ><button
                  class="icon-button"
                  aria-label="查看成员资料"
                  @click="showMembers = !showMembers"
                >
                  <Icon
                    :name="conversation.kind === 'group' ? 'users' : 'more'"
                  />
                </button>
              </div>
            </header>
            <div
              class="chat-scene"
              :class="{ 'chat-scene--group': conversation.kind === 'group' }"
            >
              <img
                v-if="conversation.kind !== 'group' && peer?.illustrationUrl && !backdropUnavailable"
                class="character-backdrop"
                :style="backdropStyle"
                :src="backdropFailed ? peer.illustrationUrl : artworkUrl(peer.illustrationUrl)"
                decoding="async"
                @error="backdropFailed ? backdropUnavailable = true : backdropFailed = true"
                alt=""
              />
              <div
                class="message-scroll"
                ref="scroller"
                @scroll.passive="onScroll"
              >
                <button
                  v-if="messages.length > pageSize"
                  class="history-button"
                  @click="earlier"
                >
                  查看更早的消息
                </button>
                <div class="day-divider">
                  <span>{{
                    messages.length ? date(messages[0]!.createdAt) : "今天"
                  }}</span>
                </div>
                <div v-if="!messages.length" class="welcome-note">
                  <Icon name="chat" :size="36" />
                  <h3>{{ conversation.kind === 'group' ? '群聊' : peer?.name ? `和${peer.name}聊聊` : '开始聊天' }}</h3>
                  <p>发条消息，聊聊今天。</p>
                </div>
                <article
                  v-for="m in visibleMessages"
                  :key="m.id"
                  :data-message-id="m.id"
                  class="message-row"
                  :class="{ 'message-row--self': m.speakerId === userId }"
                >
                  <button v-if="m.type === 'task_notice'" class="soft-button" @click="openRun(runs.find(r => r.id === m.metadata?.runId))">{{ m.body }}</button>
                  <Avatar v-else :agent="agentMap[m.speakerId]" />
                  <div v-if="m.type !== 'task_notice'" class="message-content">
                    <div class="message-author">
                      <strong>{{
                        agentMap[m.speakerId]?.name || "港区消息"
                      }}</strong
                      ><time>{{ time(m.createdAt) }}</time><small v-if="m.metadata?.routeKind" class="route-badge">{{ routeLabels[m.metadata.routeKind] || m.metadata.routeKind }}</small>
                    </div>
                    <div v-if="m.type === 'route_proposal' && m.metadata?.routeProposal" class="message-bubble route-proposal">
                      <strong>协作确认</strong>
                      <p>本条委托将分享给 {{ m.metadata.routeProposal.actorIds.map(id => agentMap[id]?.name || id).join('、') }}：</p>
                      <p>{{ m.metadata.routeProposal.summary }}</p>
                      <div v-if="m.metadata.routeProposal.status === 'proposed'" class="route-actions">
                        <button type="button" class="soft-button" @click="decideProposal(m.metadata!.routeProposal!.requestId,'confirm')">确认协作</button>
                        <button type="button" class="text-button" @click="decideProposal(m.metadata!.routeProposal!.requestId,'cancel')">取消</button>
                      </div>
                      <small v-else>{{ m.metadata.routeProposal.status === 'executed' ? '已确认' : m.metadata.routeProposal.status === 'expired' ? '已过期' : '已取消' }}</small>
                    </div>
                    <details v-if="m.type === 'task_progress' && !m.metadata?.expression"><summary>历史工作回复</summary><div class="message-bubble">{{ m.body }}</div></details>
                    <SpeechBubbles v-else-if="m.type !== 'route_proposal'" :text="m.body" :streaming="m.streaming" :self="m.speakerId === userId" :single="m.speakerId === userId" :message-id="m.id" :conversation-id="activeId" @reveal="speechRevealed" />
                    <a v-for="file in m.metadata?.attachments || []" :key="file.id" :href="`/api/attachments/${file.id}?conversationId=${encodeURIComponent(file.conversationId)}`" target="_blank" rel="noreferrer">附件 · {{file.name}}</a>
                  </div>
                </article>
                <div v-for="pending in (data?.pendingReplies || []).filter(p => p.conversationId === activeId)"
                  :key="pending.actorId + pending.sourceMessageId" class="typing-indicator" role="status">
                  {{ agentMap[pending.actorId]?.name || '这位成员' }}正在处理任务，稍后若话题仍相关会决定是否回复。
                </div>
                <div
                  v-if="working && !activeStreams.some(([, s]) => !s.done && s.text)"
                  class="typing-indicator"
                >
                  <span></span><span></span><span></span
                  ><small>{{
                    working.mode === 'chat' ? '正在回复…' : working.status === "waiting_approval"
                      ? "有一项操作需要你的确认"
                      : Object.values(working.runtimeStages || {}).sort((a, b) => b.at - a.at)[0]?.label || "已接收，等待执行"
                  }}</small><button v-if="working.mode !== 'chat'" class="text-button" @click="selectedRunId = working.id; showWork = true">查看进度</button>
                  <button v-else class="text-button" @click="stopChat(working.id)">停止回复</button>
                </div>
                <button
                  v-for="r in taskRuns.filter((item) => !['completed', 'cancelled'].includes(item.status)).slice(0, 3)"
                  :key="r.id"
                  class="inline-task"
                  @click="openRun(r)"
                >
                  <span class="task-symbol"
                    ><Icon
                      :name="
                        r.status === 'completed'
                          ? 'check'
                          : r.status === 'failed'
                            ? 'alert'
                            : 'activity'
                      "
                  /></span>
                  <div>
                    <strong>{{ r.prompt }}</strong
                    ><small :class="'status--' + r.status"
                      >{{ statuses[r.status]
                      }}<span v-if="r.artifacts?.length">
                        · {{ r.artifacts.length }} 个交付文件</span
                      ></small
                    >
                  </div>
                  <Icon name="next" />
                </button>
                <div
                  v-for="r in activeRuns
                    .filter((r) => r.mode === 'chat' && ['failed', 'paused'].includes(r.status))
                    .slice(0, 1)"
                  :key="r.id"
                  class="error-message"
                >
                  {{ r.error || statuses[r.status]
                  }}<button class="text-button" @click="selectedRunId = r.id; showWork = true">
                    查看并继续
                  </button>
                </div>
              </div>
            </div>
            <div v-if="showMembers" class="member-popover">
              <strong>{{
                conversation.kind === "group" ? "频道成员" : peer?.name
              }}</strong>
              <p v-if="conversation.announcement">
                {{ conversation.announcement }}
              </p>
              <div
                v-for="id in conversation.memberIds"
                :key="id"
                class="member-row"
              >
                <Avatar :agent="agentMap[id]" /><span>{{
                  agentMap[id]?.name
                }}</span
                ><select
                  v-if="conversation.kind === 'group' && id !== 'commander'"
                  :value="conversation.roles?.[id] || 'member'"
                  @change="
                    changeRole(id, ($event.target as HTMLSelectElement).value)
                  "
                >
                  <option value="member">成员</option>
                  <option value="admin">管理员</option>
                </select>
                <button v-if="id !== 'commander'" class="text-button" @click="methodActor = agentMap[id]">方法与成长</button>
              </div>
              <p v-if="conversation.kind === 'dm'" class="muted">
                {{ peer?.persona }}
              </p>
              <SocialControls v-if="conversation.kind === 'group'" :key="conversation.id"
                :conversation-id="conversation.id" @changed="refresh" />
            </div>
            <form class="composer" @submit.prevent="send">
              <div class="composer-tools" aria-label="消息工具">
                <AttachmentPicker :conversation-id="activeId" v-model="attachmentIds" />
                <StickerPicker :key="activeId" @select="insertSticker" />
                <details ref="modeMenu" class="composer-mode composer-tool">
                <summary :aria-label="`处理方式：${mode === 'auto' ? '自动判断' : mode === 'chat' ? '闲聊' : mode === 'task' ? '任务' : '协作'}`" :title="`处理方式：${mode === 'auto' ? '自动判断' : mode === 'chat' ? '闲聊' : mode === 'task' ? '任务' : '协作'}`"><Icon :name="mode === 'auto' ? 'activity' : mode === 'chat' ? 'chat' : mode === 'task' ? 'folder' : 'users'" :size="17" /></summary>
                <div class="composer-tool-panel">
                  <p v-if="guidingTask" class="field-hint">当前任务可继续引导或询问进度。</p>
                <button
                  type="button"
                  v-for="item in [
                    { id: 'auto', text: '自动判断', icon: 'activity' },
                    { id: 'chat', text: '闲聊', icon: 'chat' },
                    { id: 'task', text: '任务', icon: 'folder' },
                    { id: 'swarm', text: '协作', icon: 'users' },
                  ] as const"
                  :key="item.id"
                  :class="{ active: mode === item.id }"
                  @click="setMode(item.id)"
                >
                  <Icon :name="item.icon" :size="15" />{{ item.text }}</button
                >
                </div>
              </details>
              </div>
              <div class="composer-input">
                <div v-if="mentionOptions.length" class="mention-options" aria-label="提及人物">
                  <button v-for="actor in mentionOptions" :key="actor.id" type="button"
                    @mousedown.prevent @click="insertMention(actor.name)">
                    <span>{{ actor.name }}</span><small>{{ actor.faction }}</small>
                  </button>
                </div>
                <textarea
                  v-model="draft"
                  rows="2"
                  :placeholder="
                    mode === 'task' || mode === 'swarm' ? '描述委托、文件位置和期望结果…' : '想说些什么呢…'
                  "
                  aria-label="消息输入"
                  @keydown="keydown"
                  @compositionstart="composing = true"
                  @compositionend="composing = false"
                /><button
                  class="send-button"
                  :disabled="sending || !draft.trim() || !online"
                  aria-label="发送消息"
                >
                  <Icon :name="sending ? 'loading' : 'send'" /><span>发送</span>
                </button>
              </div>
              <footer class="composer-footer">
                <span>{{
                  mode === 'auto' ? '自动判断聊天、追问或委托 · 工具权限仍按任务审批'
                    : mode === 'chat' ? '仅聊天 · 不操作文件' : '任务内自主执行 · 重要操作集中确认'
                }}</span
                ><span>Enter 发送 · Shift + Enter 换行</span>
              </footer>
            </form>
          </section>
          <div v-else class="empty-state">
            <Icon name="chat" :size="50" />
            <h2>选择一位伙伴，开始对话</h2>
            <button class="soft-button" @click="showSettings = true">
              连接港区成员
            </button>
          </div>
        </section>
        <MomentsPage v-if="view === 'circle'" :agents="agents" :user="data?.user" />
      </template>
    </main>
    <Transition name="drawer"><MethodPanel v-if="methodActor" :key="methodActor.id" :actor-id="methodActor.id" :name="methodActor.name" @close="methodActor = undefined" @inspect="selectedRunId = $event; methodActor = undefined; showWork = true" /></Transition>
    <Transition name="drawer"
      ><WorkDrawer
        v-if="showWork"
        :run="selectedRun"
        :runs="runs"
        :agents="agents"
        @close="showWork = false"
        @select="selectedRunId = $event"
        @changed="refresh"
    /></Transition>
    <Transition name="modal"
      ><SettingsPanel
        v-if="showSettings && workspace"
        :workspace="workspace"
        @close="showSettings = false"
        @saved="refresh"
    /></Transition>

  </div>
</template>
