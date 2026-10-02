# astrbot_plugin_laizhi 实现方案

> 群友「逆天言论」素材收集与随机发送插件。
> 核心交互：**回复一张图 + `/添加 猫猫`** → 入库并打 tag；**`/来只 猫猫`** → 随机抽一张发出，2 分钟后自动撤回。

本文档是设计稿（plan），不是最终代码。文中代码片段用于说明接口契约与关键分支，落地时按实际 AstrBot 版本校准。

---

## 0. 本版相对初稿的变更

| # | 变更 | 影响 |
| --- | --- | --- |
| 1 | 指令改为**空格分隔**（`/添加 猫猫`），前缀交给框架 | 删掉全量监听器，改用原生 `@filter.command`，见 §4.1 |
| 2 | 支持图片/视频/GIF/音频等多种格式，上限 10MB | 新增 `kind` 维度与组件映射，见 §5 |
| 3 | 一个文件可挂多个 tag | 存储改为多对多，见 §3.4 |
| 4 | 群白名单控制谁能 `/来只` | 新增 `enabled_groups`，见 §6.2 |
| 5 | tag 严格匹配，不做模糊 | 删掉 `difflib` 建议逻辑 |
| 6 | 重复添加要明确告知 | 靠 `(file_id, tag_id)` 主键判定 |
| 7 | `/alias 猫猫 动物` 合并 tag | 新增 §7 |
| 8 | tag 不存在时自动新建 | `get_or_create_tag` |
| 9 | 每条指令可配权限 | 运行时权限表，见 §6.1 |
| 10 | 限流：未撤回图片占满名额时丢弃后续指令 | 名额信号量，见 §8.2 |
| 11 | 发送后 2 分钟自动撤回 | 需绕过 `event.send()`，见 §8.1 ⚠️ |
| 12 | 回复图片 `/删除 猫猫` 解除该 tag | 见 §9.3 |
| 13 | 文件按内容哈希全局存一份 | 同 #3，物理层只存一份 |

---

## 1. 目标与非目标

### 1.1 指令总览

| 指令 | 用法 | 默认权限 |
| --- | --- | --- |
| 添加 | 回复一条含媒体的消息，发 `/添加 猫猫` | everyone |
| 来只 | `/来只 猫猫`（别名 `来张`/`来个`） | everyone |
| 删除 | 回复那条媒体，发 `/删除 猫猫` | everyone |
| alias | `/alias 猫猫 动物` 合并两个 tag | admin |
| 标签 | `/标签` 列出全部 tag 及数量 | everyone |
| 标签详情 | `/标签 猫猫` 看某 tag 的数量与最近添加 | everyone |

全部指令前缀由 AstrBot 的 `wake_prefix` 决定（默认 `/`，用户若配成 `%` 就是 `%添加 猫猫`）。**插件代码里不出现前缀字面量**，见 §4.2。

### 1.2 不做（v1 范围外）

- 不做内容识别/自动打标，不接 LLM。
- 不做 Web 管理面板，只用 AstrBot 自带插件配置页。
- 不做跨平台/跨实例同步与云备份。
- 不做 tag 模糊匹配（按需求明确排除）。

---

## 2. 插件骨架

### 2.1 目录结构

```
astrbot_plugin_laizhi/
├── main.py              # 插件类 + 6 个指令 Handler（薄层）
├── storage.py           # SQLite：files / tags / file_tags 三表
├── media.py             # 下载、magic 嗅探、kind 判定、hash
├── recall.py            # 发送-撤回-名额回收闭环
├── permission.py        # 运行时权限与群白名单
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
└── plan.md
```

职责边界（防止 `main.py` 膨胀成一坨）：

- `main.py`：只做「解参 → 校验 → 调模块 → 拼回复」。**不含 SQL、不含 HTTP、不含 asyncio 定时器**。
- `storage.py`：只管数据一致性与随机抽样，不认识消息格式。
- `media.py`：把任意媒体引用（http / `file://` / base64 / 本地路径）变成磁盘上的字节 + 元信息。
- `recall.py`：唯一持有「已发送待撤回」状态的地方，§8 的名额与定时器都在这。

### 2.2 metadata.yaml

