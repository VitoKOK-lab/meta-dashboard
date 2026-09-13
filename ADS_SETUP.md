# 廣告代操 — 設定與授權規則

> 這條線跟原本的自然流量看板完全分開：不同 token、不同 workflow、不同資料夾。
> 原有的 `pipeline.py` / `META_TOKEN` 不受影響。

---

## 一、授權模型（寫死在 code 裡，不靠人自律）

| 動作 | 分類 | 誰能放行 |
|---|---|---|
| 關閉 ad / adset / campaign | **自動** | Claude 直接執行 |
| 降低日預算 | **自動** | Claude 直接執行 |
| 測試池內新建 ad set | **自動** | Claude 直接執行（受池上限） |
| 提高日預算 | **需核可** | 你在 GitHub 按核可 |
| 重新啟用已暫停項目 | **需核可** | 你在 GitHub 按核可 |
| 池外新建 ad set / ad | **需核可** | 你在 GitHub 按核可 |

**永遠拒絕（有核可也不行）**
- 執行後帳戶投放中總日預算 > `ADS_ACCOUNT_DAILY_CAP`
- 單次預算調幅 > `ADS_MAX_BUDGET_CHANGE_PCT`（預設 30%）
- 測試池日預算 > `ADS_TEST_POOL_DAILY_CAP`

新建的 ad set / ad 一律以 `PAUSED` 建立。要開啟是另一個「需核可」動作，不會被順手打開。

「需核可」的強制點在 GitHub `ads-approval` environment 的必要審核者，
不是程式參數 —— Claude 有能力觸發 workflow，但**沒有能力通過那道審核關卡**。

---

## 二、一次性設定（照順序做）

### 1. 開 System User Token

1. https://business.facebook.com → 左下「商業設定」
2. 使用者 → **系統使用者** → 新增，命名 `meta-dashboard-ads`，角色**管理員**
3. 該系統使用者 → **指派資產** → 廣告帳戶 → 選帳戶 → 開「**管理廣告帳戶**」
4. 再指派一次 → 資料來源 → **Pixel** → 完整控制
5. 「**產生新的存取權杖**」→ App 選「Zanagems 數據追蹤」→ 勾選：
   `ads_management`、`ads_read`、`business_management`、`read_insights`
6. System User Token 不會過期（除非改密碼或撤銷 App）

### 2. 存進 GitHub

```bash
cd ~/Documents/AIcode-claude/meta-dashboard
gh secret set META_ADS_TOKEN -R VitoKOK-lab/meta-dashboard
```

### 3. 設定護欄參數（Repository variables，非機密）

```bash
R=VitoKOK-lab/meta-dashboard
gh variable set AD_ACCOUNT_ID             -R $R --body "act_你的帳戶ID"
gh variable set ADS_ACCOUNT_DAILY_CAP     -R $R --body "帳戶總日預算天花板"
gh variable set ADS_TEST_POOL_DAILY_CAP   -R $R --body "測試池每日上限"
gh variable set ADS_MAX_BUDGET_CHANGE_PCT -R $R --body "30"
```

沒設 `ADS_ACCOUNT_DAILY_CAP` 的話 `ads_act.py` 直接拒絕啟動 —— 沒有天花板就不准動預算。

### 4. 建立審核關卡

GitHub repo → Settings → Environments → New environment → 命名 **`ads-approval`**
→ 勾選 **Required reviewers** → 加入你自己 → Save。

沒有這一步的話，「需核可」的 job 會直接跑掉，等於沒有防線。

---

## 三、日常運作

### 拉數據 / 健檢
```bash
gh workflow run ads_pull.yml -R VitoKOK-lab/meta-dashboard -f days=90
```
每日台灣時間 09:00 自動跑一次。結果進 `data/ads/`，健檢報告在 `data/ads/health_report.md`。

### 執行變更
```bash
# 先模擬，看會做什麼
gh workflow run ads_act.yml -R VitoKOK-lab/meta-dashboard \
  -f plan='[{"op":"pause_adset","id":"123","reason":"30天零轉換"}]' -f dry_run=true

# 確認後實跑
gh workflow run ads_act.yml -R VitoKOK-lab/meta-dashboard \
  -f plan='...' -f dry_run=false

# 含需核可動作（會停在 ads-approval 等你按）
gh workflow run ads_act.yml -R VitoKOK-lab/meta-dashboard \
  -f plan='...' -f dry_run=false -f include_approval_ops=true
```

所有動作留痕在 `data/ads/action_log.jsonl`，含執行結果與被拒理由。

---

## 四、資料檔案

```
data/ads/
├── account.json          帳戶基本資料
├── campaigns.json        campaign 結構
├── adsets.json           ad set 結構（含 targeting、學習期狀態）
├── ads.json              ad 與素材
├── daily_account.json    帳戶層逐日
├── daily_campaign.json   campaign 層逐日
├── daily_adset.json      ad set 層逐日（30 天）
├── daily_ad.json         ad 層逐日（30 天）
├── summary.json          各層區間彙總
├── health_report.md      健檢報告
└── action_log.jsonl      變更留痕
```

---

## 五、口徑說明

- **歸因視窗**：7 天點擊 + 1 天瀏覽（固定，避免不同報表口徑打架）
- **`until` 一律是昨天**：今日資料未結算，拿來判斷會誤導
- **購買事件同義處理**：`purchase` / `omni_purchase` / `fb_pixel_purchase` 取最大值，不重複計算
- **金額 offset**：Meta 以幣別最小單位回傳預算。TWD 用 offset=100。
  第一次跑健檢時**務必對照 Ads Manager 的總花費核對一次**，數字對不上要先修 offset 再談優化。
- **reach 不可加總**：跨日彙總時取最大值，不是相加

---

## 六、素材流程

素材由你放 Google Drive 資料夾，Claude 讀取後上傳到廣告帳戶。
素材建議（換哪支、加什麼角度）由 Claude 依 `daily_ad.json` 的疲勞度與 CTR 分佈提出，你決定要不要做。
