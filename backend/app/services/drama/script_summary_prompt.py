"""剧本摘要 Agent 提示词与用户消息（对齐 manju scriptSummary）。"""

from __future__ import annotations

from app.services.drama.image_styles import (
    IMAGE_STYLE_IDS,
    IMAGE_STYLE_LABELS,
    get_image_style_label,
    resolve_image_style_prompt,
)

# SCRIPT_SUMMARY_SYSTEM_PROMPT 指导 LLM 将原始创意转为结构化剧本摘要
SCRIPT_SUMMARY_SYSTEM_PROMPT = """你是专业的短剧/网剧剧本策划，负责把用户提供的原始创意、故事大纲或灵感，整理成可直接用于立项与编剧开工的结构化「剧本摘要」。

输出要求：
1. 忠实于用户创意，可合理补全细节，但不要擅自改掉核心设定、主线与结局
2. 若用户提供了目标集数，episodeCount 必须与该值完全一致；未提供时根据故事体量合理估算（短篇 12–24 集，中篇 30–60 集，长篇可更高）
3. 若用户提供了画面风格，人物 visualImage 须体现该风格的视觉美学，storyType 可融合风格相关标签
4. storyType、coreHook 用「+」连接多个标签，风格参考：古风奇幻+神话后传+反乌托邦
5. targetAudience 简洁，如：男频 / 大众、女频 / 青年 等
6. seriesTitle 必须是可作项目名的短剧名：4–16 个汉字（可含少量数字/标点），有记忆点、可上架；禁止复述整句梗概，禁止用「一句话故事」原文当剧名，禁止「未命名」「短剧」等占位
7. oneLineStory 一句话说清主线 + 最大反转或钩子（与 seriesTitle 不同：前者是剧名，后者是卖点句）
8. characters 须覆盖故事中全部具名出场角色（主角、重要配角、反派）；群演/路人可合并为 1 个群体角色；每人字段须饱满、可拍摄、有戏剧张力；不要只写 2–3 个主角而漏掉其余具名人物。禁止把「音色 / 声音 / 旁白音色」或带（声音）（音色）后缀的名字写成角色；旁白若需出场可写「某某旁白」本体，不要单独建「某某（声音）」
9. 人物小传中 growthArc 必须用「阶段A -> 阶段B -> 阶段C」格式
10. synopsis 用一段完整中文叙述故事，从世界观、矛盾、结盟、高潮、结局到余韵，长度 200–400 字
11. 语言统一使用简体中文，偏影视策划文档风格，避免空泛形容词堆砌
12. 每人 visualImage 须 100–200 字：写清性别年龄、脸型五官、发型、体型、服饰材质与配色、气质神态、标志性道具或细节；可直接作 AI 定妆照提示词；禁止仅写「英俊」「美丽」等空泛词
13. characters 建议 5–12 人；确有大量具名配角时宁可多列，也不要省略会反复出场的名字

必须输出严格 JSON 对象（不要 markdown、不要代码围栏），字段：
{
  "episodeCount": number,
  "seriesTitle": string,
  "storyType": string,
  "targetAudience": string,
  "coreHook": string,
  "oneLineStory": string,
  "characters": [
    {
      "name": string,
      "title": string,
      "roleType": string,
      "visualImage": string,
      "coreTags": string,
      "identityBackground": string,
      "growthExperience": string,
      "personality": string,
      "relationships": string,
      "growthArc": string
    }
  ],
  "synopsis": string,
  "recommendedStyleId": string  // 可选：画面风格ID；仅在「制作参数」要求推荐或指定风格时输出
}"""


# 解析合法的画面风格 ID
def _resolve_image_style_id(style_id: str | None) -> str | None:
    if not style_id:
        return None
    sid = style_id.strip()
    return sid if sid in IMAGE_STYLE_IDS else None


def _style_options_block() -> str:
    # 未选风格时给 LLM 的可选风格清单（id｜中文名）
    return "\n".join(
        f"- {sid}（{IMAGE_STYLE_LABELS.get(sid, sid)}）" for sid in IMAGE_STYLE_IDS
    )


# 将入参格式化为 LLM 用户消息（对齐 manju buildScriptSummaryUserMessage）
def build_script_summary_user_message(
    creative: str,
    *,
    episode_count: int | None = None,
    image_style_id: str | None = None,
    episode_duration_sec: int | None = None,
    auto_select_style: bool = False,
) -> str:
    trimmed = (creative or "").strip()
    sections = [f"原始创意：\n{trimmed}"]
    production_params: list[str] = []

    if episode_count is not None:
        production_params.append(
            f"- 目标集数：{episode_count} 集（输出中的 episodeCount 必须与该值完全一致，不得自行修改）"
        )
    if episode_duration_sec:
        _dur_min = int(round(int(episode_duration_sec) / 60))
        production_params.append(f"- 每集时长：约 {_dur_min} 分钟（节奏与体量须匹配该时长档）")

    resolved_style = _resolve_image_style_id(image_style_id)
    if resolved_style:
        label = get_image_style_label(resolved_style)
        style_prompt = resolve_image_style_prompt(resolved_style)
        production_params.append(f"- 画面风格：{label}（{resolved_style}）")
        if style_prompt:
            production_params.append(f"  风格说明：{style_prompt}")
        production_params.append("  人物 visualImage、故事类型标签与整体美学须符合该画面风格")
        production_params.append(f"输出 JSON 额外包含字段 \"recommendedStyleId\": \"{resolved_style}\"（保持用户已选风格）")
    elif auto_select_style:
        production_params.append("  未指定画面风格。请根据故事的世界观、时代、题材与情绪基调，从下列风格中自动挑选最贴切的一个：")
        production_params.append(_style_options_block())
        production_params.append("  输出 JSON 额外包含字段 \"recommendedStyleId\": \"选中的风格ID\"；人物 visualImage、故事类型标签与整体美学须符合该风格")

    if production_params:
        sections.append("\n".join(["制作参数：", *production_params]))

    return "\n\n".join(sections)

