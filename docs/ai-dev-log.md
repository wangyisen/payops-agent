# AI 开发日志

这个项目主要由 AI 编程助手（Claude Code）驱动开发。这里如实记录 AI 犯过的错：错在哪里、怎么发现的、怎么修的、以后怎么防。目的是展示“用 AI 做开发”时，人需要在哪些地方把关。

## 记录格式

```text
### YYYY-MM-DD · 里程碑 · 一句话标题
- 现象：
- 怎么发现的：测试 / 类型检查 / lint / code review / 运行时 / 人工阅读
- 根因：
- 怎么修的：（附 commit）
- 以后怎么防：加测试 / 改 CLAUDE.md 规则 / 改提示词 / 加 CI 检查
```

## 记录

### 2026-11-01 · M0 · AI 记忆里的“最新版本”早就过时了

- **现象**：AI 写的初稿里，CI 用的是 `actions/checkout@v4`、`astral-sh/setup-uv@v6`、`gitleaks/gitleaks-action@v2`；设计 M1 时默认 DeepSeek 的模型叫 `deepseek-chat` / `deepseek-reasoner`；以为 Starlette 的 `TestClient` 依赖 `httpx`。
- **怎么发现的**：
  - 用 `gh api repos/<owner>/<repo>/releases/latest` 查到实际最新版本是 checkout v7、setup-uv v10、gitleaks-action v3；
  - 让一个子 agent 去读 DeepSeek 官方文档，发现旧模型名已于 2026-07-24 下线，新模型是 `deepseek-flash` / `deepseek-v4-pro`，而且**思考模式默认开启**；
  - 测试输出里出现 `StarletteDeprecationWarning`，提示改用 `httpx2`。
- **根因**：模型的训练数据有截止日期，它“记得”的版本号和 API 细节会落后于现实，而且它不会主动意识到这一点。
- **怎么修的**：CI 升级到当前版本；M1 改用 `deepseek-flash`，并且每次请求都显式关闭思考模式；开发依赖换成 `httpx2`。
- **以后怎么防**：CLAUDE.md 加了一条规则：第三方版本和外部 API 的细节必须先查当前文档或最新 release，不能凭记忆；能用一次真实调用验证的（比如列出模型），就去验证。
