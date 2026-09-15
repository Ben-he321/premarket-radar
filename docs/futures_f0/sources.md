# Futures F0：合约、日历与成本来源核验

核验日期：2026-09-15。研究窗口固定为 2022–2025；2021 仅暖机。本文只记录本轮真实阅读到的交易所、监管提交与供应商说明，不证明已经取得历史合约行情或开户权限。

## 本轮证据边界

`contract_registry.csv` 保留六个市场的 13 个根代码，全部为 `ROOT_TEMPLATE_ONLY`。已核验根级产品规格与首个交易日期，但尚未取得供应商 instrument definitions、具体到期合约 ID、实际挂牌/退市日期、逐日假日日历、first notice、券商交割前强平期限、历史保证金及费用流水。对应字段为 UNKNOWN 或 NOT_ACQUIRED；现金交割产品的实物通知日使用 NOT_APPLICABLE_CASH_SETTLED。具体合约不得因为根模板存在而取得回放资格。

根的最早交易日期不等于每个到期合约的挂牌日期，更不等于本地数据覆盖起点。ZC 的 1877 日期是 CME 公布的产品家族历史，并非现代 Globex 或供应商 instrument ID 的上市时间。CME 的 [Historical First Trade Dates](https://www.cmegroup.com/media-room/historical-first-trade-dates.html) 支持 GC 1974-12-31、HG 1988-07-29、CL 1983-03-30、ZC 1877-01-02、6E 1999-01-04、M6E 2009-03-23、ES 1997-09-09。

## 合约规格与上市核验

| 市场 | 根 | 合约单位 | 报价单位 | 最小价格变动 | 每手 tick 美元值 | 交割 |
|---|---|---:|---|---:|---:|---|
| 黄金 | GC | 100 金衡盎司 | USD/盎司 | 0.10 | 10.00 | 实物 |
| 黄金 | MGC | 10 金衡盎司 | USD/盎司 | 0.10 | 1.00 | 实物 ACE |
| 黄金 | 1OZ | 1 金衡盎司 | USD/盎司 | 0.25 | 0.25 | 现金 |
| 铜 | HG | 25,000 磅 | 规范化 USD/磅 | 0.0005 | 12.50 | 实物 |
| 铜 | MHG | 2,500 磅 | USD/磅 | 0.0005 | 1.25 | 现金 |
| 原油 | CL | 1,000 桶 | USD/桶 | 0.01 | 10.00 | 实物 |
| 原油 | MCL | 100 桶 | USD/桶 | 0.01 | 1.00 | 现金 |
| 玉米 | ZC | 5,000 蒲式耳 | 美分/蒲式耳 | 0.25 美分 | 12.50 | 实物 |
| 玉米 | MZC | 500 蒲式耳 | 美分/蒲式耳 | 0.50 美分 | 2.50 | 现金 |
| 欧元 | 6E | 125,000 EUR | USD/EUR | 0.00005 | 6.25 | 外汇实物 |
| 欧元 | M6E | 12,500 EUR | USD/EUR | 0.0001 | 1.25 | 外汇实物 |
| 标普 | ES | 每点 50 USD | 指数点 | 0.25 | 12.50 | 现金 SOQ |
| 标普 | MES | 每点 5 USD | 指数点 | 0.25 | 1.25 | 现金 SOQ |

黄金依据：[GC 规格卡](https://www.cmegroup.com/trading/metals/files/fact-card-gold-futures-options.pdf)、[MGC 上市 SER-5391](https://www.cmegroup.com/tools-information/lookups/advisories/market-regulation/SER-5391.html)、[1OZ 上市清算通知 24-385](https://www.cmegroup.com/content/dam/cmegroup/notices/clearing/2024/12/chadv24-385.pdf)。MGC 首个交易日为 2010-10-04；ACE 代表标准金条仓单权益，不能假定交割一根独立 10 盎司金条。1OZ 为 2025-01-13，禁止补造 2022–2024 年可交易历史。

铜依据：[HG 规格卡](https://www.cmegroup.com/trading/metals/files/copper-futures-and-options.pdf)、[MHG 初始上市正式提交 22-089](https://www.cmegroup.com/market-regulation/rule-filings/2022/4/22-089.pdf)、[MHG 规格卡](https://www.cmegroup.com/education/files/micro-copper-futures-factcard.pdf)。MHG 首个交易日为 2022-05-02，首个到期月份为 2022 年 6 月，规则章节为 COMEX 914。HG 文件同时使用美分报价与美元传播说明，供应商价格缩放仍须 definitions 双重核验；表中数值乘数以 USD/磅规范化输入为前提。

原油依据：[NYMEX 200 章 CL](https://www.cmegroup.com/rulebook/NYMEX/2/200.pdf)、[NYMEX 309 章 MCL](https://www.cmegroup.com/rulebook/NYMEX/3/309.pdf)、[MCL 初始上市通知 21-191](https://www.cmegroup.com/notices/clearing/2021/06/Chadv21-191.html)、[2021 MCL FAQ](https://www.cmegroup.com/education/articles-and-reports/micro-wti-crude-oil-futures-faq)。本轮 100 桶 MCL 首个交易日为 2021-07-12；第一到期月份是 2021 年 8 月。历史上存在 [2011 年同名 MCL 代码、不同规格的 Micro Crude Oil 产品](https://www.cmegroup.com/tools-information/lookups/advisories/market-regulation/SER-5861.html)，不能用旧根代码行情证明当前 100 桶合约提前存在。CL 不能套用股票价格必须为正的质量规则。

玉米依据：[ZC 规格卡](https://www.cmegroup.com/trading/agricultural/files/grain-and-oilseed-futures-options-fact-card.pdf)、[Micro Agricultural 初始上市监管提交 25-024，第 1–3、6 页](https://www.cmegroup.com/content/dam/cmegroup/market-regulation/rule-filings/2025/1/25-024.pdf)、[CME 上市后报道](https://www.cmegroup.com/articles/2025/micro-agricultural-futures-how-customers-can-derive-value.html)。MZC 首个交易日为 2025-02-24，首批包括 2025 年 5/7/9/12 月合约。不得用 ZC 历史按微型乘数缩放后称为当年可执行的 MZC。

外汇依据：[2021 FX Guide](https://www.cmegroup.com/trading/fx/files/fx-product-guide-2021-us.pdf)、[2023 FX Guide，Micro EUR/USD 页](https://www.cmegroup.com/trading/fx/files/fx-product-guide-2023-us.pdf)、[2010 E-micro 规格表](https://www.cmegroup.com/trading/fx/files/FX-241_EmicroSellSheet_Updated_11_10.pdf)、[2009 年 Globex 上市公告](https://www.cmegroup.com/tools-information/lookups/advisories/electronic-trading/20090216.html)。M6E 是外汇实物交割；早期预告稿的 cash-settled 字样不能覆盖后来的实际合约说明。美元盈亏不意味着无需处理交割。6E 的 outright tick 与价差 tick 不同。

股指依据：[Micro E-mini FAQ](https://www.cmegroup.com/articles/faqs/frequently-asked-questions-micro-e-mini-equity-index-futures.html)、[Micro E-mini 规格卡](https://www.cmegroup.com/trading/equity-index/files/cme-micro-e-mini-futures-fact-card.pdf)、[CME 五周年回顾](https://www.cmegroup.com/openmarkets/equity-index/2024/After-Five-Years-Micro-Equity-Futures-Still-Gaining-Steam.html)。MES 首个交易日为 2019-05-06；ES/MES 的最终 SOQ 结算与普通收盘价不同，不能互换。表中只给 outright tick。

## 已发现的来源冲突与时间口径

1. **MZC tick 冲突**：[当前农业 FAQ](https://www.cmegroup.com/articles/faqs/faq-micro-agriculture-futures.html) 文本表把 corn tick 写成 USD 0.050/bu、标准写成 USD 0.025/bu；初始监管提交及规则明确写半美分 USD 0.005/bu。采用规则文件：500 × 0.005 = USD 2.50。供应商定义尚未取得，仍不放行实际输入。若 raw 报价为 450 美分/蒲式耳，ZC 名义金额为 450 × 50 = USD 22,500；MZC 为 450 × 5 = USD 2,250。这是单位例算，不是行情或绩效。
2. **1OZ 交易时段变更**：2025 上市通知是芝加哥时区前一日 17:00 至当日 16:00，每日一小时休市。[当前 FAQ](https://www.cmegroup.com/articles/faqs/faq-1-oz-gold-futures.html) 已更新为 24/7；[CME 技术变更公告](https://cmegroupclientsite.atlassian.net/wiki/spaces/EPICSANDBOX/pages/1618903042/1-Ounce+Gold+Futures+Expansion+to+24-7+Trading) 明确该变更首日为 2026-07-24，处于本研究窗口之外。不能回填进 2025 日历。
3. **历史模板不等于完成日历验证**：GC、MGC、HG、MHG、CL、MCL、FX 通常是 Chicago 17:00–16:00；ZC/MZC 是前日 19:00–07:45 和当日 08:30–13:20 两段；ES/MES 还存在 15:15–15:30 休市段。原 MGC 2010 上市公告是 16:15 收市、45 分钟休市，证明交易时段会变。本轮尚未重建全部 2021–2025 历史变更、节假日、提前收市和特殊中断表。[CME 交易时间与假日入口](https://www.cmegroup.com/trading-hours.html) 是后续核验来源，不是本轮已完成的日历数据库。
4. 所有时区应使用 America/Chicago、America/New_York 与 Europe/Madrid 的 IANA 转换，保留 UTC 原始时间。自然日 UTC OHLCV 不得当成交易所 session bar；分时 bar 跨越玉米 13:20 收市或 07:45 休市边界时，小时 OHLC 无法还原精确切割，应针对相关合约估价并使用必要分钟数据。
5. 每条 bar 的 close、settlement、事件时间、数据接收时间、公布可得时间要分开。当前日收盘完整不保证官方 settlement 已在下一时段开盘前可得；不能提前使用。

## 到期与换月资格

根级期限只做交叉检查：GC/MGC/HG 通常在交割月倒数第三营业日终止；1OZ/MHG 在前月倒数第三营业日；MCL 比对应 CL 提前一个营业日；ZC 在合约月 15 日前一营业日；MZC 在前月月末前至少两个营业日的周五，遇假期提前。M6E 在第三个周三前第二营业日 09:16 Chicago 终止；ES/MES 在第三个周五 08:30 Chicago 终止，假日例外须逐合约核验。以上均未生成具体日期。

本轮冻结的“最早 first notice / last trade / 券商强平边界前五个市场交易日退出”和“下一月份量连续两日超过旧月、下一时段换月”是用户研究规则，不是交易所统一规定。缺任何适用期限来源不能新开。换月必须保留两腿 raw 成交、费用与 campaign，并只用决策前同时已知的旧/新合约价格构造连续信号调整，基差不记利润。只有根规格、没有合约定义和有效交易日历，不足以执行换月。

## 保证金、盯市与费用

[CME 保证金说明](https://www.cmegroup.com/education/courses/introduction-to-futures/margin-know-what-is-needed) 区分初始和维持保证金；保证金是履约担保，不是全额购买期货名义资产。[CME 每日盯市说明](https://www.cmegroup.com/education/courses/introduction-to-futures/mark-to-market) 支持按结算价逐日转移盈亏；同一盈亏不能在 variation margin 和最终平仓再记两遍。

[CME 历史保证金 FAQ](https://www.cmegroup.com/solutions/risk-management/performance-bonds-margins/faq-performance-bonds-margins.html) 表示 2003 年以来的历史变动以 PDF 提供；例如 [GC 2020 年以后历史表](https://www.cmegroup.com/clearing/risk-management/files/GC-2020-to-present.pdf)。这是已找到的来源，**本轮未导入、未完成每个到期合约的日期匹配**，也不是用户券商 house margin 证明。不能把 2021 MCL FAQ 某一天的保证金或当前页面的数字铺满四年。10%/20% 名义金额及明确的维持比例只能标 ASSUMED_MARGIN_SCENARIO，与实际历史约束结果分表。

[IBKR 官方期货费用页](https://www.interactivebrokers.com/en/pricing/commissions-futures.php) 区分执行、交易所、清算、监管与可能的隔夜费用，且受市场和客户档位影响。没有本账户、合约和历史日期匹配证据时，registry 费率保持 UNKNOWN；用户授权的每手每边 USD 2 全包属于明示假设，不称 IBKR 实际报价。2/4 ticks 每边摩擦只是日线代理；微型低流动性标的的 2 ticks 未经报价验证，不宣称保守。不得自动按期货全名义金额收取股票融资利息；不得额外重复扣已包含的费用。现金利息为 0 的假设与实际成本分别展示。

## 数据权限、成本及定义的后续核验入口

[Databento GLBX.MDP3 入口](https://databento.com/docs/venues-and-datasets/glbx-mdp3)、[instrument definitions](https://databento.com/docs/schemas-and-data-formats/instrument-definitions)、[metadata.get_cost](https://databento.com/docs/api-reference-historical/metadata/metadata-get-cost)、[价格页](https://databento.com/pricing) 用于后续定向核验。get_cost 返回指定请求的美元估价；文档提醒非 10 分钟整倍时间范围可能高估，definitions 以整 24 小时范围估价更准确。公开额度宣传不证明当前账户余额或授权。

本分工没有使用 API key、请求有成本行情或查询券商账户。数据下载仍由主执行者在确认 DATABENTO_API_KEY、实际权限、账户已授权余额与有效期、请求成本及本地缓存后决定。只接受期货合约数据；不将 Alpaca 美股权限、ETF、现货、CFD 或模拟测试数据替代 CME 历史收益。定义至少要核对 instrument_id、raw_symbol、有效区间、证券类型、合约尺寸、tick、价格显示缩放和交割货币；具体字段以当期 vendor 文档和实际记录为准。

Databento 的 GLBX.MDP3 文档还明确：`.FUT` parent 包含交易所上市的期货价差，不能把 root parent 请求当作纯 outright 合约白名单；应先由 definitions 识别具体合约和 instrument_class，再定向请求行情。statistics 的 `ts_ref` 表示交易参考日期，虽然储存为纳秒时间戳，却没有日内时间精度，不得再按当地时区转换成前一天。`ts_event`、`ts_recv` 与该参考日期不同。结算统计存在初步/最终多次公布，`stat_flags` 区分 final、actual/theoretical、intraday；只有决策时已实际公布的记录可用，不能事后用最终消息倒填可得时间。文档指出无持仓量或成交量的合约可能没有 MDP settlement，缺失需单列，不能自动拿 close 伪装官方结算。

## 研究文献的使用边界

[AQR: Time Series Momentum](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum) 是多市场趋势研究的背景入口。本轮没有复现该论文算法或取得论文数据；更不能据此证明用户固定的 55/20 通道、ATR20、2ATR 止损、首层保证金上限和浮盈加仓组合有效。任何性能结论必须来自本任务真实输入的有限 F0/F1 矩阵；缺数据时标 NOT_RUN，而非零收益账户。
