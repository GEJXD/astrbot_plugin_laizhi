# astrbot_plugin_laizhi

「来只」是一个 AstrBot 插件，用于收集群友的逆天素材，并按标签随机抽取。

## 使用方式

默认指令前缀由 AstrBot 的 `wake_prefix` 决定。下面以默认的 `/` 为例；如果把 `wake_prefix` 配成 `%`，将 `/` 替换为 `%` 即可。

```text
回复一条图片/GIF/视频/音频：
/添加 猫猫

随机抽取：
/来只 猫猫
/来张 猫猫
/来个 猫猫

查看全部标签：
/标签

查看单个标签：
/标签 猫猫

回复一条已经收录的媒体，从某个标签中移除：
/删除 猫猫

管理员合并标签（把 A 合并进 B，A 保留为别名）：
/alias 猫猫 动物
```

指令名和参数之间需要有空格，例如 `/来只 猫猫`。标签严格匹配（忽略 ASCII 大小写），不做模糊推荐；添加时标签不存在会自动创建。

删除权限按标签关系计算：只有把这份素材添加到当前标签的用户，或者 AstrBot 管理员，才能执行 `/删除`。如果文件属于多个标签，删除其中一个标签仍然不影响其他标签；历史数据中没有记录添加者的关系只能由管理员删除。

`/添加` 优先读取被回复消息中的第一个媒体。如果回复链没有媒体，会在 aiocqhttp 上回退调用 `get_msg` 获取原消息；没有回复时，也支持当前消息直接带媒体。一次含多个媒体时只收录第一个，并在回复中说明数量。

## 支持的格式

默认允许：

- 图片：jpg、jpeg、png、webp、bmp
- GIF：gif
- 视频：mp4、mov、mkv、webm
- 音频：mp3、wav、amr、ogg、m4a

插件根据文件 magic bytes 判断格式，不信任 URL 后缀或平台提供的 MIME。单文件默认上限为 10 MB，下载时会流式限制大小。开启 `allow_arbitrary_file` 后可以把未识别格式按文件收录，请谨慎使用。

## 自动撤回与限流

在 aiocqhttp 群聊中，`/来只` 会直接调用 OneBot `send_group_msg`，取得 `message_id` 后按照 `recall_after_seconds`（默认 120 秒）调用 `delete_msg` 撤回。每群默认最多同时存在 3 条未撤回素材，达到上限后后续指令直接丢弃；`cooldown_seconds` 默认 3 秒，用于防止连续点击。

待撤回消息会写入数据库。插件热重载/重启后，在下一次获得 aiocqhttp bot 实例时会补偿过期或未过期的撤回任务。撤回失败只记录日志并释放名额，不会再次刷屏提示。

其他平台会降级为 AstrBot 普通消息发送，因此不承诺自动撤回；插件元数据目前只声明支持 `aiocqhttp`。

## 数据目录

所有数据都保存在 AstrBot 的插件数据目录，而不是源码目录：

```text
data/plugin_data/astrbot_plugin_laizhi/
├── laizhi.db
├── blobs/                 # 按 SHA-256 内容寻址，一份内容只保存一次
│   └── ab/ab....jpg
└── tmp/                   # 下载中转，启动时清理
```

同一个文件可以挂到多个标签。删除只解除当前标签关系；当文件不再属于任何标签时，默认删除物理文件。请在升级或迁移 AstrBot 时一并备份该目录。

## 配置

插件配置可以在 AstrBot WebUI 的插件配置页修改，主要选项包括：

- `max_size_mb`：单文件大小上限。
- `allow_arbitrary_file`：是否允许白名单外的文件。
- `recall_after_seconds`：自动撤回延迟，设为 `0` 关闭撤回。
- `max_outstanding`、`cooldown_seconds`：每群未撤回名额和最小间隔。
- `enabled_groups`：群白名单，留空表示全部群。
- `allow_private`：是否允许私聊指令，默认关闭。
- `perm`：分别设置添加、来只、删除、合并标签和查看标签的权限；`alias` 默认仅 AstrBot 管理员可用。

依赖由 `requirements.txt` 管理：`aiohttp` 用于异步下载，`filetype` 用于 magic bytes 嗅探。
