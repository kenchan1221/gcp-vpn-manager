# 從零建立 GCP WireGuard VPN

這份教學先建立 **一台** Ubuntu 24.04 VM。一台就能供電腦與手機使用；
需要不同地區出口時才新增伺服器，地區與名稱可依需求選擇。
Windows 管理工具只負責控制你自己的 VM 和本機 WireGuard，不提供免費伺服器。

## 0. 先確認費用與權限

- 建立 Google Cloud 專案、連結帳單帳戶，記下 **Project ID**，不是專案顯示名稱。
- 設定 Billing budget / alerts；**預算通知不是費用硬上限**。
- 開機時會有 VM、對外 IPv4、網路流量等費用；關機後磁碟、保留 IP 等仍可能計費。
- 不要假設各地區的 `e2-micro` 都免費。Free Tier 的地區、時數、流量和資源限制
  以[官方規則](https://cloud.google.com/free/docs/free-cloud-features#compute)為準。
- 建立基礎設施需啟用 API、管理網路/VM/IAM 的權限；初始部署管理員與日常
  Windows 控制器應分開考慮，不要為了方便給日常控制器 Owner。
- 以下 `gcloud` 範例在 **Google Cloud Shell（Bash）** 執行。
  Windows 本機登入指令則在後面的 PowerShell 步驟執行。
- 組織環境請先詢問管理員；外部 IP、VM 地區、金鑰建立可能受組織政策限制。
  不要為了這個工具直接停用整個組織的防護政策。

## 1. 建立隔離 VPC、單一 UDP 防火牆規則

Cloud Console 右上角開啟 Cloud Shell，先更換下列值：

```bash
export PROJECT_ID="your-project-id"
export REGION="asia-east1"
export ZONE="asia-east1-b"
export VM="vpn-one"
export NETWORK="vpn-network"
export SUBNET="vpn-subnet"
export UDP_PORT="51820"

gcloud config set project "$PROJECT_ID"
gcloud services enable compute.googleapis.com iap.googleapis.com

gcloud compute networks create "$NETWORK" --subnet-mode=custom
gcloud compute networks subnets create "$SUBNET" \
  --network="$NETWORK" --region="$REGION" --range=10.60.0.0/24

gcloud compute firewall-rules create vpn-wireguard-in \
  --network="$NETWORK" --direction=INGRESS --action=ALLOW \
  --rules="udp:$UDP_PORT" --source-ranges=0.0.0.0/0 \
  --target-tags=wireguard-vpn

gcloud compute firewall-rules create vpn-iap-ssh \
  --network="$NETWORK" --direction=INGRESS --action=ALLOW \
  --rules=tcp:22 --source-ranges=35.235.240.0/20 \
  --target-tags=wireguard-vpn
```

**防火牆設定說明：**

- 只開一個 UDP 埠（例如 `51820`），不開整段 `50000-65535`。
- 只套用 `wireguard-vpn` tag，不套用 VPC 所有 VM。
- WireGuard 各 VM 可以共用同一個埠：外部 IP 不同，不會衝突。
- UDP 對 `0.0.0.0/0` 是為了手機/外出網路的變動來源 IP；WireGuard 仍需正確金鑰。
  若來源固定，可以縮小範圍。
- SSH 只透過 IAP 的來源範圍，不直接向全網開 TCP 22。

以上子網不得與你的既有 VPC/公司網路衝突；WireGuard 稍後使用 `10.77.0.0/24`，
也不能與本機網路/VPC 重疊。

## 2. 建立一台 VM

```bash
gcloud compute instances create "$VM" \
  --zone="$ZONE" --machine-type=e2-micro \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --network="$NETWORK" --subnet="$SUBNET" \
  --tags=wireguard-vpn --can-ip-forward \
  --boot-disk-size=10GB --boot-disk-type=pd-standard \
  --metadata=enable-oslogin=TRUE \
  --no-service-account --no-scopes \
  --labels=app=gcp-vpn-manager
```

這是 **x86-64 Ubuntu** 雲端主機；Windows 客戶端是 x64 或 x86 和伺服器架構無關。
`e2-micro` 適合先試用，但高吞吐/多人用可能需要升級；它不保證某個網速。
VM 不需要服務帳戶，Windows 控制器會用自己的 Google 身分呼叫 Compute API。

GCP 的 `--can-ip-forward` 與 Linux 核心 `net.ipv4.ip_forward=1` 是不同設定；
本教學兩者都開啟。**光開 GCP 選項不會自動幫 Linux 啟用 NAT/轉送。**

IAP SSH + OS Login 使用者需要相應權限，例如
`roles/iap.tunnelResourceAccessor`、`roles/compute.osAdminLogin`
以及讀取專案/VM 的權限；由管理員授予，不要為了登入改成全網 SSH。
參考 [IAP TCP forwarding](https://cloud.google.com/iap/docs/using-tcp-forwarding)
和 [OS Login](https://cloud.google.com/compute/docs/oslogin/set-up-oslogin)。

查看外部 IP：

```bash
gcloud compute instances describe "$VM" --zone="$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

記下輸出。若沒有外部 IP，先修正 VM/組織網路設定，本工具不能靠內部 IP 遠端連入。

## 3. 在 Ubuntu 安裝 WireGuard

在 Cloud Shell 取得本專案：

```bash
git clone https://github.com/kenchan1221/gcp-vpn-manager.git
cd gcp-vpn-manager
```

從專案根目錄執行以下指令，只傳送安裝腳本，不傳送任何既有私鑰：

```bash
gcloud compute scp server/setup-server.sh server/add-peer.sh \
  "$VM:~/" --zone="$ZONE" --tunnel-through-iap
gcloud compute ssh "$VM" --zone="$ZONE" --tunnel-through-iap
```

接著是在 **VM 的 SSH 終端機**，先閱讀腳本，再執行。將埠號改成第 1 步的值：

```bash
less setup-server.sh
less add-peer.sh
sudo bash setup-server.sh 51820
sudo systemctl status wg-quick@wg0 --no-pager
sudo wg show
```

腳本會安裝 Ubuntu 套件庫的 `wireguard`、`iptables`、`qrencode`，
產生伺服器金鑰、建立 `wg0`、設定 IPv4 forwarding / NAT 並開機自動啟動。
它會拒絕覆寫既有 VPN 設定，且不是第三方遠端 `curl | bash` 安裝器。
僅適用新的 Ubuntu 24.04；若已有 UFW、其他 VPN、防火牆或路由，先人工整合，
不要直接執行。NAT 規則會讓受信任 peer 透過 VM 對外存取；不是不受信任租戶隔離方案。

**IPv6 限制：**本教學只提供 IPv4 網際網路出口。客戶端的 `::/0` 仍導向 VPN，
但伺服器不提供 IPv6 egress，因此 IPv6-only 網站不可用；不要為了「修好 IPv6」
隨意移除 `::/0`，否則可能讓 IPv6 流量直接從本機網路離開 VPN。

## 4. 各裝置建立獨立 peer

仍在 VM 上。替換 `PUBLIC_IPV4` 為第 2 步讀到的**外部 IPv4**：

```bash
sudo bash add-peer.sh pc 2 PUBLIC_IPV4
sudo bash add-peer.sh phone 3 PUBLIC_IPV4
```

這兩個 peer 分別使用 `10.77.0.2`、`10.77.0.3`，有各自的 PrivateKey /
PublicKey / PresharedKey。每個新裝置使用未用過的名稱和 host number（2-254）。
重複名稱或地址會被拒絕。伺服器端設定永久保存，不需中斷其他 peer 來加入。

PC profile 是 `/etc/wireguard/clients/pc.conf`。
為了透過自己的 SSH 使用者下載，在 VM 上建立僅自己可讀的暫存副本：

```bash
sudo install -m 600 -o "$USER" /etc/wireguard/clients/pc.conf "$HOME/pc.conf"
```

退出 SSH 回 Cloud Shell：

```bash
exit
mkdir -p "$HOME/vpn-private"
chmod 700 "$HOME/vpn-private"
gcloud compute scp "$VM:~/pc.conf" "$HOME/vpn-private/pc.conf" \
  --zone="$ZONE" --tunnel-through-iap
chmod 600 "$HOME/vpn-private/pc.conf"
cloudshell download "$HOME/vpn-private/pc.conf"
```

下載到 Windows 的私人資料夾，**不要放進 public repo**。確認安全備份後，可以
人工清理 Cloud Shell/VM home 的這兩份暫存副本；不要刪除伺服器正在使用的設定。
也不要在公開截圖、issue、log 或聊天分享 `.conf` 內容。

手機安裝官方 WireGuard App，在 VM 的私人 SSH 畫面顯示 QR：

```bash
sudo sh -c 'qrencode -t ansiutf8 < /etc/wireguard/clients/phone.conf'
```

QR 包含手機私鑰，和 `.conf` 一樣敏感。手機掃描後保存。
**不要把 PC profile 匯入手機重複使用**，兩台裝置同 key 可能互相搶 endpoint。

## 5. 給 Windows 控制器最小必要權限

回到 Cloud Shell，以有權建立 custom role 的管理員身分：

```bash
gcloud iam roles create vpnInstanceController --project="$PROJECT_ID" \
  --title="VPN instance controller" \
  --description="Read/start/stop VPN VMs and read zonal operations" \
  --permissions=compute.instances.get,compute.instances.start,compute.instances.stop,compute.zoneOperations.get \
  --stage=GA
```

日常使用的 Google 帳戶取代下列 `you@example.com`：

```bash
export CONTROLLER_USER="you@example.com"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$CONTROLLER_USER" \
  --role="projects/$PROJECT_ID/roles/vpnInstanceController"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="user:$CONTROLLER_USER" --role=roles/serviceusage.serviceUsageConsumer
```

以上範例是**專案範圍**，會讓該身分控制該專案所有 VM；
建議使用獨立 VPN 專案。若必須與其他工作負載共用專案，
請管理員設計資源層 IAM / Conditions，並同時處理 zonal operation 讀取權限。
此範例沒有將控制權限限制在單一 VM。

### 推薦：ADC，不下載服務帳戶金鑰

Windows 安裝 [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)，
使用與 EXE 相同的 Windows 帳戶在 PowerShell 執行：

```powershell
gcloud auth application-default login
gcloud auth application-default set-quota-project YOUR_PROJECT_ID
```

第一次瀏覽器登入 Google 帳戶；之後 SDK 自動刷新短期 token。
不需要每次開工具重新登入。若憑證被撤銷/過期，重新執行登入指令即可。
`gcloud auth login` 與 ADC 不同，只有前者不夠。

ADC refresh token 也屬於敏感資料，存在 Google SDK 的本機私人設定目錄。
不要把它當成可公開檔案，也不要上傳 Google SDK 設定目錄。

### 可選：服務帳戶 JSON（不是必需）

若你的組織/使用情境允許，建立專用服務帳戶，授予上述 custom role；
僅在政策允許時產生它的 JSON key，保存到安全的私人資料夾。
在設定精靈選擇 **Service-account JSON key** 並匯入；工具加密保存內容。

不要因為金鑰建立被封鎖就照網路文章停用整個組織政策。
這時優先使用 ADC，或請管理員提供符合組織政策的認證方式。
不建議直接給 `roles/compute.instanceAdmin.v1`，因為它比本工具所需權限大得多。
長期金鑰一旦外洩，需到 IAM 撤銷/刪除該 key 並重新配置，不是只從 repo 刪掉就安全。

## 6. 第一次啟動 Windows 工具

1. 安裝 [WireGuard for Windows](https://www.wireguard.com/install/)。
2. 選擇 x64 或 x86 EXE，先比對 `releases/SHA256SUMS.txt`。
3. 啟動 EXE，批准管理員權限（用來管理本機 WireGuard service）。
4. 設定精靈輸入：
   - Project ID，例如你第 0 步記下的 ID。
   - **ADC**（推薦），或匯入自己的 JSON key。
   - 顯示名稱，例如「My VPN」。
   - Zone，例如 `asia-east1-b`，不是只填 `asia-east1`。
   - Instance name，例如 `vpn-one`。
   - **Import WireGuard .conf** 選擇下載的 **PC** profile。
5. 只用一台：直接 **Save settings**。多台：每次 **Add / update server**，
   然後 **New server** 填另一台；最後儲存。
6. **Connect this PC**：開 VM、讀目前外部 IP、替換 Endpoint 的主機部分，
   保留原 UDP port、啟動本機通道。

直接匯入一般 WireGuard `.conf` 即可，不需另外製作樣板或手動寫入 `{IP}`。
程式使用設定精靈中儲存的專案、VM 與認證資訊。
設定加密保存於 `%LOCALAPPDATA%\GcpVpnManager\settings.dat`，下次啟動直接讀取。

**保護界線：**DPAPI 綁定目前 Windows 帳戶/機器，不適合直接拷貝到別台。
`gvpn-*.conf` 是 WireGuard service 重啟仍需讀取的 runtime profile，保存在同一個
僅目前使用者/SYSTEM 可讀的目錄，不是程式另外加密。請保護本機帳戶、磁碟及備份。

## 7. 日常操作與手機

| 按鈕 | 行為 |
|---|---|
| Connect this PC | 先移除舊通道，暫用一般網路連 Cloud API，再開啟選定 VPN |
| Start VM only | 只開 VM，不變更本機 WireGuard，可供手機使用 |
| Disconnect PC (keep VM) | 只中斷本機；不影響手機，不關 VM |
| Stop VM | 二次確認後關閉 VM；所有裝置斷線 |
| Settings | 沒有本工具通道執行時可編輯/新增/移除；不刪除雲端 VM |
| Cloud Console | 開啟所選 VM 的管理頁面 |
| Open logs | 開啟七天保留的本機診斷日誌 |

關閉視窗**不是關機**，切換另一個節點也**不會**關掉舊 VM。
電腦關機時，手機可以用瀏覽器登入 Google Cloud Console 啟動 VM，
不需要先連 VPN。這版沒有獨立手機控制網站。

如果 VM 使用 ephemeral IP，停止/啟動後 IP 可能改變：
PC 按 Connect 會自動更新；手機需手動更新 Endpoint，保持同樣 port。
想省去手機更新，可先在 GCP 預留並指派 static external IPv4，
但請先看 IPv4 定價，不要假設保留/未使用 IP 免費。

## 8. 驗證與故障排除

先確認以下三個層次，不要把服務 RUNNING 當成 VPN 一定正常：

1. **Cloud**：VM RUNNING、外部 IP 正確。
2. **WireGuard**：`sudo wg show` 看手機/PC peer 是否有近期 handshake、傳輸位元組增加。
3. **實際網路**：開網站測 DNS、查詢出口 IP 是否等於所選 VM；IPv4-only 的本教學
   不提供原生 IPv6 網際網路。

| 症狀 | 檢查 |
|---|---|
| HTTP 403 | ADC/JSON 的身分是否正確、有 custom role、Compute API 已啟用、quota project 可用 |
| 無法連上 VM | GCP tag、UDP port、Endpoint 外部 IP、VM `wg-quick@wg0` 狀態 |
| 服務已啟動但上不了網 | server handshake、Linux forwarding/NAT、DNS、重複 peer key、其他 VPN 路由 |
| 隔夜重連 DNS/reset 錯誤 | 先 Disconnect PC 保留 VM，再 Connect；程式會重建 HTTP connection 並等待網路 |
| 頻繁斷線 | 每台裝置不同 key/address，檢查 `PersistentKeepalive = 25`、NAT、防火牆 |
| 部分網站卡住 | 路徑 MTU；本教學使用 1380 作保守起點，依自己的網路調整 |
| 找不到 WireGuard | 安裝官方 Windows 版本；程式會檢查原生/32-bit Program Files |
| 不斷要求首次設定 | 是否使用不同 Windows 帳戶提升權限、設定檔是否被移除 |

程式檢查的是 Google DNS/TLS 可達性，**不是**直接驗證 WireGuard handshake。
網路讀取會有限次重試；不會默默重複啟動/關閉 VM。
持續失敗會顯示錯誤。日誌的金鑰/token 會遮蔽，但 project、IP、路徑等仍需自行
檢查後再分享。不要上傳整個 AppData、runtime `.conf` 或原始 JSON key。

## 9. 增加第二台（可選）

先確認真的需要其他出口地區。不同地區要建立該地區的子網，
選擇不重疊 CIDR、region、zone，再重複第 2-4 步。
相同 VPC 的防火牆可重用同一 tag/UDP port。每台 VM 要各自安裝、
產生自己的 server key / PC peer；不能把 A 的 PC conf 拿來當 B 的 conf。
在工具 Settings 新增一列即可。不使用的 VM 要手動停止，避免忘記計費。

## 10. 成本收尾與資源管理

停止 VM 不等於刪除資源。確定不再使用後，在 Cloud Console 人工檢查
VM、磁碟、保留 external IP、防火牆、服務帳戶 key 等資源，再決定是否刪除。
**本教學不提供一鍵刪除專案/整個網路的破壞性腳本。**

## 參考官方文件

- [停止/啟動 Compute Engine](https://cloud.google.com/compute/docs/instances/stop-start-instance)
- [VPC firewall rules](https://cloud.google.com/firewall/docs/firewalls)
- [IP forwarding](https://cloud.google.com/vpc/docs/using-routes#canipforward)
- [本機 ADC](https://cloud.google.com/docs/authentication/set-up-adc-local-dev-environment)
- [Service account key best practices](https://cloud.google.com/iam/docs/best-practices-for-managing-service-account-keys)
- [WireGuard quick start](https://www.wireguard.com/quickstart/)
- [Google Cloud pricing calculator](https://cloud.google.com/products/calculator)
