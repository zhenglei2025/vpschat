# 在 `vps2` 上重启 `vps_chat_relay` 服务

这份项目当前在 `vps2` 上不是通过 `systemd` 管理，而是直接在远端用后台命令启动：

```bash
env PORT=80 python3 -B /root/vps_chat_relay/main.py
```

## 最稳的重启方式

先登录：

```bash
ssh vps2
```

进入项目目录：

```bash
cd /root/vps_chat_relay
```

停止旧进程：

```bash
ps -ef | grep '/root/vps_chat_relay/main.py' | grep -v grep | awk '{print $2}' | xargs -r kill
```

重新启动：

```bash
setsid sh -c 'env PORT=80 python3 -B /root/vps_chat_relay/main.py >> server.log 2>&1 < /dev/null' >/dev/null 2>&1 &
```

## 启动后检查

确认进程还在：

```bash
ps -ef | grep '/root/vps_chat_relay/main.py' | grep -v grep
```

确认 80 端口在监听：

```bash
ss -ltnp | grep ':80 '
```

确认首页和摄影教程页能访问：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/portrait-photography-tutorial
```

正常情况下这两个请求都应返回：

```text
200
200
```

## 看日志

如果启动失败，先看日志尾部：

```bash
tail -n 80 /root/vps_chat_relay/server.log
```

## 一条龙版本

如果只想快速执行，也可以在本机直接运行：

```bash
ssh vps2
```

然后在远端粘贴：

```bash
cd /root/vps_chat_relay
ps -ef | grep '/root/vps_chat_relay/main.py' | grep -v grep | awk '{print $2}' | xargs -r kill
setsid sh -c 'env PORT=80 python3 -B /root/vps_chat_relay/main.py >> server.log 2>&1 < /dev/null' >/dev/null 2>&1 &
sleep 2
ps -ef | grep '/root/vps_chat_relay/main.py' | grep -v grep
ss -ltnp | grep ':80 ' || true
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/portrait-photography-tutorial
```

## 备注

- `pkill -f` 在某些情况下可能误伤当前 SSH 会话，不如按 PID 杀进程稳。
- 这个项目当前没有 `systemd` service 文件，所以不要直接用 `systemctl restart`。
- 如果后面把服务改成 `systemd` 或 `supervisor`，这份文档也要同步更新。
