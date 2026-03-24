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
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Request, Header
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from typing import List, Optional
import shutil
import json
import termios

# ===== 配置 =====
CHAT_PASSWORD = "3635363"
UPLOAD_DIR = "uploads"
REPOS_DIR = "repos"
PATCHES_DIR = "patches"
CLEANUP_INTERVAL = 86400   # 每天检查一次（秒）
FILE_MAX_AGE = 5 * 86400   # 文件最大保留 5 天（秒）
TOKEN_MAX_AGE = 86400       # Token 有效期 24 小时

# 确保目录存在
for d in [UPLOAD_DIR, REPOS_DIR, PATCHES_DIR]:
    os.makedirs(d, exist_ok=True)


# ===== Token 管理 =====
# {token: expire_timestamp}
active_tokens: dict[str, float] = {}


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
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ===== 2. 登录接口（公开） =====
@app.post("/login")
async def login(request: Request):
    """验证密码，返回 token"""
    body = await request.json()
    password = body.get("password", "")
    if password != CHAT_PASSWORD:
        return JSONResponse(status_code=401, content={"error": "密码错误"})
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
  curl -s -X POST "http://127.0.0.1:8000/hook/{display_name}" \\
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
    return patches


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


# ===== 启动 =====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
