# Tavily API Proxy

将多个 Tavily API Key 池化，对外暴露统一端点和 Token，附带 Web 管理控制台。

## 功能

- **Key 池化轮询**：Round-robin 分配请求到多个 API Key，连续失败 3 次自动禁用
- **Token 管理**：创建多个访问 Token，每个 Token 独立配额（小时/日/月）
- **真实额度同步**：后台通过 Tavily 官方 `/usage` 接口同步每个 Key 的真实已用 / 总额度 / 剩余额度
- **用量统计**：同时保留代理侧成功/失败次数、延迟和 Token 配额使用情况
- **Web 控制台**：可视化管理 Key、Token 和用量
- **批量导入**：支持从 `api_keys.md` 格式文本批量导入 Key
- **兼容 Tavily 官方 API**：客户端只需改 base URL 即可

## 快速开始

### Docker 部署（推荐）

```bash
cd proxy/

# 修改管理密码
cp .env.example .env
# 编辑 .env 中的 ADMIN_PASSWORD

# 启动
docker compose up -d
```

服务运行在 `http://localhost:9874`。

### 本地运行

```bash
cd proxy/
pip install -r requirements.txt
ADMIN_PASSWORD=your-password uvicorn server:app --host 0.0.0.0 --port 9874
```

## 使用流程

1. 访问 `http://localhost:9874/`，输入管理密码登录
2. 在 Key 管理中导入 Tavily API Key（支持单个添加或批量导入）
3. 创建 Token，复制 Token ID
4. 在应用中使用代理端点：

```bash
curl -X POST http://localhost:9874/api/search \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "hello world"}'
```

也可以把 Token 放在 body 的 `api_key` 字段中：

```bash
curl -X POST http://localhost:9874/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "hello world", "api_key": "YOUR_TOKEN"}'
```

## API 端点

### 代理端点

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/search` | 代理 Tavily Search API |
| POST | `/api/extract` | 代理 Tavily Extract API |

认证方式：`Authorization: Bearer {token}` 或 body 中 `api_key` 字段。

### 管理端点

所有管理端点需要 `X-Admin-Password` 请求头或 `Authorization: Bearer {ADMIN_PASSWORD}`。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | Web 管理控制台 |
| GET | `/api/stats` | 用量统计概览 |
| GET | `/api/keys` | 列出所有 Key（脱敏显示） |
| POST | `/api/usage/sync` | 手动同步 Tavily 官方真实额度 |
| POST | `/api/keys` | 添加 Key：`{"key":"tvly-xxx"}` 或批量 `{"file":"文本内容"}` |
| DELETE | `/api/keys/{id}` | 删除 Key |
| PUT | `/api/keys/{id}/toggle` | 启用/禁用 Key：`{"active": 1}` |
| GET | `/api/tokens` | 列出所有 Token |
| POST | `/api/tokens` | 创建 Token：`{"name":"备注"}` |
| DELETE | `/api/tokens/{id}` | 删除 Token |

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ADMIN_PASSWORD` | `admin` | 管理控制台和管理 API 的密码 |
| `USAGE_SYNC_TTL_SECONDS` | `300` | Tavily 真实额度缓存秒数，超过后控制台会自动重新同步 |
| `USAGE_SYNC_CONCURRENCY` | `4` | 后台并发同步 `/usage` 的最大 Key 数 |

## Token 配额

每个 Token 默认配额：

- 小时限制：100 次
- 日限制：500 次
- 月限制：5000 次

超出配额返回 `429 Too Many Requests`。

## 数据持久化

SQLite 数据库存储在 `data/proxy.db`。Docker 部署时通过 volume 挂载 `./data:/app/data` 持久化。
