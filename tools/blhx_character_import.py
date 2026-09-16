from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, unquote, urlsplit

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

WIKI_BASE = "https://wiki.biligame.com/blhx/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "resources" / "characters"

FACTION_THEMES = {
    "重樱": {"palette": ["#f1d8e8", "#fff4fb"], "accent": "#ff9fc6"},
    "北方联合": {"palette": ["#dcecff", "#f6fbff"], "accent": "#7fd7ff"},
    "皇家": {"palette": ["#f7edd7", "#fffaf0"], "accent": "#f3cb78"},
    "白鹰": {"palette": ["#dde8ff", "#f7faff"], "accent": "#9ec7ff"},
    "铁血": {"palette": ["#d8dbe5", "#f6f7fb"], "accent": "#a4adbf"},
    "东煌": {"palette": ["#ffe1d4", "#fff7f3"], "accent": "#ffb08e"},
}
ROLE_INFO_KEYS = {
    "身份", "性格", "关键词", "持有物", "发色", "瞳色", "萌点", "CV", "画师", "微博", "推特", "PIXIV"
}
ROLE_INFO_SECTION = "角色信息"
ROLE_INFO_STOP_MARKERS = {
    "强度评价", "配装推荐", "角色设定", "舰船台词", "相关解释", "相关图片", "剧情相关"
}
SUMMARY_SECTION = "角色设定"
VOICE_SECTION = "舰船台词"
VOICE_REFERENCE_KEYS = [
    "自我介绍",
    "登录台词",
    "获取台词",
    "查看详情",
    "主界面",
    "触摸台词",
    "特殊触摸",
    "任务提醒",
    "任务完成",
    "回港台词",
    "好感度-喜欢",
    "好感度-爱",
]


@dataclass
class CharacterProfile:
    name: str
    url: str
    english_name: str | None = None
    ship_number: str | None = None
    ship_type: str | None = None
    faction: str | None = None
    rarity: str | None = None
    avatar_url: str | None = None
    illustrations: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    page_title: str | None = None
    setting_summary: list[str] = field(default_factory=list)
    role_info: dict[str, str] = field(default_factory=dict)
    ship_info: dict[str, str] = field(default_factory=dict)
    voice_lines: dict[str, list[str]] = field(default_factory=dict)
    extra_sections: dict[str, list[list[str]]] = field(default_factory=dict)
    prompt_seed: str = ""


def normalize_url(name_or_url: str) -> str:
    if name_or_url.startswith("http://") or name_or_url.startswith("https://"):
        return name_or_url
    return f"{WIKI_BASE}{quote(name_or_url)}"


def clean_text(value: str) -> str:
    value = (value or "").replace("\xa0", " ").replace("\u3000", " " ).replace("\r", " ")
    return re.sub(r"\s+", " ", value).strip()


