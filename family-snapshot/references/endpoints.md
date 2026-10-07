# 接口契约要点 · 家底快照

本技能只与账本的本机 HTTP 服务对话。**所有接口路径常量只有一个定义处：
`scripts/_common.py` 的 `EP` 字典。** 要改路径，改那里。

## 路径常量表（`EP`）

| 键 | 路径 | 说明 |
|---|---|---|
| `health` | `GET /api/health` | **唯一免令牌端点**，探活用 |
| `ready` | `GET /api/ready` | 数据格式版本 + 表规模（自检用） |
| `settings` | `GET/PUT /api/settings` | 读设置 / 改设置（机制性键被服务端挡） |
| `base_currency` | `POST /api/settings/base-currency` | 变更本位币（不可逆档） |
| `base_currency_history` | `GET /api/base-currency-history` | 变更历史 |
| `rates` | `GET/PUT /api/rates` | 默认汇率表 / 整表覆盖（不可逆档） |
| `currencies` / `currency` | `GET /api/currencies` · `/api/currencies/{code}` | 币种字典 |
| `platforms` / `platform` | `GET /api/platforms` · `/api/platforms/{id}` | 平台字典 |
| `categories` / `category` | `GET /api/categories` · `/api/categories/{id}` | 分类字典 |
| `tags` / `tag` | `GET /api/tags` · `/api/tags/{id}` | 标签字典 |
| `accounts` / `account` | `GET/POST /api/accounts` · `GET/PATCH/DELETE /api/accounts/{id}` | 账户 |
| `inventory_session` | `POST /api/inventory/session` | 盘点开局（**无请求体**） |
| `inventory_draft` | `PUT/DELETE /api/inventory/draft` | 盘点草稿 |
| `inventory_preview` | `POST /api/inventory/preview` | **只读试算**，不落库 |
| `inventory_snapshots` | `POST /api/inventory/snapshots` | 保存快照 |
| `snapshots` | `GET /api/snapshots` | 快照列表（**时间倒序**） |
| `snapshots_compare` | `GET /api/snapshots/compare` | 两期对比 |
| `snapshot` | `GET/DELETE /api/snapshots/{id}` | 快照详情 |
| `snapshot_returns` | `GET /api/snapshots/{id}/returns` | 某期收益 |
| `reports_trend` | `GET /api/reports/trend` | 趋势 |
| `reports_breakdown` | `GET /api/reports/breakdown` | 结构占比 |
| `reports_platform_currency` | `GET /api/reports/platform-currency` | 平台 × 币种 |
| `reports_currency` | `GET /api/reports/currency` | 币种分布 |
| `reports_returns` | `GET /api/reports/returns` | 收益面板 |
| `reports_returns_breakdown` | `GET /api/reports/returns-breakdown` | 收益按维度拆解 |
| `reports_returns_trend` | `GET /api/reports/returns-trend` | 收益趋势 |
| `backup` | `POST /api/backup` | 立即备份一次 |
| `restore` | `POST /api/restore` | 从备份恢复（不可逆档） |
| `import_preview` | `POST /api/import/preview` | 导入预览（只读） |
| `import_apply` | `POST /api/import/apply` | 应用导入（不可逆档） |

`{id}` / `{code}` / `{kind}` / `{ref}` 是占位符，由 `Ledger.send(method, key, body, **fmt)` 填充。

## 错误契约

服务端一律返回：

```json
{ "error": { "code": "...", "message": "...", "details": {...}, "request_id": "..." } }
```

`code` 取值与 HTTP 状态码：

| code | HTTP | 含义 |
|---|---|---|
| `VALIDATION_FAILED` | 400 | 入参不合法（zod 层） |
| `UNAUTHORIZED` | 401 | 缺少或无效令牌 |
| `FORBIDDEN` | 403 | 来源不被允许（`Origin` 不在白名单） |
| `NOT_FOUND` | 404 | 资源不存在（含引用不存在的基础资料） |
| `METHOD_NOT_ALLOWED` | 405 | 方法不对 |
| `CONFLICT` | 409 | 冲突（草稿版本、字典被引用、导入冲突） |
| `RULE_VIOLATION` | 422 | 违反业务规则（未填写门禁等） |
| `INTERNAL` | 500 | 服务内部错误 |

**技能一律按 `code` 分派，不看 HTTP 状态码**（`scripts/_common.py` 的 `ApiError`）。
服务端错误**原样透出**，不改写文案、不替使用者做决定。

⚠ **已知实现坑（已修，勿回退）**：`ApiError.__init__` 必须把 `details` 交给 `super().__init__(...)`。
`FsError.__init__` 里有一句 `self.details = details`，若先自己赋值再调 `super()`，会被 `None` 覆盖
⇒ 所有按 `details` 分派的分支（如 `confirm_required` 门禁）静默失效，
表现为「本该 exit 4 的待确认错误漏成 exit 2 的普通接口错误」。

## 安全边界（实测）

- 令牌走请求头 **`X-App-Token`**，值取自 `DATA_DIR/.token`。
- **唯一免令牌端点**：`/api/health`。其余 `/api/*` 无令牌 → **401**。
- 带非白名单 `Origin` → **403**。
- **不带 `Origin` 头时由令牌兜底放行 → 200** —— 这是本机非浏览器客户端的合法口子，
  因此技能**刻意不带 `Origin` 头**（带了反而要维护一份来源白名单）。
- **无请求体的写操作不需要 `Content-Type`**（内容类型守卫只针对带体写，按 HTTP 分帧判定）。

## 接口变了改哪里

| 变了什么 | 改哪里 |
|---|---|
| 路径增删改 | `scripts/_common.py` 的 `EP`（唯一定义处） |
| 入参约束 | `app/shared/src/schemas.ts`（zod，前后端共用）→ 对应用户提示与 `plan.py`/`write.py` 的参数 |
| 响应形状 / DTO 字段 | `app/shared/src/contracts.ts` + `app/server/src/lib/dto.ts` → `scripts/read.py` 的渲染与 `references/recipes.md` |
| 错误码语义 | `app/server/src/lib/errors.ts` + 本文件上表 → `scripts/_common.py` 的分派 |
| 新增能力 | 先在 `scripts/safety.py` 的 `ACTIONS` 登记分级，再在 `write.py` 的 `HANDLERS` 实现（未登记的动作 `write.py` 直接拒） |
| 仓库里挪了目录 / 改了路径 | 技能里所有引用（含 `HELP_SOURCE_REL`、本文件表格、`README.md`、`references/*.md`）→ 用 `npm run check:skill:refs` 体检（查旧路径、查引用是否存在、查装机副本是否过期） |

⚠ **口径（折算 / 汇总 / 收益 / 两期对比 / 趋势）一律不在技能内实现**，
技能只负责「调接口 + 把结果摆给人看」。任何在技能里复刻公式的改动都是错的。
