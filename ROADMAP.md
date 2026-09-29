# ROADMAP — GitHub Actions → VPS Migration

> Status: **Draft v1** · Owner: Yoh Homie · Scope: `extractor/` runtime ကို GitHub runner ကနေ VPS ပေါ်ရွှေ့မယ်။ Worker (bot + `/urls`) က Cloudflare မှာပဲ ဆက်နေမယ်။

---

## 0. ဘာကြောင့် ရွှေ့တာလဲ (Goal)

| ပြဿနာ | ယခု | VPS ပြီးရင် |
|---|---|---|
| GH Actions heavy usage → account flag ဖြစ်နိုင် | Job တစ်ခု 5 နာရီအထိ run (အများစုက batch rest `sleep` 15–30 မိနစ်) | GH ပေါ်မှာ run မရှိတော့ဘူး၊ deploy ၁၀ စက္ကန့်ပဲ |
| Telegram session ကို run တိုင်း IP အသစ်ကနေ ဝင် | GH runner IP ပြောင်းနေတယ် → session revoke risk | VPS IP တစ်ခုတည်း တည်ငြိမ် |
| 6h hard cap | `MAX_RUN_MINUTES=300` နဲ့ ကာထားရ | Cap မရှိ (safety cap ကိုယ်ပိုင်သတ်မှတ်) |

**Non-goals:** Extraction logic ပြောင်းမယ် မဟုတ်ဘူး။ Worker endpoint contract (`/urls`, `/status`, Telegram webhook) မပြောင်းဘူး။

---

## 1. As-Is (ယခုအခြေအနေ)

```
Bot /extract ─► Worker ─► GitHub API (workflow_dispatch, GH_PAT)
                              └─► Actions runner: extract.py (Telethon)
                                     ├─ read/write Cloudflare KV (REST)
                                     └─ send urls → Bot chat
Bot /status ─► Worker ─► GitHub API (last run) + KV live_status
```

GH ကို မှီခိုနေတဲ့နေရာ (ပြောင်းရမယ့်နေရာ)
- `worker/src/index.js`: `ACCOUNTS[*].workflowFile`, `triggerWorkflow()`, `getActiveRun()`, `getStatus()` (runLine)
- `.github/workflows/extract-{vsn,nch,izm}.yml`: env tunables + secrets + `concurrency` group
- `deploy-worker.yml`: `GH_DISPATCH_TOKEN → GH_PAT` worker secret

---

## 2. To-Be (Target Architecture)

```
                    ┌──────────────── Cloudflare ────────────────┐
Bot /extract ─────► │ Worker                                     │
                    │  ├─ write  <acct>:run_request   (KV)       │
                    │  └─ read   global:runner_status (KV)       │
                    └───────────────▲──────────────┬─────────────┘
                                    │ KV REST      │ KV REST
                              (outbound only)      │
                    ┌───────────────┴──────────────▼─────────────┐
                    │ VPS  (1GB RAM + 2GB swap, Ubuntu 24.04)    │
                    │  gp-ext-dispatcher.service (systemd)       │
                    │   ├─ poll <acct>:run_request  every 30s    │
                    │   ├─ flock + spawn extract.py per account  │
                    │   └─ heartbeat → global:runner_status      │
                    │  /opt/gp-ext/current ─► releases/<sha>     │
                    └───────────────▲────────────────────────────┘
                                    │ SSH + rsync (push to main)
                          GitHub Action: deploy-vps.yml  (~10s)
```

**Principle:** VPS က **outbound connection ပဲ** သုံးတယ် (KV REST + Telegram)။ Inbound port SSH မှလွဲပြီး ဘာမှမဖွင့်ဘူး။ VPS က **stateless** — state အားလုံး KV ထဲမှာ၊ VPS ပျက်ရင် bootstrap script နဲ့ ပြန်ဆောက်လို့ရတယ်။

---

## 3. Design Decisions

