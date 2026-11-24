"""Typed, fixed errors for routing and diagnostics; upstream payloads stay private."""

from astrbot.api import logger

MESSAGES = {
    "usage_unavailable": "本地统计库暂不可用。",
    "auth_permissions": "请检查 OpenCode 专用 auth.json，文件必须可读且仅所有者可访问（0600）。",
    "registration_save_failed": "OpenCode 来源或模型配置未能保存，请检查 AstrBot 配置写入权限并重载插件。",
    "source_id_conflict": "opencode_cli 来源 ID 已被其他提供商占用；未覆盖已有配置，请处理冲突后重载。",
    "config": "OpenCode 配置无效，请检查插件配置。",
    "model": "请明确配置同一个 OpenCode 模型 ID；不支持 Auto 或默认路由。",
    "model_unavailable": "指定模型不在 OpenCode 返回的模型列表中；没有自动更换模型。",
    "model_not_validated": "OpenCode model_id 尚未通过 ACP 校验，请先完成登录并重载插件。",
    "model_mismatch": "OpenCode 未确认指定模型，已停止请求。",
    "connection_stalled": "OpenCode 生成连接停滞；请稍后重试或使用备用模型。",
    "ping_timeout": "OpenCode 生成连接心跳超时；请稍后重试或使用备用模型。",
    "upstream_retryable": "OpenCode 生成流暂时失败；请稍后重试或使用备用模型。",
    "chat_mode_required": "OpenCode 未确认纯聊天模式，已停止请求。",
    "image_unsupported": "当前 OpenCode 模型或运行端不支持图片，请更换支持图片的模型。",
    "image_source": "图片不可读取。请重新发送图片。",
    "image_capability_unverified": "无法确认所选 OpenCode 模型支持图片，本次未发送。请检查 CLI 及模型配置。",
    "image_unavailable": "OpenCode 或模型服务商无法读取本轮图片，请重新发送。",
    "image_invalid": "图片格式无效或不受支持，仅支持静态 PNG、JPEG 和 WebP。",
    "image_limit": "本轮图片超过数量、字节或像素限制。",
    "unsupported": "OpenCode 当前适配器不支持此输入能力。",
    "context_limit": "完整上下文超过 context_max_bytes，已报错；不会截断或悄悄丢弃历史。",
    "timeout": "OpenCode 请求超时，已发出取消；请检查诊断记录和用量后再决定是否重试。",
    "queue_timeout": "请求排队超时，本次没有开始模型调用；请稍后重试。",
    "startup_timeout": "OpenCode 进程或登录握手超时，尚未开始模型调用。",
    "not_logged_in": "请用 OpenCode 专用配置为该服务商保存 API Key，再重载插件。",
    "quota_exhausted": "OpenCode 额度不足；本次不自动重试，请检查套餐用量。",
    "rate_limited": "OpenCode 请求受到限流；本次不自动重试。",
    "cli_failed": "OpenCode 调用失败；原始输出已隐藏，请查看 AstrBot 日志。",
    "protocol": "OpenCode ACP 返回格式不兼容，请检查 CLI 版本。",
    "output_limit": "OpenCode 输出超过安全限制，已发出取消请求。",
    "unsupported_output": "OpenCode 返回了不支持的输出类型。",
    "empty_input": "没有可发送的文本上下文。",
    "empty_output": "OpenCode 没有返回文本回复。",
    "transport": "OpenCode 无法启动，请检查 CLI 路径和隔离环境。",
    "tool_blocked": "OpenCode 纯聊天模式已阻止工具调用。",
    "session_missing": "OpenCode 会话已失效。重新发送即可使用 AstrBot 历史重建会话。",
    "process_exited": "OpenCode 常驻进程退出；本次不会自动重试。",
    "cancelled": "OpenCode 请求已取消。",
    "cancel_unconfirmed": "远端取消未确认，已关闭连接；请核对用量。",
    "closed": "OpenCode Provider 已停止。",
    "cleanup_timeout": "OpenCode 进程清理未在期限内完成。",
    "cli_missing": "未找到官方 OpenCode CLI。",
    "sandbox_missing": "本地模式缺少 bubblewrap；未降级为无隔离执行。",
    "sandbox_unavailable": "本地文件隔离不可用，请检查内核或容器权限。",
    "sandbox_path_conflict": "AstrBot 数据位于本地沙盒允许运行库路径中，请使用专用运行器。",
}


class OpenCodeError(RuntimeError):
    retryable = False

    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in MESSAGES else "cli_failed"
        super().__init__(f"[opencode:{self.code}] {MESSAGES[self.code]}")


class UnsupportedFeatureError(OpenCodeError):
    """Stable capability failure; upstream can select a supported provider."""

    def __init__(self, feature):
        self.feature = (
            feature if feature in {"image", "audio", "tool_calls", "content"} else "content"
        )
        self.code = "unsupported_" + self.feature
        RuntimeError.__init__(
            self,
            f"[opencode:{self.code}] 当前 OpenCode ACP 适配器不支持 {self.feature}，请使用支持该能力的模型。",
        )


class ImageUnsupportedError(OpenCodeError):
    def __init__(self, message):
        self.code = "image_unsupported"
        RuntimeError.__init__(self, f"[opencode:{self.code}] {message}")


class ImageLimitError(OpenCodeError):
    def __init__(self, kind, limit, actual=None):
        self.code = "image_limit"
        self.metadata = {"image_limit_kind": kind, "image_limit_max": limit}
        if actual is not None:
            self.metadata["image_limit_actual"] = actual
        label = {
            "count": "张数",
            "file_bytes": "单张字节数",
            "total_bytes": "总字节数",
            "pixels": "像素数",
            "frame_bytes": "ACP 帧字节数",
        }[kind]
        measured = f"，实际 {actual}" if actual is not None else ""
        RuntimeError.__init__(
            self,
            f"[opencode:image_limit] 本轮图片{label}超限（上限 {limit}{measured}），本次请求未发送。",
        )


class OpenCodeStreamError(OpenCodeError):
    retryable = True


async def retry_stream_errors(operation, *, request_max_retries=None):
    """At most one retry of a detected stream error; other failures propagate."""
    retries = 0 if isinstance(request_max_retries, int) and request_max_retries <= 1 else 1
    for attempt in range(retries + 1):
        try:
            return await operation()
        except OpenCodeStreamError as exc:
            logger.warning(
                "opencode_stream_failure code=%s attempt=%d max_attempts=%d retryable=true",
                exc.code,
                attempt + 1,
                retries + 1,
            )
            if attempt == retries:
                raise
