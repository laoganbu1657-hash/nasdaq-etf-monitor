# 纳指ETF溢价轮动微信提醒

这个项目用于在 GitHub Actions 上自动监控场内纳指100ETF。

核心逻辑：

```text
相对偏离 = 当前实时溢价率 - 自己过去20个交易日溢价均值
切换优势 = 当前持仓ETF相对偏离 - 候选ETF相对偏离
```

当切换优势达到阈值时，通过 PushPlus 推送到微信。

## 1. 上传到 GitHub

1. 在 GitHub 新建一个私有仓库，例如 `nasdaq-etf-monitor`。
2. 把本文件夹里的所有文件上传到这个仓库。
3. 上传后确认仓库里有这些文件：

```text
scripts/monitor.py
scripts/update_data.py
data/nasdaq_qdii_live_premium.csv
data/nasdaq_etf_fee.csv
.github/workflows/monitor.yml
.github/workflows/update-data.yml
requirements-monitor.txt
requirements-update.txt
```

## 2. 配置 PushPlus Token

进入 GitHub 仓库：

```text
Settings -> Secrets and variables -> Actions -> Secrets -> New repository secret
```

新增：

```text
Name: PUSHPLUS_TOKEN
Value: 你的 PushPlus token
```

不要把 token 写进代码或 README。

## 3. 配置当前持仓和阈值

进入：

```text
Settings -> Secrets and variables -> Actions -> Variables -> New repository variable
```

建议新增这些变量：

```text
CURRENT_HOLDING_CODE = 159660
SWITCH_THRESHOLD = 1.2
MIN_AVG7_AMOUNT_WAN = 10000
MAX_CANDIDATE_PREMIUM = 3.0
PUSH_COOLDOWN_MINUTES = 60
```

说明：

- `CURRENT_HOLDING_CODE`：你当前持有的场内ETF代码。
- `SWITCH_THRESHOLD`：切换优势阈值，单位百分点。
- `MIN_AVG7_AMOUNT_WAN`：候选ETF最近7日日均成交额下限，单位万元；10000 = 1亿。
- `MAX_CANDIDATE_PREMIUM`：候选ETF当前溢价率上限。
- `PUSH_COOLDOWN_MINUTES`：同一组切换提醒的冷却时间。

## 4. 手动测试

进入 GitHub 仓库：

```text
Actions -> Nasdaq ETF Rotation Monitor -> Run workflow
```

如果当前不是A股交易时间，脚本会自动退出，这是正常的。

如果想在本地测试：

```bash
pip install -r requirements-monitor.txt
python scripts/monitor.py --holding 159660 --ignore-market-time --dry-run
```

## 5. 自动运行

已经配置两个自动任务：

### 交易时间监控

`.github/workflows/monitor.yml`

GitHub 会在工作日交易时间附近每10分钟运行一次。脚本内部会判断北京时间：

```text
09:45-11:30
13:00-14:55
```

不在这个时间段会自动退出。

### 每日更新数据

`.github/workflows/update-data.yml`

工作日北京时间大约：

```text
08:35
19:30
```

自动更新 `data/nasdaq_qdii_live_premium.csv` 并提交回仓库。

## 6. 当前监控池

```text
159501 嘉实纳指100
159513 大成纳指100
159632 华安纳指100
159659 招商纳指100
159660 汇添富纳指100
159696 易方达纳指100
159941 广发纳指100
513100 国泰纳指100
513110 华泰柏瑞纳指100
513300 华夏纳指100
513390 博时纳指100
513870 富国纳指100
```

## 7. 风险提醒

- 这个项目只负责提醒，不会自动交易。
- QDII净值不是实时净值，监控的是“实盘可用溢价率”。
- GitHub Actions 定时任务可能延迟几分钟，不适合秒级套利。
- PushPlus token 不要公开。
