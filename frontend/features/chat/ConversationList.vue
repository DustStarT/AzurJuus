<script setup lang="ts">
import { computed, ref } from "vue";
import type { Agent, Conversation, Run, Snapshot } from "../../shared/types";
import Icon from "../../shared/Icon.vue";
import Avatar from "../../shared/Avatar.vue";
import CreateGroup from "./CreateGroup.vue";

const props = defineProps<{
  snapshot: Snapshot;
  runs: Run[];
  agents: Agent[];
  activeId: string;
  userId: string;
}>();
const emit = defineEmits<{
  select: [conversation: Conversation];
  created: [id: string];
}>();
const search = ref("");
const filter = ref("all");
const showFilters = ref(false);
const agentMap = computed<Record<string, Agent>>(() =>
  Object.fromEntries(
    [...props.agents, props.snapshot.user].map((agent) => [agent.id, agent]),
  ),
);
const conversations = computed(() =>
  props.snapshot.conversations.filter(
    (c) =>
      !c.archived &&
      (!search.value ||
        [c.title, c.preview].some((value) =>
          value?.toLowerCase().includes(search.value.toLowerCase()),
        )) &&
      (filter.value === "all" ||
        filter.value === c.kind ||
        (filter.value === "favorite" &&
          c.memberIds.some((id) => agentMap.value[id]?.favorite))),
  ),
);
const friend = (c: Conversation) =>
  c.memberIds
    .map((id) => agentMap.value[id])
    .find((agent) => agent && agent.id !== props.userId);
function subtitle(c: Conversation): string {
  const running =
    props.runs.find(
      (run) =>
        run.conversationId === c.id &&
        ["running", "waiting_approval"].includes(run.status),
    )?.status === "running";
  if (running) return "正在处理你的委托…";
  const latest = props.snapshot.messages[c.id]?.at(-1);
  const speaker =
    latest?.speakerId === props.userId
      ? "我"
      : latest && agentMap.value[latest.speakerId]?.name;
  return speaker && c.preview
    ? `${speaker}：${c.preview}`
    : c.preview || "今天想聊些什么？";
}
</script>
<template>
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
      <CreateGroup :agents="agents" @created="(id) => emit('created', id)" />
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
      <span>消息列表</span><small>{{ conversations.length }} 个会话</small>
    </div>
    <div class="conversation-list">
      <button
        v-for="c in conversations"
        :key="c.id"
        class="conversation-card"
        :class="{ active: c.id === activeId }"
        @click="emit('select', c)"
      >
        <Avatar
          :agent="friend(c)"
          :group="c.kind === 'group'"
          :hub="c.id === 'port-hub'"
        />
        <div class="conversation-copy">
          <div class="conversation-title">
            <strong>{{ c.title }}</strong
            ><span v-if="c.kind === 'group'" class="group-tag">群聊</span>
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
</template>
