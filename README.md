# 盘前雷达 Pre-Market Radar

盘前雷达是一个美股盘前分析 App。当前仓库处于 M0 骨架阶段，只包含 Streamlit 项目结构和占位页面，不连接真实数据源，不调用任何 API，也不需要任何密钥即可运行。

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

## 密钥说明

当前 M0 阶段不会读取任何 API key。将来接入 Finnhub、Alpaca、OpenAI 或 Telegram 时，请把真实密钥配置在 Streamlit Secrets 中，不要提交到 GitHub 仓库。

`.env.example` 只提供将来可能需要的 key 名称模板，所有值必须保持为空。
