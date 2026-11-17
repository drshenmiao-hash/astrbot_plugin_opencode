# astrbot_plugin_opencode

让 AstrBot 的 QQ 机器人通过官方 `opencode acp` 使用明确指定的模型。插件会在校验模型后注册原生 `opencode_cli` 来源，模型以 `opencode_cli:provider/model` 显示。需要使用时，在目标会话里选择它即可。

第一版支持纯文本聊天、会话复用、system 增量、排队提示「呜呜等一下」、断流单次重试和本地统计。AstrBot 继续管理人格、多轮历史和备用模型。模型用量由你在 OpenCode 配置的服务商结算。

## 安装

适用 AstrBot 4.29.4 系列、Python 3.10+ 和 Linux。OpenCode ACP 对接以官方 CLI 1.19.6 为基准。需要在 **AstrBot 实际运行的环境**中安装官方原生 `opencode` 二进制和 `bubblewrap`。宿主机安装的工具不会自动进入 AstrBot 容器。

- 原生 Ubuntu 可通过 `apt install bubblewrap` 安装隔离工具，并从 [OpenCode 官方发布页](https://github.com/anomalyco/opencode/releases/tag/v1.19.6) 安装对应架构的二进制。
- Ubuntu 上的 Docker 或 1Panel 部署可以在独立测试编排中使用下面的镜像配方。配方面向 Linux amd64，基础镜像须有 apt。用你的 AstrBot 镜像标签替换示例变量，保留原有数据卷与启动设置。如果基础镜像使用非 root 用户，用 `--build-arg ASTRBOT_USER=原用户` 保留该用户。

```bash
docker build -f Dockerfile.astrbot \
  --build-arg ASTRBOT_IMAGE=your-astrbot-image:tag \
  -t astrbot-with-opencode:0.1.0 .
```

使用 AstrBot 运行用户，在相同环境登录你打算用的服务商，并选择 API Key 登录方式。独立 XDG 目录可以避免复用个人开发环境的认证。

```bash
OPENCODE_AUTH_DATA="$HOME/.local/share/astrbot-opencode"
install -d -m 700 "$OPENCODE_AUTH_DATA"
XDG_DATA_HOME="$OPENCODE_AUTH_DATA" /usr/local/bin/opencode auth login
chmod 600 "$OPENCODE_AUTH_DATA/opencode/auth.json"
XDG_DATA_HOME="$OPENCODE_AUTH_DATA" /usr/local/bin/opencode models
```

1. 在 AstrBot 插件页上传源码 ZIP，或把此目录放到 `data/plugins/astrbot_plugin_opencode/`。
2. 在插件设置填写 `model_id`，例如你在模型列表里看到且准备使用的 `opencode/big-pickle`。填写 CLI 和专用 `auth.json` 的绝对路径，或使用上面的默认路径。保存并重载。
3. 校验会连接本地官方 CLI，枚举 ACP 模型选项，并确认使用专用聊天 agent。通过后插件自动注册模型。首次聊天前，请在目标会话手动选择它。

OpenCode 仅使用所选服务商写入专用 `auth.json` 的 API Key。通过外部插件或浏览器 OAuth 登录的账户、代理环境变量、自定义服务商配置、图片与 AstrBot 工具调用均未接入。当前模型列表取自所安装 OpenCode 二进制的内置目录；如果所需模型缺失，先在测试环境核对 CLI 版本。

## 隔离与统计

运行时创建临时 HOME 和空工作目录，只复制选定服务商的认证条目。OpenCode 使用专用文本 agent、拒绝工具权限，ACP 也拒绝权限请求。应用按固定清单传递环境变量，以 bubblewrap 隔离文件和进程视图。CLI 需要网络访问所选服务商。容器内如果无法创建隔离，插件会返回错误。

管理员可使用 `/opencode_usage` 查看本地请求次数、字节数、失败码和复用数据，或加 `session`、`day`、`model` 查看分组。这些是插件记录的运行数据。费用和额度以服务商账单为准。统计库位于 `data/plugin_data/astrbot_plugin_opencode/`。

## 验证与升级

```bash
python -m unittest discover -s tests
```

测试使用模拟 ACP 对端。本项目还在隔离环境中用官方 CLI 1.19.6 实测了 ACP 初始化、模型枚举、指定模型和聊天 agent 确认。实际 QQ 联动、服务商账号认证和生成请求由安装者在自己的环境验证。

更新时上传新版 ZIP 后重载插件。使用测试编排检查 CLI 和 bubblewrap 的兼容性。需要恢复时可换回旧 ZIP，统计和认证目录可独立保留。

[AGPL-3.0](LICENSE) · [来源及许可说明](NOTICE) · [OpenCode ACP 文档](https://opencode.ai/docs/acp/)
