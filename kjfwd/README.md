# 柯基服务队群聊答疑 Bot

该服务复用 wx4py 已有的群监听和回复通道。它不会回滚聊天记录、读取群成员、探测发送者、截图识别或访问微信数据文件；SQLite 中只有服务启动后由监听接口收到的增量消息。

## 配置

1. 将 `config.example.json` 复制为 `config.json`。
2. 配置所有需要监听的群名和机器人在该群中的昵称。显式昵称可避免程序进入群详情自动读取。
3. 在项目根目录 `.env` 中设置：

```dotenv
API_KEY=...
BASE_URL=https://example.com/v1
MODEL=model-name
BRAVE_KEY=...
```

每个监听群支持以下配置：

- `listen_mode`：
  - `mention_only`：默认，只在被 @ 时回复。
  - `all_messages`：监听到该群每条消息都回复。
  - `question_only`：每条消息先交给轻量 LLM 判断是否是电脑维修/软硬件使用相关求助，是则回复。
- `reply_groups`：回复投递到哪些群。默认回复到来源群；也可以把多个答疑群的问题集中回复到一个或多个机器人参考群。会话和历史仍按来源监听群隔离，机器人参考群只作为输出目的地。

当 `reply_groups` 中的目标群不是来源群时，回复前会自动加上 `[来源群：群名]`，方便真人判断问题来自哪里。

`reply_groups` 中的机器人参考群不需要再写进 `groups`；启动时程序会自动把它们加入 wx4py 的群窗口管理，用于发送。handler 会忽略这些输出群的普通消息，避免参考群内容反向污染答疑群上下文。

四个答疑群使用 `question_only` 并统一转发到一个机器人参考群的模板见 `config.question-only-forward.example.json`。


## Skills

向 `skills/` 放入 UTF-8 Markdown 文件即可增加 skill，文件名是不含扩展名的 skill 名。所有 skill 都会写入 system prompt：

- `@柯基服务队 /skill名 问题`：显式指定。
- 普通自然语言问题：模型分析后主动采用相关 skill。

本期 skill 只提供知识和回复规范。`Capability` 抽象保留了未来接入联网查询等工具的边界，但当前不会执行任何外部动作。

内置彩蛋：`@柯基服务队 /neko 问题` 会让本次回答使用轻微猫娘语气；这只改变表达风格，不改变安全规则和服务边界。

## 启动

先在项目虚拟环境中安装本项目，然后运行：

```powershell
.\.venv\Scripts\python.exe kjfwd\app.py --config kjfwd\config.json
```

## 历史与重复回复

- 群内每条可见增量消息都会写入 SQLite。
- 相邻消息超过 30 分钟时开启新会话。
- 触发模型时会先做隐式 conversation 路由。明确承接某个已有问题时，只使用该 conversation 的历史；明显是新问题时新建 conversation；无法判定时使用最近 1 小时全局群聊历史作为 fallback，并提示模型这些历史可能包含多组交错话题。
- 默认 debug 会在回复开头显示 `[conv: xxxxxxxx]` 或 `[conv: ambiguous]`，便于观察路由效果。稳定后可在 `debug.conversation_id_in_reply` 中关闭。
- 普通未 @ 消息只进入全局群聊历史，不会自动污染任何确定 conversation；ambiguous fallback 的消息和回复也不会并入确定 conversation。
- 触发模型时冻结当时的上下文，默认最多 100 条、16,000 字符。
- 每条 @ 都会被原子认领；同一消息被监听层紧邻重复投递时不会再次请求模型。UIA RuntimeId 只作为当前事件的特征，不会被当成永久唯一 ID，因为微信刷新虚拟列表后可能复用它。
- `triggers.sent=1` 表示回复已交给 wx4py 的串行发送队列。微信 UIA 没有最终送达回执，因此它不等同于对端确认收到。
- 对内容相同的 @ 使用默认 1 秒短窗口辅助去重；在该窗口内故意连续发送完全相同的两条 @，仍可能被视为重复。可通过 `trigger_dedupe_seconds` 调整。

当前未实现发送者识别和成员白名单，任何人 @ 机器人都可以触发。

当微信把位于消息中间的 @ 渲染成多个富文本子控件时，服务会对当前消息控件执行受限的只读 UIA 回退检查。该检查最多读取3层、64个节点，不点击、不滚动、不聚焦，也不访问其他窗口。

发送 `@机器人昵称 /clear` 或 `@机器人昵称 /new` 会忽略该群此前的聊天上下文并开始新会话。命令后可以直接接新问题，例如 `@机器人昵称 /new 新会话的问题`；重置时，此前仍在排队或生成中的旧回复会被丢弃。

发送 `@机器人昵称 /help`，或询问“如何使用你”“你有哪些指令”，会直接返回 Bot 介绍、核心命令和当前加载的 skill 指令列表。该帮助响应不调用 LLM；新增 skill 并重启后，列表会自动更新。

若微信把 mention 显示为 `@机器人昵称@微信 /指令`，服务会定向清理紧跟机器人昵称的 `@微信` 残留后再解析命令。为避免引用或否定句误触发 `/clear`、`/new` 等命令，核心命令仍须位于清理后的消息开头。

联网搜索默认启用。模型会在核对特定硬件参数、官方文档、错误码和可能变化的信息时主动调用 Brave Search。`@机器人昵称 /search 问题` 会强制本次回答至少搜索一次；`/search` 只是开关，不会作为 skill 或回答内容传给模型。

为避免模型因“记得这个型号”而跳过核验，当前问题中出现可识别的具体硬件或软件型号时，代码会直接要求搜索。每次请求还会把当天日期加入 system prompt，防止“最新”查询被错误限定到旧年份。

使用搜索结果的回答会经过一次基于原始网页片段的事实和风格复核。最终来源标题与 URL 由代码直接从 Brave 响应附加，不依赖模型自行生成；普通回答默认控制在约 700 字以内，`/explain` 可放宽到约 2500 字。

对于具体型号的清灰、换硅脂、拆机、散热维护或硬件升级问题，服务会强制搜索该型号的导热材料和特殊结构，并先拆解实际维修操作，再判断是否属于科服能够安全提供的服务。中文别名加数字的型号（例如“幻14”）也会被识别。

## 测试

单元测试不连接微信：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s kjfwd\tests -v
```

可选的真实 LLM 冒烟测试会读取根目录 `.env`，仍然不会连接微信：

```powershell
$env:KJFWD_RUN_LLM_TEST='1'
.\.venv\Scripts\python.exe -m unittest kjfwd.tests.test_llm_integration -v
```

真实 Brave 搜索及完整 Agent tool call 测试同样不会连接微信：

```powershell
$env:KJFWD_RUN_SEARCH_TEST='1'
.\.venv\Scripts\python.exe -m unittest kjfwd.tests.test_search_integration -v

$env:KJFWD_RUN_AGENT_TEST='1'
.\.venv\Scripts\python.exe -m unittest kjfwd.tests.test_agent_integration -v
```

DeepSeek 当前不允许在思考模式下指定或强制工具，因此工具调用轮会显式关闭思考模式；普通无工具回答不受影响。