```yaml
name: astrbot_plugin_laizhi
display_name: 来只
desc: 收集群友逆天素材，按 tag 归档，随机来一张，支持自动撤回
short_desc: 回复素材「/添加 xx」入库，「/来只 xx」随机来一张
version: 0.1.0
author: hsin
repo: https://github.com/<you>/astrbot_plugin_laizhi
support_platforms:
  - aiocqhttp
astrbot_version: ">=4.9.2"
```

**只声明 `aiocqhttp`**，这是本版的重要收敛。原因：自动撤回（需求 11）依赖 OneBot 的 `delete_msg`，并且需要拿到自己发出消息的 `message_id`——这两件事目前只有 aiocqhttp 路径能做到（§8.1）。其他平台可加载但撤回会降级为不撤回，故不在 `support_platforms` 里承诺。

`astrbot_version >= 4.9.2`：`StarTools.get_data_dir()` 依赖的 `self.name` 自该版本可用。

### 2.3 requirements.txt

```
aiohttp>=3.9
filetype>=1.2
```

- `aiohttp`：异步下载（官方禁止插件用 `requests`）。
- `filetype`：纯 magic-bytes 嗅探，**不解码图像**，比 Pillow 轻且无 decompression-bomb 风险。需求 2 要支持视频，Pillow 本来也处理不了，正好换掉。
- 不引入 `ffmpeg`/`opencv`：只为取视频宽高/时长不值得，这两个字段设为可空（§3.4）。

---

## 3. 存储设计

### 3.1 选型

用标准库 `sqlite3`。需求 3/6/13（多对多、重复判定、全局单份存储）本质是关系型问题，JSON 要全量重写且得自己加锁，活跃群很容易丢数据。

### 3.2 目录布局

全部落在 `StarTools.get_data_dir()`（即 `data/plugin_data/astrbot_plugin_laizhi/`），**绝不写插件自身目录**（更新即丢）。

```
data/plugin_data/astrbot_plugin_laizhi/
├── laizhi.db
├── blobs/
│   └── ab/ab12cd34....mp4      # 按 hash 前 2 位分桶
└── tmp/                        # 下载中转，启动时清空
```

分桶理由：单目录几万文件后 `readdir` 退化，排查也难。

### 3.3 内容寻址（需求 13）

物理文件以 `sha256(bytes)` 命名，**一份内容在磁盘上只存一次**，无论它被挂到多少个 tag。`files` 表记录物理文件，`file_tags` 记录归属关系。

好处：同一张图挂 5 个 tag 只占 1 份空间；重复添加天然幂等（hash 已存在就只插关系行）。

### 3.4 表结构

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 物理文件，一份内容一行
CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    hash       TEXT NOT NULL UNIQUE,     -- sha256 hex
    rel_path   TEXT NOT NULL,            -- blobs/ab/ab12....mp4
    kind       TEXT NOT NULL,            -- image | gif | video | audio | file
    ext        TEXT NOT NULL,
    mime       TEXT,
    size       INTEGER NOT NULL,
    width      INTEGER,                  -- 可空：视频/未知格式不强求
    height     INTEGER,
    created_at INTEGER NOT NULL
);

-- tag。name 唯一且大小写不敏感
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL COLLATE NOCASE UNIQUE,
    alias_of   INTEGER REFERENCES tags(id) ON DELETE SET NULL,  -- 非空表示本 tag 是别名
    created_by TEXT,
    created_at INTEGER NOT NULL
);

-- 多对多归属（需求 3）
CREATE TABLE IF NOT EXISTS file_tags (
    file_id   INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    added_by  TEXT,
    group_id  TEXT,
    added_at  INTEGER NOT NULL,
    PRIMARY KEY (file_id, tag_id)        -- 需求 6：重复添加靠它拦截
);

CREATE INDEX IF NOT EXISTS idx_ft_tag ON file_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_ft_file ON file_tags(file_id);

