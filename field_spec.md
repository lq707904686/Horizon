# 三仓同名字段统一定义（L4，person-lexicon-v1 / field-spec-v1，2026-11-23）

三仓各放一份本文件副本（字节一致），检查函数按此实现。只对新轮次（长文 055 起、视频 REC-66 起）生效。

| 字段 | 统一定义 | 检查 |
|---|---|---|
| subject_risk | 字符串（≥6 字）或非空 dict；同时扫已故/未成年当事人词（两仓同一词表） | 缺失/过短 WARN；命中已故/未成年 WARN（不 BLOCK） |
| comment_hook | 置顶评论钩子，人写，≥8 字，不得诱导互动；**两线都必须人填，不再回落自动生成** | 缺失/<8 字/诱导 → 长文 BLOCK、视频 FAIL |
| radar | {score, spread_score, spread_parts{person,hook,family,audience}, src}；来源统一：优先 hotspots.json 按标题匹配，退回台账 | 两线都查 person==0 与 hook==0，只 WARN |
| topic_type | 取值统一 4 个：人/处境、公司/资本、行业冷账、政策/制度（雷达打标同步扩到 4 值） | 只打标 |
| series_no | 视频专有（REC 号−64）；长文不用此字段，删去“同名同义”自称 | 视频 FAIL 规则不变 |
| ai_label | 长文 38_ 字段；视频等价物为 anim/ai_badge.json + 发布包必勾两栏；注释改为“等价物”而非“同名” | 不变 |
| finance_redline / finance_redline_ok | 判据词表三仓一致；输入文本统一为 标题 + 前 400 字（视频为标题 + 旁白前 400 字）；三处拦截点（建集/渲染/qc、排期）都认 finance_redline_ok 豁免 | 视频 BLOCK/FAIL、长文 WARN 不变 |
| 人物判定 | 一律读 person_lexicon.json（terms + patterns − exclude） | — |

重复提示只留一份：C-P1-2 仅 <055 轮；R7 删（A4 覆盖）；36/04 只留 v6 T2 BLOCK；前 3 秒只留一条（WARN，见 L6/L10）；35–50 秒反转只留一条 WARN；T-P0-1 与 R6b 只留 cadence R6b；L01/R5 标题完整性只留 cadence R5（按排期日）；长文财经 L16/C10 只留 C10；R26 删。
