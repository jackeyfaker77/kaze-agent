# Codex 订阅连接

在模型页的“添加其他连接”中选择 **Codex · ChatGPT 订阅登录**：

1. 点击“登录 ChatGPT”，Kaze 自动打开系统浏览器中的 OpenAI 登录页面。
2. 在 OpenAI 页面完成账号登录和授权，浏览器会自动回调 Kaze；无需输入设备授权码，也无需开启设备码登录。
3. 回到 Kaze，连接状态自动更新，并读取该账号的可用模型。浏览器没有打开时可以点击“重新打开登录页面”。
4. 选择模型和推理强度，保存连接；随后可在聊天模型选择器中切换。

没有固定预填的 Codex 型号；模型列表以账号返回结果为准。添加连接不替换现有默认模型。“取消登录”或关闭连接窗口会停止等待本次授权；已完成授权的凭据仍保留。登录等待最多 15 分钟，超时后可以重新开始。

## 浏览器回调

- 后端在打开浏览器前监听 `127.0.0.1:1455`，授权回调地址为 `http://localhost:1455/auth/callback`。监听器仅在有登录请求等待时保留。
- 每次登录使用独立的随机 `state`、OIDC `nonce` 和 PKCE S256；回调必须匹配当前登录请求，授权结果只能消费一次。多连接登录按 `state` 独立路由。
- PKCE verifier 和授权码只保留在后端内存，renderer 仅接收登录地址和状态；浏览器响应不包含凭据或回调参数。
- 本地端口被其他程序占用时会明确报错，不接管已有监听器。请先关闭其他正在登录的 Codex 客户端，再重试。

## 持久化与调用

- 配置中保存普通模型注册信息：UUID、`provider = "codex"`、模型名、推理强度；`api_key` 和 `base_url` 为空。
- OAuth 凭据按连接 UUID 独立保存于当前 workspace 的 `.desktop/codex-auth.bin`。Windows 使用当前用户的 DPAPI 加密，通过文件锁和原子替换处理刷新。
- 不读取或改写用户的 Codex App/CLI 登录文件，不把 token 传给 renderer；模型请求固定发送到 ChatGPT Codex 服务，不接受自定义认证目标。
- Codex 使用 Responses 流式通道，普通 API 连接继续使用原有 Provider。工具调用、工具结果及模型返回的 continuation 状态通过现有 Agent 接口衔接。
- 移除模型配置不会注销账号或删除已经保存的凭据；复制 workspace 到其他 Windows 用户后需要重新授权。

## 规范与验证

浏览器登录流程参考 [OpenAI 官方认证文档](https://learn.chatgpt.com/docs/auth)。Kaze 使用 ChatGPT 订阅账号的浏览器 OAuth 授权和 Codex Responses 流式接口，与标准 API Key 连接分别配置。

自动测试使用本机 HTTP 回调、模拟 token 交换和 SSE 事件，覆盖 PKCE、回调校验、重复回调、同连接并发登录、凭据隔离、Windows 加密、取消登录、超时、端口占用、失败状态、工具调用和断流。浏览器打开逻辑验证官方登录地址和失败处理。真实账号授权和远程推理尚需用户登录后验证；开发验证不读取用户 token，不调用付费模型。
