# 因子使用与检验边界

五个因子的公式、回看周期、输入价格基准见 MOMENTUM_PROTOCOL.json；来源是本地 src/watchlist/features.py 的既有因果计算。

| 因子 | 本轮独立研究 | 本轮成本回放 | 原前向实验账户 |
|---|---|---|---|
| return_1 | 5/10/20日标签的时序 IC、训练三档及后续验证 | 无新增过滤器；R0 原规则已有收盘高于前收盘的条件 | 仅原条件，未加新因子 |
| return_5 | 同上 | 未加入 | 未加入 |
| return_20 | 同上 | R1 固定正值过滤 | 未加入 |
| return_60 | 同上 | R2 固定正值过滤 | 未加入 |
| relative20 | 同上，相对SPY | R3 固定正值过滤 | 未加入 |

源码调用链：v12.data.frame → 既有 feature_frame；v12.research → 训练分位、后续固定档及日期块统计；v12.accounts → 既有 signal 与 v11.kernel.replay。
检验执行不等于有效性通过。本轮没有通过预冻结多重比较的组合，四个成本对照也没有正净收益；不把描述性正 IC 称为已验证因子。
因果与账本工程测试覆盖下一日开盘、标签未成熟、缺失行情、参考缺失、块重采样固定性、价格/股息对账、边界 A/B 和原 V1.1 回归。日志见 tests.log。
三个 QuantSkills 本轮未安装或运行完整上游算法：factor-factory 仍只是既有目录/结构参考，factor-orthogonalize 未执行完整正交化，backtest-overfit 的旧 DSR/PBO 证据属于 V1，本轮没有冒称它生成了动量结论。本轮是五个明确公式的本地检验。
价值、质量、增长、情绪及自动月度改进：NOT_IMPLEMENTED。