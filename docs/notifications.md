# 通知 + LLM 解释（M5）

> **定位**：在 M4 的 paper-trading 之上叠了两层 — 一层把每日跑的结果推到飞书 / 企微 / QQ；一层用 LLM（DeepSeek / Claude / OpenAI 兼容）自动生成中文策略解释。**两层都是可选的**，不配置就静默走兜底文本，不影响主流程。

---

## 一分钟接入（推荐：飞书 + DeepSeek）

```bash
# 1) 复制 .env
cp .env.example .env

# 2) 填两个变量
echo 'FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxx-xxx"' >> .env
echo 'DEEPSEEK_API_KEY="sk-xxxxxxxxxxxx"' >> .env

# 3) 测试通知通道是否通
uv run alphaforge notify test --config configs/run/demo_momentum.paper.yaml

# 4) 跑一次 paper + 推送
uv run alphaforge paper run \
    --config configs/run/demo_momentum.paper.yaml \
    --notify

# 5) 启动守护进程（每个交易日 15:30 自动跑 + 推送）
uv run alphaforge paper schedule \
    --config configs/run/demo_momentum.paper.yaml
```

`configs/run/demo_momentum.paper.yaml` 已预填 `notify` + `llm` 两段，使用 `${FEISHU_WEBHOOK}` 和 `${DEEPSEEK_API_KEY}` 占位符，所以 yaml 可以放进 git，秘密只放本地 `.env`。

---

## 一、通知通道（notify）

### 1.1 飞书自定义机器人（推荐）

**为什么推荐**：群里 5 秒钟就能拉起一个；webhook 直接 POST，不需要鉴权服务器；卡片格式好看，标题带颜色。

**步骤**：

1. 飞书桌面端 → 进入要接收推送的群 → 右上角 **设置 → 群机器人 → 添加机器人**。
2. 选 **自定义机器人** → 起个名字（"Alphaforge"）→ 复制 webhook URL。
3. （可选）开启 **签名校验**：勾选后会给一个 secret，更安全，建议开。
4. 把 webhook 写到 `.env`：
   ```bash
   FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx"
   FEISHU_SIGN_SECRET="xxxxxxxx"   # 仅在开了签名校验时
   ```
5. yaml 里：
   ```yaml
   notify:
     type: feishu
     webhook: "${FEISHU_WEBHOOK}"
     secret: "${FEISHU_SIGN_SECRET}"   # 没开签名时整行删掉
   ```

### 1.2 企业微信群机器人

1. 群设置 → 群机器人 → 添加 → 复制 webhook。
2. yaml：
   ```yaml
   notify:
     type: wecom
     webhook: "${WECOM_WEBHOOK}"
   ```

注意：企微单条消息 4096 字节硬限，本模块自动按字节切分多次发。

### 1.3 QQ（OneBot v11）

