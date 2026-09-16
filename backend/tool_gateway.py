from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import APPDATA_ROOT


READ_ONLY_TOOLS = {"list_dir", "read_text_file", "search_text", "read_word_document", "read_excel_document"}
WRITE_TOOLS = {"write_text_file", "create_folder", "move_file", "copy_file", "create_snapshot", "restore_snapshot", "write_word_document", "write_excel_document"}
HIGH_RISK_TOOLS = {"move_file", "write_text_file", "restore_snapshot", "write_word_document", "write_excel_document"}

LOCAL_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "list_dir": {
        "label": "列出目录",
        "kind": "local_workspace",
        "description": "查看授权工作区中的目录和文件列表。",
        "riskLevel": "low",
        "requiresSecretaryApproval": False,
        "requiresUserApproval": False,
        "supportsUndo": False,
        "autoCallable": True,
    },
    "read_text_file": {
        "label": "读取文本文件",
        "kind": "local_workspace",
        "description": "读取授权工作区中的文本文件内容。",
        "riskLevel": "low",
        "requiresSecretaryApproval": False,
        "requiresUserApproval": False,
        "supportsUndo": False,
        "autoCallable": True,
    },
    "search_text": {
        "label": "搜索文本",
        "kind": "local_workspace",
        "description": "在授权工作区内按关键字搜索文本文件。",
        "riskLevel": "low",
        "requiresSecretaryApproval": False,
        "requiresUserApproval": False,
        "supportsUndo": False,
        "autoCallable": True,
    },
    "write_text_file": {
        "label": "写入文本文件",
        "kind": "local_workspace",
        "description": "在授权工作区写入或覆盖文本文件，执行前会保留快照。",
        "riskLevel": "high",
        "requiresSecretaryApproval": True,
        "requiresUserApproval": True,
        "supportsUndo": True,
        "autoCallable": False,
    },
    "create_folder": {
        "label": "创建文件夹",
        "kind": "local_workspace",
        "description": "在授权工作区创建目录。",
        "riskLevel": "medium",
        "requiresSecretaryApproval": True,
        "requiresUserApproval": False,
        "supportsUndo": True,
        "autoCallable": False,
    },
    "move_file": {
        "label": "移动文件",
        "kind": "local_workspace",
        "description": "移动授权工作区内的文件，支持快照和回滚。",
        "riskLevel": "high",
        "requiresSecretaryApproval": True,
        "requiresUserApproval": True,
        "supportsUndo": True,
        "autoCallable": False,
    },
    "copy_file": {
        "label": "复制文件",
        "kind": "local_workspace",
        "description": "复制授权工作区内的文件。",
        "riskLevel": "medium",
        "requiresSecretaryApproval": True,
        "requiresUserApproval": False,
        "supportsUndo": True,
        "autoCallable": False,
    },
    "create_snapshot": {
        "label": "创建快照",
        "kind": "local_workspace",
        "description": "为指定路径创建快照，用于后续回滚。",
        "riskLevel": "medium",
        "requiresSecretaryApproval": True,
        "requiresUserApproval": False,
        "supportsUndo": True,
        "autoCallable": False,
    },
    "restore_snapshot": {
        "label": "恢复快照",
        "kind": "local_workspace",
        "description": "恢复指定路径的快照内容。",
        "riskLevel": "high",
        "requiresSecretaryApproval": True,
        "requiresUserApproval": True,
        "supportsUndo": True,
        "autoCallable": False,
    },
    "read_word_document": {
        "label": "读取Word文档",
        "kind": "local_workspace",
        "description": "读取授权工作区中的.docx文件内容。",
        "riskLevel": "low",
        "requiresSecretaryApproval": False,
        "requiresUserApproval": False,
        "supportsUndo": False,
        "autoCallable": True,
    },
    "write_word_document": {
        "label": "写入Word文档",
        "kind": "local_workspace",
        "description": "在授权工作区写入或覆盖.docx文件，执行前会保留快照。",
        "riskLevel": "high",
        "requiresSecretaryApproval": True,
        "requiresUserApproval": True,
        "supportsUndo": True,
        "autoCallable": False,
    },
    "read_excel_document": {
        "label": "读取Excel文档",
        "kind": "local_workspace",
        "description": "读取授权工作区中的.xlsx文件数据(返回JSON格式)。",
        "riskLevel": "low",
        "requiresSecretaryApproval": False,
        "requiresUserApproval": False,
        "supportsUndo": False,
        "autoCallable": True,
    },
    "write_excel_document": {
        "label": "写入Excel文档",
        "kind": "local_workspace",
        "description": "在授权工作区将JSON数据写入.xlsx文件，执行前会保留快照。",
        "riskLevel": "high",
        "requiresSecretaryApproval": True,
        "requiresUserApproval": True,
        "supportsUndo": True,
        "autoCallable": False,
    },
}

