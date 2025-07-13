#this is a forced test change to trigger git
#this isto verify real file
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
        "desc": "Locks your nickname from changes",
        "level_req": 15
    },
    "temp_admin": {
        "price": 25000,
        "desc": "Gives temporary admin role for 1 hour",
        "level_req": 20
    },
    "hmw_role": {
        "price": 50000,
        "desc": "Grants the prestigious HMW role",
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
            return await ctx.send(
                f"⏳ *You have already claimed your daily power. The spiritual energy replenishes in {remaining} hour(s).*"
            )
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
        level_up_msg = f"\n🎉 **Level Up!** *Your cultivation has advanced to Level {user_data['level']}!*"

    user_data.update({
        "sp": user_data.get("sp", 0) + reward,  # Changed from balance to sp
        "last_claim": now.isoformat(),
        "streak": streak
    })
    data[user_id] = user_data
    save_json(DATA_FILE, data)

    bar = ''.join(["🟩" if i < streak else "🟥" for i in range(5)])
    embed = discord.Embed(
        title="⚡ Daily Spirit Points Claimed!",
        description=
        f"*Hmph... Another day, another harvest of spiritual energy.*{level_up_msg}",
        color=discord.Color.green())
    embed.add_field(name="Reward",
                    value=f"⚡ {reward} Spirit Points\n📈 {xp_gained} XP",
                    inline=False)
    embed.add_field(name="Streak Progress", value=f"{bar}", inline=False)
    embed.add_field(
        name="Level",
        value=
        f"📊 Level {user_data['level']} ({user_data['xp']}/{user_data['level'] * 100} XP)",
        inline=False)
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

    embed = discord.Embed(
        title=f"💎 {user.display_name}'s Spirit Stone Treasury",
        description=f"*The accumulated wealth of spiritual cultivation...*",
        color=discord.Color.gold())
    embed.add_field(name="💎 Spirit Stones", value=f"{balance:,}", inline=True)
    embed.add_field(name="📊 Level",
                    value=f"{level} ({xp}/{required_xp} XP)",
                    inline=True)
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

    embed = discord.Embed(
        title=f"⚡ {user.display_name}'s Spirit Point Energy",
        description=
        f"*The raw spiritual energy flowing through one's essence...*",
        color=discord.Color.blue())
    embed.add_field(name="⚡ Spirit Points", value=f"{sp:,}", inline=True)
    embed.add_field(name="📊 Level",
                    value=f"{level} ({xp}/{required_xp} XP)",
                    inline=True)
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
            return await ctx.send(
                "⚠️ *Invalid amount. Specify a number or 'all'.*")

    if exchange_amount <= 0 or exchange_amount > user_data.get("sp", 0):
        return await ctx.send(
            "🚫 *Insufficient Spirit Points for this exchange.*")

    user_data["sp"] = user_data.get("sp", 0) - exchange_amount
    user_data["balance"] = user_data.get("balance", 0) + exchange_amount
    data[user_id] = user_data
    save_json(DATA_FILE, data)

    embed = discord.Embed(
        title="🔄 Spiritual Energy Exchange",
        description=f"*The conversion is complete. Energy becomes substance.*",
        color=discord.Color.purple())
    embed.add_field(
        name="Exchanged",
        value=f"⚡ {exchange_amount:,} SP → 💎 {exchange_amount:,} SS",
        inline=False)
    embed.add_field(name="Remaining SP",
                    value=f"⚡ {user_data['sp']:,}",
                    inline=True)
    embed.add_field(name="New SS Balance",
                    value=f"💎 {user_data['balance']:,}",
                    inline=True)
    await ctx.send(embed=embed)


@bot.command()
async def coinflip(ctx, guess: str, amount: str):
    now = datetime.datetime.utcnow()
    user_id = str(ctx.author.id)
    guess = guess.lower()
    data = load_json(DATA_FILE)

    if guess not in ["heads", "tails"]:
        return await ctx.send(
            "⚠️ *Foolish mortal... Your guess must be 'heads' or 'tails'. Do not waste my time.*"
        )

    user_data = data.get(user_id, {
        "sp": 0,
        "monthly_wins": 0,
        "monthly_losses": 0
    })
    sp = user_data.get("sp", 0)

    if user_id in last_gamble_times and (
            now - last_gamble_times[user_id]).total_seconds() < 60:
        return await ctx.send(
            "⏳ *Patience... Even the strongest must wait. The spiritual energy needs time to settle.*"
        )

    if amount.lower() == "all":
        bet = min(sp, 20000)
    else:
        try:
            bet = int(amount)
        except:
            return await ctx.send(
                "⚠️ *Your bet amount is invalid. Numbers only, peasant.*")

    if bet <= 0 or bet > 20000 or bet > sp:
        return await ctx.send(
            "🚫 *Impossible. You either lack the funds or exceed the spiritual limit of 20,000 SP.*"
        )

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
        result_msg = "🎉 *Hmph... Fortune favors you this time. Your spiritual power grows.*"
    else:
        user_data["sp"] = sp - bet
        user_data[monthly_key] = user_data.get(monthly_key, {
            "wins": 0,
            "losses": 0
        })
        user_data[monthly_key]["losses"] += bet
        result_msg = "💀 *Pathetic... Your greed has cost you dearly. Learn from this failure.*"

    last_gamble_times[user_id] = now
    data[user_id] = user_data
    save_json(DATA_FILE, data)

    embed = discord.Embed(
        title="🎰 Coin of Fate",
        description=
        f"*The coin spins through the void... and lands on* **{flip}**!",
        color=discord.Color.green() if won else discord.Color.red())
    embed.add_field(name="Outcome", value=result_msg, inline=False)
    embed.add_field(name="New SP Balance",
                    value=f"⚡ {user_data['sp']:,} SP",
                    inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def shop(ctx):
    embed = discord.Embed(
        title="🏪 Gu Chang's Spiritual Emporium",
        description="*These treasures... only the worthy may possess them.*",
        color=discord.Color.orange())
    for item, details in SHOP_ITEMS.items():
        embed.add_field(
            name=f"✨ {item.replace('_', ' ').title()}",
            value=
            f"*{details['desc']}*\n💎 {details['price']:,} SS\n📊 Level {details['level_req']} Required",
            inline=False)
    embed.set_footer(text="Use !buy <item> to claim these artifacts of power")
    await ctx.send(embed=embed)


@bot.command()
async def buy(ctx, item: str):
    user_id = str(ctx.author.id)
    data = load_json(DATA_FILE)
    user_data = data.get(user_id, {"balance": 0, "sp": 100, "level": 1})

    item = item.lower()
    if item not in SHOP_ITEMS:
        return await ctx.send(
            "❌ *Such an item does not exist in my collection. Check the shop again, mortal.*"
        )

    item_data = SHOP_ITEMS[item]
    user_level = user_data.get("level", 1)

    if user_level < item_data["level_req"]:
        return await ctx.send(
            f"🚫 *Your spiritual cultivation is insufficient. Reach Level {item_data['level_req']} before attempting to claim this treasure.*"
        )

    if user_data["balance"] < item_data["price"]:
        return await ctx.send(
            "💸 *Your Spirit Stones are lacking. Gather more power before returning to me.*"
        )

    if item == "nickname_lock":
        nick_locks = load_json(NICK_LOCKS)
        nick_locks[user_id] = True
        save_json(NICK_LOCKS, nick_locks)
        effect = "🔒 *Your identity is now sealed against change. Well done.*"
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
        effect = "🛡️ *Temporal authority flows through you. Use it wisely... it fades with time.*"
    elif item == "hmw_role":
        role = ctx.guild.get_role(ROLE_ID_HMW)
        if role:
            await ctx.author.add_roles(role)
        effect = "👑 *The HMW blessing is yours. You have proven your worth among the elite.*"
    else:
        effect = "🎁 *The artifact is yours. May it serve you well.*"

    user_data["balance"] -= item_data["price"]
    data[user_id] = user_data
    save_json(DATA_FILE, data)

    embed = discord.Embed(
        title="✅ Transaction Complete",
        description="*The exchange is sealed. Power has been transferred.*",
        color=discord.Color.green())
    embed.add_field(name="Artifact Claimed",
                    value=item.replace('_', ' ').title(),
                    inline=True)
    embed.add_field(name="Cost",
                    value=f"💎 {item_data['price']:,} SS",
                    inline=True)
    embed.add_field(name="Effect", value=effect, inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def gift(ctx, member: discord.Member, amount: int):
    sender_id = str(ctx.author.id)
    receiver_id = str(member.id)

    if sender_id == receiver_id:
        return await ctx.send(
            "🚫 *Foolish... One cannot gift power to oneself. The spiritual laws forbid such paradox.*"
        )

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
            return await ctx.send(
                f"⏳ *The flow of power must rest. You can gift {member.display_name} again in {remaining} hour(s).*"
            )

    sender_data = data.get(sender_id, {"balance": 0})
    receiver_data = data.get(receiver_id, {"balance": 0})

    if amount <= 0 or sender_data["balance"] < amount:
        return await ctx.send(
            "🚫 *Impossible. Either your offering is worthless or your treasury lacks the required Spirit Stones.*"
        )

    sender_data["balance"] -= amount
    receiver_data["balance"] += amount
    data[sender_id] = sender_data
    data[receiver_id] = receiver_data
    save_json(DATA_FILE, data)

    # Update gift tracker
    gift_data[pair_key] = now.isoformat()
    save_json(GIFT_TRACKER, gift_data)

    embed = discord.Embed(
        title="🎁 Spiritual Transfer Complete",
        description=
        "*The flow of power has been redirected. Generosity... or perhaps strategy?*",
        color=discord.Color.purple())
    embed.add_field(name="Benefactor",
                    value=ctx.author.display_name,
                    inline=True)
    embed.add_field(name="Recipient", value=member.display_name, inline=True)
    embed.add_field(name="Transfer Amount",
                    value=f"💎 {amount:,} Spirit Stones",
                    inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def transfer(ctx, member: discord.Member, amount: int):
    # Check if user has admin role
    admin_role = ctx.guild.get_role(ROLE_ID_ADMIN)
    if admin_role not in ctx.author.roles:
        return await ctx.send(
            "🚫 *You lack the divine authority to perform such transfers. Only the chosen administrators may wield this power.*"
        )

    receiver_id = str(member.id)
    data = load_json(DATA_FILE)
    receiver_data = data.get(receiver_id, {"balance": 0})

    if amount <= 0:
        return await ctx.send(
            "🚫 *The transfer amount must be positive. Do not waste divine power on emptiness.*"
        )

    receiver_data["balance"] = receiver_data.get("balance", 0) + amount
    data[receiver_id] = receiver_data
    save_json(DATA_FILE, data)

    embed = discord.Embed(
        title="⚡ Divine Transfer Complete",
        description=
        "*The administrator's will has been enforced. Power flows from the eternal treasury.*",
        color=discord.Color.red())
    embed.add_field(name="Administrator",
                    value=ctx.author.display_name,
                    inline=True)
    embed.add_field(name="Recipient", value=member.display_name, inline=True)
    embed.add_field(name="Amount Granted",
                    value=f"💎 {amount:,} Spirit Stones",
                    inline=False)
    embed.add_field(name="New Balance",
                    value=f"💎 {receiver_data['balance']:,} SS",
                    inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def top(ctx):
    data = load_json(DATA_FILE)
    leaderboard = sorted(data.items(),
                         key=lambda x: x[1].get("balance", 0),
                         reverse=True)[:10]

    embed = discord.Embed(
        title="🏆 Hierarchy of Spiritual Power",
        description=
        "*These are the ones who have accumulated true strength...*",
        color=discord.Color.gold())
    for i, (uid, user_data) in enumerate(leaderboard, start=1):
        user = ctx.guild.get_member(int(uid))
        name = user.display_name if user else "Unknown Spirit"
        level = user_data.get("level", 1)
        embed.add_field(
            name=f"#{i} - {name}",
            value=f"💎 {user_data.get('balance', 0):,} SS | 📊 Level {level}",
            inline=False)
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
        title="🍀 Fortune's Chosen Ones",
        description="*Those blessed by the gambling spirits this month...*",
        color=discord.Color.green())

    if not lucky_users:
        embed.add_field(
            name="No Data",
            value="*The month is young... or fortune has abandoned all.*",
            inline=False)
    else:
        for i, (uid, wins) in enumerate(lucky_users, start=1):
            user = ctx.guild.get_member(int(uid))
            name = user.display_name if user else "Unknown Spirit"
            embed.add_field(name=f"#{i} - {name}",
                            value=f"⚡ {wins:,} SP won through gambling",
                            inline=False)
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
        title="💀 Cursed by Misfortune",
        description=
        "*Those who have fed the void with their greed this month...*",
        color=discord.Color.red())

    if not unlucky_users:
        embed.add_field(
            name="No Data",
            value="*Perhaps wisdom has prevailed... or none dared to gamble.*",
            inline=False)
    else:
        for i, (uid, losses) in enumerate(unlucky_users, start=1):
            user = ctx.guild.get_member(int(uid))
            name = user.display_name if user else "Unknown Spirit"
            embed.add_field(name=f"#{i} - {name}",
                            value=f"💸 {losses:,} SP lost to the void",
                            inline=False)
    await ctx.send(embed=embed)


@bot.command(name="help")
async def custom_help(ctx):
    embed = discord.Embed(
        title="📜 Gu Chang's Spiritual Codex",
        description=
        "*These are the pathways to power, mortal. Study them well.*",
        color=discord.Color.blue())
    embed.add_field(name="!daily",
                    value="*Claim your daily tribute of Spirit Points*",
                    inline=False)
    embed.add_field(name="!ssbal",
                    value="*Examine your Spirit Stone treasury*",
                    inline=False)
    embed.add_field(name="!spbal",
                    value="*Check your Spirit Point energy reserves*",
                    inline=False)
    embed.add_field(
        name="!exchange <amount/all>",
        value="*Convert Spirit Points to Spirit Stones (1:1 ratio)*",
        inline=False)
    embed.add_field(name="!coinflip <heads/tails> <amount>",
                    value="*Test your fate against the cosmic coin*",
                    inline=False)
    embed.add_field(name="!shop",
                    value="*Browse my collection of spiritual artifacts*",
                    inline=False)
    embed.add_field(name="!buy <item>",
                    value="*Claim an artifact if you prove worthy*",
                    inline=False)
    embed.add_field(name="!gift <user> <amount>",
                    value="*Transfer Spirit Stones to another*",
                    inline=False)
    embed.add_field(name="!transfer <user> <amount>",
                    value="*[ADMIN ONLY] Grant unlimited Spirit Stones*",
                    inline=False)
    embed.add_field(name="!top",
                    value="*Witness the hierarchy of spiritual power*",
                    inline=False)
    embed.add_field(name="!lucky",
                    value="*See who fortune has smiled upon this month*",
                    inline=False)
    embed.add_field(name="!unlucky",
                    value="*Observe those cursed by gambling misfortune*",
                    inline=False)
    embed.set_footer(
        text=
        "*Master these commands, and spiritual ascension shall be yours...*")
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
