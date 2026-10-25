# Echo-Mate 密钥管理

## 目标

- 密钥按使用方拆分，不把所有服务凭据塞进一个 `.env`。
- 服务通过 systemd `LoadCredential` 只读取自己需要的凭据。
- 仓库只保存字段模板、使用方和轮换记录，不保存真实值。
- 客户端不保存、不发送 AI Key。

## 目录与权限

| 路径 | 权限 | 内容 |
|---|---|---|
| `/etc/echomate` | `700` | 凭据根目录 |
| `/etc/echomate/credentials` | `700` | 凭据文件目录 |
| `/etc/echomate/credentials/<name>` | `600` | 单个凭据 |
| `/root/echomate-deploy/shared/echomate_credentials.py` | `600` | 凭据读取兼容层 |
| `credential-manifest.example.yaml` | 仓库文件 | 字段、使用方、轮换信息 |

凭据文件只存值，不带 `KEY=` 前缀。例如：

```text
/etc/echomate/credentials/dashscope_api_key
/etc/echomate/credentials/lark_app_secret
```

## 服务边界

| 服务 | DashScope | 飞书 ID/Secret | Tavily |
|---|---:|---:|---:|
| `echomate-server.service` | 是 | 否 | 否 |
| `echomate-feishu-bot.service` | 是 | 是 | 是 |
| `echomate-monitor.service` | 否 | 是 | 否 |
| `echomate-ssh-session-watch.service` | 否 | 是 | 否 |

飞书机器人需要 DashScope，是因为普通聊天、意图识别和联网搜索问答在该进程内完成。这是当前架构的显式依赖，不通过读取 Server `.env` 绕过。

## systemd 配置

示例：

```ini
Environment=PYTHONPATH=/root/echomate-deploy/shared
LoadCredential=dashscope_api_key:/etc/echomate/credentials/dashscope_api_key
```

程序通过 `echomate_credentials.credential()` 读取：

1. 优先读 `$CREDENTIALS_DIRECTORY/<name>`。
2. 找不到时回退到旧环境变量或旧 `.env`。
3. 回退路径只用于兼容和回滚，不应成为长期主路径。

## 普通配置

以下内容不是秘密，继续放在服务原有配置文件中：

- 模型名、ASR/TTS 地址、超时、采样参数。
- 主动播报开关、间隔、上限和静音时段。
- 飞书 App ID、Owner Open ID 等身份标识可放配置，但当前为减少改动也已进入凭据目录。

Server 的 `DASHSCOPE_API_KEY` 已从 `.env` 移除；飞书的 `LARK_APP_SECRET` 和 `TAVILY_API_KEY` 已从 `.env` 移除。

## 轮换

1. 在对应平台创建新 Key 或 Secret。
2. 以 `0600` 覆盖 `/etc/echomate/credentials/<name>`。
3. 只重启使用该凭据的服务。
4. 确认服务 `active`、关键调用成功、日志无认证错误。
5. 吊销旧 Key，并更新清单中的 `last_rotated`。

不能只删除仓库中的明文；已进入 Git 历史的 Key 必须吊销。

## 检查

只输出布尔值和权限，不打印真实值：

```bash
systemctl is-active echomate-server.service
stat -c '%a %U:%G %n' /etc/echomate/credentials/*
```

不要使用 `systemctl show -p Environment`、`env` 或打印完整 `.env` 的方式检查秘密。
