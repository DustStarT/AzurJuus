<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref } from "vue";
import { api } from "../../shared/api";
import type { Agent, Post } from "../../shared/types";
import Avatar from "../../shared/Avatar.vue";
import Icon from "../../shared/Icon.vue";

type MomentsStatus = {
  reason:
    | "mind_disabled"
    | "paused"
    | "disabled"
    | "no_actors"
    | "quiet"
    | "busy"
    | "budget"
    | "pending"
    | "waiting";
  nextConsiderAt: number | null;
  pendingCount: number;
  consideredCount: number;
};
const props = defineProps<{ agents: Agent[]; user?: Agent }>();
const page = ref<"list" | "detail" | "compose">("list");
const selectedPostId = ref("");
const feed = ref<HTMLElement>(),
  detailTitle = ref<HTMLElement>();
const failedMedia = ref(new Set<string>());
const pendingLikes = ref(new Set<string>()),
  pendingComments = ref(new Set<string>());
const activePost = computed(() =>
  posts.value.find((post) => post.id === selectedPostId.value),
);
let listScroll = 0,
  returnFocus: HTMLElement | null = null,
  disposed = false,
  refreshPending = false;
let activeLoad: Promise<void> | undefined;
function hasMedia(post: Post) {
  return !!post.mediaUrl && !failedMedia.value.has(post.mediaUrl);
}
function failMedia(post: Post) {
  if (post.mediaUrl) failedMedia.value.add(post.mediaUrl);
}
function audienceLabel(post: Post) {
  return post.legacyArchived
    ? "历史归档"
    : !visible(post)
      ? "仅查看"
      : post.audience?.kind === "selected"
        ? "指定成员"
        : "港区公开";
}
function rememberList() {
  listScroll = feed.value?.scrollTop || 0;
  returnFocus =
    document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
}
async function openPost(post: Post) {
  rememberList();
  selectedPostId.value = post.id;
  page.value = "detail";
  await nextTick();
  detailTitle.value?.focus({ preventScroll: true });
}
async function backToList() {
  page.value = "list";
  await nextTick();
  if (feed.value) feed.value.scrollTop = listScroll;
  if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
}
async function composePost() {
  rememberList();
  page.value = "compose";
  await nextTick();
  document
    .querySelector<HTMLTextAreaElement>('[aria-label="动态内容"]')
    ?.focus();
}
const posts = ref<Post[]>([]),
  cursor = ref<string | null>(null),
  loading = ref(false),
  error = ref("");
const status = ref<MomentsStatus | null>(null);
const draft = ref(""),
  audience = ref<"port" | "selected">("port"),
  selected = ref<string[]>([]);
const comments = ref<Record<string, string>>({}),
  sending = ref(false);
