# agent_tool

一个用于管理 Hermes 本地会话历史的命令行工具仓库。

当前仓库主要提供 `hhist`，用于读取 Hermes `state.db`，并通过 sidecar 元数据库实现归档、恢复、软删除等管理能力。

## 当前内容

- `bin/hhist`：Hermes SQLite history manager
- `.gitignore`
- `README.md`

## 功能概览

`hhist` 支持：

- 按根会话分组列出历史会话
- 查看完整会话内容
- 搜索消息内容
- 导出会话为 JSON
- 归档会话
- 恢复会话
- 软删除会话
- 硬删除会话（调用官方 `hermes sessions delete`）
- 维护本地 sidecar 元数据库 `~/.hermes/hhist.db`

## 数据设计

- Hermes 原始数据：`~/.hermes/state.db`
- hhist 元数据：`~/.hermes/hhist.db`

原则：

- `state.db` 是 canonical history store
- 归档 / 删除 / 标签 / note 等管理信息写入 `hhist.db`
- 普通 `delete` 是软删除，只影响 `hhist.db`
- `delete --hard` 才会调用官方命令对 Hermes 会话做物理删除

## 环境要求

- Python 3.8+
- 本机存在 Hermes 数据库：`~/.hermes/state.db`
- 可选：`less`
- 可选：`hermes` CLI（执行 `delete --hard` 时需要）

## 使用方法

直接运行：

```bash
./bin/hhist --list
```

或：

```bash
python3 ./bin/hhist --list
```

## 兼容写法

```bash
./bin/hhist -l
./bin/hhist <session_id>
./bin/hhist --search keyword
```

## 主要命令

列出会话：

```bash
./bin/hhist --list
./bin/hhist list
./bin/hhist list --limit 20
./bin/hhist list --all
./bin/hhist list --active
./bin/hhist list --archived
./bin/hhist list --deleted
```

查看完整会话：

```bash
./bin/hhist show <session_id>
```

搜索：

```bash
./bin/hhist search 关键词
./bin/hhist search --session <session_id> 关键词
./bin/hhist --search 关键词
```

导出 JSON：

```bash
./bin/hhist dump <session_id>
```

归档 / 恢复：

```bash
./bin/hhist archive <session_id>
./bin/hhist archive <session_id> --group
./bin/hhist archive <session_id> --children

./bin/hhist restore <session_id>
./bin/hhist restore <session_id> --group
./bin/hhist restore <session_id> --children
```

删除：

```bash
./bin/hhist delete <session_id>
./bin/hhist delete <session_id> --group
./bin/hhist delete <session_id> --children
```

硬删除：

```bash
./bin/hhist delete <session_id> --hard
./bin/hhist delete <session_id> --hard --group
./bin/hhist delete <session_id> --hard --children
```

## 环境变量

- `HERMES_HOME`：默认从 `$HERMES_HOME/state.db` 读取
- `HHIST_META_DB`：指定元数据库路径
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
./bin/hhist search hermes | head
```
