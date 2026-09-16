from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable


@dataclass(frozen=True)
class SkillTemplate:
    id: str
    name: str
    visibility: str
    origin: str
    kind: str
    modes: tuple[str, ...]
    summary: str
    prompt_patch: str
    tool_allowlist: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    teachable: bool = True
    learnable: bool = True


@dataclass(frozen=True)
class SkillSelection:
    primary_skill_id: str
    primary_skill_name: str
    selected_skills: list[dict[str, Any]]
    prompt_patch: str
    tool_allowlist: list[str]


PUBLIC_SKILLS: tuple[SkillTemplate, ...] = (
    SkillTemplate(
        id="public.chat.companion",
        name="陪伴式对话",
        visibility="public",
        origin="builtin",
        kind="chat",
        modes=("chat",),
        summary="保持人设与陪伴感，先理解情绪与诉求，再自然回应。",
        prompt_patch="优先保持角色口吻与陪伴感，先理解用户当前情绪与真实诉求，再给出自然、贴近角色的回应。",
        tags=("陪伴", "闲聊", "理解"),
    ),
    SkillTemplate(
        id="public.task.breakdown",
        name="基础任务拆解",
        visibility="public",
        origin="builtin",
        kind="task",
        modes=("task", "swarm"),
        summary="先厘清目标、约束、风险与交付物，再推进执行。",
        prompt_patch="在任务模式下，先明确目标、约束、风险和交付物，再提出当前最值得执行的一步。除非被要求，否则不要机械列点。",
        tags=("任务", "规划", "拆解"),
    ),
    SkillTemplate(
        id="public.task.parallel",
        name="并行协作调度",
        visibility="public",
        origin="builtin",
        kind="task",
        modes=("swarm",),
        summary="适用于多人协作，强调同步、分工、依赖与阶段回报。",
        prompt_patch="如果在多人协作场景中发言，明确自己的负责边界、依赖项、与他人的同步点，并保留角色化的自然表达。",
        tags=("协作", "同步", "分工"),
    ),
    SkillTemplate(
        id="public.secretary.route",
        name="秘书统筹路由",
        visibility="public",
        origin="builtin",
        kind="secretary",
        modes=("swarm", "review"),
        summary="适用于秘书智能体，负责组队、分工、纠偏、总结与审查衔接。",
        prompt_patch="如果你当前承担秘书职责，要优先完成统筹、风险提醒、分工安排、阶段审查衔接与最终归纳，但仍需保留角色化表达，不要变成冷冰冰的系统播报。",
        tags=("秘书", "统筹", "审查", "调度"),
    ),
    SkillTemplate(
        id="public.review.stage_gate",
        name="阶段审查",
        visibility="public",
        origin="builtin",
        kind="review",
        modes=("task", "swarm", "review"),
        summary="围绕阶段目标、风险、缺口和是否进入下一步给出审查意见。",
        prompt_patch="在审查时关注阶段目标是否完成、风险是否被覆盖、交付是否足够清晰。给出通过或退回的理由，并说明下一步。",
        tags=("审查", "质检", "阶段"),
    ),
    SkillTemplate(
        id="public.social.post",
        name="动态感想",
        visibility="public",
        origin="builtin",
        kind="social",
        modes=("social_post",),
        summary="生成符合人设的动态感想、任务感悟或日常碎片。",
        prompt_patch="写动态时保持真实社交口吻，像在 JUUS 里自然发一条近况，不要像正式汇报。",
        tags=("社交", "动态", "感想"),
    ),
    SkillTemplate(
        id="public.social.comment",
        name="自然互动评论",
        visibility="public",
        origin="builtin",
        kind="social",
        modes=("social_comment",),
        summary="对他人的动态做简洁自然的角色化评论。",
        prompt_patch="评论时要像真实社交互动，简洁自然，避免重复原文或写成长总结。",
        tags=("社交", "评论"),
    ),
    SkillTemplate(
        id="public.help.consult",
        name="协作求助",
        visibility="public",
        origin="builtin",
        kind="task",
        modes=("task", "swarm"),
        summary="在不确定时先说明卡点、缺口和希望得到的帮助。",
        prompt_patch="如果存在疑问、信息不足或能力边界，请主动说明卡点、缺什么信息、希望谁提供怎样的帮助。",
        tags=("求助", "协作", "澄清"),
    ),
    SkillTemplate(
        id="public.tool.safe_workspace",
        name="安全工作区操作",
        visibility="public",
        origin="builtin",
        kind="utility",
        modes=("task", "swarm"),
        summary="涉及本地工具时，先说明计划、风险和是否需要审批。",
        prompt_patch="涉及本地工具时，先输出计划而不是直接执行，并明确风险、影响范围、回滚方式和审批需要。",
        tool_allowlist=(
            "list_dir",
            "read_text_file",
            "search_text",
            "read_word_document",
            "read_excel_document",
            "write_text_file",
            "create_folder",
            "move_file",
            "copy_file",
            "create_snapshot",
            "restore_snapshot",
            "write_word_document",
            "write_excel_document",
        ),
        tags=("工具", "安全", "工作区"),
    ),
)


