import os
import re
import json
import subprocess
import tempfile
from functools import lru_cache
from typing import Any, Optional

try:
    import fitz
except ModuleNotFoundError:
    fitz = None

try:
    from pypdf import PdfReader
except ModuleNotFoundError:
    PdfReader = None


JP_DIR = "/Users/leizheng/code/jp"
BASE_DIR = os.path.dirname(__file__)
CACHE_DIR = os.path.join(BASE_DIR, ".cache", "jlpt_ocr")
PAGE_CACHE_DIR = os.path.join(CACHE_DIR, "pages")
VISION_OCR_SCRIPT = os.path.join(BASE_DIR, "vision_ocr.swift")
INTERMEDIATE_STATIC_CACHE = os.path.join(BASE_DIR, "processed_intermediate_materials.json")

SOURCE_FILES = {
    "beginner-upper-doc": os.path.join(JP_DIR, "新版中日交流标准日本语_初级上册_课文_译文_单词.doc"),
    "beginner-upper-book": os.path.join(JP_DIR, "新版中日交流标准日本语 第2版 初级 上.pdf"),
    "beginner-lower-text": os.path.join(JP_DIR, "新版中日交流标准日本语_初级下册__课文_译文_单词.pdf"),
    "beginner-lower-book": os.path.join(JP_DIR, "新版中日交流标准日本语 第2版 初级 下.pdf"),
    "intermediate-upper-book": os.path.join(JP_DIR, "新版标准语中级（上）.pdf"),
    "intermediate-upper-vocab": os.path.join(JP_DIR, "新标日中级单词上.pdf"),
    "intermediate-lower-book": os.path.join(JP_DIR, "新版标准语中级（下）.pdf"),
    "intermediate-lower-vocab": os.path.join(JP_DIR, "新标日中级单词下.pdf"),
}

VOCAB_AUDIO_DIR = os.path.join(
    JP_DIR,
    "标准日本语同步测试卷听力+单词读本听力",
    "单词听力",
    "单词1",
)

BEGINNER_TITLES = {
    1: "李さんは中国人です",
    2: "これは本です",
    3: "ここはデパートです",
    4: "部屋に机といすがあります",
    5: "森さんは7時に起きます",
    6: "吉田さんは来月中国へ行きます",
    7: "李さんは毎日コーヒーを飲みます",
    8: "李さんは日本語で手紙を書きます",
    9: "四川料理は辛いです",
    10: "京都の紅葉は有名です",
    11: "小野さんは歌が好きです",
    12: "李さんは森さんより若いです",
    13: "机の上に本が3冊あります",
    14: "昨日デパートへ行って、買い物しました",
    15: "小野さんは今新聞を読んでいます",
    16: "ホテルの部屋は広くて明るいです",
    17: "わたしは新しい洋服が欲しいです",
    18: "携帯電話はとても小さくなりました",
    19: "部屋のかぎを忘れないでください",
    20: "スミスさんはピアノを弾くことができます",
    21: "私はすき焼きを食べたことがあります",
    22: "森さんは毎晩テレビを見る",
    23: "休みの日、散歩したり買い物に行ったりします",
    24: "李さんはもうすぐ来ると思います",
    25: "これは明日会議で使う資料です",
    26: "自転車に2人で乗るのは危ないです",
    27: "手紙を出すのを忘れました",
    28: "馬さんは私に地図をくれました",
    29: "電気を消せ",
    30: "もう11時だから寝よう",
    31: "このボタンを押すと、電源が入ります",
    32: "今度の日曜日に遊園地へ行くつもりです",
    33: "電車が急に止まりました",
    34: "壁にカレンダーが掛けてあります",
    35: "明日雨が降ったら、マラソン大会は中止です",
    36: "遅くなって、すみません",
    37: "優勝すれば、オリンピックに出場することができます",
    38: "戴さんは英語が話せます",
    39: "眼鏡をかけて本を読みます",
    40: "これから友達と食事に行くところです",
    41: "李さんは部長にほめられました",
    42: "テレビをつけたまま、出かけてしまいました",
    43: "陳さんは息子をアメリカに留学させます",
    44: "玄関のところにだれかいるようです",
    45: "少子化が進んで、日本の人口はだんだん減っていくでしょう",
    46: "これは柔らかくて、まるで本物の毛皮のようです",
    47: "周先生は明日日本へ行かれます",
    48: "お荷物は私がお持ちします",
}

FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

INTERMEDIATE_BOOK_LABELS = {
    "upper": "标日中级上",
    "lower": "标日中级下",
}


def _local_materials_runtime_ready() -> bool:
    return os.path.isdir(JP_DIR) and fitz is not None and PdfReader is not None


def _intermediate_static_cache_ready() -> bool:
    return os.path.isfile(INTERMEDIATE_STATIC_CACHE)


def _unavailable_payload(reason: str) -> dict[str, Any]:
    return {
        "mode": "unavailable",
        "title": "本地教材暂不可用",
        "summary": reason,
        "blocks": [
            {
                "title": "当前状态",
                "text": reason,
            }
        ],
        "resources": [],
    }


def _ensure_cache_dirs() -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(PAGE_CACHE_DIR, exist_ok=True)


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_toc_title(title: str) -> str:
    return re.sub(r"\s+", "", title or "")


def _clean_ocr_text(text: str) -> str:
    cleaned_lines = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        if "JP.YesH" in line or "JP.YesHI" in line or "JP.YesHJ" in line:
            continue
        if re.fullmatch(r"[0-9０-９]+", line):
            continue
        cleaned_lines.append(line)

    cleaned = "\n".join(cleaned_lines)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _fix_common_ocr_errors(text: str) -> str:
    replacements = {
        "会活": "会话",
        "課文": "课文",
        "第！課": "第1课",
        "第＆": "第",
        "第 会话": "会话",
        "文 にほん": "日本",
        "aban Raffmags": "Japan Railways",
        "子R": "JR",
        "TR": "JR",
        "写R": "JR",
        "磁愚浮列年": "磁浮列车",
        "平面": "中国",
        "自米": "日本",
        "私録": "私铁",
        "新類": "新干线",
        "労面": "场面",
        "言業": "言叶",
        "会※室": "会议室",
        "突ですが": "失礼ですが",
        "効強": "勉强",
        "転動": "转勤",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _strip_speaker_label(line: str) -> str:
    match = re.match(r"^(.{1,4})[：:](.+)$", line)
    if not match:
        return line
    body = match.group(2).strip()
    prefix = match.group(1).strip()
    if len(prefix) <= 3:
        return body
    return line


def _line_is_reliable(line: str, min_confidence: float) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) <= 1:
        return False
    if stripped.startswith("JP.Yes"):
        return False
    if re.fullmatch(r"[0-9０-９]+", stripped):
        return False

    kana_count = len(re.findall(r"[ぁ-んァ-ヶー]", stripped))
    cjk_count = len(re.findall(r"[一-龯々]", stripped))
    punct_count = len(re.findall(r"[。、「」『』（）！？：]", stripped))
    latin_count = len(re.findall(r"[A-Za-z]", stripped))
    weird_count = len(re.findall(r"[=£★※◆▶■□△▽]+", stripped))
    total_jp = kana_count + cjk_count

    if weird_count > 0:
        return False
    if total_jp < 4:
        return False
    if latin_count > 8 and kana_count == 0:
        return False
    if kana_count < 2 and "：" not in stripped and ":" not in stripped:
        return False
    if total_jp > 0 and kana_count / total_jp < 0.12 and "：" not in stripped and ":" not in stripped:
        return False
    return True


def _postprocess_ocr_lines(items: list[dict[str, Any]], min_confidence: float = 0.55) -> str:
    lines = []
    for item in items:
        text = str(item.get("text", "")).strip()
        confidence = float(item.get("confidence", 0.0))
        if confidence < min_confidence:
            continue
        text = _fix_common_ocr_errors(text)
        text = _strip_speaker_label(text)
        if _line_is_reliable(text, min_confidence):
            lines.append(text)

    cleaned = _clean_ocr_text("\n".join(lines))
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _lesson_heading(lesson_number: int) -> str:
    title = BEGINNER_TITLES.get(lesson_number)
    if title:
        return f"第 {lesson_number} 课 · {title}"
    return f"第 {lesson_number} 课"


