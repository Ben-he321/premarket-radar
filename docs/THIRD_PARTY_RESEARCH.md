# 第三方研究 Skills 审计

只下载了任务书指定的3个仓库到本项目 `vendor/`（Git忽略），未安装全套量化环境、A股数据栈、下单插件或付费API。
下载由已安装的 skill-installer 完成；每个 SKILL.md、脚本及 LICENSE 已实际阅读。供应商代码不复制到本仓库，也不运行其随机演示或自动数据源回退。

|仓库|固定提交|许可证|实际采用与限制|
|---|---|---|---|
|quantskills/skill-quant-factor-skill-factory|d0fae3882bdf4b141ee56a939a3ce6f4e59700aa|GPL-3.0|导入 `generate_factor_skill_batch.py` 的 BASES 分类，与本地22个因果特征适配；完整生成器依赖未随仓库提供的 `tools/real_data_factor_pipeline.py`，未声称运行完整生成管线|
|quantskills/skill-factor-orthogonalize|36972acf62e37f9f5dd40504422d57ec561dee21|GPL-3.0|审阅入口会导入 `pandadata_runtime`；未安装A股栈、未运行该脚本。按任务书允许的可选处理，改用本地训练区间相关性审计，不做残差正交化、不声称已运行该Skill算法|
|quantskills/skill-backtest-overfit|c6c01622193d85340e156bd6fc5c16f1808f8a01|GPL-3.0|仅动态导入 `deflated_sharpe.py`、`pbo_cscv.py` 的纯计算函数；实际处理真实下载派生的开发区间日期矩阵。未运行 `__main__` 随机样本或 `data_source.py` 合成回退|

GPL LICENSE 保留在各本地 Skill 目录。若另行打包或分发这些第三方文件/衍生作品，必须另行履行其 GPL 义务；本 PR 不再分发供应商代码或行情。

本地依赖采用现有 pandas/numpy，新增 `scipy==1.18.1`；无 pandadata_runtime。22特征、12候选和旧固定基线在本轮绩效计算前冻结，未调用工厂生成数千候选。

统计适配器记录输入矩阵 SHA256、列名、观察数、全部证券×配置及滚动选择计数。DSR 输入是每日/每观察单位的 Sharpe，不传年化值；无亏损、常数序列、缺少矩阵时明确不可用。上游 PBO 对排名并列偏乐观，适配器拒绝退化块/明显并列，不删除失败候选来美化结果。

已执行已知答案：零Sharpe对零基准PSR=0.5、单试验最大Sharpe基准=0、年化单位换算、上下半区反转矩阵PBO=1。测试数据仅在临时目录；真实统计输入保存在本地 `research/complete-config-date-matrix.parquet`。

完整证券×配置×滚动模型搜索并非一组交换的独立试验。因此全搜索正式 DSR 为 `UNAVAILABLE_NONEXCHANGEABLE_SYMBOL_AND_ROLLING_TRIALS`；13个固定配置聚合矩阵只产生局部统计诊断，不是整个流程的防过拟合认证，也没有据此反复优化。

实际限制和调用结果见本地 `research/statistics.json`。未安装固定 Skill 的新环境可继续下载/回放，统计工具明确报告不可用，绝不改用假数据。

## 官方口径参考

- https://docs.alpaca.markets/us/docs/market-data-faq （SIP权限、证券更名、日线口径）
- https://docs.alpaca.markets/us/docs/about-market-data-api （Basic历史访问与额度；本项目更保守限制120/分钟）
- https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
- https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt
- 身份证据逐项在 `config/identity_evidence.json`，当前挂牌身份不等于已核实全部历史公司行动。
