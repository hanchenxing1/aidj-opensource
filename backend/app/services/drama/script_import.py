"""整部剧本导入：文件文本提取 + LLM 解析定稿（自动判断集数/时长档/风格/分集）。"""

from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Any

from app.services.drama.llm import drama_chat_json
from app.services.drama.script_summary_prompt import _style_options_block, _resolve_image_style_id
from app.services.drama.image_styles import get_image_style_label, resolve_image_style_prompt
from app.services.drama.episode_duration import EPISODE_DURATION_PRESETS_SEC

# 导入剧本最少文本量（字符）
MIN_IMPORT_TEXT_CHARS = 500
# 导入剧本最多文本量（字符）：source 列已升 LONGTEXT，留足整部空间
MAX_IMPORT_TEXT_CHARS = 200000
# 送入 LLM 的全文上限（超出截断，分集标记与逐集摘要保证集数判断不漂移）
PARSE_INPUT_CHARS = 40000
# 与创建项目上限对齐
MAX_IMPORT_EPISODES = 120

# 分集标记行（第N集 / EP N / Episode N）
_EP_LINE_RE = re.compile(
    r"^\s*(?:\d+[\.、]?\s*)?(?:第\s*[0-9一二三四五六七八九十百千零]+\s*[集话回幕]"
    r"|\bEP(?:ISODE)?\s*[0-9]+\b)(?:\s+.{0,30})?[:：]?\s*$",
    re.IGNORECASE,
)


def detect_episode_markers(text: str, limit: int = 40) -> list[str]:
    """扫描全文，列出形似分集标题的原始行（供 LLM 定集数时参考）。"""
    out: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if 2 <= len(s) <= 60 and _EP_LINE_RE.match(s):
            out.append(s)
            if len(out) >= limit:
                break
    return out


def _pre_decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError("无法识别文件编码，请另存为 UTF-8 文本后上传")


def _extract_docx(raw: bytes) -> str:
    # 优先 python-docx；不可用时标准库解 zip + XML
    try:
        import docx  # type: ignore

        d = docx.Document(io.BytesIO(raw))
        parts = [p.text for p in d.paragraphs]
        text = "\n".join(x for x in parts if x.strip())
        if text.strip():
            return text
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        pass
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("DOCX 解析失败，请检查文件是否完整") from exc
    paras = re.findall(r"<w:p[ >].*?</w:p>", xml, re.S)
    lines: list[str] = []
    for p in paras:
        ts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, re.S)
        line = "".join(ts)
        line = (
            line.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&apos;", "'")
        )
        lines.append(line.strip())
    return "\n".join(x for x in lines if x)


def _extract_pdf(raw: bytes) -> str:
    try:
        import fitz  # type: ignore  # PyMuPDF

        doc = fitz.open(stream=raw, filetype="pdf")
        text = "\n".join(page.get_text("text") for page in doc)
        if text.strip():
            return text
        raise ValueError("PDF 未提取到文本（可能是扫描件）")
    except ImportError:
        raise ValueError("服务器暂不支持 PDF，请转为 docx/txt 后上传")
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError("PDF 解析失败") from exc


def _extract_json_text(raw_text: str) -> str | None:
    """.json 剧本：尽量拼出叙述文本。"""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, str):
        return data
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str) and node.strip():
            out.append(node.strip())
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return "\n".join(out) or None


def extract_script_text(filename: str | None, raw: bytes) -> str:
    """按扩展名把上传文件转成剧本文本。"""
    name = (filename or "").lower()
    ext = re.search(r"\.([a-z0-9]+)$", name)
    ext = "." + ext.group(1) if ext else ""
    if ext == ".docx":
        return _extract_docx(raw).strip()
    if ext == ".doc":
        raise ValueError("不支持旧版 .doc，请另存为 .docx 或 .txt 后上传")
    if ext == ".pdf":
        return _extract_pdf(raw).strip()
    text = _pre_decode(raw).strip()
    if ext == ".json":
        joined = _extract_json_text(text)
        if joined:
            return joined
    return text


