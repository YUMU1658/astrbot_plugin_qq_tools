# astrbot_plugin_qq_tools

> 为 **AstrBot** 提供 QQ 平台（OneBot / aiocqhttp）特定的工具集：引用回复、撤回消息、检索最近消息、群管理、头像查看、网页浏览（Playwright）、视频分析（Gemini）、以及「定时唤醒」等能力。  
> 让机器人在 QQ 场景下的“动手能力”更强、更像一个真正能帮你操作的助理，增强自主性。

---

## ⚠️ 重要声明（请务必阅读）

1. **本插件基本全利用 AI 完成编写**（包括逻辑组织与大量代码片段）。  
2. 因为 AI 生成代码的客观局限，**可能存在较多不完善之处**：边界条件、异常处理、兼容性、性能、安全性等都可能需要进一步打磨。  
3. **仅在 aiocqhttp（NapCat）适配器下完成测试**。  
4. **约 90% 的功能仅适用于 aiocqhttp / OneBot**（尤其是：消息 ID、撤回、群管理、视频消息解析等）。

如果你在其他适配器上使用，可能会遇到“工具不可用 / 无效果 / 报错”等情况，这属于预期范围。

---

## ✨ 功能一览

### 1) 消息与上下文能力（强烈推荐开启）
- **引用回复**：对指定 `message_id` 的消息进行引用回复  
- **复读 / 转发**：复读指定消息（用于“转发/复述/复读”）
- **撤回消息**：撤回（删除）一条或多条消息（含权限/时间窗口判断）
- **搜索最近消息**：从缓存或 API 拉取最近消息，供 LLM“查上下文”
- **刷新消息**：主动刷新最近消息列表（补齐上下文）
- **结束对话**：让机器人主动结束当前会话（用于中止任务/清理状态）
- **消息详情**：根据 `message_id` 获取更完整的消息结构（可选注入图片到视觉上下文）

### 2) 用户信息与互动
- **获取 QQ 用户信息**：查询用户公开资料
- **戳一戳**：触发 QQ 的戳一戳动作（可返回动作文案）
- **查看头像**：获取头像大图（支持“注入视觉上下文”或“调用模型描述图片”）

### 3) 群管理工具（建议默认关闭 + 配合权限控制）
- 修改群名片  
- 群禁言 / 全体禁言  
- 踢人  
- 发布群公告  
- 设置精华消息  
- 设置专属头衔（通常要求机器人为群主）  
- 黑名单（AstrBot 内部黑名单：可设定时长，支持自动过期）

### 4) 网页浏览（Playwright）✅ *可选*
- 打开网页、点击、输入、滚动、等待
- 获取链接、查看页面图片、裁剪局部放大
- 截图并可注入到 LLM 视觉上下文（也可发送给 QQ 用户）

> **注意**：浏览器功能默认关闭，且可能涉及安全风险（SSRF），请务必阅读下方「安全与权限」章节。

### 5) 视频分析（Gemini）✅ *可选*
- 支持：
  - **B 站视频**：链接 / BV 号 / av 号 / b23.tv 短链 / 分享文本  
  - **QQ 视频消息**：通过 `message_id` 提取视频并分析  
- 需要配置 Gemini API Key（见下方配置说明）

### 6) 定时唤醒（Wake Scheduler）
- 让 LLM 在当前会话创建“延迟 N 秒后唤醒”的任务  
- 到期后会在**原会话**触发一次“系统唤醒”事件，继续执行任务/提醒

---

## ✅ 运行环境与依赖

- **适配器**：仅保证 `aiocqhttp (NapCat)` 可用  
- **可选依赖**
  - 网页浏览：`playwright` + `chromium`
  - 视频分析：Gemini API Key（以及可用的网络环境）

---

## 📦 安装方式

> 以下为通用安装示例，具体以你当前 AstrBot 的插件安装方式为准。

### 方法 A：直接放入插件目录
1. 将本插件文件夹 `astrbot_plugin_qq_tools/` 放入 AstrBot 的插件目录（通常为 `plugins/`）
2. 安装依赖（仅在你启用对应功能时需要）：
   ```bash
   pip install -r requirements.txt
   # 若启用网页浏览，还需要安装 Chromium
   playwright install chromium
   ```
3. 重启 AstrBot，在插件管理中启用本插件

### 方法 B：以源码形式管理
- 适合你想自行修改、二次开发、PR 的场景  
- 原理同上：确保插件目录可被 AstrBot 扫描到 + 安装依赖


