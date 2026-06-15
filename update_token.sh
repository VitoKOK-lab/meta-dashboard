#!/bin/bash
# Meta Long-lived Token 更新腳本
# App: Zanagems 數據追蹤（App ID: 1283256170398333）
# 使用方式：bash update_token.sh

APP_ID="1283256170398333"

echo "======================================"
echo "  Meta Token 更新工具"
echo "  App: Zanagems 數據追蹤"
echo "======================================"
echo ""

# 讀取 App Secret（不顯示輸入）
read -s -p "請輸入 App Secret（輸入不顯示）: " APP_SECRET
echo ""

# 讀取目前的舊 token
read -p "請貼上目前的 token（即將到期的那個）: " OLD_TOKEN
echo ""

echo "正在換取新的 Long-lived Token..."

# 呼叫 Meta API 換 token
RESPONSE=$(curl -s "https://graph.facebook.com/oauth/access_token?grant_type=fb_exchange_token&client_id=${APP_ID}&client_secret=${APP_SECRET}&fb_exchange_token=${OLD_TOKEN}")

# 解析新 token
NEW_TOKEN=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('access_token','ERROR'), end='')" 2>/dev/null)
EXPIRES=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); sec=d.get('expires_in',0); print(f'{sec//86400} 天' if sec else '未知', end='')" 2>/dev/null)


if [ "$NEW_TOKEN" = "ERROR" ] || [ -z "$NEW_TOKEN" ]; then
    echo ""
    echo "❌ 換 token 失敗，API 回傳："
    echo "$RESPONSE"
    exit 1
fi

echo ""
echo "✅ 成功取得新 Token！有效期：${EXPIRES}"
echo ""

# 確認是否更新 GitHub Secret
read -p "是否自動更新到 GitHub Secrets？(y/n): " CONFIRM
if [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ]; then
    gh secret set META_TOKEN -R VitoKOK-lab/meta-dashboard --body "$NEW_TOKEN"
    echo "✅ GitHub Secret 已更新！"
    echo ""
    echo "📌 建議記下更新日期：$(date '+%Y-%m-%d')，下次約 $(date -v+55d '+%Y-%m-%d' 2>/dev/null || date -d '+55 days' '+%Y-%m-%d' 2>/dev/null) 前需再更新"
else
    echo ""
    echo "新 Token（請手動貼到 GitHub Secrets）："
    echo "$NEW_TOKEN"
fi

echo ""
echo "======================================"