| # | ဆုံးဖြတ်ချက် | ရွေးတာ | မရွေးတာ + အကြောင်းရင်း |
|---|---|---|---|
| D1 | Action ရဲ့ role | **Deploy only** | Action က SSH ဝင်ပြီး job run → Action ၅ နာရီ ဆက်ရှိနေတော့ ပြဿနာမပြေ |
| D2 | Trigger ပုံစံ | **KV poll** (VPS က ဆွဲယူ) | Worker→VPS HTTP/Tunnel: inbound port/IP ဖွင့်ရ၊ attack surface တိုး |
| D3 | Run lock | **`flock` (VPS local)** | GH `concurrency:` ကို အစားထိုးရမယ်။ Worker ရဲ့ pre-check က UX အတွက်ပဲ၊ authoritative က flock |
| D4 | Release ပုံစံ | **`releases/<sha>` + `current` symlink** | In-place overwrite: run နေတုန်း code ပြောင်းသွားနိုင် |
| D5 | Deploy နဲ့ in-flight run | **Deploy က run မသတ်ဘူး** | Run အသစ်ကသာ code အသစ်သုံး၊ ရှိပြီးသားက ဆက်ပြေး |
| D6 | Run liveness | **Dispatcher heartbeat** (`global:runner_status`) | `live_status` သုံးရင် batch rest (15–30min) အတွင်း stale ဖြစ်တတ် — extract.py ကို မထိဘဲ ဖြေရှင်းလို့ရ |
| D7 | Secrets ထားရာ | **VPS `.env` (chmod 600)** + tunables ကို repo ထဲ | Action က STRING_SESSION တွေ VPS ဆီ ပို့မနေဘူး |

---

## 4. Component Design

### 4.1 VPS Layout

```
/opt/gp-ext/
├── releases/<sha>/            # rsync ပို့တဲ့ code (နောက်ဆုံး ၃ ခု သိမ်း)
│   ├── extractor/  (extract.py, requirements.txt)
│   └── deploy/     (dispatcher.py, activate.sh, accounts/*.tunables.env)
├── current -> releases/<sha>  # atomic symlink
├── venv/                      # Python 3.12, requirements hash ပြောင်းမှ reinstall
└── shared/env/<acct>.env      # SECRETS (chmod 600, owner gpext)
/var/lib/gp-ext/locks/<acct>.lock    # flock files (restart မှာ မပျက်အောင် /run မသုံး)
```

- User: `gpext` (non-root, no password login)။ Restart အတွက်ပဲ sudoers: `gpext ALL=(root) NOPASSWD: /usr/bin/systemctl restart gp-ext-dispatcher`
- Slice: `gp-ext.slice` → `MemoryMax=800M` (1GB box မှာ OOM က sshd ကို မထိအောင်)

### 4.2 `dispatcher.py`

```
loop every 30s:
  for acct in ACCOUNTS:
      req = kv_get(f"{acct}:run_request")
      if req and not is_locked(acct):
          spawn(acct)                 # flock -n <lock> python extract.py
          kv_delete(f"{acct}:run_request")   # id ကိုက်မှ delete
      stop = kv_get(f"{acct}:stop_request")  # Phase 6
      if stop and is_locked(acct): SIGTERM → child
  every 5min (+ on state change): kv_put("global:runner_status", {...})
```

- **Spawn:** `flock -n /var/lib/gp-ext/locks/vsn.lock python extract.py` — env = `tunables.env` + `shared/env/vsn.env`, cwd = `current/extractor`
- **`is_locked()`**: lock file ကို `LOCK_NB` နဲ့ probe။ Dispatcher restart ဖြစ်ရင်လည်း လက်ရှိ run ကို lock ကနေ ပြန်သိမယ်။
- **systemd:** `Restart=always`, `KillMode=process` (dispatcher restart လုပ်လို့ extract.py မသေအောင်)
- Request idempotent: `{id, requested_at, by}` — မှားပြီး ၂ ခါ ရေးမိလည်း flock က ကာမယ်။

### 4.3 Worker Changes (`worker/src/index.js`)

| ယခု | ပြောင်းမယ့်ပုံစံ |
|---|---|
| `ACCOUNTS[k].workflowFile` | ဖယ်ရှား |
| `triggerWorkflow()` | `requestRun(env, acct)` → `<acct>:run_request` ရေး |
| `getActiveRun()` | `getRunState(env, acct)` → `global:runner_status` + heartbeat စစ် |
| `getStatus()` runLine (GH API) | Runner line: `running / idle / VPS offline` + last heartbeat |
| `GH_PAT` secret | Phase 7 မှာ ဖယ်ရှား |

