-- 盘前雷达 Pre-Market Radar：影子组合与每日复盘表
-- 使用方法：复制整段 SQL 到 Supabase SQL Editor 执行。

create table if not exists shadow_account (
    id bigserial primary key,
    account_date date not null default current_date,
    cash numeric(14, 2) not null default 4500.00,
    market_value numeric(14, 2) not null default 0.00,
    total_equity numeric(14, 2) generated always as (cash + market_value) stored,
    note text,
    created_at timestamptz not null default now()
);

create table if not exists shadow_positions (
    id bigserial primary key,
    ticker text not null,
    entry_price numeric(14, 4) not null,
    quantity numeric(14, 4) not null,
    entry_date date not null default current_date,
    stop_loss numeric(14, 4),
    strategy_tag text,
    note text,
    created_at timestamptz not null default now()
);

create table if not exists shadow_trades (
    id bigserial primary key,
    ticker text not null,
    side text not null check (side in ('买', '卖', '做多', '做空')),
    price numeric(14, 4) not null,
    quantity numeric(14, 4) not null,
    trade_date date not null default current_date,
    pnl numeric(14, 2) default 0.00,
    reason text,
    strategy_tag text,
    created_at timestamptz not null default now()
);

create table if not exists daily_report (
    id bigserial primary key,
    report_date date not null unique default current_date,
    daily_pnl numeric(14, 2) default 0.00,
    actions text,
    loss_analysis text,
    missed_opportunities text,
    summary text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists sector_snapshots (
    id bigserial primary key,
    trade_date date not null default current_date,
    session text not null default 'prev_close',
    data_source text not null default '未知',
    "板块" text not null,
    "代表ETF" text not null,
    "涨跌幅%" numeric(14, 6) not null default 0,
    "RVOL" numeric(14, 6) not null default 0,
    "热度分" numeric(14, 6) not null default 0,
    "热度排名" integer not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint sector_snapshots_trade_date_sector_key unique (trade_date, "板块")
);

create table if not exists sector_leader_snapshots (
    id bigserial primary key,
    trade_date date not null default current_date,
    session text not null default 'prev_close',
    data_source text not null default '未知',
    "板块" text not null,
    "代码" text not null,
    "涨跌幅%" numeric(14, 6) not null default 0,
    "成交量" numeric(20, 2) not null default 0,
    "RVOL" numeric(14, 6) not null default 0,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint sector_leader_snapshots_trade_date_sector_ticker_key unique (trade_date, "板块", "代码")
);

-- 如果已经执行过旧版快照表 SQL，这几句会把旧表补齐到诚实口径。
alter table sector_snapshots add column if not exists snapshot_date date;
alter table sector_snapshots add column if not exists trade_date date;
alter table sector_snapshots add column if not exists session text not null default 'prev_close';
alter table sector_snapshots add column if not exists data_source text not null default '未知';
update sector_snapshots set trade_date = coalesce(trade_date, snapshot_date, current_date) where trade_date is null;
alter table sector_snapshots alter column trade_date set default current_date;
alter table sector_snapshots alter column trade_date set not null;

alter table sector_leader_snapshots add column if not exists snapshot_date date;
alter table sector_leader_snapshots add column if not exists trade_date date;
alter table sector_leader_snapshots add column if not exists session text not null default 'prev_close';
alter table sector_leader_snapshots add column if not exists data_source text not null default '未知';
update sector_leader_snapshots set trade_date = coalesce(trade_date, snapshot_date, current_date) where trade_date is null;
alter table sector_leader_snapshots alter column trade_date set default current_date;
alter table sector_leader_snapshots alter column trade_date set not null;

create index if not exists idx_shadow_account_date on shadow_account(account_date desc);
create index if not exists idx_shadow_positions_ticker on shadow_positions(ticker);
create index if not exists idx_shadow_trades_date on shadow_trades(trade_date desc);
create index if not exists idx_daily_report_date on daily_report(report_date desc);
create unique index if not exists idx_sector_snapshots_trade_date_sector on sector_snapshots(trade_date, "板块");
create unique index if not exists idx_sector_leader_snapshots_trade_date_sector_ticker on sector_leader_snapshots(trade_date, "板块", "代码");
create index if not exists idx_sector_snapshots_date_rank on sector_snapshots(trade_date desc, "热度排名" asc);
create index if not exists idx_sector_leader_snapshots_date on sector_leader_snapshots(trade_date desc);

-- 权限修复：允许 Streamlit 使用 anon key 通过 REST API 读写影子组合数据。
-- 使用方法：如果表已经创建过，也可以单独复制本段到 Supabase SQL Editor 执行。
grant usage on schema public to anon, authenticated;

grant select, insert, update, delete on table shadow_account to anon, authenticated;
grant select, insert, update, delete on table shadow_positions to anon, authenticated;
grant select, insert, update, delete on table shadow_trades to anon, authenticated;
grant select, insert, update, delete on table daily_report to anon, authenticated;
grant select, insert, update, delete on table sector_snapshots to anon, authenticated;
grant select, insert, update, delete on table sector_leader_snapshots to anon, authenticated;

grant usage, select on sequence shadow_account_id_seq to anon, authenticated;
grant usage, select on sequence shadow_positions_id_seq to anon, authenticated;
grant usage, select on sequence shadow_trades_id_seq to anon, authenticated;
grant usage, select on sequence daily_report_id_seq to anon, authenticated;
grant usage, select on sequence sector_snapshots_id_seq to anon, authenticated;
grant usage, select on sequence sector_leader_snapshots_id_seq to anon, authenticated;

alter table shadow_account enable row level security;
alter table shadow_positions enable row level security;
alter table shadow_trades enable row level security;
alter table daily_report enable row level security;
alter table sector_snapshots enable row level security;
alter table sector_leader_snapshots enable row level security;

drop policy if exists shadow_account_anon_all on shadow_account;
create policy shadow_account_anon_all on shadow_account
    for all to anon, authenticated
    using (true)
    with check (true);

drop policy if exists shadow_positions_anon_all on shadow_positions;
create policy shadow_positions_anon_all on shadow_positions
    for all to anon, authenticated
    using (true)
    with check (true);

drop policy if exists shadow_trades_anon_all on shadow_trades;
create policy shadow_trades_anon_all on shadow_trades
    for all to anon, authenticated
    using (true)
    with check (true);

drop policy if exists daily_report_anon_all on daily_report;
create policy daily_report_anon_all on daily_report
    for all to anon, authenticated
    using (true)
    with check (true);

drop policy if exists sector_snapshots_anon_all on sector_snapshots;
create policy sector_snapshots_anon_all on sector_snapshots
    for all to anon, authenticated
    using (true)
    with check (true);

drop policy if exists sector_leader_snapshots_anon_all on sector_leader_snapshots;
create policy sector_leader_snapshots_anon_all on sector_leader_snapshots
    for all to anon, authenticated
    using (true)
    with check (true);