def _truncate(text: str, limit: int = 2200) -> str:
    text = _normalize_whitespace(text)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _review_excerpt(label: str, text: str, limit: int = 520) -> str:
    snippet = _truncate(text, limit)
    return f"【{label}】\n{snippet}"


def _section_excerpt(label: str, text: str, limit: int = 1400) -> str:
    if not text:
        return ""
    return f"【{label}】\n{_truncate(text, limit)}"


def _run_textutil(path: str) -> str:
    result = subprocess.run(
        ["textutil", "-convert", "txt", "-stdout", path],
        capture_output=True,
        check=True,
    )
    return result.stdout.decode("utf-8", errors="ignore")


@lru_cache(maxsize=2)
def _intermediate_toc(book_key: str) -> dict[int, dict[str, Any]]:
    pdf_path = SOURCE_FILES[f"intermediate-{book_key}-book"]
    doc = fitz.open(pdf_path)
    entries = doc.get_toc()
    lesson_order: list[int] = []
    lessons: dict[int, dict[str, Any]] = {}
    current_lesson: Optional[int] = None

    for level, title, page in entries:
        normalized = _normalize_toc_title(title)
        match = re.fullmatch(r"第([0-9０-９]+)课", normalized)
        if level == 2 and match:
            lesson_number = int(match.group(1).translate(FULLWIDTH_DIGITS))
            current_lesson = lesson_number
            lesson_order.append(lesson_number)
            lessons[lesson_number] = {
                "lessonPage": page,
                "sections": [],
            }
            continue

        if level == 3 and current_lesson in lessons:
            lessons[current_lesson]["sections"].append({
                "title": normalized,
                "page": page,
            })

    for index, lesson_number in enumerate(lesson_order):
        next_page = doc.page_count
        if index + 1 < len(lesson_order):
            next_page = lessons[lesson_order[index + 1]]["lessonPage"] - 1
        lessons[lesson_number]["endPage"] = next_page

    return lessons


def _section_page_range(lesson_meta: dict[str, Any], section_title: str) -> Optional[tuple[int, int]]:
    sections = lesson_meta.get("sections", [])
    for index, section in enumerate(sections):
        if section["title"] != section_title:
            continue
        start_page = section["page"]
        next_page = lesson_meta["endPage"]
        if index + 1 < len(sections):
            next_page = sections[index + 1]["page"] - 1
        return start_page, next_page
    return None


def _ocr_page(book_key: str, page_number: int) -> str:
    _ensure_cache_dirs()
    cache_path = os.path.join(PAGE_CACHE_DIR, f"{book_key}_{page_number:03d}.txt")
    if os.path.isfile(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return f.read()

    pdf_path = SOURCE_FILES[f"intermediate-{book_key}-book"]
    doc = fitz.open(pdf_path)
    page = doc.load_page(page_number - 1)
    rect = page.rect
    clip = fitz.Rect(rect.x0 + 10, rect.y0 + 28, rect.x1 - 10, rect.y1 - 18)

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_image:
        image_path = temp_image.name

    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), clip=clip, alpha=False)
        pix.save(image_path)
        result = subprocess.run(
            ["swift", VISION_OCR_SCRIPT, "--jsonl", image_path],
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )
        items = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        cleaned = _postprocess_ocr_lines(items)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(cleaned)
        return cleaned
    finally:
        if os.path.exists(image_path):
            os.remove(image_path)