---

## 🧰 工具列表（LLM 可调用）

> 工具是给 LLM 调用的“函数”。你在 QQ 里只需要自然语言发指令，模型会自动选择是否调用工具。  
> 如果你开启了 `compatibility.add_tool_prefix=true`，则所有工具名会加 `qts_` 前缀以避免冲突。

### 基础消息工具
| 工具名 | 作用 | 典型用途 |
|---|---|---|
| `get_recent_messages` | 获取最近消息列表 | “帮我看看刚才大家说了什么” |
| `refresh_messages` | 刷新最近消息 | “再刷新一下最新消息” |
| `reply_message` | 引用回复指定消息 | “回复他这条：xxx” |
| `repeat_message` | 复读/转发指定消息 | “把他刚刚那条转发出来” |
| `delete_message` | 撤回消息 | “撤回我上一条” |
| `stop_conversation` | 结束对话 | “停止/结束本次任务” |
| `get_message_detail` | 获取消息结构详情 | “这条消息里到底有什么？把图片也看一下” |

### QQ/群相关工具
| 工具名 | 作用 |
|---|---|
| `get_user_info` | 获取用户资料 |
| `poke_user` | 戳一戳 |
| `view_qq_avatar` | 查看头像大图 |
| `change_group_card` | 修改群名片 |
| `group_ban` / `group_mute_all` | 禁言 / 全体禁言 |
| `kick_user` | 踢人 |
| `send_group_notice` | 群公告 |
| `set_essence_message` | 设精华 |
| `set_special_title` | 设头衔 |
| `ban_user` | AstrBot 黑名单（非 QQ 黑名单） |

### 网页浏览工具（需开启 `tools.browser=true`）
| 工具名 | 作用 |
|---|---|
| `browser_open` | 打开网页并生成带标记截图（注入视觉上下文） |
| `browser_click` | 按红色数字标记点击 |
| `browser_grid_overlay` | **点位辅助**：显示网格与坐标轴（用于定位未标记元素） |
| `browser_click_relative` | **相对点击**：配合网格工具，点击页面相对坐标 |
| `browser_click_in_element` | 在 Canvas/SVG 等元素内相对位置点击 |
| `browser_input` | 向输入框输入 |
| `browser_scroll` | 滚动页面 |
| `browser_wait` | 等待加载 |
| `browser_get_link` | 获取某个标记的链接 |
| `browser_view_image` | 查看页面内图片（注入视觉上下文） |
| `browser_crop` | 裁剪局部放大（便于看小字/细节） |
| `browser_screenshot` | 发送截图给用户（QQ） |
| `browser_close` | 关闭浏览器会话 |
| `browser_send_image` | 发送页面图片给用户（QQ） |

### 视频分析工具（需开启 `tools.view_video=true`）
| 工具名 | 作用 |
|---|---|
| `view_video` | 解析并分析视频（B站 / QQ视频消息） |

### 定时唤醒工具（默认开启）
| 工具名 | 作用 |
|---|---|
| `schedule` | 创建延迟唤醒任务 |
| `manage_wake` | 列出/删除/清空当前会话唤醒任务 |

---

## 🧪 推荐用法示例（在 QQ 里怎么说）

### 1) 引用回复 / 撤回
- “**回复**他这条：我已经看到了，稍后处理。”  
- “**撤回**我刚才那条消息。”  
- “把你上一条**用引用回复**的方式再说一遍。”

> 小技巧：开启 `general.show_message_id=true` 后，机器人发送的消息会自动附带 `[MSG_ID:xxxx]`，便于模型精准引用/撤回。

### 2) 让机器人“查上下文”
- “你能**回顾一下最近 20 条消息**吗？”  
- “刚才谁提到了 XX？**帮我搜一下**。”

### 3) 群管理（需权限）
- “把 @xxx **禁言 10 分钟**。”  
- “发一个群公告：今晚 9 点开会。”  
- “把这条消息设为精华。”

> 建议把群管理类工具放在 `tool_permission.admin_only_tools` 中，只允许管理员/白名单触发。

### 4) 网页浏览（需要 Playwright）
- “打开这个网页并帮我找到下载按钮：https://example.com”
- “在页面里搜索『pricing』并截图给我”
- “找不到按钮？用**网格工具**看一下，然后点右下角那个位置。”