-- 待撤回队列，用于重启后补偿（§8.3）
CREATE TABLE IF NOT EXISTS pending_recalls (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    group_id   TEXT,
    recall_at  INTEGER NOT NULL
);
```

`storage.py` 对外接口：

- `get_or_create_tag(name, creator) -> Tag`：需求 8，不存在就建，`INSERT ... ON CONFLICT DO NOTHING` + `SELECT` 兜回。
- `resolve_tag(name) -> Tag | None`：严格匹配（`COLLATE NOCASE`），并跟随 `alias_of` 跳到目标 tag（最多跳 1 层，§7）。
- `attach(file, tag) -> "added" | "duplicate"`：靠 `PRIMARY KEY` 冲突判定，`changes()` 为 0 即重复。
- `detach(file, tag) -> bool`：需求 12。
- `random_file(tag) -> Row | None`：`COUNT(*)` + `OFFSET randrange(n)`，避免 `ORDER BY RANDOM()` 的全表排序。
- `merge_tag(src, dst)`：需求 7，见 §7。
- `gc_orphans()`：删掉 `file_tags` 已无引用的 `files` 行与物理文件。

**写入顺序**（崩溃安全）：先写 `tmp/` → `fsync` → `os.replace` 原子改名到 `blobs/` → 再提交事务。反过来会产生「有行无文件」的坏数据；这个顺序最坏只留孤儿文件，由 `gc_orphans()` 清理。

### 3.5 为什么不再有 scope 字段

需求 4 把隔离语义从「图库隔离」改成了「**使用权限**隔离」：图库始终是 global 的，只是限定哪些群能用 `/来只`。所以 `tags` 不再需要 `scope` 列，白名单纯属权限层（§6.2）。这比初稿简单一档。

---

## 4. 指令解析

### 4.1 为什么能用原生 `@filter.command`（以及为什么粘连写法不行）

读了 `astrbot/core/star/filter/command.py` 的 `CommandFilter.filter()`，匹配逻辑是：

```python
message_str = re.sub(r"\s+", " ", event.get_message_str().strip())
for full_cmd in self.get_complete_command_names():
    if message_str.startswith(f"{full_cmd} ") or message_str == full_cmd:
        ok = True
        message_str = message_str[len(full_cmd):].strip()
```

即指令名后面**必须是空格或字符串结尾**。`添加猫猫` 既不等于 `添加`，也不以 `添加 ` 开头，所以匹配不上——这正是改用空格分隔的直接原因。改完之后：

- 不需要 `@filter.event_message_type(ALL)` 全量监听，省掉「每条群消息都进 Handler」的开销与误触发风险。
- 不需要自己剥离 `%`/`/` 前缀。
- 参数由框架 `validate_and_convert_params` 解析后经 `event.set_extra("parsed_params", ...)` 注入，直接作为函数形参拿到。

### 4.2 前缀交给框架

`WakingCheckStage` 已经做了前缀剥离：

```python
if event.message_str.startswith(wake_prefix):
    is_wake = True
    event.is_at_or_wake_command = True
    event.message_str = event.message_str[len(wake_prefix):].strip()
```

到 `CommandFilter` 时 `message_str` 已不含前缀。所以注册 `@filter.command("添加")`，用户配 `/` 就是 `/添加`，配 `%` 就是 `%添加`，**插件零感知**。需求 1 自动满足。

### 4.3 Handler 签名

```python
from astrbot.api.event import filter, AstrMessageEvent

@filter.command("添加")
async def cmd_add(self, event: AstrMessageEvent, tag: str = ""):
    ...

@filter.command("来只", alias={"来张", "来个"})
async def cmd_lai(self, event: AstrMessageEvent, tag: str = ""):
    ...

@filter.command("删除")
async def cmd_del(self, event: AstrMessageEvent, tag: str = ""):
    ...

@filter.command("alias")
async def cmd_alias(self, event: AstrMessageEvent, src: str = "", dst: str = ""):
    ...

@filter.command("标签")
async def cmd_tags(self, event: AstrMessageEvent, tag: str = ""):
    ...
```

⚠️ **参数一律给默认值 `""`**。看 `validate_and_convert_params`：缺参数时会 `raise ValueError("必要参数缺失...")`，而 `WakingCheckStage` 捕获后会直接把 `f"插件 {name}: {e}"` 发给用户——那是一条很难看的报错。给默认值后不抛异常，由我们自己回一句「用法：`/添加 <标签名>`」。

### 4.4 tag 名规范化与严格匹配（需求 5）

```python
def normalize_tag(raw: str) -> str | None:
    name = unicodedata.normalize("NFKC", raw.strip())   # 全角→半角
    if not name or len(name) > 32:
        return None
    if re.search(r'[\\/:*?"<>|\s]', name):              # 挡路径穿越与空白
        return None
    return name
