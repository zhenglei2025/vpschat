import os
import time
import asyncio
import subprocess
import stat
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from typing import List
import shutil
import json

# ===== 配置 =====
CHAT_PASSWORD = "3635363"
UPLOAD_DIR = "uploads"
REPOS_DIR = "repos"
PATCHES_DIR = "patches"
CLEANUP_INTERVAL = 86400   # 每天检查一次（秒）
FILE_MAX_AGE = 5 * 86400   # 文件最大保留 5 天（秒）

# 确保目录存在
for d in [UPLOAD_DIR, REPOS_DIR, PATCHES_DIR]:
    os.makedirs(d, exist_ok=True)


# ===== 后台清理任务 =====
async def cleanup_old_files():
    """定期删除超过 5 天的上传文件和 patch 文件"""
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

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

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


# ===== 1. 首页 UI =====
@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


# ===== 2. 聊天 WebSocket =====
@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    try:
        password = await websocket.receive_text()
    except WebSocketDisconnect:
        return
    if password != CHAT_PASSWORD:
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


# ===== 3. 文件上传 =====
@app.post("/upload")
async def upload_file(file: UploadFile = File(...), sender: str = "Unknown"):
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


# ===== 4. 文件下载 =====
@app.get("/download/{filename}")
async def download_file(filename: str):
    return FileResponse(path=os.path.join(UPLOAD_DIR, filename), filename=filename)


# ===== 5. Git 仓库管理 =====

@app.get("/repos")
async def list_repos():
    """列出所有 bare repo"""
    repos = []
    if os.path.exists(REPOS_DIR):
        for name in sorted(os.listdir(REPOS_DIR)):
            repo_path = os.path.join(REPOS_DIR, name)
            if os.path.isdir(repo_path) and name.endswith(".git"):
                display_name = name[:-4]  # 去掉 .git 后缀
                repos.append({
                    "name": display_name,
                    "path": os.path.abspath(repo_path),
                })
    return repos


@app.post("/repos/{name}")
async def create_repo(name: str):
    """创建新的 bare repo 并写入 post-receive hook"""
    repo_name = name if name.endswith(".git") else f"{name}.git"
    repo_path = os.path.join(REPOS_DIR, repo_name)

    if os.path.exists(repo_path):
        return JSONResponse(status_code=400, content={"error": "仓库已存在"})

    # 创建 bare repo
    result = subprocess.run(
        ["git", "init", "--bare", repo_path],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return JSONResponse(status_code=500, content={"error": result.stderr})

    # 写入 post-receive hook
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
    # 添加执行权限
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


# ===== 6. Git Hook 回调 =====

from fastapi import Request

@app.post("/hook/{repo_name}")
async def git_hook(repo_name: str, request: Request):
    """post-receive hook 回调：生成 patch 并广播"""
    body = await request.json()
    oldrev = body.get("oldrev", "")
    newrev = body.get("newrev", "")
    ref = body.get("ref", "")
    branch = ref.split("/")[-1] if "/" in ref else ref

    repo_dir_name = f"{repo_name}.git"
    repo_path = os.path.join(REPOS_DIR, repo_dir_name)

    if not os.path.exists(repo_path):
        return JSONResponse(status_code=404, content={"error": "仓库不存在"})

    # 判断是否为新分支（oldrev 全为 0）
    is_new_branch = oldrev == "0" * 40

    # 生成 patch 文件
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    patch_filename = f"{repo_name}_{branch}_{timestamp}.patch"
    patch_path = os.path.join(PATCHES_DIR, patch_filename)

    try:
        if is_new_branch:
            # 新分支：导出所有 commit 的 diff
            result = subprocess.run(
                ["git", "--git-dir", repo_path, "diff", "--stat", newrev],
                capture_output=True, text=True
            )
            # 对于新分支，用 format-patch 导出
            result = subprocess.run(
                ["git", "--git-dir", repo_path, "format-patch",
                 "--stdout", f"--root", newrev],
                capture_output=True, text=True
            )
        else:
            # 已有分支：生成两个 commit 之间的 patch
            result = subprocess.run(
                ["git", "--git-dir", repo_path, "format-patch",
                 "--stdout", f"{oldrev}..{newrev}"],
                capture_output=True, text=True
            )

        if result.stdout:
            with open(patch_path, "w") as f:
                f.write(result.stdout)
        else:
            # fallback: 用 diff
            result = subprocess.run(
                ["git", "--git-dir", repo_path, "diff", f"{oldrev}..{newrev}"],
                capture_output=True, text=True
            )
            with open(patch_path, "w") as f:
                f.write(result.stdout if result.stdout else "# Empty patch\n")

    except Exception as e:
        with open(patch_path, "w") as f:
            f.write(f"# Error generating patch: {e}\n")

    # 统计 commit 数
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

    # 广播 push 事件
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


# ===== 7. Patch 列表和下载 =====

@app.get("/patches")
async def list_patches():
    """返回所有 patch 文件列表"""
    patches = []
    if os.path.exists(PATCHES_DIR):
        for filename in sorted(os.listdir(PATCHES_DIR), reverse=True):
            filepath = os.path.join(PATCHES_DIR, filename)
            if os.path.isfile(filepath) and filename.endswith(".patch"):
                # 从文件名解析信息：repo_branch_timestamp.patch
                parts = filename[:-6].rsplit("_", 2)  # 去掉 .patch
                repo = parts[0] if len(parts) >= 3 else filename
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
async def download_patch(filename: str):
    """下载 patch 文件"""
    filepath = os.path.join(PATCHES_DIR, filename)
    if not os.path.exists(filepath):
        return JSONResponse(status_code=404, content={"error": "文件不存在"})
    return FileResponse(path=filepath, filename=filename)


# ===== 启动 =====
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
