# Probatio：智能报销助手

## 项目介绍

面向报销制度的 Agentic RAG 应用，支持上传制度文件、检索政策依据和带原文引用的问答。区别于传统 RAG 应用，本项目不再使用一套固定的检索算法，而是将检索作为一种能力开放给模型。模型可在语义搜索、关键词搜索、原文读取、知识关联读取中选择当前需要的检索方式。wiki作为知识导航，在后台定期维护知识库内容间关联的更新，作为模型的一种“全局记忆“，帮助其理解特定真实世界场景，在应用层为模型的推理能力构建harness，不只将模型作为检索中的一环。

- **制度管理**：上传 PDF、DOC、DOCX、XLSX，完成安全扫描、解析、分块和索引，查看处理进度并管理制度版本与生效日期。
- **报销问答**：根据问题自主选择查询工具检索制度，信息不足时发起澄清，回答附带原文引用与定位。
- **知识 Wiki**：根据已入库制度生成来源页、主题页和概览，支持浏览与编辑。

后端使用 Python、FastAPI、LangGraph，前端使用 React、TypeScript、Vite；数据服务使用 PostgreSQL、Redis、Elasticsearch，文件扫描使用 ClamAV。

## 运行命令

以下命令均在仓库根目录执行。

### 1. 准备配置

运行环境：Linux / amd64、Bash、Docker Engine 和支持 `--wait` 的 Docker Compose 插件。

```bash
test -e .env || cp .env.example .env
chmod 600 .env
```

编辑 `.env`：

- 设置镜像版本 `RELEASE_TAG`，例如 `0.1.0`；修改源码并重新构建时使用新的版本标签。
- 填写数据库密码，并同步修改 `APP_SETTINGS.database_url`；连接串中的密码须按 URL 规则编码。保留模板中的容器服务名与容器内路径。
- 填写 `APP_SETTINGS` 中的 MinerU、DeepSeek、PPIO API Key；默认通过这些外部服务完成 PDF 解析、模型分块、生成、向量化和重排，按供应商规则计费。
- 准备下表资源，将对应变量改为宿主机上的绝对目录，并保证容器用户 UID 10001 可读取和访问。

| 配置变量 | 本地资源 |
|---|---|
| `DOCLING_MODELS_DIR` | 默认使用 MinerU，可填已创建的空目录；切换到 Docling PDF 解析时需准备 Docling / OCR 模型 |
| `BGE_M3_TOKENIZER_DIR` | BGE-M3 tokenizer |
| `QA_TOKENIZER_DIR` | 与 `qa.model.model_id` 一致的 tokenizer，模板使用 DeepSeek Flash |
| `CHUNKING_MODEL_TOKENIZER_DIR` | 与分块模型一致的 tokenizer，模板使用 DeepSeek V4 Pro |
| `RERANKER_TOKENIZER_DIR` | BGE-Reranker-v2-M3 tokenizer |

`APP_SETTINGS` 为单引号包围的 JSON，数据库连接串需直接填写完整值。

若目标报销场景包含现有词典未覆盖的专业术语，可在 `deploy/elasticsearch/analysis-ik/custom/reimbursement.dic` 中补充或调整词条，以改善关键词检索效果。词典采用 UTF-8 编码，每行一个术语。

### 2. 首次启动

完成配置与资源准备后执行：

```bash
bash scripts/stack.sh build &&
bash scripts/stack.sh init &&
bash scripts/stack.sh up
```

`build` 构建镜像；`init` 迁移数据库、创建检查点表和检索索引；`up` 启动服务。首次初始化病毒库可能较慢。

打开 [http://127.0.0.1:8080](http://127.0.0.1:8080)，创建知识库并上传制度，处理成功后即可提问。服务默认监听本机回环地址；远程访问时配置 SSH 端口转发或带认证的反向代理。

### 3. 日常操作

```bash
bash scripts/stack.sh ps                     # 查看服务状态
bash scripts/stack.sh logs ingestion-worker  # 查看文档处理日志
bash scripts/stack.sh logs qa-worker         # 查看问答日志
bash scripts/stack.sh logs wiki-worker       # 查看 Wiki 日志
bash scripts/stack.sh stop                   # 停止全部服务
bash scripts/stack.sh up                     # 启动已初始化的服务
bash scripts/stack.sh down                   # 移除容器和网络，保留数据卷
```

修改应用配置后重启：

```bash
bash scripts/stack.sh stop-app &&
bash scripts/stack.sh up
```

修改源码后，更新 `.env` 的 `RELEASE_TAG`，再执行：

```bash
bash scripts/stack.sh stop-app &&
bash scripts/stack.sh build &&
bash scripts/stack.sh up
```

升级涉及数据库迁移或索引初始化时，在 `up` 前执行 `bash scripts/stack.sh init`；执行 `init` 时应用服务须已停止。

### 4. 前端源码构建

容器构建会自动编译前端。直接构建前端源码时，准备满足 `frontend/package.json` 版本要求的 Node.js 和 npm，然后执行：

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

构建产物位于 `frontend/dist/`。构建所需依赖由 `frontend/package-lock.json` 锁定。

前端字体及其原始许可保存在 `frontend/public/fonts/`。
