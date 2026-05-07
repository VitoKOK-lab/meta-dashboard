# meta-dashboard — 換機/換資料夾快速上手

> 整套自動化在 GitHub 雲端跑，**這台電腦只是備份 + 操作工具**。
> 換電腦 = 重新 clone + 重新登入 GitHub CLI 即可，沒有任何遷移步驟。

---

## 一、線上是什麼狀態（永遠是真相來源）

- **GitHub Repo**：`VitoKOK-lab/meta-dashboard` (private)
- **線上儀表板**：https://vitokok-lab.github.io/meta-dashboard/
- **Token**：存在 GitHub repo Secrets 的 `META_TOKEN`，本機看不到內容
- **自動化排程**（`.github/workflows/daily.yml`）：
  - 例行：台灣時間 **07/11/15/19**（每 4 小時，深夜 23-07 不跑）
  - 週一 00:00：90 天完整重算
  - 每月 1 日 / 20 日 02:00：拉 1 年內所有歷史影片
- **直播追蹤**（`.github/workflows/monitor_live.yml`）：手動觸發，直播結束後跑

---

## 二、新電腦第一次設定

### 1. 裝必要工具
```bash
# Mac (用 Homebrew)
brew install git gh
# 若要本機跑 pipeline.py 才需要
brew install python@3.11
```

### 2. 登入 GitHub CLI
```bash
gh auth login
# 選 GitHub.com → HTTPS → Login with a web browser
# 跳出瀏覽器點同意即可
```

### 3. Clone repo
```bash
mkdir -p ~/Documents/AIcode-claude && cd ~/Documents/AIcode-claude
gh repo clone VitoKOK-lab/meta-dashboard
cd meta-dashboard
chmod +x update_token.sh
```

完成。線上自動化照常跑，這台機器隨時開關都不影響儀表板。

---

## 三、日常操作備忘

### 看儀表板
直接打開 https://vitokok-lab.github.io/meta-dashboard/

### 看自動化跑得順不順
```bash
gh run list -R VitoKOK-lab/meta-dashboard --limit 10
```

### 手動補跑一次（資料想立刻刷新）
```bash
gh workflow run daily.yml -R VitoKOK-lab/meta-dashboard
```

### 直播結束後抓收尾數據
```bash
gh workflow run monitor_live.yml -R VitoKOK-lab/meta-dashboard
```

### 拉最新版本下來（線上 Actions 會自動 commit 資料更新）
```bash
cd ~/Documents/AIcode-claude/meta-dashboard
git pull --rebase
```

### 改了程式碼想推上去
```bash
git add <檔案>
git commit -m "說明這次改了什麼"
git pull --rebase   # 先把線上 Actions commit 的合併進來
git push
```

---

## 四、Meta Token 過期更新流程（約每 60 天一次）

### 1. 拿新 Long-lived Token
- 進 https://developers.facebook.com/tools/explorer/
- 選 App → Page → Generate Access Token
- 用 Access Token Debugger 點「Extend Access Token」換 long-lived
- 確認 Page Token 通常會顯示「永不過期」

### 2. 跑腳本
```bash
cd ~/Documents/AIcode-claude/meta-dashboard
./update_token.sh        # 互動式貼 token，不會 echo
```

腳本會自動：驗證 token → 推到 GitHub Secret → 詢問是否立刻觸發測試。

---

## 五、檔案結構（僅供理解，不用記）

```
meta-dashboard/
├── pipeline.py            # 主程式：抓 Meta API → 寫 data/ → 重建 index.html
├── template.html          # 儀表板模板
├── index.html             # 渲染後的儀表板（GitHub Pages 服務這個）
├── data/
│   ├── videos.json        # 影片資料庫（增量更新）
│   ├── archive.json       # 15 天以上趨穩的長期歸檔
│   ├── follower_history.json  # 90 天每日粉絲快照
│   └── lives.json         # 直播紀錄
├── update_token.sh        # ↑ 第四節在用的腳本
├── requirements.txt       # python 依賴（只有 requests）
└── .github/workflows/
    ├── daily.yml          # 例行抓取 + weekly + history
    └── monitor_live.yml   # 直播後手動觸發
```

---

## 六、提醒事項（macOS 提醒事項 App）

- **2026-06-12 09:00**：更新 Meta META_TOKEN（提前 7 天提醒）
- 換電腦時記得重新建一個提醒（用 `osascript` 或手動建）

---

## 七、出狀況怎麼查

### 自動化失敗
```bash
gh run view <run_id> -R VitoKOK-lab/meta-dashboard --log-failed
```

常見原因：
| 訊息 | 原因 | 處理 |
|---|---|---|
| `Runner of type hosted ... not acquired` | GitHub 自己掛了 | 不用管，下次排程會自動跑 |
| `META_TOKEN ... 401/403` | Token 過期 | 跑 `./update_token.sh` 換新 token |
| `rate limit` | API 打太快 | 通常自動恢復，連續幾次失敗才需處理 |

### 確認 token 還有效
```bash
# 不貼 token：去 https://developers.facebook.com/tools/debug/accesstoken/ 貼 secret 內容
# 或要更新時直接跑 ./update_token.sh，腳本會驗證並印出 expires_at
```

---

## 八、絕對不要做的事

- ❌ **不要把 Token 寫進任何 .py / .txt / .md 然後 commit** — 會永遠留在 git 歷史
- ❌ **不要手動編輯 `data/videos.json`** — 會被下次 Actions 覆蓋
- ❌ **不要 force push** — Actions 也在推這個 repo，會撞車
- ❌ 改完 code **不要忘記 `git pull --rebase`** 再 push（線上 Actions 一直在 commit 資料）
