<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from "vue";
import { api, useWorkspace } from "./api";
import type { Agent, Conversation, Run } from "./types";
import Icon from "./Icon.vue";
import Avatar from "./Avatar.vue";
import { artworkUrl } from './assets';
import WorkDrawer from "./WorkDrawer.vue";
import SpeechBubbles from "./SpeechBubbles.vue";
import { liveSpeechIds } from './speechQueue';
import MethodPanel from "./MethodPanel.vue";
import SettingsPanel from "./SettingsPanel.vue";
import AttachmentPicker from './AttachmentPicker.vue';
import StickerPicker from './StickerPicker.vue';
import SocialControls from "./SocialControls.vue";
import CreateGroup from './CreateGroup.vue';
const { workspace, runs, online, error, streams, refresh, start, stop } =
  useWorkspace();
const methodActor = ref<Agent>();
const attachmentIds = ref<string[]>([]);
const view = ref("chat"),
  activeId = ref(""),
  postId = ref(""),
  search = ref(""),
  filter = ref("all"),
  showFilters = ref(false),
  showSettings = ref(false),
  showWork = ref(false),
  showMembers = ref(false),
  selectedRunId = ref("");
const draft = ref(""),
  mode = ref("chat"),
  sending = ref(false),
  composing = ref(false),
  notice = ref("");
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
  (data.value?.conversations || []).filter(
    (c) =>
      !c.archived && (!search.value ||
        [c.title, c.preview].some((s) =>
          s?.toLowerCase().includes(search.value.toLowerCase()),
        )) &&
      (filter.value === "all" ||
        filter.value === c.kind ||
        (filter.value === "favorite" &&
          c.memberIds.some((id) => agentMap.value[id]?.favorite))),
  ),
);
const peer = computed(() =>
  conversation.value?.memberIds
    .map((id) => agentMap.value[id])
    .find((a) => a && a.id !== "commander"),
);
const backdropFailed = ref(false);
watch(() => peer.value?.illustrationUrl, () => { backdropFailed.value = false; });
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
const friend = (c: Conversation): Agent | undefined =>
  c.memberIds
    .map((id) => agentMap.value[id])
    .find((a) => a && a.id !== "commander");