```

查询用 `WHERE name = ? COLLATE NOCASE` —— 这是**严格相等**比较，只是忽略 ASCII 大小写，不是模糊匹配。初稿的 `difflib` 相似推荐按需求 5 删除。tag 不存在时：

- `/添加`：自动创建（需求 8）。
- `/来只`：回「还没有「xxx」这个标签，`/标签` 看看有哪些」，**不推荐相似项**。

---

## 5. 媒体处理（需求 2）

### 5.1 来源提取

按优先级，命中即停：

1. 回复消息的 `Reply.chain` 里的媒体组件（`Image` / `Video` / `Record` / `File`）。
2. `Reply.chain` 为空或只有占位符时，用 `Reply.id` 回退调 OneBot：
   `await event.bot.call_action("get_msg", message_id=str(reply.id))`，从返回段里再找媒体。**这条路最可靠**，AstrBot 核心自己的引用解析也是这么做的。
3. 当前消息自带的媒体（支持「直接发图 + `/添加 猫猫`」）。
4. 都没有 → 提示「回复一条图片/视频再发 `/添加 <标签>`」。

多个媒体时取第一个，并在回复里注明「已取第 1 个，共 3 个」。

### 5.2 下载与校验

```python
async def fetch(src) -> bytes:
    # http(s)://  -> aiohttp 流式读，累计超 10MB 立刻 abort
    # file://     -> unquote 后 open()（aiocqhttp 常下发 file:// URI，
    #                Windows 形如 file:///C:/... 要处理前导斜杠）
    # base64://   -> b64decode
    # 其他        -> 当本地路径处理
```

校验链：

1. **大小**：`max_size_mb`（默认 10，需求 2）。必须**流式累计判断**，不能 `await resp.read()` 之后再测长度——否则一个恶意大文件就能打爆内存。
2. **magic bytes 嗅探**：`filetype.guess(head_bytes)`。**不信任扩展名、不信任 URL 后缀、不信任平台给的 mime**。
3. **kind 归类**：决定回放时用哪个组件。
4. **hash**：`sha256`。

### 5.3 kind → 消息组件映射

这是需求 2 的落点。各组件的构造方式已对过源码（`astrbot/core/message/components.py`）：

| kind | 典型格式 | 回放组件 | 备注 |
| --- | --- | --- | --- |
| `image` | jpg/png/webp/bmp | `Comp.Image.fromFileSystem(abs_path)` | |
| `gif` | gif | `Comp.Image.fromFileSystem(abs_path)` | QQ 当动图发，无需特殊处理 |
| `video` | mp4/mov/mkv/webm | `Comp.Video.fromFileSystem(abs_path)` | 有 `fromFileSystem`，已确认 |
| `audio` | mp3/wav/amr/silk | `Comp.Record.fromFileSystem(abs_path)` | 有 `fromFileSystem`，已确认 |
| `file`  | 其他白名单内 | `Comp.File(name=..., file=abs_path)` | 构造签名是 `File(name, file, url)`，与前三者不同 |

```python
ALLOWED = {
    "image": {"jpg", "jpeg", "png", "webp", "bmp"},
    "gif":   {"gif"},
    "video": {"mp4", "mov", "mkv", "webm"},
    "audio": {"mp3", "wav", "amr", "ogg", "m4a"},
}
```

`allow_arbitrary_file`（默认 `false`）：开启后白名单外的类型也收，按 `file` kind 存。**默认关闭**——群里随手一个 exe/zip 被收进库，既占空间又是分发风险。

⚠️ 一个必须注意的坑：`Image`/`Record` 在 aiocqhttp 发送时会走 `convert_to_base64()`（见 `_from_segment_to_dict`），10MB 文件转 base64 后约 13.3MB，单条消息可能被协议端拒绝。所以 `max_size_mb` 虽可配到 100，但**默认 10 是有道理的**，调大时要提示用户风险。

---

## 6. 权限设计

### 6.1 逐命令权限（需求 9）

不能用 `@filter.permission_type(ADMIN)` 装饰器——它在**导入期**求值，而我们的权限要**用户在面板上可配**。所以用运行时检查：

```python
# permission.py
LEVELS = ("everyone", "admin")

