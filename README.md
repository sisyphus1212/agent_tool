# agent_tool

一个可安装的 Python CLI 项目，用于管理 Hermes 本地会话历史。

安装后提供命令：

```bash
hhist
```

## 功能

- 按根会话分组列出历史会话
- 查看完整会话内容
- 搜索消息内容
- 导出会话为 JSON
- 归档 / 恢复会话
- 软删除会话
- 硬删除会话（调用 `hermes sessions delete`）
- 使用 sidecar 元数据库 `~/.hermes/hhist.db`

## 数据设计

- Hermes 原始数据库：`~/.hermes/state.db`
- hhist 元数据库：`~/.hermes/hhist.db`

原则：

- `state.db` 是 canonical history store
- 管理状态写入 `hhist.db`
- `delete` 是软删除
- `delete --hard` 才会物理删除 Hermes 会话

## 项目结构

```text
agent_tool/
├── bin/
│   └── hhist
├── src/
│   └── agent_tool/
│       ├── __init__.py
│       └── cli.py
├── .gitignore
├── LICENSE
├── pyproject.toml
├── README.md
└── hhist
```

## 开发安装

在仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

安装后可直接运行：

```bash
hhist --list
```

也可以直接运行包装脚本：

```bash
./bin/hhist --list
```

## 常用命令

```bash
hhist --list
hhist list --limit 20
hhist list --all
hhist list --active
hhist list --archived
hhist list --deleted

hhist show <session_id>
hhist search 关键词
hhist search --session <session_id> 关键词
hhist dump <session_id>

hhist archive <session_id>
hhist archive <session_id> --group
hhist restore <session_id>
hhist delete <session_id>
hhist delete <session_id> --hard
```

兼容写法：

```bash
hhist -l
hhist <session_id>
hhist --search keyword
```

## 环境要求

- Python 3.8+
- Hermes 数据库：`~/.hermes/state.db`
- 可选：`less`
- 可选：`hermes` CLI（`delete --hard` 时需要）

## 环境变量

- `HERMES_HOME`
- `HHIST_META_DB`
- `HHIST_LIMIT`
- `HHIST_SEARCH_LIMIT`

## 打包构建

```bash
python3 -m build
```

## 快速验证

```bash
python3 -m py_compile src/agent_tool/cli.py
./bin/hhist --list | head
```
