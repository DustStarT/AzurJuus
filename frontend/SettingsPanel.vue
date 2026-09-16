<script setup lang="ts">
import { ref } from "vue";
import { api } from "./api";
import type { Workspace, Settings, Agent } from "./types";
import Icon from "./Icon.vue";
import Avatar from "./Avatar.vue";
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
const reduced = ref(localStorage.getItem("azur-reduced-motion") === "true");
const selected = ref<Agent>(),
  persona = ref("");
const tabs = [
  { id: "connection", label: "连接与工作区", icon: "folder" },
  { id: "agents", label: "港区成员", icon: "users" },
  { id: "appearance", label: "外观与动态", icon: "circle" },
  { id: "skills", label: "技能档案", icon: "sparkle" },
] as const;
async function save() {
  busy.value = true;
  error.value = "";
  saved.value = false;
  try {
    await api("/api/workspace/save", {
      workspace: { settings: settings.value },
    });
    await api("/api/window/preset", {
      preset: settings.value.resolutionPreset,
    });
    settings.value.llmApiKey = "";
    localStorage.setItem("azur-reduced-motion", String(reduced.value));
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
              />此模型支持图像输入，可使用截图定位</label
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
              输入 5–10 位角色的名字，每行一位。历史聊天和角色资料会保留。
            </p>
            <button class="soft-button" :disabled="busy" @click="connectRoster">
              根据名单连接角色
            </button></template
          >
          <template v-if="tab === 'appearance'"
            ><span class="eyebrow">DISPLAY & LIFE</span>
            <h3>让港区慢下来</h3>
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
