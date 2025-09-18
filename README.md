# Festival Greeter 节日祝福插件

一个面向 AstrBot 的节日祝福插件，在指定节日自动调用 LLM 生成问候语并推送到目标群聊，支持群黑白名单、兜底文案与手动触发。默认内置中国节日（含部分传统农历节日映射），可按需扩展。

## 功能概览
- 预置以中国法定与传统节日为主的节日集合，支持自定义扩展。
- 自动在配置时间调用 LLM 生成 40-80 字的中文祝福，并推送到目标群聊。
- 群黑名单/白名单控制，可按需关闭名单限制。
- 支持多条兜底模板，在 LLM 不可用时保持服务。
- 提供 `/festival-send` 指令手动触发节日祝福（可配置开关）。
- 持久化记录推送历史，避免同一节日在冷却时间内重复发送。
- 支持节日持续天数配置，可选择仅首日推送或节期内每日推送。

## 安装与目录结构
```
festival_greeter/
├─ main.py              # 插件主逻辑
├─ holidays.py          # 节日定义与查询
├─ message_builder.py   # LLM 提示词与响应解析
├─ state_store.py       # 推送记录持久化
├─ _conf_schema.json    # AstrBot WebUI 配置 Schema
├─ metadata.yaml        # 插件元数据
└─ README.md            # 本说明文档
```

## 配置说明（_conf_schema.json）
- `timezone`：IANA 时区标识，默认 `Asia/Shanghai`。
- `trigger_time`：每日调度时间，24 小时制 `HH:MM`。
- `group_filter_mode`：`disabled` / `whitelist` / `blacklist`，用于控制推送目标。
- `group_filter_list`：与名单模式配套的群列表，既可填统一会话 ID，也可填群号。
- `llm_provider_id`：可选，指定用于祝福生成的 LLM Provider ID。
- `llm_prompt_style`：祝福语风格，`warm` / `formal` / `cheerful`。
- `custom_holidays`：补充节日，按两行一组填写：第一行写日期（`MMDD`，如 `0810`），第二行写节日名称；未配置的节日仍沿用内置集合。
- `fallback_messages`：兜底祝福模板，支持 `{holiday}`、`{date}`、`{year}` 占位符。
- `max_generation_retries`：LLM 调用失败时的额外重试次数。
- `allow_manual_trigger`：是否允许 `/festival-send` 指令。
- `holiday_repeat_mode`：节日推送策略，`first-day` 仅首日推送，`every-day` 节期内每日推送。

示例 `custom_holidays` 配置：

```
0803
七夕节
0928
孔子诞辰
```

每两行表示一个节日条目。

## 使用建议
1. **首次登记目标群**：在目标群执行一次 `/festival-send` 指令即可加入推送列表，同时立刻发送当日祝福。
2. **名单应用**：
   - 白名单：仅名单内会话允许推送，未登记的群不会被纳入。
   - 黑名单：名单内会话被排除，其余仍可推送。
3. **兜底模板**：至少预置 1 条 `fallback_messages`，保障在 LLM 服务不可用时仍能发送祝福。
4. **数据存储**：推送记录保存在 `data/festival_greeter/deliveries.json`，自动定期清理。
5. **Napcat / QQ 适配**：请确认机器人具备对目标群的主动发言权限。
6. **自定义节日**：在 `custom_holidays` 中按“日期+名称”两行一组填写，例如 `0923` 与 `秋分节气`；未匹配到的节日会回退到插件内置清单。

## 指令说明
- `/festival-send`：在当前会话手动发送当日节日祝福。若当天节日已推送或会话被名单限制，将返回提示。
- `/festival-debug`：管理员专用调试指令，忽略冷却直接向当前会话推送当日节日祝福，可用于验证配置与 LLM 调用。

## 开发要点
- 遵循 AstrBot 插件的 `@register` 注册与 `Star` 生命周期约定。
- 调度任务通过 `asyncio.create_task` 启动，`terminate` 中会安全取消。
- LLM 响应通过 `extract_text_from_response` 做格式化处理，兼容多种 Provider 返回结构。
- 群推送调用 `context.send_message(unified_msg_origin, MessageChain)`，可兼容 Napcat。

## 常见问题
- **为何没有发送祝福？**
  - 确认群不在黑名单，或白名单模式下已经加入名单。
  - 查看 `data/festival_greeter/deliveries.json` 是否已记录当日发送。
- **如何新增推送群聊？**
  - 在目标群执行 `/festival-send` 指令可立即发送并登记。
  - 也可通过自定义节日测试触发校验配置是否生效。
- **LLM 未生成文本**：日志会输出错误原因，插件自动使用兜底模板。可调大重试次数或更换 Provider。
- **节日日期不正确**：内置节日包含部分农历节日，对 2024-2030 年进行了阳历映射；可在 `custom_holidays` 中覆盖或追加。

欢迎根据团队需求继续扩展，例如接入节日日程 API、支持多平台同步等。
