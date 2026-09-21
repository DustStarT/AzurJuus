<script setup lang="ts">
import { ref, watch } from "vue";
import { api } from "./api";
import type { Workspace, Settings, Agent } from "./types";
import Icon from "./Icon.vue";
import Avatar from "./Avatar.vue";
import TerminalConfiguration from './TerminalConfiguration.vue';
const props = defineProps<{ workspace: Workspace }>();
const emit = defineEmits<{ close: []; saved: [] }>();
const tab = ref("connection"),
  error = ref(""),
  saved = ref(false),
  busy = ref(false);
const settings = ref<Settings>({
  ...props.workspace.settings,
  connectedAgentIds: [...props.workspace.settings.connectedAgentIds],
  llmApiKey: "",
});
watch(() => props.workspace.data.agents.map(a=>a.id).join(','), () => {
 settings.value.connectedAgentIds=[...props.workspace.settings.connectedAgentIds];
 settings.value.characterRosterText=props.workspace.settings.characterRosterText;
 settings.value.maxConnectedAgents=props.workspace.settings.maxConnectedAgents;
});
const reduced = ref(localStorage.getItem("azur-reduced-motion") === "true");
const speechPace = ref(localStorage.getItem('azur-speech-pace') || 'natural');
const userName = ref(props.workspace.data.user.name);
const userAvatar = ref(props.workspace.data.user.avatarUrl || '');
const clearing = ref(''), confirmation = ref('');
async function avatarFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  if (!file) return;
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 500000) {
    error.value = '请选择不超过 500KB 的 PNG、JPEG 或 WebP 图片。'; return;
  }
  const reader = new FileReader();
  reader.onload = () => { userAvatar.value = String(reader.result); };
  reader.onerror = () => { error.value = '无法读取头像图片。'; };
  reader.readAsDataURL(file);
}
async function clearData() {
  if (!clearing.value || confirmation.value !== '确认清理') return;
  busy.value = true; error.value = '';
  try {
    await api(clearing.value === 'reset' ? '/api/system/reset' : `/api/system/clear/${clearing.value}`, {});
    // Drop stale stream/UI state only after the server has committed the reset.
    window.location.reload();
  } catch(e) { error.value = (e as Error).message; }
  finally { busy.value = false; }
}
const selected = ref<Agent>(),
  persona = ref("");