def _ocr_page_range(book_key: str, page_range: Optional[tuple[int, int]]) -> str:
    if not page_range:
        return ""
    start_page, end_page = page_range
    parts = []
    for page_number in range(start_page, end_page + 1):
        text = _ocr_page(book_key, page_number)
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _ocr_page_range_preview(book_key: str, page_range: Optional[tuple[int, int]], page_limit: int = 1) -> str:
    if not page_range:
        return ""
    start_page, end_page = page_range
    parts = []
    for page_number in range(start_page, min(end_page, start_page + page_limit - 1) + 1):
        text = _ocr_page(book_key, page_number)
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _build_intermediate_static_cache() -> dict[str, Any]:
    payload: dict[str, Any] = {"upper": {}, "lower": {}}
    for book_key in ("upper", "lower"):
        vocab_source = f"intermediate-{book_key}-vocab"
        vocab_lessons = _intermediate_vocab_lessons(vocab_source)
        for lesson_number in sorted(_intermediate_toc(book_key).keys()):
            ocr_payload = _intermediate_lesson_ocr(book_key, lesson_number)
            payload[book_key][str(lesson_number)] = {
                "conversation": _truncate(ocr_payload.get("conversation", ""), 7000),
                "reading": _truncate(ocr_payload.get("reading", ""), 7000),
                "vocab": _truncate(vocab_lessons.get(lesson_number, ""), 2500),
            }
    return payload