class SkillRegistry:
    def __init__(self) -> None:
        self._public = {skill.id: skill for skill in PUBLIC_SKILLS}

    def _derive_private_tool_allowlist(self, actor: dict[str, Any]) -> list[str]:
        capability_tokens = {str(item).strip() for item in actor.get("capabilities") or [] if str(item).strip()}
        text = " ".join(
            [
                str(actor.get("summary") or ""),
                str(actor.get("persona") or ""),
                str(actor.get("keywords") or ""),
            ]
        )
        tools: list[str] = []

        def has_any(*tokens: str) -> bool:
            return any(token in capability_tokens or token in text for token in tokens)

        if has_any("执行", "整理", "归档", "处理", "工程", "实现", "制作"):
            tools.extend(["create_folder", "copy_file"])
        if has_any("协调", "秘书", "统筹", "规划", "审查", "质检", "风控"):
            tools.extend(["create_snapshot", "restore_snapshot"])
        if has_any("执行", "整理", "归档", "协调", "流程", "搬运"):
            tools.append("move_file")
        if has_any("文书", "记录", "写作", "总结", "规划", "报告"):
            tools.append("write_text_file")

        return list(dict.fromkeys(tools))

    def public_catalog(self, approved_public_proposals: Iterable[Any] | None = None) -> list[SkillTemplate]:
        return [self._apply_public_overlays(skill, approved_public_proposals) for skill in self._public.values()]

    def serialize_catalog(self, approved_public_proposals: Iterable[Any] | None = None) -> list[dict[str, Any]]:
        return [
            self.serialize_template(item, approved_public_proposals=approved_public_proposals)
            for item in self.public_catalog(approved_public_proposals)
        ]

    def serialize_template(
        self,
        skill: SkillTemplate,
        *,
        approved_public_proposals: Iterable[Any] | None = None,
    ) -> dict[str, Any]:
        overlays = self._proposal_overlays(skill.id, approved_public_proposals)
        return {
            "id": skill.id,
            "name": skill.name,
            "visibility": skill.visibility,
            "origin": skill.origin,
            "kind": skill.kind,
            "modes": list(skill.modes),
            "summary": skill.summary,
            "tags": list(skill.tags),
            "teachable": skill.teachable,
            "learnable": skill.learnable,
            "revisionCount": len(overlays),
            "proposalIds": [item["id"] for item in overlays],
            "proposalTitles": [item["title"] for item in overlays],
        }

    def get(self, skill_id: str) -> SkillTemplate | None:
        return self._public.get(skill_id)

    def public_for_mode(
        self,
        mode: str,
        *,
        workflow_role: str | None = None,
        source_kind: str | None = None,
        approved_public_proposals: Iterable[Any] | None = None,
    ) -> SkillTemplate:
        normalized_mode = mode if mode in {"chat", "task", "swarm", "social_post", "social_comment", "review"} else "chat"
        if normalized_mode == "chat":
            return self._apply_public_overlays(self._public["public.chat.companion"], approved_public_proposals)
        if normalized_mode == "social_post":
            return self._apply_public_overlays(self._public["public.social.post"], approved_public_proposals)
        if normalized_mode == "social_comment":
            return self._apply_public_overlays(self._public["public.social.comment"], approved_public_proposals)
        if normalized_mode == "swarm" and workflow_role == "secretary":
            return self._apply_public_overlays(self._public["public.secretary.route"], approved_public_proposals)
        if normalized_mode == "swarm":
            return self._apply_public_overlays(self._public["public.task.parallel"], approved_public_proposals)
        if source_kind == "review" or normalized_mode == "review":
            return self._apply_public_overlays(self._public["public.review.stage_gate"], approved_public_proposals)
        return self._apply_public_overlays(self._public["public.task.breakdown"], approved_public_proposals)

    def _proposal_overlays(
        self,
        skill_id: str,
        approved_public_proposals: Iterable[Any] | None = None,
    ) -> list[dict[str, Any]]:
        overlays: list[dict[str, Any]] = []
        for proposal in approved_public_proposals or []:
            proposal_skill_id = getattr(proposal, "base_skill_id", None)
            if proposal_skill_id is None and isinstance(proposal, dict):
                proposal_skill_id = proposal.get("baseSkillId") or proposal.get("base_skill_id")
            if proposal_skill_id != skill_id:
                continue
            proposal_status = getattr(proposal, "status", None)
            if proposal_status is None and isinstance(proposal, dict):
                proposal_status = proposal.get("status")
            if proposal_status and proposal_status != "approved":
                continue
            proposal_id = getattr(proposal, "id", None)
            if proposal_id is None and isinstance(proposal, dict):
                proposal_id = proposal.get("id")
            proposal_title = getattr(proposal, "title", None)
            if proposal_title is None and isinstance(proposal, dict):
                proposal_title = proposal.get("title")
            proposal_summary = getattr(proposal, "summary", None)
            if proposal_summary is None and isinstance(proposal, dict):
                proposal_summary = proposal.get("summary")
            prompt_patch = getattr(proposal, "prompt_patch", None)
            if prompt_patch is None and isinstance(proposal, dict):
                prompt_patch = proposal.get("promptPatch") or proposal.get("prompt_patch")
            overlays.append(
                {
                    "id": str(proposal_id or ""),
                    "title": str(proposal_title or ""),
                    "summary": str(proposal_summary or ""),
                    "promptPatch": str(prompt_patch or ""),
                }
            )
        return overlays

    def _apply_public_overlays(
        self,
        skill: SkillTemplate,
        approved_public_proposals: Iterable[Any] | None = None,
    ) -> SkillTemplate:
        overlays = self._proposal_overlays(skill.id, approved_public_proposals)
        if not overlays:
            return skill
        summary_parts = [skill.summary, *[item["summary"] for item in overlays if item["summary"]]]
        prompt_parts = [skill.prompt_patch, *[item["promptPatch"] for item in overlays if item["promptPatch"]]]
        tags = list(skill.tags)
        if "改进" not in tags:
            tags.append("改进")
        return replace(
            skill,
            summary=" ".join(part.strip() for part in summary_parts if part and part.strip()),
            prompt_patch="\n".join(part.strip() for part in prompt_parts if part and part.strip()),
            tags=tuple(dict.fromkeys(tags)),
        )

    def build_signature_skill_seed(self, actor: dict[str, Any]) -> dict[str, Any]:
        actor_name = str(actor.get("name") or "智能体")
        capabilities = [str(item) for item in actor.get("capabilities") or [] if str(item).strip()]
        keywords = [item.strip() for item in str(actor.get("keywords") or "").replace("，", ",").split(",") if item.strip()]
        persona = str(actor.get("persona") or actor.get("summary") or "").strip()
        faction = str(actor.get("faction") or "港区")
        tone_basis = "、".join((keywords[:2] or capabilities[:2] or [faction]))
        signature_name = f"{actor_name}的固有节奏"
        summary = f"把 {actor_name} 的人设、节奏和判断偏好融入基础流程，在 {tone_basis} 的气质下推进任务。"
        prompt_patch = (
            f"在任何任务或对话中，都要先保留 {actor_name} 作为角色的气质。"
            f"这项固有方法强调 {tone_basis} 的推进方式：不机械，不脱离人设，用 {actor_name} 自己会采用的节奏完成工作。"
            f"角色背景参考：{persona or faction}。"
        )
        return {
            "skill_id": f"private.signature.{actor.get('id')}",
            "name": signature_name,
            "visibility": "private",
            "origin": "builtin_private",
            "summary": summary,
            "prompt_patch": prompt_patch,
            "mode_json": ["chat", "task", "swarm", "social_post", "social_comment", "review"],
            "tool_allowlist_json": self._derive_private_tool_allowlist(actor),
            "tags_json": list(dict.fromkeys([*keywords[:4], *capabilities[:4], faction])),
            "teachable": True,
            "learnable": True,
            "confidence": 0.7,
            "extra_json": {
                "kind": "signature",
                "personaHint": persona,
                "keywords": keywords[:6],
                "capabilities": capabilities[:6],
            },
        }

    def build_learned_skill_seed(
        self,
        *,
        learner: dict[str, Any],
        teacher: dict[str, Any],
        source_skill: dict[str, Any],
    ) -> dict[str, Any]:
        learner_name = str(learner.get("name") or "学习者")
        teacher_name = str(teacher.get("name") or "同伴")
        teacher_skill_name = str(source_skill.get("name") or "协作方法")
        learner_keywords = str(learner.get("keywords") or "").strip()
        summary = f"受 {teacher_name} 的“{teacher_skill_name}”启发，沉淀出的私有方法。会以 {learner_name} 自己的节奏重新组织流程。"
        prompt_patch = (
            f"你可以借鉴 {teacher_name} 曾展现出的“{teacher_skill_name}”方法，但只能把它改造成 {learner_name} 自己的版本。"
            f"不要照抄对方流程，要结合自己的性格、判断与语言方式重新组织。"
            f"当前保留的角色关键词：{learner_keywords or learner.get('summary') or learner.get('faction') or '当前人设'}。"
        )
        slug = str(source_skill.get("id") or teacher_skill_name).replace(".", "-")
        return {
            "skill_id": f"private.learned.{learner.get('id')}.{slug}",
            "name": f"{learner_name}习得方法",
            "visibility": "private",
            "origin": "learned_from_collaboration",
            "base_skill_id": source_skill.get("id"),
            "summary": summary,
            "prompt_patch": prompt_patch,
            "mode_json": ["task", "swarm", "review"],
            "tool_allowlist_json": list(source_skill.get("toolAllowlist") or []),
            "tags_json": list(dict.fromkeys([*(source_skill.get("tags") or []), "学习", "协作"])),
            "teachable": False,
            "learnable": True,
            "confidence": 0.46,
            "extra_json": {
                "teacherActorId": teacher.get("id"),
                "teacherActorName": teacher_name,
                "sourceSkillName": teacher_skill_name,
            },
        }