EXTERNAL_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "web_search": {
        "label": "联网搜索",
        "kind": "external_service",
        "description": "搜索互联网信息，适合检索资料与时效性信息。",
        "implemented": False,
        "configKey": "searchApiKey",
    },
    "maps_lookup": {
        "label": "地图查询",
        "kind": "external_service",
        "description": "查询地点、路线与地理信息。",
        "implemented": False,
        "configKey": "mapApiKey",
    },
    "recommend_places": {
        "label": "推荐检索",
        "kind": "external_service",
        "description": "做地点、餐饮、出行等推荐时的外部检索工具。",
        "implemented": False,
        "configKey": "recommendationApiKey",
    },
    "weather_lookup": {
        "label": "天气查询",
        "kind": "external_service",
        "description": "查询实时天气与预报。",
        "implemented": False,
        "configKey": "weatherApiKey",
    },
    "time_now": {
        "label": "当前时间",
        "kind": "external_service",
        "description": "查询指定时区或本地当前时间。",
        "implemented": False,
        "configKey": "",
    },
}


@dataclass
class ToolPlan:
    tool_name: str
    risk_level: str
    requires_secretary_approval: bool
    requires_user_approval: bool
    supports_undo: bool
    normalized_args: dict[str, Any]


class ToolGateway:
    def __init__(self, default_workspace_root: Path):
        self._default_workspace_root = Path(default_workspace_root).resolve()
        self._trash_root = APPDATA_ROOT / "tool-trash"
        self._trash_root.mkdir(parents=True, exist_ok=True)

    def plan(self, tool_name: str, args: dict[str, Any], authorized_root: str | None = None) -> ToolPlan:
        if tool_name not in READ_ONLY_TOOLS | WRITE_TOOLS:
            raise ValueError(f"Unsupported tool: {tool_name}")
        normalized_args = self._normalize_args(tool_name, args or {}, authorized_root)
        risk_level = "high" if tool_name in HIGH_RISK_TOOLS else ("medium" if tool_name in WRITE_TOOLS else "low")
        return ToolPlan(
            tool_name=tool_name,
            risk_level=risk_level,
            requires_secretary_approval=tool_name in WRITE_TOOLS,
            requires_user_approval=tool_name in HIGH_RISK_TOOLS,
            supports_undo=tool_name in {"move_file", "write_text_file", "restore_snapshot", "create_folder", "copy_file", "write_word_document", "write_excel_document"},
            normalized_args=normalized_args,
        )

    def execute(self, tool_name: str, args: dict[str, Any], authorized_root: str | None = None) -> dict[str, Any]:
        plan = self.plan(tool_name, args, authorized_root)
        handler = getattr(self, f"_tool_{tool_name}")
        result = handler(**plan.normalized_args)
        return {
            "tool": tool_name,
            "riskLevel": plan.risk_level,
            "supportsUndo": plan.supports_undo,
            "result": result,
        }

    def local_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                **LOCAL_TOOL_SPECS[name],
            }
            for name in sorted(LOCAL_TOOL_SPECS.keys())
        ]

    def external_catalog(self, settings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        settings = settings or {}
        catalog: list[dict[str, Any]] = []
        for name in sorted(EXTERNAL_TOOL_SPECS.keys()):
            spec = dict(EXTERNAL_TOOL_SPECS[name])
            config_key = str(spec.get("configKey") or "")
            configured = bool(settings.get(config_key)) if config_key else True
            catalog.append(
                {
                    "name": name,
                    **spec,
                    "configured": configured,
                    "available": bool(spec.get("implemented")) and configured,
                }
            )
        return catalog

    def _normalize_args(self, tool_name: str, args: dict[str, Any], authorized_root: str | None) -> dict[str, Any]:
        root = Path(authorized_root).resolve() if authorized_root else self._default_workspace_root
        clean_args = {k: v for k, v in args.items() if k in {"path", "src", "dest", "pattern", "content", "data"}}
        normalized: dict[str, Any] = {"root": root}
        if tool_name in {"list_dir", "read_text_file", "search_text", "create_folder", "write_text_file", "create_snapshot", "restore_snapshot", "read_word_document", "write_word_document", "read_excel_document", "write_excel_document"}:
            if "path" in clean_args:
                normalized["path"] = self._resolve_path(root, clean_args["path"])
        if tool_name in {"move_file", "copy_file"}:
            if "src" in clean_args:
                normalized["src"] = self._resolve_path(root, clean_args["src"])
            if "dest" in clean_args:
                normalized["dest"] = self._resolve_path(root, clean_args["dest"])
        if tool_name == "search_text":
            normalized["pattern"] = str(clean_args.get("pattern") or "")
        if tool_name in {"write_text_file", "write_word_document"}:
            normalized["content"] = str(clean_args.get("content") or "")
        if tool_name == "write_excel_document":
            normalized["data"] = clean_args.get("data")
        return normalized

    def _resolve_path(self, root: Path, raw_path: str | Path) -> Path:
        import os
        candidate = Path(raw_path)
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        resolved_str = os.path.normpath(str(resolved)).lower()
        root_str = os.path.normpath(str(root)).lower()
        if resolved_str != root_str and not resolved_str.startswith(root_str + os.sep):
            raise ValueError(f"Path escapes authorized workspace: {resolved}")
        return resolved

    def _snapshot_path(self, path: Path) -> Path:
        safe_name = path.name or "workspace"
        bucket = self._trash_root / safe_name
        bucket.mkdir(parents=True, exist_ok=True)
        return bucket / f"{path.stem}-snapshot{path.suffix}"

    def _tool_list_dir(self, root: Path, path: Path | None = None) -> dict[str, Any]:
        target = path or root
        if not target.exists():
            raise ValueError(f"路径不存在: {target}（请检查授权工作区配置是否正确）")
        return {
            "path": str(target),
            "entries": [
                {
                    "name": child.name,
                    "isDir": child.is_dir(),
                    "isFile": child.is_file(),
                }
                for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
            ],
        }

    def _tool_read_text_file(self, root: Path, path: Path) -> dict[str, Any]:
        return {
            "path": str(path),
            "content": path.read_text(encoding="utf-8"),
        }

    def _tool_search_text(self, root: Path, path: Path, pattern: str) -> dict[str, Any]:
        if not pattern:
            return {"path": str(path), "pattern": pattern, "matches": []}
        base = path if path.is_dir() else path.parent
        matches = []
        for candidate in base.rglob("*"):
            if not candidate.is_file():
                continue
            try:
                content = candidate.read_text(encoding="utf-8")
            except Exception:
                continue
            if pattern.lower() not in content.lower():
                continue
            matches.append({"path": str(candidate), "preview": content[:240]})
            if len(matches) >= 20:
                break
        return {"path": str(base), "pattern": pattern, "matches": matches}

    def _tool_create_folder(self, root: Path, path: Path) -> dict[str, Any]:
        path.mkdir(parents=True, exist_ok=True)
        return {"path": str(path), "created": True}

    def _tool_write_text_file(self, root: Path, path: Path, content: str) -> dict[str, Any]:
        snapshot = self._snapshot_path(path)
        if path.exists() and path.is_file():
            snapshot.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"path": str(path), "snapshot": str(snapshot), "bytes": len(content.encode("utf-8"))}

    def _tool_move_file(self, root: Path, src: Path, dest: Path) -> dict[str, Any]:
        snapshot = self._snapshot_path(src)
        if src.exists() and src.is_file():
            snapshot.write_bytes(src.read_bytes())
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        return {"src": str(src), "dest": str(dest), "snapshot": str(snapshot)}

    def _tool_copy_file(self, root: Path, src: Path, dest: Path) -> dict[str, Any]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return {"src": str(src), "dest": str(dest)}

    def _tool_create_snapshot(self, root: Path, path: Path) -> dict[str, Any]:
        snapshot = self._snapshot_path(path)
        if path.is_dir():
            if snapshot.exists():
                shutil.rmtree(snapshot)
            shutil.copytree(path, snapshot)
        elif path.exists():
            snapshot.write_bytes(path.read_bytes())
        else:
            snapshot.write_text(json.dumps({"missing": str(path)}, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"path": str(path), "snapshot": str(snapshot)}

    def _tool_restore_snapshot(self, root: Path, path: Path) -> dict[str, Any]:
        snapshot = self._snapshot_path(path)
        if not snapshot.exists():
            raise FileNotFoundError(snapshot)
        if snapshot.is_dir():
            if path.exists():
                shutil.rmtree(path)
            shutil.copytree(snapshot, path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot, path)
        return {"path": str(path), "snapshot": str(snapshot), "restored": True}

    def _tool_read_word_document(self, root: Path, path: Path) -> dict[str, Any]:
        try:
            import docx
            doc = docx.Document(path)
            content = "\n".join([p.text for p in doc.paragraphs])
            return {"path": str(path), "content": content}
        except ImportError:
            return {"error": "python-docx library is not installed."}

    def _tool_write_word_document(self, root: Path, path: Path, content: str) -> dict[str, Any]:
        try:
            import docx
            snapshot = self._snapshot_path(path)
            if path.exists() and path.is_file():
                snapshot.write_bytes(path.read_bytes())
            path.parent.mkdir(parents=True, exist_ok=True)
            doc = docx.Document()
            for line in content.split("\n"):
                doc.add_paragraph(line)
            doc.save(path)
            return {"path": str(path), "snapshot": str(snapshot), "written": True}
        except ImportError:
            return {"error": "python-docx library is not installed."}

    def _tool_read_excel_document(self, root: Path, path: Path) -> dict[str, Any]:
        try:
            import pandas as pd
            import math
            df = pd.read_excel(path, sheet_name=None)
            data = {}
            for sheet, sheet_df in df.items():
                records = sheet_df.to_dict(orient="records")
                for row in records:
                    for k, v in list(row.items()):
                        if isinstance(v, float) and math.isnan(v):
                            row[k] = None
                data[sheet] = records
            return {"path": str(path), "data": data}
        except ImportError:
            return {"error": "pandas or openpyxl library is not installed."}

    def _tool_write_excel_document(self, root: Path, path: Path, data: Any) -> dict[str, Any]:
        try:
            import pandas as pd
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except Exception:
                    pass
            snapshot = self._snapshot_path(path)
            if path.exists() and path.is_file():
                snapshot.write_bytes(path.read_bytes())
            path.parent.mkdir(parents=True, exist_ok=True)
            
            with pd.ExcelWriter(path) as writer:
                if isinstance(data, dict):
                    for sheet, records in data.items():
                        df = pd.DataFrame(records)
                        df.to_excel(writer, sheet_name=sheet, index=False)
                elif isinstance(data, list):
                    df = pd.DataFrame(data)
                    df.to_excel(writer, index=False)
                else:
                    df = pd.DataFrame([{"Data": str(data)}])
                    df.to_excel(writer, index=False)
            return {"path": str(path), "snapshot": str(snapshot), "written": True}
        except ImportError:
            return {"error": "pandas or openpyxl library is not installed."}
