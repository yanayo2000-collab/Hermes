# 群聊天助手话术学习质量重构方案

> Goal: 重构“上传学习 / 学习机器人 / 候选生成 / 分类 / 筛选 / 提纯 / 入库 / 展示”的质量规则，避免用户原话、系统分析产物、语义重复、错类型文案进入可用话术。

## 一、现有问题结论

当前规则过弱，问题不只是几个关键词漏判，而是链路缺少分层质量门禁：

1. 原始聊天记录、系统总结、AI生成候选混在同一层处理。
2. 分类先于清洗，导致用户提问、噪音句也被分到某个类型。
3. 去重主要按完整文本，不能识别“同模板不同变量”的语义重复。
4. safe_to_send / enabled 过早变成 true，候选未经过足够质量审查。
5. 质量失败原因没有结构化保存，运营只能看到“候选”，看不到为什么被过滤或降级。
6. 当前本地规则分散在多个函数里，缺少统一 Candidate Quality Gate。

## 二、目标原则

1. 学习不是直接生成可用话术；学习只产出“候选素材”。
2. 所有候选必须先过质量门禁，再进入备选区。
3. 默认保守：不确定就待确认，不自动可发送。
4. 用户原话、问题、投诉、求助、闲聊不能直接作为主动发言话术。
5. 系统分析产物、词频总结、模板调试句不能进入候选。
6. 同模板不同变量、同意改写、轻微标点差异都应去重或合并。
7. 过滤、降级、合并都要留下 reason，便于后续调参。

## 三、新链路设计

### 1. 原始输入层 Raw Input

来源：
- 上传文件
- 学习机器人读取群消息
- 人工新增话术

处理：
- 只做基础解析，不做可发送判断。
- 保留 source_type：upload_file / learning_account / manual。
- 标记 raw_text、sender、created_at、file_name。

### 2. 清洗层 Clean

清洗规则：
- 去 WhatsApp 时间戳、发言人、手机号、链接、@、系统消息。
- 去纯数字、纯表情、短回复、感谢词、无业务价值闲聊。
- 识别并丢弃：
  - media omitted
  - message deleted
  - joined/left group
  - missed call
  - 安全码提示

输出：clean_text。

### 3. 意图识别层 Intent

先判定文本属于什么类型，而不是直接套话术类型。

建议 intent 枚举：
- user_question：用户提问
- user_request：用户求助/索要教程
- user_complaint：用户抱怨/问题反馈
- admin_instruction：管理员指令
- natural_atmosphere：自然气氛话术
- faq_answer：答疑话术
- onboarding_guide：教程引导
- motivation：激励运营
- system_artifact：系统/分析产物
- noise：噪音

只有下面几类允许进入候选：
- natural_atmosphere
- faq_answer
- onboarding_guide
- motivation
- 部分 admin_instruction 经改写后可进入

必须过滤：
- user_question
- user_request 原话
- user_complaint
- system_artifact
- noise

### 4. 候选改写层 Rewrite

对可用素材，不直接用原文，先改写成主动发言口吻：

例：
用户原话：
“Kok bisa ada user yang nyariin kak...”

不能入库。
如果要利用其业务意图，应改写为：
“Biar user mudah ingat, kakak bisa rutin menyapa dan bantu jawab pertanyaan dengan ramah ya.”

改写要求：
- 主动发言口吻
- 不引用用户个人问题
- 不出现“常见词/词频/系统分析”表达
- 不夹中文
- 符合目标类型

### 5. 质量门禁 Quality Gate

每条候选输出质量结构：

```json
{
  "decision": "accept | review | reject",
  "role_positioning": "community_seed | faq_helper | newcomer_guide | motivation_admin",
  "quality_score": 0-100,
  "safe_to_send": false,
  "enabled": false,
  "reasons": ["question_like", "meta_summary", "semantic_duplicate"],
  "normalized_key": "...",
  "semantic_key": "..."
}
```

强制 reject：
- 含 “istilah grup yang sering muncul”
- 含 “常见词/词频/terms/frequent terms” 这类分析产物
- 疑问句并且含 boleh tau / caranya / nyariin / gimana / gmna / bagaimana / why/how 等用户求助信号
- 第一人称求助或用户经历：aku/saya + bingung/mau/tidak bisa
- 包含手机号、链接、群链接、邀请码、SID、账号ID等敏感/动态信息
- 含辱骂、涉黄、赌博承诺、收益夸张承诺
- 语言混杂严重或翻译不可用

默认 review：
- AI 改写后可读但不确定类型
- 语义相似但不完全重复
- 过短/过长但可人工调整

accept 条件：
- 语义清晰
- 类型匹配
- 无敏感动态信息
- 非用户原问
- 通过语义去重
- 翻译状态 ok

注意：accept 只代表可进入备选，不等于自动 enabled=true。上传学习默认仍应待确认。

### 6. 分类规则

固定四类：

1. 气氛活跃型 community_seed
- 问候、鼓励群内互动、提醒看置顶、轻量活跃。
- 不包含具体绑定步骤。

2. 解惑答疑型 faq_helper
- 解释 code、ID、admin、截图、提交信息等。
- 应是“回答口吻”，不是用户问题原文。

