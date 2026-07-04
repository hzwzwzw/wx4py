# 柯基服务队群聊答疑 Bot

该服务复用 wx4py 已有的群监听和回复通道。它不会回滚聊天记录、读取群成员、探测发送者、截图识别或访问微信数据文件；SQLite 中只有服务启动后由监听接口收到的增量消息。

## 配置

1. 将 `config.example.json` 复制为 `config.json`。
2. 配置所有群的群名和机器人在该群中的昵称。显式昵称可避免程序进入群详情自动读取。
3. 在项目根目录 `.env` 中设置：

```dotenv
API_KEY=...
BASE_URL=https://example.com/v1
MODEL=model-name
```


## Skills

向 `skills/` 放入 UTF-8 Markdown 文件即可增加 skill，文件名是不含扩展名的 skill 名。所有 skill 都会写入 system prompt：

- `@柯基服务队 /skill名 问题`：显式指定。
- 普通自然语言问题：模型分析后主动采用相关 skill。

本期 skill 只提供知识和回复规范。`Capability` 抽象保留了未来接入联网查询等工具的边界，但当前不会执行任何外部动作。

## 启动

先在项目虚拟环境中安装本项目，然后运行：

```powershell
.\.venv\Scripts\python.exe kjfwd\app.py --config kjfwd\config.json
```

## 历史与重复回复

- 群内每条可见增量消息都会写入 SQLite。
- 相邻消息超过 30 分钟时开启新会话。
- 触发模型时冻结当时的上下文，默认最多 100 条、16,000 字符。
- 每条 @ 使用 UIA RuntimeId 生成稳定键并原子认领；同一消息被监听层重复投递时不会再次请求模型。
- `triggers.sent=1` 表示回复已交给 wx4py 的串行发送队列。微信 UIA 没有最终送达回执，因此它不等同于对端确认收到。
- RuntimeId 不可用时，会按群名、内容和短时间窗口降级去重；短时间内完全相同的两条 @ 可能被视为重复。

当前未实现发送者识别和成员白名单，任何人 @ 机器人都可以触发。

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
