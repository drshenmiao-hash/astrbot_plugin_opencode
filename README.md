# astrbot_plugin_opencode

让 AstrBot 的 QQ 机器人通过官方 `opencode acp` 使用明确指定的模型。插件会在校验模型后注册原生 `opencode_cli` 来源，模型以 `opencode_cli:provider/model` 显示。需要使用时，在目标会话里选择它即可。

插件支持文本、兼容模型的图片输入、会话复用、system 增量、排队提示「呜呜等一下」、断流单次重试和本地用量管理页。AstrBot 继续管理人格、多轮历史和备用模型。模型用量由你在 OpenCode 配置的服务商结算。

## 安装

当前发布版本为 `v0.2.1`，可在 AstrBot 插件页用本仓库地址安装或更新，也可上传发布页的源码 ZIP。[v0.2.1 发布页](https://github.com/drshenmiao-hash/astrbot_plugin_opencode/releases/tag/v0.2.1)

适用 AstrBot 4.29.4 系列、Python 3.12+ 和 Linux。OpenCode ACP 对接以官方 CLI 1.19.6 为基准。图片校验依赖的 Pillow 已在 `requirements.txt` 声明。在 **AstrBot 实际运行的环境**中安装官方原生 `opencode` 二进制和 `bubblewrap`。

- 原生 Ubuntu 可通过 `apt install bubblewrap` 安装隔离工具，并从 [OpenCode 官方发布页](https://github.com/anomalyco/opencode/releases/tag/v1.19.6) 安装对应架构的二进制。
- Ubuntu 上的 Docker 或 1Panel 部署可以在独立测试编排中使用下面的镜像配方。配方面向 Linux amd64，基础镜像须有 apt。用你的 AstrBot 镜像标签替换示例变量，保留原有数据卷与启动设置。如果基础镜像使用非 root 用户，用 `--build-arg ASTRBOT_USER=原用户` 保留该用户。

```bash
docker build -f Dockerfile.astrbot \
  --build-arg ASTRBOT_IMAGE=your-astrbot-image:tag \
  -t astrbot-with-opencode:0.2.1 .
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

切换 `model_id` 后，原先自动注册的模型保留其 ID 和配置并暂停使用。切回旧模型时，只恢复插件因模型切换而停用的条目。已保存的默认模型和会话选择由 AstrBot 管理，切换后可在目标会话手动选择新模型。

OpenCode 仅使用所选服务商写入专用 `auth.json` 的 API Key。通过外部插件或浏览器 OAuth 登录的账户、代理环境变量、自定义服务商配置、音频与 AstrBot 工具调用均未接入。当前模型列表取自所安装 OpenCode 二进制的内置目录；如果所需模型缺失，先在测试环境核对 CLI 版本。

## 图片输入

OpenCode 1.19.6 通过 ACP 接收图文和多图。插件在首次使用图片时用同一隔离环境下的官方 `opencode models --verbose` 核对当前模型的图片能力，不额外调用模型。模型不支持时返回 `image_unsupported`，可在设置中用 `image_unsupported_message` 自定义提示。能力无法确认时返回 `image_capability_unverified`。模型 ID 仍由 ACP 校验。

每轮最多 4 张静态 PNG、JPEG 或 WebP，单张最多 2 MiB，合计最多 4 MiB，每张最多 400 万像素。只接收 AstrBot 准备好的内联数据或其临时目录内的普通文件，不直接下载 URL。超限时日志会记录具体限制和已测得的值。发送给 ACP 的只有当前轮附件。会话重建时保留历史文字并标记旧图缺失，已复用会话中的历史仍由 OpenCode 管理。`wire_bytes` 只统计插件发送的 ACP 帧。

## 隔离与统计

运行时创建临时 HOME 和空工作目录，只复制选定服务商的认证条目。OpenCode 使用专用文本 agent、拒绝工具权限，ACP 也拒绝权限请求。应用按固定清单传递环境变量，以 bubblewrap 隔离文件和进程视图。CLI 需要网络访问所选服务商。容器内如果无法创建隔离，插件会返回错误。

登录 AstrBot WebUI，在 OpenCode 插件详情打开 **用量与运行状态**。页面显示当前模型、CLI 状态、排队和执行中的请求数，也可以查看近 7、30 或 90 天的请求趋势、成功率、耗时、流量和进程／会话复用情况。明细支持按模型、匿名会话和结果分组。聊天中的管理员命令 `/opencode_usage` 仍可使用，附加 `session`、`day`、`model` 可查看分组。

日期按 UTC 划分。每次处理尝试单独记录，包含失败、重试和缓存命中。**Token 用量**读取 OpenCode 1.19.6 写入临时会话库的计数，显示总量、输入、输出和缓存命中，支持按模型、匿名会话和日期查看。输入包含缓存读写，输出包含推理 Token。历史记录、回放和服务商未提供的计数显示为 `—`。插件只保存数字和请求元数据，统计库可沿用。费用和额度以模型服务商的账单为准。

CLI、认证或模型校验失败时，用量页仍会显示历史记录和启动失败原因。启动时的暂时性故障最多尝试 3 次，已有模型的后续请求可在冷却后共用一次校验。认证和模型配置错误需处理后重载。当前聊天失败时提示由 AstrBot 处理后续响应。

使用 `/opencode_source` 可查看许可证和此版本的源码地址。

## 验证与升级

在可以导入 AstrBot 4.29.4 的 Python 环境运行：

```bash
python -m unittest discover -s tests
```

默认测试使用模拟 ACP 对端。下面的可选测试通过 bubblewrap 启动官方 CLI 1.19.6，把上游指向本地模拟模型。它验证两轮文本复用、Token 读取、认证报错，以及真实 CLI 向本地模型替身转发图片字节。还验证纯文本模型在发出上游请求前拒绝图片。

```bash
OPENCODE_TEST_BIN=/usr/local/bin/opencode python -m unittest tests.test_official_cli -v
```

真实服务商账号和 QQ 联动需在安装环境中验证。

更新时上传新版 ZIP 后重载插件。使用测试编排检查 CLI 和 bubblewrap 的兼容性。需要恢复时可换回旧 ZIP，统计和认证目录可独立保留。

会话、图片和校验恢复逻辑改编自 [astrbot_plugin_cursor](https://github.com/drshenmiao-hash/astrbot_plugin_cursor)，原生 Provider 注册与重载流程借鉴 [astrbot_plugin_openai_oauth](https://github.com/wcqqq1214/astrbot_plugin_openai_oauth)。

[AGPL-3.0](LICENSE) · [来源及许可说明](NOTICE) · [OpenCode ACP 文档](https://opencode.ai/docs/acp/)