def check(event, config, cmd: str) -> bool:
    need = config.get("perm", {}).get(cmd, "everyone")
    return event.is_admin() if need == "admin" else True
```

`event.is_admin()` 等价于 `event.role == "admin"`，而 `role` 由 `WakingCheckStage` 依据 `admins_id` 配置写入——即 **AstrBot 管理员**，不是 QQ 群主/群管。需求 9 说的「astrbot 管理员」正是这个，语义对上了。

配置项 `perm.add` / `perm.lai` / `perm.del` / `perm.alias` / `perm.tags`，默认 `alias` 为 `admin`（合并 tag 是破坏性操作），其余 `everyone`。

### 6.2 群白名单（需求 4）

```python
def group_allowed(event, config) -> bool:
    groups = config.get("enabled_groups", [])
    if not groups:          # 空 = 全部放行
        return True
    return event.get_group_id() in groups
```

- 作用于哪些指令由 `whitelist_applies_to` 配置，默认 `["lai", "add", "del"]`（`/标签` 放行）。
- 私聊：`get_group_id()` 为空，由 `allow_private` 控制，默认 `false`。
- 不在白名单时**静默忽略**（`stop_event` 但不回复），避免机器人在不该说话的群里刷存在感。

---

## 7. tag 合并 `/alias`（需求 7）

`/alias 猫猫 动物` 的语义定为：**把「猫猫」合并进「动物」，并让「猫猫」继续作为别名可用**。

```sql
BEGIN;
-- 1) 关系迁移，已存在的组合自动跳过（需求 6 的幂等性在这里复用）
INSERT OR IGNORE INTO file_tags (file_id, tag_id, added_by, group_id, added_at)
    SELECT file_id, :dst_id, added_by, group_id, added_at
    FROM file_tags WHERE tag_id = :src_id;
-- 2) 清空源 tag 的关系
DELETE FROM file_tags WHERE tag_id = :src_id;
-- 3) 源 tag 退化为别名
UPDATE tags SET alias_of = :dst_id WHERE id = :src_id;
COMMIT;
```

要点：

- 保留别名而非直接删 tag，群友习惯了 `/来只 猫猫` 不会突然失效。
- `resolve_tag()` 跟随 `alias_of` **只跳一层**。合并前先把 `dst` 自身 resolve 一次，保证 `alias_of` 永远指向实体 tag，从根上杜绝链式/环形引用。
- 防御：`src == dst` 直接拒绝；`dst` 不存在则按需求 8 自动创建；`src` 不存在则提示而非静默。
- 回复要给出实际迁移数量：「已将「猫猫」(37) 合并进「动物」，新增 31 个，重复 6 个」。

---

## 8. 发送、撤回与限流（需求 10、11）

这是本方案**技术风险最高**的部分，先说清楚约束。

### 8.1 ⚠️ `event.send()` 拿不到 message_id

读 `aiocqhttp_message_event.py` 的 `_dispatch_send()`：

```python
await bot.send_group_msg(group_id=..., message=messages, **routing_params)
# ↑ 返回值（含 message_id）被直接丢弃，函数签名是 -> None
```

整条 `send()` → `send_message()` → `_dispatch_send()` 链路都不回传 `message_id`（上游 issue #9356 仍是 open 状态）。**所以 `yield event.chain_result(...)` 这条常规路径无法支撑撤回功能。**

解决：`/来只` 专门走底层 API，自己拿 `message_id`。

```python
# recall.py
payload = await event.bot.call_action(
    "send_group_msg",
    group_id=int(event.get_group_id()),
    message=[{"type": "image", "data": {"file": file_uri}}],
)
message_id = payload["message_id"]
```

代价与缓解：

- 绕过了框架的 `on_decorating_result` / `after_message_sent` 钩子，其他插件装饰不到这条消息。对一个发梗图的插件可接受。
- 绕过了 `_parse_onebot_json` 的 base64 转换，需自己按 kind 拼 OneBot 段；本地文件用 `file:///abs/path` URI。
- 非 aiocqhttp 平台 `call_action` 不存在 → 回退到 `yield event.chain_result(...)`，**不撤回、不占名额**，并 `logger.info` 说明降级。