class SkillRuntime:
    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or SkillRegistry()

    def serialize_actor_skills(self, actor_skill_rows: Iterable[Any]) -> list[dict[str, Any]]:
        serialized = []
        for row in actor_skill_rows:
            serialized.append(
                {
                    "id": row.skill_id,
                    "name": row.name,
                    "visibility": row.visibility,
                    "origin": row.origin,
                    "baseSkillId": row.base_skill_id,
                    "summary": row.summary,
                    "modes": list(row.mode_json or []),
                    "toolAllowlist": list(row.tool_allowlist_json or []),
                    "tags": list(row.tags_json or []),
                    "teachable": bool(row.teachable),
                    "learnable": bool(row.learnable),
                    "confidence": float(row.confidence or 0),
                }
            )
        return serialized

    def choose_skills(
        self,
        *,
        actor: dict[str, Any],
        actor_skill_rows: Iterable[Any],
        mode: str,
        prompt: str,
        conversation_kind: str,
        workflow_role: str | None = None,
        source_kind: str = "message",
        approved_public_proposals: Iterable[Any] | None = None,
    ) -> SkillSelection:
        public_skill = self.registry.public_for_mode(
            mode,
            workflow_role=workflow_role,
            source_kind=source_kind,
            approved_public_proposals=approved_public_proposals,
        )
        private_rows = [row for row in actor_skill_rows if getattr(row, "is_enabled", True)]
        primary_private = self._pick_private_skill(
            actor=actor,
            actor_skill_rows=private_rows,
            mode=mode,
            prompt=prompt,
            workflow_role=workflow_role,
            conversation_kind=conversation_kind,
        )

        chosen: list[dict[str, Any]] = [
            self.registry.serialize_template(public_skill, approved_public_proposals=approved_public_proposals)
        ]
        prompt_parts = [public_skill.prompt_patch]
        tool_allowlist = list(public_skill.tool_allowlist)
        primary = chosen[0]

        if primary_private is not None:
            private_entry = {
                "id": primary_private.skill_id,
                "name": primary_private.name,
                "visibility": primary_private.visibility,
                "origin": primary_private.origin,
                "baseSkillId": primary_private.base_skill_id,
                "summary": primary_private.summary,
                "modes": list(primary_private.mode_json or []),
                "toolAllowlist": list(primary_private.tool_allowlist_json or []),
                "tags": list(primary_private.tags_json or []),
                "teachable": bool(primary_private.teachable),
                "learnable": bool(primary_private.learnable),
                "confidence": float(primary_private.confidence or 0),
            }
            chosen.append(private_entry)
            prompt_parts.append(primary_private.prompt_patch)
            tool_allowlist.extend(primary_private.tool_allowlist_json or [])
            primary = private_entry

        if mode in {"task", "swarm", "review"}:
            utility_skill = self.registry.get("public.tool.safe_workspace")
            if utility_skill is not None:
                utility_skill = self.registry._apply_public_overlays(utility_skill, approved_public_proposals)
                utility_entry = self.registry.serialize_template(
                    utility_skill,
                    approved_public_proposals=approved_public_proposals,
                )
                if all(item["id"] != utility_entry["id"] for item in chosen):
                    chosen.append(utility_entry)
                    prompt_parts.append(utility_skill.prompt_patch)
                    tool_allowlist.extend(utility_skill.tool_allowlist)

        return SkillSelection(
            primary_skill_id=primary["id"],
            primary_skill_name=primary["name"],
            selected_skills=chosen,
            prompt_patch="\n".join(part for part in prompt_parts if part).strip(),
            tool_allowlist=list(dict.fromkeys(tool_allowlist)),
        )

    def _pick_private_skill(
        self,
        *,
        actor: dict[str, Any],
        actor_skill_rows: Iterable[Any],
        mode: str,
        prompt: str,
        workflow_role: str | None,
        conversation_kind: str,
    ) -> Any | None:
        prompt_text = str(prompt or "").lower()
        capability_tokens = {str(item).lower() for item in actor.get("capabilities") or []}
        best_row = None
        best_score = 0.0

        for row in actor_skill_rows:
            family = (getattr(row, 'extra_json', None) or {}).get('trialFamily')
            if family:
                matches = ('分类' in prompt_text or '整理文件' in prompt_text) if family == 'organize' else any(word in prompt_text for word in ('列出', '清单', '目录', '哪些文件'))
                if not matches or mode not in {'task', 'swarm'}:
                    continue
            modes = set(row.mode_json or [])
            score = float(row.confidence or 0.3)
            if mode in modes or "any" in modes:
                score += 0.24
            tags = {str(item).lower() for item in row.tags_json or []}
            score += 0.05 * len(tags & capability_tokens)
            score += 0.08 * sum(1 for tag in tags if tag and tag in prompt_text)
            if row.origin == "learned_from_collaboration" and mode in {"task", "swarm", "review"}:
                score += 0.09
            if row.origin == "builtin_private" and conversation_kind == "group":
                score += 0.03
            if workflow_role == "secretary" and any(tag in {"协调", "秘书", "调度", "审查"} for tag in tags):
                score += 0.12
            if score > best_score:
                best_score = score
                best_row = row

        return best_row
