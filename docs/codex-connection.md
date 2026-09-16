# Codex 订阅连接

在模型页的“添加其他连接”中选择 **Codex · ChatGPT 订阅登录**：

1. 点击“登录 ChatGPT”，取得设备授权码。
2. 点击“打开授权页面”，由用户在 OpenAI 页面登录并输入代码。若设备码登录未启用，按 OpenAI 页面提示调整 ChatGPT 安全设置。
3. 回到 Kaze，授权完成后自动读取该账号的可用模型。
4. 选择模型和推理强度，保存连接；随后可在聊天模型选择器中切换。

没有固定预填的 Codex 型号；模型列表以账号返回结果为准。添加连接不替换现有默认模型。取消授权窗口会停止该次登录轮询；已完成授权的凭据仍保留。

## 持久化与调用

- 配置中保存普通模型注册信息：UUID、`provider = "codex"`、模型名、推理强度；`api_key` 和 `base_url` 为空。
- OAuth 凭据按连接 UUID 独立保存于当前 workspace 的 `.desktop/codex-auth.bin`。Windows 使用当前用户的 DPAPI 加密，通过文件锁和原子替换处理刷新。
- 不读取或改写用户的 Codex App/CLI 登录文件，不把 token 传给 renderer；模型请求固定发送到 ChatGPT Codex 服务，不接受自定义认证目标。
- Codex 使用 Responses 流式通道，普通 API 连接继续使用原有 Provider。工具调用、工具结果及模型返回的 continuation 状态通过现有 Agent 接口衔接。
- 移除模型配置不会注销账号或删除已经保存的凭据；复制 workspace 到其他 Windows 用户后需要重新授权。

## 规范与验证

认证、模型目录及 Responses 流式转换遵循 OpenAI 官方规范与 Responses API 标准；设备码登录说明参见 [OpenAI 官方认证文档](https://learn.chatgpt.com/docs/auth)。该连接基于官方 ChatGPT 订阅账号的设备授权流程，与标准 API Key 模式相互独立。

自动测试使用模拟认证和 SSE 事件，覆盖凭据隔离、Windows 加密、取消登录、失败状态、模型选择、工具调用和断流。界面使用模拟 RPC 验证授权码、模型选择及保存流程。真实账号授权和远程推理尚需用户登录后验证；开发验证不读取用户 token，不调用付费模型。