### 8.2 名额信号量（需求 10）

需求描述的是「未撤回图片数达上限后，必须等撤回一张才能再来一张，期间指令丢弃」——这是**并发名额**而非时间窗限流。

```python
# 每个群一个计数器
outstanding: dict[str, int]      # group_id -> 当前未撤回数

max_outstanding = config["max_outstanding"]   # 默认 3

if outstanding.get(gid, 0) >= max_outstanding:
    return          # 直接丢弃，不回复、不排队（需求明确「丢弃」）
```

- 发送成功 `+1`，撤回完成（无论成功失败）`-1`，用 `try/finally` 保证**一定回收**，否则一次撤回失败就永久占死一个名额。
- 可叠加一个传统时间窗 `cooldown_seconds`（默认 3 秒）防连点，两者是 AND 关系。
- 丢弃时默认不回复。配 `notify_on_throttle: true` 可回一句「等会儿，还有 3 张没撤」，但默认关闭——限流时再刷屏是自相矛盾的。

### 8.3 定时撤回（需求 11）

```python
async def _schedule_recall(self, bot, gid, message_id, delay: int):
    try:
        await asyncio.sleep(delay)          # 默认 120
        await bot.call_action("delete_msg", message_id=int(message_id))
    except Exception as e:
        logger.warning(f"撤回 {message_id} 失败: {e}")
    finally:
        self._release(gid, message_id)      # 名额必须回收
```

用 `asyncio.create_task`，**不要用 `threading.Timer`**（异步环境里起线程做延时是浪费且难管理）。

必须处理的三件事：

1. **任务引用**：`create_task` 的返回值要存进 `self._tasks: set`，并加 `task.add_done_callback(self._tasks.discard)`。否则任务可能被 GC 回收导致撤回静默失效——这是 asyncio 的经典坑。
2. **插件卸载**：`terminate()` 里把未完成任务全部 `cancel()`，否则热重载后旧任务还在跑，可能撤销新实例的消息。
3. **重启补偿**：`pending_recalls` 表落盘。`__init__` 时扫描：`recall_at` 已过期的立即尝试撤回一次，未过期的重建定时任务。QQ 对超过 2 分钟的消息撤回常常失败，所以过期项**尝试一次即删行**，不重试。

### 8.4 撤回失败的现实情况

`delete_msg` 在这些情况下会失败：消息已被手动撤回、机器人非管理员且消息超时、协议端不支持。这些都**只记 warning，不回复用户**，并照常释放名额。撤回是锦上添花，不能因为它失败就影响主流程。

---

## 9. 关键流程

### 9.1 `/添加 猫猫`

```
框架已匹配指令并注入 tag 参数
 ├─ tag 为空 → 回「用法：/添加 <标签名>」
 ├─ 权限检查 perm.add + 群白名单 → 不过则静默/提示
 ├─ normalize_tag() → 非法则提示
 ├─ 提取媒体（Reply.chain → get_msg 回退 → 当前消息）→ 无则提示
 ├─ 下载 → 10MB 校验 → magic 嗅探 → kind 判定 → 白名单校验
 ├─ sha256 → files 表查/插（已存在则复用，不重复落盘｜需求 13）
 ├─ get_or_create_tag（需求 8）
 ├─ attach(file, tag)
 │    ├─ "duplicate" → 「这个已经在「猫猫」里了」（需求 6）
 │    └─ "added"     → 「已加入「猫猫」，当前 37 个」
 └─ event.stop_event()
```

### 9.2 `/来只 猫猫`

```
 ├─ tag 为空 → 用法提示
 ├─ 权限 + 群白名单（不过则静默）
 ├─ 名额检查：outstanding >= max → 直接 return（丢弃｜需求 10）
 ├─ resolve_tag（严格匹配 + 跟随 alias）→ 不存在则提示，不推荐相似项
 ├─ random_file(tag) → 空 tag 则提示
 │    └─ 行在但文件丢了 → 删关系行、重抽一次（自愈，最多 3 次）
 ├─ 按 kind 组装 OneBot 段 → call_action 发送 → 取 message_id
 ├─ outstanding += 1；写 pending_recalls；起撤回任务（需求 11）
 └─ event.stop_event()
```

