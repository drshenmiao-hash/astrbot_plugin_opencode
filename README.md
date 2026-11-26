# astrbot_plugin_opencode

让 AstrBot 的 QQ 机器人通过官方 `opencode acp` 使用明确指定的模型。插件会在校验模型后注册原生 `opencode_cli` 来源，模型以 `opencode_cli:provider/model` 显示。需要使用时，在目标会话里选择它即可。

插件支持流式文本、兼容模型的图片输入、会话复用、system 增量、排队提示「呜呜等一下」、首段输出前特定上游错误的单次重试和本地用量管理页。AstrBot 继续管理人格、多轮历史和备用模型。模型用量由你在 OpenCode 配置的服务商结算。

## 从安装到第一次对话

1. 在 AstrBot WebUI 的**插件管理 → ＋**通过仓库 URL 安装，填写 `https://github.com/drshenmiao-hash/astrbot_plugin_opencode`。离线安装可上传 Release 的 **Source code (zip)**。[下载 v0.6.0](https://github.com/drshenmiao-hash/astrbot_plugin_opencode/archive/refs/tags/v0.6.0.zip)
2. 使用面板管理员账号打开插件详情中的**环境与配置**。页面会自动检查，先按「下一步」处理当前缺项。Docker 或 1Panel 中，页面看到的是 **AstrBot 容器内部**。
3. 按下方对应部署方式准备 OpenCode 和默认启用的 bubblewrap。若发现 PATH 候选，在插件设置中填写可用的绝对路径。设置由你保存。
4. 为要使用的服务商创建专用 API Key 认证文件，将完整的 `provider/model` 填入 `model_id`。**保存并重载本插件**，再打开环境页。仅补装文件或修正权限后，也可以点**重新检查环境**查看结果。
5. 模型项出现成功的 ACP 记录后，管理员在目标会话发送 `/provider`，找到 `opencode_cli:provider/model`，发送 `/provider 序号`。发一条消息验证服务商生成，再追问一次验证连续对话。**用量与运行状态**可查看成功次数和会话复用。

### 在 Linux 或 Docker 中准备依赖

需要 Linux、Python 3.12+ 和 AstrBot 4.29.4 或兼容的 4.x。OpenCode ACP 对接以官方原生 CLI 1.19.6 为测试基准。其他版本在环境页显示「兼容性未验证」。

- 原生 Ubuntu 可通过 `apt install bubblewrap` 安装隔离工具，并从 [OpenCode 官方发布页](https://github.com/anomalyco/opencode/releases/tag/v1.19.6) 安装对应架构的二进制。将解压的 `opencode` 放在 `/usr/local/bin/opencode`，或在插件设置中指定其绝对路径。可先运行 `opencode --version` 核对版本。
- Ubuntu 上的 Docker 或 1Panel 部署可以在独立测试编排中使用下面的镜像配方。配方面向 Linux amd64，基础镜像须有 apt。用你的 AstrBot 镜像标签替换示例变量，保留原有数据卷与启动设置。如果基础镜像使用非 root 用户，用 `--build-arg ASTRBOT_USER=原用户` 保留该用户。

```bash
docker build -f Dockerfile.astrbot \
  --build-arg ASTRBOT_IMAGE=your-astrbot-image:tag \
  -t astrbot-with-opencode:0.6.0 .
```

仅构建镜像不会改变正在运行的容器。由部署管理员在原编排中选择新镜像，保留数据卷；如果 bubblewrap 无法创建所需命名空间，请管理员核对容器和内核限制。插件不会修改镜像或运行参数。

### 配置专用认证与模型

容器部署请将下面的专用认证目录放在持久卷中。使用 AstrBot 运行用户，在相同环境登录你打算用的服务商，并选择 API Key 登录方式。独立 XDG 目录可以避免复用个人开发环境的认证。

```bash
OPENCODE_AUTH_DATA="$HOME/.local/share/astrbot-opencode"
install -d -m 700 "$OPENCODE_AUTH_DATA"
XDG_DATA_HOME="$OPENCODE_AUTH_DATA" /usr/local/bin/opencode auth login
chmod 600 "$OPENCODE_AUTH_DATA/opencode/auth.json"
XDG_DATA_HOME="$OPENCODE_AUTH_DATA" /usr/local/bin/opencode models
```

示例使用默认 CLI 路径，安装在其他位置时用实际绝对路径。将最后一条命令列出的完整模型 ID 填入插件设置。认证文件必须由 AstrBot 运行用户所有并可读，其他用户不得访问。环境页只校验文件权限、JSON 和所选服务商的 API Key 条目。实际密钥有效性、额度及上游响应由首次聊天验证。[OpenCode 认证与模型命令](https://opencode.ai/docs/cli/)

### 读懂环境检查

- **需要处理**给出可操作的原因，例如路径无效、容器不能创建隔离、认证条目缺失或模型不在上次 ACP 目录中。每项还列出其检测依据。
- **未验证**可能表示版本未知、探针超时或尚无本次加载的 ACP 记录。开启沙盒时，须先能创建隔离，才会在其中执行 CLI 版本检查。管理员显式关闭后，沙盒项显示**未启用**。
- **重新检查环境**只运行本地版本和隔离探针并读取配置文件，不登录服务商、不生成消息。自动检查在首次打开页面时运行；30 秒内复用结果，手动刷新最短间隔 2 秒。ACP 状态读取当前实例已有记录；修改设置后须保存并重载本插件，重载会结束本插件的在途请求。

### 会话与模型切换

OpenCode 会话在内存中按 Provider、消息来源、发送者和 AstrBot 会话标识区分。同一会话的请求串行。群聊是否包含其他成员的历史取决于 AstrBot 的群聊配置。历史改变、插件重载或进程重启后，下次请求会从 AstrBot 提供的历史重建会话；没有稳定标识的调用每次新建。

切换 `model_id` 后，原先自动注册的模型保留其 ID 和配置并暂停使用。切回旧模型时，只恢复插件因模型切换而停用的条目。已保存的默认模型和会话选择由 AstrBot 管理，切换后可在目标会话手动选择新模型。

OpenCode 仅使用所选服务商写入专用 `auth.json` 的 API Key。通过外部插件或浏览器 OAuth 登录的账户、代理环境变量、自定义服务商配置、音频与 AstrBot 工具调用均未接入。当前模型列表取自所安装 OpenCode 二进制的内置目录；如果所需模型缺失，先在测试环境核对 CLI 版本。

## 图片输入

OpenCode 1.19.6 通过 ACP 接收图文和多图。插件在首次使用图片时用同一隔离环境下的官方 `opencode models --verbose` 仅读取已选模型的目录项，核对当前模型的图片能力，不额外调用模型。模型不支持时返回 `image_unsupported`，可在设置中用 `image_unsupported_message` 自定义提示。能力无法确认时返回 `image_capability_unverified`。模型 ID 仍由 ACP 校验。

每轮最多 4 张静态 PNG、JPEG 或 WebP，单张最多 2 MiB，合计最多 4 MiB，每张最多 400 万像素。只接收 AstrBot 准备好的内联数据或其临时目录内的普通文件，不直接下载 URL。超限时日志会记录具体限制和已测得的值。发送给 ACP 的只有当前轮附件。会话重建时保留历史文字并标记旧图缺失，已复用会话中的历史仍由 OpenCode 管理。`wire_bytes` 只统计插件发送的 ACP 帧。

## 隔离与统计

运行时创建临时 HOME 和空工作目录，只复制选定服务商的认证条目。OpenCode 使用专用文本 agent、拒绝工具权限，ACP 也拒绝权限请求。应用按固定清单传递环境变量，默认以 bubblewrap 隔离文件和进程视图。插件管理 `opencode acp` 子进程，通过标准输入输出交换 JSON-RPC。CLI 内部 HTTP 服务绑定回环地址并使用每次启动生成的密码。用户无需启动 `opencode serve` 或填写远程 API 地址。CLI 需要网络访问所选服务商。启用隔离时，创建失败会返回错误。

**可选的直接运行模式。** 管理员可在插件配置中关闭「启用 bubblewrap 文件与进程隔离」（`sandbox_enabled`），保存并重载本插件。此时无需安装 bubblewrap。CLI、ACP 会话和图片能力查询使用真实的临时工作目录、独立 HOME 和 XDG 目录，只复制所选服务商密钥。**进程具有 AstrBot 运行用户的文件访问权限。纯聊天 Agent 和禁止工具调用不能替代操作系统隔离。** 环境页在检测到 root 或有效 Linux capabilities 时额外提示风险。建议以低权限用户或受限容器运行。重新开启后，再次保存并重载即可恢复 bubblewrap。

登录 AstrBot WebUI，在 OpenCode 插件详情打开 **用量与运行状态**。页面显示当前模型、CLI 状态、排队和执行中的请求数，也可以查看近 7、30 或 90 天的请求趋势、成功率、耗时、流量和进程／会话复用情况。在「每日趋势」切换请求次数、Token 用量或平均耗时，点选或拖动图表查看某天的数值，也可用方向键切换日期。Token 详情显示当天有计数的请求数。聊天中的管理员命令 `/opencode_usage` 仍可使用，附加 `session`、`day`、`model` 可查看分组。

在**用量排行 → Token 消耗**查看所选时段哪些模型用了最多 Token，也可切到匿名会话或请求结果。图表显示前 5 项，百分比按时段内所有已记录的总 Token 计算。展开前 20 项明细可查看输入、输出、缓存命中，以及每组有计数的请求数。仅有缺失计数的组不参与 Token 排名；记录为 0 的组可上榜，总量为 0 时占比显示为 `—`。切回**请求次数**可对照调用量。

日期按 UTC 划分。每次处理尝试单独记录，包含失败、重试和缓存命中。**Token 用量**读取 OpenCode 1.19.6 写入临时会话库的计数，显示总量、输入、输出和缓存命中，支持按模型、匿名会话和日期查看。输入包含缓存读写，输出包含推理 Token。历史记录、回放和服务商未提供的计数显示为 `—`。插件只保存数字和请求元数据，统计库可沿用。费用和额度以模型服务商的账单为准。

CLI、认证或模型校验失败时，用量页仍会显示历史记录和启动失败原因。启动时的暂时性故障最多尝试 3 次，已有模型的后续请求可在冷却后共用一次校验。认证和模型配置错误需处理后重载。当前聊天失败时提示由 AstrBot 处理后续响应。

使用 `/opencode_source` 可查看许可证和此版本的源码地址。

## 验证与升级

在可以导入 AstrBot 4.29.4 的 Python 环境运行：

```bash
python -m unittest discover -s tests
```

在测试环境安装 Python Playwright 后，设置 `OPENCODE_TEST_CHROMIUM` 为 Chromium 的可执行文件路径，可运行 `python -m unittest tests.test_usage_view -v` 检查图表交互，运行 `python -m unittest tests.test_environment_view -v` 检查首次配置页。

默认测试使用模拟 ACP 对端。下面的可选测试分别在默认沙盒和直接运行模式启动官方 CLI 1.19.6，把上游指向本地模拟模型。它验证两轮文本复用、Token 读取、认证报错，以及真实 CLI 向本地模型替身转发图片字节。还验证纯文本模型在发出上游请求前拒绝图片。

```bash
OPENCODE_TEST_BIN=/usr/local/bin/opencode python -m unittest tests.test_official_cli -v
```

宿主安装与升级集成测试会在独立的 AstrBot 数据目录中安装 ZIP、选择模型、流式对话、取消、终止 CLI，再以新 Python 进程读取已保存的对话。可用 `OPENCODE_TEST_PREVIOUS_ZIP` 指向旧版 ZIP，`OPENCODE_TEST_ZIP` 指向待发布 ZIP。运行 `OPENCODE_TEST_BIN=/usr/local/bin/opencode python -m unittest tests.test_host_install -v`。这两组集成测试使用回环模型服务和测试密钥。实际服务商账号和 QQ 投递需在部署环境中验证。

| 状态或错误码 | 处理方法 |
| --- | --- |
| `cli_missing` / `sandbox_missing` | 在 AstrBot 所在的容器或主机核对 CLI、bubblewrap 的绝对路径和执行权限。 |
| `sandbox_unavailable` | 检查部署环境是否允许 bubblewrap 创建 Linux 用户和进程命名空间。 |
| `not_logged_in` / `auth_permissions` | 用 AstrBot 的系统用户在专用 XDG 目录重新登录对应服务商，核对文件路径和权限后重载。 |
| `model_unavailable` | 核对 `opencode models`、服务商前缀和 CLI 版本后重载。 |
| `session_missing` / `process_exited` | 当前请求已失败。再次发送会从宿主历史重建；流中断前可能已显示部分回复。 |
| `timeout` / `queue_timeout` | 前者已开始执行并进入取消流程，重发前可核对用量。后者在排队阶段到期。 |

要升级到指定版本，在本仓库的 GitHub Release 下载 **Source code (zip)**，在 WebUI 的**插件管理 → ＋ → 从文件安装**上传 ZIP。同名插件会覆盖安装。通过仓库 URL 安装的用户也可在插件卡片的**更多 → 重新安装**获取该仓库默认分支的当前版本。AstrBot 4.29.4 的市场更新提示取决于安装来源，选择来源时请核对仓库所有者 `drshenmiao-hash`。

配置字段、原生模型 ID 和统计库兼容旧版，升级后在用量页确认所选模型。新的会话隔离规则会生成新的匿名统计标签，旧记录仍可查询。回退时可上传旧版 Release ZIP。

会话、图片和校验恢复逻辑改编自 [astrbot_plugin_cursor](https://github.com/drshenmiao-hash/astrbot_plugin_cursor)，原生 Provider 注册与重载流程借鉴 [astrbot_plugin_openai_oauth](https://github.com/wcqqq1214/astrbot_plugin_openai_oauth)。

[AGPL-3.0](LICENSE) · [来源及许可说明](NOTICE) · [OpenCode ACP 文档](https://opencode.ai/docs/acp/)