const tabs = [
  { id: "connection", label: "连接与工作区", icon: "folder" },
  { id: "agents", label: "港区成员", icon: "users" },
  { id: "world", label: "人物关系与任务", icon: "users" },
  { id: "appearance", label: "外观与动态", icon: "circle" },
  { id: "skills", label: "技能档案", icon: "sparkle" },
  { id: "personal", label: "我的资料与数据", icon: "users" },
] as const;
async function save() {
  busy.value = true;
  error.value = "";
  saved.value = false;
  try {
    if (userName.value !== props.workspace.data.user.name || userAvatar.value !== (props.workspace.data.user.avatarUrl || '')) {
      await api('/api/profile', {name: userName.value, avatar: userAvatar.value});
    }
    await api("/api/workspace/save", {
      workspace: { settings: settings.value },
    });
    await api("/api/window/preset", {
      preset: settings.value.resolutionPreset,
    });
    settings.value.llmApiKey = "";
    localStorage.setItem("azur-reduced-motion", String(reduced.value));
    localStorage.setItem('azur-speech-pace', speechPace.value);
    document.documentElement.classList.toggle("reduced-motion", reduced.value);
    emit("saved");
    saved.value = true;
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
async function favorite(agent: Agent) {
  try {
    await api("/api/agents/favorite", { agentId: agent.id });
    emit("saved");
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function applyPersona() {
  if (!selected.value) return;
  try {
    await api("/api/agents/persona", {
      agentId: selected.value.id,
      systemPrompt: persona.value,
    });
    emit("saved");
    selected.value = undefined;
  } catch (e) {
    error.value = (e as Error).message;
  }
}
async function connectRoster() {
  busy.value = true;
  error.value = "";
  try {
    const names = settings.value.characterRosterText
      .split(/[\n,，、]+/)
      .map((v) => v.trim())
      .filter(Boolean);
    await api("/api/personas/compose", {
      names,
      participantCount: names.length,
    });
    emit("saved");
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
  }
}
</script>
<template>
  <div class="modal-scrim" @click.self="emit('close')">
    <section
      class="settings-modal"
      role="dialog"
      aria-modal="true"
      aria-label="港区设置"
    >
      <header class="drawer-header">
        <div>
          <span class="eyebrow">PREFERENCES</span>
          <h2>港区设置</h2>
        </div>
        <button
          class="icon-button"
          aria-label="关闭设置"
          @click="emit('close')"
        >
          <Icon name="close" />
        </button>
      </header>
      <div class="settings-layout">
        <nav class="settings-nav">
          <button
            v-for="item in tabs"
            :key="item.id"
            :class="{ active: tab === item.id }"
            @click="tab = item.id"
          >
            <Icon :name="item.icon" />{{ item.label }}
          </button>
        </nav>
        <div class="settings-content">
          <p v-if="error" class="error-message" role="alert">{{ error }}</p>
          <template v-if="tab === 'personal'">
            <h3>我的资料</h3>
            <label>希望被怎样称呼<input v-model="userName" maxlength="40" /></label>
            <Avatar :agent="{...workspace.data.user, name:userName, avatarUrl:userAvatar}" />
            <label>更换头像<input type="file" accept="image/png,image/jpeg,image/webp" @change="avatarFile" /></label>
            <button class="text-button" @click="userAvatar = ''">使用文字头像</button>
            <p class="field-hint">图片保存在本地，最大 500KB。点击下方保存设置后生效，新对话会使用你的称呼。</p>
            <div class="section-divider"></div>
            <h3>记录与记忆</h3>
            <p class="muted">清理前先停止所有任务。以下操作均不会删除工作区内的实际文件。</p>
            <label>清理范围<select v-model="clearing" @change="confirmation = ''">
              <option value="">请选择</option>
              <option value="records">清空聊天和工作列表（保留长期记忆）</option>
              <option value="memory">忘记长期经历与关系判断（保留聊天和技能）</option>
              <option value="reset">初始化系统（清除记录、成长、技能与配置）</option>
            </select></label>
            <p v-if="clearing === 'records'" class="field-hint">清空聊天正文，归档工作记录；任务审计记录仍保留，人物可能仍记得长期经历。</p>
            <p v-if="clearing === 'memory'" class="field-hint">清除长期经历、推断与承诺；当前聊天内容仍可作为上下文。要从全新状态开始，请选择初始化。</p>
            <p v-if="clearing === 'reset'" class="error-message">恢复初始角色和设置，需要重新配置模型。此操作不是磁盘安全擦除，历史备份和诊断日志仍可能保留。</p>
            <template v-if="clearing">
              <label>输入“确认清理”后执行<input v-model="confirmation" autocomplete="off" /></label>
              <button class="soft-button danger" :disabled="busy || confirmation !== '确认清理'" @click="clearData">执行清理</button>
            </template>
          </template>
          <template v-if="tab === 'connection'"
            ><span class="eyebrow">01 / MODEL</span>
            <h3>连接你的模型</h3>
            <p class="muted">
              工作在本机完成，模型负责理解、执行与检查。请选择支持工具调用、至少
              64K 上下文的模型。
            </p>
            <label
              >接口地址<input
                v-model="settings.llmBaseUrl"
                placeholder="https://api.deepseek.com/v1" /></label
            ><label
              >模型名称<input
                v-model="settings.llmModel"
                placeholder="模型 ID" /></label
            ><label
              >API Key
              <span
                class="configured"
                v-if="workspace.settings.llmApiKeyConfigured"
                >已安全保存</span
              ><input
                v-model="settings.llmApiKey"
                type="password"
                autocomplete="new-password"
                :placeholder="
                  workspace.settings.llmApiKeyConfigured
                    ? '留空保留现有密钥'
                    : '输入 API Key'
                " /></label
            ><label class="check-label"
              ><input
                type="checkbox"
                v-model="settings.visionEnabled"
              />启用图片输入与截图定位（deepseek-flash 支持）</label
            >
            <div class="section-divider"></div>
            <span class="eyebrow">02 / WORKSPACE</span>
            <h3>授权工作区</h3>
            <label
              >任务文件所在目录<input
                v-model="settings.authorizedWorkspaceRoot"
                placeholder="D:\MyWorkspace"
            /></label>
            <p class="field-hint">
              角色可在此目录内读写文件。执行程序与需要确认的操作会显示在工作抽屉中。
            </p>
            <label
              >秘书<select v-model="settings.secretaryAgentId">
                <option
                  v-for="agent in workspace.data.agents"
                  :key="agent.id"
                  :value="agent.id"
                >
                  {{ agent.name }}
                </option>
              </select></label
            ></template
          >
          <template v-if="tab === 'agents'"
            ><span class="eyebrow">MEMBERS</span>
            <h3>在港的伙伴</h3>
            <p class="muted">选择连接的成员，保留她们各自的表达和工作方法。</p>
            <div
              class="member-setting"
              v-for="agent in workspace.data.agents"
              :key="agent.id"
            >
              <Avatar :agent="agent" />
              <div>
                <strong>{{ agent.name }}</strong
                ><small>{{ agent.faction }}</small>
              </div>
              <button
                class="icon-button"
                :class="{ liked: agent.favorite }"
                :aria-label="`收藏${agent.name}`"
                @click="favorite(agent)"
              >
                <Icon name="heart" /></button
              ><button
                class="text-button"
                @click="
                  selected = agent;
                  persona = agent.systemPrompt || agent.persona || '';
                "
              >
                人设</button
              ><input
                type="checkbox"
                :value="agent.id"
                v-model="settings.connectedAgentIds"
                :aria-label="`连接${agent.name}`"
              />
            </div>
            <div v-if="selected" class="persona-editor">
              <h4>{{ selected.name }} · 人设</h4>
              <textarea v-model="persona" rows="10" /><button
                class="soft-button"
                @click="applyPersona"
              >
                应用人设
              </button>
            </div>
            <label
              >角色名单<textarea
                v-model="settings.characterRosterText"
                rows="4"
              />
            </label>
            <p class="field-hint">
              输入 1–24 位角色的名字，每行一位。历史聊天和用户修改会保留；已核实的人物背景与关系按原作身份匹配。
            </p>
            <button class="soft-button" :disabled="busy" @click="connectRoster">
              根据名单连接角色
            </button></template
          >
          <TerminalConfiguration v-if="tab === 'world'" @changed="emit('saved')" />
          <template v-if="tab === 'appearance'"
            ><span class="eyebrow">DISPLAY & LIFE</span>
            <h3>让港区慢下来</h3>
            <label>消息节奏<select v-model="speechPace"><option value="instant">即时</option><option value="natural">自然</option><option value="calm">舒缓</option></select></label>
            <label
              >窗口尺寸<select v-model="settings.resolutionPreset">
                <option value="compact">紧凑</option>
                <option value="balanced">标准</option>
                <option value="expanded">宽阔</option>
              </select></label
            ><label class="switch-row"
              ><span
                ><strong>减少动态效果</strong
                ><small>减少转场与移动，保留即时反馈</small></span
              ><input type="checkbox" v-model="reduced" /></label
            ><label class="switch-row"
              ><span
                ><strong>闲暇时发布动态</strong
                ><small>有工作时优先处理委托</small></span
              ><input
                type="checkbox"
                v-model="settings.allowIdleSocial" /></label
            ><label
              >动态间隔（分钟）<input
                type="number"
                min="5"
                v-model.number="settings.socialIntervalMinutes" /></label
          ></template>
          <template v-if="tab === 'skills'"
            ><span class="eyebrow">SKILL ARCHIVE</span>
            <h3>技能档案</h3>
            <p class="muted">
              角色已有的方法和经验。工具执行情况以任务记录为准。
            </p>
            <details
              class="tool-record"
              v-for="(skill, index) in workspace.data.skillCatalog"
              :key="index"
            >
              <summary>
                {{ (skill as { name?: string }).name || "技能 " + (index + 1) }}
              </summary>
              <pre>{{ JSON.stringify(skill, null, 2) }}</pre>
            </details></template
          >
        </div>
      </div>
      <footer class="settings-footer">
        <span class="muted">{{
          saved ? "设置已保存" : "AZURJUUS / LOCAL FIRST"
        }}</span
        ><button class="primary-button" :disabled="busy" @click="save">
          <Icon :name="saved ? 'check' : 'shield'" />{{
            busy ? "保存中…" : "保存设置"
          }}
        </button>
      </footer>
    </section>
  </div>
</template>
