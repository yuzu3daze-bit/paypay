import discord
from discord.ext import commands
from discord import ui
from paypaypy import PayPay
import json
import os
import uuid
from flask import Flask
from threading import Thread

# --- Flask サーバー設定 (Render用) ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    # Renderは環境変数 PORT を指定してくるため、それに合わせる
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- データベース設定 ---
DATA_FILE = "vending_machine.json"

def load_data():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {"admins": [], "phone": None, "password": None, "items": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

db = load_data()

# --- ボット設定 (環境変数から取得) ---
TOKEN = os.getenv("DISCORD_TOKEN")
# OWNER_IDは数値で取得
try:
    OWNER_ID = int(os.getenv("OWNER_ID", 0))
except ValueError:
    OWNER_ID = 0

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
pp_client = None

# --- 権限チェック ---
def is_admin_check(ctx):
    return ctx.author.id == OWNER_ID or ctx.author.id in db["admins"]

# --- UI: 商品選択パネル ---
class VendingView(ui.View):
    def __init__(self):
        super().__init__(timeout=None) # 永続的なボタン(custom_idが必要)

    @ui.button(label="🛒 商品一覧を見る", style=discord.ButtonStyle.green, custom_id="vending:view_items")
    async def view_items(self, interaction: discord.Interaction, button: ui.Button):
        items = db.get("items", {})
        if not items:
            return await interaction.response.send_message("現在、商品はありません。", ephemeral=True)

        embed = discord.Embed(title="🛒 商品ラインナップ", color=0x2ecc71)
        select = ui.Select(placeholder="購入したい商品を選んでください", custom_id="vending:select_item")

        has_stock = False
        for item_id, info in items.items():
            stock_count = len(info['stock'])
            embed.add_field(
                name=f"📦 {info['name']}",
                value=f"価格: `{info['price']}円` | 在庫: `{stock_count}個`",
                inline=False
            )
            if stock_count > 0:
                select.add_option(label=f"{info['name']} ({info['price']}円)", value=item_id)
                has_stock = True

        if not has_stock:
            return await interaction.response.send_message("現在すべての商品が売り切れです。", ephemeral=True)

        async def select_callback(it: discord.Interaction):
            item_id = select.values[0]
            item = db["items"][item_id]
            await it.response.send_message(
                f"✅ **{item['name']}** を選択中\n"
                f"**{item['price']}円** のPayPayリンクをこのチャットに送信してください。", ephemeral=True)
        
        select.callback = select_callback
        view = ui.View()
        view.add_item(select)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    # 再起動後もボタンが動くように登録
    bot.add_view(VendingView())
    
    global pp_client
    if db.get("phone") and db.get("password"):
        try:
            pp_client = PayPay(phone=db["phone"], password=db["password"])
            print("PayPay session ready.")
        except:
            print("PayPay auto-login failed.")

# --- コマンド ---

@bot.command()
@commands.check(is_admin_check)
async def set_panel(ctx):
    embed = discord.Embed(
        title="🏧 PayPay自動販売機",
        description="下のボタンから商品を購入できます。\n支払いはPayPay送金リンクを貼るだけ！",
        color=0xf1c40f
    )
    await ctx.send(embed=embed, view=VendingView())

@bot.command()
@commands.check(is_admin_check)
async def add_item(ctx, name: str, price: int):
    item_id = str(uuid.uuid4())[:8]
    db["items"][item_id] = {"name": name, "price": price, "stock": []}
    save_data(db)
    await ctx.send(f"✅ 商品追加: {name} ({price}円) ID: `{item_id}`")

@bot.command()
@commands.check(is_admin_check)
async def add_stock(ctx, item_id: str, *, content: str):
    if item_id in db["items"]:
        db["items"][item_id]["stock"].append(content)
        save_data(db)
        await ctx.send(f"📦 在庫追加完了。現在庫数: {len(db['items'][item_id]['stock'])}")
    else:
        await ctx.send("❌ 商品IDが見つかりません。")

@bot.command()
@commands.check(is_admin_check)
async def login(ctx, phone, password):
    global pp_client
    try:
        pp_client = PayPay(phone=phone, password=password)
        db["phone"] = phone
        db["password"] = password
        save_data(db)
        await ctx.send("📲 OTP(SMS)を受信したら `!otp コード` を送信してください。")
    except Exception as e:
        await ctx.send(f"❌ 失敗: {e}")

@bot.command()
@commands.check(is_admin_check)
async def otp(ctx, code):
    if pp_client:
        try:
            pp_client.otp_login(code)
            await ctx.send("✅ ログイン完了。自動受領を開始します。")
        except Exception as e:
            await ctx.send(f"❌ 認証失敗: {e}")

# --- メッセージ監視 (自動受領) ---
@bot.event
async def on_message(message):
    if message.author.bot: return
    await bot.process_commands(message)

    if "https://pay.paypay.ne.jp/" in message.content and pp_client:
        url = message.content.split()[0]
        try:
            info = pp_client.get_link_info(url)
            amount = info.get("order_amount")
            
            target_id = None
            for i_id, i_info in db["items"].items():
                if i_info["price"] == amount and len(i_info["stock"]) > 0:
                    target_id = i_id
                    break
            
            if target_id:
                if pp_client.accept_link(url):
                    gift = db["items"][target_id]["stock"].pop(0)
                    save_data(db)
                    try:
                        await message.author.send(f"📦 購入ありがとうございます！\n商品: **{db['items'][target_id]['name']}**\n内容: `{gift}`")
                        await message.channel.send(f"✅ {message.author.mention} 購入完了！DMを確認してください。")
                    except:
                        await message.channel.send(f"⚠️ {message.author.mention} DMに送信できませんでした。")
                else:
                    await message.channel.send("❌ リンク受領エラー。")
            else:
                await message.channel.send("❓ 対応する在庫のある商品が見つかりません。")
        except Exception as e:
            print(f"Error: {e}")

# 実行
if __name__ == "__main__":
    if TOKEN:
        keep_alive()
        bot.run(TOKEN)
    else:
        print("Error: DISCORD_TOKEN is not set.")
