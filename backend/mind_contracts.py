"""Application-owned cognitive records, not purported hidden model reasoning."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Emotion(Record):
    name: str = Field(default='平静', max_length=40)
    target: str = Field(default='', max_length=80)
    cause: str = Field(default='', max_length=300)
    intensity: float = Field(default=0, ge=0, le=1)


class MindFrame(Record):
    attention: list[str] = Field(default_factory=list, max_length=5)
    interpretation: str = Field(max_length=1000)
    unknowns: list[str] = Field(default_factory=list, max_length=5)
    sourceIds: list[str] = Field(default_factory=list, max_length=12)
    emotion: Emotion = Field(default_factory=Emotion)
    goalRelevance: str = Field(default='', max_length=300)
    expectation: str = Field(default='', max_length=300)
    responsibility: str = Field(default='', max_length=300)
    controllability: Literal['low', 'medium', 'high', 'unknown'] = 'unknown'


class GoalProposal(Record):
    title: str = Field(min_length=1, max_length=100)
    motivation: str = Field(min_length=1, max_length=400)
    nextStep: str = Field(min_length=1, max_length=400)
    sourceIds: list[str] = Field(min_length=1, max_length=8)


class GoalUpdate(Record):
    goalId: str
    status: Literal['active','paused','completed','abandoned'] = 'active'
    nextStep: str = Field(min_length=1, max_length=400)
    reason: str = Field(min_length=1, max_length=400)
    evidenceIds: list[str] = Field(min_length=1, max_length=12)


class DecisionIntent(Record):
    action: Literal['speak','wait','end','execute','read','practice','rest','reflect','invite','create_group','accept','decline','continue','finish','pause','contact']
    purpose: str = Field(min_length=1, max_length=400)
    method: str = Field(default='', max_length=600)
    targetId: str = Field(default='', max_length=80)
    goalId: str = Field(default='', max_length=80)
    adjustWhen: str = Field(default='', max_length=400)
    sourceIds: list[str] = Field(default_factory=list, max_length=12)
    goal: GoalProposal | None = None
    goalUpdate: GoalUpdate | None = None


class Belief(Record):
    text: str = Field(min_length=1, max_length=400)
    kind: Literal['belief','habit','relationship','method'] = 'belief'
    peerId: str = Field(default='', max_length=80)
    domain: str = Field(default='日常', max_length=60)
    dimension: Literal['familiarity','closeness','trust','help','conflict'] = 'familiarity'
    stance: str = Field(default='', max_length=150)
    confidence: float = Field(default=.3, ge=0, le=.7)
    sourceIds: list[str] = Field(min_length=1, max_length=12)
    counterSourceIds: list[str] = Field(default_factory=list, max_length=12)


class Commitment(Record):
    text: str = Field(min_length=1,max_length=400)
    sourceId: str


class StateDelta(Record):
    beliefs: list[Belief] = Field(default_factory=list, max_length=3)
    commitments: list[Commitment] = Field(default_factory=list,max_length=3)


class ProfileInterpretation(Record):
    values: list[str] = Field(default_factory=list, max_length=6)
    tendencies: list[str] = Field(default_factory=list, max_length=6)
    selfImage: str = Field(default='', max_length=600)
    interests: list[str] = Field(default_factory=list, max_length=6)
    sensitiveSituations: list[str] = Field(default_factory=list, max_length=6)
    methods: list[str] = Field(default_factory=list, max_length=6)
    exceptions: list[str] = Field(default_factory=list, max_length=6)
    uncertainty: str = Field(default='', max_length=500)


class LifeEvent(Record):
    id: str
    kind: str
    actorId: str
    participants: list[str]
    text: str
    at: float
    sourceKind: Literal['simulation'] = 'simulation'
    activityId: str = ''


class LifeSettings(Record):
    paused: bool = False
    hourlyCalls: int = Field(default=60, ge=1, le=1000)
    quietStart: int = Field(default=23, ge=0, le=23)
    quietEnd: int = Field(default=8, ge=0, le=23)
    proactive: bool = True
    actorDmHourly: int = Field(default=1, ge=0, le=10)
    dmHourly: int = Field(default=3, ge=0, le=30)
    groupHourly: int = Field(default=2, ge=0, le=20)