SCRIPT_IMPORT_PARSE_SYSTEM = """你是专业的短剧/网剧剧本编辑，负责把用户导入的整部剧本「解析定稿」：不改写正文，只做结构化梳理，为后续逐集改写标准拍摄剧本做准备。

输出要求：
1. summary：结构化剧本摘要，忠实于原剧本，不改核心设定、主线与结局；characters 覆盖全部具名出场角色（主角、重要配角、反派；群演可合并为 1 个群体角色）；每人 visualImage 100–200 字（性别年龄、脸型五官、发型、体型、服饰材质与配色、气质神态、标志性道具），growthArc 用「阶段A -> 阶段B -> 阶段C」格式；synopsis 200–400 字完整叙述主线
2. seriesTitle：可作项目名的短剧名，4–16 个汉字，有记忆点；禁止复述整句梗概、禁止「未命名」「短剧」等占位
3. storyType、coreHook 用「+」连接多个标签；targetAudience 简洁（如：男频/大众）
4. episodeCount：全剧总集数。若剧本自带明确分集标记（第N集/EP N 等），必须与最大集号一致；无标记时按剧情体量估算（短篇 12–24 集，中篇 30–60 集），上限 120
5. episodeDurationSec：本剧每集成片时长最接近 60 / 120 / 300 / 600 秒中的哪一档（依据单集对白量与剧情密度），四选一输出数字
6. episodeTitles：逐集输出 episodeNumber（1..episodeCount 连续）、title（4–12 字集名，概括本集核心事件/钩子）、summary（100–300 字本集剧情摘要）、firstLine（本集正文在原文中开头处的那一行，原样复制不超过 30 字，供程序定位切分；第 1 集填 ""）
7. 若「制作参数」指定了目标集数/每集时长/画面风格，必须完全遵守；未指定画面风格且要求自动挑选时，从给定风格列表选最贴切的一个
8. 语言统一简体中文

必须输出严格 JSON 对象（不要 markdown、不要代码围栏）：
{"summary":{"episodeCount":数字,"seriesTitle":"...","storyType":"...","targetAudience":"...","coreHook":"...","oneLineStory":"...","characters":[{"name":"...","title":"...","roleType":"...","visualImage":"...","coreTags":"...","identityBackground":"...","growthExperience":"...","personality":"...","relationships":"...","growthArc":"..."}],"synopsis":"...","recommendedStyleId":"..."},"episodeCount":数字,"episodeDurationSec":60,"episodeTitles":[{"episodeNumber":1,"title":"...","summary":"...","firstLine":""}]}"""


def build_script_import_parse_user_message(
    text: str,
    *,
    episode_count: int | None = None,
    episode_duration_sec: int | None = None,
    image_style_id: str | None = None,
    auto_select_style: bool = False,
) -> str:
    body = (text or "").strip()
    if len(body) > PARSE_INPUT_CHARS:
        body = body[:PARSE_INPUT_CHARS] + "\n…（剧本全文过长，已截断；分集标记与逐集摘要以全文为准）"
    sections = [f"导入的整部剧本：\n{body}"]
    markers = detect_episode_markers(body)
    if markers:
        sections.append(
            "原文检测到的分集标记行（辅助定集数；若与全文矛盾以全文为准）：\n"
            + "\n".join(f"- {m}" for m in markers)
        )
    production: list[str] = []
    if episode_count is not None:
        production.append(f"- 目标集数：{episode_count} 集（episodeCount 必须与该值完全一致）")
    if episode_duration_sec:
        production.append(
            f"- 每集时长：约 {int(round(int(episode_duration_sec) / 60))} 分钟"
            f"（episodeDurationSec 必须为 {int(episode_duration_sec)}）"
        )
    resolved_style = _resolve_image_style_id(image_style_id)
    if resolved_style:
        label = get_image_style_label(resolved_style)
        style_prompt = resolve_image_style_prompt(resolved_style)
        production.append(f"- 画面风格：{label}（{resolved_style}）" + (f"：{style_prompt}" if style_prompt else ""))
        production.append('输出 JSON 中 summary.recommendedStyleId 固定为 "' + resolved_style + '"')
    elif auto_select_style:
        production.append("未指定画面风格。请根据剧本的世界观、时代、题材与情绪基调，从下列风格中自动挑选最贴切的一个：")
        production.append(_style_options_block())
        production.append("输出 JSON 中 summary.recommendedStyleId 填选中的风格ID")
    if production:
        sections.append("\n".join(["制作参数（用户指定则必须遵守）：", *production]))
    return "\n\n".join(sections)


