import os, sqlite3, random, asyncio
import datetime
import time
from flask import Flask
import discord
from discord.ext import commands, tasks
from threading import Thread
from dotenv import load_dotenv
import base64
import json
import signal
import sys
import aiohttp
import requests
import shutil
from datetime import datetime, timezone, timedelta
import logging
import asyncio
from collections import defaultdict
import functools  # This should already be there, but verify it exists

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    logger.info("🛑 Shutdown signal received, closing bot...")
    try:
        if bot:
            asyncio.create_task(bot.close())
    except:
        pass
    sys.exit(0)


# Register signal handlers
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Rate limiting configuration
COMMAND_COOLDOWNS = {
    'daily': 86400,  # 24 hours
    'coinflip': 60,  # 1 minute
    'exchange': 10,  # 10 seconds
    'shop': 5,  # 5 seconds
    'buy': 30,  # 30 seconds
    'ssbal': 3,  # 3 seconds
    'spbal': 3,  # 3 seconds
    'top': 10,  # 10 seconds
    'lucky': 10,  # 10 seconds
    'unlucky': 10,  # 10 seconds
    'lose': 5,  # 5 seconds
    'help': 5,  # 5 seconds
    'sendsp': 5,  # ADD THIS - 5 seconds cooldown
    'nextconvert': 10,  # 10 seconds
}

# Global rate limiting storage
user_command_cooldowns = defaultdict(lambda: defaultdict(float))
global_command_usage = defaultdict(list)

# Discord API rate limiting
discord_api_calls = []
DISCORD_API_LIMIT = 50  # requests per minute
API_WINDOW = 60  # seconds


def check_discord_api_limit():
    """Check if we're within Discord API limits"""
    now = time.time()
    # Remove old entries
    global discord_api_calls
    discord_api_calls = [
        call_time for call_time in discord_api_calls
        if now - call_time < API_WINDOW
    ]

    if len(discord_api_calls) >= DISCORD_API_LIMIT:
        return False, (API_WINDOW - (now - discord_api_calls[0]))

    discord_api_calls.append(now)
    return True, 0


def check_command_cooldown(user_id, command_name):
    """Check if user can use a command (returns True if allowed)"""
    if command_name not in COMMAND_COOLDOWNS:
        return True, 0

    now = time.time()
    last_used = user_command_cooldowns[str(user_id)][command_name]
    cooldown = COMMAND_COOLDOWNS[command_name]

    if now - last_used < cooldown:
        remaining = cooldown - (now - last_used)
        return False, remaining

    user_command_cooldowns[str(user_id)][command_name] = now
    return True, 0


async def safe_api_call(func, *args, **kwargs):
    """Safely make API calls with retry logic"""
    max_retries = 3
    base_delay = 1

    for attempt in range(max_retries):
        try:
            # Check Discord API limits
            can_call, wait_time = check_discord_api_limit()
            if not can_call:
                logger.warning(
                    f"⚠️ Discord API limit reached, waiting {wait_time:.1f}s")
                await asyncio.sleep(wait_time + 1)

            result = await func(*args, **kwargs)
            return result, None

        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry_after = getattr(e, 'retry_after',
                                      base_delay * (2**attempt))
                logger.warning(
                    f"⚠️ Rate limited, waiting {retry_after}s (attempt {attempt + 1})"
                )
                await asyncio.sleep(retry_after)
                continue
            elif e.status >= 500:  # Server error
                delay = base_delay * (2**attempt)
                logger.warning(
                    f"⚠️ Server error {e.status}, retrying in {delay}s")
                await asyncio.sleep(delay)
                continue
            else:
                return None, f"Discord API Error: {e.status} - {e.text}"

        except asyncio.TimeoutError:
            delay = base_delay * (2**attempt)
            logger.warning(f"⚠️ Timeout error, retrying in {delay}s")
            await asyncio.sleep(delay)
            continue

        except Exception as e:
            logger.error(f"❌ Unexpected error in API call: {e}")
            if attempt == max_retries - 1:
                return None, str(e)
            await asyncio.sleep(base_delay * (2**attempt))

    return None, "Max retries exceeded"


# Command cooldown decorator
def cooldown_check(command_name=None):
    """Decorator to add cooldown checking to commands"""

    def decorator(func):

        @functools.wraps(func)
        async def cooldown_wrapper(ctx, *args, **kwargs):
            cmd_name = command_name or func.__name__
            user_id = ctx.author.id

            can_use, remaining = check_command_cooldown(user_id, cmd_name)

            if not can_use:
                hours = int(remaining // 3600)
                minutes = int((remaining % 3600) // 60)
                seconds = int(remaining % 60)

                if hours > 0:
                    time_str = f"{hours}h {minutes}m {seconds}s"
                elif minutes > 0:
                    time_str = f"{minutes}m {seconds}s"
                else:
                    time_str = f"{seconds}s"

                embed = discord.Embed(
                    title="⏰ **COMMAND COOLDOWN**",
                    description=
                    f"```diff\n- Command '{cmd_name}' is on cooldown\n+ Try again in {time_str}\n```\n🌀 *The cosmic forces need time to recharge...*",
                    color=0xFF6347)

                embed.set_footer(
                    text=
                    f"⚡ Cooldown prevents API overload • {ctx.author.display_name}",
                    icon_url=ctx.author.avatar.url
                    if ctx.author.avatar else None)

                result, error = await safe_api_call(ctx.send, embed=embed)
                if error:
                    logger.error(f"❌ Failed to send cooldown message: {error}")
                return

            # Execute the original command
            try:
                await func(ctx, *args, **kwargs)
            except Exception as e:
                logger.error(f"❌ Error in command {cmd_name}: {e}")

                embed = discord.Embed(
                    title="❌ **COMMAND ERROR**",
                    description=
                    "```diff\n- An error occurred while processing your command\n+ Please try again later\n```",
                    color=0xFF0000)

                result, error = await safe_api_call(ctx.send, embed=embed)
                if error:
                    logger.error(f"❌ Failed to send error message: {error}")

        return cooldown_wrapper

    return decorator


def safe_command_wrapper(func):
    """Decorator to add extra error protection to critical commands"""

    @functools.wraps(func)
    async def wrapper(ctx, *args, **kwargs):
        try:
            await func(ctx, *args, **kwargs)
        except Exception as e:
            logger.error(f"❌ Critical error in {func.__name__}: {e}",
                         exc_info=True)

            # Send a simple error message without fancy embeds
            try:
                await ctx.send(
                    "❌ An unexpected error occurred. The issue has been logged."
                )
            except:
                pass  # If we can't even send a simple message, just log it

    return wrapper


# Enhanced message sending with rate limiting
async def safe_send(ctx, content=None, embed=None, **kwargs):
    """Safely send messages with rate limiting"""
    return await safe_api_call(ctx.send,
                               content=content,
                               embed=embed,
                               **kwargs)


async def safe_edit(message, content=None, embed=None, **kwargs):
    """Safely edit messages with rate limiting"""
    return await safe_api_call(message.edit,
                               content=content,
                               embed=embed,
                               **kwargs)


async def safe_add_reaction(message, emoji):
    """Safely add reactions with rate limiting"""
    return await safe_api_call(message.add_reaction, emoji)


async def safe_remove_roles(member, *roles):
    """Safely remove roles with rate limiting"""
    return await safe_api_call(member.remove_roles, *roles)


async def safe_add_roles(member, *roles):
    """Safely add roles with rate limiting"""
    return await safe_api_call(member.add_roles, *roles)


# Global error handler for uncaught exceptions
async def handle_global_error(ctx, error):
    """Handle global command errors with fallback protection"""
    logger.error(f"❌ Global error in {ctx.command}: {error}")

    if isinstance(error, commands.CommandOnCooldown):
        return  # Already handled by cooldown system

    try:
        embed = discord.Embed(
            title="💥 **SYSTEM ERROR**",
            description=
            "```diff\n- A system error occurred\n+ Our cosmic engineers have been notified\n```\n🔧 *Please try again in a few moments...*",
            color=0xFF1744)

        embed.add_field(name="🆘 **Error Code**",
                        value=f"```\n{type(error).__name__}\n```",
                        inline=True)

        embed.set_footer(
            text="🛠️ If this persists, contact an administrator",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

        # Use a simple send without the safe_send wrapper to prevent recursion
        try:
            await ctx.send(embed=embed)
        except Exception as send_error:
            # Fallback: try sending a simple text message
            try:
                await ctx.send(
                    "❌ A system error occurred. Please contact an administrator."
                )
            except:
                # Ultimate fallback: just log it
                logger.error(
                    f"❌ Could not send error message to user {ctx.author.id}")
                pass

    except Exception as handler_error:
        # Don't let the error handler itself crash the bot
        logger.error(f"❌ Error in global error handler: {handler_error}")


load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# ==== Constants ====
DB_FILE = "bot_database.db"
ROLE_ID_TEMP_ADMIN = 1393927331101544539  # Replace with actual ID
ROLE_ID_HMW = 1393927051685400790  # Replace with actual HMW role ID
ROLE_ID_ADMIN = 1397799884790169771  # Replace with actual Admin role ID
ROLE_ID_BOOSTER = 1393289422241271940  # Replace with actual Server Booster role ID
ROLE_ID_OWNER = 1393074903716073582
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_BACKUP_REPO = os.getenv(
    "GITHUB_BACKUP_REPO")  # Format: "username/repo-name"
GITHUB_API_BASE = "https://api.github.com"

SHOP_ITEMS = {
    "nickname_lock": {
        "price": 5000,
        "desc": "🔒 Locks your nickname from changes"
    },
    "temp_admin": {
        "price": 25000,
        "desc": "⚡ Gives temporary admin role for 1 hour"
    },
    "hmw_role": {
        "price": 50000,
        "desc": "👑 Grants the prestigious HMW role"
    },
    "name_change_card": {  # ADD THIS NEW ITEM
        "price": 10000,
        "desc": "🃏 Change someone's nickname for 24 hours"
    },
}


# ==== Database Setup ====
def init_database():
    """Initialize SQLite database with all required tables"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Users table for economy data
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            sp INTEGER DEFAULT 100,
            last_claim TEXT,
            streak INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Monthly stats table for gambling tracking
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS monthly_stats (
            user_id TEXT,
            month TEXT,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, month),
            FOREIGN KEY (user_id) REFERENCES users (user_id)
        )
    ''')

    # Nickname locks table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nickname_locks (
            user_id TEXT PRIMARY KEY,
            locked_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Temporary admins table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS temp_admins (
            user_id TEXT PRIMARY KEY,
            expires_at TEXT,
            guild_id TEXT,
            granted_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Add this table creation in init_database() function
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS name_change_cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id TEXT,
            target_id TEXT,
            original_nickname TEXT,
            new_nickname TEXT,
            expires_at TEXT,
            guild_id TEXT,
            used_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Transactions log for audit trail
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            transaction_type TEXT,
            amount INTEGER,
            balance_before INTEGER,
            balance_after INTEGER,
            description TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()


# ==== Database Helper Functions ====
def get_user_data(user_id):
    """Get user data, create if doesn't exist"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id, ))
    user = cursor.fetchone()

    if not user:
        cursor.execute(
            '''
            INSERT INTO users (user_id, balance, sp, streak)
            VALUES (?, 0, 100, 0)
        ''', (user_id, ))
        conn.commit()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id, ))
        user = cursor.fetchone()

    conn.close()
    return {
        'user_id': user[0],
        'balance': user[1],
        'sp': user[2],
        'last_claim': user[3],
        'streak': user[4],
        'created_at': user[5]
    }


def update_user_data(user_id, **kwargs):
    """Update user data"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # Get current data for transaction log
        current_data = get_user_data(user_id)

        # Build update query dynamically
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['balance', 'sp', 'last_claim', 'streak']:
                fields.append(f"{key} = ?")
                values.append(value)

        if fields:
            values.append(user_id)
            query = f"UPDATE users SET {', '.join(fields)} WHERE user_id = ?"
            cursor.execute(query, values)
            conn.commit()

        conn.close()
        return True
    except Exception as e:
        logger.error(f"❌ Database update error: {e}")
        if 'conn' in locals():
            conn.close()
        return False


def get_monthly_stats(user_id, month=None):
    """Get monthly gambling stats for user"""
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        '''
        SELECT wins, losses FROM monthly_stats 
        WHERE user_id = ? AND month = ?
    ''', (user_id, month))

    result = cursor.fetchone()
    conn.close()

    if result:
        return {'wins': result[0], 'losses': result[1]}
    return {'wins': 0, 'losses': 0}


async def test_bot_connection():
    """Test bot connection before full startup"""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": f"Bot {TOKEN}"}
            async with session.get("https://discord.com/api/v10/users/@me",
                                   headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(
                        f"✅ Bot token valid for: {data.get('username')}")
                    return True
                else:
                    logger.error(f"❌ Invalid bot token: {resp.status}")
                    return False
    except Exception as e:
        logger.error(f"❌ Connection test failed: {e}")
        return False


async def startup_backup():
    """Create backup on startup"""
    try:
        if github_backup and os.path.exists(DB_FILE):
            backup_file = create_backup_with_cloud_storage()
            if backup_file:
                success, result = github_backup.upload_backup_to_github(
                    backup_file)
                if success:
                    logger.info("✅ Startup backup created")
                    return True
        return False
    except Exception as e:
        logger.error(f"❌ Startup backup failed: {e}")
        return False


def update_monthly_stats(user_id, win_amount=0, loss_amount=0, month=None):
    """Update monthly gambling stats"""
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        '''
        INSERT OR REPLACE INTO monthly_stats (user_id, month, wins, losses)
        VALUES (?, ?, 
            COALESCE((SELECT wins FROM monthly_stats WHERE user_id = ? AND month = ?), 0) + ?,
            COALESCE((SELECT losses FROM monthly_stats WHERE user_id = ? AND month = ?), 0) + ?)
    ''', (user_id, month, user_id, month, win_amount, user_id, month,
          loss_amount))

    conn.commit()
    conn.close()


class GitHubBackupManager:

    def __init__(self, token, repo):
        self.token = token
        self.repo = repo
        self.headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        }

    def upload_backup_to_github(self, backup_file_path):
        """Upload backup file to GitHub repository"""
        try:
            # Read the backup file
            with open(backup_file_path, 'rb') as f:
                file_content = f.read()

            # Encode file content to base64
            encoded_content = base64.b64encode(file_content).decode('utf-8')

            # Create filename for GitHub
            filename = os.path.basename(backup_file_path)
            github_path = f"backups/{filename}"

            # Check if file already exists
            check_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/{github_path}"
            check_response = requests.get(check_url, headers=self.headers)

            # Prepare the commit data
            commit_data = {
                "message": f"🤖 Auto backup: {filename}",
                "content": encoded_content,
                "branch": "main"
            }

            # If file exists, we need the SHA for update
            if check_response.status_code == 200:
                existing_file = check_response.json()
                commit_data["sha"] = existing_file["sha"]

            # Upload/update the file
            upload_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/{github_path}"
            response = requests.put(upload_url,
                                    headers=self.headers,
                                    json=commit_data)

            if response.status_code in [200, 201]:
                return True, response.json()
            else:
                return False, f"GitHub API Error: {response.status_code} - {response.text}"

        except Exception as e:
            return False, str(e)

    def download_backup_from_github(self, filename=None):
        """Download backup file from GitHub repository"""
        try:
            if not filename:
                # Get the latest backup file
                list_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/backups"
                response = requests.get(list_url, headers=self.headers)

                if response.status_code != 200:
                    return False, "Failed to list backup files"

                files = response.json()
                backup_files = [f for f in files if f['name'].endswith('.db')]

                if not backup_files:
                    return False, "No backup files found in repository"

                # Sort by name (which includes timestamp) to get latest
                backup_files.sort(key=lambda x: x['name'], reverse=True)
                latest_file = backup_files[0]
                filename = latest_file['name']

            # Download the specific file
            download_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/backups/{filename}"
            response = requests.get(download_url, headers=self.headers)

            if response.status_code != 200:
                return False, f"Failed to download {filename}"

            file_data = response.json()

            # Decode base64 content
            file_content = base64.b64decode(file_data['content'])

            # Create local backups directory if it doesn't exist
            if not os.path.exists("backups"):
                os.makedirs("backups")

            # Save the downloaded file locally
            local_path = os.path.join("backups", filename)
            with open(local_path, 'wb') as f:
                f.write(file_content)

            return True, local_path

        except Exception as e:
            return False, str(e)

    def list_github_backups(self):
        """List all backup files in GitHub repository"""
        try:
            list_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/backups"
            response = requests.get(list_url, headers=self.headers)

            if response.status_code != 200:
                return False, []

            files = response.json()
            backup_files = [f for f in files if f['name'].endswith('.db')]

            # Sort by name (timestamp) in descending order
            backup_files.sort(key=lambda x: x['name'], reverse=True)

            return True, backup_files

        except Exception as e:
            return False, []


# Initialize GitHub backup manager
github_backup = None
if GITHUB_TOKEN and GITHUB_BACKUP_REPO:
    github_backup = GitHubBackupManager(GITHUB_TOKEN, GITHUB_BACKUP_REPO)


def create_backup_with_cloud_storage():
    """Create a backup file and return the file path"""
    try:
        # Create backups directory if it doesn't exist
        if not os.path.exists("backups"):
            os.makedirs("backups")

        # Generate backup filename with timestamp
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup_{timestamp}.db"
        backup_path = os.path.join("backups", backup_filename)

        # Copy the database file
        shutil.copy2(DB_FILE, backup_path)

        return backup_path
    except Exception as e:
        print(f"Backup creation error: {e}")
        return None


def restore_from_cloud():
    """Restore database from the most recent backup (local or GitHub)"""
    try:
        restored_from_github = False

        # Try to download latest from GitHub first
        if github_backup:
            success, result = github_backup.download_backup_from_github()
            if success:
                latest_backup = result
                restored_from_github = True
            else:
                print(f"GitHub download failed: {result}")

        # Fallback to local backups if GitHub fails
        if not restored_from_github:
            if not os.path.exists("backups"):
                return False

            backup_files = [
                f for f in os.listdir("backups")
                if f.startswith("backup_") and f.endswith(".db")
            ]
            if not backup_files:
                return False

            backup_files.sort(
                key=lambda x: os.path.getmtime(os.path.join("backups", x)),
                reverse=True)
            latest_backup = os.path.join("backups", backup_files[0])

        # Backup current database before restore
        current_backup = f"pre_restore_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2(DB_FILE, os.path.join("backups", current_backup))

        # Restore from backup
        shutil.copy2(latest_backup, DB_FILE)

        return True
    except Exception as e:
        print(f"Restore error: {e}")
        return False


def log_transaction(user_id,
                    transaction_type,
                    amount,
                    balance_before,
                    balance_after,
                    description=""):
    """Log transaction for audit trail"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        '''
        INSERT INTO transactions (user_id, transaction_type, amount, balance_before, balance_after, description)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (user_id, transaction_type, amount, balance_before, balance_after,
          description))

    conn.commit()
    conn.close()


