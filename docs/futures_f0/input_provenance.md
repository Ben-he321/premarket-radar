# F0 派生日线来源契约

2026-09-16 的恢复沿用原分支、数据根和 07:47:32 Europe/Madrid 截止，未开启新的四小时。

`scripts/futures_f0/run_cached_sessions.py` 显式将原 `CAPTURE_PREFIX_POLICY.json` 传入整合器。整合器和 `QualifiedInputs` 使用同一 `capture_prefix_clock` 验证函数，检查政策种类、GLBX 数据集、小时/分钟 schema、日期范围、更正政策与独立逻辑阶段。实际政策文件和冻结协议不改写。关联审阅文档的内容哈希进入派生记录；这证明所用审阅材料的版本，不是供应商对每个文件的认证。

`INTERNAL_CAPTURE_PREFIX` 的截止时间和内部计算时间数值相同，逻辑阶段为 `AFTER_INPUT_CUTOFF`，不添加一微秒。供应商发布时间、历史用户接收时间保留空值；下载 receipt 只说明本次文件取得时间。

派生身份与 raw 身份分开：

1. `derivation_sha256` 对除三个派生视图字段之外的固定载荷计算规范哈希；载荷版本为 1。
2. 载荷绑定具体合约、定义版本、逐日分段、政策、结算截止、registry 行、所有源 CSV 与 receipt 哈希及价格编码。
3. 每条价格记录绑定原始 CSV 哈希、`csv.DictReader.line_num` 和按原表头顺序保留的字段字符串哈希。行号包含表头，指记录结束的物理行，不能替换成记录序号。
4. 验证器流式重读源文件，依据定义身份和完整分段重新选桶、重新聚合，比较整个派生结果和候选字段。读取耗尽后再次检查文件与收据，避免信任仅存在于声明中的记录列表。
5. `source_object_hashes` 只含原始文件身份；`session_derivations` 是独立的派生文件索引。禁止把派生哈希塞入 raw 白名单。包含派生索引的 manifest 也不能把行改成供应商发布模式来绕过重算。
6. 导入器核对完整交易分段和日历证据哈希，不能只比较开收盘端点。价格字符串在严格检查有限数值后转换为引擎数值。

HTTP CSV 下载器的历史缺省格式按官方 `pretty_px=false` 解释为 1e-9 定点价格；decimal 只有 receipt 明确记录 `pretty_px=true` 才允许。真实 HTTP 默认依据：[Databento Historical API](https://databento.com/docs/api-reference-historical?historical=http)。没有重写旧收据。

真实样本 M6EM5、MZCK5 同一消息出现 trading-tick 与 clearing-tick 结算记录。所有版本保留；对尚未明确选择口径的同批版本不任意取最后一行。后续唯一版本按真实捕获时间单独选择，不能回填到先前决策。[GLBX statistics flags](https://databento.com/docs/venues-and-datasets/glbx-mdp3)。

来源链通过仍不等于可交易：完整预热、合约/交割边界、逐日日历、状态/停牌/限价与结算定价参考时刻仍各自检查。缺少真实券商保证金不是假设保证金层的独立阻断；本轮仍保留 10%/20% 初始保证金和原佣金假设，不改变风险规则。

恢复核对实际完成 15 个真实合约日线候选的重算，其中 13 个价格桶完整。两个玉米样本缺边界分钟，MZCK5 还缺两个小时；没有用零值填补。仅有两个不连续交易日，未构成最小账户贯通或 2022–2025 矩阵，实际账户完成数为 0，收益为 NA。