3. 教程引导型 newcomer_guide
- 步骤、准备资料、按流程提交。
- 应包含清晰动作，但不承诺结果。

4. 激励运营型 motivation_admin
- 鼓励坚持、按指导完成、保持积极。
- 不得夸张承诺收益。

分类冲突处理：
- faq 和 guide 冲突：优先 guide，如果有“步骤/先/然后/准备/提交”。
- community 和 motivation 冲突：有 semangat/pelan/konsisten 优先 motivation。
- 不确定：review，不自动归类。

### 7. 去重规则

三层去重：

1. Exact key
- 小写、去标点、去多空格。

2. Template semantic key
- 将变量部分归一。
- 示例：
  “istilah grup yang sering muncul: kak, wa, ya...”
  “istilah grup yang sering muncul: kak, ya, yg...”
  统一为：
  “istilah grup yang sering muncul: <terms>. kalau bingung...”

3. Embedding/近似语义 key（后续）
- 没有 embedding 时先用规则版 simhash/token overlap。
- 相似度超过阈值进入 review 或合并 frequency。

合并策略：
- 已存在 accept/review 的同语义候选：不新增，只增加 frequency/source_files。
- 跨类型重复：只保留最匹配类型一条。
- reject 不展示给运营，但可计入调试日志。

### 8. 翻译与释义

- 先识别源语言，不能固定印尼语。
- 中文释义仅用于运营理解，不影响原文发送。
- 翻译失败：text_zh_status=needs_translation，候选只能 review，不能 accept。
- 不能把规则替换结果当准确翻译。

### 9. 前端展示改造

备选区应展示：
- 类型
- 原文
- 中文释义
- 来源：上传/学习机器人/人工
- 状态：待确认/可用/已拒绝（默认隐藏已拒绝，可开调试）
- 质量原因：如“疑似用户问题”“系统分析产物”“语义重复已合并”

批量操作：
- “待确认批量操作”只作用于待确认项。
- “全选本列表”改成“全选待确认 N 条”。
- 删除按钮改为“删除已选话术”，避免误解。

## 四、实施步骤

### Task 1: 抽出 CandidateQualityGate

文件：`app/main.py`

新增统一方法：
- `_evaluate_group_atmosphere_candidate_quality(text, role='', source_type='')`
- `_normalize_group_atmosphere_semantic_phrase_key(text)`
- `_is_group_atmosphere_reject_pattern(text)`

测试：`tests/test_group_atmosphere_role_bridge.py`

覆盖：
- 用户疑问句 reject
- 系统分析产物 reject
- 正常气氛话术 accept/review

### Task 2: 改上传学习入库链路

文件：`app/main.py`

位置：
- `auto_learn_group_atmosphere_chat_records`
- `save_role_manual_phrases` / manual-phrases API 对 upload_file/learning_account 的处理

规则：
- upload_file / learning_account 必须过 Quality Gate。
- reject 不进 template_pool。
- review 进 pool，但 enabled=false safe_to_send=false。
- accept 也默认 enabled=false safe_to_send=false，等待运营确认。

### Task 3: 改 AI候选生成器

文件：`app/main.py`

位置：`generate_group_atmosphere_ai_candidates`

规则：
- 不再使用 short_hint 生成“常见词总结类”话术。
- local_abbreviations 只能作为语气参考，不能拼进文案。
- 每条生成候选也要过 Quality Gate。

### Task 4: 历史数据清理脚本

文件：`scripts/cleanup_group_atmosphere_candidate_quality.py`

功能：
- 备份 DB。
- 扫描所有 template_pool。
- 对 reject 项删除或标记 disabled_rejected。
- 对 semantic_duplicate 合并。
- 输出清理报告。

### Task 5: 前端展示状态原因

文件：`app/main.py` 内嵌 JS/HTML

改造：
- candidate card 展示 quality_status / quality_reasons。
- 批量操作文案修正。
- 待确认数量只统计待确认。

### Task 6: 回归测试与线上验证

测试命令：

```bash
python3 -m py_compile app/main.py tests/test_group_atmosphere_role_bridge.py
python3 -m pytest tests/test_group_atmosphere_role_bridge.py -q
```

线上验证：
- 不触发 WhatsApp 发言。
- 不跑 scheduler/run-due。
- 只调用候选池 API / DB 只读审计。
- 验证无 reject 模式残留。

## 五、上线策略

1. 先本地实现 Quality Gate + 测试。
2. 本地用现有脏样本验证。
3. 用户确认后最小补丁上线。
4. 上线后先只读审计。
5. 备份 DB。
6. 清理历史脏候选。
7. 重启后端并验证 health。
8. 验证候选池页面展示。

## 六、验收标准

1. 这类不再出现：
- “istilah grup yang sering muncul...”
- 用户疑问句原文
- 词频/术语总结产物

2. 同模板不同变量只算一类重复。

3. 上传学习后：
- 结果 tips 保持到接口返回。
- 明确显示入库/生成/过滤数量。

4. 候选默认不自动可发送。

5. 运营能看到为什么某些话术待确认或被过滤。

6. 测试通过，线上无自动发言触发。