def is_nickname_locked(user_id):
    """Check if user has nickname lock"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('SELECT user_id FROM nickname_locks WHERE user_id = ?',
                   (user_id, ))
    result = cursor.fetchone()
    conn.close()

    return result is not None


def add_name_change_card(owner_id, target_id, original_nick, new_nick,
                         expires_at, guild_id):
    """Add a name change card record"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        '''
        INSERT INTO name_change_cards (owner_id, target_id, original_nickname, new_nickname, expires_at, guild_id)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (owner_id, target_id, original_nick, new_nick, expires_at, guild_id))

    conn.commit()
    conn.close()


def get_active_name_changes():
    """Get all active name changes"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, owner_id, target_id, original_nickname, expires_at, guild_id 
        FROM name_change_cards
    ''')
    results = cursor.fetchall()
    conn.close()

    return [{
        'id': r[0],
        'owner_id': r[1],
        'target_id': r[2],
        'original_nickname': r[3],
        'expires_at': r[4],
        'guild_id': r[5]
    } for r in results]


def remove_name_change_card(card_id):
    """Remove a name change card record"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('DELETE FROM name_change_cards WHERE id = ?', (card_id, ))
    conn.commit()
    conn.close()


def add_nickname_lock(user_id):
    """Add nickname lock for user"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        'INSERT OR REPLACE INTO nickname_locks (user_id) VALUES (?)',
        (user_id, ))
    conn.commit()
    conn.close()


def get_temp_admins():
    """Get all temp admins"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('SELECT user_id, expires_at, guild_id FROM temp_admins')
    results = cursor.fetchall()
    conn.close()

    return [{
        'user_id': r[0],
        'expires_at': r[1],
        'guild_id': r[2]
    } for r in results]


def add_temp_admin(user_id, expires_at, guild_id):
    """Add temporary admin"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        '''
        INSERT OR REPLACE INTO temp_admins (user_id, expires_at, guild_id)
        VALUES (?, ?, ?)
    ''', (user_id, expires_at, guild_id))

    conn.commit()
    conn.close()


def remove_temp_admin(user_id):
    """Remove temporary admin"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('DELETE FROM temp_admins WHERE user_id = ?', (user_id, ))
    conn.commit()
    conn.close()


def get_leaderboard(field='balance', limit=10):
    """Get leaderboard by specified field"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    query = f'SELECT user_id, {field} FROM users ORDER BY {field} DESC LIMIT ?'
    cursor.execute(query, (limit, ))
    results = cursor.fetchall()
    conn.close()

    return results


def get_top_losers(month=None, limit=10):
    """Get top losers for the month"""
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        '''
        SELECT user_id, losses FROM monthly_stats 
        WHERE month = ? ORDER BY losses DESC LIMIT ?
    ''', (month, limit))

    results = cursor.fetchall()
    conn.close()

    return results


# Initialize database on startup
def ensure_database_exists():
    """Ensure database exists, restore from backup if needed"""
    if not os.path.exists(DB_FILE):
        logger.warning("⚠️ Database not found, attempting restore...")
        if github_backup and restore_from_cloud():
            logger.info("✅ Database restored from backup")
        else:
            logger.info("📝 Creating new database...")
            init_database()
    else:
        init_database()  # Ensure all tables exist


# Initialize database on startup
ensure_database_exists()


# ==== Input Validation ====
def validate_amount(amount_str, max_amount=1000000):
    """Validate and convert amount string to integer"""
    if amount_str.lower() == "all":
        return "all"
    try:
        amount = int(amount_str)
        if amount <= 0:
            return None
        if amount > max_amount:
            return None
        return amount
    except ValueError:
        return None


# ==== Auto Backup Task ====
@tasks.loop(hours=6)  # Backup every 6 hours
async def auto_backup():
    """Automatically backup database to GitHub"""
    try:
        if github_backup:
            backup_file = create_backup_with_cloud_storage()
            if backup_file:
                success, result = github_backup.upload_backup_to_github(
                    backup_file)
                if success:
                    logger.info(f"✅ Auto backup completed: {backup_file}")
                else:
                    logger.error(f"❌ Auto backup failed: {result}")
    except Exception as e:
        logger.error(f"❌ Auto backup error: {e}")


# ==== Monthly Conversion System ====


def get_all_users_with_sp():
    """Get all users who have SP > 0"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute('SELECT user_id, sp, balance FROM users WHERE sp > 0')
    results = cursor.fetchall()
    conn.close()

    return [{'user_id': r[0], 'sp': r[1], 'balance': r[2]} for r in results]