**自愈逻辑**值得单列：迁移漏拷文件、手工删过 `blobs/`、磁盘故障，都会造成「有行无文件」。抽到这种行时不要报错，删行后重抽，并记 warning。

### 9.3 `/删除 猫猫`（需求 12）

```
 ├─ 必须是回复消息 → 否则提示「请回复要删除的那张图」
 ├─ 提取被回复消息的媒体 → 下载 → 算 sha256
 │    注：必须重新下载算 hash，不能信 URL——QQ 的图片 URL 带时效参数，同图不同 URL
 ├─ files 按 hash 查 → 不存在则「这个不在库里」
 ├─ resolve_tag → 不存在则提示
 ├─ detach(file, tag) → False 则「这个不在「猫猫」里」
 ├─ 若该 file 已无任何 tag → 按 gc_orphan 配置删物理文件（默认 true）
 └─ 回「已从「猫猫」移除，剩余 36 个」
```

注意：删除只解除**当前 tag** 的关系，文件若还挂在别的 tag 上则物理文件保留（需求 3 的必然推论）。回复里要说清楚：「已从「猫猫」移除（仍在「动物」中）」。

---

## 10. 配置（`_conf_schema.json`）

```json
{
  "max_size_mb": {
    "description": "单个文件大小上限（MB）",
    "type": "int",
    "default": 10,
    "hint": "超过 10MB 时 base64 编码后可能被协议端拒绝，请谨慎调大",
    "slider": { "min": 1, "max": 100, "step": 1 }
  },
  "allow_arbitrary_file": {
    "description": "允许收录白名单之外的任意文件类型",
    "type": "bool",
    "default": false,
    "obvious_hint": true,
    "hint": "开启后 zip/exe 等也会被收录，存在分发风险"
  },
  "recall_after_seconds": {
    "description": "发送后自动撤回延迟（秒），0 为不撤回",
    "type": "int",
    "default": 120
  },
  "max_outstanding": {
    "description": "每群同时存在的未撤回素材上限，达到后新指令被丢弃",
    "type": "int",
    "default": 3
  },
  "cooldown_seconds": {
    "description": "同群「来只」最小间隔（秒）",
    "type": "int",
    "default": 3
  },
  "notify_on_throttle": {
    "description": "被限流时是否提示用户",
    "type": "bool",
    "default": false
  },
  "enabled_groups": {
    "description": "允许使用的群号白名单，留空表示全部允许",
    "type": "list",
    "default": []
  },
  "whitelist_applies_to": {
    "description": "白名单生效的指令",
    "type": "list",
    "default": ["lai", "add", "del"]
  },
  "allow_private": {
    "description": "是否允许私聊使用",
    "type": "bool",
    "default": false
  },
  "perm": {
    "description": "各指令所需权限",
    "type": "object",
    "items": {
      "add":   { "description": "添加", "type": "string", "default": "everyone", "options": ["everyone", "admin"] },
      "lai":   { "description": "来只", "type": "string", "default": "everyone", "options": ["everyone", "admin"] },
      "del":   { "description": "删除", "type": "string", "default": "everyone", "options": ["everyone", "admin"] },
      "alias": { "description": "合并标签", "type": "string", "default": "admin",    "options": ["everyone", "admin"] },
      "tags":  { "description": "查看标签", "type": "string", "default": "everyone", "options": ["everyone", "admin"] }
    }
  },
  "gc_orphan": {
    "description": "文件不再属于任何标签时删除物理文件",
    "type": "bool",
    "default": true
  },
  "download_timeout": {
    "description": "下载超时（秒）",
    "type": "int",
    "default": 30
  }
}
```

注入方式：

```python
from astrbot.api import AstrBotConfig

def __init__(self, context: Context, config: AstrBotConfig):
```

`AstrBotConfig` 继承 `dict`。改 schema 后框架会递归补默认值、移除废弃项，所以代码里**一律 `.get(key, 默认值)`**，不要假设某个键一定存在。

---

## 11. 健壮性与生命周期