const people = computed(() =>
  Object.fromEntries(
    [...(props.user ? [props.user] : []), ...props.agents].map((a) => [
      a.id,
      a,
    ]),
  ),
);
const statusHint = computed(() => {
  const value = status.value;
  if (!value) return "";
  const reasons: Record<Exclude<MomentsStatus["reason"], "waiting">, string> = {
    mind_disabled: "心智系统未启用，人物暂不会自主发动态。",
    paused: "自主生活已暂停，人物暂不会发动态。",
    disabled: "自主朋友圈已关闭，可在自主生活设置中启用。",
    no_actors: "当前没有可参与的港区成员。",
    quiet: "当前处于免打扰时段，人物会在时段结束后再考虑。",
    busy: "当前有任务进行，人物的自主动态会稍后再考虑。",
    budget: "本小时自主模型额度已用完，额度恢复后再考虑。",
    pending: "已有活动或互动等待人物考虑。",
  };
  if (value.reason !== "waiting") return reasons[value.reason];
  if (!value.nextConsiderAt) return "人物会在有新活动或合适的想法时考虑分享。";
  if (value.nextConsiderAt * 1000 <= Date.now())
    return "已有到期的想法，人物会在运行时自主决定是否分享。";
  return `下一次自主分享判断预计在 ${new Date(value.nextConsiderAt * 1000).toLocaleString()} 后；人物也可以选择不发。`;
});
function visible(post: Post) {
  return (
    !post.legacyArchived &&
    (post.authorId === props.user?.id ||
      post.audience?.kind === "port" ||
      post.audience?.actorIds.includes(props.user?.id || "commander"))
  );
}
function date(value: string) {
  return new Date(value).toLocaleString();
}
type PostPage = { posts: Post[]; nextCursor: string | null };
function load(more = false): Promise<void> {
  if (disposed) return Promise.resolve();
  if (activeLoad) {
    if (!more) refreshPending = true;
    return activeLoad;
  }
  activeLoad = fetchPosts(more).finally(() => {
    activeLoad = undefined;
  });
  return activeLoad;
}
async function fetchPosts(more = false): Promise<void> {
  loading.value = true;
  try {
    // Re-fetch through the oldest loaded ID, so events also update page 2+.
    // Commit one merged window only after all pages succeed.
    const boundary = more ? undefined : posts.value.at(-1)?.id;
    let before = more ? cursor.value : null;
    const merged = new Map<string, Post>(
      more ? posts.value.map((post) => [post.id, post]) : [],
    );
    let next: string | null = null;
    do {
      const params = new URLSearchParams({ mode: "observe", limit: "20" });
      if (before) params.set("before", before);
      const result = await api<PostPage>(`/api/posts?${params}`);
      if (disposed) return;
      for (const post of result.posts) merged.set(post.id, post);
      next = result.nextCursor;
      if (more || !boundary || merged.has(boundary) || !next || next === before)
        break;
      before = next;
    } while (true);
    posts.value = [...merged.values()];
    cursor.value = next;
    if (page.value === "detail" && !activePost.value) {
      error.value = "这条动态暂时不可用。";
      await backToList();
    }
    if (!posts.value.length) {
      try {
        status.value = await api<MomentsStatus>("/api/posts/status");
      } catch {
        status.value = null;
      }
    }
  } catch (e) {
    if (!disposed) error.value = (e as Error).message;
  } finally {
    loading.value = false;
    if (refreshPending && !disposed) {
      refreshPending = false;
      await fetchPosts();
    }
  }
}
async function publish() {
  if (
    !draft.value.trim() ||
    sending.value ||
    (audience.value === "selected" && !selected.value.length)
  )
    return;
  sending.value = true;
  error.value = "";
  try {
    await api("/api/posts/publish", {
      body: draft.value,
      audience: {
        kind: audience.value,
        actorIds: audience.value === "selected" ? selected.value : [],
      },
    });
    draft.value = "";
    page.value = "list";
    listScroll = 0;
    await load();
    await nextTick();
    if (feed.value) feed.value.scrollTop = 0;
    document
      .querySelector<HTMLElement>('[aria-label="新建动态"]')
      ?.focus({ preventScroll: true });
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    sending.value = false;
  }
}
async function like(post: Post) {
  if (!visible(post) || pendingLikes.value.has(post.id)) return;
  pendingLikes.value.add(post.id);
  error.value = "";
  try {
    await api("/api/posts/like", { postId: post.id });
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    pendingLikes.value.delete(post.id);
  }
}
async function comment(post: Post) {
  const body = comments.value[post.id]?.trim();
  if (!body || !visible(post) || pendingComments.value.has(post.id)) return;
  pendingComments.value.add(post.id);
  error.value = "";
  try {
    await api("/api/posts/comment", { postId: post.id, body });
    if (comments.value[post.id]?.trim() === body) comments.value[post.id] = "";
    await load();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    pendingComments.value.delete(post.id);
  }
}
function onEvent(event: Event) {
  const kind = (event as CustomEvent<{ type: string }>).detail?.type;
  if (
    kind === "post.created" ||
    kind === "post.comment.created" ||
    kind === "workspace.changed"
  )
    void load();
}
onMounted(() => {
  void load();
  window.addEventListener("azur-run-event", onEvent);
});
onUnmounted(() => {
  disposed = true;
  window.removeEventListener("azur-run-event", onEvent);
});
</script>