def reset_monthly_stats():
    """Reset monthly gambling stats for new month"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Clear previous month's stats (keep only current month)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    cursor.execute('DELETE FROM monthly_stats WHERE month != ?',
                   (current_month, ))

    conn.commit()
    conn.close()


async def perform_monthly_conversion():
    """Convert all SP to SS for all users and reset SP"""
    try:
        users_with_sp = get_all_users_with_sp()
        total_converted = 0
        conversion_count = 0

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        for user_data in users_with_sp:
            user_id = user_data['user_id']
            sp_amount = user_data['sp']
            old_balance = user_data['balance']

            if sp_amount > 0:
                # Convert SP to SS
                new_balance = old_balance + sp_amount
                new_sp = 100  # Reset to starting SP amount

                # Update database
                cursor.execute(
                    '''
                    UPDATE users SET balance = ?, sp = ? WHERE user_id = ?
                ''', (new_balance, new_sp, user_id))

                # Log transaction
                log_transaction(
                    user_id, "monthly_conversion", sp_amount, old_balance,
                    new_balance,
                    f"Monthly auto-conversion: {sp_amount} SP → SS")

                total_converted += sp_amount
                conversion_count += 1

                logger.info(
                    f"✅ Converted {sp_amount} SP → SS for user {user_id}")

        conn.commit()
        conn.close()

        # Reset monthly gambling stats
        reset_monthly_stats()

        logger.info(
            f"🎯 Monthly conversion complete: {conversion_count} users, {total_converted:,} SP converted"
        )
        return total_converted, conversion_count

    except Exception as e:
        logger.error(f"❌ Monthly conversion error: {e}")
        return 0, 0


@tasks.loop(hours=1)  # Check every hour
async def monthly_conversion_check():
    """Check if it's time for monthly conversion (1st of month, 00:00 UTC)"""
    try:
        now = datetime.now(timezone.utc)

        # Check if it's the 1st day of the month and between 00:00-01:00
        if now.day == 1 and now.hour == 0:
            logger.info("🗓️ Monthly conversion time detected!")

            total_converted, user_count = await perform_monthly_conversion()

            if total_converted > 0:
                # Create backup after monthly conversion
                if github_backup:
                    backup_file = create_backup_with_cloud_storage()
                    if backup_file:
                        success, result = github_backup.upload_backup_to_github(
                            backup_file)
                        if success:
                            logger.info("✅ Post-conversion backup created")

                # Notify in all guilds (optional)
                for guild in bot.guilds:
                    # Find a general channel to announce
                    channel = discord.utils.get(
                        guild.channels,
                        name='general') or guild.text_channels[0]
                    if channel:
                        try:
                            embed = discord.Embed(
                                title="🌟 **MONTHLY ASCENSION COMPLETE** 🌟",
                                description=
                                f"```css\n[SPIRITUAL ENERGY CRYSTALLIZATION RITUAL]\n```\n💎 *The cosmic cycle renews, power has been preserved...*",
                                color=0x00FF7F)

                            embed.add_field(
                                name="⚗️ **CONVERSION RESULTS**",
                                value=
                                f"```yaml\nUsers Affected: {user_count:,}\nTotal SP Converted: {total_converted:,}\nConversion Rate: 1 SP = 1 SS\n```",
                                inline=False)

                            embed.add_field(
                                name="🔄 **WHAT HAPPENED?**",
                                value=
                                "```diff\n+ All Spirit Points → Spirit Stones\n+ SP reset to 100 for everyone\n+ Monthly gambling stats reset\n+ Your wealth is now permanent!\n```",
                                inline=False)

                            embed.add_field(
                                name="🚀 **NEW MONTH BEGINS**",
                                value=
                                "```fix\nFresh start for daily claims and gambling!\nYour converted SS is safe forever.\nTime to build your SP again!\n```",
                                inline=False)

                            embed.set_footer(
                                text=
                                f"💫 Monthly Conversion • {now.strftime('%B %Y')}",
                                icon_url=guild.icon.url
                                if guild.icon else None)

                            await channel.send(embed=embed)
                            logger.info(
                                f"📢 Monthly conversion announced in {guild.name}"
                            )

                        except Exception as e:
                            logger.error(
                                f"❌ Failed to announce in {guild.name}: {e}")

    except Exception as e:
        logger.error(f"❌ Monthly conversion check error: {e}")


# API Health monitoring
@tasks.loop(minutes=5)
async def api_health_monitor():
    """Monitor API health and reset counters"""
    try:
        now = time.time()

        # Clean old API call records
        global discord_api_calls
        discord_api_calls = [
            call_time for call_time in discord_api_calls
            if now - call_time < API_WINDOW
        ]

        # Log API usage stats
        if len(discord_api_calls) > 0:
            usage_percentage = (len(discord_api_calls) /
                                DISCORD_API_LIMIT) * 100
            if usage_percentage > 80:
                logger.warning(
                    f"⚠️ High API usage: {usage_percentage:.1f}% ({len(discord_api_calls)}/{DISCORD_API_LIMIT})"
                )
            else:
                logger.info(
                    f"📊 API usage: {usage_percentage:.1f}% ({len(discord_api_calls)}/{DISCORD_API_LIMIT})"
                )

        # Clean old cooldown records (older than 24 hours)
        for user_id in list(user_command_cooldowns.keys()):
            for command in list(user_command_cooldowns[user_id].keys()):
                if now - user_command_cooldowns[user_id][
                        command] > 86400:  # 24 hours
                    del user_command_cooldowns[user_id][command]

            # Remove empty user records
            if not user_command_cooldowns[user_id]:
                del user_command_cooldowns[user_id]

    except Exception as e:
        logger.error(f"❌ API health monitor error: {e}")


async def main():
    """Main async function with proper startup sequence"""
    logger.info("🚀 Starting Discord Bot...")

    # Test connection first
    if not await test_bot_connection():
        logger.error("❌ Bot token validation failed")
        return

    # Create startup backup
    await startup_backup()

    # Start the bot
    try:
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        logger.info("🛑 Keyboard interrupt received")
    except Exception as e:
        logger.error(f"❌ Bot error: {e}")
    finally:
        if not bot.is_closed():
            await bot.close()
        logger.info("🛑 Bot shutdown complete")


# ==== Flask Setup ====
app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is alive!"


@app.route('/health')
def health():
    """Enhanced health check for monitoring"""
    try:
        # Test database connection
        conn = sqlite3.connect(DB_FILE)
        conn.close()

        # Test bot status
        bot_status = "online" if bot.is_ready() else "offline"

        return {
            "status": "healthy",
            "bot_status": bot_status,
            "database": "connected",
            "backup_configured": github_backup is not None
        }
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}, 500


# ==== Discord Bot Setup ====
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
last_gamble_times = {}


# ==== Background Task for Temp Admin Management ====
@tasks.loop(minutes=5)
async def remove_expired_items():
    """Remove expired temp admin roles and name changes"""
    # Handle temp admins
    temp_admins = get_temp_admins()
    now = datetime.now(timezone.utc)

    for admin_data in temp_admins:
        expire_time = datetime.fromisoformat(admin_data["expires_at"])
        if now >= expire_time:
            try:
                guild = bot.get_guild(int(admin_data["guild_id"]))
                if guild:
                    member = guild.get_member(int(admin_data["user_id"]))
                    role = guild.get_role(ROLE_ID_TEMP_ADMIN)
                    if member and role:
                        result, error = await safe_remove_roles(member, role)
                remove_temp_admin(admin_data["user_id"])
            except Exception as e:
                logger.error(f"Error removing temp admin role: {e}")

    # Handle name changes
    active_name_changes = get_active_name_changes()

    for change in active_name_changes:
        expire_time = datetime.fromisoformat(change["expires_at"])
        if now >= expire_time:
            try:
                guild = bot.get_guild(int(change["guild_id"]))
                if guild:
                    member = guild.get_member(int(change["target_id"]))
                    if member:
                        original_nick = change["original_nickname"]
                        if original_nick == "None":
                            original_nick = None
                        await member.edit(nick=original_nick,
                                          reason="Name change card expired")

                remove_name_change_card(change["id"])
                logger.info(
                    f"✅ Name change expired for user {change['target_id']}")

            except Exception as e:
                logger.error(f"❌ Error restoring nickname: {e}")


# ==== Bot Events ====
# REPLACE your existing on_ready function with this enhanced version:
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    logger.info(f"✅ Bot logged in as {bot.user}")

    # Start background tasks
    if not remove_expired_items.is_running():
        remove_expired_items.start()
    if not auto_backup.is_running():
        auto_backup.start()
    if not monthly_conversion_check.is_running():
        monthly_conversion_check.start()
    if not api_health_monitor.is_running():
        api_health_monitor.start()

    logger.info("✅ All background tasks started")

    # Try to create initial backup
    try:
        if github_backup:
            backup_file = create_backup_with_cloud_storage()
            if backup_file:
                success, result = github_backup.upload_backup_to_github(
                    backup_file)
                if success:
                    logger.info("✅ Initial backup created on startup")
    except Exception as e:
        logger.error(f"❌ Initial backup failed: {e}")


@bot.event
async def on_error(event, *args, **kwargs):
    logger.error(f"❌ Error in event {event}: {args}")


@bot.event
async def on_command_error(ctx, error):
    """Enhanced command error handler with comprehensive coverage"""

    # Handle cooldown errors (already handled by decorator)
    if isinstance(error, commands.CommandOnCooldown):
        # Don't send duplicate cooldown messages since decorator handles this
        return

    # Handle permission errors
    elif isinstance(error, commands.MissingPermissions):
        embed = discord.Embed(
            title="🚫 **ACCESS DENIED**",
            description=
            "```diff\n- Insufficient permissions\n+ Administrator access required\n```",
            color=0xFF0000)

        try:
            await ctx.send(embed=embed)
        except:
            await ctx.send("🚫 You don't have permission to use this command.")

    # Handle missing arguments
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(
            title="❌ **MISSING ARGUMENT**",
            description=
            f"```diff\n- Missing required parameter: {error.param.name}\n+ Use !help for correct usage\n```",
            color=0xFF4500)

        try:
            await ctx.send(embed=embed)
        except:
            await ctx.send(f"❌ Missing required argument: {error.param.name}")

    # Handle bad arguments (wrong type, etc.)
    elif isinstance(error, commands.BadArgument):
        embed = discord.Embed(
            title="⚠️ **INVALID ARGUMENT**",
            description=
            "```diff\n- Invalid argument provided\n+ Check your input and try again\n```",
            color=0xFF6347)

        try:
            await ctx.send(embed=embed)
        except:
            await ctx.send("⚠️ Invalid argument provided.")

    # Handle command not found (optional - you can remove this to ignore)
    elif isinstance(error, commands.CommandNotFound):
        # Silently ignore unknown commands
        return

    # Handle user input errors
    elif isinstance(error, commands.UserInputError):
        embed = discord.Embed(
            title="📝 **INPUT ERROR**",
            description=
            "```diff\n- Invalid input format\n+ Use !help for command examples\n```",
            color=0xFFB347)

        try:
            await ctx.send(embed=embed)
        except:
            await ctx.send("📝 Invalid input. Use !help for guidance.")

    # Handle bot missing permissions
    elif isinstance(error, commands.BotMissingPermissions):
        missing_perms = ', '.join(error.missing_permissions)
        embed = discord.Embed(
            title="🤖 **BOT PERMISSION ERROR**",
            description=
            f"```diff\n- Bot lacks required permissions\n+ Missing: {missing_perms}\n```",
            color=0xFF1744)

        try:
            await ctx.send(embed=embed)
        except:
            await ctx.send(f"🤖 Bot missing permissions: {missing_perms}")

    # Handle check failures (custom command checks)
    elif isinstance(error, commands.CheckFailure):
        embed = discord.Embed(
            title="🛡️ **ACCESS RESTRICTED**",
            description=
            "```diff\n- Command requirements not met\n+ Check role/permission requirements\n```",
            color=0xFF6347)

        try:
            await ctx.send(embed=embed)
        except:
            await ctx.send(
                "🛡️ You don't meet the requirements for this command.")

    # Handle command disabled
    elif isinstance(error, commands.DisabledCommand):
        embed = discord.Embed(
            title="🚫 **COMMAND DISABLED**",
            description=
            "```diff\n- This command is temporarily disabled\n+ Try again later\n```",
            color=0x808080)

        try:
            await ctx.send(embed=embed)
        except:
            await ctx.send("🚫 This command is currently disabled.")

    # Handle unexpected errors
    else:
        # Log the full error for debugging
        logger.error(f"❌ Unhandled command error in {ctx.command}: {error}",
                     exc_info=True)

        # Call the global error handler for unhandled cases
        await handle_global_error(ctx, error)


