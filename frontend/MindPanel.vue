<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue';
import { api } from './api';
import CharacterCard from './CharacterCard.vue';
import PersonalLife from './PersonalLife.vue';
const view = ref('state');
const views = [{id:'background',label:'背景'},{id:'state',label:'当前状态'},{id:'goals',label:'目标'},{id:'experiences',label:'经历'},{id:'relationships',label:'关系判断'}];
const props = defineProps<{actorId:string}>();
type Source = {text:string;sourceId:string};
type Mind = {version:number;enabled:boolean;data:{focus:Source[];commitments:Source[];mood:string;emotion?:{name:string;target:string;cause:string};mindMood?:{text:string}};anchor:{note:string};originalBackground?:{text:string;source:string;quote:string;sourceKind:string}[];error?:string};
type Experience = {id:string;text:string;sourceSeq:number;kind:string;data:{userCorrection?:string}};
const mind = ref<Mind>();
const experiences = ref<Experience[]>([]);
const relations = ref<{peerId:string;name:string;summary:string;userDefined?:string;background?:unknown[];sharedSources?:unknown[];defaultRelationship?:{familiarity:string}}[]>([]);
const relationshipEdit = ref(''), relationshipText = ref('');
const cursor = ref<number|null>(null), error = ref(''), editing = ref(''), correction = ref('');
let active = true, generation = 0, timer = 0;
async function load() {
  const request = ++generation;
  try {
    const [state, events, peers] = await Promise.all([
      api<Mind>(`/api/actors/${props.actorId}/mind`),
      api<{experiences:Experience[];nextCursor:number|null}>(`/api/actors/${props.actorId}/experiences`),
      api<{relationships:typeof relations.value}>(`/api/actors/${props.actorId}/relationships`)]);
    if (!active || request !== generation || (mind.value && state.version < mind.value.version)) return;
    mind.value = state; experiences.value = events.experiences; cursor.value = events.nextCursor;
    relations.value = peers.relationships.filter(peer => peer.userDefined || peer.background?.length || peer.sharedSources?.length || peer.defaultRelationship?.familiarity === 'familiar');
  } catch(e) { error.value = (e as Error).message; }
}
async function action(path:string, body:unknown = {}) {
  error.value = '';
  try { await api(`/api/actors/${props.actorId}/${path}`, body); await load(); }
  catch(e) { error.value = (e as Error).message; }
}
async function more() {
  try {
    const page = await api<{experiences:Experience[];nextCursor:number|null}>(`/api/actors/${props.actorId}/experiences?before=${cursor.value}`);
    experiences.value.push(...page.experiences.filter(x => !experiences.value.some(y => y.id === x.id)));
    cursor.value = page.nextCursor;
  } catch(e) { error.value = (e as Error).message; }
}
function changed(event:Event) {
  if ((event as CustomEvent).detail?.actorId !== props.actorId) return;
  clearTimeout(timer); timer = window.setTimeout(load, 200);
}
onMounted(() => { load(); window.addEventListener('azur-mind-changed', changed); });
onUnmounted(() => { active = false; clearTimeout(timer); window.removeEventListener('azur-mind-changed', changed); });
</script>
<template>
  <section aria-label="人物经历与关系">
    <nav class="tab-row" aria-label="人物心智分类"><button v-for="item in views" :key="item.id" class="text-button" :aria-pressed="view===item.id" @click="view=item.id">{{item.label}}</button></nav>
    <CharacterCard v-if="view==='background'" :actor-id="actorId" />
    <PersonalLife :actor-id="actorId" :view="view" />
    <p v-if="error" class="error-message">{{ error }}</p>
    <p v-if="mind?.error" class="muted">认知更新暂不可用，聊天与任务仍可继续。</p>
    <label><input type="checkbox" :checked="mind?.enabled" @change="action('mind', {enabled:($event.target as HTMLInputElement).checked})"> 使用持续经历与关系</label>
    <p class="muted">{{ mind?.anchor.note }}</p>
    <template v-if="view==='state'">
    <h3>心境与情绪</h3><p>{{mind?.data.mindMood?.text || '尚未形成持续心境'}}</p>
    <p v-if="mind?.data.emotion">{{mind.data.emotion.name}} · {{mind.data.emotion.target}}：{{mind.data.emotion.cause}}</p>
    <h3>当前关注</h3>
    <p v-for="item in mind?.data.focus || []" :key="item.sourceId">{{ item.text }}</p>
    <p v-if="!mind?.data.focus.length" class="muted">暂时没有待处理的关注事项。</p>
    <h3>记得的承诺</h3>
    <p v-for="item in mind?.data.commitments || []" :key="item.sourceId + item.text">{{ item.text }}</p>
    </template><template v-if="view==='background'">
    <h3>原作资料理解</h3>
    <p class="muted">模型依据剧情、聊天或动态整理的背景，不是她在本应用亲身经历的事。</p>
    <article v-for="item in mind?.originalBackground || []" :key="item.source+item.quote" class="assignment">
      <p>{{item.text}}</p><details><summary>查看原文依据</summary><blockquote>{{item.quote}}</blockquote><a :href="item.source" target="_blank" rel="noreferrer">打开资料</a></details>
    </article>
    <p v-if="!mind?.originalBackground?.length" class="muted">尚无已核对的背景整理。</p>
    </template><template v-if="view==='relationships'">
    <h3>关系与共同经历</h3>
    <p class="muted">这里记录的是她对同伴的认识，双方不必相同。你可以设定既有关系；设定不会伪装成真实任务经历。</p>
    <article v-for="peer in relations" :key="peer.peerId" class="assignment">
      <strong>{{ peer.name }}</strong><p>{{ peer.summary }}</p>
      <button class="text-button" @click="relationshipEdit = peer.peerId; relationshipText = peer.userDefined || ''">设定关系</button>
      <form v-if="relationshipEdit === peer.peerId" @submit.prevent="action(`relationships/${peer.peerId}`, {description:relationshipText}); relationshipEdit = ''">
        <textarea v-model="relationshipText" aria-label="关系说明" maxlength="400" placeholder="例如：常合作的同伴，平时可以直说；涉及新领域仍需核验证据。"></textarea>
        <p class="muted">只影响此人物看待对方的方式。留空保存可移除设定。</p>
        <button class="primary-button">保存关系</button>
      </form>
    </article>
    </template><template v-if="view==='experiences'"><h3>个人经历</h3>
    <p class="muted">删除聊天只隐藏聊天记录；忘记经历会同时排除相关判断和承诺，保留任务审计事实。</p>
    <article v-for="item in experiences" :key="item.id" class="assignment">
      <p>{{ item.text }}</p><p v-if="item.data.userCorrection">你的纠正：{{ item.data.userCorrection }}</p>
      <button class="text-button" @click="editing = item.id; correction = item.data.userCorrection || ''">纠正理解</button>
      <button class="text-button" @click="action(`experiences/${item.id}/forget`)">忘记这段经历</button>
      <form v-if="editing === item.id" @submit.prevent="action(`experiences/${item.id}/correct`, {interpretation:correction}); editing = ''">
        <textarea v-model="correction" aria-label="纠正人物理解" maxlength="800" required></textarea><button class="primary-button">保存纠正</button>
      </form>
    </article>
    <button v-if="cursor" class="text-button" @click="more">更早的经历</button>
    </template>
    <details><summary>清除成长状态</summary><p>清除个人经历、关系推断与承诺，保留原作设定、任务证据与技能。</p><button class="text-button danger" @click="action('mind', {reset:true})">清除这个人物的成长状态</button></details>
  </section>
</template>
