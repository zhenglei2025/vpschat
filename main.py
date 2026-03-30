import os
import pty
import fcntl
import struct
import signal
import time
import asyncio
import subprocess
import stat
import secrets
import mimetypes
from functools import lru_cache
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, Header
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from typing import List, Optional
import shutil
import json
import termios
import re
from threading import Lock
from urllib.parse import urlparse

from jlpt_local_materials import get_local_material_for_day, get_material_source_path

# ===== 配置 =====
CHAT_PASSWORD = "3635363"
UPLOAD_DIR = "uploads"
REPOS_DIR = "repos"
PATCHES_DIR = "patches"
NEWS_BASE_DIR = "/root/NewsAgent"
NEWS_CATEGORIES = [
    "arxiv_summaries", "finance_summaries", "live_summaries",
    "payment_summaries", "paper_summaries", "game_summaries",
]
CLEANUP_INTERVAL = 86400   # 每天检查一次（秒）
FILE_MAX_AGE = 5 * 86400   # 文件最大保留 5 天（秒）
TOKEN_MAX_AGE = 86400       # Token 有效期 24 小时
LOGIN_MAX_ATTEMPTS = 5      # 每分钟最多登录尝试次数
LOGIN_WINDOW = 60           # 限流窗口（秒）
VISITOR_STATS_FILE = "visitor_stats.json"
CCF_DEADLINES_FILE = "ccf_ai_deadlines.json"
MAX_RECENT_VISITS = 200
ARXIV_CONTACT_RE = re.compile(
    r'<p[^>]*>\s*<span[^>]*>📧\s*联系人：人工智能团队/郑雷\s*&nbsp;\s*zhenglei2@unionpay\.com</span>\s*</p>',
    re.IGNORECASE,
)
EMAIL_LAYOUT_WIDTH_RE = re.compile(r'(<table\b[^>]*?)\swidth="800"', re.IGNORECASE)
RESPONSIVE_NEWS_CATEGORIES = {"arxiv_summaries", "finance_summaries", "live_summaries"}
RESPONSIVE_NEWS_STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
    html, body {
        width: 100% !important;
        max-width: 100% !important;
        overflow-x: hidden !important;
    }
    body {
        margin: 0 !important;
        padding: 0 !important;
        word-break: break-word;
        overflow-wrap: anywhere;
    }
    body > table:first-of-type {
        width: 100% !important;
    }
    body > table:first-of-type > tbody > tr > td {
        padding: 20px 16px !important;
        text-align: center !important;
    }
    .responsive-news-shell {
        width: min(100%, 800px) !important;
        max-width: 800px !important;
        margin: 0 auto !important;
        background-color: #ffffff !important;
        text-align: left !important;
    }
    .responsive-news-shell td {
        max-width: 100% !important;
    }
    img, table, iframe, video {
        max-width: 100% !important;
    }
    a, code, pre, p, li, blockquote, td, div, span, h1, h2, h3, h4 {
        overflow-wrap: anywhere;
        word-break: break-word;
    }
    pre, code {
        white-space: pre-wrap !important;
    }
    @media (max-width: 768px) {
        body > table:first-of-type > tbody > tr > td {
            padding: 12px 0 !important;
        }
        .responsive-news-shell {
            border-left: none !important;
            border-right: none !important;
        }
        .responsive-news-shell td[style] {
            padding-left: 16px !important;
            padding-right: 16px !important;
        }
        .responsive-news-shell h1[style] {
            font-size: 20px !important;
            line-height: 1.35 !important;
        }
        .responsive-news-shell h2[style] {
            font-size: 18px !important;
            line-height: 1.45 !important;
        }
        .responsive-news-shell h3[style] {
            font-size: 16px !important;
            line-height: 1.45 !important;
        }
        .responsive-news-shell p,
        .responsive-news-shell li,
        .responsive-news-shell td,
        .responsive-news-shell blockquote {
            font-size: 15px !important;
            line-height: 1.8 !important;
        }
    }
