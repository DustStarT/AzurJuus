export interface Agent {
  id: string;
  name: string;
  sourceCharacter?: string;
  handle: string;
  faction: string;
  initials: string;
  avatarUrl?: string;
  illustrationUrl?: string;
  favorite: boolean;
  persona?: string;
  systemPrompt?: string;
  capabilities: string[];
  skills?: unknown[];
}
export interface Conversation {
  id: string;
  kind: string;
  title: string;
  preview: string;
  memberIds: string[];
  unreadCount: number;
  roles?: Record<string, string>;
  announcement?: string;
  backgroundId?: string;
  archived?: boolean;
}
export interface Message {
  id: string;
  speakerId: string;
  body: string;
  type: string;
  createdAt: string;
  metadata?: { runId?: string; expression?: boolean; segments?: string[]; sourceIds?: string[]; attachments?:{id:string;name:string;mime:string;conversationId:string}[] };
}
export interface Post {
  id: string;
  authorId: string;
  excerpt: string;
  mediaUrl?: string;
  likes: number;
  likedByUser: boolean;
  comments: { id: string; authorId: string; body: string }[];
  createdAt: string;
}
export interface Artifact {
  path: string;
  bytes: number;
  sha256: string;
}
export interface Result {
  summary: string;
  artifacts: Artifact[];
  checks: { callId: string; description: string }[];
  unresolved: string[];
}
export interface Assignment {
  id: string;
  actorId: string;
  brief: string;
  dependsOn: string[];
  acceptance: string[];
  status: string;
  result?: Result;
}
export interface Run {
  expressionError?: string;
  id: string;
  conversationId: string;
  prompt: string;
  actorId: string;
  mode: string;
  status: string;
  error?: string;
  assignments: Assignment[];
  result?: Result;
  artifacts: Artifact[];
  createdAt: number;
  updatedAt: number;
  uncertainCalls?: string[];
  runtimeStages?: Record<string, { stage: string; label: string; actorId: string; at: number }>;
  methods?: Record<string, { actorId: string; names: string[] }>;
  learningCandidates?: { skillId: string; name: string; actorId: string; status: string }[];
}
export interface ToolCall {
  id: string;
  run_id: string;
  name: string;
  args: Record<string, unknown>;
  status: string;
  result?: Record<string, unknown>;
  approval?: string;
}
export interface Settings {
  maxConnectedAgents?: number;
  llmModel: string;
  llmBaseUrl: string;
  llmProvider: string;
  authorizedWorkspaceRoot: string;
  llmApiKeyConfigured?: boolean;
  llmApiKey?: string;
  connectedAgentIds: string[];
  secretaryAgentId: string;
  allowIdleSocial: boolean;
  socialIntervalMinutes: number;
  visionEnabled: boolean;
  characterRosterText: string;
  resolutionPreset: string;
}
export interface Snapshot {
  user: Agent;
  agents: Agent[];
  conversations: Conversation[];
  messages: Record<string, Message[]>;
  posts: Post[];
  workflows: { id: string; title: string; status: string }[];
  skillCatalog: unknown[];
  approvals: { id: string; title: string; summary: string; status: string }[];
}
export interface Workspace {
  data: Snapshot;
  settings: Settings;
  uiSession: {
    view?: string;
    activeConversationId?: string;
    activePostId?: string;
  };
}
export interface RunEvent {
  seq: number;
  runId: string;
  type: string;
  payload: Record<string, unknown>;
  at: number;
}
