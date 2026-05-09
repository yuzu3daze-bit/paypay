import discord
from discord.ext import commands
from discord import ui
import json
import os
import uuid
import requests
import re
from flask import Flask
from threading import Thread

# --- Flask サーバー設定 (Render常時起動用) ---
app = Flask('')

@app.route('/')
def home():
    return "Vending Bot is Running!"

def run():
    port = int(os.getenv("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
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
    return {"admins": [], "token": None, "items": {}}

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

db = load_data()

# --- ボット設定 ---
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
try:
    OWNER_ID = int(os.getenv("OWNER_ID", 0))
except:
    OWNER_ID = 0

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# --- PayPay簡易操作クラス ---
class PayPaySimple:
    def __init__(self, access_token):
        self.token = access_token
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def get_link_info(self, url):
        # リンクからVerificationCodeを抽出
        code = url.split("/")[-1]
        res = requests.get(f"https://www.paypay.ne.jp/portal/v1/order/link/detail?verificationCode={code}")
        return res.json() if res.status_code == 200 else None

    def accept_link(self, url):
        code = url.split("/")[-1]
        payload = {"verificationCode": code}
        # 実際にはより複雑な認証が必要な場合があります
        res = requests.post("https://www.paypay.ne.jp/portal/v1/order/link/receive", json=payload, headers=self.headers)
        return res.status_code == 200

# --- UI: 自販機パネル ---
class VendingView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @ui.button(label="🛒 商品を見る", style=discord.ButtonStyle.green, custom_id="v_view")
    async def view_items(self, interaction: discord.Interaction, button: ui.Button):
        items = db.get("items", {})
        if not items:
            return await interaction.response.send_message("在庫がありません。", ephemeral=True)

        embed = discord.Embed(title="🛒 商品一覧", color=0x2ecc71)
        select = ui.Select(placeholder="商品を選択", custom_id="v_select")

        for i_id, info in items.items():
            stock_count = len(info['stock'])
            embed.add_field(name=info['name'], value=f"価格: {info['price']}円\n在庫: {stock_count}個", inline=False)
            if stock_count > 0:
                select.add_option(label=f"{info['name']} ({info['price']}円)", value=i_id)

        async def callback(it: discord.Interaction):
            item = db["items"][select.values[0]]
            await it.response.send_message(f"✅ **{item['name']}** を選択しました。\n{item['price']}円のPayPayリンクを貼ってください。", ephemeral=True)
        
        select.callback = callback
        view = ui.View(); view.add_item(select)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    bot.add_view(VendingView())

# --- 管理コマンド ---
@bot.command()
async def set_panel(ctx):
    if ctx.author.id != OWNER_ID and ctx.author.id not in db["admins"]: return
    await ctx.send(embed=discord.Embed(title="🏧 PayPay自販機", description="ボタンから購入開始"), view=VendingView())

@bot.command()
async def add_item(ctx, name: str, price: int):
    if ctx.author.id != OWNER_ID: return
    i_id = str(uuid.uuid4())[:8]
    db["items"][i_id] = {"name": name, "price": price, "stock": []}
    save_data(db)
    await ctx.send(f"商品登録: {name} ID: `{i_id}`")

@bot.command()
async def add_stock(ctx, i_id: str, *, content: str):
    if ctx.author.id != OWNER_ID: return
    if i_id in db["items"]:
        db["items"][i_id]["stock"].append(content)
        save_data(db)
        await ctx.send("在庫を追加しました。")

@bot.command()
async def set_token(ctx, token: str):
    """PayPayのアクセストークンを直接設定"""
    if ctx.author.id != OWNER_ID: return
    db["token"] = token
    save_data(db)
    await ctx.delete_message()
    await ctx.send("✅ トークンを更新しました。")

# --- メッセージ監視 ---
@bot.event
async def on_message(message):
    if message.author.bot: return
    await bot.process_commands(message)

    if "https://pay.paypay.ne.jp/" in message.content and db.get("token"):
        url = re.search(r"https://pay\.paypay\.ne\.jp/\S+", message.content).group()
        pp = PayPaySimple(db["token"])
        
        info = pp.get_link_info(url)
        if info and "data" in info:
            amount = info["data"]["orderAmount"]
            # 在庫チェック
            for i_id, i_info in db["items"].items():
                if i_info["price"] == amount and len(i_info["stock"]) > 0:
                    if pp.accept_link(url):
                        gift = db["items"][i_id]["stock"].pop(0)
                        save_data(db)
                        await message.author.send(f"🛍️ 購入完了！\n商品: {i_info['name']}\n内容: `{gift}`")
                        await message.channel.send(f"✅ {message.author.mention} 様、ご購入ありがとうございます！")
                        return
        await message.channel.send("❌ 処理に失敗しました。")

if __name__ == "__main__":
    keep_alive()
    bot.run(DISCORD_TOKEN)