</style>
""".strip()
JLPT_N2_PLAN_HTML = "jlpt_n2_plan.html"
AGENTIC_RL_GUIDE_HTML = "agentic_rl_guide.html"
GUFENG_XIQIANG_TUTORIAL_HTML = "gufeng_xiqiang_tutorial.html"
PORTRAIT_PHOTOGRAPHY_TUTORIAL_HTML = "portrait_photography_tutorial.html"
BEGINNER_STATIC_CACHE = "processed_beginner_materials.json"
INTERMEDIATE_STATIC_CACHE = "processed_intermediate_materials.json"


def get_server_port() -> int:
    try:
        return int(os.getenv("PORT", "8000"))
    except (TypeError, ValueError):
        return 8000


APP_PORT = get_server_port()

# 确保目录存在
for d in [UPLOAD_DIR, REPOS_DIR, PATCHES_DIR]:
    os.makedirs(d, exist_ok=True)


# ===== Token 管理 =====
# {token: expire_timestamp}
active_tokens: dict[str, float] = {}

# ===== 登录限流 =====
# {ip: [timestamp1, timestamp2, ...]}
login_attempts: dict[str, list[float]] = {}

# ===== 访客统计 =====
visitor_stats_lock = Lock()


def default_visitor_stats() -> dict:
    return {
        "total_page_views": 0,
        "daily_views": {},
        "page_views": {},
        "source_types": {},
        "referers": {},
        "browsers": {},
        "devices": {},
        "visitors": {},
        "recent_visits": [],
    }


def load_visitor_stats() -> dict:
    stats = default_visitor_stats()
    if not os.path.exists(VISITOR_STATS_FILE):
        return stats
    try:
        with open(VISITOR_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key, value in data.items():
                if key in stats and isinstance(value, type(stats[key])):
                    stats[key] = value
    except Exception:
        pass
    return stats


visitor_stats = load_visitor_stats()


def save_visitor_stats_locked():
    temp_file = f"{VISITOR_STATS_FILE}.tmp"
    with open(temp_file, "w", encoding="utf-8") as f:
        json.dump(visitor_stats, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, VISITOR_STATS_FILE)


def bump_counter(counter: dict, key: str, value: int = 1):
    if not key:
        return
    counter[key] = counter.get(key, 0) + value


def get_client_ip(request: Request) -> str:
    for header_name in ("x-forwarded-for", "x-real-ip"):
        header_value = request.headers.get(header_name, "").strip()
        if header_value:
            return header_value.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def mask_ip(ip: str) -> str:
    if "." in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return f"{parts[0]}.{parts[1]}.*.*"
    if ":" in ip:
        parts = [part for part in ip.split(":") if part]
        if len(parts) >= 3:
            return ":".join(parts[:3]) + ":*"
    return ip


def shorten_text(value: str, limit: int = 80) -> str:
    if not value:
        return ""
    return value if len(value) <= limit else value[:limit - 3] + "..."


def detect_browser(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if not ua:
        return "未知"
    if "micromessenger" in ua:
        return "微信"
    if "edg/" in ua:
        return "Edge"
    if "chrome/" in ua and "edg/" not in ua:
        return "Chrome"
    if "safari/" in ua and "chrome/" not in ua:
        return "Safari"
    if "firefox/" in ua:
        return "Firefox"
    if "curl/" in ua:
        return "curl"
    return "其他"


def detect_device(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if any(flag in ua for flag in ("bot", "spider", "crawler", "curl/")):
        return "Bot/脚本"
    if any(flag in ua for flag in ("mobile", "iphone", "android")):
        return "移动端"
    if any(flag in ua for flag in ("ipad", "tablet")):
        return "平板"
    return "桌面端"


def classify_source(request: Request, referer: str) -> str:
    if not referer:
        return "直接访问"
    referer_host = (urlparse(referer).netloc or "").lower()
    current_host = (request.headers.get("host") or "").lower()
    if referer_host and current_host and referer_host == current_host:
        return "站内跳转"
    return "外部来源"


def tracked_page_path(path: str) -> bool:
    return (
        path in {"/", "/news", "/chat", "/visitor-stats", "/ccf-deadlines", "/jlpt-n2-plan", "/gufeng-xiqiang-tutorial", "/portrait-photography-tutorial"}
        or path.startswith("/news/view/")
        or path.startswith("/jlpt-n2-plan/day/")
    )


def sanitize_news_html(category: str, content: str) -> str:
    if category == "arxiv_summaries":
        content = ARXIV_CONTACT_RE.sub("", content)
    if category in RESPONSIVE_NEWS_CATEGORIES:
        content = make_news_html_responsive(content)
    return content


def make_news_html_responsive(content: str) -> str:
    updated = EMAIL_LAYOUT_WIDTH_RE.sub(r'\1 class="responsive-news-shell" width="100%"', content, count=1)
    if 'name="viewport"' not in updated.lower():
        if "</head>" in updated:
            updated = updated.replace("</head>", f"{RESPONSIVE_NEWS_STYLE}\n</head>", 1)
        elif "<head>" in updated:
            updated = updated.replace("<head>", f"<head>\n{RESPONSIVE_NEWS_STYLE}\n", 1)
        else:
            updated = RESPONSIVE_NEWS_STYLE + "\n" + updated
    elif "</head>" in updated:
        updated = updated.replace("</head>", f"<style>{RESPONSIVE_NEWS_STYLE.split('<style>', 1)[1].rsplit('</style>', 1)[0]}</style>\n</head>", 1)
    return updated


@lru_cache(maxsize=1)
def load_jlpt_n2_plan_template() -> str:
    with open(JLPT_N2_PLAN_HTML, "r", encoding="utf-8") as f:
        return f.read()


@lru_cache(maxsize=1)
def load_beginner_materials_inline_json() -> str:
    try:
        with open(BEGINNER_STATIC_CACHE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        payload = {}
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


@lru_cache(maxsize=1)
def load_intermediate_materials_inline_json() -> str:
    try:
        with open(INTERMEDIATE_STATIC_CACHE, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except FileNotFoundError:
        payload = {}
    return json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")


def render_jlpt_n2_plan_html() -> str:
    template = load_jlpt_n2_plan_template()
    beginner_inline_json = load_beginner_materials_inline_json()
    intermediate_inline_json = load_intermediate_materials_inline_json()
    rendered = template.replace('"__EMBEDDED_BEGINNER_MATERIALS__"', beginner_inline_json, 1)
    return rendered.replace('"__EMBEDDED_INTERMEDIATE_MATERIALS__"', intermediate_inline_json, 1)


def record_visit(request: Request):
    path = request.url.path
    if request.method != "GET" or not tracked_page_path(path):
        return

    ip = get_client_ip(request)
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    day_key = now.strftime("%Y-%m-%d")
    referer = request.headers.get("referer", "").strip()
    source_type = classify_source(request, referer)
    referer_label = urlparse(referer).netloc or shorten_text(referer, 60) if referer else "直接访问"
    user_agent = request.headers.get("user-agent", "").strip()
    browser = detect_browser(user_agent)
    device = detect_device(user_agent)

    with visitor_stats_lock:
        bump_counter(visitor_stats["daily_views"], day_key)
        bump_counter(visitor_stats["page_views"], path)
        bump_counter(visitor_stats["source_types"], source_type)
        bump_counter(visitor_stats["browsers"], browser)
        bump_counter(visitor_stats["devices"], device)
        visitor_stats["total_page_views"] += 1

        if referer:
            bump_counter(visitor_stats["referers"], referer_label)

        visitor = visitor_stats["visitors"].get(ip)
        if not visitor:
            visitor = {
                "first_seen": now_str,
                "last_seen": now_str,
                "visits": 0,
                "pages": {},
            }
            visitor_stats["visitors"][ip] = visitor

        visitor["last_seen"] = now_str
        visitor["visits"] += 1
        bump_counter(visitor["pages"], path)

        visitor_stats["recent_visits"].insert(0, {
            "time": now_str,
            "ip": mask_ip(ip),
            "path": path,
            "referer": referer_label,
            "source_type": source_type,
            "browser": browser,
            "device": device,
        })
        visitor_stats["recent_visits"] = visitor_stats["recent_visits"][:MAX_RECENT_VISITS]
        save_visitor_stats_locked()


def top_items(counter: dict, limit: int = 10, label_key: str = "label") -> list[dict]:
    return [
        {label_key: key, "count": value}
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def build_visitor_stats_payload() -> dict:
    today_key = datetime.now().strftime("%Y-%m-%d")
    with visitor_stats_lock:
        top_visitors = []
        for ip, info in sorted(
            visitor_stats["visitors"].items(),
            key=lambda item: (-item[1].get("visits", 0), item[0])
        )[:20]:
            top_visitors.append({
                "ip": mask_ip(ip),
                "visits": info.get("visits", 0),
                "first_seen": info.get("first_seen", ""),
                "last_seen": info.get("last_seen", ""),
                "pages": top_items(info.get("pages", {}), limit=3, label_key="path"),
            })

        return {
            "summary": {
                "total_page_views": visitor_stats["total_page_views"],
                "unique_visitors": len(visitor_stats["visitors"]),
                "today_views": visitor_stats["daily_views"].get(today_key, 0),
                "tracked_pages": len(visitor_stats["page_views"]),
            },
            "pages": top_items(visitor_stats["page_views"], limit=20, label_key="path"),
            "sources": top_items(visitor_stats["source_types"], limit=10, label_key="source"),
            "referers": top_items(visitor_stats["referers"], limit=15, label_key="referer"),
            "browsers": top_items(visitor_stats["browsers"], limit=10, label_key="browser"),
            "devices": top_items(visitor_stats["devices"], limit=10, label_key="device"),
            "visitors": top_visitors,
            "recent_visits": visitor_stats["recent_visits"][:50],
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


def create_token() -> str:
    """创建新 token"""
    token = secrets.token_urlsafe(32)
    active_tokens[token] = time.time() + TOKEN_MAX_AGE
    return token


def verify_token(token: str) -> bool:
    """验证 token 是否有效"""
    if not token:
        return False
    expire = active_tokens.get(token)
    if expire is None:
        return False
    if time.time() > expire:
        active_tokens.pop(token, None)
        return False
    return True


def cleanup_expired_tokens():
    """清理过期 token"""
    now = time.time()
    expired = [t for t, exp in active_tokens.items() if now > exp]
    for t in expired:
        active_tokens.pop(t, None)


def get_token_from_header(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """从 Authorization header 提取 token"""
    if authorization and authorization.startswith("Bearer "):
        return authorization[7:]
    return None


# ===== 后台清理任务 =====
async def cleanup_old_files():
    """定期删除超过 5 天的上传文件 + 清理过期 token"""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.time()
        for dir_path in [UPLOAD_DIR]:
            if os.path.exists(dir_path):
                for filename in os.listdir(dir_path):
                    filepath = os.path.join(dir_path, filename)
                    if os.path.isfile(filepath):
                        file_age = now - os.path.getmtime(filepath)
                        if file_age > FILE_MAX_AGE:
                            os.remove(filepath)
                            print(f"[清理] 已删除过期文件: {filepath}")
        cleanup_expired_tokens()


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(cleanup_old_files())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def visitor_stats_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        if response.status_code < 400:
            record_visit(request)
    except Exception:
        pass
    return response


# ===== WebSocket 连接管理 =====
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except:
                pass

manager = ConnectionManager()


# ===== 认证中间件 =====
def require_auth(authorization: Optional[str] = Header(None)):
    """校验 HTTP 请求的 token"""
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not verify_token(token):
        return False
    return True


# ===== 1. 首页 UI（公开） =====
@app.get("/")
async def get():
    with open("news.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/chat")
async def chat_page():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/terminal")
async def terminal_page():
    with open("terminal.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ===== 2. 登录接口（公开） =====
@app.post("/login")
async def login(request: Request):
    """验证密码，返回 token（带 IP 限流）"""
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()

    # 清理过期记录，只保留窗口内的尝试
    attempts = login_attempts.get(client_ip, [])
    attempts = [t for t in attempts if now - t < LOGIN_WINDOW]
    login_attempts[client_ip] = attempts

    # 检查是否超过限制
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        return JSONResponse(status_code=429, content={"error": "尝试次数过多，请稍后再试"})

    body = await request.json()
    password = body.get("password", "")
    if password != CHAT_PASSWORD:
        # 仅在密码错误时记录尝试
        attempts.append(now)
        login_attempts[client_ip] = attempts
        return JSONResponse(status_code=401, content={"error": "密码错误"})
    # 登录成功，清除该 IP 的记录
    login_attempts.pop(client_ip, None)
    token = create_token()
    return {"token": token}


# ===== 3. 聊天 WebSocket（需 token） =====
@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    # 第一条消息必须是 token
    try:
        token = await websocket.receive_text()
    except WebSocketDisconnect:
        return
    if not verify_token(token):
        await websocket.send_text(json.dumps({"type": "auth", "success": False}))
        await websocket.close()
        return
    await websocket.send_text(json.dumps({"type": "auth", "success": True}))
    manager.active_connections.append(websocket)
    await manager.broadcast(json.dumps({"type": "system", "content": f"{client_id} 加入了聊天"}))
    try:
        while True:
            data = await websocket.receive_text()
            await manager.broadcast(json.dumps({"type": "chat", "sender": client_id, "content": data}))
    except WebSocketDisconnect:
        manager.disconnect(websocket)
        await manager.broadcast(json.dumps({"type": "system", "content": f"{client_id} 离开了聊天"}))


# ===== 4. 文件上传（需 token） =====
@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    sender: str = "Unknown",
    authorization: Optional[str] = Header(None)
):
    if not require_auth(authorization):
        return JSONResponse(status_code=401, content={"error": "未授权"})
    file_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    file_url = f"/download/{file.filename}"
    await manager.broadcast(json.dumps({
        "type": "file",
        "sender": sender,
        "filename": file.filename,
        "url": file_url
    }))
    return {"filename": file.filename}


# ===== 5. 文件下载（需 token，通过 query param） =====
@app.get("/download/{filename}")
async def download_file(filename: str, token: str = ""):
    if not verify_token(token):
        return JSONResponse(status_code=401, content={"error": "未授权"})
    return FileResponse(path=os.path.join(UPLOAD_DIR, filename), filename=filename)


# ===== 6. Git 仓库管理（需 token） =====

@app.get("/repos")
async def list_repos(authorization: Optional[str] = Header(None)):
    if not require_auth(authorization):
        return JSONResponse(status_code=401, content={"error": "未授权"})
    repos = []
    if os.path.exists(REPOS_DIR):
        for name in sorted(os.listdir(REPOS_DIR)):
            repo_path = os.path.join(REPOS_DIR, name)
            if os.path.isdir(repo_path) and name.endswith(".git"):
                display_name = name[:-4]
                repos.append({
                    "name": display_name,
                    "path": os.path.abspath(repo_path),
                })
    return repos


@app.post("/repos/{name}")
async def create_repo(name: str, authorization: Optional[str] = Header(None)):
    if not require_auth(authorization):
        return JSONResponse(status_code=401, content={"error": "未授权"})

    repo_name = name if name.endswith(".git") else f"{name}.git"
    repo_path = os.path.join(REPOS_DIR, repo_name)

    if os.path.exists(repo_path):
        return JSONResponse(status_code=400, content={"error": "仓库已存在"})

    result = subprocess.run(
        ["git", "init", "--bare", repo_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return JSONResponse(status_code=500, content={"error": result.stderr})

    hooks_dir = os.path.join(repo_path, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "post-receive")

    display_name = name.replace(".git", "")
    hook_script = f"""#!/bin/bash