<template>
  <section class="circle-layout moments-page" aria-label="港区动态">
    <header class="circle-toolbar">
      <h1>港区动态<span>JUUSTAGRAM</span></h1>
      <button
        v-if="page === 'list'"
        type="button"
        class="moments-create icon-button"
        aria-label="新建动态"
        title="新建动态"
        @click="composePost"
      >
        <Icon name="plus" :size="20" />
      </button>
      <button
        v-else
        type="button"
        class="moments-back text-button"
        @click="backToList"
      >
        <Icon name="back" :size="17" />返回动态
      </button>
    </header>
    <p v-if="loading" class="muted moments-loading" role="status">正在加载…</p>
    <p v-if="error" class="error-message" role="alert">
      {{ error
      }}<button
        class="text-button"
        @click="
          error = '';
          load();
        "
      >
        重试刷新
      </button>
    </p>
    <form
      v-if="page === 'compose'"
      class="moments-composer"
      @submit.prevent="publish"
    >
      <header>
        <h2>发布动态</h2>
        <span class="muted">{{ draft.length }} / 4000</span>
      </header>
      <textarea
        v-model="draft"
        maxlength="4000"
        placeholder="分享你的想法…"
        aria-label="动态内容"
      />
      <div class="moments-compose-actions">
        <label
          >可见范围
          <select v-model="audience">
            <option value="port">港区公开</option>
            <option value="selected">指定成员</option>
          </select></label
        >
        <button
          class="primary-button"
          :disabled="
            sending ||
            !draft.trim() ||
            (audience === 'selected' && !selected.length)
          "
        >
          {{ sending ? "发布中…" : "发布动态" }}
        </button>
      </div>
      <div v-if="audience === 'selected'" class="moments-audience">
        <label v-for="agent in agents" :key="agent.id"
          ><input v-model="selected" type="checkbox" :value="agent.id" />{{
            agent.name
          }}</label
        >
      </div>
    </form>
    <div
      v-show="page === 'list'"
      ref="feed"
      class="post-list moments-feed"
      :aria-busy="loading"
    >
      <div v-if="!posts.length && !loading" class="empty-state">
        <Icon name="circle" :size="42" />
        <h2>这里还没有动态。</h2>
        <p>分享一条想法，或等港区成员有话想说。</p>
        <p v-if="statusHint">{{ statusHint }}</p>
      </div>
      <button
        v-for="post in posts"
        :key="post.id"
        type="button"
        class="moments-card"
        :data-post-id="post.id"
        @click="openPost(post)"
      >
        <span class="moments-person"
          ><Avatar :agent="people[post.authorId]" /><strong>{{
            people[post.authorId]?.name || "港区成员"
          }}</strong></span
        >
        <img
          v-if="hasMedia(post)"
          class="moments-thumb"
          :src="post.mediaUrl"
          alt=""
          loading="lazy"
          @error="failMedia(post)"
        />
        <span class="moments-copy"
          ><span class="moments-excerpt">{{ post.body || post.excerpt }}</span
          ><small
            >{{ date(post.createdAt) }} · {{ audienceLabel(post) }}</small
          ></span
        >
        <span class="moments-count"
          ><Icon name="heart" :size="21" /><strong>{{
            post.likes
          }}</strong></span
        >
        <Icon name="next" class="moments-arrow" :size="18" />
      </button>
      <button
        v-if="cursor"
        class="soft-button moments-more"
        :disabled="loading"
        @click="load(true)"
      >
        查看更多
      </button>
    </div>
    <article
      v-if="page === 'detail' && activePost"
      class="moment-detail"
      :class="{ 'moment-detail--media': hasMedia(activePost) }"
      :data-post-id="activePost.id"
      aria-label="动态详情"
    >
      <div v-if="hasMedia(activePost)" class="moment-art">
        <img
          :src="activePost.mediaUrl"
          alt="动态配图"
          @error="failMedia(activePost)"
        />
      </div>
      <section class="moment-discussion">
        <header class="moment-author">
          <Avatar :agent="people[activePost.authorId]" />
          <div>
            <h2 ref="detailTitle" tabindex="-1">
              {{ people[activePost.authorId]?.name || "港区成员" }}
            </h2>
            <small
              >{{ date(activePost.createdAt) }} ·
              {{ audienceLabel(activePost) }}</small
            >
          </div>
        </header>
        <div class="moment-reading">
          <p class="moments-body">
            {{ activePost.body || activePost.excerpt }}
          </p>
          <p v-if="activePost.legacyArchived" class="muted">
            历史记录只有摘要，无法恢复原文。
          </p>
          <h3 class="moment-comments-title">
            评论 <span>{{ activePost.comments.length }}</span>
          </h3>
          <p v-if="!activePost.comments.length" class="muted">还没有评论。</p>
          <div
            v-for="item in activePost.comments"
            :key="item.id"
            class="moments-comment"
          >
            <Avatar :agent="people[item.authorId]" />
            <div>
              <strong>{{ people[item.authorId]?.name || "成员" }}</strong>
              <p>{{ item.body }}</p>
            </div>
          </div>
        </div>
        <footer class="moment-footer">
          <div class="moments-actions">
            <button
              type="button"
              class="text-button"
              :class="{ liked: activePost.likedByUser }"
              :aria-label="activePost.likedByUser ? '取消点赞' : '点赞'"
              :aria-pressed="activePost.likedByUser"
              :disabled="
                !visible(activePost) || pendingLikes.has(activePost.id)
              "
              @click="like(activePost)"
            >
              <Icon name="heart" :size="20" />{{
                activePost.likedByUser ? "已点赞" : "点赞"
              }}
            </button>
            <span>{{ activePost.likes }} 人赞同</span>
            <span v-if="!visible(activePost)" class="moment-readonly">{{
              activePost.legacyArchived
                ? "历史归档，仅供查看"
                : "仅查看，不可参与互动"
            }}</span>
          </div>
          <form
            v-if="visible(activePost)"
            class="moments-comment-form"
            @submit.prevent="comment(activePost)"
          >
            <input
              v-model="comments[activePost.id]"
              maxlength="1000"
              placeholder="写条评论…"
              :aria-label="`评论${people[activePost.authorId]?.name || '成员'}的动态`"
              @keydown.enter="$event.isComposing && $event.preventDefault()"
            />
            <button
              class="primary-button"
              type="submit"
              :disabled="
                !comments[activePost.id]?.trim() ||
                pendingComments.has(activePost.id)
              "
            >
              发送
            </button>
          </form>
        </footer>
      </section>
    </article>
  </section>
</template>
