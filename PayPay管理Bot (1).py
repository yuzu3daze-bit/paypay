import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import os
import requests
from datetime import datetime

# ===== 設定 =====
DISCORD_TOKEN = "MTQ3ODU5OTY5NDQwMDc0OTU2OA.GRmKsX.WPhZJd7lgtLQPf8q3PIwf6RkRg0GV11G3Gljfk"
LICENSE_KEY   = "7174315f-7581-4ae9-b1fa-db34cf4bcde9"
LOGIN_API_URL = "https://plogin-api.xvps.jp"
USER_DATA_DIR  = "solo_user_data"
ALLOWED_USERS  = [911353660414492752]  # 使用を許可するDiscordユーザーID

# ===== アクセス制御 =====
def is_whitelisted(user_id: int) -> bool:
    return user_id in ALLOWED_USERS

# ===== ユーザー別トークン管理 =====
def user_dir(user_id: int) -> str:
    path = os.path.join(USER_DATA_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path

def user_tokens_path(user_id: int) -> str:
    return os.path.join(user_dir(user_id), "tokens.json")

def load_user_data(user_id: int) -> dict:
    path = user_tokens_path(user_id)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"active": None, "accounts": {}}

def save_user_data(user_id: int, data: dict):
    with open(user_tokens_path(user_id), "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_active_tokens(user_id: int) -> tuple:
    data = load_user_data(user_id)
    name = data.get("active")
    if not name or name not in data.get("accounts", {}):
        return None, None
    return name, data["accounts"][name]

def save_account(user_id: int, name: str, tokens: dict):
    data = load_user_data(user_id)
    if "accounts" not in data:
        data["accounts"] = {}
    data["accounts"][name] = tokens
    if data.get("active") is None:
        data["active"] = name
    save_user_data(user_id, data)

def set_active(user_id: int, name: str) -> bool:
    data = load_user_data(user_id)
    if name not in data.get("accounts", {}):
        return False
    data["active"] = name
    save_user_data(user_id, data)
    return True

def delete_account(user_id: int, name: str) -> bool:
    data = load_user_data(user_id)
    if name not in data.get("accounts", {}):
        return False
    del data["accounts"][name]
    if data.get("active") == name:
        remaining = list(data["accounts"].keys())
        data["active"] = remaining[0] if remaining else None
    save_user_data(user_id, data)
    return True

def list_accounts(user_id: int) -> list:
    return list(load_user_data(user_id).get("accounts", {}).keys())

# ===== PayPayクライアント取得 =====
def get_paypay(user_id: int):
    from PayPaython_mobile import PayPay, PayPayLoginError
    name, tokens = get_active_tokens(user_id)
    if not tokens or not tokens.get("access_token"):
        return None, None, None
    try:
        pp = PayPay(access_token=tokens["access_token"])
        return pp, tokens, name
    except PayPayLoginError:
        return _try_refresh(user_id, name, tokens)

def _try_refresh(user_id: int, name: str, tokens: dict):
    from PayPaython_mobile import PayPay, PayPayLoginError
    try:
        pp = PayPay(access_token=tokens["access_token"])
        pp.token_refresh(tokens["refresh_token"])
        new_tokens = {
            "access_token":  pp.access_token,
            "refresh_token": pp.refresh_token,
            "device_uuid":   tokens.get("device_uuid", ""),
        }
        save_account(user_id, name, new_tokens)
        return pp, new_tokens, name
    except Exception:
        return None, None, None

# ===== ログイン代行API =====
def login_step1(phone: str, password: str, device_uuid: str = None) -> dict:
    payload = {"phone": phone, "password": password}
    if device_uuid:
        payload["device_uuid"] = device_uuid
    r = requests.post(
        f"{LOGIN_API_URL}/login",
        headers={"Content-Type": "application/json", "X-License-Key": LICENSE_KEY},
        json=payload, timeout=15
    )
    return r.json()

def login_step2(session_id: str, auth_url: str) -> dict:
    r = requests.post(
        f"{LOGIN_API_URL}/login/complete",
        headers={"Content-Type": "application/json", "X-License-Key": LICENSE_KEY},
        json={"session_id": session_id, "auth_url": auth_url},
        timeout=15
    )
    return r.json()

# ===== WLチェックデコレータ =====
async def check_whitelist(interaction: discord.Interaction) -> bool:
    if not is_whitelisted(interaction.user.id):
        await interaction.response.send_message(
            "❌ 利用権限がありません。管理者にお問い合わせください。",
            ephemeral=True
        )
        return False
    return True

# ===== Modals =====
class LoginModal(discord.ui.Modal, title="PayPay ログイン"):
    account_name = discord.ui.TextInput(label="Bot上で保存する名前", placeholder="メイン / サブ など")
    phone        = discord.ui.TextInput(label="電話番号",           placeholder="09012345678")
    password     = discord.ui.TextInput(label="パスワード",         placeholder="PayPayのパスワード")

    def __init__(self, bot_ref):
        super().__init__()
        self.bot_ref = bot_ref

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid          = interaction.user.id
        account_name = str(self.account_name).strip()
        phone        = str(self.phone).strip()
        password     = str(self.password).strip()

        data     = load_user_data(uid)
        existing = data.get("accounts", {}).get(account_name, {})
        device_uuid = existing.get("device_uuid")

        result = login_step1(phone, password, device_uuid)
        if result.get("status_code") != "P0000":
            await interaction.followup.send(
                f"❌ ステップ1失敗: {result.get('message')} (debug: {result.get('debug_code')})",
                ephemeral=True
            )
            return

        session_id = result["data"]["session_id"]
        self.bot_ref.pending_sessions[uid] = {
            "session_id":   session_id,
            "account_name": account_name,
        }
        await interaction.followup.send(
            f"✅ ステップ1完了！（アカウント: **{account_name}**）\n"
            "PayPayアプリまたはSMSに届いた**認証URL**を `/paypay auth` で入力してください。\n"
            "⏳ 5分以内に入力してください。",
            ephemeral=True
        )


class AuthModal(discord.ui.Modal, title="認証URL入力"):
    auth_url = discord.ui.TextInput(
        label="認証URL または ID",
        placeholder="https://www.paypay.ne.jp/portal/oauth2/l?id=XXXXXX",
        style=discord.TextStyle.long
    )

    def __init__(self, bot_ref):
        super().__init__()
        self.bot_ref = bot_ref

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        uid     = interaction.user.id
        pending = self.bot_ref.pending_sessions.get(uid)
        if not pending:
            await interaction.followup.send(
                "❌ セッションが見つかりません。先に `/paypay login` を実行してください。",
                ephemeral=True
            )
            return

        result = login_step2(pending["session_id"], str(self.auth_url))
        if result.get("status_code") != "P0000":
            await interaction.followup.send(
                f"❌ ログイン失敗: {result.get('message')} (debug: {result.get('debug_code')})",
                ephemeral=True
            )
            return

        account_name = pending["account_name"]
        d = result["data"]
        save_account(uid, account_name, {
            "access_token":  d["access_token"],
            "refresh_token": d["refresh_token"],
            "device_uuid":   d["device_uuid"],
        })
        del self.bot_ref.pending_sessions[uid]
        await interaction.followup.send(
            f"✅ ログイン完了！アカウント **{account_name}** を保存しました。",
            ephemeral=True
        )

# ===== Bot =====
class PayPayBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.pending_sessions: dict = {}

    async def setup_hook(self):
        await self.tree.sync()

    async def on_ready(self):
        print(f"[Pay管理Bot Solo] ready: {self.user}")
        alive_loop.start()

bot = PayPayBot()

# ===== alive定期実行（全ユーザー全アカウント）=====
@tasks.loop(minutes=30)
async def alive_loop():
    from PayPaython_mobile import PayPay
    if not os.path.exists(USER_DATA_DIR):
        return
    for uid_str in os.listdir(USER_DATA_DIR):
        try:
            data = load_user_data(int(uid_str))
        except ValueError:
            continue
        for name, tokens in data.get("accounts", {}).items():
            try:
                pp = PayPay(access_token=tokens["access_token"])
                pp.alive()
                print(f"[Pay管理Bot alive] {uid_str}/{name} OK")
            except Exception as e:
                print(f"[Pay管理Bot alive] {uid_str}/{name} エラー: {e}")

# ===== ヘルパー =====
async def require_login(interaction: discord.Interaction):
    pp, tokens, name = get_paypay(interaction.user.id)
    if not pp:
        await interaction.followup.send(
            "❌ 未ログインまたはトークン期限切れ。`/paypay login` でログインしてください。",
            ephemeral=True
        )
    return pp, name

# ===== コマンドグループ =====
paypay_group = app_commands.Group(name="paypay", description="PayPay操作コマンド")


@paypay_group.command(name="login", description="PayPayにログインする（ステップ1）")
async def paypay_login(interaction: discord.Interaction):
    if not await check_whitelist(interaction):
        return
    await interaction.response.send_modal(LoginModal(bot))


@paypay_group.command(name="auth", description="認証URLを入力してログインを完了する（ステップ2）")
async def paypay_auth(interaction: discord.Interaction):
    if not await check_whitelist(interaction):
        return
    await interaction.response.send_modal(AuthModal(bot))


@paypay_group.command(name="accounts", description="登録済みアカウント一覧を表示する")
async def paypay_accounts(interaction: discord.Interaction):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    uid    = interaction.user.id
    data   = load_user_data(uid)
    active = data.get("active")
    accounts = list(data.get("accounts", {}).keys())
    if not accounts:
        await interaction.followup.send("アカウントが登録されていません。", ephemeral=True)
        return
    lines = [f"{'▶' if a == active else '　'} {a}" for a in accounts]
    embed = discord.Embed(title="📋 登録アカウント", description="\n".join(lines), color=0xFF0000)
    embed.set_footer(text="▶ = アクティブ")
    await interaction.followup.send(embed=embed, ephemeral=True)


@paypay_group.command(name="switch", description="使用するアカウントを切り替える")
async def paypay_switch(interaction: discord.Interaction):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    uid      = interaction.user.id
    data     = load_user_data(uid)
    accounts = list(data.get("accounts", {}).keys())
    if not accounts:
        await interaction.followup.send("❌ アカウントが登録されていません。", ephemeral=True)
        return
    active = data.get("active")
    await interaction.followup.send(
        "切り替えるアカウントを選択してください：",
        view=SwitchView(uid, accounts, active),
        ephemeral=True
    )


@paypay_group.command(name="delete", description="アカウントを削除する")
async def paypay_delete(interaction: discord.Interaction):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    uid      = interaction.user.id
    accounts = list_accounts(uid)
    if not accounts:
        await interaction.followup.send("❌ アカウントが登録されていません。", ephemeral=True)
        return
    await interaction.followup.send(
        "削除するアカウントを選択してください：",
        view=DeleteView(uid, accounts),
        ephemeral=True
    )


@paypay_group.command(name="balance", description="PayPay残高を確認する")
async def paypay_balance(interaction: discord.Interaction):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    from PayPaython_mobile import PayPayLoginError

    pp, name = await require_login(interaction)
    if not pp:
        return

    try:
        r  = pp.get_balance()
        ws = r.raw["payload"]["walletSummary"]
        wd = r.raw["payload"]["walletDetail"]

        def bal(obj):
            if obj is None: return 0
            return obj.get("balance") or 0

        all_total = bal(ws.get("allTotalBalanceInfo"))
        emoney    = bal(wd.get("emoneyBalanceInfo"))
        prepaid   = bal(wd.get("prepaidBalanceInfo"))
        points    = bal(wd.get("cashBackBalanceInfo"))
        pending   = bal(wd.get("cashBackPendingInfo"))

        embed = discord.Embed(title=f"💰 PayPay残高（{name}）", color=0xFF0000)
        embed.add_field(name="合計",           value=f"¥{all_total:,}", inline=False)
        embed.add_field(name="マネー",         value=f"¥{emoney:,}",   inline=True)
        embed.add_field(name="マネーライト",   value=f"¥{prepaid:,}",  inline=True)
        embed.add_field(name="ポイント",       value=f"¥{points:,}",   inline=True)
        if pending:
            embed.add_field(name="付与予定ポイント", value=f"¥{pending:,}", inline=True)
        embed.set_footer(text=f"確認日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayPayLoginError:
        await interaction.followup.send("❌ トークンが無効です。`/paypay login` で再ログインしてください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@paypay_group.command(name="receive", description="受け取りリンクを消化する")
@app_commands.describe(link="PayPay受け取りリンク（URL または ID）")
async def paypay_receive(interaction: discord.Interaction, link: str):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    from PayPaython_mobile import PayPayLoginError

    pp, name = await require_login(interaction)
    if not pp:
        return

    try:
        link_info = pp.link_check(link)
        if link_info.status != "PENDING":
            await interaction.followup.send(f"❌ このリンクは受け取れません（ステータス: {link_info.status}）", ephemeral=True)
            return
        pp.link_receive(link, link_info=link_info)
        embed = discord.Embed(title=f"✅ 受け取り完了（{name}）", color=0x00FF00)
        embed.add_field(name="受け取り金額", value=f"¥{link_info.amount:,}" if link_info.amount else "不明")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayPayLoginError:
        await interaction.followup.send("❌ トークンが無効です。`/paypay login` で再ログインしてください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@paypay_group.command(name="send", description="送金する")
@app_commands.describe(receiver_id="送り先のExternal User ID", amount="送金額（円）")
async def paypay_send(interaction: discord.Interaction, receiver_id: str, amount: int):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    from PayPaython_mobile import PayPayLoginError

    pp, name = await require_login(interaction)
    if not pp:
        return

    if amount <= 0:
        await interaction.followup.send("❌ 金額は1円以上にしてください。", ephemeral=True)
        return

    try:
        pp.send_money(amount=amount, receiver_id=receiver_id)
        embed = discord.Embed(title=f"✅ 送金完了（{name}）", color=0x00FF00)
        embed.add_field(name="送金額",   value=f"¥{amount:,}")
        embed.add_field(name="送り先ID", value=receiver_id)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayPayLoginError:
        await interaction.followup.send("❌ トークンが無効です。`/paypay login` で再ログインしてください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@paypay_group.command(name="request", description="送金リンクを生成する")
@app_commands.describe(amount="請求金額（円）", passcode="パスワード（任意）")
async def paypay_request(interaction: discord.Interaction, amount: int, passcode: str = ""):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    from PayPaython_mobile import PayPayLoginError

    pp, name = await require_login(interaction)
    if not pp:
        return

    if amount <= 0:
        await interaction.followup.send("❌ 金額は1円以上にしてください。", ephemeral=True)
        return

    try:
        result = pp.create_link(amount=amount, passcode=passcode if passcode else None)
        embed = discord.Embed(title=f"🔗 送金リンク生成完了（{name}）", color=0xFF0000)
        embed.add_field(name="請求金額", value=f"¥{amount:,}", inline=True)
        if passcode:
            embed.add_field(name="パスワード", value=passcode, inline=True)
        embed.add_field(name="リンク", value=result.link, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayPayLoginError:
        await interaction.followup.send("❌ トークンが無効です。`/paypay login` で再ログインしてください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@paypay_group.command(name="link_check", description="送金リンクの情報を確認する")
@app_commands.describe(link="確認するリンク（URL または ID）")
async def paypay_link_check(interaction: discord.Interaction, link: str):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    from PayPaython_mobile import PayPayLoginError

    pp, name = await require_login(interaction)
    if not pp:
        return

    try:
        r = pp.link_check(link)
        embed = discord.Embed(title="🔍 リンク情報", color=0x0099FF)
        embed.add_field(name="金額",       value=f"¥{r.amount:,}" if r.amount else "指定なし", inline=True)
        embed.add_field(name="ステータス", value=r.status,                                      inline=True)
        embed.add_field(name="パスワード", value="あり 🔒" if r.has_password else "なし",       inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayPayLoginError:
        await interaction.followup.send("❌ トークンが無効です。`/paypay login` で再ログインしてください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@paypay_group.command(name="link_cancel", description="自分が作った送金リンクをキャンセルする")
@app_commands.describe(link="キャンセルするリンク（URL または ID）")
async def paypay_link_cancel(interaction: discord.Interaction, link: str):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    from PayPaython_mobile import PayPayLoginError

    pp, name = await require_login(interaction)
    if not pp:
        return

    try:
        link_info = pp.link_check(link)
        pp.link_cancel(link, link_info=link_info)
        embed = discord.Embed(title="✅ リンクキャンセル完了", color=0x888888)
        embed.add_field(name="金額", value=f"¥{link_info.amount:,}" if link_info.amount else "不明")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayPayLoginError:
        await interaction.followup.send("❌ トークンが無効です。`/paypay login` で再ログインしてください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@paypay_group.command(name="p2p", description="請求リンクを作成する")
@app_commands.describe(amount="金額を指定する場合（任意）")
async def paypay_p2p(interaction: discord.Interaction, amount: int = 0):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    from PayPaython_mobile import PayPayLoginError

    pp, name = await require_login(interaction)
    if not pp:
        return

    try:
        r = pp.create_p2pcode(amount=amount if amount > 0 else None)
        embed = discord.Embed(title=f"🔗 請求リンク（{name}）", color=0xFF0000)
        if amount > 0:
            embed.add_field(name="指定金額", value=f"¥{amount:,}", inline=True)
        embed.add_field(name="請求リンク", value=r.p2pcode, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayPayLoginError:
        await interaction.followup.send("❌ トークンが無効です。`/paypay login` で再ログインしてください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


@paypay_group.command(name="search", description="PayPayIDでユーザーを検索する")
@app_commands.describe(user_id="相手のPayPayID")
async def paypay_search(interaction: discord.Interaction, user_id: str):
    if not await check_whitelist(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    from PayPaython_mobile import PayPayLoginError

    pp, name = await require_login(interaction)
    if not pp:
        return

    try:
        r = pp.search_p2puser(user_id=user_id)
        embed = discord.Embed(title="🔍 ユーザー検索結果", color=0x0099FF)
        embed.add_field(name="表示名",           value=r.name,             inline=True)
        embed.add_field(name="External User ID", value=r.external_user_id, inline=False)
        if r.icon:
            embed.set_thumbnail(url=r.icon)
        embed.set_footer(text="External User IDを /paypay send の receiver_id に使えます")
        await interaction.followup.send(embed=embed, ephemeral=True)
    except PayPayLoginError:
        await interaction.followup.send("❌ トークンが無効です。`/paypay login` で再ログインしてください。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


# ===== Select Views =====
class SwitchSelect(discord.ui.Select):
    def __init__(self, uid: int, accounts: list, active: str):
        self.uid = uid
        options = [
            discord.SelectOption(label=a, value=a, default=(a == active), emoji="▶" if a == active else None)
            for a in accounts
        ]
        super().__init__(placeholder="切り替えるアカウントを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        set_active(self.uid, self.values[0])
        await interaction.response.send_message(f"✅ **{self.values[0]}** に切り替えました。", ephemeral=True)
        self.view.stop()

class SwitchView(discord.ui.View):
    def __init__(self, uid: int, accounts: list, active: str):
        super().__init__(timeout=30)
        self.add_item(SwitchSelect(uid, accounts, active))


class DeleteSelect(discord.ui.Select):
    def __init__(self, uid: int, accounts: list):
        self.uid = uid
        options = [discord.SelectOption(label=a, value=a, emoji="🗑️") for a in accounts]
        super().__init__(placeholder="削除するアカウントを選択...", options=options)

    async def callback(self, interaction: discord.Interaction):
        delete_account(self.uid, self.values[0])
        await interaction.response.send_message(f"🗑️ **{self.values[0]}** を削除しました。", ephemeral=True)
        self.view.stop()

class DeleteView(discord.ui.View):
    def __init__(self, uid: int, accounts: list):
        super().__init__(timeout=30)
        self.add_item(DeleteSelect(uid, accounts))



bot.tree.add_command(paypay_group)
bot.run(DISCORD_TOKEN)