QQ 没有官方机器人 webhook，需要自建一个 OneBot v11 兼容服务（推荐 [NapCat](https://github.com/NapNeko/NapCatQQ)、[Lagrange.OneBot](https://github.com/LagrangeDev/Lagrange.Core)，老牌 [go-cqhttp](https://github.com/Mrs4s/go-cqhttp) 已停更但仍能跑），起好后开启 HTTP API。

```yaml
notify:
  type: qq
  base_url: "http://127.0.0.1:5700"
  token: "${QQ_ACCESS_TOKEN}"   # OneBot 的 access_token，可空
  target: 123456789             # group_id 或 user_id
  target_kind: group            # group / private
```

### 1.4 多通道同时推

```yaml
notify:
  type: multi
  children:
    - { type: feishu, webhook: "${FEISHU_WEBHOOK}" }
    - { type: wecom,  webhook: "${WECOM_WEBHOOK}" }
```

任一成功即认为成功。

### 1.5 测试通道

```bash
uv run alphaforge notify test --config configs/run/demo_momentum.paper.yaml
```

成功会在群里收到一条 "Alphaforge 通知测试 ✅"。失败看终端日志（多半是 webhook 写错、签名 secret 不匹配、或被频控）。

---

## 二、LLM 策略解释（llm）

每次 `paper run` 跑完，把当天的 buy/sell 列表 + 持仓 + NAV 喂给 LLM，让它写一段 200 字以内的中文解释，附在结果末尾、随通知一起推。

> **强约束**：prompt 明确要求 "只能基于事实写"、"不许编造代码 / 价格"、"不许预测涨跌、不许给投资建议"，调用失败会自动回退到一行兜底文本（`今日（YYYY-MM-DD）有/未调仓：买 X 只 / 卖 Y 只...`），不影响主流程。

### 2.1 DeepSeek（推荐）

- 非常便宜（充 1 块钱够跑几百次），中文质量在线。
- 注册 https://platform.deepseek.com/ → API keys → 创建。

```yaml
llm:
  provider: deepseek
  model: deepseek-chat            # 或 deepseek-reasoner（更慢更贵但推理更好）
  api_key: "${DEEPSEEK_API_KEY}"
  max_tokens: 700
  temperature: 0.3
```

### 2.2 Claude（Anthropic）

```yaml
llm:
  provider: claude
  model: claude-sonnet-4-5
  api_key: "${ANTHROPIC_API_KEY}"
```

### 2.3 OpenAI 兼容（通义千问 / Kimi / 智谱 / Ollama）

只要支持 `chat/completions` 接口的都能塞进来：

```yaml
# 通义千问
llm:
  provider: openai
  model: qwen-plus
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  api_key: "${DASHSCOPE_API_KEY}"

# 本地 Ollama
llm:
  provider: openai
  model: qwen2.5:14b
  base_url: "http://localhost:11434/v1"
  api_key: "ollama"   # 占位即可
```

### 2.4 调试 prompt

不想重新跑策略，只想看最新一次的 LLM 输出长什么样：

```bash
uv run alphaforge paper explain \
    --config configs/run/demo_momentum.paper.yaml \
    --account demo_momentum
```

它从 SQLite 读最新 signals + nav + positions，重新调一次 LLM，**不写库**。便于改 prompt 后快速看效果。

---

## 三、`paper run` 与 `paper schedule` 的差异

| 命令 | 是否推送 | 触发方式 |
|---|---|---|
| `paper run` | 默认不推送，加 `--notify` 才推 | 手动 |
| `paper schedule` | 总是推送（cron 触发即推） | 守护进程 |

实际生产用法：白天人工 `paper run` 调试 → 调通了就跑 `paper schedule` 后台守着。

---

## 四、消息长什么样

通知消息（飞书卡片，蓝色 header）：

```
📈 2026-05-13  demo_momentum  · 调仓 3买/2卖

净值：1,002,345.00　持仓：10
现金：32,100.00　市值：970,245.00

今日撮合：买 0 / 卖 0　现金净流 0.00

待执行（2026-05-14 open）

买入：
- `000001.SZ`  1000 股  @ 12.345
- ...

---
策略解释
今日为月度调仓日，根据 12 月动量截面排序新增买入 3 只、卖出 2 只持仓。
当前组合 10 只票，市值占比约 96.8%，与基准 沪深300 行业暴露接近。
```

错误时（红色 header，`level=error`）：

```
❌ Paper run failed @ 2026-05-13
`RuntimeError: No daily data for 2026-05-13. ...`
请到日志文件查看完整堆栈。
```

---

## 五、安全建议

1. **永远不要把 webhook 或 api_key 直接写进 yaml** — yaml 进 git，密钥泄漏。用 `${VAR}` + `.env`。
2. 飞书机器人尽量开 **签名校验**，否则任何人拿到 webhook URL 都能往群里发消息。
3. LLM 发出的 payload 里**不包含**任何 PII（账户名也是用户在 yaml 里自定义的别名，无身份信息）。
