"""Typed JSON boundaries shared by application routes and persisted settings."""
from typing import Annotated, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Text = Annotated[str, Field(max_length=30000)]
Id = Annotated[str, Field(max_length=255)]
Ids = Annotated[list[Id], Field(max_length=100)]


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra='allow', allow_inf_nan=False)


def checked(payload, contract):
    try:
        return contract.model_validate(payload).model_dump(exclude_unset=True)
    except ValidationError as exc:
        # Error details must not echo API keys or complete submitted content.
        raise HTTPException(422, exc.errors(include_input=False, include_context=False)) from exc


class RunInput(Input):
    conversationId: Id = ''
    prompt: Text = ''
    mode: Literal['task', 'chat'] = 'task'
    collaborative: bool = False
    headless: bool = False
    requestId: Annotated[str, Field(max_length=80)] = ''
    mentions: Ids = []
    attachments: Annotated[list[Id], Field(max_length=4)] = []


class MessageInput(RunInput):
    mode: str = 'auto'
    content: Text = ''


class ControlInput(Input):
    action: str = ''
    text: Text = ''
    acknowledgeEffects: bool = False


class ReviewInput(Input):
    reviewer: str | None = None
    decision: str = ''
    note: Text = ''


class PersonaInput(Input):
    agentId: Id = ''
    systemPrompt: Text = ''


class ComposeInput(Input):
    names: Annotated[list[Id], Field(max_length=24)] = []
    participantCount: int = Field(default=0, ge=0, le=24)


class AudienceInput(Input):
    kind: Literal['port', 'selected'] = 'port'
    actorIds: Annotated[list[Id], Field(max_length=24)] = []


class PostInput(Input):
    postId: Id = ''
    authorId: Id | None = None
    body: Text = ''
    mediaUrl: Annotated[str, Field(max_length=4000)] | None = None
    audience: AudienceInput | None = None


class ModelCheckInput(Input):
    baseUrl: Annotated[str, Field(max_length=4000)] = ''
    model: Id = ''
    apiKey: Annotated[str, Field(max_length=10000)] = ''
    checkReasoning: bool = False


class WorkspaceSettingsInput(Input):
    resolutionPreset: Id = 'balanced'
    maxConnectedAgents: int = Field(default=4, ge=1, le=100)
    connectedAgentIds: Ids = []
    characterRosterText: Text = ''
    llmProvider: Id = ''
    llmModel: Id = ''
    llmBaseUrl: Annotated[str, Field(max_length=4000)] = ''
    llmApiKey: Annotated[str, Field(max_length=10000)] = ''
    clearLlmApiKey: bool = False
    toolProfile: Id = ''
    toolBaseUrl: Annotated[str, Field(max_length=4000)] = ''
    toolApiKey: Annotated[str, Field(max_length=10000)] = ''
    searchApiKey: Annotated[str, Field(max_length=10000)] = ''
    mapApiKey: Annotated[str, Field(max_length=10000)] = ''
    recommendationApiKey: Annotated[str, Field(max_length=10000)] = ''
    weatherApiKey: Annotated[str, Field(max_length=10000)] = ''
    authorizedWorkspaceRoot: Annotated[str, Field(max_length=4000)] = ''
    secretaryAgentId: Id = ''
    allowIdleSocial: bool = True
    socialIntervalMinutes: int = Field(default=60, ge=1, le=10080)
    visionEnabled: bool = False
    # These values have historically been serialized as strings by settings.
    chatTimeoutSeconds: int = Field(default=120, ge=1, le=86400, strict=False)
    runTimeoutSeconds: int = Field(default=3600, ge=1, le=86400, strict=False)
    maxTaskMembers: int = Field(default=12, ge=1, le=24, strict=False)

    @field_validator('chatTimeoutSeconds', 'runTimeoutSeconds', 'maxTaskMembers', mode='before')
    @classmethod
    def numeric_not_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError('Expected an integer, not a boolean')
        return value


class WorkspaceInput(Input):
    settings: WorkspaceSettingsInput | None = None
    uiSession: dict | None = None
