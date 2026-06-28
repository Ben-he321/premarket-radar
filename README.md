# 盘前雷达 Pre-Market Radar

盘前雷达是一个美股盘前分析 App。当前仓库处于 M1 阶段，已经在「晨报」页接入 Finnhub 免费数据，用固定股票池展示盘前/最新行情 gap scanner。

## 本地运行

1. 安装 Python 3.10 或更高版本。
2. 在项目目录安装依赖：

```bash
pip install -r requirements.txt
```

3. 启动 Streamlit：

```bash
streamlit run app.py
```

4. 浏览器打开终端提示的网址，通常是 `http://localhost:8501`。

## 部署到 Streamlit Community Cloud

1. 将本仓库推送到 GitHub。
2. 打开 [Streamlit Community Cloud](https://streamlit.io/cloud)。
3. 点击 `New app`。
4. 选择 GitHub 仓库 `Ben-he321/premarket-radar`。
5. Branch 选择 `main`，Main file path 填写 `app.py`。
6. 点击 `Deploy`，无需额外配置即可启动。

## Finnhub API Key 配置

M1 阶段需要 Finnhub API Key 才能显示真实行情。代码不会保存、打印或展示你的 key。

Streamlit Community Cloud 部署时，请在 App 的 `Secrets` 中添加：

```toml
FINNHUB_API_KEY = "你的 Finnhub API Key"
```

本地开发时，可以使用环境变量：

```bash
FINNHUB_API_KEY=你的 Finnhub API Key
```

也可以复制 `.env.example` 为 `.env`，然后只在本机填写 key。`.env` 已加入 `.gitignore`，不要提交到仓库。

如果没有配置 key，页面会显示友好提示，不会崩溃。

## 密钥说明

真实 API key 必须配置在 Streamlit Secrets 或本地环境变量中，不要提交到 GitHub 仓库。

`.env.example` 只提供将来可能需要的 key 名称模板，所有值必须保持为空。