@bot.event
async def on_member_update(before, after):
    """Prevent nickname changes for users with nickname locks"""
    if before.nick != after.nick:
        if is_nickname_locked(str(after.id)):
            try:
                await after.edit(nick=before.nick,
                                 reason="Nickname locked by user")
            except discord.Forbidden:
                pass


# ==== Commands ====
@bot.command()
@safe_command_wrapper
@cooldown_check('daily')
async def daily(ctx):
    try:
        user_id = str(ctx.author.id)
        now = datetime.now(timezone.utc)
        user_data = get_user_data(user_id)

        last_claim = user_data.get("last_claim")
        streak = user_data.get("streak", 0)
        base_reward = 300

        # Check user roles by ID and set reward accordingly
        user_role_ids = [role.id for role in ctx.author.roles]

        if ROLE_ID_ADMIN in user_role_ids:
            base_reward = 400
            role_bonus = "👑 **ADMIN BLESSING** (+100 SP)"
        elif ROLE_ID_HMW in user_role_ids:
            base_reward = 350
            role_bonus = "⚡ **HMW PRIVILEGE** (+50 SP)"
        elif ROLE_ID_BOOSTER in user_role_ids:
            base_reward = 350
            role_bonus = "💎 **BOOSTER BONUS** (+50 SP)"
        else:
            role_bonus = "🔰 **STANDARD RATE**"

        if last_claim:
            # FIX: Ensure last_time is timezone-aware
            try:
                last_time = datetime.fromisoformat(last_claim)
                # If the datetime doesn't have timezone info, assume UTC
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
            except ValueError:
                # Handle old datetime format without timezone
                last_time = datetime.strptime(last_claim,
                                              "%Y-%m-%d %H:%M:%S.%f")
                last_time = last_time.replace(tzinfo=timezone.utc)

            delta = (now - last_time).days
            if delta == 0:
                remaining = 24 - (now - last_time).seconds // 3600
                embed = discord.Embed(
                    title="⏰ **TEMPORAL LOCK ACTIVE**",
                    description=
                    f"```diff\n- ENERGY RESERVES DEPLETED\n+ Regeneration in {remaining}h\n```\n🌟 *The cosmic energy needs time to flow through your soul...*",
                    color=0x2B2D42)
                embed.set_footer(text="⚡ Daily energy recharging...",
                                 icon_url=ctx.author.avatar.url
                                 if ctx.author.avatar else None)
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

        # Update user data
        old_sp = user_data.get('sp', 0)
        new_sp = old_sp + reward

        update_user_data(user_id,
                         sp=new_sp,
                         last_claim=now.isoformat(),
                         streak=streak)

        # Log transaction
        log_transaction(user_id, "daily_claim", reward, old_sp, new_sp,
                        f"Daily claim with {role_bonus}")

        # Dynamic progress bar with better styling - Red to Green progression
        streak_emojis = ['🟥', '🟥', '🟧', '🟨',
                         '🟩']  # Red -> Orange -> Yellow -> Green
        filled_emojis = ['🟩', '🟩', '🟩', '🟩', '🟩']  # All green when filled
        bar = ''.join(
            [filled_emojis[i] if i < streak else '🟥' for i in range(5)])

        embed = discord.Embed(
            title="⚡ **DAILY ENERGY HARVESTED** ⚡",
            description=
            f"```css\n[SPIRITUAL ENERGY CHANNELING COMPLETE]\n```\n💫 *The universe grants you its power...*",
            color=0x8A2BE2 if streak >= 3 else 0x4169E1)

        embed.add_field(
            name="🎁 **REWARDS CLAIMED**",
            value=f"```diff\n+ {reward:,} Spirit Points\n```\n{role_bonus}",
            inline=False)

        embed.add_field(
            name="🔥 **STREAK PROGRESSION**",
            value=
            f"{bar} `{streak}/5`\n{'🌟 *STREAK BONUS ACTIVE!*' if streak >= 3 else '💪 *Keep the momentum going!*'}",
            inline=False)

        embed.set_thumbnail(
            url=ctx.author.avatar.url if ctx.author.avatar else None)
        embed.set_footer(
            text=
            f"⚡ Next claim available in 24 hours • {ctx.author.display_name}",
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

        result, error = await safe_send(ctx, embed=embed)
        if error:
            logger.error(f"❌ Failed to send message: {error}")

    except Exception as e:
        logger.error(f"❌ Daily command error: {e}")
        await ctx.send("❌ An error occurred while processing your daily claim."
                       )


@bot.command()
@safe_command_wrapper
@commands.has_permissions(administrator=True)
async def forceconvert(ctx):
    """Manually trigger monthly conversion (Admin only)"""
    embed = discord.Embed(
        title="⚠️ **FORCE CONVERSION WARNING** ⚠️",
        description=("```diff\n! MANUAL MONTHLY CONVERSION TRIGGER !\n```\n"
                     "🔥 **THIS WILL:**\n"
                     "• Convert ALL users' SP to SS immediately\n"
                     "• Reset everyone's SP to 100\n"
                     "• Clear monthly gambling stats\n"
                     "• Cannot be undone!\n\n"
                     "React with ✅ to proceed or ❌ to cancel."),
        color=0xFF6B00)

    message = await ctx.send(embed=embed)
    await message.add_reaction("✅")
    await message.add_reaction("❌")

    def check(reaction, user):
        return (user == ctx.author and str(reaction.emoji) in ["✅", "❌"]
                and reaction.message.id == message.id)

    try:
        reaction, user = await bot.wait_for('reaction_add',
                                            timeout=30.0,
                                            check=check)

        if str(reaction.emoji) == "✅":
            embed = discord.Embed(
                title="🔄 **PROCESSING CONVERSION...**",
                description=
                "```css\n[MANUAL CONVERSION IN PROGRESS]\n```\n⚗️ *Converting all spiritual energy to eternal stones...*",
                color=0xFFAA00)

            await message.edit(embed=embed)

            total_converted, user_count = await perform_monthly_conversion()

            if total_converted > 0:
                embed = discord.Embed(
                    title="✅ **CONVERSION COMPLETE**",
                    description=
                    "```fix\n◆ MANUAL MONTHLY CONVERSION SUCCESSFUL ◆\n```\n💎 *All spiritual energy has been crystallized!*",
                    color=0x00FF00)

                embed.add_field(
                    name="📊 **CONVERSION STATISTICS**",
                    value=
                    f"```yaml\nUsers Processed: {user_count:,}\nTotal SP Converted: {total_converted:,}\nConversion Rate: 1:1\nSP Reset To: 100\n```",
                    inline=False)

                embed.add_field(
                    name="🔧 **ADMINISTRATOR**",
                    value=
                    f"```ansi\n\u001b[0;36m{ctx.author.display_name}\u001b[0m\n```",
                    inline=True)

                embed.set_footer(
                    text="💫 Manual conversion completed by administrator")
            else:
                embed = discord.Embed(
                    title="ℹ️ **NO CONVERSION NEEDED**",
                    description="```yaml\nNo users had SP to convert\n```",
                    color=0x87CEEB)
        else:
            embed = discord.Embed(
                title="❌ **CONVERSION CANCELLED**",
                description=
                "```css\n[MANUAL CONVERSION ABORTED]\n```\n🛡️ *No changes made to user balances.*",
                color=0x808080)

    except asyncio.TimeoutError:
        embed = discord.Embed(
            title="⏰ **TIMEOUT**",
            description=
            "```css\n[CONVERSION TIMEOUT]\n```\n🕐 *No response received. Conversion cancelled.*",
            color=0x808080)

    await message.edit(embed=embed)


@bot.command()
@safe_command_wrapper
@cooldown_check('nextconvert')
async def nextconvert(ctx):
    """Show when the next monthly conversion will happen"""
    now = datetime.now(timezone.utc)

    # Calculate next conversion date
    if now.day == 1 and now.hour == 0:
        next_conversion = "🔄 **HAPPENING NOW!**"
        time_remaining = "Conversion in progress..."
    else:
        # Next month, 1st day
        if now.month == 12:
            next_month = now.replace(year=now.year + 1,
                                     month=1,
                                     day=1,
                                     hour=0,
                                     minute=0,
                                     second=0,
                                     microsecond=0)
        else:
            next_month = now.replace(month=now.month + 1,
                                     day=1,
                                     hour=0,
                                     minute=0,
                                     second=0,
                                     microsecond=0)

        time_diff = next_month - now
        days = time_diff.days
        hours = time_diff.seconds // 3600

        next_conversion = next_month.strftime("%B 1st, %Y at 00:00 UTC")
        time_remaining = f"{days} days, {hours} hours"

    embed = discord.Embed(
        title="🗓️ **MONTHLY CONVERSION SCHEDULE** 🗓️",
        description=
        "```css\n[AUTOMATIC SP → SS CONVERSION]\n```\n⏰ *When the cosmic cycle completes, all energy crystallizes...*",
        color=0x4169E1)

    embed.add_field(name="📅 **NEXT CONVERSION**",
                    value=f"```fix\n{next_conversion}\n```",
                    inline=False)

    embed.add_field(name="⏳ **TIME REMAINING**",
                    value=f"```yaml\n{time_remaining}\n```",
                    inline=True)

    embed.add_field(
        name="⚗️ **WHAT HAPPENS?**",
        value=
        "```diff\n+ All SP converts to SS (1:1)\n+ SP resets to 100 for everyone\n+ Monthly stats reset\n+ Automatic backup created\n```",
        inline=False)

    embed.add_field(
        name="💡 **PRO TIP**",
        value=
        "```fix\nSP converts automatically - no action needed!\nYour SS balance is permanent storage.\n```",
        inline=False)

    embed.set_footer(
        text=
        "💫 Monthly conversion happens automatically on the 1st of each month",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@commands.has_permissions(administrator=True)
async def cloudbackup(ctx):
    """Create a manual backup with GitHub cloud storage"""
    embed = discord.Embed(
        title="☁️ **Creating Cloud Backup...**",
        description=
        "```css\n[SPIRITUAL DATABASE PRESERVATION RITUAL]\n```\n💾 *Crystallizing the cosmic data into eternal storage...*",
        color=0x00FFAA)

    message = await ctx.send(embed=embed)

    try:
        # Create local backup first
        backup_file = create_backup_with_cloud_storage()

        if backup_file:
            # Try to upload to GitHub
            github_success = False
            github_error = "Not configured"

            if github_backup:
                # Update embed to show GitHub upload in progress
                embed.description = "```css\n[UPLOADING TO GITHUB CLOUD STORAGE]\n```\n☁️ *Transferring cosmic data to the eternal vault...*"
                await message.edit(embed=embed)

                success, result = github_backup.upload_backup_to_github(
                    backup_file)
                if success:
                    github_success = True
                else:
                    github_error = result

            # Create final success embed
            embed = discord.Embed(
                title="✅ **Cloud Backup Created**",
                description=
                "```fix\n◆ COSMIC DATA PRESERVATION COMPLETE ◆\n```\n💎 *Database backup created and processed!*",
                color=0x00FF00)

            embed.add_field(
                name="📁 **Local File**",
                value=f"```yaml\n💾 {os.path.basename(backup_file)}\n```",
                inline=True)

            # GitHub status
            if github_success:
                embed.add_field(name="☁️ **GitHub Cloud**",
                                value="```diff\n+ ✅ Uploaded to GitHub\n```",
                                inline=True)
            else:
                embed.add_field(
                    name="☁️ **GitHub Cloud**",
                    value=
                    f"```diff\n- ❌ Upload failed\n- {github_error[:50]}...\n```",
                    inline=True)

            # Database statistics
            file_size = os.path.getsize(backup_file)
            embed.add_field(
                name="📊 **Backup Statistics**",
                value=
                f"```yaml\nTables Backed Up: 5\nFile Size: {file_size:,} bytes\nTimestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\nLocation: {'GitHub + Local' if github_success else 'Local Only'}\n```",
                inline=False)

            embed.set_footer(
                text=
                "💫 Your cosmic data is now safely preserved in the eternal vault",
                icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
        else:
            embed = discord.Embed(
                title="❌ **Backup Failed**",
                description=
                "```diff\n- COSMIC ERROR DETECTED\n```\n💀 *Failed to create backup. Check logs for details.*",
                color=0xFF0000)

    except Exception as e:
        embed = discord.Embed(
            title="❌ **Backup Error**",
            description=
            f"```diff\n- RITUAL INTERRUPTED\n```\n💀 *An error occurred: {str(e)[:100]}...*",
            color=0xFF0000)

    await message.edit(embed=embed)


@bot.command()
@safe_command_wrapper
@cooldown_check('usename')
async def usename(ctx, member: discord.Member, *, new_nickname: str):
    """Use a name change card on someone"""
    user_id = str(ctx.author.id)
    user_data = get_user_data(user_id)

    # Check if they have a name change card (you'll need to track ownership)
    # For simplicity, let's check if they have enough balance as if buying it instantly
    if user_data["balance"] < 10000:
        embed = discord.Embed(
            title="🚫 **NO NAME CHANGE CARD**",
            description=
            "```diff\n- You don't own a name change card\n+ Purchase one from !shop first\n```",
            color=0xFF0000)
        return await ctx.send(embed=embed)

    # Check if target has nickname lock
    if is_nickname_locked(str(member.id)):
        embed = discord.Embed(
            title="🔒 **TARGET PROTECTED**",
            description=
            "```diff\n- Target has nickname lock active\n+ Cannot change protected nicknames\n```",
            color=0xFF6347)
        result, error = await safe_send(ctx, embed=embed)
        return

    # Check nickname length and validity
    if len(new_nickname) > 32:
        embed = discord.Embed(
            title="❌ **NICKNAME TOO LONG**",
            description="```diff\n- Maximum 32 characters allowed\n```",
            color=0xFF0000)
        result, error = await safe_send(ctx, embed=embed)
        return

    try:
        # Store original nickname
        original_nick = member.nick if member.nick else str(
            member.display_name)

        # Change the nickname
        await member.edit(
            nick=new_nickname,
            reason=f"Name change card used by {ctx.author.display_name}")

        # Deduct cost and record the change
        new_balance = user_data["balance"] - 10000
        update_user_data(user_id, balance=new_balance)

        # Set expiry (24 hours from now)
        expires_at = (datetime.now(timezone.utc) +
                      datetime.timedelta(hours=24)).isoformat()

        # Record the name change
        add_name_change_card(user_id, str(member.id), original_nick,
                             new_nickname, expires_at, str(ctx.guild.id))

        # Log transaction
        log_transaction(user_id, "name_change_card", -10000,
                        user_data["balance"], new_balance,
                        f"Used name change card on {member.display_name}")

        embed = discord.Embed(
            title="🃏 **NAME CHANGE CARD ACTIVATED** 🃏",
            description=
            "```fix\n◆ IDENTITY MANIPULATION SUCCESSFUL ◆\n```\n✨ *Reality bends to your will...*",
            color=0xFF1493)

        embed.add_field(
            name="🎯 **TARGET**",
            value=f"```ansi\n\u001b[0;36m{member.display_name}\u001b[0m\n```",
            inline=True)

        embed.add_field(
            name="🔄 **NAME CHANGE**",
            value=f"```diff\n- {original_nick}\n+ {new_nickname}\n```",
            inline=True)

        embed.add_field(name="⏰ **DURATION**",
                        value="```yaml\n24 Hours\n```",
                        inline=True)

        embed.add_field(name="💸 **COST**",
                        value="```diff\n- 10,000 Spirit Stones\n```",
                        inline=False)

        embed.set_footer(
            text="🃏 Name will automatically revert after 24 hours",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

        result, error = await safe_send(ctx, embed=embed)
        if error:
            logger.error(f"❌ Failed to send message: {error}")

    except discord.Forbidden:
        embed = discord.Embed(
            title="🚫 **PERMISSION DENIED**",
            description=
            "```diff\n- Bot lacks permission to change nicknames\n+ Check bot role hierarchy\n```",
            color=0xFF0000)
        await ctx.send(embed=embed)

    except Exception as e:
        logger.error(f"❌ Name change error: {e}")
        embed = discord.Embed(
            title="❌ **NAME CHANGE FAILED**",
            description=
            "```diff\n- An error occurred\n+ Please try again later\n```",
            color=0xFF0000)
        await ctx.send(embed=embed)


@bot.command()
@safe_command_wrapper
@cooldown_check('sendsp')
async def sendsp(ctx, member: discord.Member, amount: int):
    """Send Spirit Points to another user (Owner only)"""
    try:
        # Check if user has the Owner role
        user_role_ids = [role.id for role in ctx.author.roles]
        if ROLE_ID_OWNER not in user_role_ids:
            embed = discord.Embed(
                title="👑 **OWNER ACCESS REQUIRED** 👑",
                description=(
                    "```diff\n"
                    "- COSMIC AUTHORITY INSUFFICIENT\n"
                    "- Only the realm Owner may grant Spirit Points\n"
                    "```\n"
                    "⚡ *This power belongs to the supreme ruler alone...*"),
                color=0xFF0000)
            return await ctx.send(embed=embed)

        # Validate amount
        if amount <= 0:
            embed = discord.Embed(
                title="❌ **INVALID AMOUNT** ❌",
                description=("```diff\n"
                             "- ERROR: Amount must be positive\n"
                             "+ Enter a valid positive number\n"
                             "```"),
                color=0xFF4500)
            return await ctx.send(embed=embed)

        # Add maximum amount check to prevent abuse
        if amount > 1000000:  # Adjust limit as needed
            embed = discord.Embed(
                title="⚠️ **AMOUNT TOO LARGE** ⚠️",
                description=("```diff\n"
                             "- ERROR: Amount exceeds maximum limit\n"
                             "+ Maximum: 1,000,000 SP per transaction\n"
                             "```"),
                color=0xFF4500)
            return await ctx.send(embed=embed)

        # Check if member is bot
        if member.bot:
            embed = discord.Embed(
                title="🤖 **INVALID TARGET** 🤖",
                description="```diff\n- Cannot grant SP to bots\n```",
                color=0xFF4500)
            return await ctx.send(embed=embed)

        # Get receiver data
        receiver_id = str(member.id)
        receiver_data = get_user_data(receiver_id)
        old_sp = receiver_data.get("sp", 0)  # Use .get() for safety
        new_sp = old_sp + amount

        # Update receiver's SP
        update_user_data(receiver_id, sp=new_sp)

        # Log transaction
        log_transaction(receiver_id, "owner_sp_grant", amount, old_sp, new_sp,
                        f"SP grant by Owner {ctx.author.display_name}")

        # Success embed
        embed = discord.Embed(
            title="👑 **DIVINE SP BLESSING GRANTED** 👑",
            description=(
                "```fix\n"
                "⚡ SUPREME OWNER AUTHORITY ACTIVATED ⚡\n"
                "```\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "*✨ The cosmic Owner channels raw spiritual energy ✨*"),
            color=0x9932CC)

        embed.add_field(
            name="👑 **SUPREME OWNER**",
            value=
            f"```ansi\n\u001b[0;35m{ctx.author.display_name}\u001b[0m\n```",
            inline=True)

        embed.add_field(
            name="🎯 **BLESSED RECIPIENT**",
            value=f"```ansi\n\u001b[0;36m{member.display_name}\u001b[0m\n```",
            inline=True)

        embed.add_field(name="⚡ **SPIRIT POINTS GRANTED**",
                        value=f"```yaml\n⚡ +{amount:,} Spirit Points\n```",
                        inline=False)

        embed.add_field(name="🔋 **NEW SP BALANCE**",
                        value=f"```fix\n⚡ {new_sp:,} SP\n```",
                        inline=False)

        embed.add_field(
            name="🌟 **DIVINE BLESSING**",
            value=("```css\n"
                   "[Raw spiritual energy flows from the cosmic throne]\n"
                   "```\n"
                   "💫 *The Owner's will shapes reality itself...*"),
            inline=False)

        embed.set_footer(
            text="👑 Supreme Owner Privilege • Spirit Point Grant System",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

        # Use modern datetime
        embed.timestamp = datetime.now(timezone.utc)

        # Send the embed
        result, error = await safe_send(ctx, embed=embed)
        if error:
            logger.error(f"❌ Failed to send message: {error}")

    except Exception as e:
        logger.error(f"❌ Error in sendsp command: {e}")
        error_embed = discord.Embed(
            title="❌ **SYSTEM ERROR** ❌",
            description="```diff\n- An unexpected error occurred\n```",
            color=0xFF0000)
        await ctx.send(embed=error_embed)


@bot.command()
@safe_command_wrapper
@commands.has_permissions(administrator=True)
async def restorebackup(ctx):
    """Restore database from cloud backup (GitHub or local)"""
    embed = discord.Embed(
        title="⚠️ **Restore Confirmation**",
        description=
        ("```diff\n+ COSMIC DATABASE RESTORATION RITUAL +\n```\n"
         "⚠️ *This will replace your current database with the latest backup.*\n"
         "🔥 ***ALL CURRENT DATA WILL BE LOST!***\n\n"
         "React with ✅ to confirm or ❌ to cancel."),
        color=0xFFB800)

    embed.add_field(
        name="🛡️ **Safety Measures**",
        value=
        "```yaml\nCurrent DB: Will be backed up first\nRestore Source: Latest backup (GitHub/Local)\nRollback: Possible via pre-restore backup\n```",
        inline=False)

    message = await ctx.send(embed=embed)
    await message.add_reaction("✅")
    await message.add_reaction("❌")

    def check(reaction, user):
        return (user == ctx.author and str(reaction.emoji) in ["✅", "❌"]
                and reaction.message.id == message.id)

    try:
        reaction, user = await bot.wait_for('reaction_add',
                                            timeout=30.0,
                                            check=check)

        if str(reaction.emoji) == "✅":
            embed = discord.Embed(
                title="🔄 **Restoring from Cloud...**",
                description=
                "```css\n[COSMIC DATA RESTORATION IN PROGRESS]\n```\n⚡ *Downloading and restoring backup from cloud storage...*",
                color=0xFFAA00)

            await message.edit(embed=embed)

            success = restore_from_cloud()

            if success:
                embed = discord.Embed(
                    title="✅ **Restore Complete**",
                    description=
                    ("```fix\n◆ COSMIC DATABASE RESTORATION SUCCESSFUL ◆\n```\n"
                     "💎 *Database has been restored from backup!*\n"
                     "⚡ ***Bot restart recommended.***"),
                    color=0x00FF00)

                embed.add_field(
                    name="🔄 **Next Steps**",
                    value=
                    "```yaml\n1. Bot restart recommended\n2. Verify data integrity\n3. Check all functions\n4. Old DB backed up as pre-restore\n```",
                    inline=False)
            else:
                embed = discord.Embed(
                    title="❌ **Restore Failed**",
                    description=
                    "```diff\n- RESTORATION RITUAL FAILED\n```\n💀 *Failed to restore from backup. Check logs for details.*",
                    color=0xFF0000)

        else:
            embed = discord.Embed(
                title="❌ **Restore Cancelled**",
                description=
                "```css\n[RESTORATION RITUAL CANCELLED]\n```\n🛡️ *Your current database remains unchanged.*",
                color=0x808080)

    except asyncio.TimeoutError:
        embed = discord.Embed(
            title="⏰ **Timeout**",
            description=
            "```css\n[RESTORATION RITUAL TIMEOUT]\n```\n🕐 *No response received. Current database remains unchanged.*",
            color=0x808080)

    await message.edit(embed=embed)


# Admin command to check API status
@bot.command()
@safe_command_wrapper
@commands.has_permissions(administrator=True)
async def apistatus(ctx):
    """Check API usage and rate limiting status (Admin only)"""
    try:
        now = time.time()

        # Discord API stats
        api_usage = len(discord_api_calls)
        api_percentage = (api_usage / DISCORD_API_LIMIT) * 100

        # Active cooldowns
        active_cooldowns = sum(
            len(commands) for commands in user_command_cooldowns.values())

        # Most used commands in last hour
        recent_commands = defaultdict(int)
        for user_commands in user_command_cooldowns.values():
            for command, last_used in user_commands.items():
                if now - last_used < 3600:  # Last hour
                    recent_commands[command] += 1

        embed = discord.Embed(
            title="📊 **API STATUS DASHBOARD** 📊",
            description=
            "```css\n[SYSTEM PERFORMANCE ANALYSIS]\n```\n🔧 *Real-time API health monitoring...*",
            color=0x00FF7F if api_percentage < 80 else
            0xFFB347 if api_percentage < 95 else 0xFF0000)

        embed.add_field(
            name="🌐 **Discord API Usage**",
            value=
            f"```yaml\nCalls: {api_usage}/{DISCORD_API_LIMIT}\nUsage: {api_percentage:.1f}%\nWindow: {API_WINDOW}s\nStatus: {'🟢 Healthy' if api_percentage < 80 else '🟡 Busy' if api_percentage < 95 else '🔴 Critical'}\n```",
            inline=False)

        embed.add_field(
            name="⏰ **Command Cooldowns**",
            value=
            f"```yaml\nActive Cooldowns: {active_cooldowns}\nUsers Affected: {len(user_command_cooldowns)}\n```",
            inline=True)

        if recent_commands:
            top_commands = sorted(recent_commands.items(),
                                  key=lambda x: x[1],
                                  reverse=True)[:5]
            command_list = "\n".join(
                [f"{cmd}: {count}" for cmd, count in top_commands])
            embed.add_field(name="📈 **Popular Commands (1h)**",
                            value=f"```\n{command_list}\n```",
                            inline=True)

        embed.set_footer(
            text="🔄 Updates every 5 minutes • API monitoring active",
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

        result, error = await safe_send(ctx, embed=embed)
        if error:
            await ctx.send(f"❌ Error displaying API status: {error}")

    except Exception as e:
        logger.error(f"❌ API status command error: {e}")
        await ctx.send("❌ Failed to retrieve API status.")


@bot.command()
@safe_command_wrapper
@commands.has_permissions(administrator=True)
async def backupstatus(ctx):
    """Check backup status and list available backups (GitHub + Local)"""
    embed = discord.Embed(
        title="📊 **COSMIC BACKUP STATUS** 📊",
        description=
        "```css\n[BACKUP VAULT ANALYSIS]\n```\n💾 *Examining the preservation of cosmic data...*",
        color=0x4169E1)

    try:
        # GitHub backup status
        github_backups = []
        if github_backup:
            success, github_files = github_backup.list_github_backups()
            if success and github_files:
                github_backups = github_files[:5]  # Show latest 5

                latest_github = github_files[0]
                embed.add_field(
                    name="☁️ **GitHub Cloud Storage**",
                    value=
                    f"```yaml\nStatus: ✅ Connected\nLatest: {latest_github['name']}\nTotal Files: {len(github_files)}\nRepository: {GITHUB_BACKUP_REPO}\n```",
                    inline=False)

                # List GitHub backups
                github_list = "\n".join(
                    [f"☁️ {backup['name']}" for backup in github_backups])
                embed.add_field(name="📋 **Recent GitHub Backups**",
                                value=f"```\n{github_list}\n```",
                                inline=True)
            else:
                embed.add_field(
                    name="☁️ **GitHub Cloud Storage**",
                    value="```diff\n- ❌ Connection failed or no backups\n```",
                    inline=False)
        else:
            embed.add_field(
                name="☁️ **GitHub Cloud Storage**",
                value=
                "```diff\n- ❌ Not configured\n+ Set GITHUB_TOKEN and GITHUB_BACKUP_REPO\n```",
                inline=False)

        # Local backup status
        if os.path.exists("backups"):
            backup_files = [
                f for f in os.listdir("backups") if f.endswith(".db")
            ]
            backup_files.sort(
                key=lambda x: os.path.getmtime(os.path.join("backups", x)),
                reverse=True)

            if backup_files:
                latest_backup = backup_files[0]
                latest_path = os.path.join("backups", latest_backup)
                latest_time = datetime.fromtimestamp(
                    os.path.getmtime(latest_path))
                latest_size = os.path.getsize(latest_path)
                total_size = sum(
                    os.path.getsize(os.path.join("backups", f))
                    for f in backup_files)

                embed.add_field(
                    name="💾 **Local Storage**",
                    value=
                    f"```yaml\nLatest: {latest_backup}\nCreated: {latest_time.strftime('%Y-%m-%d %H:%M:%S')}\nFiles: {len(backup_files)}\nTotal Size: {total_size:,} bytes\n```",
                    inline=True)

                # List local backups
                recent_local = backup_files[:5]
                local_list = "\n".join(
                    [f"💾 {backup}" for backup in recent_local])
                embed.add_field(name="📋 **Recent Local Backups**",
                                value=f"```\n{local_list}\n```",
                                inline=True)
            else:
                embed.add_field(name="💾 **Local Storage**",
                                value="```diff\n- No local backups found\n```",
                                inline=True)
        else:
            embed.add_field(name="💾 **Local Storage**",
                            value="```diff\n- Backup directory not found\n```",
                            inline=True)

        # Current database info
        if os.path.exists(DB_FILE):
            current_size = os.path.getsize(DB_FILE)
            current_time = datetime.fromtimestamp(os.path.getmtime(DB_FILE))

            embed.add_field(
                name="🗄️ **Current Database**",
                value=
                f"```yaml\nFile: {DB_FILE}\nModified: {current_time.strftime('%Y-%m-%d %H:%M:%S')}\nSize: {current_size:,} bytes\n```",
                inline=False)

    except Exception as e:
        embed.add_field(
            name="❌ **Error**",
            value=
            f"```diff\n- Failed to check backup status\n- Error: {str(e)[:100]}...\n```",
            inline=False)

    embed.set_footer(text="💫 Regular backups ensure cosmic data preservation",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('ssbal')
async def ssbal(ctx, member: discord.Member = None):
    user = member or ctx.author
    user_id = str(user.id)
    user_data = get_user_data(user_id)
    balance = user_data.get("balance", 0)

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
        description=
        f"```css\n[SPIRITUAL WEALTH ANALYSIS]\n```\n{tier} • *The crystallized power of ages...*",
        color=tier_color)

    embed.add_field(name="💎 **SPIRIT STONES**",
                    value=f"```fix\n{balance:,} SS\n```",
                    inline=True)

    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.set_footer(
        text=f"💫 Wealth transcends mortal understanding • ID: {user.id}")

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('spbal')
async def spbal(ctx, member: discord.Member = None):
    user = member or ctx.author
    user_id = str(user.id)
    user_data = get_user_data(user_id)
    sp = user_data.get("sp", 0)

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
        description=
        f"```diff\n+ SPIRITUAL POWER ANALYSIS +\n```\n{energy_tier} • *Raw energy flows through your essence...*",
        color=energy_color)

    embed.add_field(name="⚡ **SPIRIT POINTS**",
                    value=f"```css\n{sp:,} SP\n```",
                    inline=True)

    embed.add_field(
        name="🔋 **ENERGY FLOW**",
        value=
        f"```\n{'▰' * (min(sp, 10000) * 15 // 10000)}{'▱' * (15 - (min(sp, 10000) * 15 // 10000))}\n```",
        inline=False)

    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.set_footer(
        text=f"🌊 Energy is the source of all creation • {user.display_name}")

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('exchange')
async def exchange(ctx, amount: str):
    user_id = str(ctx.author.id)
    user_data = get_user_data(user_id)

    if amount.lower() == "all":
        exchange_amount = user_data.get("sp", 0)
    else:
        try:
            exchange_amount = int(amount)
        except:
            embed = discord.Embed(
                title="❌ **INVALID INPUT**",
                description=
                "```diff\n- ERROR: Invalid amount detected\n+ Use: !exchange <number> or 'all'\n```",
                color=0xFF0000)
            return await ctx.send(embed=embed)

    if exchange_amount <= 0 or exchange_amount > user_data.get("sp", 0):
        embed = discord.Embed(
            title="🚫 **INSUFFICIENT ENERGY**",
            description=
            "```css\n[TRANSACTION BLOCKED]\n```\n💔 *Your spiritual energy reserves are inadequate for this conversion...*",
            color=0xFF4500)
        return await ctx.send(embed=embed)

    # Update balances
    old_sp = user_data.get('sp', 0)
    old_balance = user_data.get('balance', 0)
    new_sp = old_sp - exchange_amount
    new_balance = old_balance + exchange_amount

    update_user_data(user_id, sp=new_sp, balance=new_balance)

    # Log transaction
    log_transaction(user_id, "exchange", exchange_amount, old_balance,
                    new_balance, f"Exchanged {exchange_amount} SP to SS")

    embed = discord.Embed(
        title="🔄 **ENERGY TRANSMUTATION COMPLETE** 🔄",
        description=
        f"```fix\n◆ SPIRITUAL ALCHEMY SUCCESSFUL ◆\n```\n✨ *Energy crystallizes into eternal stone...*",
        color=0x9932CC)

    embed.add_field(
        name="⚗️ **CONVERSION RESULT**",
        value=
        f"```diff\n- {exchange_amount:,} Spirit Points\n+ {exchange_amount:,} Spirit Stones\n```",
        inline=False)

    embed.add_field(
        name="📊 **UPDATED RESERVES**",
        value=
        f"```yaml\nSP Remaining: {new_sp:,}\nSS Balance: {new_balance:,}\n```",
        inline=False)

    embed.set_footer(
        text="⚡ → 💎 Perfect 1:1 conversion rate achieved",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('coinflip')
async def coinflip(ctx, guess: str, amount: str):
    now = datetime.now(timezone.utc)
    user_id = str(ctx.author.id)
    guess = guess.lower()

    if guess not in ["heads", "tails"]:
        embed = discord.Embed(
            title="⚠️ **INVALID PREDICTION**",
            description=
            "```diff\n- COSMIC ERROR DETECTED\n+ Valid options: 'heads' or 'tails'\n```\n🎯 *Choose your fate wisely, mortal...*",
            color=0xFF6347)
        return await ctx.send(embed=embed)

    user_data = get_user_data(user_id)
    sp = user_data.get("sp", 0)

    if user_id in last_gamble_times and (
            now - last_gamble_times[user_id]).total_seconds() < 60:
        embed = discord.Embed(
            title="⏳ **COSMIC COOLDOWN**",
            description=
            "```css\n[FATE ENERGY RECHARGING]\n```\n🌌 *The universe needs time to align the cosmic forces...*",
            color=0x4682B4)
        embed.set_footer(
            text="⚡ Gambling cooldown: 60 seconds between attempts")
        result, error = await safe_send(ctx, embed=embed)
        return

    validated_amount = validate_amount(amount, 20000)
    if validated_amount is None:
        embed = discord.Embed(
            title="💸 **INVALID WAGER**",
            description=
            "```diff\n- BETTING ERROR\n+ Enter a valid number or 'all'\n```",
            color=0xFF0000)
        result, error = await safe_send(ctx, embed=embed)
        return

    if validated_amount == "all":
        bet = min(sp, 20000)
    else:
        bet = validated_amount

    if bet <= 0 or bet > 20000 or bet > sp:
        embed = discord.Embed(
            title="🚫 **WAGER REJECTED**",
            description=
            "```css\n[INSUFFICIENT FUNDS OR LIMIT EXCEEDED]\n```\n💰 *Maximum bet: 20,000 SP*\n⚡ *Current SP: {:,}*"
            .format(sp),
            color=0xFF4500)
        result, error = await safe_send(ctx, embed=embed)
        return

    flip = random.choice(["heads", "tails"])
    won = (flip == guess)

    if won:
        new_sp = sp + bet
        update_user_data(user_id, sp=new_sp)
        update_monthly_stats(user_id, win_amount=bet)
        log_transaction(user_id, "gambling_win", bet, sp, new_sp,
                        f"Coinflip win: {flip}")

        embed = discord.Embed(
            title="🎉 **FATE SMILES UPON YOU** 🎉",
            description=
            f"```diff\n+ COSMIC VICTORY ACHIEVED +\n```\n🪙 *The coin reveals: **{flip.upper()}***\n✨ *Fortune flows through your spirit...*",
            color=0x00FF00)

        embed.add_field(name="🏆 **VICTORY SPOILS**",
                        value=f"```css\n+{bet:,} Spirit Points\n```",
                        inline=True)

        embed.add_field(name="💰 **NEW BALANCE**",
                        value=f"```fix\n{new_sp:,} SP\n```",
                        inline=True)
    else:
        new_sp = sp - bet
        update_user_data(user_id, sp=new_sp)
        update_monthly_stats(user_id, loss_amount=bet)
        log_transaction(user_id, "gambling_loss", -bet, sp, new_sp,
                        f"Coinflip loss: {flip}")

        embed = discord.Embed(
            title="💀 **THE VOID CLAIMS ITS DUE** 💀",
            description=
            f"```diff\n- COSMIC DEFEAT ENDURED -\n```\n🪙 *The coin reveals: **{flip.upper()}***\n🌑 *Your greed feeds the endless darkness...*",
            color=0xFF0000)

        embed.add_field(name="💸 **LOSSES SUFFERED**",
                        value=f"```css\n-{bet:,} Spirit Points\n```",
                        inline=True)

        embed.add_field(name="💔 **REMAINING BALANCE**",
                        value=f"```fix\n{new_sp:,} SP\n```",
                        inline=True)

    last_gamble_times[user_id] = now

    embed.add_field(
        name="🎯 **PREDICTION vs REALITY**",
        value=
        f"```yaml\nYour Guess: {guess.title()}\nActual Result: {flip.title()}\nOutcome: {'WIN' if won else 'LOSS'}\n```",
        inline=False)

    embed.set_footer(
        text="🎰 The cosmic coin never lies • Gamble responsibly",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('shop')
async def shop(ctx):
    embed = discord.Embed(
        title="🏪 **GU CHANG'S MYSTICAL EMPORIUM** 🏪",
        description=
        "```css\n[LEGENDARY ARTIFACTS AWAIT]\n```\n✨ *Only the worthy may claim these treasures of power...*",
        color=0xFF6B35)

    for item, details in SHOP_ITEMS.items():
        item_name = item.replace('_', ' ').title()

        embed.add_field(
            name=f"{details['desc'].split()[0]} **{item_name.upper()}**",
            value=
            f"```yaml\nPrice: {details['price']:,} Spirit Stones\n```\n*{details['desc'][2:]}*",
            inline=True)

    embed.add_field(
        name="💳 **PURCHASE INSTRUCTIONS**",
        value=
        "```fix\n!buy <item_name>\n```\n🛒 *Use the command above to claim your artifact*",
        inline=False)

    embed.set_footer(text="⚡ Spiritual artifacts enhance your cosmic journey",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('buy')
async def buy(ctx, item: str):
    user_id = str(ctx.author.id)
    user_data = get_user_data(user_id)

    item = item.lower()
    if item not in SHOP_ITEMS:
        available_items = ", ".join(
            [i.replace('_', ' ').title() for i in SHOP_ITEMS.keys()])
        embed = discord.Embed(
            title="❌ **ARTIFACT NOT FOUND**",
            description=
            f"```diff\n- UNKNOWN ITEM REQUESTED\n```\n🔍 **Available Items:** {available_items}",
            color=0xFF0000)
        return await ctx.send(embed=embed)

    item_data = SHOP_ITEMS[item]
    balance = user_data["balance"]

    if balance < item_data["price"]:
        shortage = item_data["price"] - balance
        embed = discord.Embed(
            title="💸 **INSUFFICIENT SPIRIT STONES**",
            description=
            f"```diff\n- TREASURY INADEQUATE\n```\n💰 **Required:** {item_data['price']:,} SS\n💎 **You Have:** {balance:,} SS\n📉 **Shortage:** {shortage:,} SS\n\n⚡ *Gather more power before returning...*",
            color=0xFF6347)
        return await ctx.send(embed=embed)

    if item == "nickname_lock":
        add_nickname_lock(user_id)
        effect = "🔒 **IDENTITY SEALED** - *Your name is now protected from all changes*"
        effect_color = 0x4169E1
    elif item == "temp_admin":
        expiry = datetime.now(timezone.utc) + datetime.timedelta(hours=1)
        add_temp_admin(user_id, expiry.isoformat(), str(ctx.guild.id))
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

    elif item == "name_change_card":
        # This gives them the card, they use it with a separate command
        effect = "🃏 **NAME CHANGE CARD ACQUIRED** - *Use !usename @target 'new nickname' to activate*"
        effect_color = 0xFF1493
    else:
        effect = "✨ **ARTIFACT BONDED** - *The power is now yours to wield*"
        effect_color = 0x9932CC

    # Update balance
    new_balance = balance - item_data["price"]
    update_user_data(user_id, balance=new_balance)

    # Log transaction
    log_transaction(user_id, "shop_purchase", -item_data["price"], balance,
                    new_balance, f"Purchased {item}")

    embed = discord.Embed(
        title="✅ **TRANSACTION COMPLETED** ✅",
        description=
        f"```fix\n◆ ARTIFACT ACQUISITION SUCCESSFUL ◆\n```\n{effect}",
        color=effect_color)

    embed.add_field(name="🎁 **ARTIFACT CLAIMED**",
                    value=f"```css\n{item.replace('_', ' ').title()}\n```",
                    inline=True)

    embed.add_field(
        name="💰 **COST PAID**",
        value=f"```diff\n- {item_data['price']:,} Spirit Stones\n```",
        inline=True)

    embed.add_field(name="💎 **REMAINING BALANCE**",
                    value=f"```yaml\n{new_balance:,} SS\n```",
                    inline=True)

    embed.set_thumbnail(
        url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.set_footer(text="🌟 Power has been transferred • Use it wisely",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
async def givess(ctx, member: discord.Member, amount: int):
    # Check if user has administrator permissions
    if not ctx.author.guild_permissions.administrator:
        embed = discord.Embed(
            title="🚫 **ACCESS DENIED** 🚫",
            description=
            "```diff\n- INSUFFICIENT AUTHORITY\n- Administrator permissions required\n```\n⚡ *Only those with divine authority may grant such power...*",
            color=0xFF0000)
        return await ctx.send(embed=embed)

    if amount <= 0:
        embed = discord.Embed(
            title="❌ **INVALID AMOUNT** ❌",
            description=
            "```diff\n- ERROR: Amount must be positive\n+ Enter a valid positive number\n```",
            color=0xFF4500)
        return await ctx.send(embed=embed)

    receiver_id = str(member.id)
    receiver_data = get_user_data(receiver_id)
    old_balance = receiver_data["balance"]
    new_balance = old_balance + amount

    update_user_data(receiver_id, balance=new_balance)

    # Log transaction
    log_transaction(receiver_id, "admin_grant", amount, old_balance,
                    new_balance, f"Admin grant by {ctx.author.display_name}")

    embed = discord.Embed(
        title="✨ **DIVINE BLESSING GRANTED** ✨",
        description=
        "```fix\n⚡ ADMINISTRATOR AUTHORITY ACTIVATED ⚡\n```\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*🌟 The cosmic treasury flows with divine will 🌟*",
        color=0x00FF7F)

    embed.add_field(
        name="👑 **ADMINISTRATOR**",
        value=f"```ansi\n\u001b[0;35m{ctx.author.display_name}\u001b[0m\n```",
        inline=True)

    embed.add_field(
        name="🎯 **RECIPIENT**",
        value=f"```ansi\n\u001b[0;36m{member.display_name}\u001b[0m\n```",
        inline=True)

    embed.add_field(name="💰 **AMOUNT GRANTED**",
                    value=f"```yaml\n💎 +{amount:,} Spirit Stones\n```",
                    inline=False)

    embed.add_field(name="💎 **NEW BALANCE**",
                    value=f"```fix\n💎 {new_balance:,} SS\n```",
                    inline=False)

    embed.set_footer(
        text="⚡ Divine Administrative System ⚡",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.timestamp = datetime.now(timezone.utc)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
async def takess(ctx, member: discord.Member, amount: int):
    # Check if user has administrator permissions
    if not ctx.author.guild_permissions.administrator:
        embed = discord.Embed(
            title="🚫 **ACCESS DENIED** 🚫",
            description=
            "```diff\n- INSUFFICIENT AUTHORITY\n- Administrator permissions required\n```\n⚡ *Only those with divine authority may wield such power...*",
            color=0xFF0000)
        return await ctx.send(embed=embed)

    if amount <= 0:
        embed = discord.Embed(
            title="❌ **INVALID AMOUNT** ❌",
            description=
            "```diff\n- ERROR: Amount must be positive\n+ Enter a valid positive number\n```",
            color=0xFF4500)
        return await ctx.send(embed=embed)

    target_id = str(member.id)
    target_data = get_user_data(target_id)
    current_balance = target_data["balance"]

    if amount > current_balance:
        embed = discord.Embed(
            title="⚠️ **INSUFFICIENT FUNDS** ⚠️",
            description=
            f"```diff\n- Cannot remove more than available\n```\n💰 **Target Balance:** {current_balance:,} SS\n📉 **Requested Removal:** {amount:,} SS\n\n⚡ *The void cannot claim what does not exist...*",
            color=0xFF6347)
        return await ctx.send(embed=embed)

    new_balance = current_balance - amount
    update_user_data(target_id, balance=new_balance)

    # Log transaction
    log_transaction(target_id, "admin_remove", -amount, current_balance,
                    new_balance, f"Admin removal by {ctx.author.display_name}")

    embed = discord.Embed(
        title="💀 **DIVINE JUDGMENT EXECUTED** 💀",
        description=
        "```fix\n⚡ ADMINISTRATOR AUTHORITY ACTIVATED ⚡\n```\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*🌑 The cosmic balance demands sacrifice 🌑*",
        color=0xFF1744)

    embed.add_field(
        name="👑 **ADMINISTRATOR**",
        value=f"```ansi\n\u001b[0;31m{ctx.author.display_name}\u001b[0m\n```",
        inline=True)

    embed.add_field(
        name="🎯 **TARGET**",
        value=f"```ansi\n\u001b[0;33m{member.display_name}\u001b[0m\n```",
        inline=True)

    embed.add_field(name="💸 **AMOUNT REMOVED**",
                    value=f"```yaml\n💎 -{amount:,} Spirit Stones\n```",
                    inline=False)

    embed.add_field(name="💔 **REMAINING BALANCE**",
                    value=f"```fix\n💎 {new_balance:,} SS\n```",
                    inline=False)

    embed.set_footer(
        text="⚡ Divine Administrative System ⚡",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.timestamp = datetime.now(timezone.utc)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('top')
async def top(ctx):
    leaderboard = get_leaderboard('balance', 10)

    embed = discord.Embed(
        title="🏆 **SPIRIT STONES LEADERBOARD** 🏆",
        description=
        "```css\n[HALL OF SPIRITUAL LEGENDS]\n```\n💎 *The most powerful cultivators in the realm...*",
        color=0xFFD700)

    medal_emojis = [
        "🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"
    ]

    leaderboard_text = ""
    for i, (user_id, balance) in enumerate(leaderboard):
        try:
            user = bot.get_user(int(user_id))
            username = user.display_name if user else f"Unknown User"

            leaderboard_text += f"{medal_emojis[i]} **{username}** - `{balance:,}` SS\n"
        except:
            continue

    if leaderboard_text:
        embed.add_field(name="💰 **TOP CULTIVATORS**",
                        value=leaderboard_text,
                        inline=False)
    else:
        embed.add_field(
            name="💰 **TOP CULTIVATORS**",
            value=
            "```diff\n- No data available yet\n+ Start cultivating to appear here!\n```",
            inline=False)

    embed.set_footer(text="⚡ Power rankings updated in real-time",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('lucky')
async def lucky(ctx):
    """Shows total SP of top 10 players instead of individual gambling stats"""
    sp_leaderboard = get_leaderboard('sp', 10)

    total_sp = sum(sp for _, sp in sp_leaderboard)

    embed = discord.Embed(
        title="🍀 **COSMIC FORTUNE READING** 🍀",
        description=
        "```css\n[SPIRITUAL ENERGY ANALYSIS]\n```\n⚡ *The combined power of the top cultivators flows through the realm...*",
        color=0x00FF7F)

    # Show top 10 SP holders
    medal_emojis = [
        "🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"
    ]

    leaderboard_text = ""
    for i, (user_id, sp) in enumerate(sp_leaderboard):
        try:
            user = bot.get_user(int(user_id))
            username = user.display_name if user else f"Unknown User"

            leaderboard_text += f"{medal_emojis[i]} **{username}** - `{sp:,}` SP\n"
        except:
            continue

    if leaderboard_text:
        embed.add_field(name="⚡ **TOP ENERGY MASTERS**",
                        value=leaderboard_text,
                        inline=False)

    embed.add_field(
        name="🌟 **COSMIC ENERGY POOL**",
        value=
        f"```fix\n💫 {total_sp:,} Total Spirit Points\n```\n*The accumulated power of the realm's elite...*",
        inline=False)

    embed.add_field(
        name="🔮 **FORTUNE INSIGHT**",
        value=
        f"```yaml\nAverage Power: {total_sp // 10 if sp_leaderboard else 0:,} SP\nRealm Status: {'Flourishing' if total_sp > 100000 else 'Growing' if total_sp > 50000 else 'Developing'}\n```",
        inline=False)

    embed.set_footer(
        text="🍀 The universe reveals its secrets to those who seek",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('unlucky')
async def unlucky(ctx):
    """Shows top 10 users who lost the most SP this month"""
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    top_losers = get_top_losers(current_month, 10)

    if not top_losers:
        embed = discord.Embed(
            title="🌟 **BLESSED MONTH** 🌟",
            description=
            "```css\n[NO SIGNIFICANT LOSSES DETECTED]\n```\n✨ *The void has been merciful this month...*",
            color=0x32CD32)

        embed.add_field(
            name="🛡️ **COSMIC PROTECTION**",
            value=
            "```diff\n+ No major gambling losses recorded\n+ The realm enjoys fortune's favor\n```",
            inline=False)

        embed.set_footer(
            text=
            f"📅 {datetime.now(timezone.utc).strftime('%B %Y')} • Keep the luck flowing!",
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

        return await ctx.send(embed=embed)

    total_losses = sum(losses for _, losses in top_losers)

    embed = discord.Embed(
        title="💀 **HALL OF COSMIC MISFORTUNE** 💀",
        description=
        "```css\n[THE VOID'S GREATEST VICTIMS]\n```\n🌑 *Those who have fed the darkness most this month...*",
        color=0x8B0000)

    # Show top 10 losers
    skull_emojis = ["💀", "☠️", "👹", "😈", "🔥", "⚡", "💸", "😭", "😱", "🆘"]

    leaderboard_text = ""
    for i, (user_id, losses) in enumerate(top_losers):
        try:
            user = bot.get_user(int(user_id))
            username = user.display_name if user else f"Unknown User"

            leaderboard_text += f"{skull_emojis[i]} **{username}** - `{losses:,}` SP Lost\n"
        except:
            continue

    if leaderboard_text:
        embed.add_field(name="💸 **VOID'S FAVORED VICTIMS**",
                        value=leaderboard_text,
                        inline=False)

    embed.add_field(
        name="🌑 **TOTAL DEVASTATION**",
        value=
        f"```fix\n💀 {total_losses:,} Spirit Points Consumed\n```\n*The accumulated suffering feeds the endless void...*",
        inline=False)

    # Calculate average loss and provide insight
    average_loss = total_losses // len(top_losers) if top_losers else 0

    if total_losses >= 500000:
        void_status = "🌑 **VOID ASCENDANT** - *The darkness grows stronger with each sacrifice*"
        status_color = "Catastrophic"
    elif total_losses >= 250000:
        void_status = "💀 **VOID DOMINANT** - *Misfortune reigns supreme this month*"
        status_color = "Critical"
    elif total_losses >= 100000:
        void_status = "😈 **VOID ACTIVE** - *The gambling demons feast well*"
        status_color = "High"
    else:
        void_status = "🔥 **VOID STIRRING** - *Minor tributes to the darkness*"
        status_color = "Moderate"

    embed.add_field(
        name="👹 **VOID ANALYSIS**",
        value=
        f"```yaml\nAverage Loss: {average_loss:,} SP\nVoid Status: {status_color}\nMonth: {datetime.now(timezone.utc).strftime('%B %Y')}\n```\n{void_status}",
        inline=False)

    embed.add_field(
        name="⚠️ **COSMIC WARNING**",
        value=
        "```diff\n- The void remembers every sacrifice\n- Fortune is fickle, wisdom is eternal\n+ Practice restraint in your cosmic journey\n```",
        inline=False)

    embed.set_footer(
        text=
        f"💀 Monthly Misfortune Report • {datetime.now(timezone.utc).strftime('%B %Y')}",
        icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@commands.has_permissions(administrator=True)
async def errortest(ctx, error_type: str = "generic"):
    """Test error handling (Admin only)"""
    if error_type == "generic":
        raise Exception("Test error for debugging")
    elif error_type == "permission":
        raise commands.MissingPermissions(["administrator"])
    elif error_type == "argument":
        raise commands.MissingRequiredArgument(
            commands.Parameter("test",
                               commands.Parameter.POSITIONAL_OR_KEYWORD))
    else:
        await ctx.send("Available error types: generic, permission, argument")


@bot.command()
@safe_command_wrapper
@cooldown_check('lose')
async def lose(ctx, member: discord.Member = None):
    """Shows SP lost this month for the user or tagged member"""
    user = member or ctx.author
    user_id = str(user.id)

    # Get current month stats
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    monthly_stats = get_monthly_stats(user_id, current_month)

    losses = monthly_stats.get("losses", 0)
    wins = monthly_stats.get("wins", 0)
    net_result = wins - losses

    # Determine loss tier and color
    if losses >= 100000:
        loss_tier = "💀 **VOID TOUCHED**"
        tier_color = 0x8B0000
    elif losses >= 50000:
        loss_tier = "🔥 **RECKLESS**"
        tier_color = 0xFF4500
    elif losses >= 25000:
        loss_tier = "⚠️ **DANGEROUS**"
        tier_color = 0xFF6347
    elif losses >= 10000:
        loss_tier = "😬 **RISKY**"
        tier_color = 0xFFB347
    elif losses > 0:
        loss_tier = "🎯 **CAUTIOUS**"
        tier_color = 0xFFD700
    else:
        loss_tier = "🛡️ **UNTOUCHED**"
        tier_color = 0x32CD32

    embed = discord.Embed(
        title=f"💸 **{user.display_name.upper()}'S VOID TRIBUTE** 💸",
        description=
        f"```css\n[MONTHLY GAMBLING ANALYSIS]\n```\n{loss_tier} • *The darkness remembers every sacrifice...*",
        color=tier_color)

    embed.add_field(name="💀 **LOSSES TO THE VOID**",
                    value=f"```diff\n- {losses:,} Spirit Points\n```",
                    inline=True)

    embed.add_field(name="🏆 **GAINS FROM FORTUNE**",
                    value=f"```diff\n+ {wins:,} Spirit Points\n```",
                    inline=True)

    embed.add_field(
        name="⚖️ **NET RESULT**",
        value=
        f"```{'diff' if net_result >= 0 else 'css'}\n{'+ ' if net_result >= 0 else ''}{net_result:,} Spirit Points\n```",
        inline=False)

    # Loss ratio calculation
    total_gambled = wins + losses
    if total_gambled > 0:
        loss_percentage = (losses / total_gambled) * 100
        embed.add_field(
            name="📊 **GAMBLING STATISTICS**",
            value=
            f"```yaml\nTotal Gambled: {total_gambled:,} SP\nLoss Rate: {loss_percentage:.1f}%\nRisk Level: {'HIGH' if loss_percentage > 60 else 'MODERATE' if loss_percentage > 40 else 'LOW'}\n```",
            inline=False)

    # Motivational message based on performance
    if net_result > 0:
        message = "🌟 *Fortune favors your bold spirit!*"
    elif net_result == 0:
        message = "⚖️ *Perfect balance - the universe is neutral.*"
    else:
        message = "🌑 *The void grows stronger with your offerings...*"

    embed.add_field(name="🔮 **COSMIC INSIGHT**", value=message, inline=False)

    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.set_footer(
        text=
        f"📅 {datetime.now(timezone.utc).strftime('%B %Y')} • Gamble responsibly",
        icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


@bot.command()
@safe_command_wrapper
@cooldown_check('help')
async def help(ctx):
    """Display all available commands"""
    embed = discord.Embed(
        title="📚 **GU CHANG'S CULTIVATION MANUAL** 📚",
        description=
        "```css\n[COMPLETE COMMAND REFERENCE]\n```\n⚡ *Master these commands to ascend in power...*",
        color=0x9932CC)

    # Economy Commands
    embed.add_field(name="💰 **ECONOMY COMMANDS**",
                    value="""```
!daily - Claim daily SP (300-400 based on roles)
!ssbal [@user] - Check Spirit Stones balance
!spbal [@user] - Check Spirit Points balance
!exchange <amount|all> - Convert SP to SS (1:1)
!top - View SS leaderboard (top 10)
```""",
                    inline=False)

    # Gambling Commands
    embed.add_field(name="🎰 **GAMBLING COMMANDS**",
                    value="""```
!coinflip <heads|tails> <amount|all> - Bet SP on coinflip
!lucky - View top 10 SP holders and total energy
!unlucky - View top 10 biggest losers this month
!lose [@user] - Check monthly gambling losses
```""",
                    inline=False)

    # Shop Commands
    embed.add_field(name="🛒 **SHOP COMMANDS**",
                    value="""```
    !shop - View available items
    !buy <item> - Purchase shop items
      • nickname_lock (5,000 SS)
      • temp_admin (25,000 SS) 
      • hmw_role (50,000 SS)
      • name_change_card (10,000 SS)
    !usename @user "new nickname" - Use name change card
    ```""",
                    inline=False)

    # Admin Commands
    embed.add_field(name="👑 **ADMIN COMMANDS**",
                    value="""```
!givess <@user> <amount> - Grant Spirit Stones
!takess <@user> <amount> - Remove Spirit Stones
!sendsp <@user> <amount> - Grant Spirit Points (Owner Only)
```""",
                    inline=False)

    embed.add_field(name="⚡ **DAILY REWARDS**",
                    value="""```yaml
Standard: 300 SP
Booster: 350 SP (+50)
HMW: 350 SP (+50)
Admin: 400 SP (+100)
Streak Bonus: 2x reward at 5-day streak
```""",
                    inline=False)

    embed.set_footer(text="🌟 Master the commands, master your destiny • !help",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)

    result, error = await safe_send(ctx, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")


# ==== Flask Thread ====
def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)


# ==== Main Execution ====
if __name__ == "__main__":
    # Start Flask in a separate thread
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("🌐 Flask server started")

    # Run the main bot with proper error handling
    max_retries = 3
    for attempt in range(max_retries):
        try:
            asyncio.run(main())
            break
        except Exception as e:
            logger.error(f"❌ Bot startup attempt {attempt + 1} failed: {e}")
            if attempt == max_retries - 1:
                logger.error("❌ Max retries exceeded, shutting down")
                raise
            time.sleep(5)  # Wait before retry
            logger.info(f"🔄 Retrying bot startup in 5 seconds...")
