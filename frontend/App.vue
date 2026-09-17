<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from "vue";
import { api, useWorkspace } from "./api";
import type { Agent, Conversation, Post, Run } from "./types";
import Icon from "./Icon.vue";
import Avatar from "./Avatar.vue";
import { artworkUrl } from './assets';
import WorkDrawer from "./WorkDrawer.vue";
import SpeechBubbles from "./SpeechBubbles.vue";
import { liveSpeechIds } from './speechQueue';
import MethodPanel from "./MethodPanel.vue";
import SettingsPanel from "./SettingsPanel.vue";
const { workspace, runs, online, error, streams, refresh, start, stop } =
  useWorkspace();
const methodActor = ref<Agent>();
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
  notice = ref(""),
  comment = ref(""),
  postDraft = ref(""),
  publishing = ref(false);
const scroller = ref<HTMLElement>(),
  pageSize = ref(100),
  atBottom = ref(true),
  mobileChat = ref(false);
const data = computed(() => workspace.value?.data);
const userId = computed(() => data.value?.user.id || 'commander');
const agents = computed(() => data.value?.agents || []);
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
    runs.value.find((r) => r.id === selectedRunId.value) || taskRuns.value[0],
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
const posts = computed(() => data.value?.posts || []);
const post = computed(() => posts.value.find((p) => p.id === postId.value));
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
        mode: mode.value === "chat" ? "chat" : "task",
        collaborative: mode.value === "swarm",
        requestId: crypto.randomUUID(),
      },
    );
    draft.value = "";
    activeId.value = result.conversationId;
    selectedRunId.value = result.runId;
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
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && !composing.value) {
    e.preventDefault();
    void send();
  }
}
async function like(p: Post) {
  try {
    await api("/api/posts/like", { postId: p.id });
    await refresh();
  } catch (e) {
    notice.value = (e as Error).message;
  }
}
async function addComment() {
  if (!post.value || !comment.value.trim()) return;
  try {
    await api("/api/posts/comment", {
      postId: post.value.id,
      body: comment.value,
    });
    comment.value = "";
    await refresh();
  } catch (e) {
    notice.value = (e as Error).message;
  }
}
async function publish() {
  if (!postDraft.value.trim()) return;
  try {
    await api("/api/posts/publish", {
      body: postDraft.value,
      authorId:
        workspace.value?.settings.secretaryAgentId || agents.value[0]?.id,
    });
    postDraft.value = "";
    publishing.value = false;
    await refresh();
  } catch (e) {
    notice.value = (e as Error).message;
  }
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
    publishing.value = false;
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
                <Avatar :agent="friend(c)" :group="c.kind === 'group'" />
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
                  </div>
                </article>
                <div
                  v-if="working && !activeStreams.some(([, s]) => !s.done && s.text)"
                  class="typing-indicator"
                >
                  <span></span><span></span><span></span
                  ><small>{{
                    working.status === "waiting_approval"
                      ? "有一项操作需要你的确认"
                      : Object.values(working.runtimeStages || {}).sort((a, b) => b.at - a.at)[0]?.label || "已接收，等待执行"
                  }}</small><button class="text-button" @click="selectedRunId = working.id; showWork = true">查看进度</button>
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
            </div>
            <form class="composer" @submit.prevent="send">
              <div class="composer-mode">
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
                <textarea
                  v-model="draft"
                  rows="2"
                  :placeholder="
                    mode === 'chat'
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
                  mode === "chat"
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
          <header class="circle-toolbar">
            <div>
              <span class="eyebrow">MOMENTS AT THE PORT</span>
              <h1>{{ post ? "这一刻的港区" : "港区日常" }}</h1>
            </div>
            <button
              v-if="post"
              class="soft-button"
              @click="
                postId = '';
                saveSession();
              "
            >
              <Icon name="list" />动态总览</button
            ><button v-else class="soft-button" @click="publishing = true">
              <Icon name="plus" />新动态
            </button>
          </header>
          <div v-if="!post" class="post-list">
            <button
              v-for="p in posts"
              :key="p.id"
              class="post-card"
              @click="
                postId = p.id;
                saveSession();
              "
            >
              <div class="post-person">
                <Avatar :agent="agentMap[p.authorId]" /><strong>{{
                  agentMap[p.authorId]?.name
                }}</strong>
              </div>
              <div class="post-thumb">
                <img
                  v-if="p.mediaUrl || agentMap[p.authorId]?.illustrationUrl"
                  :src="p.mediaUrl || agentMap[p.authorId]?.illustrationUrl"
                  alt="动态配图"
                  loading="lazy"
                /><Icon v-else name="image" :size="35" />
              </div>
              <div class="post-copy">
                <p>{{ p.excerpt }}</p>
                <small
                  >{{ date(p.createdAt) }} ·
                  {{ p.comments.length }} 条留言</small
                >
              </div>
              <span class="post-likes" :class="{ liked: p.likedByUser }"
                ><Icon name="heart" :size="28" /><strong>{{
                  p.likes
                }}</strong></span
              ><Icon class="post-arrow" name="next" />
            </button>
            <div v-if="!posts.length" class="empty-state">
              <Icon name="circle" :size="48" />
              <h3>今天的故事，等你开启</h3>
              <p>伙伴的动态和任务后的感想会出现在这里。</p>
            </div>
          </div>
          <div v-else class="post-detail" :key="post.id">
            <div class="post-art-panel">
              <div class="post-art">
                <img
                  v-if="
                    post.mediaUrl || agentMap[post.authorId]?.illustrationUrl
                  "
                  :src="
                    post.mediaUrl || agentMap[post.authorId]?.illustrationUrl
                  "
                  alt="动态配图"
                />
                <div v-else class="art-placeholder">
                  <Icon name="circle" :size="100" /><span>JUUS / MOMENT</span>
                </div>
              </div>
              <div class="post-art-actions">
                <button
                  class="icon-button"
                  :class="{ liked: post.likedByUser }"
                  aria-label="点赞动态"
                  @click="like(post)"
                >
                  <Icon name="heart" :size="30" /></button
                ><Icon name="chat" :size="28" /><span
                  >{{ post.likes }} 次赞</span
                ><small>{{ date(post.createdAt) }}</small>
              </div>
            </div>
            <div class="post-discussion">
              <header>
                <Avatar :agent="agentMap[post.authorId]" />
                <div>
                  <h2>{{ agentMap[post.authorId]?.name }}</h2>
                  <small>{{ agentMap[post.authorId]?.handle }}</small>
                </div>
                <span class="following">· FOLLOWING ·</span>
              </header>
              <div class="comment-scroll">
                <p class="post-description">{{ post.excerpt }}</p>
                <div class="comments-divider">
                  {{ post.comments.length }} 条留言
                </div>
                <article v-for="c in post.comments" :key="c.id" class="comment">
                  <Avatar :agent="agentMap[c.authorId]" />
                  <div>
                    <strong>{{ agentMap[c.authorId]?.name }}</strong>
                    <p>{{ c.body }}</p>
                  </div>
                </article>
              </div>
              <form class="comment-form" @submit.prevent="addComment">
                <Avatar :agent="data?.user" /><input
                  v-model="comment"
                  placeholder="留下你的回应…"
                  aria-label="动态评论"
                /><button
                  class="icon-button"
                  :disabled="!comment.trim()"
                  aria-label="发送评论"
                >
                  <Icon name="send" />
                </button>
              </form>
            </div>
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
    <div v-if="publishing" class="modal-scrim" @click.self="publishing = false">
      <form class="publish-modal" @submit.prevent="publish">
        <header>
          <h2>分享港区日常</h2>
          <button
            type="button"
            class="icon-button"
            aria-label="关闭发布"
            @click="publishing = false"
          >
            <Icon name="close" />
          </button>
        </header>
        <p class="muted">把这份想法交给秘书，由她分享给港区伙伴。</p>
        <textarea
          v-model="postDraft"
          rows="6"
          placeholder="今天发生了什么？"
          aria-label="动态内容"
        /><button class="primary-button" :disabled="!postDraft.trim()">
          <Icon name="send" />发布动态
        </button>
      </form>
    </div>
  </div>
</template>