> 机制说明：
> 1. `browser_open` 会给 LLM 注入一张带数字标记的截图，模型可据此点击/输入/滚动。
> 2. 如果页面元素无法被自动标记（如 Canvas 游戏、复杂地图），模型可调用 `browser_grid_overlay` 获取网格辅助图，再通过 `browser_click_relative` 进行坐标点击。

### 5) 视频分析（需要 Gemini Key）
- “帮我总结这个 B 站视频内容：BV1xxxxxx”  
- “分析一下刚刚发的视频（message_id=xxxx），告诉我它在讲什么。”

### 6) 定时唤醒（提醒/续作）
- “10 分钟后提醒我去喝水。”  
- “1 小时后在本群继续跟进刚才的任务。”

---

## 🔐 权限与安全

### 1) 危险工具建议默认关闭
- 群管理、撤回、浏览器、视频分析等，都属于“有副作用/有风险”的工具  
- 推荐：默认关闭 → 有需求再开启 → 配合 `tool_permission` 做权限限制

### 2) SSRF 风险：浏览器访问限制
浏览器工具支持打开 URL，这在不设防的情况下可能被诱导访问：
- `localhost` / 内网地址（10.x / 172.16.x / 192.168.x）
- 某些敏感服务、管理后台、内部面板等

因此插件提供了：
- `browser_config.allow_private_network=false`（默认禁用访问私有网络）
- `allowed_domains`（域名白名单）
- `blocked_domains`（域名黑名单，优先级更高）

**除非你非常确定自己在做什么,否则不要开启 `allow_private_network`。**

### 3) 浏览器等待策略配置
为确保交互操作后页面状态正确更新,插件提供了两个等待配置:
- `browser_config.post_action_wait_ms`（默认 500ms）：点击、输入、滚动等交互后,在截图前的等待时间
- `browser_config.user_screenshot_wait_ms`（默认 500ms）：`browser_screenshot` 工具发送截图给用户前的等待时间

这些配置确保:
- 点击按钮后,页面有时间加载新内容
- 输入文本后,页面动态效果有时间完成
- 用户截图时看到完全加载的页面状态

可在配置文件中调整（范围 100-2000ms）:
```json
{
  "browser_config": {
    "post_action_wait_ms": 500,
    "user_screenshot_wait_ms": 500
  }
}
```

### 4) 工具名冲突：前缀机制
如果你装了多个插件，可能存在工具重名。  
开启 `compatibility.add_tool_prefix=true` 后，本插件工具将全部变为 `qts_***`，同时自动清理旧名称残留（可用 `disable_auto_uninstall` 控制）。

---

## 🧩 兼容性说明

- ✅ **仅保证 aiocqhttp（NapCat）测试通过**  
- ❗约 **90% 的功能依赖 OneBot/aiocqhttp** 的事件结构与 API 能力  
- 其他适配器可能出现：
  - `message_id` 获取不到
  - 撤回/群管理 API 不存在
  - 视频消息结构不同导致解析失败
  - 头像/图片注入方式不一致等

---

## 🆘 常见问题（FAQ）

### Q1：为什么撤回失败？
- QQ/OneBot 通常限制 **2 分钟内撤回**（非管理员）  
- 群内撤回他人消息需要机器人有 **管理员/群主权限**  
- 某些实现返回的 `message_id` 形态不同（如 `12345_6789`），本插件已做多种兼容尝试，但仍可能失败

### Q2：模型找不到消息 ID，无法引用/撤回？
- 确保 `general.show_message_id=true`  
- 或让模型先调用 `get_recent_messages` 获取历史消息并提取 ID  
- 若担心 `[MSG_ID:xxx]` 进入长期记忆，可开启 `compatibility.delay_append_msg_id=true`

### Q3：浏览器工具无法使用/报 Playwright 错误？
- 需要安装依赖（注意新增了 Pillow 依赖）：
  ```bash
  pip install playwright Pillow
  playwright install chromium
  ```
- 确保已开启：`tools.browser=true`
- 若你运行环境无法下载 Chromium，可尝试使用镜像或手动安装浏览器

### Q4：视频分析一直失败？
- 检查 `gemini_video_config.api_key` 是否正确
- 尝试降低 B 站清晰度：`bilibili_quality=fluent`
- 视频过大/过长可能触发超时或上传失败，调小 `size_limit/duration_limit` 或增大 `timeout`

---

## 🤝 贡献与反馈

欢迎提 Issue / PR 
建议反馈时附带：
- AstrBot 版本、适配器类型（NapCat / 其他）
- 复现步骤、日志、消息样例（尽量脱敏）

---

## License

This project is licensed under the MIT License.
