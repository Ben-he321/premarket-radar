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

create index if not exists idx_shadow_account_date on shadow_account(account_date desc);
create index if not exists idx_shadow_positions_ticker on shadow_positions(ticker);
create index if not exists idx_shadow_trades_date on shadow_trades(trade_date desc);
create index if not exists idx_daily_report_date on daily_report(report_date desc);