- **`terminate()` 必须实现**：`cancel()` 所有撤回任务 → 关闭 `aiohttp.ClientSession` → `conn.close()`。三者任一遗漏，热重载几次就会攒出幽灵任务、连接泄漏。
- **启动清理**：清空 `tmp/`；加载 `pending_recalls` 做补偿（§8.3）。
- **并发**：SQLite 用 `check_same_thread=False` + 一个 `asyncio.Lock`。同 tag 并发添加由 `PRIMARY KEY` 兜底，**不要用「先查再插」**（有 TOCTOU 竞态）。
- **异常边界**：每个 Handler 顶层 `try/except Exception`，`logger.exception()` 后回一句「出错了，已记日志」。异常外抛会污染 AstrBot 的消息管道。
- **`event.stop_event()`**：所有指令处理完都要调，否则消息继续流到 LLM，机器人会多嘴接一句。
- **日志**：`from astrbot.api import logger`。只记关键节点（新增/重复/拒绝/自愈/撤回失败）。
- **不要**在 `__init__` 里做网络 IO 或大量磁盘扫描，会拖慢 AstrBot 启动。

---

## 12. 测试计划

| 类型 | 用例 |
| --- | --- |
| 单元 | `normalize_tag` 路径穿越/超长/全角；magic 嗅探各格式；`attach` 重复返回 `duplicate`；`merge_tag` 的幂等与计数；`random_file` 分布；`resolve_tag` 别名跳转与自指防御 |
| 集成 | 回复图/视频/GIF 添加；直接发图添加；重复添加提示；超 10MB 拒绝；白名单外类型拒绝；tag 自动创建；`/删除` 解除单 tag 后其他 tag 仍可抽到 |
| 限流撤回 | 连发 5 次 `/来只` 确认第 4、5 次被丢弃；等 2 分钟确认自动撤回且名额释放；手动先撤回那条消息再等自动撤回，确认失败不卡名额 |
| 重启 | 发一张后立刻重启 AstrBot，确认 `pending_recalls` 补偿生效；过期项只试一次 |
| 破坏性 | 手删 `blobs/` 下文件后 `/来只` 确认自愈；热重载 3 次确认无幽灵撤回任务、无 `tmp` 残留 |
| 权限 | 非管理员执行 `/alias` 被拒；白名单外群静默无响应 |

测试资源放 `tests/fixtures/`，不要塞进 `data/`。

---

## 13. 里程碑

| 阶段 | 产出 | 完成标准 |
| --- | --- | --- |
| M1 骨架 | `metadata.yaml`、`_conf_schema.json`、5 个指令空壳 | 加载成功，`/添加 x` 有回应，参数解析正确 |
| M2 存储 | `storage.py` 三表 + `media.py` 嗅探下载 | 能存图、重复能识别、`/标签` 看到计数 |
| M3 出图 | `random_file` + `/来只` + kind 组件映射 | 图/视频/GIF 都能正确发出 |
| M4 撤回限流 | `recall.py` 全套（**风险最高，留足时间**） | §12 限流撤回与重启用例全过 |
| M5 管理 | `/删除`、`/alias`、权限、白名单 | 权限用例全过 |
| M6 收尾 | README、logo、ruff、发布 | 上架插件市场 |

**M4 建议先做技术验证**：写个 10 行脚本确认 `event.bot.call_action("send_group_msg", ...)` 确实返回 `message_id`、`delete_msg` 确实能撤回自己的消息。这两点是需求 10/11 的地基，verify 失败就得立刻换方案（降级为「不撤回，仅时间窗限流」）。

---

## 14. 待确认的决策点

1. **`/alias` 的语义**：当前设计是「合并 + 源名保留为别名」。若你想要的是「纯合并、源名直接消失」，把第 3 步 `UPDATE tags SET alias_of` 改成 `DELETE FROM tags` 即可。
2. **合并方向**：`/alias A B` = 把 A 并进 B（后者为主）。反过来也说得通，需确认直觉是否一致。
3. **撤回失败兜底**：目前失败只记日志。是否需要在群里提示「撤回失败，请手动删除」？默认不提示。
4. **名额粒度**：当前按**群**计数。若希望全局共享名额（机器人在多群同时活跃时更保守），改成单一计数器即可。
5. **`max_size_mb` 上调风险**：视频接近 10MB 时 base64 膨胀到约 13MB，部分协议端（尤其 Lagrange/NapCat 默认配置）会拒收。若群友常传大视频，可能需要改走「本地路径直传」而非 base64，届时要测协议端是否与机器人同机部署。
