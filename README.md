[README.md](https://github.com/user-attachments/files/32414131/README.md)
\# MediaFix



\*\*影视资源智能重命名与自动归类系统 · WebDAV + 123 云盘 + AI 大模型 · Docker\*\*



MediaFix 是一个面向 NAS、WebDAV、123 云盘用户的自动化媒体库整理工具。启动服务后，在 Web 管理界面中配置网盘和 AI 大模型，即可自动扫描影视文件、智能识别、重命名，并按照 Emby / Jellyfin / Plex 等媒体库规范完成目录归类。



\## 默认信息



| 项目 | 默认值 |

|---|---|

| Web 管理端口 | `5225` |

| 默认管理员账号 | `chenchengcheng666` |

| 默认管理员密码 | `chenchengcheng666` |

| 默认访问地址 | `http://<设备IP>:5225` |



> 首次登录后请立即在「个人资料」中修改默认密码。



\## 功能特性



\- \*\*多源支持\*\* — WebDAV（Alist、Nextcloud、群晖等）与 123 云盘 OpenAPI 直连

\- \*\*Web UI 配置\*\* — 网盘、AI API 均在 Web 界面中填写，无需启动前改环境变量

\- \*\*AI 智能识别\*\* — 接入大语言模型，对混乱文件名进行语义理解与识别

\- \*\*批量 AI 解析\*\* — 每批 15 个文件，支持目录上下文推断剧名，自动缓存结果

\- \*\*自动重命名\*\* — 按标准媒体库格式批量重命名，同步处理同名字幕/海报附件

\- \*\*目录自动归类\*\* — 自动创建电影、剧集、动漫分类目录并移动文件

\- \*\*非影视资源过滤\*\* — 自动识别广告、预告片、OP/ED/PV、资源站水印等并移入 `\_trash/`

\- \*\*同名文件去重\*\* — 目标已存在时按扩展名优先级（mkv/iso/ts > mp4/mov/avi）保留更优版本

\- \*\*空目录自动清理\*\* — 整理完成后自动删除源目录中的空文件夹

\- \*\*自动定时任务\*\* — 支持间隔循环与每日定点两种模式，自动扫描 · 解析 · 归档

\- \*\*重命名任务持久化\*\* — 任务进度实时持久化，服务重启后自动标记中断

\- \*\*多用户与邀请码\*\* — 支持 admin / member / normal 三种角色，额度分级

\- \*\*运行日志与记录\*\* — 在 Web 界面查看扫描、识别、整理和失败清单



\## 技术栈



\- Python 3.10+

\- FastAPI + Uvicorn

\- 原生 JavaScript 前端（无框架）

\- WebDAV 协议 + 123 云盘 OpenAPI

\- AI 大模型接口，兼容 OpenAI API 格式

\- Docker / Docker Compose



\## 项目结构



```text

MediaFix/

├── app/

│   ├── \_\_init\_\_.py

│   ├── main.py              # 入口，FastAPI 应用与核心业务

│   ├── batch\_parse.py       # 批量 AI 解析模块（SSE 流式）

│   ├── rename\_tasks.py      # 重命名任务持久化与后台执行

│   ├── pan123.py            # 123 云盘 OpenAPI 客户端（多账号）

│   ├── unified\_source.py    # 统一数据源接口

│   ├── source\_dispatch.py   # 统一分派层（基于 drivers registry）

│   ├── drivers/

│   │   ├── \_\_init\_\_.py

│   │   ├── base.py          # 驱动抽象基类

│   │   ├── registry.py      # 驱动注册表

│   │   ├── webdav.py        # WebDAV 驱动

│   │   └── pan123.py        # 123 云盘驱动

│   └── static/

│       ├── index.html

│       ├── style.css

│       ├── fx.js            # 动效工具集

│       └── app.js           # 业务逻辑

├── data/                    # 持久化数据（运行时生成）

├── Dockerfile

├── docker-compose.yml

├── requirements.txt

├── README.md

└── LICENSE

```



\## 快速开始



\### 1. 克隆仓库



```bash

git clone https://github.com/chenchengcheng66/MediaFix.git

cd MediaFix

```



\### 2. 启动服务



```bash

docker compose up -d

```



或：



```bash

docker-compose up -d

```



> 启动前不需要填写 WebDAV、123 云盘或 AI API 信息，全部在 Web 界面中配置。



\### 3. 打开 Web 管理界面



浏览器访问：



```text

http://<你的设备IP>:5225

```



本机运行可访问：



```text

http://127.0.0.1:5225

```



\### 4. 登录



默认管理员账号：



```text

chenchengcheng666

```



默认管理员密码：



```text

chenchengcheng666

```



> 首次登录后请立即修改默认密码。



\### 5. 首次配置流程



1\. 进入「网盘管理」添加 WebDAV 或 123 云盘

2\. 进入「设置 → AI API 配置」添加并激活一个 AI 供应商

3\. 返回主界面，选择网盘，浏览目录，点击「递归扫描目录视频」

4\. 勾选文件，点击「执行重命名」，先预览标准名再确认

5\. 如需自动运行，进入「自动定时任务」创建任务



\## 使用流程



```text

启动 Docker

&#x20;  ↓

打开 Web 管理界面 http://<设备IP>:5225

&#x20;  ↓

使用默认账号密码登录

&#x20;  ↓

修改默认密码

&#x20;  ↓

配置网盘（WebDAV / 123 云盘）

&#x20;  ↓

配置并激活 AI API

&#x20;  ↓

扫描目录视频

&#x20;  ↓

勾选文件 → 执行重命名

&#x20;  ↓

在「重命名任务」中查看进度

&#x20;  ↓

（可选）创建自动定时任务

```



\## 支持的网盘



| 类型 | 说明 | 认证方式 |

|---|---|---|

| WebDAV | Alist、Nextcloud、群晖、坚果云等 | URL + 用户名 + 密码 |

| 123 云盘 | OpenAPI 直连，支持多账号 | OAuth 授权获取 Token |



> 115 云盘已在架构中预留，尚未实现。



\## 支持的 AI 供应商



| 供应商 | 默认 Base URL | 备注 |

|---|---|---|

| DeepSeek | `https://api.deepseek.com` | 支持余额查询 |

| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 通义千问系列 |

| 月之暗面 Kimi | `https://api.moonshot.cn/v1` | 支持余额查询 |

| 智谱 AI | `https://open.bigmodel.cn/api/paas/v4` | GLM 系列，支持思考模式 |

| 火山引擎豆包 | `https://ark.cn-beijing.volces.com/api/v3` | 豆包系列 |

| 自定义 | 自行填写 | 兼容 OpenAI API 格式 |



\## 命名规则



\### 电影



```text

{title} ({year}).{ext}

```



\### 剧集 / 动漫



```text

{title} ({year}) - S{season}E{episode}.{ext}

```



\### 目录结构



```text

{成品库}/

├── 电影/

│   └── {title} ({year})/

│       └── {title} ({year}).{ext}

├── 剧集/

│   └── {title} ({year})/

│       └── Season 01/

│           └── {title} ({year}) - S01E01.{ext}

└── 动漫/

&#x20;   └── {title} ({year})/

&#x20;       └── Season 01/

&#x20;           └── {title} ({year}) - S01E01.{ext}

```



\## 非影视资源过滤



以下文件会被自动识别并移入源目录下的 `\_trash/` 文件夹：



\- 文件名含网址（`www.` / `http` / `.com` 等）

\- 广告、推广、加群、公众号等关键词

\- 资源站水印（电影天堂、飘花、韩剧TV 等）

\- 预告片、试看、sample、preview、trailer

\- 动漫 OP / ED / PV / SP / NC / CM / Menu 等特典

\- 小于 20MB 的视频文件



\## 环境变量



`docker-compose.yml` 中已配置以下变量：



| 变量 | 说明 | 默认值 |

|---|---|---|

| `TZ` | 时区 | `Asia/Shanghai` |

| `MEDIAFIX\_ADMIN\_USER` | 管理员用户名 | `chenchengcheng666` |

| `MEDIAFIX\_INIT\_PWD` | 管理员初始密码 | `chenchengcheng666` |



> WebDAV、123 云盘、AI API 等配置均保存在 `/data` 目录下的 JSON 文件中，通过 Web 界面管理，不通过环境变量配置。



\## 数据持久化



`docker-compose.yml` 已配置：



```yaml

volumes:

&#x20; - ./data:/data

&#x20; - ./app:/app/app

```



`/data` 目录下保存以下文件：



| 文件 | 说明 |

|---|---|

| `users.json` | 用户账号 |

| `secrets.env` | 密钥与激活的 API ID |

| `webdav\_list.json` | WebDAV 源配置 |

| `pan123\_accounts.json` | 123 云盘账号 |

| `api\_configs.json` | AI API 配置 |

| `ai\_cache.json` | AI 解析缓存 |

| `auto\_config.json` | 自动任务配置 |

| `auto\_log.json` | 自动任务执行日志 |

| `series\_index.json` | 剧集索引（统一目录） |

| `rename\_tasks.json` | 重命名任务记录 |

| `history.json` | 操作历史 |

| `stats.json` | 统计数据 |



\## 用户角色与额度



| 角色 | 每日额度 | 自动任务 |

|---|---|---|

| `admin` | 无限 | 可用 |

| `member` | 3000 | 可用 |

| `normal` | 100 | 不可用 |



管理员可在「设置」中生成邀请码，邀请码可指定角色与有效期。



\## 常见问题



\### 默认端口是多少？



默认端口是 `5225`，访问地址为 `http://<设备IP>:5225`。



\### 默认管理员账号密码是什么？



默认账号：`chenchengcheng666`  

默认密码：`chenchengcheng666`



首次登录后请立即修改。



\### 启动前要填 WebDAV 和 AI API 吗？



不用。先启动服务，进入 Web 管理界面后再填写。



\### 配置会保存在哪里？



保存在 `./data` 目录中。请确保 `docker-compose.yml` 中配置了持久化卷，避免容器重建后配置丢失。



\### 为什么建议先预览？



因为重命名和移动文件会直接修改媒体库。先预览可以确认 AI 识别是否准确，避免误整理。



\### 支持哪些 AI 服务？



只要兼容 OpenAI API 格式，一般都可以。内置了 DeepSeek、阿里云百炼、Kimi、智谱、火山引擎豆包等预设。



\### 123 云盘如何授权？



进入「网盘管理 → 123 云盘 → 添加新账号」，点击「打开 123 授权页面」，登录并授权后，将页面显示的两段 Token 粘贴回来即可。



\### 服务重启后正在运行的任务会怎样？



重命名任务会标记为 `interrupted`（异常中断），不会自动恢复。自动任务会在下个周期重新执行。



\## 开源协议



本项目基于仓库中的 LICENSE 文件授权。  

请查阅项目根目录下的 `LICENSE` 文件了解详情。



\## 贡献



欢迎提交 Issue 和 Pull Request。  

提交 PR 前请确保代码通过基本测试，并遵循项目现有代码风格。



\---



\*\*Made with ❤ by 陈橙呈 · 仅供学习交流，禁止商用\*\*