`/extract` logic:
```
rs = kv_get("global:runner_status"); alive = now - rs.heartbeat < 15min
if alive && rs.accounts[acct].running → ⚠️ "run နေတုန်း" (ယခု message အတိုင်း)
elif kv_exists(<acct>:run_request)    → ⏳ "queue ထဲရှိပြီးသား"
else write run_request → ✅ "queued"  (alive=false ဆိုရင် ⚠️ "VPS offline ဖြစ်နိုင်" ထည့်ပြော)
```

### 4.4 `extract.py` — အနည်းဆုံးပြင်မယ်

1. `SIGTERM → KeyboardInterrupt` handler ထည့် (systemd stop / `/stop` က cursor state save ပြီး ရပ်နိုင်အောင်) — *လက်ရှိ interrupt path ရှိ/မရှိ Phase 3 မှာ စမ်းစစ်မယ်*
2. `MAX_RUN_MINUTES` default ကို GH-cap သဘောကနေ safety-cap သဘောသို့ (ဥပမာ 720)
3. Log က stdout → journald (`::warning::` / `::error::` prefix တွေက GH အတွက်၊ ထားလည်းရ၊ မထိဘူး)
4. ကျန်တာ **မပြောင်းဘူး**

### 4.5 CI/CD — `deploy-vps.yml`

```
on: push (paths: extractor/**, deploy/**)   + workflow_dispatch
concurrency: deploy-vps (cancel-in-progress: false)
steps:
  1. checkout
  2. write SSH key (VPS_SSH_KEY) + pin host key (VPS_KNOWN_HOSTS)   # ssh-keyscan မသုံး
  3. rsync -az --delete extractor/ deploy/ → /opt/gp-ext/releases/$SHA/
  4. ssh: deploy/activate.sh $SHA
        ├─ requirements hash ပြောင်းမှ pip install (venv)
        ├─ python -m py_compile (syntax smoke test)
        ├─ ln -sfn + mv -T  (atomic current swap)
        ├─ sudo systemctl restart gp-ext-dispatcher   (KillMode=process → run မသေ)
        └─ prune releases (နောက်ဆုံး ၃ ခုထား)
  5. (optional) Bot ဆီ "✅ deployed <sha>"
```

### 4.6 Secrets Map

| Secret | ထားမယ့်နေရာ | မှတ်ချက် |
|---|---|---|
| `VPS_HOST`, `VPS_PORT`, `VPS_USER`, `VPS_SSH_KEY`, `VPS_KNOWN_HOSTS` | GitHub Actions | **IP တစ်ခုတည်း မလုံလောက်** — private key လိုတယ်။ Deploy-only key သီးသန့်ထုတ် |
| `*_API_ID/HASH/STRING_SESSION`, `BOT_TOKEN`, `BOT_CHAT_ID`, `CF_*` | **VPS `shared/env/<acct>.env`** | Cutover ပြီး ၂ ပတ်နေမှ GH ကနေ ဖျက် (rollback အတွက်) |
| `CF_API_TOKEN`, `CF_ACCOUNT_ID`, `BOT_TOKEN`, `TG_WEBHOOK_SECRET` | GitHub (deploy-worker အတွက် ဆက်ထား) | |
| `GH_DISPATCH_TOKEN` | ဖယ်ရှား (Phase 7) | PAT ကိုလည်း revoke |

GH ကနေ VPS ကို SSH ဝင်တာမို့ **GH runner IP ပြောင်းနေတာကြောင့် IP allowlist မလုပ်လို့ရဘူး** → key-only auth + non-root + fail2ban နဲ့ ကာမယ်။

---

## 5. KV Key Contract

| Key | Writer | Reader | ရည်ရွယ်ချက် | အခြေအနေ |
|---|---|---|---|---|
| `<acct>:state` | extract | extract | cursors, seen, classifications | ရှိပြီး |
| `<acct>:urls` | extract | worker | published dataset | ရှိပြီး |
| `<acct>:live_status` | extract | worker | run progress | ရှိပြီး |
| `<acct>:excluded_groups` | worker | extract | manual skip | ရှိပြီး |
| `global:delivered_groups` | extract | extract | cross-account dedup | ရှိပြီး |
| `<acct>:run_request` | worker | dispatcher (delete) | `{id, requested_at, by}` | **အသစ်** |
| `<acct>:stop_request` | worker | dispatcher (delete) | graceful stop | **အသစ်** (Ph.6) |
| `global:runner_status` | dispatcher **တစ်ဦးတည်း** | worker | `{heartbeat, host, sha, accounts:{acct:{running,started_at}}}` | **အသစ်** |