while read oldrev newrev refname; do
  curl -s -X POST "http://127.0.0.1:{APP_PORT}/hook/{display_name}" \\
    -H "Content-Type: application/json" \\
    -d '{{"oldrev":"'$oldrev'","newrev":"'$newrev'","ref":"'$refname'"}}'
done
"""
    with open(hook_path, "w") as f:
        f.write(hook_script)
    st = os.stat(hook_path)
    os.chmod(hook_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    await manager.broadcast(json.dumps({
        "type": "system",
        "content": f"📦 Git 仓库 [{display_name}] 已创建"
    }))

    return {
        "name": display_name,
        "path": os.path.abspath(repo_path),
    }


# ===== 7. Git Hook 回调（仅 localhost，无需 token） =====

@app.post("/hook/{repo_name}")
async def git_hook(repo_name: str, request: Request):
    """post-receive hook 回调：仅允许 localhost 调用"""
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse(status_code=403, content={"error": "仅限本机调用"})

    body = await request.json()
    oldrev = body.get("oldrev", "")
    newrev = body.get("newrev", "")
    ref = body.get("ref", "")
    branch = ref.split("/")[-1] if "/" in ref else ref

    repo_dir_name = f"{repo_name}.git"
    repo_path = os.path.join(REPOS_DIR, repo_dir_name)

    if not os.path.exists(repo_path):
        return JSONResponse(status_code=404, content={"error": "仓库不存在"})

    is_new_branch = oldrev == "0" * 40

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    patch_filename = f"{repo_name}_{branch}_{timestamp}.patch"
    patch_path = os.path.join(PATCHES_DIR, patch_filename)

    try:
        if is_new_branch:
            result = subprocess.run(
                ["git", "--git-dir", repo_path, "format-patch",
                 "--stdout", "--root", newrev],
                capture_output=True, text=True
            )
        else:
            result = subprocess.run(
                ["git", "--git-dir", repo_path, "format-patch",
                 "--stdout", f"{oldrev}..{newrev}"],
                capture_output=True, text=True
            )

        if result.stdout:
            with open(patch_path, "w") as f:
                f.write(result.stdout)
        else:
            result = subprocess.run(
                ["git", "--git-dir", repo_path, "diff", f"{oldrev}..{newrev}"],
                capture_output=True, text=True
            )
            with open(patch_path, "w") as f:
                f.write(result.stdout if result.stdout else "# Empty patch\n")

    except Exception as e:
        with open(patch_path, "w") as f:
            f.write(f"# Error generating patch: {e}\n")

    commit_count = 0
    if not is_new_branch:
        try:
            count_result = subprocess.run(
                ["git", "--git-dir", repo_path, "rev-list", "--count",
                 f"{oldrev}..{newrev}"],
                capture_output=True, text=True
            )
            commit_count = int(count_result.stdout.strip())
        except:
            commit_count = 0

    patch_size = os.path.getsize(patch_path) if os.path.exists(patch_path) else 0

    await manager.broadcast(json.dumps({
        "type": "git_push",
        "repo": repo_name,
        "branch": branch,
        "commits": commit_count,
        "patch_filename": patch_filename,
        "patch_url": f"/patches/{patch_filename}",
        "patch_size": patch_size,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }))

    return {"status": "ok", "patch": patch_filename}


# ===== 8. Patch 列表和下载（需 token） =====

@app.get("/patches")
async def list_patches(authorization: Optional[str] = Header(None)):
    if not require_auth(authorization):
        return JSONResponse(status_code=401, content={"error": "未授权"})
    patches = []
    if os.path.exists(PATCHES_DIR):
        for filename in sorted(os.listdir(PATCHES_DIR), reverse=True):
            filepath = os.path.join(PATCHES_DIR, filename)
            if os.path.isfile(filepath) and filename.endswith(".patch"):
                patches.append({
                    "filename": filename,
                    "url": f"/patches/{filename}",
                    "size": os.path.getsize(filepath),
                    "time": datetime.fromtimestamp(
                        os.path.getmtime(filepath)
                    ).strftime("%Y-%m-%d %H:%M:%S"),
                })
    return patches[:10]


@app.get("/patches/{filename}")
async def download_patch(filename: str, token: str = ""):
    """下载 patch 文件（通过 query param 传 token）"""
    if not verify_token(token):
        return JSONResponse(status_code=401, content={"error": "未授权"})
    filepath = os.path.join(PATCHES_DIR, filename)
    if not os.path.exists(filepath):
        return JSONResponse(status_code=404, content={"error": "文件不存在"})
    return FileResponse(path=filepath, filename=filename)


# ===== 9. Web Terminal（需 token） =====

@app.websocket("/ws-terminal")
async def terminal_endpoint(websocket: WebSocket):
    await websocket.accept()

    # 第一条消息必须是 token
    try:
        token = await websocket.receive_text()
    except WebSocketDisconnect:
        return
    if not verify_token(token):
        await websocket.send_text(json.dumps({"type": "auth", "success": False}))
        await websocket.close()
        return
    await websocket.send_text(json.dumps({"type": "auth", "success": True}))

    # 创建 PTY 子进程
    child_pid, master_fd = pty.fork()
    if child_pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        os.execvp("/bin/bash", ["/bin/bash", "-l"])

    # 父进程：设置 master_fd 非阻塞
    fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    # 后台任务：读取 PTY 输出 → WebSocket
    async def read_pty():
        try:
            while True:
                await asyncio.sleep(0.01)
                try:
                    data = os.read(master_fd, 4096)
                    if data:
                        await websocket.send_bytes(data)
                except OSError:
                    pass
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    reader_task = asyncio.create_task(read_pty())

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" in msg:
                text_data = msg["text"]
                try:
                    cmd = json.loads(text_data)
                    if cmd.get("type") == "resize":
                        rows = cmd.get("rows", 24)
                        cols = cmd.get("cols", 80)
                        winsize = struct.pack("HHHH", rows, cols, 0, 0)
                        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                        continue
                except (json.JSONDecodeError, ValueError):
                    pass
                os.write(master_fd, text_data.encode("utf-8"))
            elif "bytes" in msg:
                os.write(master_fd, msg["bytes"])
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        reader_task.cancel()
        try:
            os.close(master_fd)
        except:
            pass
        try:
            os.kill(child_pid, signal.SIGTERM)
            os.waitpid(child_pid, os.WNOHANG)
        except:
            pass


# ===== 10. 新闻中心（公开访问） =====

@app.get("/news")
async def news_page():
    """新闻中心页面"""
    with open("news.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/visitor-stats")
async def visitor_stats_page():
    """访客统计页面"""
    with open("visitor_stats.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/visitor-stats/data")
async def visitor_stats_data(authorization: Optional[str] = Header(None)):
    if not require_auth(authorization):
        return JSONResponse(status_code=401, content={"error": "未授权"})
    return build_visitor_stats_payload()


@app.get("/ccf-deadlines")
async def ccf_deadlines_page():
    """CCF AI 顶会 ddl 页面"""
    with open("ccf_deadlines.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/ccf-deadlines/data")
async def ccf_deadlines_data():
    if not os.path.exists(CCF_DEADLINES_FILE):
        return JSONResponse(
            status_code=404,
            content={"error": "DDL 数据尚未生成，请先运行更新脚本"},
        )
    try:
        with open(CCF_DEADLINES_FILE, "r", encoding="utf-8") as f:
            return JSONResponse(content=json.load(f))
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"读取 DDL 数据失败: {e}"})


@app.get("/jlpt-n2-plan")
async def jlpt_n2_plan_page():
    """JLPT N2 学习计划页面"""
    return HTMLResponse(content=render_jlpt_n2_plan_html())


@app.get("/jlpt-n2-plan/day/{day}")
async def jlpt_n2_plan_day_page(day: int):
    """JLPT N2 单日计划页面"""
    if day < 1 or day > 99:
        return HTMLResponse(content="未找到对应学习计划页面", status_code=404)
    return HTMLResponse(content=render_jlpt_n2_plan_html())


@app.get("/agentic-rl-guide")
async def agentic_rl_guide_page():
    """Agentic RL 学习教材页面"""
    with open(AGENTIC_RL_GUIDE_HTML, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/rl_sections/{filename}")
async def rl_section_file(filename: str):
    """Serve Agentic RL guide section HTML fragments"""
    safe_name = os.path.basename(filename)
    if not safe_name.endswith(".html"):
        return JSONResponse(status_code=400, content={"error": "仅支持 HTML 文件"})
    filepath = os.path.join("rl_sections", safe_name)
    if not os.path.isfile(filepath):
        return JSONResponse(status_code=404, content={"error": "文件不存在"})
    with open(filepath, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/gufeng-xiqiang-tutorial")
async def gufeng_xiqiang_tutorial_page():
    """通用唱歌教程页面，侧重流行唱法和古风戏腔"""
    with open(GUFENG_XIQIANG_TUTORIAL_HTML, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/portrait-photography-tutorial")
async def portrait_photography_tutorial_page():
    """人像摄影教程页面"""
    with open(PORTRAIT_PHOTOGRAPHY_TUTORIAL_HTML, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


@app.get("/jlpt-materials/day/{day}")
async def jlpt_material_day(day: int):
    """返回指定 Day 对应的本地教材内容"""
    if day < 1 or day > 99:
        return JSONResponse(status_code=404, content={"error": "day 超出范围"})
    try:
        return JSONResponse(content=get_local_material_for_day(day))
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"读取本地教材失败: {e}"})


@app.get("/jlpt-materials/file/{source_key}")
async def jlpt_material_file(source_key: str):
    """打开白名单中的本地教材文件"""
    filepath = get_material_source_path(source_key)
    if not filepath:
        return JSONResponse(status_code=404, content={"error": "教材文件不存在"})
    media_type, _ = mimetypes.guess_type(filepath)
    return FileResponse(path=filepath, media_type=media_type or "application/octet-stream")


@app.get("/news/list")
async def news_list():
    """返回所有新闻 HTML 文件列表（按分类）"""
    result = {}
    for cat in NEWS_CATEGORIES:
        cat_dir = os.path.join(NEWS_BASE_DIR, cat)
        files = []
        if os.path.isdir(cat_dir):
            for f in os.listdir(cat_dir):
                if f.endswith(".html"):
                    files.append(f)
        files.sort(reverse=True)
        result[cat] = files
    return result


@app.get("/news/view/{category}/{filename}")
async def news_view(category: str, filename: str):
    """公开查看单篇新闻 HTML"""
    if category not in NEWS_CATEGORIES:
        return JSONResponse(status_code=404, content={"error": "分类不存在"})
    safe_filename = os.path.basename(filename)
    if not safe_filename.endswith(".html"):
        return JSONResponse(status_code=400, content={"error": "仅支持 HTML 文件"})
    filepath = os.path.join(NEWS_BASE_DIR, category, safe_filename)
    if not os.path.isfile(filepath):
        return JSONResponse(status_code=404, content={"error": "文件不存在"})
    with open(filepath, "r", encoding="utf-8") as f:
        return HTMLResponse(content=sanitize_news_html(category, f.read()))


# ===== 启动 =====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=APP_PORT)