def _norm_episode_title_list(raw: Any, total: int) -> list[dict[str, Any]]:
    """规整 LLM 的 episodeTitles：连续 1..total，缺号补占位。"""
    have: dict[int, dict[str, Any]] = {}
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("episodeNumber") or 0)
            except (TypeError, ValueError):
                continue
            if 1 <= n <= total:
                have[n] = {
                    "title": str(item.get("title") or "").strip(),
                    "summary": str(item.get("summary") or item.get("synopsis") or "").strip(),
                    "firstLine": str(item.get("firstLine") or "").strip(),
                }
    out: list[dict[str, Any]] = []
    for n in range(1, total + 1):
        item = have.get(n) or {"title": "", "summary": "", "firstLine": ""}
        out.append(
            {
                "episodeNumber": n,
                "title": item["title"] or f"第 {n} 集",
                "summary": item["summary"],
                "firstLine": item["firstLine"],
            }
        )
    return out


def split_script_chunks(text: str, titles: list[dict[str, Any]], total: int) -> list[str]:
    """按 LLM 给的集标题 firstLine 定位切分全文；定位失败的按相邻已知点线性插值。返回 total 段。"""
    n = max(int(total), 1)
    length = len(text or "")
    starts = [0] * (n + 1)
    starts[0] = 0
    starts[n] = length
    for t in titles or []:
        if not isinstance(t, dict):
            continue
        try:
            idx = int(t.get("episodeNumber") or 0) - 1
        except (TypeError, ValueError):
            continue
        if idx <= 0 or idx >= n:
            continue
        fl = str(t.get("firstLine") or "").strip()
        if len(fl) < 4:
            continue
        lo = starts[idx - 1] if idx > 1 else 0
        pos = text.find(fl, lo) if lo else text.find(fl)
        if pos < 0:
            pos = text.find(fl)
        if 0 < pos < length:
            starts[idx] = pos
    for i in range(1, n):
        if starts[i] in (0, length):
            lo, hi = i - 1, i + 1
            while lo > 0 and starts[lo] in (0, length):
                lo -= 1
            while hi < n and starts[hi] in (0, length):
                hi += 1
            a = starts[lo]
            b = starts[hi] if hi <= n else length
            span = hi - lo
            starts[i] = int(a + (b - a) * (i - lo) / span) if span > 0 else a
    return [text[starts[i] : starts[i + 1]].strip() for i in range(n)]


def _snap_duration(raw: Any) -> int:
    try:
        sec = int(raw or 0)
    except (TypeError, ValueError):
        sec = 0
    if sec <= 0:
        return int(EPISODE_DURATION_PRESETS_SEC[0])
    return int(min(EPISODE_DURATION_PRESETS_SEC, key=lambda p: abs(p - sec)))


async def run_script_import_parse(
    text: str,
    *,
    episode_count: int | None = None,
    episode_duration_sec: int | None = None,
    image_style_id: str | None = None,
    auto_select_style: bool = False,
) -> dict[str, Any]:
    """LLM 解析定稿：summary + 自动集数/时长档/风格 + 逐集标题与摘要。"""
    body = (text or "").strip()
    if len(body) < MIN_IMPORT_TEXT_CHARS:
        raise ValueError(f"导入的剧本至少需要 {MIN_IMPORT_TEXT_CHARS} 字")
    user_message = build_script_import_parse_user_message(
        body,
        episode_count=episode_count,
        episode_duration_sec=episode_duration_sec,
        image_style_id=image_style_id,
        auto_select_style=bool(auto_select_style and not image_style_id),
    )
    data = await drama_chat_json(
        SCRIPT_IMPORT_PARSE_SYSTEM,
        user_message,
        temperature=0.4,
        max_tokens=32000,
    )
    if not isinstance(data, dict):
        raise ValueError("剧本解析返回格式无效")
    summary = data.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    total = episode_count or int(summary.get("episodeCount") or data.get("episodeCount") or 0)
    if total < 1:
        raise ValueError("无法判断总集数，请在导入时指定集数后重试")
    total = min(int(total), MAX_IMPORT_EPISODES)
    summary["episodeCount"] = total
    duration = _snap_duration(episode_duration_sec or data.get("episodeDurationSec") or summary.get("episodeDurationSec"))
    style = (image_style_id or "").strip()
    if not style:
        style = str(
            data.get("recommendedStyleId")
            or summary.get("recommendedStyleId")
            or ""
        ).strip()
    titles = _norm_episode_title_list(data.get("episodeTitles"), total)
    return {
        "summary": summary,
        "episodeCount": total,
        "episodeDurationSec": duration,
        "imageStyleId": style,
        "episodeTitles": titles,
    }