const subtitle = (c: Conversation) =>
  runs.value.find(
    (r) =>
      r.conversationId === c.id &&
      ["running", "waiting_approval"].includes(r.status),
  )?.status === "running"
    ? "正在处理你的委托…"
    : c.preview || "今天想聊些什么？";
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
  sending.value = true;
  notice.value = "";
  try {
    const result = await api<{ runId: string; conversationId: string }>(
      "/api/messages/send",
      {
        conversationId: activeId.value,
        content,
        attachments: attachmentIds.value,
        mentions: conversation.value.kind === 'group'
          ? agents.value.filter(a => content.includes(`@${a.name}`)).map(a => a.id) : [],
        mode: mode.value === "chat" ? "chat" : "task",
        collaborative: mode.value === "swarm",
        requestId: crypto.randomUUID(),
      },
    );
    draft.value = "";
    attachmentIds.value = [];
    activeId.value = result.conversationId;
    if (mode.value !== 'chat' || guidingTask.value) selectedRunId.value = result.runId;
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
    showFilters.value = false;
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
          <aside class="conversation-panel">
            <CreateGroup :agents="agents" @created="async id => { await refresh(); activeId=id; mobileChat=true; }" />
            <div class="list-tools">
              <div class="search-field">
                <Icon name="search" :size="18" /><input
                  v-model="search"
                  aria-label="搜索会话"
                  placeholder="搜索伙伴或频道"
                />
              </div>
              <button
                class="icon-button"
                :class="{ selected: showFilters }"
                aria-label="筛选会话"
                @click="showFilters = !showFilters"
              >
                <Icon name="filter" />
              </button>
            </div>
            <div v-if="showFilters" class="filter-row">
              <button
                v-for="f in [
                  { id: 'all', text: '全部' },
                  { id: 'dm', text: '私聊' },
                  { id: 'group', text: '群聊' },
                  { id: 'favorite', text: '收藏' },
                ]"
                :key="f.id"
                :class="{ active: filter === f.id }"
                @click="filter = f.id"
              >
                {{ f.text }}
              </button>
            </div>
            <div class="list-heading">
              <span>消息列表</span
              ><small>{{ conversations.length }} 个会话</small>
            </div>
            <div class="conversation-list">
              <button
                v-for="c in conversations"
                :key="c.id"
                class="conversation-card"
                :class="{ active: c.id === activeId }"
                @click="selectConversation(c)"
              >
                <Avatar :agent="friend(c)" :group="c.kind === 'group'" :hub="c.id === 'port-hub'" />
                <div class="conversation-copy">
                  <div class="conversation-title">
                    <strong>{{ c.title }}</strong
                    ><span v-if="c.kind === 'group'" class="group-tag"
                      >群聊</span
                    >
                  </div>
                  <p>{{ subtitle(c) }}</p>
                </div>
                <span v-if="c.unreadCount" class="unread-badge">{{
                  c.unreadCount > 99 ? "99+" : c.unreadCount
                }}</span>
              </button>
              <div v-if="!conversations.length" class="empty-state compact">
                <Icon name="search" />
                <p>没有找到相关会话</p>
              </div>
            </div>
          </aside>
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
                v-if="conversation.kind !== 'group' && peer?.illustrationUrl"
                class="character-backdrop"
                :src="backdropFailed ? peer.illustrationUrl : artworkUrl(peer.illustrationUrl)"
                decoding="async"
                @error="backdropFailed = true"
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
                  <h3>{{ peer?.name || "伙伴们" }}在这里</h3>
                  <p>聊聊今天，或交给她一项新的委托。</p>
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
                      ><time>{{ time(m.createdAt) }}</time>
                    </div>
                    <details v-if="m.type === 'task_progress' && !m.metadata?.expression"><summary>历史工作回复</summary><div class="message-bubble">{{ m.body }}</div></details>
                    <SpeechBubbles v-else :text="m.body" :streaming="m.streaming" :self="m.speakerId === userId" :single="m.speakerId === userId" :message-id="m.id" :conversation-id="activeId" @reveal="speechRevealed" />
                    <a v-for="file in m.metadata?.attachments || []" :key="file.id" :href="`/api/attachments/${file.id}?conversationId=${encodeURIComponent(file.conversationId)}`" target="_blank" rel="noreferrer">附件 · {{file.name}}</a>
                  </div>
                </article>
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
                  v-for="r in taskRuns.slice(0, 3)"
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
              <AttachmentPicker :conversation-id="activeId" v-model="attachmentIds" />
              <StickerPicker :key="activeId" @select="insertSticker" />
              <p v-if="guidingTask" class="field-hint">继续发送将补充到当前协作任务。暂停中的任务可在工作抽屉继续。</p>
              <p v-else-if="teamTask" class="field-hint">任务已结束，可以继续讨论结果。新委托请从其他会话发起。</p>
              <div v-else class="composer-mode">
                <button
                  type="button"
                  v-for="item in [
                    { id: 'chat', text: '闲聊', icon: 'chat' },
                    { id: 'task', text: '任务', icon: 'folder' },
                    { id: 'swarm', text: '协作', icon: 'users' },
                  ] as const"
                  :key="item.id"
                  :class="{ active: mode === item.id }"
                  @click="mode = item.id"
                >
                  <Icon :name="item.icon" :size="15" />{{ item.text }}</button
                >
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
                    guidingTask ? '补充要求、纠正方向或提供线索…' : teamTask || mode === 'chat'
                      ? '想说些什么呢…'
                      : '描述任务、文件位置和期望的结果…'
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
                  guidingTask ? '引导当前任务 · 保留已有进度' : teamTask || mode === "chat"
                    ? "仅聊天 · 不操作文件"
                    : "任务内自主执行 · 重要操作集中确认"
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
        <section v-if="view === 'circle'" class="circle-layout">
          <div class="empty-state">
            <Icon name="circle" :size="56" />
            <h2>动态功能完善中</h2>
            <p>港区朋友圈暂时关闭。</p>
          </div>

        </section>
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
