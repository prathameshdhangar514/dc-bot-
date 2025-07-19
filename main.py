import os, json, datetime, random, asyncio
from flask import Flask
import discord
from discord.ext import commands
from threading import Thread
from dotenv import load_dotenv
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

import os, json, datetime, random, asyncio
from flask import Flask  #hello
import discord
from discord.ext import commands
from threading import Thread
from dotenv import load_dotenv

# ==== Constants ====
DATA_FILE = "data.json"
NICK_LOCKS = "nick_locks.json"
TEMP_ADMINS = "temp_admins.json"
GIFT_TRACKER = "gift_tracker.json"
ROLE_ID_TEMP_ADMIN = 1393927331101544539  # Replace with actual ID
ROLE_ID_HMW = 1393927051685400790  # Replace with actual HMW role ID
ROLE_ID_ADMIN = 1393094845479780426  # Replace with actual Admin role ID for transfers
# Replace with actual token

SHOP_ITEMS = {
    "nickname_lock": {
        "price": 5000,
        "desc": "🔒 Locks your nickname from changes",
        "level_req": 15
    },
    "temp_admin": {
        "price": 25000,
        "desc": "⚡ Gives temporary admin role for 1 hour",
        "level_req": 20
    },
    "hmw_role": {
        "price": 50000,
        "desc": "👑 Grants the prestigious HMW role",
        "level_req": 30
    },
}

# ==== Ensure Data Files Exist ====
for f in [DATA_FILE, NICK_LOCKS, TEMP_ADMINS, GIFT_TRACKER]:
    if not os.path.exists(f):
        with open(f, "w") as x:
            json.dump({}, x)


# ==== Utility Functions ====
def load_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=4)


# ==== Flask Setup ====
app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is alive!"


# ==== Discord Bot Setup ====
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
last_gamble_times = {}


# ==== Bot Events ====
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    bot.loop.create_task(remove_expired_admins())


