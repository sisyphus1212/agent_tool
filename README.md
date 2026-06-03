# agent_tool

尽可能简单的版本：

- 一个主脚本：`hhist`
- 一个安装脚本：`install.sh`
- 两个 MCP 查询脚本：`jira_mcp_search.sh`、`confluence_mcp_search.sh`

这样你可以直接拷贝 `hhist` 到别的机器使用；如果想安装到本机 PATH，就运行 `install.sh`。MCP 脚本直接运行即可。

## 文件

| 文件 | 说明 |
|------|------|
| `hhist` | 主脚本：Hermes 会话历史管理（list/show/search/archive/delete） |
| `install.sh` | 安装脚本：将 `hhist` 安装到 `~/.local/bin/` |
| `jira_mcp_search.sh` | JIRA MCP 查询脚本：通过流式 MCP 端点执行 JQL 搜索 |
| `confluence_mcp_search.sh` | Confluence MCP 查询脚本：通过流式 MCP 端点执行页面搜索 |
| `.gitignore` | Git 忽略规则 |
| `README.md` | 项目说明文档 |

## 使用方式

### hhist

直接运行单文件：

```bash
python3 hhist --list
```

或者给执行权限后直接运行：

```bash
chmod +x hhist
./hhist --list
```

### 安装

执行：

```bash
./install.sh
```

默认会安装到：

```bash
~/.local/bin/hhist
```

如果 `~/.local/bin` 不在 PATH，加入：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

### JIRA MCP 查询

```bash
./jira_mcp_search.sh '<TOKEN>' 'key = COR-17556'
```

- 参数 1：JIRA Bearer Token
- 参数 2（可选）：JQL 查询语句，默认为 `key = COR-17556`

### Confluence MCP 查询

```bash
./confluence_mcp_search.sh '<TOKEN>' 'AI各团队key'
```

- 参数 1：Confluence Bearer Token
- 参数 2（可选）：搜索关键词，默认为 `AI各团队key`

两个脚本的 MCP 端点均为 `http://10.1.88.121:8087/mcp`。

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

## 依赖

只需要：

- Python 3
- 本机有 `~/.hermes/state.db`
- 可选：`hermes` CLI（仅 `delete --hard` 时需要）

## 数据说明

- Hermes 原始数据库：`~/.hermes/state.db`
- hhist 元数据库：`~/.hermes/hhist.db`

原则：

- `state.db` 是原始历史数据
- `hhist.db` 保存 archive / delete 等管理状态
- 普通 `delete` 是软删除
- `delete --hard` 才会真正调用 Hermes 删除会话

## 快速验证

```bash
python3 -m py_compile hhist
./hhist --list | head
```
