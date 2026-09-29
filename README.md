# Bobby Open API Examples

Bobby 开放接口（`https://open.bobby.ai/v1`）的接入示例。目前提供一个端到端自测脚本：用测试 key 把已上线的接口逐项调一遍，检查返回是否符合《接口文档》，帮你在接入前确认 key、网络和各条业务分支都能走通。

接口的完整规格见《接口文档》，请向你的 Bobby 对接人索取。本仓库只是示例，接口行为以文档为准。

## 准备

- **测试 key**：向 Bobby 对接人申请，以 `bk_test_` 开头。测试 key 下的用户、会话与正式数据完全隔离，也不计费。脚本只接受测试 key。
- **Python 3.9+**，依赖只有 [httpx](https://www.python-httpx.org/)。

```bash
git clone https://github.com/RockFlow-AI/bobby-open-api-examples.git
cd bobby-open-api-examples
pip install -r python/requirements.txt
```

API key 只能放在服务端，不要写进代码、提交到仓库，也不要放进 App。

## 运行

```bash
export BOBBY_API_KEY=bk_test_xxx

python python/e2e.py                            # 跑全部用例，约 3～5 分钟
python python/e2e.py --only chat,register -v    # 只跑部分用例，并打印每个请求
python python/e2e.py --list                     # 列出用例及对应的文档章节
```

输出示例：

```text
目标 https://open.bobby.ai  key bk_test_Ab12…

▶ auth  缺 key / 错 key → 401；X-User-Id 缺失或不合法 → invalid_request
  PASS  0.2s

▶ chat  建会话 → 继续对话两轮 → 查询消息；别人的 / 不存在的会话 → 404
  PASS  15.7s
...
通过 11，失败 0，跳过 0
```

全部通过时退出码为 0，有失败时为 1，可以直接接进 CI。

### 参数

| 参数 | 说明 |
|-|-|
| `--only a,b` | 只跑指定用例，用例名见 `--list` |
| `-v` | 打印每个请求的摘要：首字耗时、总耗时、`requestId`、`runId` |
| `--timeout 秒` | 单轮回答的最长等待时间，默认 300 |
| `--reset-trial-user` | 先注销 `test_trial_exhausted` 再测试（见下文「测试用户」） |
| `--keep` | 跑完不注销本次创建的用户，方便事后调「查询消息」查看 |
| `--base-url` | 默认 `https://open.bobby.ai`，也可以用环境变量 `BOBBY_BASE_URL` 指定 |

## 用例

| 用例 | 覆盖的接口与行为 |
|-|-|
| `auth` | 缺少 key、key 错误返回 401 `invalid_api_key`；`X-User-Id` 缺失或不合法返回 `invalid_request` |
| `validation` | `prompt` 为空、全是空白、超过 10000 字，`mode` 非法，请求体不是 JSON，都返回 `invalid_request` |
| `chat` | 创建会话、继续对话、查询消息；访问别人的或不存在的会话返回 404 `session_not_found` |
| `abort` | 回答中再发消息返回 409 `session_busy`；中断回答后流以 `result.aborted` 结束，之后可以立即再发 |
| `register` | 注册的参数校验；首次注册 `created: true`，重复注册 `created: false` |
| `trial_exhausted` | 试用额度用完：403 `trial_exhausted` → 注册 → 重发成功 |
| `entitlement` | 流里的 `entitlement.required` 事件（三种 `reason`），以及查询消息里对应的 `events` |
| `downgrade` | 未订阅用户传 `mode: "bobby_pro"`，降级为 Bobby 回答，并发出 `entitlement.required` |
| `entitlements` | 查询权益：普通用户没有订阅，`test_pro` 有 `bobby_pro` |
| `pro` | 已订阅用户默认走 Bobby Pro，传 `mode: "bobby"` 走 Bobby |
| `delete` | 注销返回 202；删除完成前，会话接口返回 404 `user_not_found` |

脚本只检查文档承诺的行为。响应里多出的字段、流里不认识的事件都会忽略，你们的解析代码也应该这样处理。

## 测试用户

测试 key 下有几个保留的用户 ID，用来联调注册和订阅的分支（详见文档「通用约定」）：

| `X-User-Id` | 行为 |
|-|-|
| 任意没注册过的 ID | 普通试用用户 |
| `test_trial_exhausted` | 注册前会话接口一律返回 403 `trial_exhausted`，注册后放行 |
| `test_subscription_required` | 每轮回答都带 `entitlement.required`，`reason` 为 `subscription_required` |
| `test_quota_exhausted` | 同上，`reason` 为 `quota_exhausted` |
| `test_payment_required` | 同上，`reason` 为 `payment_required` |
| `test_pro` | 视为已订阅 Bobby Pro |

脚本对这些用户的影响：

- 每次运行都会新建一批用户（`e2e_<时间戳>_*`），跑完调「注销」清理掉。
- `test_subscription_required`、`test_pro` 等只用来发几条消息，不改变它们的状态，你们自己的联调可以同时进行。
- `test_trial_exhausted` 注册一次后就不再返回 403。如果它已经注册过（比如你们联调时注册过），`trial_exhausted` 用例会记为 SKIP。加 `--reset-trial-user` 会先注销它再测，这会影响同一把 key 下正在用这个 ID 联调的同事，请先确认。

## 常见问题

**个别用例跑了一两分钟**

回答由大模型生成，偶尔会有单轮等待较久的情况，属于正常现象。超过 `--timeout`（默认 300 秒）才会记为失败。

**`abort` 用例显示 SKIP**

这个用例要在回答输出过程中中断。如果回答在出字前就结束了，或者调中断时恰好已经答完，就没法验证中断，所以记为 SKIP 而不是失败。重跑一次一般就能通过。

**用例失败了怎么办**

失败信息里会带 `requestId`，加 `-v` 能看到每个请求的 `requestId`。把它和失败信息一起发给 Bobby 对接人，我们据此排查。

**返回 401 `invalid_api_key`**

检查 `BOBBY_API_KEY` 是否完整、是否以 `bk_test_` 开头。key 被吊销后最多 60 秒内失效，需要重新申请。

## 在自己的代码里调用

脚本里的 `Api` 类（[`python/e2e.py`](python/e2e.py)）演示了调用方式：设置请求头、解析 SSE 流、按 `error.code` 处理错误，可以作为参考。最简单的调用用 curl 即可：

```bash
curl -N https://open.bobby.ai/v1/sessions \
  -H "Authorization: Bearer $BOBBY_API_KEY" \
  -H "X-User-Id: u_10086" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "特斯拉最近为什么跌？"}'
```

响应是 SSE 流，事件遵循 [AG-UI](https://docs.ag-ui.com/concepts/events) 协议。会话 ID 在响应头 `X-Session-Id` 里，继续对话时用它。
