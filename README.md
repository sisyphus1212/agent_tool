# agent_tool

一个用于整理 Hermes 本地会话历史查看脚本的小仓库。

## 当前内容

- `bin/hhist`：读取 Hermes `state.db` 的命令行工具，可用于：
  - 按根会话分组列出历史会话
  - 展示完整会话内容
  - 搜索消息内容
  - 导出会话为 JSON
  - 查看数据库 schema

## 环境要求

- Python 3.8+
- 本机存在 Hermes 数据库，默认路径：
  - `~/.hermes/state.db`
- 可选：`less`，用于分页查看长输出

## 使用方法

先给脚本执行权限（已经在仓库内设置）：

```bash
chmod +x bin/hhist
```

直接运行：

```bash
./bin/hhist --list
```

或：

```bash
python3 ./bin/hhist --list
```

## 常用命令

列出最近会话：

```bash
./bin/hhist --list
./bin/hhist list --limit 20
```

查看某个完整会话：

```bash
./bin/hhist show <session_id>
```

兼容旧写法：

```bash
./bin/hhist <session_id>
```

搜索消息：

```bash
./bin/hhist search 关键词
./bin/hhist search --session <session_id> 关键词
```

导出 JSON：

```bash
./bin/hhist dump <session_id>
```

查看 schema：

```bash
./bin/hhist --schema
```

指定数据库路径：

```bash
./bin/hhist --db /path/to/state.db --list
```

## 环境变量

- `HERMES_HOME`：默认会从 `$HERMES_HOME/state.db` 读取数据库
- `HHIST_LIMIT`：默认列表条数
- `HHIST_SEARCH_LIMIT`：默认搜索返回条数

## 仓库结构

```text
agent_tool/
├── bin/
│   └── hhist
├── .gitignore
└── README.md
```

## 快速验证

```bash
python3 -m py_compile ./bin/hhist
./bin/hhist --list | head
```