def clean_multiline(value: str) -> str:
    value = (value or "").replace("\xa0", " " ).replace("\u3000", " " ).replace("\r", "")
    lines = [clean_text(line) for line in value.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def strip_urls(value: str) -> str:
    return clean_text(re.sub(r"https?://\S+", "", value or ""))


def unique_list(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = clean_text(value)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def canonical_name_from_url(url: str) -> str | None:
    slug = urlsplit(url).path.rsplit("/", 1)[-1]
    name = clean_text(unquote(slug))
    return name or None


def fetch_page(url: str) -> tuple[BeautifulSoup, str]:
    response = requests.get(url, headers=HEADERS, timeout=20)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return BeautifulSoup(response.text, "html.parser"), response.url


def cell_text(cell: Tag) -> str:
    clone = BeautifulSoup(str(cell), "html.parser")
    for br in clone.find_all("br"):
        br.replace_with("\n")
    return clean_multiline(clone.get_text("\n", strip=True))


def previous_heading(node: Tag) -> str:
    for sibling in node.previous_elements:
        if isinstance(sibling, Tag) and sibling.name in {"h1", "h2", "h3", "h4", "caption"}:
            text = clean_text(sibling.get_text(" ", strip=True))
            if text:
                return text
    return "未分类"


def parse_table_rows(table: Tag) -> list[list[str]]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        row: list[str] = []
        for cell in tr.find_all(["th", "td"]):
            value = cell_text(cell)
            if value:
                row.append(value)
        if row:
            rows.append(row)
    return rows


def flatten_pairs(rows: Iterable[list[str]]) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for row in rows:
        if len(row) == 2:
            pairs[clean_text(row[0])] = strip_urls(row[1])
        elif len(row) == 4:
            pairs[clean_text(row[0])] = strip_urls(row[1])
            pairs[clean_text(row[2])] = strip_urls(row[3])
    return pairs


def extract_title_block(text: str) -> str | None:
    match = re.search(r"IJN\s+([A-Za-z0-9 .'-]+)", text)
    return clean_text(match.group(1)) if match else None


def collect_images(soup: BeautifulSoup) -> tuple[str | None, list[str]]:
    avatar_url = None
    illustrations: list[str] = []
    for image in soup.find_all("img"):
        source = image.get("data-src") or image.get("src") or ""
        alt = clean_text(image.get("alt") or "")
        if not source:
            continue
        if source.startswith("//"):
            source = f"https:{source}"
        elif source.startswith("/"):
            source = f"https://wiki.biligame.com{source}"
        if "头像" in alt and not avatar_url:
            avatar_url = source
        if any(keyword in alt for keyword in ["立绘", "头像", "换装", "誓约"]) and source not in illustrations:
            illustrations.append(source)
    return avatar_url, illustrations[:20]


def split_voice_value(value: str) -> list[str]:
    chunks = [strip_urls(item) for item in clean_multiline(value).split("\n")]
    return [item for item in chunks if item]


SUMMARY_NOISE_MARKERS = [
    "角色剧情卡",
    "JUUs",
    "一格漫画",
    "皮肤剧情",
    "开展/折叠",
]


def sanitize_setting_summary(lines: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in lines or []:
        line = clean_multiline(strip_urls(raw))
        if not line:
            continue
        if "翻译：" in line:
            translated = clean_text(line.split("翻译：", 1)[1])
            if translated:
                match = re.search(r"[一-鿿]", translated)
                line = translated[match.start():] if match else translated
        leading_chinese = re.search(r"[\u4e00-\u9fff]", line)
        if leading_chinese and leading_chinese.start() > 0:
            prefix = line[: leading_chinese.start()]
            if re.fullmatch(r"[A-Za-z0-9_@.\-\s]+", prefix):
                line = line[leading_chinese.start():]
        if any(marker in line for marker in SUMMARY_NOISE_MARKERS):
            continue
        cleaned.append(line)
    return unique_list(cleaned)[:3]


def build_voice_lines(rows: Iterable[list[str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if len(row) < 2:
            continue
        key = clean_text(row[0])
        values = split_voice_value("\n".join(row[1:]))
        for value in values:
            if key and value and value not in result[key]:
                result[key].append(value)
    return dict(result)


def extract_role_info_from_rows(rows: Iterable[list[str]]) -> dict[str, str]:
    role_info: dict[str, str] = {}
    in_role_section = False
    for raw_row in rows:
        row = [clean_text(cell) for cell in raw_row if clean_text(cell)]
        if not row:
            continue
        if len(row) == 1 and row[0] == ROLE_INFO_SECTION:
            in_role_section = True
            continue
        if len(row) == 1 and row[0] in ROLE_INFO_STOP_MARKERS:
            if in_role_section:
                break
            continue
        if in_role_section and len(row) >= 2:
            value = strip_urls(" ".join(row[1:]))
            if value:
                role_info[row[0]] = value
        elif len(row) == 2 and row[0] in ROLE_INFO_KEYS:
            role_info[row[0]] = strip_urls(row[1])
    return role_info


def extract_text_fragments(node: Tag) -> list[str]:
    if node.name in {"style", "script", "table", "img"}:
        return []
    text = clean_text(node.get_text(" ", strip=True))
    if not text or text in {"JUUs", "一格漫画"} or text.startswith("Image:"):
        return []
    return [text]


def extract_section_lines(soup: BeautifulSoup, section_title: str) -> list[str]:
    heading = None
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        if section_title in clean_text(tag.get_text(" ", strip=True)):
            heading = tag
            break
    if heading is None:
        return []

    current_level = int(heading.name[1])
    lines: list[str] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, NavigableString):
            continue
        if isinstance(sibling, Tag) and sibling.name and re.fullmatch(r"h[1-6]", sibling.name):
            if int(sibling.name[1]) <= current_level:
                break
        if not isinstance(sibling, Tag):
            continue
        lines.extend(extract_text_fragments(sibling))

    lines = unique_list(lines)
    sanitized = sanitize_setting_summary(lines)
    if sanitized:
        return sanitized
    for index, line in enumerate(lines):
        if line.startswith("???"):
            translated = [item for item in lines[index + 1 :] if not item.startswith("???")]
            sanitized = sanitize_setting_summary(translated)
            if sanitized:
                return sanitized
    return lines[:4]


def merge_role_info(role_info: dict[str, str], extra_sections: dict[str, list[list[str]]]) -> dict[str, str]:
    merged = {clean_text(key): strip_urls(value) for key, value in (role_info or {}).items() if clean_text(key) and strip_urls(value)}
    for groups in (extra_sections or {}).values():
        for rows in groups:
            merged.update(extract_role_info_from_rows(rows))
    return merged


def normalize_profile(profile: CharacterProfile, rebuild_prompt: bool = True) -> CharacterProfile:
    canonical = canonical_name_from_url(profile.url) or clean_text(profile.name)
    aliases = list(profile.aliases or [])
    if profile.page_title and clean_text(profile.page_title) not in {canonical, profile.name}:
        aliases.append(profile.page_title)
    if profile.name and clean_text(profile.name) != canonical:
        aliases.append(profile.name)

    profile.name = canonical
    profile.aliases = unique_list(aliases)
    profile.page_title = clean_text(profile.page_title or "") or None
    profile.role_info = merge_role_info(profile.role_info, profile.extra_sections)
    profile.setting_summary = sanitize_setting_summary(profile.setting_summary)
    profile.voice_lines = {
        clean_text(key): unique_list(strip_urls(value) for value in values)
        for key, values in (profile.voice_lines or {}).items()
        if clean_text(key)
    }
    profile.ship_info = {
        clean_text(key): strip_urls(value)
        for key, value in (profile.ship_info or {}).items()
        if clean_text(key) and strip_urls(value)
    }
    profile.ship_number = clean_text(profile.ship_number or "") or profile.ship_info.get("编号")
    profile.ship_type = clean_text(profile.ship_type or "") or profile.ship_info.get("类型")
    profile.faction = clean_text(profile.faction or "") or profile.ship_info.get("阵营")
    profile.rarity = clean_text(profile.rarity or "") or profile.ship_info.get("稀有度")
    profile.avatar_url = clean_text(profile.avatar_url or "") or None
    profile.illustrations = unique_list(profile.illustrations)
    if rebuild_prompt:
        profile.prompt_seed = build_prompt_seed(profile)
    return profile


def fallback_setting_summary(profile: CharacterProfile) -> list[str]:
    lines: list[str] = []
    identity = strip_urls(profile.role_info.get("身份", ""))
    personality = strip_urls(profile.role_info.get("性格", ""))
    keywords = strip_urls(profile.role_info.get("关键词", ""))
    charm = strip_urls(profile.role_info.get("萌点", ""))
    faction = clean_text(profile.faction or "")
    ship_type = clean_text(profile.ship_type or "")

    if identity:
        lines.append(identity)
    if personality:
        lines.append(f"性格气质：{personality}")
    if keywords:
        lines.append(f"互动关键词：{keywords}")
    if charm:
        lines.append(f"外显特点：{charm}")
    background_parts = [part for part in [faction, ship_type] if part]
    if background_parts:
        lines.append(f"基础背景：{' / '.join(background_parts)}")
    return unique_list(lines)[:4]


def short_summary(profile: CharacterProfile) -> str:
    summary_lines = profile.setting_summary or fallback_setting_summary(profile)
    if summary_lines:
        return " ".join(summary_lines[:2])
    return f"{profile.faction or '\u672a\u77e5\u9635\u8425'}\u6240\u5c5e\u7684{profile.ship_type or '\u89d2\u8272'}"


def get_role_value(profile: CharacterProfile, key: str, fallback: str = "未整理") -> str:
    value = strip_urls(profile.role_info.get(key, ""))
    return value or fallback


def collect_voice_references(
    profile: CharacterProfile,
    limit: int = 24,
    *,
    char_budget: int = 3600,
) -> list[tuple[str, str]]:
    collected: list[tuple[str, str]] = []
    seen: set[str] = set()
    consumed = 0
    for preferred_key in VOICE_REFERENCE_KEYS:
        for key, values in profile.voice_lines.items():
            if preferred_key not in key or not values:
                continue
            sample = strip_urls(values[0])
            if not sample or sample in seen:
                continue
            if consumed + len(preferred_key) + len(sample) > char_budget:
                return collected
            seen.add(sample)
            collected.append((preferred_key, sample))
            consumed += len(preferred_key) + len(sample)
            break
        if len(collected) >= limit:
            break
    for key, values in profile.voice_lines.items():
        if len(collected) >= limit:
            break
        if not values:
            continue
        sample = strip_urls(values[0])
        if not sample or sample in seen:
            continue
        if consumed + len(key) + len(sample) > char_budget:
            break
        seen.add(sample)
        collected.append((clean_text(key), sample))
        consumed += len(key) + len(sample)
    return collected


def build_prompt_seed(profile: CharacterProfile) -> str:
    identity = get_role_value(profile, "身份", profile.ship_type or "未整理")
    personality = get_role_value(profile, "性格")
    keywords = get_role_value(profile, "关键词")
    accessories = get_role_value(profile, "持有物")
    hair = get_role_value(profile, "发色")
    eyes = get_role_value(profile, "瞳色")
    charm = get_role_value(profile, "萌点")
    summary_lines = profile.setting_summary or fallback_setting_summary(profile) or [short_summary(profile)]
    voice_references = collect_voice_references(profile)

    lines = [
        f"你将扮演《碧蓝航线》角色 {profile.name}。",
        "请始终保持角色口吻、情绪节奏、世界观用词与待人方式，不要跳出角色设定。",
        "",
        "角色定位：",
        f"- 角色名：{profile.name}",
        f"- 阵营：{profile.faction or '\u672a\u6574\u7406'}",
        f"- 舰种：{profile.ship_type or '\u672a\u6574\u7406'}",
        f"- 身份：{identity}",
        f"- 性格：{personality}",
        f"- 关键词：{keywords}",
        f"- 持有物：{accessories}",
        f"- 外显特征：发色{hair}；瞳色{eyes}",
        f"- 角色特点：{charm}",
        "",
        "角色设定概述：",
        *[f"- {line}" for line in summary_lines[:4]],
        "",
        "说话与互动风格参考：",
    ]

    if voice_references:
        lines.extend(f"- {label}：{sample}" for label, sample in voice_references)
    else:
        lines.append("- 角色台词样本：后续补充")

    lines.extend([
        "",
        "扮演要求：",
        "- 回答时优先保留该角色原有称呼、语气和节奏，不用现代网文式万能客服口吻。",
        "- 允许温柔、迟缓、冷淡、傲娇、威严等角色差异明确表现出来，但不要无端失控或过度夸张。",
        "- 遇到设定中没有明确说明的事情，不要硬编背景，可以用角色口吻表达保留、犹豫或推测。",
        "- 如果用户发起任务协作，在不丢失角色性的前提下给出清晰行动建议、分工意见或阶段性判断。",
        "- 回复长度按场景自适应：闲聊时自然、含蓄；任务时更清楚、有条理，但仍保持角色感。",
        "- 不要输出语音文件链接、网页链接或元数据说明，不要提自己是提示词生成结果。",
    ])
    return "\n".join(lines)


def extract_profile(url: str) -> CharacterProfile:
    soup, final_url = fetch_page(url)
    page_title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else "碧蓝航线角色"
    english_name = extract_title_block(page_title)

    tables_by_heading: dict[str, list[list[list[str]]]] = defaultdict(list)
    ship_info: dict[str, str] = {}
    role_info: dict[str, str] = {}
    voice_lines: dict[str, list[str]] = {}
    extra_sections: dict[str, list[list[str]]] = {}

    for table in soup.find_all("table"):
        rows = parse_table_rows(table)
        if not rows:
            continue
        heading = previous_heading(table)
        tables_by_heading[heading].append(rows)
        role_info.update(extract_role_info_from_rows(rows))

    for heading, grouped_rows in tables_by_heading.items():
        normalized_heading = clean_text(heading)
        for rows in grouped_rows:
            pairs = flatten_pairs(rows)
            if not ship_info and any(key in pairs for key in ["编号", "类型", "阵营", "稀有度"]):
                ship_info = pairs
                continue
            if VOICE_SECTION in normalized_heading:
                voice_lines.update(build_voice_lines(rows))
                continue
            if ROLE_INFO_SECTION in normalized_heading and pairs:
                role_info.update(pairs)
                continue
            extra_sections.setdefault(normalized_heading, []).append(rows)

    avatar_url, illustrations = collect_images(soup)
    canonical_name = canonical_name_from_url(final_url) or ship_info.get("名称") or clean_text(page_title.split("-")[0])
    page_name = clean_text(page_title.split("-")[0])
    aliases = unique_list([page_name]) if page_name and page_name != canonical_name else []

    profile = CharacterProfile(
        name=canonical_name,
        url=final_url,
        english_name=english_name,
        ship_number=ship_info.get("编号"),
        ship_type=ship_info.get("类型"),
        faction=ship_info.get("阵营"),
        rarity=ship_info.get("稀有度"),
        avatar_url=avatar_url,
        illustrations=illustrations,
        aliases=aliases,
        page_title=page_name or None,
        setting_summary=extract_section_lines(soup, SUMMARY_SECTION),
        role_info=role_info,
        ship_info=ship_info,
        voice_lines=voice_lines,
        extra_sections=extra_sections,
    )
    return normalize_profile(profile, rebuild_prompt=True)


def save_profile(profile: CharacterProfile) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    alias_stems = [
        alias
        for alias in (profile.aliases or [])
        if clean_text(alias) and clean_text(alias) != clean_text(profile.name) and len(clean_text(alias)) >= 2
    ]
    stems = unique_list([profile.name, *alias_stems])
    primary_json = None
    primary_prompt = None
    payload = json.dumps(asdict(profile), ensure_ascii=False, indent=2)
    for name in stems:
        stem = slugify(name)
        json_path = OUTPUT_DIR / f"{stem}.json"
        prompt_path = OUTPUT_DIR / f"{stem}.prompt.md"
        json_path.write_text(payload, encoding="utf-8")
        prompt_path.write_text(profile.prompt_seed, encoding="utf-8")
        if primary_json is None:
            primary_json, primary_prompt = json_path, prompt_path
    return primary_json, primary_prompt


def load_profile(path: Path) -> CharacterProfile:
    payload = json.loads(path.read_text(encoding="utf-8"))
    profile = CharacterProfile(**payload)
    return normalize_profile(profile, rebuild_prompt=True)


def slugify(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff_-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "character"


def normalize_lookup_key(value: str) -> str:
    return clean_text(value).lower().replace(" ", "")


def find_cached_profile(target: str) -> CharacterProfile | None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    target_key = normalize_lookup_key(target)
    exact_path = OUTPUT_DIR / f"{slugify(target)}.json"
    if exact_path.exists():
        return load_profile(exact_path)

    for path in OUTPUT_DIR.glob("*.json"):
        profile = load_profile(path)
        candidates = {
            normalize_lookup_key(profile.name),
            normalize_lookup_key(profile.english_name or ""),
            normalize_lookup_key(path.stem),
            normalize_lookup_key(canonical_name_from_url(profile.url) or ""),
            *{normalize_lookup_key(alias) for alias in profile.aliases},
        }
        if target_key in candidates:
            return profile
    return None


def profile_needs_refresh(profile: CharacterProfile) -> bool:
    canonical = canonical_name_from_url(profile.url) or profile.name
    if profile.name != canonical:
        return True
    if not profile.role_info:
        return True
    if not profile.setting_summary:
        return True
    if any("http://" in value or "https://" in value for values in profile.voice_lines.values() for value in values):
        return True
    return False


def resolve_profile(target: str, refresh: bool = False) -> CharacterProfile:
    cached = find_cached_profile(target)
    if cached and not refresh and not profile_needs_refresh(cached):
        return cached

    if cached and not refresh:
        try:
            profile = extract_profile(normalize_url(target))
            save_profile(profile)
            return profile
        except Exception:
            save_profile(cached)
            return cached

    profile = extract_profile(normalize_url(target))
    save_profile(profile)
    return profile


def infer_capabilities(profile: CharacterProfile) -> list[str]:
    joined = " ".join([
        profile.role_info.get("性格", ""),
        profile.role_info.get("关键词", ""),
        profile.role_info.get("身份", ""),
        " ".join(profile.setting_summary),
    ])
    capabilities: list[str] = []
    if any(keyword in joined for keyword in ["冷静", "理性", "知性", "思考", "命运", "稳重"]):
        capabilities.append("规划")
    if any(keyword in joined for keyword in ["观察", "敏锐", "迷糊", "检索", "梦", "判断"]):
        capabilities.append("检索")
    if any(keyword in joined for keyword in ["行动", "执行", "航母", "作战", "引导", "推进"]):
        capabilities.append("执行")
    if any(keyword in joined for keyword in ["温柔", "社交", "陪伴", "交流", "感情", "同伴"]):
        capabilities.append("社交")
    if not capabilities:
        capabilities = ["规划", "社交"]
    return capabilities[:4]


def build_handle(profile: CharacterProfile) -> str:
    return f"@{slugify(profile.name).lower()}.juus"


def pick_greeting(profile: CharacterProfile) -> str:
    for label in ["登录台词", "自我介绍", "获取台词", "查看详情", "主界面", "触摸台词"]:
        for key, values in profile.voice_lines.items():
            if label in key and values:
                return values[0]
    return f"{profile.name} 已经抵达 AzurJuus，本次设定接入完成。"


def build_runtime_persona(profile: CharacterProfile) -> dict:
    theme = FACTION_THEMES.get(profile.faction or "", {"palette": ["#dce8f5", "#f7fbff"], "accent": "#84caef"})
    personality = get_role_value(profile, "性格")
    identity = get_role_value(profile, "身份", profile.ship_type or "未整理")
    keywords = get_role_value(profile, "关键词")
    summary = short_summary(profile)
    illustration_url = next((item for item in profile.illustrations if item and item != profile.avatar_url), profile.avatar_url)
    prompt_seed = profile.prompt_seed or build_prompt_seed(profile)
    return {
        "sourceName": profile.name,
        "displayName": profile.name,
        "englishName": None,
        "characterUrl": profile.url,
        "avatarUrl": profile.avatar_url,
        "illustrationUrl": illustration_url,
        "faction": profile.faction or "未知阵营",
        "shipType": profile.ship_type or "未知类型",
        "rarity": profile.rarity or "未知稀有度",
        "persona": identity,
        "tone": f"{personality}；关键词：{keywords}",
        "summary": summary,
        "keywords": keywords,
        "capabilities": infer_capabilities(profile),
        "tools": ["persona.reply", "persona.memory", "persona.juus"],
        "handle": build_handle(profile),
        "palette": theme["palette"],
        "accent": theme["accent"],
        "greeting": pick_greeting(profile),
        "promptSeed": prompt_seed,
        "voiceSamples": {key: values[:2] for key, values in profile.voice_lines.items() if values},
        "aliases": profile.aliases,
    }


def resolve_personas(names: list[str], refresh: bool = False) -> tuple[list[dict], list[str]]:
    personas: list[dict] = []
    missing: list[str] = []
    for name in names:
        try:
            profile = resolve_profile(name, refresh=refresh)
        except Exception:
            missing.append(name)
            continue
        personas.append(build_runtime_persona(profile))
    return personas, missing


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract Azur Lane wiki character info for AzurJuus persona building.")
    parser.add_argument("target", help="Character name like Shinano, or a full wiki URL.")
    parser.add_argument("--refresh", action="store_true", help="Refresh from wiki even if local cache exists.")
    args = parser.parse_args()

    profile = resolve_profile(args.target, refresh=args.refresh)
    json_path, prompt_path = save_profile(profile)
    print(f"CHARACTER_URL={profile.url}")
    print(f"CHARACTER_NAME={profile.name}")
    print(f"OUTPUT_JSON={json_path}")
    print(f"OUTPUT_PROMPT={prompt_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

