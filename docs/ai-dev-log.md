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

### 2026-11-01 · M0 · CI 第一次运行，两项检查都失败

- **现象**：PR #1 的 `python` 和 `secrets` 两个任务都失败了。
  1. `python`：`Unable to resolve action astral-sh/setup-uv@v10`。上一条记录里 AI 查到了最新版本是 v10.3.3，就想当然地写成 `@v10`，但这个仓库从 v8 起只发布完整版本号的标签，**没有 `v10` 这种浮动的大版本标签**。
  2. `secrets`：gitleaks 在两个 README 里发现了 `curl -H "Authorization: Bearer dev-mock-pay-key"`，按“curl 请求头里的令牌”规则报警。
- **怎么发现的**：CI 日志（`gh run view --log-failed`）。
- **根因**：
  1. 查证只做了一半：查了“最新版本是多少”，没查“这个标签是否存在”，套用了其他 action 的惯例。
  2. 这个值是文档里公开的本地开发 key，不是真密钥；扫描器是按格式判断的，它报警是正常行为。
- **怎么修的**：
  1. 所有第三方 action 改为**固定到 commit SHA**，并在注释里写明版本（`@<sha> # v10.3.0`）。标签可以被改指向，SHA 不能，这也是防供应链攻击的通行做法。版本只选发布满两周的。
  2. 新增 `.gitleaks.toml`，在默认规则的基础上只放行 `dev-mock-pay-key` 这一个精确值；工作流里显式指定配置文件路径和 gitleaks 版本，不依赖 action 内置的默认版本。
- **以后怎么防**：引用 action 一律用 SHA 固定；密钥扫描的例外只能是精确值，并在配置里写明原因。