# ==== Commands ====
@bot.command()
async def daily(ctx):
    user_id = str(ctx.author.id)
    now = datetime.datetime.utcnow()
    data = load_json(DATA_FILE)
    user_data = data.get(
        user_id, {
            "balance": 0,
            "last_claim": None,
            "streak": 0,
            "sp": 100,
            "level": 1,
            "xp": 0
        })

    last_claim = user_data.get("last_claim")
    streak = user_data.get("streak", 0)
    base_reward = 300

    roles = [r.name.lower() for r in ctx.author.roles]
    if "admin" in roles:
        base_reward = 400
    elif "booster" in roles:
        base_reward = 350

    if last_claim:
        last_time = datetime.datetime.fromisoformat(last_claim)
        delta = (now - last_time).days
        if delta == 0:
            remaining = 24 - (now - last_time).seconds // 3600
            embed = discord.Embed(
                title="⏰ **TEMPORAL LOCK ACTIVE**",
                description=f"```diff\n- ENERGY RESERVES DEPLETED\n+ Regeneration in {remaining}h\n```\n🌟 *The cosmic energy needs time to flow through your soul...*",
                color=0x2B2D42
            )
            embed.set_footer(text="⚡ Daily energy recharging...", icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
            return await ctx.send(embed=embed)
        elif delta == 1:
            streak += 1
        else:
            streak = 1
    else:
        streak = 1

    reward = base_reward * 2 if streak == 5 else base_reward
    if streak == 5:
        streak = 0

    # Add XP and level up check
    xp_gained = 25 + (streak * 5)
    user_data["xp"] = user_data.get("xp", 0) + xp_gained
    current_level = user_data.get("level", 1)
    required_xp = current_level * 100

    level_up_msg = ""
    if user_data["xp"] >= required_xp:
        user_data["level"] = current_level + 1
        user_data["xp"] = user_data["xp"] - required_xp
        level_up_msg = f"\n\n🎆 **LEVEL ASCENSION ACHIEVED** 🎆\n```fix\n◆ LEVEL {user_data['level']} UNLOCKED ◆\n```"

    user_data.update({
        "sp": user_data.get("sp", 0) + reward,
        "last_claim": now.isoformat(),
        "streak": streak
    })
    data[user_id] = user_data
    save_json(DATA_FILE, data)

    # Dynamic progress bar with better styling
    streak_emojis = ['⬛', '🟦', '🟨', '🟧', '🟥']
    bar = ''.join([streak_emojis[i] if i < streak else '⬛' for i in range(5)])

    embed = discord.Embed(
        title="⚡ **DAILY ENERGY HARVESTED** ⚡",
        description=f"```css\n[SPIRITUAL ENERGY CHANNELING COMPLETE]\n```\n💫 *The universe grants you its power...* {level_up_msg}",
        color=0x8A2BE2 if streak >= 3 else 0x4169E1
    )

    embed.add_field(
        name="🎁 **REWARDS CLAIMED**",
        value=f"```diff\n+ {reward:,} Spirit Points\n+ {xp_gained} Experience\n```",
        inline=False
    )

    embed.add_field(
        name="🔥 **STREAK PROGRESSION**",
        value=f"{bar} `{streak}/5`\n{'🌟 *STREAK BONUS ACTIVE!*' if streak >= 3 else '💪 *Keep the momentum going!*'}",
        inline=False
    )

    embed.add_field(
        name="📊 **CULTIVATION STATUS**",
        value=f"```yaml\nLevel: {user_data['level']}\nXP: {user_data['xp']}/{user_data['level'] * 100}\nProgress: {'█' * (user_data['xp'] * 10 // (user_data['level'] * 100))}{'░' * (10 - (user_data['xp'] * 10 // (user_data['level'] * 100)))}\n```",
        inline=False
    )

    embed.set_thumbnail(url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.set_footer(text=f"⚡ Next claim available in 24 hours • {ctx.author.display_name}", icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    await ctx.send(embed=embed)


@bot.command()
async def ssbal(ctx, member: discord.Member = None):
    user = member or ctx.author
    user_id = str(user.id)
    data = load_json(DATA_FILE)
    user_data = data.get(user_id, {"balance": 0, "level": 1, "xp": 0})

    balance = user_data.get("balance", 0)
    level = user_data.get("level", 1)
    xp = user_data.get("xp", 0)
    required_xp = level * 100

    # Wealth tier determination
    if balance >= 100000:
        tier = "🏆 **LEGEND**"
        tier_color = 0xFFD700
    elif balance >= 50000:
        tier = "💎 **ELITE**"
        tier_color = 0x9932CC
    elif balance >= 20000:
        tier = "⚡ **MASTER**"
        tier_color = 0x4169E1
    elif balance >= 5000:
        tier = "🌟 **RISING**"
        tier_color = 0x32CD32
    else:
        tier = "🔰 **BEGINNER**"
        tier_color = 0x708090

    embed = discord.Embed(
        title=f"💰 **{user.display_name.upper()}'S TREASURY** 💰",
        description=f"```css\n[SPIRITUAL WEALTH ANALYSIS]\n```\n{tier} • *The crystallized power of ages...*",
        color=tier_color
    )

    embed.add_field(
        name="💎 **SPIRIT STONES**",
        value=f"```fix\n{balance:,} SS\n```",
        inline=True
    )

    embed.add_field(
        name="📈 **CULTIVATION**",
        value=f"```yaml\nLevel: {level}\nXP: {xp}/{required_xp}\n```",
        inline=True
    )

    embed.add_field(
        name="📊 **PROGRESS BAR**",
        value=f"```\n{'█' * (xp * 20 // required_xp)}{'░' * (20 - (xp * 20 // required_xp))}\n```",
        inline=False
    )

    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.set_footer(text=f"💫 Wealth transcends mortal understanding • ID: {user.id}")

    await ctx.send(embed=embed)


@bot.command()
async def spbal(ctx, member: discord.Member = None):
    user = member or ctx.author
    user_id = str(user.id)
    data = load_json(DATA_FILE)
    user_data = data.get(user_id, {"sp": 0, "level": 1, "xp": 0})

    sp = user_data.get("sp", 0)
    level = user_data.get("level", 1)
    xp = user_data.get("xp", 0)
    required_xp = level * 100

    # Energy tier determination
    if sp >= 50000:
        energy_tier = "⚡ **STORM**"
        energy_color = 0xFF1493
    elif sp >= 25000:
        energy_tier = "🔥 **INFERNO**"
        energy_color = 0xFF4500
    elif sp >= 10000:
        energy_tier = "💫 **RADIANT**"
        energy_color = 0x8A2BE2
    elif sp >= 2000:
        energy_tier = "🌟 **BRIGHT**"
        energy_color = 0x4169E1
    else:
        energy_tier = "✨ **SPARK**"
        energy_color = 0x20B2AA

    embed = discord.Embed(
        title=f"⚡ **{user.display_name.upper()}'S ENERGY CORE** ⚡",
        description=f"```diff\n+ SPIRITUAL POWER ANALYSIS +\n```\n{energy_tier} • *Raw energy flows through your essence...*",
        color=energy_color
    )

    embed.add_field(
        name="⚡ **SPIRIT POINTS**",
        value=f"```css\n{sp:,} SP\n```",
        inline=True
    )

    embed.add_field(
        name="🎯 **CULTIVATION**",
        value=f"```yaml\nLevel: {level}\nXP: {xp}/{required_xp}\n```",
        inline=True
    )

    embed.add_field(
        name="🔋 **ENERGY FLOW**",
        value=f"```\n{'▰' * (min(sp, 10000) * 15 // 10000)}{'▱' * (15 - (min(sp, 10000) * 15 // 10000))}\n```",
        inline=False
    )

    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.set_footer(text=f"🌊 Energy is the source of all creation • {user.display_name}")

    await ctx.send(embed=embed)


@bot.command()
async def exchange(ctx, amount: str):
    user_id = str(ctx.author.id)
    data = load_json(DATA_FILE)
    user_data = data.get(user_id, {"balance": 0, "sp": 0})

    if amount.lower() == "all":
        exchange_amount = user_data.get("sp", 0)
    else:
        try:
            exchange_amount = int(amount)
        except:
            embed = discord.Embed(
                title="❌ **INVALID INPUT**",
                description="```diff\n- ERROR: Invalid amount detected\n+ Use: !exchange <number> or 'all'\n```",
                color=0xFF0000
            )
            return await ctx.send(embed=embed)

    if exchange_amount <= 0 or exchange_amount > user_data.get("sp", 0):
        embed = discord.Embed(
            title="🚫 **INSUFFICIENT ENERGY**",
            description="```css\n[TRANSACTION BLOCKED]\n```\n💔 *Your spiritual energy reserves are inadequate for this conversion...*",
            color=0xFF4500
        )
        return await ctx.send(embed=embed)

    user_data["sp"] = user_data.get("sp", 0) - exchange_amount
    user_data["balance"] = user_data.get("balance", 0) + exchange_amount
    data[user_id] = user_data
    save_json(DATA_FILE, data)

    embed = discord.Embed(
        title="🔄 **ENERGY TRANSMUTATION COMPLETE** 🔄",
        description=f"```fix\n◆ SPIRITUAL ALCHEMY SUCCESSFUL ◆\n```\n✨ *Energy crystallizes into eternal stone...*",
        color=0x9932CC
    )

    embed.add_field(
        name="⚗️ **CONVERSION RESULT**",
        value=f"```diff\n- {exchange_amount:,} Spirit Points\n+ {exchange_amount:,} Spirit Stones\n```",
        inline=False
    )

    embed.add_field(
        name="📊 **UPDATED RESERVES**",
        value=f"```yaml\nSP Remaining: {user_data['sp']:,}\nSS Balance: {user_data['balance']:,}\n```",
        inline=False
    )

    embed.set_footer(text="⚡ → 💎 Perfect 1:1 conversion rate achieved", icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

    await ctx.send(embed=embed)


@bot.command()
async def coinflip(ctx, guess: str, amount: str):
    now = datetime.datetime.utcnow()
    user_id = str(ctx.author.id)
    guess = guess.lower()
    data = load_json(DATA_FILE)

    if guess not in ["heads", "tails"]:
        embed = discord.Embed(
            title="⚠️ **INVALID PREDICTION**",
            description="```diff\n- COSMIC ERROR DETECTED\n+ Valid options: 'heads' or 'tails'\n```\n🎯 *Choose your fate wisely, mortal...*",
            color=0xFF6347
        )
        return await ctx.send(embed=embed)

    user_data = data.get(user_id, {
        "sp": 0,
        "monthly_wins": 0,
        "monthly_losses": 0
    })
    sp = user_data.get("sp", 0)

    if user_id in last_gamble_times and (
            now - last_gamble_times[user_id]).total_seconds() < 60:
        embed = discord.Embed(
            title="⏳ **COSMIC COOLDOWN**",
            description="```css\n[FATE ENERGY RECHARGING]\n```\n🌌 *The universe needs time to align the cosmic forces...*",
            color=0x4682B4
        )
        embed.set_footer(text="⚡ Gambling cooldown: 60 seconds between attempts")
        return await ctx.send(embed=embed)

    if amount.lower() == "all":
        bet = min(sp, 20000)
    else:
        try:
            bet = int(amount)
        except:
            embed = discord.Embed(
                title="💸 **INVALID WAGER**",
                description="```diff\n- BETTING ERROR\n+ Enter a valid number\n```",
                color=0xFF0000
            )
            return await ctx.send(embed=embed)

    if bet <= 0 or bet > 20000 or bet > sp:
        embed = discord.Embed(
            title="🚫 **WAGER REJECTED**",
            description="```css\n[INSUFFICIENT FUNDS OR LIMIT EXCEEDED]\n```\n💰 *Maximum bet: 20,000 SP*\n⚡ *Current SP: {:,}*".format(sp),
            color=0xFF4500
        )
        return await ctx.send(embed=embed)

    flip = random.choice(["heads", "tails"])
    won = (flip == guess)

    # Update monthly stats
    current_month = now.strftime("%Y-%m")
    monthly_key = f"monthly_stats_{current_month}"

    if won:
        user_data["sp"] = sp + bet
        user_data[monthly_key] = user_data.get(monthly_key, {
            "wins": 0,
            "losses": 0
        })
        user_data[monthly_key]["wins"] += bet

        embed = discord.Embed(
            title="🎉 **FATE SMILES UPON YOU** 🎉",
            description=f"```diff\n+ COSMIC VICTORY ACHIEVED +\n```\n🪙 *The coin reveals: **{flip.upper()}***\n✨ *Fortune flows through your spirit...*",
            color=0x00FF00
        )

        embed.add_field(
            name="🏆 **VICTORY SPOILS**",
            value=f"```css\n+{bet:,} Spirit Points\n```",
            inline=True
        )

        embed.add_field(
            name="💰 **NEW BALANCE**",
            value=f"```fix\n{user_data['sp']:,} SP\n```",
            inline=True
        )

    else:
        user_data["sp"] = sp - bet
        user_data[monthly_key] = user_data.get(monthly_key, {
            "wins": 0,
            "losses": 0
        })
        user_data[monthly_key]["losses"] += bet

        embed = discord.Embed(
            title="💀 **THE VOID CLAIMS ITS DUE** 💀",
            description=f"```diff\n- COSMIC DEFEAT ENDURED -\n```\n🪙 *The coin reveals: **{flip.upper()}***\n🌑 *Your greed feeds the endless darkness...*",
            color=0xFF0000
        )

        embed.add_field(
            name="💸 **LOSSES SUFFERED**",
            value=f"```css\n-{bet:,} Spirit Points\n```",
            inline=True
        )

        embed.add_field(
            name="💔 **REMAINING BALANCE**",
            value=f"```fix\n{user_data['sp']:,} SP\n```",
            inline=True
        )

    last_gamble_times[user_id] = now
    data[user_id] = user_data
    save_json(DATA_FILE, data)

    embed.add_field(
        name="🎯 **PREDICTION vs REALITY**",
        value=f"```yaml\nYour Guess: {guess.title()}\nActual Result: {flip.title()}\nOutcome: {'WIN' if won else 'LOSS'}\n```",
        inline=False
    )

    embed.set_footer(text="🎰 The cosmic coin never lies • Gamble responsibly", icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

    await ctx.send(embed=embed)


@bot.command()
async def shop(ctx):
    embed = discord.Embed(
        title="🏪 **GU CHANG'S MYSTICAL EMPORIUM** 🏪",
        description="```css\n[LEGENDARY ARTIFACTS AWAIT]\n```\n✨ *Only the worthy may claim these treasures of power...*",
        color=0xFF6B35
    )

    for item, details in SHOP_ITEMS.items():
        item_name = item.replace('_', ' ').title()

        if details['level_req'] >= 25:
            rarity = "🌟 **LEGENDARY**"
        elif details['level_req'] >= 20:
            rarity = "💎 **EPIC**" 
        else:
            rarity = "⚡ **RARE**"

        embed.add_field(
            name=f"{details['desc'].split()[0]} **{item_name.upper()}**",
            value=f"{rarity}\n```yaml\nPrice: {details['price']:,} Spirit Stones\nLevel Required: {details['level_req']}\n```\n*{details['desc'][2:]}*",
            inline=True
        )

    embed.add_field(
        name="💳 **PURCHASE INSTRUCTIONS**",
        value="```fix\n!buy <item_name>\n```\n🛒 *Use the command above to claim your artifact*",
        inline=False
    )

    embed.set_footer(text="⚡ Spiritual artifacts enhance your cosmic journey", icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    await ctx.send(embed=embed)


@bot.command()
async def buy(ctx, item: str):
    user_id = str(ctx.author.id)
    data = load_json(DATA_FILE)
    user_data = data.get(user_id, {"balance": 0, "sp": 100, "level": 1})

    item = item.lower()
    if item not in SHOP_ITEMS:
        available_items = ", ".join([i.replace('_', ' ').title() for i in SHOP_ITEMS.keys()])
        embed = discord.Embed(
            title="❌ **ARTIFACT NOT FOUND**",
            description=f"```diff\n- UNKNOWN ITEM REQUESTED\n```\n🔍 **Available Items:** {available_items}",
            color=0xFF0000
        )
        return await ctx.send(embed=embed)

    item_data = SHOP_ITEMS[item]
    user_level = user_data.get("level", 1)

    if user_level < item_data["level_req"]:
        embed = discord.Embed(
            title="🚫 **CULTIVATION INSUFFICIENT**",
            description=f"```css\n[SPIRITUAL POWER TOO WEAK]\n```\n📊 **Required Level:** {item_data['level_req']}\n⚡ **Your Level:** {user_level}\n\n🌟 *Strengthen your cultivation before attempting to claim this artifact...*",
            color=0xFF4500
        )
        return await ctx.send(embed=embed)

    if user_data["balance"] < item_data["price"]:
        shortage = item_data["price"] - user_data["balance"]
        embed = discord.Embed(
            title="💸 **INSUFFICIENT SPIRIT STONES**",
            description=f"```diff\n- TREASURY INADEQUATE\n```\n💰 **Required:** {item_data['price']:,} SS\n💎 **You Have:** {user_data['balance']:,} SS\n📉 **Shortage:** {shortage:,} SS\n\n⚡ *Gather more power before returning...*",
            color=0xFF6347
        )
        return await ctx.send(embed=embed)

    if item == "nickname_lock":
        nick_locks = load_json(NICK_LOCKS)
        nick_locks[user_id] = True
        save_json(NICK_LOCKS, nick_locks)
        effect = "🔒 **IDENTITY SEALED** - *Your name is now protected from all changes*"
        effect_color = 0x4169E1
    elif item == "temp_admin":
        temp_admins = load_json(TEMP_ADMINS)
        expiry = datetime.datetime.utcnow() + datetime.timedelta(hours=1)
        temp_admins[user_id] = {
            "expires_at": expiry.isoformat(),
            "guild_id": str(ctx.guild.id)
        }
        save_json(TEMP_ADMINS, temp_admins)
        role = ctx.guild.get_role(ROLE_ID_TEMP_ADMIN)
        if role:
            await ctx.author.add_roles(role)
        effect = "⚡ **TEMPORAL AUTHORITY GRANTED** - *Divine power flows through you for 1 hour*"
        effect_color = 0xFF1493
    elif item == "hmw_role":
        role = ctx.guild.get_role(ROLE_ID_HMW)
        if role:
            await ctx.author.add_roles(role)
        effect = "👑 **ELITE STATUS ACHIEVED** - *You have joined the HMW elite circle*"
        effect_color = 0xFFD700
    else:
        effect = "✨ **ARTIFACT BONDED** - *The power is now yours to wield*"
        effect_color = 0x9932CC

    user_data["balance"] -= item_data["price"]
    data[user_id] = user_data
    save_json(DATA_FILE, data)

    embed = discord.Embed(
        title="✅ **TRANSACTION COMPLETED** ✅",
        description=f"```fix\n◆ ARTIFACT ACQUISITION SUCCESSFUL ◆\n```\n{effect}",
        color=effect_color
    )

    embed.add_field(
        name="🎁 **ARTIFACT CLAIMED**",
        value=f"```css\n{item.replace('_', ' ').title()}\n```",
        inline=True
    )

    embed.add_field(
        name="💰 **COST PAID**",
        value=f"```diff\n- {item_data['price']:,} Spirit Stones\n```",
        inline=True
    )

    embed.add_field(
        name="💎 **REMAINING BALANCE**",
        value=f"```yaml\n{user_data['balance']:,} SS\n```",
        inline=True
    )

    embed.set_thumbnail(url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.set_footer(text="🌟 Power has been transferred • Use it wisely", icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    await ctx.send(embed=embed)


@bot.command()
async def gift(ctx, member: discord.Member, amount: int):
    sender_id = str(ctx.author.id)
    receiver_id = str(member.id)

    if sender_id == receiver_id:
        embed = discord.Embed(
            title="🚫 **PARADOX DETECTED**",
            description="```css\n[SELF-TRANSFER IMPOSSIBLE]\n```\n🌌 *The cosmic laws prevent gifting power to oneself...*",
            color=0xFF4500
        )
        return await ctx.send(embed=embed)

    now = datetime.datetime.utcnow()
    data = load_json(DATA_FILE)
    gift_data = load_json(GIFT_TRACKER)

    # Check cooldown
    pair_key = f"{sender_id}:{receiver_id}"
    last_gift_time = gift_data.get(pair_key)
    if last_gift_time:
        last_time = datetime.datetime.fromisoformat(last_gift_time)
        if (now - last_time).total_seconds() < 86400:
            remaining = 24 - (now - last_time).seconds // 3600
            embed = discord.Embed(
                title="⏰ **GIFT COOLDOWN ACTIVE**",
                description=f"```css\n[SPIRITUAL FLOW RESTRICTION]\n```\n🕐 **Cooldown Remaining:** {remaining} hour(s)\n🎁 *The energy flow between souls must rest...*",
                color=0x4682B4
            )
            return await ctx.send(embed=embed)

    sender_data = data.get(sender_id, {"balance": 0})
    receiver_data = data.get(receiver_id, {"balance": 0})

    if amount <= 0 or sender_data["balance"] < amount:
        embed = discord.Embed(
            title="💸 **TRANSFER FAILED**",
            description="```diff\n- INSUFFICIENT SPIRIT STONES\n```\n💰 **Your Balance:** {:,} SS\n🎯 **Amount Requested:** {:,} SS\n\n⚡ *Your generosity exceeds your treasury...*".format(sender_data["balance"], amount),
            color=0xFF0000
        )
        return await ctx.send(embed=embed)

    sender_data["balance"] -= amount
    receiver_data["balance"] += amount
    data[sender_id] = sender_data
    data[receiver_id] = receiver_data
    save_json(DATA_FILE, data)

# Update gift tracker
    gift_data[pair_key] = now.isoformat()
    save_json(GIFT_TRACKER, gift_data)

    embed = discord.Embed(
        title="✨ 𝕊𝕡𝕚𝕣𝕚𝕥𝕦𝕒𝕝 ℂ𝕣𝕒𝗇𝗌𝖿𝖾𝗋 ℂ𝗈𝗆𝗉𝗅𝖾𝗍𝖾 ✨",
        description="```fix\n⚡ The ethereal energies flow between souls... ⚡\n```\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*🌟 A generous spirit has shared their power 🌟*",
        color=0x8B00FF)

    embed.add_field(
        name="🎭 **BENEFACTOR**",
        value=f"```ansi\n\u001b[0;35m{ctx.author.display_name}\u001b[0m\n```",
        inline=True
    )
    embed.add_field(
        name="🎯 **RECIPIENT**", 
        value=f"```ansi\n\u001b[0;36m{member.display_name}\u001b[0m\n```",
        inline=True
    )
    embed.add_field(
        name="💰 **TRANSFER AMOUNT**",
        value=f"```fix\n💎 {amount:,} Spirit Stones\n```",
        inline=False
    )

    embed.set_thumbnail(url="https://cdn.discordapp.com/emojis/123456789.gif")  # Optional: add animated gem
    embed.set_footer(text="⚡ Spiritual Energy Transfer System ⚡", icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.timestamp = datetime.datetime.utcnow()

    await ctx.send(embed=embed)


@bot.command()
async def transfer(ctx, member: discord.Member, amount: int):
    # Check if user has admin role
    admin_role = ctx.guild.get_role(ROLE_ID_ADMIN)
    if admin_role not in ctx.author.roles:
        return await ctx.send(
            "```diff\n- 🚫 ACCESS DENIED 🚫\n- Divine authority required for such transfers\n```"
        )

    receiver_id = str(member.id)
    data = load_json(DATA_FILE)
    receiver_data = data.get(receiver_id, {"balance": 0})

    if amount <= 0:
        return await ctx.send(
            "```diff\n- ❌ INVALID AMOUNT ❌\n- Positive values only, mortal\n```"
        )

    receiver_data["balance"] = receiver_data.get("balance", 0) + amount
    data[receiver_id] = receiver_data
    save_json(DATA_FILE, data)

    embed = discord.Embed(
        title="⚡ 𝕯𝖎𝖛𝖎𝖓𝖊 𝕬𝖉𝖒𝖎𝖓 𝕿𝖗𝖆𝗇𝗌𝖿𝖾𝗋 ⚡",
        description="```fix\n🔥 ADMINISTRATOR POWER ACTIVATED 🔥\n```\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*⚡ The eternal treasury flows with divine will ⚡*",
        color=0xFF0040)

    embed.add_field(
        name="👑 **ADMINISTRATOR**",
        value=f"```ansi\n\u001b[0;31m{ctx.author.display_name}\u001b[0m\n```",
        inline=True
    )
    embed.add_field(
        name="🎯 **RECIPIENT**",
        value=f"```ansi\n\u001b[0;32m{member.display_name}\u001b[0m\n```",
        inline=True
    )
    embed.add_field(
        name="💸 **AMOUNT GRANTED**",
        value=f"```yaml\n💎 {amount:,} Spirit Stones\n```",
        inline=False
    )
    embed.add_field(
        name="💰 **NEW BALANCE**",
        value=f"```fix\n💎 {receiver_data['balance']:,} SS\n```",
        inline=False
    )

    embed.set_footer(text="⚡ Divine Administrative System ⚡", icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.timestamp = datetime.datetime.utcnow()

    await ctx.send(embed=embed)


@bot.command()
async def top(ctx):
    data = load_json(DATA_FILE)
    leaderboard = sorted(data.items(),
                         key=lambda x: x[1].get("balance", 0),
                         reverse=True)[:10]

    embed = discord.Embed(
        title="🏆 𝕊𝕡𝕚𝕣𝕚𝕥𝕦𝕒𝕝 ℍ𝕚𝖊𝗋𝖺𝗋𝖼𝗁𝗒 🏆",
        description="```fix\n🌟 THE MOST POWERFUL SPIRITUAL CULTIVATORS 🌟\n```\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*⚡ These souls have transcended mortal limitations ⚡*",
        color=0xFFD700)

    medal_emojis = ["🥇", "🥈", "🥉", "🏅", "⭐", "💫", "✨", "🔸", "🔹", "🔺"]

    for i, (uid, user_data) in enumerate(leaderboard, start=1):
        user = ctx.guild.get_member(int(uid))
        name = user.display_name if user else "Unknown Spirit"
        level = user_data.get("level", 1)
        balance = user_data.get('balance', 0)

        medal = medal_emojis[i-1] if i <= 10 else "💠"

        embed.add_field(
            name=f"{medal} **RANK #{i} - {name}**",
            value=f"```ansi\n\u001b[0;33m💎 {balance:,} Spirit Stones\u001b[0m\n\u001b[0;36m📊 Level {level}\u001b[0m\n```",
            inline=False
        )

    embed.set_footer(text="⚡ Updated Spiritual Rankings ⚡")
    embed.timestamp = datetime.datetime.utcnow()

    await ctx.send(embed=embed)


@bot.command()
async def lucky(ctx):
    data = load_json(DATA_FILE)
    now = datetime.datetime.utcnow()
    current_month = now.strftime("%Y-%m")
    monthly_key = f"monthly_stats_{current_month}"

    lucky_users = []
    for uid, user_data in data.items():
        monthly_stats = user_data.get(monthly_key, {"wins": 0, "losses": 0})
        net_wins = monthly_stats.get("wins", 0)
        if net_wins > 0:
            lucky_users.append((uid, net_wins))

    lucky_users.sort(key=lambda x: x[1], reverse=True)
    lucky_users = lucky_users[:10]

    embed = discord.Embed(
        title="🍀 𝔽𝕠𝗋𝗍𝗎𝗇𝖾'𝗌 ℂ𝗁𝗈𝗌𝖾𝗇 𝔒𝗇𝖾𝗌 🍀",
        description="```fix\n🎰 BLESSED BY THE GAMBLING SPIRITS 🎰\n```\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*🌟 Fortune smiles upon these brave souls 🌟*",
        color=0x00FF7F)

    if not lucky_users:
        embed.add_field(
            name="📊 **NO FORTUNE FOUND**",
            value="```diff\n- The month is young... or luck has fled\n```",
            inline=False
        )
    else:
        luck_emojis = ["🍀", "🎯", "⚡", "✨", "💫", "🌟", "💥", "🔥", "⭐", "🎊"]

        for i, (uid, wins) in enumerate(lucky_users, start=1):
            user = ctx.guild.get_member(int(uid))
            name = user.display_name if user else "Unknown Spirit"
            emoji = luck_emojis[i-1] if i <= 10 else "🎲"

            embed.add_field(
                name=f"{emoji} **#{i} - {name}**",
                value=f"```ansi\n\u001b[0;32m⚡ {wins:,} SP Won Through Gambling\u001b[0m\n```",
                inline=False
            )

    embed.set_footer(text=f"🎰 Monthly Fortune Report - {current_month} 🎰")
    embed.timestamp = datetime.datetime.utcnow()

    await ctx.send(embed=embed)


@bot.command()
async def unlucky(ctx):
    data = load_json(DATA_FILE)
    now = datetime.datetime.utcnow()
    current_month = now.strftime("%Y-%m")
    monthly_key = f"monthly_stats_{current_month}"

    unlucky_users = []
    for uid, user_data in data.items():
        monthly_stats = user_data.get(monthly_key, {"wins": 0, "losses": 0})
        net_losses = monthly_stats.get("losses", 0)
        if net_losses > 0:
            unlucky_users.append((uid, net_losses))

    unlucky_users.sort(key=lambda x: x[1], reverse=True)
    unlucky_users = unlucky_users[:10]

    embed = discord.Embed(
        title="💀 ℭ𝗎𝗋𝗌𝖾𝖽 𝖻𝗒 𝔐𝗂𝗌𝖿𝗈𝗋𝗍𝗎𝗇𝖾 💀",
        description="```fix\n🔥 CONSUMED BY GAMBLING'S VOID 🔥\n```\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*💀 These souls have fed the darkness with greed 💀*",
        color=0xFF1744)

    if not unlucky_users:
        embed.add_field(
            name="📊 **NO MISFORTUNE RECORDED**",
            value="```diff\n+ Wisdom prevailed... or none dared gamble\n```",
            inline=False
        )
    else:
        curse_emojis = ["💀", "⚰️", "🩸", "💥", "🔥", "⚡", "💔", "🖤", "⛈️", "🌪️"]

        for i, (uid, losses) in enumerate(unlucky_users, start=1):
            user = ctx.guild.get_member(int(uid))
            name = user.display_name if user else "Unknown Spirit"
            emoji = curse_emojis[i-1] if i <= 10 else "💸"

            embed.add_field(
                name=f"{emoji} **#{i} - {name}**",
                value=f"```ansi\n\u001b[0;31m💸 {losses:,} SP Lost to the Void\u001b[0m\n```",
                inline=False
            )

    embed.set_footer(text=f"💀 Monthly Misfortune Report - {current_month} 💀")
    embed.timestamp = datetime.datetime.utcnow()

    await ctx.send(embed=embed)


@bot.command(name="help")
async def custom_help(ctx):
    embed = discord.Embed(
        title="📜 𝔾𝕦 ℂ𝗁𝖺𝗇𝗀'𝗌 𝔖𝔭𝔦𝗋𝔦𝗍𝗎𝖺𝔩 ℭ𝗈𝖽𝖾𝗑 📜",
        description="```fix\n⚡ PATHWAYS TO ULTIMATE POWER ⚡\n```\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*🌟 Master these commands and ascend to greatness 🌟*",
        color=0x7B68EE)

    commands = [
        ("💰 !daily", "Claim your daily spiritual tribute"),
        ("💎 !ssbal", "Check your Spirit Stone treasury"),
        ("⚡ !spbal", "View your Spirit Point reserves"),
        ("🔄 !exchange <amount/all>", "Convert SP to SS (1:1 ratio)"),
        ("🎰 !coinflip <heads/tails> <amount>", "Test fate against the cosmic coin"),
        ("🛍️ !shop", "Browse spiritual artifacts collection"),
        ("🛒 !buy <item>", "Purchase mystical artifacts"),
        ("✨ !gift <user> <amount>", "Transfer SS to another soul"),
        ("👑 !transfer <user> <amount>", "[ADMIN] Grant unlimited SS"),
        ("🏆 !top", "View the spiritual hierarchy"),
        ("🍀 !lucky", "See fortune's chosen ones"),
        ("💀 !unlucky", "Witness the cursed gamblers")
    ]

    for i, (cmd, desc) in enumerate(commands):
        embed.add_field(
            name=f"**{cmd}**",
            value=f"```ansi\n\u001b[0;36m{desc}\u001b[0m\n```",
            inline=False
        )

    embed.set_footer(text="⚡ Spiritual Ascension Codex ⚡ | Master these powers wisely")
    embed.timestamp = datetime.datetime.utcnow()

    await ctx.send(embed=embed)


# ==== Temp Admin Removal ====
async def remove_expired_admins():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.datetime.utcnow()
        data = load_json(TEMP_ADMINS)
        to_remove = []

        for uid, info in data.items():
            expiry = datetime.datetime.fromisoformat(info['expires_at'])
            if now >= expiry:
                guild = discord.utils.get(bot.guilds, id=int(info['guild_id']))
                member = guild.get_member(int(uid))
                role = guild.get_role(ROLE_ID_TEMP_ADMIN)
                if member and role in member.roles:
                    try:
                        await member.remove_roles(
                            role, reason="Temporary Admin expired")
                    except:
                        pass
                to_remove.append(uid)

        for uid in to_remove:
            del data[uid]

        save_json(TEMP_ADMINS, data)
        await asyncio.sleep(3600)


# ==== Run Flask and Bot Together ====
def run_flask():
    app.run(host='0.0.0.0', port=8080)


def keep_alive():
    t = Thread(target=run_flask)
    t.start()


if __name__ == "__main__":
    keep_alive()
    asyncio.run(bot.start(TOKEN))