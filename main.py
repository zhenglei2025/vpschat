import os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse
from typing import List
import shutil
import json

app = FastAPI()

CHAT_PASSWORD = "3635363"

# 确保上传目录存在
UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# 管理活动的 WebSocket 连接
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

# 1. 首页 UI
@app.get("/")
async def get():
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())

# 2. 聊天 WebSocket
@app.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await websocket.accept()
    # 等待客户端发送密码
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

# 3. 文件上传接口
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

# 4. 文件下载接口
@app.get("/download/{filename}")
async def download_file(filename: str):
    return FileResponse(path=os.path.join(UPLOAD_DIR, filename), filename=filename)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