Per-account request keys သုံးတာက shared key ကို worker/dispatcher နှစ်ဖက်က ရေးမယ်ဆိုရင် KV ရဲ့ read-modify-write race ဖြစ်မှာစိုးလို့။ `runner_status` ကတော့ writer တစ်ဦးတည်း။

**KV quota:** Paid plan ✅ (confirmed) — write/read budget ပြဿနာမရှိ။ Heartbeat 5min + state-change immediate နဲ့ပဲ ထားမယ် (မလိုဘဲ မရေးဖို့)။

---

## 6. Phases

### Phase 0 — Decisions & Prep
- [ ] VPS spec ဆုံးဖြတ်: 1 vCPU / **1GB RAM** / 20GB+ / Ubuntu 24.04 LTS
- [x] Cloudflare KV plan → **Paid** (confirmed)
- [ ] Deploy-only SSH keypair ထုတ်
- **Exit:** VPS IP + SSH ဝင်လို့ရ

### Phase 1 — VPS Bootstrap (`deploy/bootstrap.sh`)
- [ ] `gpext` user, SSH key-only (password auth off), `PermitRootLogin prohibit-password` (key-only root; လုံးဝပိတ်ချင်ရင် `DISABLE_ROOT_SSH=1`)
- [ ] `ufw` (SSH only), `fail2ban`, `unattended-upgrades`
- [ ] **`chrony`** (Telethon က time drift မကြိုက် → `bad msg` error)
- [ ] Swap 2GB (`vm.swappiness=10`), journald size limit, `bootstrap.sh verify` (PASS/FAIL check)
- [ ] Python 3 + venv (Ubuntu 24.04 default = **3.12**; telethon 1.36 / requests 2.32 နဲ့ compatible), `/opt/gp-ext` layout, `gp-ext.slice`
- **Exit:** Script ကို fresh VPS မှာ run → idempotent ဖြစ်၊ reboot ပြီး ကျန်နေ

### Phase 2 — Runtime
- [ ] `deploy/dispatcher.py` + `gp-ext-dispatcher.service`
- [ ] `deploy/accounts/{vsn,nch,izm}.tunables.env` (workflow YAML ကနေ ကူး — DAILY_LIMIT, BATCH_* စတာ)
- [ ] `shared/env/<acct>.env` (secrets) လက်နဲ့ ထည့်
- [ ] `activate.sh`
- **Exit:** VPS ပေါ်မှာ manual `flock ... python extract.py` နဲ့ VSN ကို `DAILY_LIMIT=3` နဲ့ run → KV/bot ထဲ ရောက်

### Phase 3 — Code Changes
- [ ] `extract.py`: SIGTERM handler, MAX_RUN_MINUTES default
- [ ] `worker/src/index.js`: `requestRun`, `getRunState`, `getStatus` runLine (GH API ဖယ်)
- [ ] Unit-ish test: Worker logic (alive/stale/queued/running ၄ case)
- **Exit:** Worker deploy ပြီး `/extract` → `run_request` KV ထဲ ဝင်တာ မြင်

### Phase 4 — CI/CD
- [ ] `deploy-vps.yml` + GH secrets ၅ ခု
- [ ] Test: push → VPS `current` symlink ပြောင်း၊ run နေတုန်း deploy လုပ် → run မသေဘူးဆိုတာ စစ်
- **Exit:** Push တစ်ခါ = deploy အလိုအလျောက်၊ ≤ 30s

### Phase 5 — Cutover (တစ်ခါမှာ account တစ်ခု)
1. GH `extract-vsn.yml` ကို **disable** (`gh workflow disable`) — **တပြိုင်နက် run မဖြစ်စေနဲ့** (တူညီတဲ့ session ၂ နေရာက ဝင် → revoke)
2. Bot `/extract` → VSN ကို VPS ပေါ်မှာ full run ၁ ခါ
3. ၂၄ နာရီ observe → OK ဆိုရင် NCH → IZM
4. Rollback = `gh workflow enable` + VPS dispatcher stop (Worker က GH mode ပြန်ဖြစ်ဖို့ feature flag `RUNNER_MODE=gh|vps` ထားရင် ပိုကောင်း)
- **Exit:** Account ၃ ခုလုံး VPS ပေါ်၊ ၇ ရက် ပြဿနာမရှိ

