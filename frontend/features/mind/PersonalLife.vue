<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue';
import { api } from '../../shared/api';
const props = defineProps<{actorId:string;view:string}>();
type Goal = {id:string;title:string;motivation:string;nextStep:string;status:string;reason:string;evidence:string[]};
type Activity = {id:string;title:string;purpose:string;status:string;reason:string;onlineSeconds:number};
type LifeEvent = {id:string;seq:number;kind:string;actorId:string;text:string;at:number;sourceKind:string};
type Profile = {version:number;status:string;interpretation:Record<string,string[]|string>};
function display(value:string[]|string|undefined) { return Array.isArray(value) ? value.join('\n') : value || ''; }
const goals = ref<Goal[]>([]), activities = ref<Activity[]>([]), events = ref<LifeEvent[]>([]), profile = ref<Profile>();
const beliefs = ref<{text:string;kind:string;stable?:boolean;confidence:number;sourceIds:string[]}[]>([]);
const error = ref(''), cursor = ref<number|null>(null), busy = ref(false), editing = ref(false);
const draft = ref({id:'',title:'',motivation:'',nextStep:'',status:'active',reason:''});
const labels:Record<string,string> = {active:'进行中',invited:'等待同伴决定',suspended:'暂停',paused:'暂停',completed:'已完成',abandoned:'已放弃',cancelled:'已结束',declined:'未接受'};
const fields = [{key:'values',label:'核心价值'},{key:'tendencies',label:'行为倾向'},{key:'selfImage',label:'自我认识'},{key:'interests',label:'兴趣'},{key:'sensitiveSituations',label:'敏感情境'},{key:'methods',label:'方法偏好'},{key:'exceptions',label:'例外'},{key:'uncertainty',label:'不确定性'}];
const interpretation = ref<Record<string,string>>({});
let generation=0,active=true;
const traces=ref<{id:string;status:string;stateVersion?:number;waitSeconds?:number;intent?:{purpose:string;method:string};frame?:{interpretation:string};sourceIds?:string[]}[]>([]);
async function diagnostics() { try { traces.value=(await api<{decisions:typeof traces.value}>(`/api/actors/${props.actorId}/decisions`)).decisions; } catch(e) { error.value=(e as Error).message; } }
async function load() {
  const request=++generation;
  try {
    const base = `/api/actors/${props.actorId}`;
    const [g,l,p,b] = await Promise.all([api<{goals:Goal[]}>(base+'/goals'),api<{activities:Activity[];events:LifeEvent[];nextCursor:number|null}>(base+'/life'),api<Profile>(base+'/mind-profile'),api<{beliefs:typeof beliefs.value}>(base+'/beliefs')]);
    if (!active || request!==generation)return;
    goals.value=g.goals; activities.value=l.activities; events.value=l.events; cursor.value=l.nextCursor; profile.value=p; beliefs.value=b.beliefs;
    if (!editing.value) interpretation.value=Object.fromEntries(fields.map(f=>[f.key,display(p.interpretation[f.key])]));
  } catch(e) { error.value=(e as Error).message; }
}
async function saveGoal() {
  busy.value=true; error.value='';
  try {
    const {id,...data}=draft.value;
    await api(`/api/actors/${props.actorId}/goals${id ? '/'+id : ''}`,data);
    draft.value={id:'',title:'',motivation:'',nextStep:'',status:'active',reason:''}; await load();
  } catch(e) { error.value=(e as Error).message; } finally { busy.value=false; }
}
async function saveProfile(reset=false) {
  busy.value=true; error.value='';
  try {
    await api(`/api/actors/${props.actorId}/mind-profile`,{interpretation:reset ? null : Object.fromEntries(fields.map(f=>[f.key,['selfImage','uncertainty'].includes(f.key) ? interpretation.value[f.key] || '' : (interpretation.value[f.key] || '').split('\n').map(t=>t.trim()).filter(Boolean)]))});
    editing.value=false; await load();
  } catch(e) { error.value=(e as Error).message; } finally { busy.value=false; }
}
async function more() {
  try { const page=await api<{events:LifeEvent[];nextCursor:number|null}>(`/api/actors/${props.actorId}/life?before=${cursor.value}`); events.value.push(...page.events); cursor.value=page.nextCursor; }
  catch(e) { error.value=(e as Error).message; }
}
async function resumeActivity(activityId:string) {
  busy.value=true; error.value='';
  try {
    await api(`/api/actors/${props.actorId}/life/${activityId}/resume`,{});
    await load();
  } catch(e) { error.value=(e as Error).message; }
  finally { busy.value=false; }
}
function changed(event:Event) { if ((event as CustomEvent).detail?.actorId===props.actorId)load(); }
watch(()=>[props.actorId,props.view],load);
onMounted(()=>{load();window.addEventListener('azur-mind-changed',changed);});
onUnmounted(()=>{active=false;window.removeEventListener('azur-mind-changed',changed);});
</script>
<template>
  <section aria-label="持续人物心智">
    <p v-if="error" class="error-message" role="alert">{{ error }}</p>
    <template v-if="view==='background'">
      <h3>人格资料解释</h3><p class="muted">背景塑造注意、取舍与方法，不需要在每次对话里复述。原作身份与核心价值不会在生活中被自动改写。</p>
      <p v-if="profile?.status==='pending'" class="muted">等待下一次认知调用整理资料；查看此页不会调用模型。</p>
      <template v-if="!editing"><div v-for="field in fields" :key="field.key"><h4>{{field.label}}</h4><p>{{display(profile?.interpretation[field.key]) || '尚未整理'}}</p></div>
        <button class="soft-button" @click="editing=true">编辑人格解释</button></template>
      <form v-else @submit.prevent="saveProfile()"><label v-for="field in fields" :key="field.key">{{field.label}}（每行一项）<textarea v-model="interpretation[field.key]" rows="2"></textarea></label>
        <button class="primary-button" :disabled="busy">保存人格解释</button><button type="button" class="text-button" @click="editing=false; load()">取消</button></form>
      <button class="text-button" :disabled="busy" @click="saveProfile(true)">恢复为依据资料重新整理</button>
    </template>
    <template v-if="view==='goals'">
      <h3>个人长期目标</h3><p class="muted">最多三个活跃目标。活动记录是模拟生活的进展依据，不代表真实工具成果。</p>
      <article v-for="goal in goals" :key="goal.id" class="assignment"><strong>{{goal.title}}</strong> · {{labels[goal.status] || goal.status}}
        <p>{{goal.motivation}}</p><p>下一步：{{goal.nextStep}}</p><p v-if="goal.reason">原因：{{goal.reason}}</p><p class="muted">已记录 {{goal.evidence?.length || 0}} 条进展依据</p>
        <button class="text-button" @click="draft={id:goal.id,title:goal.title,motivation:goal.motivation,nextStep:goal.nextStep,status:goal.status,reason:goal.reason || ''}">编辑或暂停</button></article>
      <p v-if="!goals.length" class="muted">尚无个人目标；人物可以依据兴趣提出，也可以由你设定。</p>
      <form @submit.prevent="saveGoal"><h4>{{draft.id ? '编辑目标' : '添加目标'}}</h4>
        <label>目标<input v-model="draft.title" maxlength="100" required></label><label>动机<textarea v-model="draft.motivation" maxlength="400" required></textarea></label><label>下一步<textarea v-model="draft.nextStep" maxlength="400" required></textarea></label>
        <label>状态<select v-model="draft.status" aria-label="目标状态"><option value="active">进行中</option><option value="paused">暂停</option><option value="completed">完成</option><option value="abandoned">放弃</option></select></label>
        <label v-if="draft.status!=='active'">改变原因<input v-model="draft.reason" maxlength="400" required></label><button class="primary-button" :disabled="busy">保存目标</button>
      </form>
    </template>
    <template v-if="view==='state'">
      <h3>当前生活活动</h3><article v-for="a in activities.filter(a=>['active','invited','suspended'].includes(a.status))" :key="a.id" class="assignment"><strong>{{a.title}}</strong> · {{labels[a.status]}}<p>{{a.purpose}}</p><p v-if="a.reason">{{a.reason}}</p><p v-if="a.status==='suspended'" class="muted">此前在线进度已保留；继续后只从现在起推进。</p><button v-if="a.status==='suspended'" class="soft-button" :disabled="busy" @click="resumeActivity(a.id)">继续活动</button></article>
      <p v-if="!activities.some(a=>['active','invited','suspended'].includes(a.status))" class="muted">目前没有进行中的生活活动。</p>
      <details @toggle="($event.target as HTMLDetailsElement).open && diagnostics()"><summary>开发诊断：认知记录</summary>
        <p class="muted">记录简短判断、行动意图和来源，供排查使用；不代表模型完整的真实思维链。</p>
        <article v-for="t in traces" :key="t.id" class="assignment"><small>{{t.status}} · 状态版本 {{t.stateVersion ?? '待提交'}}</small><p>{{t.frame?.interpretation}}</p><p>{{t.intent?.purpose}}</p><p>{{t.intent?.method}}</p><small class="muted">{{t.sourceIds?.join('、')}}</small></article>
      </details>
    </template>
    <template v-if="view==='relationships' || view==='state'">
      <h3>{{view==='relationships' ? '关系判断' : '形成中的认识与习惯'}}</h3>
      <article v-for="(b,i) in beliefs.filter(b=>(view==='relationships')===(b.kind==='relationship'))" :key="i" class="assignment"><p>{{b.text}}</p><small class="muted">{{b.kind==='habit' && !b.stable ? '习惯候选，证据不足以视为稳定变化' : '可修正的判断'}} · {{b.sourceIds.length}} 条依据</small></article>
    </template>
    <template v-if="view==='experiences'">
      <h3>生活记录</h3><p class="muted">这些是在线发生的模拟活动。你在此观察不会让其他人物自动知情。</p>
      <article v-for="event in events" :key="event.id" class="assignment"><small>{{new Date(event.at*1000).toLocaleString()}} · 模拟生活<span v-if="event.kind==='dialogue'"> · {{event.actorId===props.actorId ? '本人发言' : '同伴发言'}}</span></small><p>{{event.text}}</p></article>
      <p v-if="!events.length" class="muted">还没有生活事件。</p><button v-if="cursor" class="text-button" @click="more">更早的生活记录</button>
    </template>
  </section>
</template>