def build_intermediate_static_cache(force_rebuild: bool = False) -> dict[str, Any]:
    _ensure_cache_dirs()
    if force_rebuild:
        for folder in (PAGE_CACHE_DIR, CACHE_DIR):
            for name in os.listdir(folder):
                if name.endswith('.txt') or name.endswith('.json'):
                    os.remove(os.path.join(folder, name))
    if os.path.isfile(INTERMEDIATE_STATIC_CACHE) and not force_rebuild:
        with open(INTERMEDIATE_STATIC_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)

    payload = _build_intermediate_static_cache()
    with open(INTERMEDIATE_STATIC_CACHE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


@lru_cache(maxsize=1)
def _load_intermediate_static_cache() -> dict[str, Any]:
    if not os.path.isfile(INTERMEDIATE_STATIC_CACHE):
        return {"upper": {}, "lower": {}}
    with open(INTERMEDIATE_STATIC_CACHE, "r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=32)
def _intermediate_lesson_ocr(book_key: str, lesson_number: int) -> dict[str, Any]:
    _ensure_cache_dirs()
    cache_path = os.path.join(CACHE_DIR, f"lesson_{book_key}_{lesson_number:02d}.json")
    if os.path.isfile(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    lesson_meta = _intermediate_toc(book_key).get(lesson_number)
    if not lesson_meta:
        return {"conversation": "", "reading": ""}

    payload = {
        "conversation": _ocr_page_range(book_key, _section_page_range(lesson_meta, "会话")),
        "reading": _ocr_page_range(book_key, _section_page_range(lesson_meta, "课文")),
    }

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


@lru_cache(maxsize=32)
def _intermediate_lesson_preview(book_key: str, lesson_number: int) -> dict[str, str]:
    lesson_meta = _intermediate_toc(book_key).get(lesson_number)
    if not lesson_meta:
        return {"conversation": "", "reading": ""}

    return {
        "conversation": _ocr_page_range_preview(book_key, _section_page_range(lesson_meta, "会话"), page_limit=1),
        "reading": _ocr_page_range_preview(book_key, _section_page_range(lesson_meta, "课文"), page_limit=1),
    }


@lru_cache(maxsize=1)
def _beginner_upper_lessons() -> dict[int, dict[str, str]]:
    text = _run_textutil(SOURCE_FILES["beginner-upper-doc"])
    pattern = re.compile(r"^第\s*(\d+)\s*課\s*(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    lessons: dict[int, dict[str, str]] = {}

    for index, match in enumerate(matches):
        lesson_number = int(match.group(1))
        title = match.group(2).strip()
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        lessons[lesson_number] = {
            "title": title,
            "text": _normalize_whitespace(body),
        }

    return lessons


@lru_cache(maxsize=1)
def _beginner_lower_lessons() -> dict[int, dict[str, str]]:
    reader = PdfReader(SOURCE_FILES["beginner-lower-text"])
    lessons: dict[int, dict[str, str]] = {}

    for lesson_number in range(25, 49):
        page_index = (lesson_number - 25) * 2
        parts: list[str] = []
        for offset in (0, 1):
            current = page_index + offset
            if current >= len(reader.pages):
                break
            page_text = reader.pages[current].extract_text() or ""
            if page_text.strip():
                parts.append(_normalize_whitespace(page_text))

        lessons[lesson_number] = {
            "title": BEGINNER_TITLES.get(lesson_number, f"第 {lesson_number} 课"),
            "text": "\n\n".join(parts).strip(),
        }

    return lessons


@lru_cache(maxsize=2)
def _intermediate_vocab_lessons(source_key: str) -> dict[int, str]:
    path = SOURCE_FILES.get(source_key)
    if PdfReader is None or not path or not os.path.isfile(path):
        return {}

    reader = PdfReader(path)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    pattern = re.compile(r"第\s*([0-9０-９]+)\s*[课課]")
    matches = list(pattern.finditer(text))
    lessons: dict[int, str] = {}

    for index, match in enumerate(matches):
        lesson_number = int(match.group(1).translate(FULLWIDTH_DIGITS))
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        lessons[lesson_number] = _normalize_whitespace(text[start:end])

    return lessons


def get_material_source_path(source_key: str) -> Optional[str]:
    if source_key.startswith("vocab-audio-"):
        lesson_number = source_key.rsplit("-", 1)[-1]
        path = os.path.join(VOCAB_AUDIO_DIR, f"第{lesson_number}课.mp3")
        return path if os.path.isfile(path) else None

    path = SOURCE_FILES.get(source_key)
    if not path or not os.path.isfile(path):
        return None
    return path


def _get_beginner_lesson(lesson_number: int) -> dict[str, str]:
    if lesson_number <= 24:
        return _beginner_upper_lessons().get(lesson_number, {})
    return _beginner_lower_lessons().get(lesson_number, {})


def _beginner_resources(lesson_number: int) -> list[dict[str, str]]:
    source_prefix = "beginner-upper" if lesson_number <= 24 else "beginner-lower"
    items = [
        {
            "name": "本地课文 / 译文 / 单词",
            "detail": "直接打开你本地已购资料里的对应整理版。",
            "sourceKey": f"{source_prefix}-doc" if lesson_number <= 24 else f"{source_prefix}-text",
        },
        {
            "name": "本地主教材 PDF",
            "detail": "原版教材 PDF，适合对照版式、插图和练习。",
            "sourceKey": f"{source_prefix}-book",
        },
    ]

    audio_key = f"vocab-audio-{lesson_number}"
    if get_material_source_path(audio_key):
        items.append(
            {
                "name": f"第 {lesson_number} 课单词音频",
                "detail": "你本地目录里的单词听力文件，可直接播放。",
                "sourceKey": audio_key,
            }
        )

    return items


def _intermediate_resources(book_key: str) -> list[dict[str, str]]:
    book_source = f"intermediate-{book_key}-book"
    vocab_source = f"intermediate-{book_key}-vocab"
    book_label = INTERMEDIATE_BOOK_LABELS[book_key]
    items = []

    if get_material_source_path(book_source):
        items.append(
            {
                "name": f"{book_label} 主教材 PDF",
                "detail": "如果你想对照原版排版、练习和插图，直接打开这份 PDF。",
                "sourceKey": book_source,
            }
        )

    if get_material_source_path(vocab_source):
        items.append(
            {
                "name": f"{book_label} 单词 PDF",
                "detail": "这份词汇 PDF 可以直接对照今天课次做记忆和复习。",
                "sourceKey": vocab_source,
            }
        )

    return items


def _build_beginner_day(lesson_number: int) -> dict[str, Any]:
    lesson = _get_beginner_lesson(lesson_number)
    text = lesson.get("text", "")
    title = lesson.get("title") or BEGINNER_TITLES.get(lesson_number, f"第 {lesson_number} 课")
    return {
        "mode": "full-text",
        "title": _lesson_heading(lesson_number),
        "summary": "已从你本地教材里抽出课文、译文和单词，可直接在网页里看，不用再手动翻书。",
        "blocks": [
            {
                "title": "本地教材摘录",
                "text": _truncate(text),
            }
        ],
        "resources": _beginner_resources(lesson_number),
        "lessonNumber": lesson_number,
        "lessonTitle": title,
    }


def _build_beginner_review(start_lesson: int, end_lesson: int) -> dict[str, Any]:
    snippets = []
    for lesson_number in range(start_lesson, end_lesson + 1):
        lesson = _get_beginner_lesson(lesson_number)
        if not lesson:
            continue
        snippets.append(
            _review_excerpt(
                _lesson_heading(lesson_number),
                lesson.get("text", ""),
            )
        )

    resources = _beginner_resources(end_lesson)
    return {
        "mode": "full-text",
        "title": f"第 {start_lesson}-{end_lesson} 课复盘",
        "summary": "复盘日会把最近几课的本地内容压缩展示，便于你快速回看场景、句型和单词。",
        "blocks": [
            {
                "title": "本地复盘摘录",
                "text": "\n\n".join(snippets),
            }
        ],
        "resources": resources,
        "lessonRange": [start_lesson, end_lesson],
    }


def _build_intermediate_lesson(book_key: str, lesson_number: int) -> dict[str, Any]:
    vocab_source = f"intermediate-{book_key}-vocab"
    static_payload = _load_intermediate_static_cache().get(book_key, {}).get(str(lesson_number), {})
    vocab_text = static_payload.get("vocab") or _intermediate_vocab_lessons(vocab_source).get(lesson_number, "")
    book_label = INTERMEDIATE_BOOK_LABELS[book_key]
    blocks = []

    if static_payload.get("conversation"):
        blocks.append({
            "title": "本地会话高置信摘录",
            "text": _truncate(static_payload["conversation"], 2600),
        })
    if static_payload.get("reading"):
        blocks.append({
            "title": "本地课文高置信摘录",
            "text": _truncate(static_payload["reading"], 2600),
        })
    if vocab_text:
        blocks.append({
            "title": "本地单词表摘录",
            "text": _truncate(vocab_text, 1800),
        })

    return {
        "mode": "full-text" if blocks else "preview-plus-vocab",
        "title": f"{book_label} 第 {lesson_number} 课",
        "summary": "中级正文已提前离线 OCR 并写入静态缓存。页面只读取处理后的高置信会话摘录、课文摘录和单词内容，不会在打开时重新跑 OCR；低质量识别行已被过滤。",
        "blocks": blocks or [
            {
                "title": "本地单词表摘录",
                "text": _truncate(vocab_text or "当前没有抽到这一课的词汇文本，请直接打开右侧 PDF。", 1800),
            }
        ],
        "resources": _intermediate_resources(book_key),
        "lessonNumber": lesson_number,
        "lessonTitle": f"第 {lesson_number} 课",
    }


def _build_intermediate_review(book_key: str, lesson_numbers: list[int]) -> dict[str, Any]:
    vocab_source = f"intermediate-{book_key}-vocab"
    book_label = INTERMEDIATE_BOOK_LABELS[book_key]
    static_payload = _load_intermediate_static_cache().get(book_key, {})
    vocab_lessons = _intermediate_vocab_lessons(vocab_source)
    excerpts = []

    for lesson_number in lesson_numbers:
        cached_lesson = static_payload.get(str(lesson_number), {})
        combined = []
        if cached_lesson.get("conversation"):
            combined.append(_section_excerpt("会话", cached_lesson["conversation"], 700))
        if cached_lesson.get("reading"):
            combined.append(_section_excerpt("课文", cached_lesson["reading"], 700))
        lesson_text = cached_lesson.get("vocab") or vocab_lessons.get(lesson_number, "")
        if lesson_text:
            combined.append(_section_excerpt("单词", lesson_text, 420))
        if combined:
            excerpts.append(f"【第 {lesson_number} 课】\n" + "\n\n".join([part for part in combined if part]))

    return {
        "mode": "full-text",
        "title": f"{book_label} 第 {lesson_numbers[0]}-{lesson_numbers[-1]} 课复盘",
        "summary": "中级复盘日会把已经离线处理好的高置信会话摘录、课文摘录和单词内容压缩到一起，方便直接在网页里回看。",
        "blocks": [
            {
                "title": "本地复盘摘录",
                "text": "\n\n".join(excerpts),
            }
        ],
        "resources": _intermediate_resources(book_key),
        "lessonRange": [lesson_numbers[0], lesson_numbers[-1]],
    }


def _foundation_lesson_for_day(day: int) -> int:
    return day - (day // 7)


def _intermediate_schedule(day: int) -> Optional[dict[str, Any]]:
    if day < 57 or day > 92:
        return None

    if day <= 74:
        offset = day - 57
        if offset == 8:
            return {"book": "upper", "review": True, "lessons": list(range(1, 9))}
        if offset == 17:
            return {"book": "upper", "review": True, "lessons": list(range(9, 17))}
        lesson_number = 1 + offset - (1 if offset > 8 else 0)
        return {"book": "upper", "review": False, "lesson": lesson_number}

    offset = day - 75
    if offset == 8:
        return {"book": "lower", "review": True, "lessons": list(range(17, 25))}
    if offset == 17:
        return {"book": "lower", "review": True, "lessons": list(range(25, 33))}
    lesson_number = 17 + offset - (1 if offset > 8 else 0)
    return {"book": "lower", "review": False, "lesson": lesson_number}


@lru_cache(maxsize=128)
def get_local_material_for_day(day: int) -> dict[str, Any]:
    if day < 1 or day > 99:
        raise ValueError("day out of range")

    if day <= 56:
        if not os.path.isdir(JP_DIR):
            return _unavailable_payload("当前服务器没有挂载本地日语资料目录，因此初级阶段无法读取本地教材摘录。")

        if fitz is None or PdfReader is None:
            return _unavailable_payload("当前服务器缺少本地教材解析依赖，因此初级阶段无法读取本地教材摘录。")

        if day % 7 == 0:
            end_lesson = _foundation_lesson_for_day(day)
            start_lesson = max(1, end_lesson - 5)
            return _build_beginner_review(start_lesson, end_lesson)
        return _build_beginner_day(_foundation_lesson_for_day(day))

    if day <= 92:
        schedule = _intermediate_schedule(day)
        if not schedule:
            return {
                "mode": "unavailable",
                "title": "本地教材暂不可用",
                "summary": "当前没有匹配到这一天的本地教材。",
                "blocks": [],
                "resources": [],
            }

        if not _intermediate_static_cache_ready() and not _local_materials_runtime_ready():
            return _unavailable_payload("当前服务器既没有已提交的中级 OCR 缓存，也没有挂载本地教材目录，因此无法读取中级教材摘录。")

        if schedule["review"]:
            return _build_intermediate_review(schedule["book"], schedule["lessons"])
        return _build_intermediate_lesson(schedule["book"], schedule["lesson"])

    return {
        "mode": "review-only",
        "title": "考前收口",
        "summary": "最后一周以官方题型、错题本和你已经整理好的高频词为主，不再强行推进新课。",
        "blocks": [
            {
                "title": "本地教材使用建议",
                "text": "把前面 92 天里最容易错的标日课次重新过一遍，优先看你自己做过标记的课文、语法和单词。",
            }
        ],
        "resources": [
            {
                "name": "标日初级上 PDF",
                "detail": "回看最薄弱的基础课次。",
                "sourceKey": "beginner-upper-book",
            },
            {
                "name": "标日初级下 PDF",
                "detail": "回看常错语法和应用课文。",
                "sourceKey": "beginner-lower-book",
            },
            {
                "name": "标日中级上 PDF",
                "detail": "挑你最薄弱的中级课次做最后回看。",
                "sourceKey": "intermediate-upper-book",
            },
            {
                "name": "标日中级下 PDF",
                "detail": "重点看自己常错的主题和表达。",
                "sourceKey": "intermediate-lower-book",
            },
        ],
    }