### Phase 6 — Observability & Ops
- [ ] Worker **cron trigger** (၁၅ မိနစ်တစ်ခါ): heartbeat stale ဖြစ်ရင် Bot ဆီ `🔴 VPS dispatcher offline` alert (dead-man switch)
- [ ] `/stop <acct>` command → `stop_request` → SIGTERM (graceful)
- [ ] `/status` မှာ VPS host, deployed sha, uptime ပြ
- [ ] `journalctl -u gp-ext-dispatcher` cheat-sheet ကို README ထဲထည့်

### Phase 7 — Cleanup (Cutover ပြီး ၂ ပတ်)
- [ ] `extract-*.yml` ဖျက်
- [ ] GH ထဲက extractor secrets ဖျက်
- [ ] `GH_DISPATCH_TOKEN` / `GH_PAT` ဖျက် + PAT revoke
- [ ] README architecture section ကို VPS flow နဲ့ update

---

## 7. Sizing (RAM / CPU)

| Item | ခန့်မှန်း |
|---|---|
| extract.py ၁ process (Telethon + state in-memory) | 100–200 MB |
| Account ၃ ခု တပြိုင်နက် | < 600 MB |
| Dispatcher | ~30 MB |
| `gp-ext.slice` MemoryMax | 800 MB |
| Swap | 2 GB |

**1GB ရတယ်။** အချိန်အများစု `sleep` ဖြစ်လို့ 1 vCPU လုံလောက်။ Watch: `seen_by_group` / `url_classifications` ကြီးလာရင် memory တက် → Phase 6 မှာ `/status` ထဲ RSS ထည့်ကြည့်ပါ။ Sustained 700MB+ ဖြစ်လာရင် 2GB သို့ upgrade။

---

## 8. Risks

| Risk | ဖြစ်နိုင်ခြေ | ကာကွယ်ချက် |
|---|---|---|
| Session ၂ နေရာက တပြိုင်နက်သုံး → revoke | မြင့် (cutover အတွင်း) | GH workflow ကို **အရင် disable**၊ flock |
| Deploy က run နေတဲ့ job ကို သတ် | မြင့် | symlink release + `KillMode=process` (D4/D5) |
| VPS/dispatcher သေ → run မလုပ်ဖြစ် | အလယ် | `Restart=always` + cron dead-man alert |
| KV write quota ကျော် | နိမ့် (Paid plan) | Heartbeat 5min၊ state-change ပဲ immediate |
| `.env` secrets ပေါက်ကြားမှု | နိမ့် | chmod 600, non-root, key-only SSH, deploy key သီးသန့် |
| OOM ကြောင့် sshd သေ | နိမ့် | `MemoryMax` slice + swap |
| Time drift → Telethon error | နိမ့် | chrony |
| Worker pre-check race (KV eventual) | အလယ် | Advisory ပဲ၊ authoritative က flock (D3) |

---

## 9. Open Questions

1. ~~KV plan Free/Paid?~~ → **Paid** ✅ ဖြေပြီး
2. VPS provider + region? (Telegram DC နဲ့ ဝေးရင် latency ကြီးနိုင်ပေမယ့် extract က sleep-heavy မို့ ပြဿနာကြီးမဟုတ်)
3. `RUNNER_MODE` feature flag ထည့်မလား (rollback လွယ်၊ code နည်းနည်းတိုး) — **ထည့်ဖို့ recommend**
4. Deploy ပြီးတိုင်း Bot ကို notify လုပ်မလား?
5. extract.py ရဲ့ existing interrupt path (KeyboardInterrupt) က cursor state ကို ကိုယ်တိုင် save လား? → Phase 3 မှာ verify

---

## 10. Definition of Done

- [ ] Account ၃ ခုလုံး VPS ပေါ်မှာ ၇ ရက်ဆက်တိုက် ပြဿနာမရှိ
- [ ] GH Actions ပေါ်မှာ extractor job လုံးဝ run မနေ (deploy ပဲ)
- [ ] Push → auto deploy ≤ 30s၊ run မသေ
- [ ] VPS ပျက်ရင် `bootstrap.sh` + `.env` ပြန်ထည့်ရုံနဲ့ ≤ 30 မိနစ်အတွင်း ပြန်ရ
- [ ] Dead-man alert အလုပ်လုပ်တာ စမ်းပြီး
