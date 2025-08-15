import os
import sqlite3
import random
import asyncio
import datetime
import time
from enum import Enum
from flask import Flask
import discord
from discord.ext import commands, tasks
from threading import Thread
from dotenv import load_dotenv
import base64
from typing import Optional
import contextlib
import threading
from queue import Queue
from concurrent.futures import ThreadPoolExecutor
import signal
import sys
import aiohttp
import requests
import shutil
from datetime import timezone, timedelta
import logging
from collections import defaultdict
from collections import deque
import functools

DB_LOCK = threading.Lock()
DB_POOL = ThreadPoolExecutor(max_workers=3)


class DatabasePool:

    def __init__(self, db_file, max_connections=5):
        self.db_file = db_file
        self.max_connections = max_connections
        self._pool = Queue(maxsize=max_connections)
        self._lock = threading.Lock()
        self._initialized = False

    def _initialize_pool(self):
        if not self._initialized:
            with self._lock:
                if not self._initialized:
                    for _ in range(self.max_connections):
                        conn = sqlite3.connect(self.db_file,
                                               timeout=30,
                                               check_same_thread=False)
                        conn.execute("PRAGMA journal_mode=WAL")
                        conn.execute("PRAGMA foreign_keys=ON")
                        self._pool.put(conn)
                    self._initialized = True

    @contextlib.contextmanager
    def get_connection(self):
        self._initialize_pool()
        conn = self._pool.get(timeout=10)
        try:
            yield conn
        except sqlite3.Error as e:
            logger.error(f"❌ Database error: {e}")
            conn.rollback()
            raise
        finally:
            self._pool.put(conn)


# Define constants first
DB_FILE = "bot_database.db"

# Initialize database pool
db_pool = DatabasePool(DB_FILE)


@contextlib.contextmanager
def get_db_connection():
    with db_pool.get_connection() as conn:
        yield conn


# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Initialize placeholder variables (will be properly set later)
bot: Optional[commands.Bot] = None
github_backup = None


def signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    signal_name = "SIGTERM" if signum == 15 else "SIGINT" if signum == 2 else f"Signal {signum}"
    logger.info(f"🛑 Shutdown signal received: {signal_name}")

    # For hosting platforms, we want to shut down gracefully
    if bot and not bot.is_closed():
        logger.info("🔄 Initiating graceful bot shutdown...")
        try:
            # Try to create a final backup
            backup_file = create_backup_with_cloud_storage()
            if backup_file and github_backup:
                success, result = github_backup.upload_backup_to_github(
                    backup_file)
                if success:
                    logger.info("✅ Emergency backup created before shutdown")
        except Exception as e:
            logger.error(f"❌ Emergency backup failed: {e}")

        # Close the bot
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(bot.close())
        except Exception as e:
            logger.exception(f"An unexpected error occurred: {e}")
            pass

    logger.info("🛑 Bot shutdown complete")
    sys.exit(0)


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class AsyncCircuitBreaker:

    def __init__(self, failure_threshold=5, timeout=60):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = CircuitState.CLOSED
        self._lock = asyncio.Lock()

    async def call(self, func, *args, **kwargs):
        async with self._lock:
            now = time.time()

            if self.state == CircuitState.OPEN:
                if now - self.last_failure_time > self.timeout:
                    self.state = CircuitState.HALF_OPEN
                    logger.info("🔄 Circuit breaker transitioning to HALF_OPEN")
                else:
                    raise Exception(
                        "Circuit breaker is OPEN - rejecting request")

        try:
            # Call the async function properly
            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)

            await self._on_success()
            return result
        except Exception as e:
            logger.exception(f"An unexpected error occurred: {e}")
            await self._on_failure()
            raise

    async def _on_success(self):
        async with self._lock:
            self.failure_count = 0
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.CLOSED
                logger.info("✅ Circuit breaker CLOSED - service recovered")

    async def _on_failure(self):
        async with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = CircuitState.OPEN
                logger.error("🔴 Circuit breaker OPEN - service failing")

class TimedCache:

    def __init__(self, ttl_seconds=300):  # 5 minute TTL
        self.cache = {}
        self.ttl = ttl_seconds

    def get(self, key):
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl:
                return value
            else:
                del self.cache[key]
        return None

    def set(self, key, value):
        self.cache[key] = (value, time.time())

    def clear(self):
        self.cache.clear()


# Create cache instances
user_cache = TimedCache(ttl_seconds=60)  # Cache user data for 1 minute
leaderboard_cache = TimedCache(ttl_seconds=300)
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
API_WINDOW = 60  # seconds
DISCORD_API_LIMIT = 45  # requests per window
discord_api_calls = []


class AdvancedRateLimit:

    def __init__(self, max_requests=45, window=60):  # More conservative
        self.max_requests = max_requests
        self.window = window
        self.requests = deque()
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            now = time.time()

            # Remove old requests
            while self.requests and self.requests[0] <= now - self.window:
                self.requests.popleft()

            # Check if we can make a request
            if len(self.requests) >= self.max_requests:
                sleep_time = self.window - (now - self.requests[0]) + 1
                logger.warning(
                    f"⚠️ Rate limit hit, sleeping {sleep_time:.1f}s")
                await asyncio.sleep(sleep_time)
                return await self.acquire()  # Recursive retry

            self.requests.append(now)
            return True


discord_rate_limiter = AdvancedRateLimit(max_requests=45, window=60)


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

api_circuit_breaker = AsyncCircuitBreaker(failure_threshold=3, timeout=120)  # Critical functions
light_circuit_breaker = AsyncCircuitBreaker(failure_threshold=5, timeout=30)  # Non-critical functions

# Core API call logic (shared by both breakers)
async def safe_api_call_internal(func, *args, **kwargs):
    """Internal API call logic used by both circuit breakers."""
    max_retries = 3
    base_delay = 1

    for attempt in range(max_retries):
        try:
            await discord_rate_limiter.acquire()

            if asyncio.iscoroutinefunction(func):
                result = await func(*args, **kwargs)
            else:
                result = func(*args, **kwargs)
            return result, None

        except discord.HTTPException as e:
            if e.status == 429:  # Rate limited
                retry_after = getattr(e, 'retry_after', base_delay * (2**attempt))
                logger.warning(f"🔄 Rate limited, waiting {retry_after}s")
                await asyncio.sleep(min(retry_after, 60))
                continue
            elif e.status >= 500:  # Server errors
                delay = min(base_delay * (2**attempt), 30)
                logger.warning(f"🔄 Server error {e.status}, retrying in {delay}s")
                await asyncio.sleep(delay)
                continue
            elif e.status == 403:  # Forbidden - don't retry
                logger.warning(f"❌ Permission denied: {e}")
                return None, f"Permission error: {e}"
            else:  # Other HTTP errors
                logger.error(f"❌ Discord API Error {e.status}: {e}")
                return None, f"Discord API Error: {e.status}"

        except asyncio.TimeoutError:
            delay = min(base_delay * (2**attempt), 15)
            logger.warning(f"⏰ Timeout, retrying in {delay}s")
            await asyncio.sleep(delay)
            continue

        except Exception as e:
            logger.error(f"❌ Unexpected error: {e}")
            return None, str(e)

    logger.error("❌ Max retries exceeded")
    return None, "Max retries exceeded"

# Enhanced API call for CRITICAL functions
async def enhanced_safe_api_call(func, *args, **kwargs):
    """For critical Discord API calls that should trip main circuit breaker."""
    try:
        return await api_circuit_breaker.call(safe_api_call_internal, func, *args, **kwargs)
    except Exception as e:
        if "Circuit breaker is OPEN" in str(e):
            logger.error("❌ API call error: Circuit breaker is OPEN - rejecting request")
            return None, "Service temporarily unavailable"
        logger.error(f"❌ API call failed: {e}")
        return None, str(e)

# Light API call for NON-CRITICAL functions (NEW)
async def light_safe_api_call(func, *args, **kwargs):
    """For non-critical Discord API calls (cooldowns, errors, notifications)."""
    try:
        return await light_circuit_breaker.call(safe_api_call_internal, func, *args, **kwargs)
    except Exception as e:
        if "Circuit breaker is OPEN" in str(e):
            logger.warning("⚠️ Light circuit breaker open - skipping non-critical message")
            return None, "Light breaker open"
        logger.warning(f"⚠️ Light API call failed: {e}")
        return None, str(e)

# Updated safe_api_call wrapper
async def safe_api_call(func, *args, **kwargs):
    """Main API call function - uses critical circuit breaker."""
    now = time.time()
    # Clean old records to prevent memory leak (keep last 5 minutes)
    discord_api_calls[:] = [t for t in discord_api_calls if now - t < 300]
    # Track current API call
    discord_api_calls.append(now)
    result, error = await enhanced_safe_api_call(func, *args, **kwargs)
    return result, error



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

                result, error = await light_safe_api_call(ctx.send, embed=embed)
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

                result, error = await light_safe_api_call(ctx.send, embed=embed)
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
            except discord.DiscordException as e:
                logger.error(
                    f"❌ Could not send error message to user {ctx.author.id}: {e}"
                )
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


async def check_database_integrity():
    """Check if database is corrupted and attempt repair"""
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("PRAGMA integrity_check;").fetchone()
        conn.close()
        return True
    except sqlite3.DatabaseError as e:
        logger.error(f"❌ Database corruption detected: {e}")
        return False


async def init_database_async():
    """Initialize database with corruption check (async version)"""
    try:
        # Check if database exists and is intact
        if os.path.exists(DB_FILE):
            if not await check_database_integrity():
                logger.warning(
                    "🔄 Database corruption detected during initialization")

                # Try to restore from backup first
                if not await restore_from_latest_backup():
                    # If no backup, create fresh database
                    logger.warning("🆕 Creating fresh database")
                    os.remove(DB_FILE)

        # Proceed with normal initialization
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # Create tables with proper error handling
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                sp INTEGER DEFAULT 100,
                last_claim TEXT DEFAULT '',
                streak INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Add other tables...

        conn.commit()
        conn.close()

        logger.info("✅ Database initialized successfully")
        return True

    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")
        return False


async def repair_database():
    """Attempt to repair corrupted database"""
    try:
        # Backup corrupted database
        timestamp = datetime.datetime.now(
            timezone.utc).strftime("%Y%m%d_%H%M%S")
        corrupted_backup = f"{DB_FILE}.corrupted_{timestamp}"
        shutil.copy2(DB_FILE, corrupted_backup)
        logger.info(f"💾 Corrupted database backed up to: {corrupted_backup}")

        # Try to dump and restore
        temp_dump = f"temp_dump_{timestamp}.sql"

        # Attempt to dump recoverable data
        os.system(f"sqlite3 {DB_FILE} .dump > {temp_dump}")

        # Remove corrupted file
        os.remove(DB_FILE)

        # Restore from dump
        os.system(f"sqlite3 {DB_FILE} < {temp_dump}")

        # Clean up
        os.remove(temp_dump)

        # Initialize database structure
        await init_database()

        logger.info("✅ Database repair attempted")
        return True

    except Exception as e:
        logger.error(f"❌ Database repair failed: {e}")
        return False


async def restore_from_latest_backup():
    """Restore database from latest backup"""
    try:
        backup_dir = "backups"
        if not os.path.exists(backup_dir):
            logger.error("❌ No backup directory found")
            return False

        backup_files = [f for f in os.listdir(backup_dir) if f.endswith('.db')]
        if not backup_files:
            logger.error("❌ No backup files found")
            return False

        # Get latest backup
        latest_backup = max(
            backup_files,
            key=lambda x: os.path.getmtime(os.path.join(backup_dir, x)))
        backup_path = os.path.join(backup_dir, latest_backup)

        # Backup current corrupted file
        timestamp = datetime.datetime.now(
            timezone.utc).strftime("%Y%m%d_%H%M%S")
        shutil.copy2(DB_FILE, f"{DB_FILE}.corrupted_{timestamp}")

        # Restore from backup
        shutil.copy2(backup_path, DB_FILE)

        logger.info(f"✅ Database restored from backup: {latest_backup}")
        return True

    except Exception as e:
        logger.error(f"❌ Backup restore failed: {e}")
        return False


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
            "``````\n🔧 *Please try again in a few moments...*",
            color=0xFF1744)
        embed.add_field(name="🆘 **Error Code**",
                        value="``````",
                        inline=True)
        embed.set_footer(
            text="🛠️ If this persists, contact an administrator",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else None)

        # Use light_safe_api_call instead of direct ctx.send
        result, embed_error = await light_safe_api_call(ctx.send, embed=embed)
        if embed_error:
            # Fallback: try sending a simple text message with light breaker
            logger.error(f"❌ Error sending embed: {embed_error}")
            result, text_error = await light_safe_api_call(
                ctx.send, 
                "❌ A system error occurred. Please contact an administrator."
            )
            if text_error:
                # Ultimate fallback: just log it
                logger.error(
                    f"❌ Could not send error message to user {ctx.author.id}: {text_error}"
                )

    except Exception as handler_error:
        # Don't let the error handler itself crash the bot
        logger.error(f"❌ Error in global error handler: {handler_error}")



load_dotenv()
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if TOKEN:
    TOKEN = TOKEN.strip()
else:
    logger.critical("❌ DISCORD_BOT_TOKEN not set in env variables")

# ==== Constants ====
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
async def init_database():
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

    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_users_balance ON users(balance DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_users_sp ON users(sp DESC)')
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_monthly_stats_month ON monthly_stats(month)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_monthly_stats_losses ON monthly_stats(losses DESC)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_temp_admins_expires ON temp_admins(expires_at)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_name_change_cards_expires ON name_change_cards(expires_at)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_transactions_user_id ON transactions(user_id)'
    )
    cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_transactions_timestamp ON transactions(timestamp DESC)'
    )

    conn.commit()
    conn.close()


# ==== Database Helper Functions ====
def get_user_data(user_id):
    """Fetch user data without overwriting restored values unless new"""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT user_id, balance, sp, last_claim, streak, created_at FROM users WHERE user_id = ?',
                (user_id, ))
            user = cursor.fetchone()

            # If user not found, insert with default starting values
            if not user:
                cursor.execute(
                    '''
                    INSERT INTO users (user_id, balance, sp, streak)
                    VALUES (?, 0, 100, 0)
                    ''', (user_id, ))
                conn.commit()
                cursor.execute(
                    'SELECT user_id, balance, sp, last_claim, streak, created_at FROM users WHERE user_id = ?',
                    (user_id, ))
                user = cursor.fetchone()

            return {
                'user_id': user[0],
                'balance': user[1],
                'sp': user[2],
                'last_claim': user[3],
                'streak': user[4],
                'created_at': user[5]
            }

    except Exception as e:
        logger.error(f"❌ get_user_data error: {e}")
        return {
            'user_id': user_id,
            'balance': 0,
            'sp': 100,
            'last_claim': None,
            'streak': 0,
            'created_at': None
        }


async def _safe_update_user_data(user_id, **kwargs):
    """Helper function to perform the actual database update (async)"""
    # Validate allowed fields to prevent injection
    ALLOWED_FIELDS = {'balance', 'sp', 'last_claim', 'streak'}

    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Ensure user exists first
            cursor.execute('SELECT user_id FROM users WHERE user_id = ?',
                           (user_id, ))
            if not cursor.fetchone():
                cursor.execute(
                    'INSERT INTO users (user_id, balance, sp, streak) VALUES (?, 0, 100, 0)',
                    (user_id, ))

            # Build update query with validated fields only
            valid_updates = {
                k: v
                for k, v in kwargs.items() if k in ALLOWED_FIELDS
            }

            if valid_updates:
                placeholders = ', '.join(f'{field} = ?'
                                         for field in valid_updates.keys())
                query = f'UPDATE users SET {placeholders} WHERE user_id = ?'
                values = list(valid_updates.values()) + [user_id]
                cursor.execute(query, values)

            conn.commit()
            return True

    except sqlite3.OperationalError as e:
        logger.error(f"❌ Database error: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Update error: {e}")
        return False


async def safe_db_operation(operation_func, *args, **kwargs):
    """Safely execute database operations with error handling"""
    max_retries = 3
    retry_count = 0

    while retry_count < max_retries:
        try:
            return await operation_func(*args, **kwargs)
        except sqlite3.Error as e:
            logger.error(f"❌ Database error (attempt {retry_count + 1}): {e}")

            if "malformed" in str(e).lower() or "corrupt" in str(e).lower():
                if retry_count == 0:
                    # First attempt: try to repair
                    if await repair_database():
                        retry_count += 1
                        continue
                elif retry_count == 1:
                    # Second attempt: restore from backup
                    if await restore_from_latest_backup():
                        retry_count += 1
                        continue
                else:
                    # Final attempt: reinitialize database
                    await init_database()

            retry_count += 1
            if retry_count < max_retries:
                await asyncio.sleep(1)  # Wait before retry

    logger.error("❌ All database operation attempts failed")
    return None


async def safe_update_user_data(user_id, **kwargs):
    """Thread-safe user data update with retry logic (async wrapper)"""
    max_retries = 3
    retry_delay = 0.5

    for attempt in range(max_retries):
        try:
            result = await _safe_update_user_data(user_id, **kwargs)
            if result:
                return True
            else:
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (2**attempt))
                    continue
                else:
                    return False

        except Exception as e:
            logger.error(f"❌ Error during retry: {e}")
            return False

    return False


async def update_user_data(user_id: str, **kwargs):
    """Update user data asynchronously - FIXED VERSION"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()

        # Get current data
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id, ))
        user_data = cursor.fetchone()

        if not user_data:
            # Create new user
            cursor.execute(
                """
                INSERT INTO users (user_id, balance, sp, daily_streak, last_daily)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, kwargs.get('balance', 0), kwargs.get('sp', 100),
                  kwargs.get('daily_streak', 0), kwargs.get('last_daily', '')))
        else:
            # Update existing user
            set_clauses = []
            values = []

            for key, value in kwargs.items():
                if key in ['balance', 'sp', 'daily_streak', 'last_daily']:
                    set_clauses.append(f"{key} = ?")
                    values.append(value)

            if set_clauses:
                values.append(user_id)
                query = f"UPDATE users SET {', '.join(set_clauses)} WHERE user_id = ?"
                cursor.execute(query, values)

        conn.commit()
        conn.close()
        return True

    except sqlite3.Error as e:
        logger.error(f"❌ Database error in update_user_data: {e}")

        # Check if database is corrupted
        if "malformed" in str(e).lower() or "corrupt" in str(e).lower():
            logger.warning(
                "🔄 Database corruption detected, attempting repair...")

            # Attempt repair
            if await repair_database():
                # Retry the operation
                try:
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    # ... repeat the operation
                    conn.commit()
                    conn.close()
                    return True
                except Exception:
                    # If repair fails, restore from backup
                    await restore_from_latest_backup()

        return False
    except Exception as e:
        logger.error(f"❌ Unexpected error in update_user_data: {e}")
        return False


def get_monthly_stats(user_id, month=None):
    """Get monthly gambling stats for user"""
    if not month:
        month = datetime.datetime.now(timezone.utc).strftime("%Y-%m")

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
        month = datetime.datetime.now(timezone.utc).strftime("%Y-%m")

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
        self.max_retries = 3
        self.retry_delay = 5

    def upload_backup_to_github(self, backup_file_path):
        """Upload backup with retry logic and better error handling"""
        for attempt in range(self.max_retries):
            try:
                logger.info(
                    f"🔄 GitHub upload attempt {attempt + 1}/{self.max_retries}"
                )

                # Read and encode file
                with open(backup_file_path, 'rb') as f:
                    file_content = f.read()

                if len(file_content) == 0:
                    return False, "Backup file is empty"

                encoded_content = base64.b64encode(file_content).decode(
                    'utf-8')
                filename = os.path.basename(backup_file_path)
                github_path = f"backups/{filename}"

                # Check if file exists
                check_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/{github_path}"
                check_response = requests.get(check_url,
                                              headers=self.headers,
                                              timeout=30)

                # Prepare commit data
                commit_data = {
                    "message":
                    f"🤖 Auto backup: {filename} ({len(file_content):,} bytes)",
                    "content": encoded_content,
                    "branch": "main"
                }

                # Add SHA if file exists
                if check_response.status_code == 200:
                    existing_file = check_response.json()
                    commit_data["sha"] = existing_file["sha"]
                    logger.info("📝 Updating existing backup file")
                else:
                    logger.info("📝 Creating new backup file")

                # Upload file
                upload_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/{github_path}"
                response = requests.put(upload_url,
                                        headers=self.headers,
                                        json=commit_data,
                                        timeout=60)

                if response.status_code in [200, 201]:
                    logger.info(f"✅ GitHub upload successful: {filename}")
                    return True, response.json()
                elif response.status_code == 403:
                    error_msg = f"GitHub API forbidden (check token permissions): {response.text[:200]}"
                    logger.error(error_msg)
                    return False, error_msg
                elif response.status_code == 422:
                    error_msg = f"GitHub API validation error: {response.text[:200]}"
                    logger.error(error_msg)
                    return False, error_msg
                else:
                    error_msg = f"GitHub API error {response.status_code}: {response.text[:200]}"
                    logger.warning(error_msg)

                    # Retry on certain errors
                    if attempt < self.max_retries - 1 and response.status_code in [
                            500, 502, 503, 504
                    ]:
                        logger.info(
                            f"⏳ Retrying in {self.retry_delay} seconds...")
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        return False, error_msg

            except requests.exceptions.Timeout:
                error_msg = f"GitHub upload timeout (attempt {attempt + 1})"
                logger.warning(error_msg)
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
                else:
                    return False, "GitHub upload timeout after retries"

            except Exception as e:
                error_msg = f"GitHub upload error: {str(e)}"
                logger.error(error_msg)
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_delay)
                    continue
                else:
                    return False, error_msg

        return False, "Max retries exceeded"

    def test_connection(self):
        """Test GitHub API connection and permissions"""
        try:
            # Test repository access
            test_url = f"{GITHUB_API_BASE}/repos/{self.repo}"
            response = requests.get(test_url, headers=self.headers, timeout=15)

            if response.status_code == 200:
                repo_data = response.json()
                logger.info(
                    f"✅ GitHub repo accessible: {repo_data.get('full_name')}")

                # Test write permissions by checking if we can list contents
                contents_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents"
                contents_response = requests.get(contents_url,
                                                 headers=self.headers,
                                                 timeout=15)

                if contents_response.status_code in [
                        200, 404
                ]:  # 404 is OK for empty repo
                    logger.info("✅ GitHub write access confirmed")
                    return True, "Connection successful"
                else:
                    return False, f"No write access: {contents_response.status_code}"
            else:
                return False, f"Repository access failed: {response.status_code} - {response.text[:100]}"

        except Exception as e:
            return False, f"Connection test failed: {str(e)}"

    def download_backup_from_github(self, filename=None):
        """Download backup file from GitHub repository with retries"""
        for attempt in range(self.max_retries):
            try:
                if not filename:
                    # Get the latest backup file
                    success, files = self.list_github_backups()
                    if not success or not files:
                        return False, "No backup files found in repository"
                    filename = files[0]['name']  # Already sorted by date

                # Download the specific file
                download_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/backups/{filename}"
                response = requests.get(download_url,
                                        headers=self.headers,
                                        timeout=60)

                if response.status_code != 200:
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                        continue
                    return False, f"Failed to download {filename}: {response.status_code}"

                file_data = response.json()
                file_content = base64.b64decode(file_data['content'])

                # Create local backups directory
                if not os.path.exists("backups"):
                    os.makedirs("backups")

                # Save the downloaded file locally
                local_path = os.path.join("backups", filename)
                with open(local_path, 'wb') as f:
                    f.write(file_content)

                logger.info(
                    f"✅ Downloaded backup: {filename} ({len(file_content):,} bytes)"
                )
                return True, local_path

            except Exception as e:
                if attempt < self.max_retries - 1:
                    logger.warning(
                        f"Download attempt {attempt + 1} failed: {e}")
                    time.sleep(self.retry_delay)
                    continue
                else:
                    return False, f"Download failed after retries: {str(e)}"

        return False, "Max retries exceeded"

    def list_github_backups(self):
        """List all backup files in GitHub repository with retries"""
        for attempt in range(self.max_retries):
            try:
                list_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/backups"
                response = requests.get(list_url,
                                        headers=self.headers,
                                        timeout=30)

                if response.status_code == 200:
                    files = response.json()
                    backup_files = [
                        f for f in files if f['name'].endswith('.db')
                    ]
                    backup_files.sort(key=lambda x: x['name'], reverse=True)
                    return True, backup_files
                elif response.status_code == 404:
                    # Backups directory doesn't exist yet
                    return True, []
                else:
                    if attempt < self.max_retries - 1:
                        time.sleep(self.retry_delay)
                        continue
                    return False, []

            except Exception as e:
                if attempt < self.max_retries - 1:
                    logger.warning(f"List attempt {attempt + 1} failed: {e}")
                    time.sleep(self.retry_delay)
                    continue
                else:
                    logger.error(f"Error listing GitHub backups: {e}")
                    return False, []

        return False, []

    def delete_old_backups(self, keep_count=20):
        """Delete old backup files from GitHub, keeping only the newest ones"""
        try:
            success, backup_files = self.list_github_backups()
            if not success:
                return False, "Failed to list backups"

            if len(backup_files) <= keep_count:
                return True, f"Only {len(backup_files)} backups found, no cleanup needed"

            old_backups = backup_files[keep_count:]  # Files to delete
            deleted_count = 0

            for backup_file in old_backups:
                try:
                    delete_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/backups/{backup_file['name']}"
                    delete_data = {
                        "message":
                        f"🗑️ Auto cleanup: Remove old backup {backup_file['name']}",
                        "sha": backup_file['sha'],
                        "branch": "main"
                    }

                    response = requests.delete(delete_url,
                                               headers=self.headers,
                                               json=delete_data,
                                               timeout=30)
                    if response.status_code == 200:
                        deleted_count += 1
                        logger.info(
                            f"🗑️ Deleted old backup: {backup_file['name']}")
                    else:
                        logger.warning(
                            f"Failed to delete {backup_file['name']}: {response.status_code}"
                        )

                except Exception as delete_error:
                    logger.error(
                        f"Error deleting {backup_file['name']}: {delete_error}"
                    )

            return True, f"Deleted {deleted_count} old backups"

        except Exception as e:
            return False, f"Cleanup error: {str(e)}"


# Initialize GitHub backup manager (overwrite the None placeholder)
if GITHUB_TOKEN and GITHUB_BACKUP_REPO:
    github_backup = GitHubBackupManager(GITHUB_TOKEN, GITHUB_BACKUP_REPO)


def create_backup_with_cloud_storage():
    """Create a comprehensive backup with proper SQLite handling"""
    try:
        # Create backups directory
        if not os.path.exists("backups"):
            os.makedirs("backups")

        # Generate unique timestamp
        timestamp = datetime.datetime.now(
            timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup_{timestamp}.db"
        backup_path = os.path.join("backups", backup_filename)

        logger.info(f"🔄 Creating SQLite backup: {backup_filename}")

        # Method 1: Use VACUUM INTO for clean backup (recommended)
        try:
            conn = sqlite3.connect(DB_FILE, timeout=30)
            conn.execute(f"VACUUM INTO '{backup_path}'")
            conn.close()
            logger.info("✅ Used VACUUM INTO method")
        except Exception as vacuum_error:
            logger.warning(f"⚠️ VACUUM method failed: {vacuum_error}")

            # Method 2: Fallback to file copy with WAL checkpoint
            try:
                conn = sqlite3.connect(DB_FILE, timeout=30)
                conn.execute("PRAGMA wal_checkpoint(FULL)")
                conn.close()

                # Copy main database file
                shutil.copy2(DB_FILE, backup_path)
                logger.info("✅ Used checkpoint + copy method")
            except Exception as copy_error:
                logger.error(f"❌ Backup creation failed: {copy_error}")
                return None

        # Verify backup was created
        if os.path.exists(backup_path):
            file_size = os.path.getsize(backup_path)
            if file_size > 0:
                logger.info(
                    f"✅ Backup created: {backup_filename} ({file_size:,} bytes)"
                )
                return backup_path
            else:
                logger.error("❌ Backup file is empty")
                os.remove(backup_path)
                return None
        else:
            logger.error("❌ Backup file not created")
            return None

    except Exception as e:
        logger.error(f"❌ Backup creation error: {e}")
        return None


async def restore_from_cloud():
    """Restore database from the most recent backup (local or GitHub)"""
    try:
        latest_backup = None
        restored_from_github = False

        # Try to download the latest from GitHub first
        if github_backup:
            success, result = github_backup.download_backup_from_github(
            )  # Ensure await is used properly# Ensure await is used properly
            if success:
                latest_backup = result
                restored_from_github = True
            else:
                logger.warning(f"GitHub download failed: {result}")

        # Fallback to local backups if GitHub fails
        if not restored_from_github:
            if not os.path.exists("backups"):
                logger.warning(
                    "⚠️ No backup found to restore from. Creating a new database."
                )
                await init_database()  # Use await here
                return True

            backup_files = [
                f for f in os.listdir("backups")
                if f.startswith("backup_") and f.endswith(".db")
            ]
            if not backup_files:
                logger.warning(
                    "⚠️ No backup found to restore from. Creating a new database."
                )
                await init_database()  # Use await here
                return True

            backup_files.sort(
                key=lambda x: os.path.getmtime(os.path.join("backups", x)),
                reverse=True)
            latest_backup = os.path.join("backups", backup_files[0])

        # Only proceed if we have a backup to restore
        if latest_backup:
            # Backup current database before restore
            current_backup = f"pre_restore_backup_{datetime.datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy2(DB_FILE, os.path.join("backups", current_backup))

            # Restore from backup
            shutil.copy2(latest_backup, DB_FILE)
            logger.info(f"✅ Database restored from: {latest_backup}")
            return True
        else:
            logger.warning(
                "⚠️ No backup found to restore from. Creating a new database.")
            await init_database()  # Use await here
            return True

    except Exception as e:
        logger.error(f"❌ Database restore failed: {e}")
        return False


def create_sqlite_backup_vacuum():
    """Create SQLite backup using VACUUM INTO for cleaner backup"""
    try:
        import sqlite3

        if not os.path.exists("backups"):
            os.makedirs("backups")

        timestamp = datetime.datetime.now(
            timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_filename = f"backup_{timestamp}.db"
        backup_path = os.path.join("backups", backup_filename)

        print(f"🔍 DEBUG: Creating SQLite VACUUM backup at: {backup_path}")

        # Use VACUUM INTO for a clean, optimized backup
        conn = sqlite3.connect(DB_FILE)
        conn.execute(f"VACUUM INTO '{backup_path}'")
        conn.close()

        if os.path.exists(backup_path):
            file_size = os.path.getsize(backup_path)
            print(
                f"✅ SQLite VACUUM backup created: {backup_filename}, size: {file_size} bytes"
            )
            return backup_path
        else:
            print("❌ SQLite VACUUM backup failed")
            return None

    except Exception as e:
        print(f"❌ SQLite VACUUM backup error: {e}")
        return None


def download_backup_from_github(self, filename=None):
    """Download backup file from GitHub repository"""
    try:
        print("🔍 DEBUG: Attempting to download backup...")
        print(f"🔍 DEBUG: Repository: {self.repo}")

        if not filename:
            # Get the latest backup file
            list_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/backups"
            print(f"🔍 DEBUG: List URL: {list_url}")

            response = requests.get(list_url, headers=self.headers)
            print(f"🔍 DEBUG: List response: {response.status_code}")

            if response.status_code != 200:
                print(f"❌ Failed to list backup files: {response.text}")
                return False, "Failed to list backup files"

            files = response.json()
            print(f"🔍 DEBUG: Found {len(files)} files in backups folder")

            backup_files = [f for f in files if f['name'].endswith('.db')]
            print(f"🔍 DEBUG: Found {len(backup_files)} .db files")

            for f in backup_files:
                print(f"🔍 DEBUG: Backup file: {f['name']}")

            if not backup_files:
                return False, "No backup files found in repository"

            # Sort by name (which includes timestamp) to get latest
            backup_files.sort(key=lambda x: x['name'], reverse=True)
            latest_file = backup_files[0]
            filename = latest_file['name']
            print(f"🔍 DEBUG: Selected latest backup: {filename}")

        # Download the specific file
        download_url = f"{GITHUB_API_BASE}/repos/{self.repo}/contents/backups/{filename}"
        print(f"🔍 DEBUG: Download URL: {download_url}")

        response = requests.get(download_url, headers=self.headers)
        print(f"🔍 DEBUG: Download response: {response.status_code}")

        if response.status_code != 200:
            print(f"❌ Failed to download {filename}: {response.text}")
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

        print(f"✅ Successfully downloaded backup to: {local_path}")
        return True, local_path

    except Exception as e:
        print(f"❌ Download error: {e}")
        return False, str(e)


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
        month = datetime.datetime.now(timezone.utc).strftime("%Y-%m")

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
async def ensure_database_exists():
    """Ensure database exists, restore from backup if needed"""
    # If DB file missing, try restore
    if not os.path.exists(DB_FILE):
        logger.warning("⚠️ Database not found, attempting restore...")
        if github_backup and await restore_from_cloud():  # Use await here
            logger.info("✅ Database restored from backup")
        else:
            logger.info("📝 Creating new database...")
            await init_database()
    else:
        # Before touching DB, attempt to restore latest backup
        if github_backup:
            logger.info(
                "🔄 Attempting to restore latest cloud backup before startup")
            await restore_from_cloud()  # Use await here
        else:
            logger.info("📂 Using existing local database")

        # Ensure all required tables exist (non-destructive)
        await init_database()

    # Clear caches after restore so fresh data loads
    if 'user_cache' in globals():
        user_cache.clear()
    if 'leaderboard_cache' in globals():
        leaderboard_cache.clear()


async def enhanced_startup():
    """Enhanced startup with health checks and graceful degradation"""
    logger.info("🚀 Enhanced startup sequence beginning...")

    # Test database first
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            logger.info("✅ Database connection verified")
    except Exception as e:
        logger.error(f"❌ Database startup failed: {e}")
        return False

    # Test Discord API
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
                total=10)) as session:
            headers = {"Authorization": f"Bot {TOKEN}"}
            async with session.get("https://discord.com/api/v10/users/@me",
                                   headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(
                        f"✅ Discord API connection verified: {data.get('username')}"
                    )
                else:
                    logger.error(f"❌ Discord API error: {resp.status}")
                    return False
    except Exception as e:
        logger.error(f"❌ Discord API test failed: {e}")
        return False

    # Initialize circuit breaker
    api_circuit_breaker.state = CircuitState.CLOSED
    logger.info("✅ Circuit breaker initialized")

    return True


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
@tasks.loop(hours=3)
async def enhanced_auto_backup():
    """Enhanced automatic backup with comprehensive error handling"""
    logger.info("🔄 [AUTO-BACKUP] Starting scheduled backup process...")

    try:
        # Pre-flight checks
        if not github_backup:
            logger.error(
                "❌ [AUTO-BACKUP] GitHub backup manager not initialized")
            return False

        # Test GitHub connection first
        github_ok, github_msg = github_backup.test_connection()
        if not github_ok:
            logger.error(
                f"❌ [AUTO-BACKUP] GitHub connection failed: {github_msg}")
            # Continue with local backup even if GitHub fails

        # Check disk space
        try:
            import shutil
            free_space = shutil.disk_usage('.').free
            if free_space < 100_000_000:  # Less than 100MB
                logger.error(
                    f"❌ [AUTO-BACKUP] Low disk space: {free_space:,} bytes")
                return False
        except Exception as space_error:
            logger.warning(
                f"⚠️ [AUTO-BACKUP] Could not check disk space: {space_error}")

        # Create local backup
        logger.info("📁 [AUTO-BACKUP] Creating local backup...")
        backup_file = create_backup_with_cloud_storage()

        if not backup_file:
            logger.error("❌ [AUTO-BACKUP] Failed to create local backup")
            return False

        file_size = os.path.getsize(backup_file)
        logger.info(
            f"✅ [AUTO-BACKUP] Local backup created: {os.path.basename(backup_file)} ({file_size:,} bytes)"
        )

        # Upload to GitHub if connection is OK
        if github_ok:
            logger.info("☁️ [AUTO-BACKUP] Uploading to GitHub...")
            success, result = github_backup.upload_backup_to_github(
                backup_file)

            if success:
                logger.info("✅ [AUTO-BACKUP] GitHub upload successful")

                # Clean up old local backups (keep last 10)
                try:
                    backup_files = [
                        f for f in os.listdir("backups")
                        if f.startswith("backup_") and f.endswith(".db")
                    ]
                    backup_files.sort(key=lambda x: os.path.getmtime(
                        os.path.join("backups", x)),
                                      reverse=True)

                    if len(backup_files) > 10:
                        for old_backup in backup_files[10:]:
                            old_path = os.path.join("backups", old_backup)
                            os.remove(old_path)
                            logger.info(
                                f"🗑️ [AUTO-BACKUP] Cleaned up old backup: {old_backup}"
                            )

                except Exception as cleanup_error:
                    logger.warning(
                        f"⚠️ [AUTO-BACKUP] Cleanup warning: {cleanup_error}")

                return True
            else:
                logger.error(f"❌ [AUTO-BACKUP] GitHub upload failed: {result}")
                return False
        else:
            logger.warning(
                "⚠️ [AUTO-BACKUP] Skipping GitHub upload due to connection issues"
            )
            return True  # Local backup succeeded

    except Exception as e:
        logger.error(f"❌ [AUTO-BACKUP] Critical error: {e}", exc_info=True)
        return False


# Set up error handler for the task
@enhanced_auto_backup.error
async def backup_task_error_handler(arg, error):
    """Handle errors in the backup task and attempt restart"""
    logger.error(f"❌ [AUTO-BACKUP] Task error occurred: {error}")

    # Wait a bit before restarting
    await asyncio.sleep(60)  # Wait 1 minute

    try:
        if not enhanced_auto_backup.is_running():
            logger.info("🔄 [AUTO-BACKUP] Attempting to restart backup task...")
            enhanced_auto_backup.restart()
            logger.info("✅ [AUTO-BACKUP] Task restarted successfully")
    except Exception as restart_error:
        logger.error(
            f"❌ [AUTO-BACKUP] Failed to restart task: {restart_error}")


# Assign to auto_backup variable for compatibility
auto_backup = enhanced_auto_backup


@tasks.loop(hours=12)
async def backup_health_monitor():
    """Monitor backup system health and alert on issues"""
    try:
        logger.info("🔍 [HEALTH-CHECK] Starting backup health check...")

        issues = []

        # Check if auto backup task is running
        if not auto_backup.is_running():
            issues.append("Auto backup task is not running")
            logger.warning("⚠️ [HEALTH-CHECK] Auto backup task stopped")

            # Try to restart it
            try:
                auto_backup.start()
                logger.info("✅ [HEALTH-CHECK] Restarted auto backup task")
            except Exception as restart_error:
                logger.error(
                    f"❌ [HEALTH-CHECK] Failed to restart backup task: {restart_error}"
                )

        # Check local backup freshness
        try:
            if os.path.exists("backups"):
                backup_files = [
                    f for f in os.listdir("backups")
                    if f.startswith("backup_") and f.endswith(".db")
                ]
                if backup_files:
                    backup_files.sort(key=lambda x: os.path.getmtime(
                        os.path.join("backups", x)),
                                      reverse=True)
                    latest_backup = backup_files[0]
                    latest_time = os.path.getmtime(
                        os.path.join("backups", latest_backup))
                    hours_since = (time.time() - latest_time) / 3600

                    if hours_since > 8:  # Alert if no backup for 8+ hours
                        issues.append(
                            f"Latest backup is {hours_since:.1f} hours old")
                        logger.warning(
                            f"⚠️ [HEALTH-CHECK] Latest backup: {hours_since:.1f} hours ago"
                        )
                else:
                    issues.append("No local backups found")
                    logger.warning("⚠️ [HEALTH-CHECK] No local backups found")
            else:
                issues.append("Backup directory doesn't exist")
                logger.warning("⚠️ [HEALTH-CHECK] Backup directory missing")
        except Exception as local_check_error:
            issues.append(
                f"Local backup check failed: {str(local_check_error)}")

        # Check GitHub connection
        if github_backup:
            github_ok, github_msg = github_backup.test_connection()
            if not github_ok:
                issues.append(f"GitHub connection failed: {github_msg}")
                logger.warning(f"⚠️ [HEALTH-CHECK] GitHub issue: {github_msg}")
        else:
            issues.append("GitHub backup not configured")

        # Report health status
        if issues:
            logger.warning(f"⚠️ [HEALTH-CHECK] Found {len(issues)} issues:")
            for issue in issues:
                logger.warning(f"  - {issue}")
        else:
            logger.info("✅ [HEALTH-CHECK] All backup systems healthy")

    except Exception as e:
        logger.error(f"❌ [HEALTH-CHECK] Health check error: {e}")


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
    current_month = datetime.datetime.now(timezone.utc).strftime("%Y-%m")
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
        now = datetime.datetime.now(timezone.utc)

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
                if bot is not None and bot.guilds:
                    for guild in bot.guilds:
                        # Find a general channel to announce - ensure it's a TextChannel
                        channel = discord.utils.get(guild.text_channels,
                                                    name='general')
                        if not channel and guild.text_channels:
                            channel = guild.text_channels[0]

                        if channel and isinstance(channel,
                                                  discord.TextChannel):
                            try:
                                embed = discord.Embed(
                                    title="🌟 **MONTHLY ASCENSION COMPLETE** 🌟",
                                    description=
                                    "```css\n[SPIRIT ENERGY CRYSTALLIZATION RITUAL]\n```\n💎 *The cosmic cycle renews, power has been preserved...*",
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
                                    f"❌ Failed to announce in {guild.name}: {e}"
                                )
                        else:
                            logger.warning(
                                f"❌ No suitable text channel found in {guild.name}"
                            )
                else:
                    logger.warning(
                        "⚠️ Bot not ready or no guilds available for monthly conversion announcement"
                    )

    except Exception as e:
        logger.error(f"❌ Monthly conversion check error: {e}")


# AP I Health monitoring
@tasks.loop(minutes=5)
async def api_health_monitor():
    """Monitor API usage and rate limiting status"""
    try:
        now = time.time()

        # Clean old API call records
        global discord_api_calls
        discord_api_calls = [
            call_time for call_time in discord_api_calls
            if now - call_time < API_WINDOW
        ]

        # IMPROVED CLEANUP - Add size limits
        for user_id in list(user_command_cooldowns.keys()):
            user_commands = user_command_cooldowns[user_id]

            # Remove expired cooldowns
            for command in list(user_commands.keys()):
                if now - user_commands[command] > 86400:  # 24 hours
                    del user_commands[command]

            # Remove empty user records
            if not user_commands:
                del user_command_cooldowns[user_id]

        # CRITICAL: Prevent memory explosion - limit total entries
        if len(user_command_cooldowns) > 10000:  # Safety limit
            logger.warning(
                "⚠️ Cooldown dictionary too large, clearing old entries")
            # Keep only the 1000 most recent entries
            sorted_users = sorted(user_command_cooldowns.items(),
                                  key=lambda x: max(x[1].values())
                                  if x[1] else 0,
                                  reverse=True)
            user_command_cooldowns.clear()
            user_command_cooldowns.update(dict(sorted_users[:1000]))

    except Exception as e:
        logger.error(f"❌ API health monitor error: {e}")


async def startup_sequence():
    """Safe startup sequence with health checks"""
    logger.info("🚀 Starting bot initialization...")

    # Test database first
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            logger.info("✅ Database connection verified")
    except Exception as e:
        logger.error(f"❌ Database startup failed: {e}")
        return False

    # Test Discord API
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(
                total=10)) as session:
            headers = {"Authorization": f"Bot {TOKEN}"}
            async with session.get("https://discord.com/api/v10/users/@me",
                                   headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(
                        f"✅ Discord API connection verified: {data.get('username')}"
                    )
                else:
                    logger.error(f"❌ Discord API error: {resp.status}")
                    return False
    except Exception as e:
        logger.error(f"❌ Discord API test failed: {e}")
        return False

    # Initialize circuit breaker
    api_circuit_breaker.state = CircuitState.CLOSED
    logger.info("✅ Circuit breaker initialized")

    return True


# ==== Main Bot Execution ====
async def main():
    """Main async function with proper startup sequence and error recovery"""
    global bot
    logger.info("🚀 Starting Discord Bot...")

    # Check if TOKEN is valid before proceeding
    if not TOKEN:
        logger.critical("❌ DISCORD_BOT_TOKEN not set in environment variables")
        logger.critical(
            "Please set your Discord bot token in the environment variables")
        return

    max_connection_retries = 5
    retry_delay = 10  # seconds

    for attempt in range(max_connection_retries):
        try:
            # Test connection first
            logger.info(
                f"🔍 Testing connection (attempt {attempt + 1}/{max_connection_retries})..."
            )

            if not await test_bot_connection():
                logger.error("❌ Bot token validation failed")
                if attempt < max_connection_retries - 1:
                    logger.info(f"⏳ Retrying in {retry_delay} seconds...")
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    logger.error("❌ All connection attempts failed")
                    return

            # Create startup backup
            await startup_backup()

            # Start the bot
            logger.info("🤖 Starting Discord bot connection...")
            if bot is not None:
                await bot.start(TOKEN)
            break  # If we get here, bot started successfully

        except discord.LoginFailure:
            logger.error("❌ Invalid bot token")
            break

        except discord.HTTPException as e:
            logger.error(f"❌ Discord HTTP error: {e}")
            if attempt < max_connection_retries - 1:
                logger.info(f"⏳ Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2,
                                  300)  # Exponential backoff with max delay
            else:
                logger.error("❌ Max retries exceeded")

        except KeyboardInterrupt:
            logger.info("🛑 Keyboard interrupt received")
            break

        except Exception as e:
            logger.error(f"❌ Unexpected bot error: {e}")
            if attempt < max_connection_retries - 1:
                logger.info(f"⏳ Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2,
                                  300)  # Exponential backoff with max delay
            else:
                logger.error("❌ Max retries exceeded")

    # Cleanup
    try:
        if bot and not bot.is_closed():
            logger.info("🛑 Closing bot connection...")
            await bot.close()
    except Exception as e:
        logger.error(f"❌ Error during cleanup: {e}")

    logger.info("🛑 Bot shutdown complete")


# ==== Flask Setup ====
app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is alive!"


@app.route('/health')
def enhanced_health():
    """Enhanced health check with detailed status"""
    status = {"status": "healthy", "timestamp": time.time()}

    try:
        # Test database
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            user_count = cursor.fetchone()[0]
            status["database"] = "healthy"
            status["user_count"] = user_count

        # Test bot status
        status["bot_connected"] = bot.is_ready() if bot is not None else False
        status["guilds_count"] = len(
            bot.guilds) if bot is not None and bot.guilds else 0

        # API status
        status["circuit_breaker"] = api_circuit_breaker.state.value
        status[
            "rate_limit_remaining"] = discord_rate_limiter.max_requests - len(
                discord_rate_limiter.requests)

        # Memory usage (optional - only if psutil is available)
        try:
            import psutil
            process = psutil.Process()
            status["memory_mb"] = round(
                process.memory_info().rss / 1024 / 1024, 1)
            status["cpu_percent"] = process.cpu_percent()
        except ImportError:
            status["memory_info"] = "psutil not available"

        return status, 200

    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "timestamp": time.time()
        }, 503


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
    now = datetime.datetime.now(timezone.utc)

    for admin_data in temp_admins:
        expire_time = datetime.datetime.fromisoformat(admin_data["expires_at"])
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
        expire_time = datetime.datetime.fromisoformat(change["expires_at"])
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
    if not backup_health_monitor.is_running():  # Add this line
        backup_health_monitor.start()

    logger.info("✅ All background tasks started")

    # Test GitHub connection on startup
    if github_backup:
        github_ok, github_msg = github_backup.test_connection()
        if github_ok:
            logger.info("✅ GitHub backup system ready")
        else:
            logger.error(f"❌ GitHub backup system issue: {github_msg}")

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
    """Enhanced error handler with better user experience"""

    # Log all errors for debugging
    logger.error(f"Command error in {ctx.command}: {error}", exc_info=True)

    # Don't handle cooldowns (decorator handles this)
    if isinstance(error, commands.CommandOnCooldown):
        return

    # Create base embed for all errors
    embed = discord.Embed(color=0xFF0000)

    if isinstance(error, commands.MissingPermissions):
        embed.title = "🚫 **INSUFFICIENT PERMISSIONS**"
        embed.description = "```diff\n- Administrator access required\n```"

    elif isinstance(error, commands.MissingRequiredArgument):
        embed.title = "❌ **MISSING ARGUMENT**"
        embed.description = f"```diff\n- Missing: {error.param.name}\n+ Use !help for examples\n```"

    elif isinstance(error, commands.BadArgument):
        embed.title = "⚠️ **INVALID ARGUMENT**"
        embed.description = "```diff\n- Check your input format\n+ Use !help for examples\n```"

    elif isinstance(error, commands.CommandNotFound):
        # Suggest similar commands
        available_commands = [cmd.name for cmd in bot.commands]
        similar = [
            cmd for cmd in available_commands if error.args[0].lower() in cmd
        ]

        if similar:
            embed.title = "❓ **UNKNOWN COMMAND**"
            embed.description = f"```diff\n- Command not found\n+ Similar: {', '.join(similar[:3])}\n```"
            result, error = await light_safe_api_call(ctx.send, embed=embed, delete_after=10)
        return  # Don't show error for unknown commands without suggestions

    else:
        embed.title = "💥 **SYSTEM ERROR**"
        embed.description = "```diff\n- An unexpected error occurred\n+ Please try again or contact support\n```"

    try:
        result, error = await light_safe_api_call(ctx.send, embed=embed, delete_after=15)
    except Exception as e:
        logger.exception(f"An unexpected error occurred: {e}")
        # Fallback to simple text if embed fails
        try:
            await ctx.send("❌ An error occurred. Please try again.",
                           delete_after=10)
        except Exception as e:
            logger.exception(f"An unexpected error occurred: {e}")
            pass  # Ultimate fallback - just log it


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
        now = datetime.datetime.now(timezone.utc)
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
                last_time = datetime.datetime.fromisoformat(last_claim)
                # If the datetime doesn't have timezone info, assume UTC
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
            except ValueError:
                # Handle old datetime format without timezone
                last_time = datetime.datetime.strptime(last_claim,
                                                       "%Y-%m-%d %H:%M:%S.%f")
                last_time = last_time.replace(tzinfo=timezone.utc)
            delta = (now - last_time).days
            if delta == 0:
                #remaining = 24 - (now - last_time).seconds // 3600
                embed = discord.Embed(
                    title="⏰ **TEMPORAL LOCK ACTIVE**",
                    description=
                    "``````\n🌟 *The cosmic energy needs time to flow through your soul...*",
                    color=0x2B2D42)
                embed.set_footer(text="⚡ Daily energy recharging...",
                                 icon_url=ctx.author.avatar.url
                                 if ctx.author.avatar else None)
                result, error = await light_safe_api_call(ctx.send, embed=embed)
                return
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
        await safe_update_user_data(user_id,
                                    sp=new_sp,
                                    last_claim=now.isoformat(),
                                    streak=streak)
        # Log transaction
        log_transaction(user_id, "daily_claim", reward, old_sp, new_sp,
                        f"Daily claim with {role_bonus}")
        # Progress bar: green for completed streak days, red for remaining
        bar = ''.join(['🟩' if i < streak else '🟥' for i in range(5)])
        embed = discord.Embed(
            title="⚡ **DAILY ENERGY HARVESTED** ⚡",
            description=
            "``````\n💫 *The universe grants you its power...*",
            color=0x8A2BE2 if streak >= 3 else 0x4169E1)
        embed.add_field(
            name="🎁 **REWARDS CLAIMED**",
            value=f"``````\n{role_bonus}",
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
        result, error = await safe_api_call(ctx.send, embed=embed)
        if error:
            logger.error(f"❌ Failed to send message: {error}")
    except Exception as e:
        logger.error(f"❌ Daily command error: {e}")
        result, error = await light_safe_api_call(ctx.send, "❌ An error occurred while processing your daily claim.")


@bot.command()
@commands.has_permissions(administrator=True)
async def forceconvert(ctx):
    """Manually trigger monthly conversion (Admin only)"""
    embed = discord.Embed(
        title="⚠️ **FORCE CONVERSION WARNING** ⚠️",
        description=("``````\n"
                     "🔥 **THIS WILL:**\n"
                     "• Convert ALL users' SP to SS immediately\n"
                     "• Reset everyone's SP to 100\n"
                     "• Clear monthly gambling stats\n"
                     "• Cannot be undone!\n\n"
                     "React with ✅ to proceed or ❌ to cancel."),
        color=0xFF6B00)

    result, error = await safe_api_call(ctx.send, embed=embed)
    if error or result is None:
        logger.error(f"❌ Failed to send forceconvert warning: {error}")
        return
    message = result

    result1, error1 = await safe_api_call(message.add_reaction, "✅")
    result2, error2 = await safe_api_call(message.add_reaction, "❌")
    if error1 or error2:
        logger.warning(f"❌ Failed to add reactions: {error1 or error2}")

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
                "``````\n⚗️ *Converting all spiritual energy to eternal stones...*",
                color=0xFFAA00)
            result, error = await safe_api_call(message.edit, embed=embed)
            if error:
                logger.error(f"❌ Failed to edit forceconvert message: {error}")

            total_converted, user_count = await perform_monthly_conversion()
            if total_converted > 0:
                embed = discord.Embed(
                    title="✅ **CONVERSION COMPLETE**",
                    description=
                    "``````\n💎 *All spiritual energy has been crystallized!*",
                    color=0x00FF00)
                embed.add_field(
                    name="📊 **CONVERSION STATISTICS**",
                    value=
                    "``````",
                    inline=False)
                embed.add_field(
                    name="🔧 **ADMINISTRATOR**",
                    value=
                    "``````",
                    inline=True)
                embed.set_footer(
                    text="💫 Manual conversion completed by administrator")
            else:
                embed = discord.Embed(
                    title="ℹ️ **NO CONVERSION NEEDED**",
                    description="``````",
                    color=0x87CEEB)
        else:
            embed = discord.Embed(
                title="❌ **CONVERSION CANCELLED**",
                description=
                "``````\n🛡️ *No changes made to user balances.*",
                color=0x808080)
    except asyncio.TimeoutError:
        embed = discord.Embed(
            title="⏰ **TIMEOUT**",
            description=
            "``````\n🕐 *No response received. Conversion cancelled.*",
            color=0x808080)

    result, error = await safe_api_call(message.edit, embed=embed)
    if error:
        logger.error(f"❌ Failed to edit final forceconvert message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('nextconvert')
async def nextconvert(ctx):
    """Show when the next monthly conversion will happen"""
    now = datetime.datetime.now(timezone.utc)
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
            f"``````\n⏰ *When the cosmic cycle completes, all energy crystallizes...*\n{f'Time remaining: {time_remaining}' if time_remaining != 'Conversion in progress...' else time_remaining}",
            color=0x4169E1)
        embed.add_field(name="📅 **NEXT CONVERSION**",
                        value=next_conversion,
                        inline=False)
        embed.add_field(name="⏳ **TIME REMAINING**",
                        value=time_remaining,
                        inline=True)
        embed.add_field(
            name="⚗️ **WHAT HAPPENS?**",
            value=
            "```diff\n+ All Spirit Points → Spirit Stones\n+ SP reset to 100 for everyone\n+ Monthly gambling stats reset\n+ Your wealth is now permanent!\n```",
            inline=False)
        embed.add_field(
            name="💡 **PRO TIP**",
            value=
            "```fix\nSave your SP for the next monthly conversion to maximize your wealth!\n```",
            inline=False)
        embed.set_footer(
            text=
            "💫 Monthly conversion happens automatically on the 1st of each month",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        if error:
            logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('cloudbackup')
async def cloudbackup(ctx):
    """Create a manual backup with GitHub cloud storage"""
    embed = discord.Embed(
        title="☁️ **Creating Cloud Backup...**",
        description=
        "``````\n💾 *Crystallizing the cosmic data into eternal storage...*",
        color=0x00FFAA)

    result, error = await safe_api_call(ctx.send, embed=embed)
    if error or result is None:
        logger.error(f"❌ Failed to send cloudbackup message: {error}")
        return
    message = result

    try:
        # Create local backup first
        backup_file = create_backup_with_cloud_storage()
        if backup_file:
            # Try to upload to GitHub
            github_success = False
            _github_error = "Not configured"
            if github_backup:
                # Update embed to show GitHub upload in progress
                embed.description = "``````\n☁️ *Transferring cosmic data to the eternal vault...*"
                result, error = await safe_api_call(message.edit, embed=embed)
                if error:
                    logger.error(f"❌ Failed to edit cloudbackup progress message: {error}")

                success, result_msg = github_backup.upload_backup_to_github(backup_file)
                if success:
                    github_success = True
                else:
                    github_success = False
                    _github_error = result_msg
            # Create final success embed
            embed = discord.Embed(
                title="✅ **Cloud Backup Created**",
                description=
                "``````\n💎 *Database backup created and processed!*",
                color=0x00FF00)

            embed.add_field(
                name="📁 **Local File**",
                value="``````",
                inline=True)

            # GitHub status
            if github_success:
                embed.add_field(name="☁️ **GitHub Cloud**",
                                value="``````",
                                inline=True)
            else:
                embed.add_field(
                    name="☁️ **GitHub Cloud**",
                    value="``````",
                    inline=True)

            # Database statistics
            _file_size = os.path.getsize(backup_file)
            embed.add_field(
                name="📊 **Backup Statistics**",
                value="``````",
                inline=False)
            embed.set_footer(
                text="💫 Your cosmic data is now safely preserved in the eternal vault",
                icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
        else:
            embed = discord.Embed(
                title="❌ **Backup Failed**",
                description=
                "``````\n💀 *Failed to create local backup. Check logs for details.*",
                color=0xFF0000)
    except Exception as e:
        embed = discord.Embed(
            title="❌ **Backup Error**",
            description=
            f"``````\n💀 *An error occurred: {str(e)[:100]}...*",
            color=0xFF0000)

    result, error = await safe_api_call(message.edit, embed=embed)
    if error:
        logger.error(f"❌ Failed to edit final cloudbackup message: {error}")

def validate_discord_input(text, max_length=2000, allow_mentions=False):
    """Validates Discord input to prevent common issues."""
    if not isinstance(text, str):
        return False, "Input must be a string."
    if len(text) > max_length:
        return False, f"Input exceeds maximum length of {max_length} characters."
    if not allow_mentions and ("@" in text or "<@!" in text or "<@" in text):
        return False, "Mentions are not allowed in this input."
    # Add more checks as needed (e.g., profanity filter, disallowed characters)
    return True, None

@bot.command()
@safe_command_wrapper
@cooldown_check('usename')
async def usename(ctx, member: discord.Member, *, new_nickname: str):
    """Use a name change card on someone"""
    # ADD INPUT VALIDATION
    is_valid, error_msg = validate_discord_input(new_nickname,
                                                 max_length=32,
                                                 allow_mentions=False)
    if not is_valid:
        embed = discord.Embed(title="❌ **INVALID NICKNAME**",
                              description="``````",
                              color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return

    # Check if target has nickname lock
    if is_nickname_locked(str(member.id)):
        embed = discord.Embed(
            title="🔒 **TARGET PROTECTED**",
            description=
            "``````",
            color=0xFF6347)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return

    # Check nickname length and validity
    if len(new_nickname) > 32:
        embed = discord.Embed(
            title="❌ **NICKNAME TOO LONG**",
            description="``````",
            color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
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
        user_id = str(ctx.author.id)  # Add this line to get user_id
        user_data = get_user_data(user_id)  # Add this line to get user_data
        new_balance = user_data["balance"] - 10000
        await update_user_data(user_id, balance=new_balance)
        # Set expiry (24 hours from now)
        expires_at = (datetime.datetime.now(timezone.utc) +
                      timedelta(hours=24)).isoformat()
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
            "``````\n✨ *Reality bends to your will...*",
            color=0xFF1493)
        embed.add_field(
            name="🎯 **TARGET**",
            value="``````",
            inline=True)
        embed.add_field(
            name="🔄 **NAME CHANGE**",
            value="``````",
            inline=True)
        embed.add_field(name="⏰ **DURATION**",
                        value="``````",
                        inline=True)
        embed.add_field(name="💸 **COST**",
                        value="``````",
                        inline=False)
        embed.set_footer(
            text="🃏 Name will automatically revert after 24 hours",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
        result, error = await safe_api_call(ctx.send, embed=embed)
        if error:
            logger.error(f"❌ Failed to send message: {error}")
    except discord.Forbidden:
        embed = discord.Embed(
            title="🚫 **PERMISSION DENIED**",
            description=
            "``````",
            color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
    except Exception as e:
        logger.error(f"❌ Name change error: {e}")
        embed = discord.Embed(
            title="❌ **NAME CHANGE FAILED**",
            description=
            "``````",
            color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=embed)


@bot.command()
@safe_command_wrapper
@cooldown_check('sendsp')
async def sendsp(ctx, member: discord.Member, amount: int):
    """Send Spirit Points to another user (Owner only) - FIXED"""
    try:
        # Check if user has the Owner role
        user_role_ids = [role.id for role in ctx.author.roles]
        if ROLE_ID_OWNER not in user_role_ids:
            embed = discord.Embed(
                title="👑 **OWNER ACCESS REQUIRED** 👑",
                description=(
                    "``````\n"
                    "⚡ *This power belongs to the supreme ruler alone...*"),
                color=0xFF0000)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return

        # Validate amount
        if amount <= 0:
            embed = discord.Embed(
                title="❌ **INVALID AMOUNT** ❌",
                description=("``````"),
                color=0xFF4500)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return

        if amount > 1000000:
            embed = discord.Embed(
                title="⚠️ **AMOUNT TOO LARGE** ⚠️",
                description=("``````"),
                color=0xFF4500)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return

        if member.bot:
            embed = discord.Embed(
                title="🤖 **INVALID TARGET** 🤖",
                description="``````",
                color=0xFF4500)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return

        # Get receiver data
        receiver_id = str(member.id)
        receiver_data = get_user_data(receiver_id)  # This should be sync
        old_sp = receiver_data.get("sp", 0)
        new_sp = old_sp + amount

        # Update receiver's SP - FIXED: Use await
        success = await update_user_data(receiver_id, sp=new_sp)
        if not success:
            embed = discord.Embed(
                title="❌ **DATABASE ERROR** ❌",
                description=
                "``````",
                color=0xFF0000)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return

        # Log transaction - Make this async too if needed
        log_transaction(receiver_id, "owner_sp_grant", amount, old_sp, new_sp,
                        f"SP grant by Owner {ctx.author.display_name}")

        # Success embed
        embed = discord.Embed(
            title="👑 **DIVINE SP BLESSING GRANTED** 👑",
            description=("``````\n"
                         "━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                         "*✨ The Owner channels raw spiritual energy ✨*"),
            color=0x9932CC)
        embed.add_field(
            name="👑 **SUPREME OWNER**",
            value=
            "``````",
            inline=True)
        embed.add_field(
            name="🎯 **BLESSED RECIPIENT**",
            value="``````",
            inline=True)
        embed.add_field(name="⚡ **SPIRIT POINTS GRANTED**",
                        value="``````",
                        inline=False)
        embed.add_field(name="🔋 **NEW SP BALANCE**",
                        value="``````",
                        inline=False)
        embed.set_footer(
            text="👑 Supreme Owner Privilege • Spirit Point Grant System",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
        embed.timestamp = datetime.datetime.now(timezone.utc)
        result, error = await safe_api_call(ctx.send, embed=embed)
        if error:
            logger.error(f"❌ Failed to send message: {error}")
    except Exception as e:
        logger.error(f"❌ Error in sendsp command: {e}")
        error_embed = discord.Embed(
            title="❌ **SYSTEM ERROR** ❌",
            description="``````",
            color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=error_embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def restorebackup(ctx, filename: Optional[str] = None):
    """Restore database from a specific backup or cloud backup (GitHub or local)"""
    if filename:
        embed = discord.Embed(
            title="⚠️ **Restore Confirmation**",
            description=f"```diff\n+ RESTORING BACKUP: {filename}\n```\n"
            "⚠️ *This will replace your current database with the specified backup.*\n"
            "🔥 ***ALL CURRENT DATA WILL BE LOST!***\n\n"
            "React with ✅ to confirm or ❌ to cancel.",
            color=0xFFB800)

        embed.add_field(
            name="🛡️ **Safety Measures**",
            value=
            "```yaml\nCurrent DB: Will be backed up first\nRestore Source: Specific backup file\nRollback: Possible via pre-restore backup\n```",
            inline=False)
    else:
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
                title="🔄 **Restoring Backup...**",
                description=
                "```css\n[BACKUP RESTORATION IN PROGRESS]\n```\n⚡ *Restoring your specified backup...*",
                color=0xFFAA00)

            await message.edit(embed=embed)

            success = False
            if filename:  # If filename is provided, restore from that specific file
                backup_path = os.path.join("backups", filename)
                if os.path.isfile(backup_path):
                    shutil.copy2(backup_path,
                                 DB_FILE)  # Restore specific backup
                    success = True
                else:
                    embed = discord.Embed(
                        title="❌ **Restore Failed**",
                        description=
                        f"```diff\n- Backup file '{filename}' not found.\n```",
                        color=0xFF0000)
                    await message.edit(embed=embed)
                    return
            else:  # Restore from the cloud
                success = await restore_from_cloud()

            if success:
                embed = discord.Embed(
                    title="✅ **Restore Complete**",
                    description=(
                        "```fix\n◆ DATABASE RESTORATION SUCCESSFUL ◆\n```\n"
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
                    "```diff\n- RESTORATION FAILED\n```\n💀 *Failed to restore from backup. Check logs for details.*",
                    color=0xFF0000)

        else:
            embed = discord.Embed(
                title="❌ **Restore Cancelled**",
                description=
                "```css\n[RESTORATION CANCELLED]\n```\n🛡️ *Your current database remains unchanged.*",
                color=0x808080)

    except asyncio.TimeoutError:
        embed = discord.Embed(
            title="⏰ **Timeout**",
            description=
            "```css\n[RESTORATION TIMEOUT]\n```\n🕐 *No response received. Current database remains unchanged.*",
            color=0x808080)

    await message.edit(embed=embed)

@bot.command()
@safe_command_wrapper
@cooldown_check('apistatus')
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
            "``````\n🔧 *Real-time API health monitoring...*",
            color=0x00FF7F if api_percentage < 80 else
            0xFFB347 if api_percentage < 95 else 0xFF0000)
        embed.add_field(
            name="🌐 **Discord API Usage**",
            value=
            "``````",
            inline=False)
        embed.add_field(
            name="⏰ **Command Cooldowns**",
            value=
            f"```yaml\nActive Cooldowns: {active_cooldowns}\n```",
            inline=True)
        if recent_commands:
            top_commands = sorted(recent_commands.items(),
                                  key=lambda x: x[1],
                                  reverse=True)[:5]
            command_list = "\n".join(
                [f"{cmd}: {count}" for cmd, count in top_commands])
            embed.add_field(name="📈 **Popular Commands (1h)**",
                            value=f"```yaml\n{command_list}\n```",
                            inline=True)
        embed.set_footer(
            text="🔄 Updates every 5 minutes • API monitoring active",
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        if error:
            result, err = await light_safe_api_call(ctx.send, f"❌ Error displaying API status: {error}")
    except Exception as e:
        logger.error(f"❌ API status command error: {e}")
        result, err = await light_safe_api_call(ctx.send, "❌ Failed to retrieve API status.")

@bot.command()
@safe_command_wrapper
@cooldown_check('backupstatus')
async def backupstatus(ctx):
    """Check backup status and list available backups (GitHub + Local)"""
    embed = discord.Embed(
        title="📊 **COSMIC BACKUP STATUS** 📊",
        description=
        "``````\n💾 *Examining the preservation of cosmic data...*",
        color=0x4169E1)
    try:
        # GitHub backup status
        github_backups = []
        if github_backup:
            success, github_files = github_backup.list_github_backups()
            if success and github_files:
                github_backups = github_files[:5]  # Show latest 5
                _latest_github = github_files[0]
                embed.add_field(
                    name="☁️ **GitHub Cloud Storage**",
                    value=
                    "``````",
                    inline=False)
                # List GitHub backups
                _github_list = "\n".join(
                    [f"☁️ {backup['name']}" for backup in github_backups])
                embed.add_field(name="📋 **Recent GitHub Backups**",
                                value="``````",
                                inline=True)
            else:
                embed.add_field(
                    name="☁️ **GitHub Cloud Storage**",
                    value="``````",
                    inline=False)
        else:
            embed.add_field(
                name="☁️ **GitHub Cloud Storage**",
                value=
                "``````",
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
                _latest_time = datetime.datetime.fromtimestamp(
                    os.path.getmtime(latest_path))
                _total_size = sum(
                    os.path.getsize(os.path.join("backups", f))
                    for f in backup_files)
                embed.add_field(
                    name="💾 **Local Storage**",
                    value=
                    "``````",
                    inline=True)
                # List local backups
                recent_local = backup_files[:5]
                _local_list = "\n".join(
                    [f"💾 {backup}" for backup in recent_local])
                embed.add_field(name="📋 **Recent Local Backups**",
                                value="``````",
                                inline=True)
            else:
                embed.add_field(name="💾 **Local Storage**",
                                value="``````",
                                inline=True)
        else:
            embed.add_field(name="💾 **Local Storage**",
                            value="``````",
                            inline=True)

        # Current database info
        if os.path.exists(DB_FILE):
            _current_size = os.path.getsize(DB_FILE)
            _current_time = datetime.datetime.fromtimestamp(
                os.path.getmtime(DB_FILE))
            embed.add_field(
                name="🗄️ **Current Database**",
                value=
                "``````",
                inline=False)
    except Exception as e:
        logger.error(f"❌ Backup status error: {e}")
        embed.add_field(
            name="❌ **Error**",
            value=
            f"``````\n💀 *Failed to retrieve backup status: {str(e)[:100]}...*",
            inline=False)

    embed.set_footer(text="💫 Regular backups ensure cosmic data preservation",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    result, error = await light_safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('ssbal')
async def ssbal(ctx, member: Optional[discord.Member] = None):
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
        f"``````\n{tier} • *The crystallized power of ages...*",
        color=tier_color)
    embed.add_field(name="💎 **SPIRIT STONES**",
        value="``````",  # Add actual balance
        inline=True)
    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.set_footer(
        text=f"💫 Wealth transcends mortal understanding • ID: {user.id}")
    result, error = await light_safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('spbal')
async def spbal(ctx, member: Optional[discord.Member] = None):
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
        f"``````\n{energy_tier} • *Raw energy flows through your essence...*",
        color=energy_color)
    embed.add_field(name="⚡ **SPIRIT POINTS**",
                    value="``````",
                    inline=True)
    embed.add_field(
        name="🔋 **ENERGY FLOW**",
        value=
        "``````",
        inline=False)
    embed.set_thumbnail(url=user.avatar.url if user.avatar else None)
    embed.set_footer(
        text=f"🌊 Energy is the source of all creation • {user.display_name}")
    result, error = await light_safe_api_call(ctx.send, embed=embed)
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
        except ValueError:
            embed = discord.Embed(
                title="❌ **INVALID INPUT**",
                description=
                "``````",
                color=0xFF0000)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return
    if exchange_amount <= 0 or exchange_amount > user_data.get("sp", 0):
        embed = discord.Embed(
            title="🚫 **INSUFFICIENT ENERGY**",
            description=
            "``````\n💔 *Your spiritual energy reserves are inadequate for this conversion...*",
            color=0xFF4500)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    # Update balances
    old_sp = user_data.get('sp', 0)
    old_balance = user_data.get('balance', 0)
    new_sp = old_sp - exchange_amount
    new_balance = old_balance + exchange_amount
    await update_user_data(user_id, balance=new_balance, sp=new_sp)
    # Log transaction
    log_transaction(user_id, "exchange", exchange_amount, old_balance,
                    new_balance, f"Exchanged {exchange_amount} SP to SS")
    embed = discord.Embed(
        title="🔄 **ENERGY TRANSMUTATION COMPLETE** 🔄",
        description=
        "``````\n✨ *Energy crystallizes into eternal stone...*",
        color=0x9932CC)
    embed.add_field(
        name="⚗️ **CONVERSION RESULT**",
        value=
        "``````",
        inline=False)
    embed.add_field(
        name="📊 **UPDATED RESERVES**",
        value=
        "``````",
        inline=False)
    embed.set_footer(
        text="⚡ Perfect 1:1 conversion rate achieved",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    result, error = await safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('coinflip')
async def coinflip(ctx, guess: str, amount: str):
    now = datetime.datetime.now(timezone.utc)
    user_id = str(ctx.author.id)
    guess = guess.lower()
    if guess not in ["heads", "tails"]:
        embed = discord.Embed(
            title="⚠️ **INVALID PREDICTION**",
            description=
            "``````\n🎯 *Choose your fate wisely, mortal...*",
            color=0xFF6347)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    user_data = get_user_data(user_id)
    sp = user_data.get("sp", 0)
    if user_id in last_gamble_times and (
            now - last_gamble_times[user_id]).total_seconds() < 60:
        embed = discord.Embed(
            title="⏳ **COSMIC COOLDOWN**",
            description=
            "``````\n🌌 *The universe needs time to align the cosmic forces...*",
            color=0x4682B4)
        embed.set_footer(
            text="⚡ Gambling cooldown: 60 seconds between attempts")
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    validated_amount = validate_amount(amount, 20000)
    if validated_amount is None:
        embed = discord.Embed(
            title="💸 **INVALID WAGER**",
            description=
            "``````",
            color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    if validated_amount == "all":
        bet = min(sp, 20000)
    else:
        bet = validated_amount
    if bet <= 0 or bet > 20000 or bet > sp:
        embed = discord.Embed(
            title="🚫 **WAGER REJECTED**",
            description=
            "``````\n💰 *Maximum bet: 20,000 SP*\n⚡ *Current SP: {:,}*"
            .format(sp),
            color=0xFF4500)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    flip = random.choice(["heads", "tails"])
    won = (flip == guess)
    if won:
        new_sp = sp + bet
        await update_user_data(user_id, sp=new_sp)
        update_monthly_stats(user_id, win_amount=bet)
        log_transaction(user_id, "gambling_win", bet, sp, new_sp,
                        f"Coinflip win: {flip}")
        embed = discord.Embed(
            title="🎉 **FATE SMILES UPON YOU** 🎉",
            description=
            f"``````\n🪙 *The coin reveals: **{flip.upper()}***\n✨ *Fortune flows through your spirit...*",
            color=0x00FF00)
        embed.add_field(name="🏆 **VICTORY SPOILS**",
                        value="``````",
                        inline=True)
        embed.add_field(name="💰 **NEW BALANCE**",
                        value="``````",
                        inline=True)
    else:
        new_sp = sp - bet
        await update_user_data(user_id, sp=new_sp)
        update_monthly_stats(user_id, loss_amount=bet)
        log_transaction(user_id, "gambling_loss", -bet, sp, new_sp,
                        f"Coinflip loss: {flip}")
        embed = discord.Embed(
            title="💀 **THE VOID CLAIMS ITS DUE** 💀",
            description=
            f"``````\n🪙 *The coin reveals: **{flip.upper()}***\n🌑 *Your greed feeds the endless darkness...*",
            color=0xFF0000)
        embed.add_field(name="💸 **LOSSES SUFFERED**",
                        value="``````",
                        inline=True)
        embed.add_field(name="💔 **REMAINING BALANCE**",
                        value="``````",
                        inline=True)
    last_gamble_times[user_id] = now
    embed.add_field(
        name="🎯 **PREDICTION vs REALITY**",
        value=
        "``````",
        inline=False)
    embed.set_footer(
        text="🎰 The cosmic coin never lies • Gamble responsibly",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    result, error = await safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('shop')
async def shop(ctx):
    embed = discord.Embed(
        title="🏪 **GU CHANG'S MYSTICAL EMPORIUM** 🏪",
        description=
        "``````\n✨ *Only the worthy may claim these treasures of power...*",
        color=0xFF6B35)
    for index, (item, details) in enumerate(SHOP_ITEMS.items(),
                                            start=1):  # Changed here
        item_name = item.replace('_', ' ').title()
        embed.add_field(
            name=
            f"{index}. {details['desc'].split()[0]} **{item_name.upper()}**",  # Changed here
            value=
            f"``````\n*{details['desc'][2:]}*",
            inline=True)
    embed.add_field(
        name="💳 **PURCHASE INSTRUCTIONS**",
        value=
        "``````\n🛒 *Use the command above to claim your artifact*",
        inline=False)
    embed.set_footer(text="⚡ Spiritual artifacts enhance your cosmic journey",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    result, error = await light_safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('buy')
async def buy(ctx, item_number: int):
    user_id = str(ctx.author.id)
    user_data = get_user_data(user_id)
    item_list = list(SHOP_ITEMS.keys())  # Extract item list again
    if item_number < 1 or item_number > len(
            item_list):  # Check if the number is valid
        available_items = ", ".join(
            [str(i + 1) for i in range(len(item_list))])
        embed = discord.Embed(
            title="❌ **ARTIFACT NOT FOUND**",
            description=
            f"``````\n🔍 **Available Item Numbers:** {available_items}",
            color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    item = item_list[item_number -
                     1]  # The item corresponds to the user's input
    item_data = SHOP_ITEMS[item]
    balance = user_data["balance"]
    if balance < item_data["price"]:
        shortage = item_data["price"] - balance
        embed = discord.Embed(
            title="💸 **INSUFFICIENT SPIRIT STONES**",
            description=
            f"``````\n💰 **Required:** {item_data['price']:,} SS\n💎 **You Have:** {balance:,} SS\n📉 **Shortage:** {shortage:,} SS\n\n⚡ *Gather more power before returning...*",
            color=0xFF6347)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    if item == "nickname_lock":
        add_nickname_lock(user_id)
        effect = "🔒 **IDENTITY SEALED** - *Your name is now protected from all changes*"
        effect_color = 0x4169E1
    elif item == "temp_admin":
        expiry = datetime.datetime.now(
            timezone.utc) + datetime.timedelta(hours=1)
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
        effect = "🃏 **NAME CHANGE CARD ACQUIRED** - *Use !usename @target 'new nickname' to activate*"
        effect_color = 0xFF1493
    else:
        effect = "✨ **ARTIFACT BONDED** - *The power is now yours to wield*"
        effect_color = 0x9932CC
    # Update balance
    new_balance = balance - item_data["price"]
    await update_user_data(user_id, balance=new_balance)
    # Log transaction
    log_transaction(user_id, "shop_purchase", -item_data["price"], balance,
                    new_balance, f"Purchased {item}")
    embed = discord.Embed(
        title="✅ **TRANSACTION COMPLETED** ✅",
        description=
        f"``````\n{effect}",
        color=effect_color)
    embed.add_field(name="🎁 **ARTIFACT CLAIMED**",
                    value="``````",
                    inline=True)
    embed.add_field(
        name="💰 **COST PAID**",
        value="``````",
        inline=True)
    embed.add_field(name="💎 **REMAINING BALANCE**",
                    value="``````",
                    inline=True)
    embed.set_thumbnail(
        url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.set_footer(text="🌟 Power has been transferred • Use it wisely",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    result, error = await safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@safe_command_wrapper
async def givess(ctx, member: discord.Member, amount: int):
    """Give Spirit Stones to another user (Admin only) - FIXED"""
    try:
        # Check if user has administrator permissions
        if not ctx.author.guild_permissions.administrator:
            embed = discord.Embed(
                title="🚫 **ACCESS DENIED** 🚫",
                description=
                "``````\n⚡ *Only those with divine authority may grant such power...*",
                color=0xFF0000)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return
        if amount <= 0:
            embed = discord.Embed(
                title="❌ **INVALID AMOUNT** ❌",
                description=
                "``````",
                color=0xFF4500)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return
        receiver_id = str(member.id)
        receiver_data = get_user_data(receiver_id)  # Sync function
        old_balance = receiver_data.get("balance", 0)
        new_balance = old_balance + amount
        # FIXED: Use await for async function
        success = await update_user_data(receiver_id, balance=new_balance)
        if not success:
            embed = discord.Embed(
                title="❌ **DATABASE ERROR** ❌",
                description=
                "``````",
                color=0xFF0000)
            result, error = await light_safe_api_call(ctx.send, embed=embed)
            return
        # Log transaction
        log_transaction(receiver_id, "admin_grant", amount, old_balance,
                        new_balance,
                        f"Admin grant by {ctx.author.display_name}")
        embed = discord.Embed(
            title="✨ **DIVINE BLESSING GRANTED** ✨",
            description=
            "``````\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*🌟 The cosmic treasury flows with divine will 🌟*",
            color=0x00FF7F)
        embed.add_field(
            name="👑 **ADMINISTRATOR**",
            value=
            "``````",
            inline=True)
        embed.add_field(
            name="🎯 **RECIPIENT**",
            value="``````",
            inline=True)
        embed.add_field(name="💰 **AMOUNT GRANTED**",
                        value="``````",
                        inline=False)
        embed.add_field(name="💎 **NEW BALANCE**",
                        value="``````",
                        inline=False)
        embed.set_footer(
            text="⚡ Divine Administrative System ⚡",
            icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
        embed.timestamp = datetime.datetime.now(timezone.utc)
        result, error = await safe_api_call(ctx.send, embed=embed)
        if error:
            logger.error(f"❌ Failed to send message: {error}")
    except Exception as e:
        logger.error(f"❌ Critical error in givess: {e}")
        import traceback
        traceback.print_exc()
        error_embed = discord.Embed(
            title="❌ **SYSTEM ERROR** ❌",
            description=
            "``````",
            color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=error_embed)


@bot.command()
@safe_command_wrapper
async def takess(ctx, member: discord.Member, amount: int):
    # Check if user has administrator permissions
    if not ctx.author.guild_permissions.administrator:
        embed = discord.Embed(
            title="🚫 **ACCESS DENIED** 🚫",
            description=
            "``````\n⚡ *Only those with divine authority may wield such power...*",
            color=0xFF0000)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    if amount <= 0:
        embed = discord.Embed(
            title="❌ **INVALID AMOUNT** ❌",
            description=
            "``````",
            color=0xFF4500)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    target_id = str(member.id)
    target_data = get_user_data(target_id)
    current_balance = target_data["balance"]
    if amount > current_balance:
        embed = discord.Embed(
            title="⚠️ **INSUFFICIENT FUNDS** ⚠️",
            description=
            f"``````\n💰 **Target Balance:** {current_balance:,} SS\n📉 **Requested Removal:** {amount:,} SS\n\n⚡ *The void cannot claim what does not exist...*",
            color=0xFF6347)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    new_balance = current_balance - amount
    await update_user_data(target_id, balance=new_balance)
    # Log transaction
    log_transaction(target_id, "admin_remove", -amount, current_balance,
                    new_balance, f"Admin removal by {ctx.author.display_name}")
    embed = discord.Embed(
        title="💀 **DIVINE JUDGMENT EXECUTED** 💀",
        description=
        "``````\n━━━━━━━━━━━━━━━━━━━━━━━━━━\n*🌑 The cosmic balance demands sacrifice 🌑*",
        color=0xFF1744)
    embed.add_field(
        name="👑 **ADMINISTRATOR**",
        value="``````",
        inline=True)
    embed.add_field(
        name="🎯 **TARGET**",
        value="``````",
        inline=True)
    embed.add_field(name="💸 **AMOUNT REMOVED**",
                    value="``````",
                    inline=False)
    embed.add_field(name="💔 **REMAINING BALANCE**",
                    value="``````",
                    inline=False)
    embed.set_footer(
        text="⚡ Divine Administrative System ⚡",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    embed.timestamp = datetime.datetime.now(timezone.utc)
    result, error = await safe_api_call(ctx.send, embed=embed)
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
        "``````\n💎 *The most powerful cultivators in the realm...*",
        color=0xFFD700)
    medal_emojis = [
        "🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"
    ]
    leaderboard_text = ""
    for i, (user_id, balance) in enumerate(leaderboard):
        try:
            user = bot.get_user(int(user_id))
            username = user.display_name if user else "Unknown User"
            leaderboard_text += f"{medal_emojis[i]} **{username}** - `{balance:,}` SS\n"
        except Exception:
            continue
    if leaderboard_text:
        embed.add_field(name="💰 **TOP CULTIVATORS**",
                        value=leaderboard_text,
                        inline=False)
    else:
        embed.add_field(
            name="💰 **TOP CULTIVATORS**",
            value=
            "``````",
            inline=False)
    embed.set_footer(text="⚡ Power rankings updated in real-time",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    result, error = await light_safe_api_call(ctx.send, embed=embed)
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
        "``````\n⚡ *The combined power of the top cultivators flows through the realm...*",
        color=0x00FF7F)
    # Show top 10 SP holders
    medal_emojis = [
        "🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"
    ]
    leaderboard_text = ""
    for i, (user_id, sp) in enumerate(sp_leaderboard):
        try:
            user = bot.get_user(int(user_id))
            username = user.display_name if user else "Unknown User"
            leaderboard_text += f"{medal_emojis[i]} **{username}** - `{sp:,}` SP\n"
        except Exception:
            continue
    if leaderboard_text:
        embed.add_field(name="⚡ **TOP ENERGY MASTERS**",
                        value=leaderboard_text,
                        inline=False)
    embed.add_field(
        name="🌟 **COSMIC ENERGY POOL**",
        value=
        "``````\n*The accumulated power of the realm's elite...*",
        inline=False)
    embed.add_field(
        name="🔮 **FORTUNE INSIGHT**",
        value=
        "``````",
        inline=False)
    embed.set_footer(
        text="🍀 The universe reveals its secrets to those who seek",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
    result, error = await light_safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('unlucky')
async def unlucky(ctx):
    """Shows top 10 users who lost the most SP this month"""
    current_month = datetime.datetime.now(timezone.utc).strftime("%Y-%m")
    top_losers = get_top_losers(current_month, 10)
    if not top_losers:
        embed = discord.Embed(
            title="🌟 **BLESSED MONTH** 🌟",
            description=
            "``````\n✨ *The void has been merciful this month...*",
            color=0x32CD32)
        embed.add_field(
            name="🛡️ **COSMIC PROTECTION**",
            value=
            "``````",
            inline=False)
        embed.set_footer(
            text=
            f"📅 {datetime.datetime.now(timezone.utc).strftime('%B %Y')} • Keep the luck flowing!",
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
        result, error = await light_safe_api_call(ctx.send, embed=embed)
        return
    total_losses = sum(losses for _, losses in top_losers)
    embed = discord.Embed(
        title="💀 **HALL OF COSMIC MISFORTUNE** 💀",
        description=
        "``````\n🌑 *Those who have fed the darkness most this month...*",
        color=0x8B0000)
    # Show top 10 losers
    skull_emojis = ["💀", "☠️", "👹", "😈", "🔥", "⚡", "💸", "😭", "😱", "🆘"]
    leaderboard_text = ""
    for i, (user_id, losses) in enumerate(top_losers):
        try:
            user = bot.get_user(int(user_id))
            username = user.display_name if user else "Unknown User"
            leaderboard_text += f"{skull_emojis[i]} **{username}** - `{losses:,}` SP Lost\n"
        except Exception:
            continue
    if leaderboard_text:
        embed.add_field(name="💸 **VOID'S FAVORED VICTIMS**",
                        value=leaderboard_text,
                        inline=False)
    embed.add_field(
        name="🌑 **TOTAL DEVASTATION**",
        value=
        "``````\n*The accumulated suffering feeds the endless void...*",
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
        f"``````\n{void_status}",
        inline=False)
    embed.add_field(
        name="⚠️ **COSMIC WARNING**",
        value=
        "``````",
        inline=False)
    embed.set_footer(
        text=
        f"💀 Monthly Misfortune Report • {datetime.datetime.now(timezone.utc).strftime('%B %Y')}",
        icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    result, error = await light_safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

@bot.command()
@commands.has_permissions(administrator=True)
async def emergency_repair(ctx):
    """Emergency database repair command"""
    embed = discord.Embed(
        title="🚨 **EMERGENCY DATABASE REPAIR** 🚨",
        description=
        "``````\n⚠️ *Attempting to repair database corruption...*",
        color=0xFF6B00)

    result, error = await safe_api_call(ctx.send, embed=embed)
    if error or result is None:
        logger.error(f"❌ Failed to send emergency repair message: {error}")
        return
    message = result

    try:
        # Check database integrity
        if await check_database_integrity():
            embed.description = "``````"
            embed.color = 0x00FF00
        else:
            embed.description = "``````"
            result, error = await safe_api_call(message.edit, embed=embed)
            if error:
                logger.error(f"❌ Failed to edit emergency repair message: {error}")

            # Attempt repair
            if await repair_database():
                embed.description = "``````"
                embed.color = 0x00FF00
            else:
                # Try backup restore
                embed.description = "``````"
                result, error = await safe_api_call(message.edit, embed=embed)
                if error:
                    logger.error(f"❌ Failed to edit emergency repair message: {error}")

                if await restore_from_latest_backup():
                    embed.description = "``````"
                    embed.color = 0xFFAA00
                else:
                    embed.description = "``````"
                    embed.color = 0xFF0000

        result, error = await safe_api_call(message.edit, embed=embed)
        if error:
            logger.error(f"❌ Failed to edit final emergency repair message: {error}")

    except Exception as e:
        embed.description = "``````"
        embed.color = 0xFF0000
        result, error = await safe_api_call(message.edit, embed=embed)
        if error:
            logger.error(f"❌ Failed to edit emergency repair error message: {error}")

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
        result, error = await light_safe_api_call(ctx.send, "Available error types: generic, permission, argument")
        if error:
            logger.error(f"❌ Failed to send errortest message: {error}")

@bot.command()
@safe_command_wrapper
@cooldown_check('lose')
async def lose(ctx, member: Optional[discord.Member] = None):
    """Shows SP lost this month for the user or tagged member"""
    user = member or ctx.author
    user_id = str(user.id)
    # Get current month stats
    current_month = datetime.datetime.now(timezone.utc).strftime("%Y-%m")
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
        f"``````\n{loss_tier} • *The darkness remembers every sacrifice...*",
        color=tier_color)
    embed.add_field(name="💀 **LOSSES TO THE VOID**",
                    value="``````",
                    inline=True)
    embed.add_field(name="🏆 **GAINS FROM FORTUNE**",
                    value="``````",
                    inline=True)
    embed.add_field(
        name="⚖️ **NET RESULT**",
        value=
        "``````",
        inline=False)
    # Loss ratio calculation
    total_gambled = wins + losses
    if total_gambled > 0:
        loss_percentage = (losses / total_gambled) * 100
        embed.add_field(
            name="📊 **GAMBLING STATISTICS**",
            value=
            "``````",
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
        f"📅 {datetime.datetime.now(timezone.utc).strftime('%B %Y')} • Gamble responsibly",
        icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    result, error = await light_safe_api_call(ctx.send, embed=embed)
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
        "``````\n⚡ *Master these commands to ascend in power...*",
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
  -  nickname_lock (5,000 SS)
  -  temp_admin (25,000 SS)
  -  hmw_role (50,000 SS)
  -  name_change_card (10,000 SS)
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
                    value="""```
Standard: 300 SP
Booster: 350 SP (+50)
HMW: 350 SP (+50)
Admin: 400 SP (+100)
Streak Bonus: 2x reward at 5-day streak
```""",
                    inline=False)
    embed.set_footer(text="🌟 Master the commands, master your destiny • !help",
                     icon_url=ctx.guild.icon.url if ctx.guild.icon else None)
    result, error = await light_safe_api_call(ctx.send, embed=embed)
    if error:
        logger.error(f"❌ Failed to send message: {error}")

# ==== Flask Thread ====
def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# == Main Execution ==
if __name__ == "__main__":
    # Start Flask in a separate thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask server started")

    # Run the bot with smart restart capability
    max_restart_attempts = 10
    restart_delay = 30

    for restart_attempt in range(max_restart_attempts):
        try:
            logger.info(
                f"🔄 Bot startup attempt {restart_attempt + 1}/{max_restart_attempts}"
            )
            asyncio.run(main())
            logger.info("✅ Bot shut down normally")
            break

        except KeyboardInterrupt:
            logger.info("🛑 Manual shutdown requested")
            break

        except Exception as e:
            error_msg = str(e).lower()

            # Don't restart for authentication errors
            if any(auth_error in error_msg for auth_error in 
                   ['401', 'unauthorized', 'invalid token', 'login failure']):
                logger.error("❌ Authentication error - check your Discord bot token")
                logger.error("🔧 Fix your token in environment variables and redeploy")
                break

            # Don't restart for rate limiting - wait longer
            elif '429' in error_msg or 'rate limit' in error_msg:
                if restart_attempt < max_restart_attempts - 1:
                    rate_limit_delay = 300  # 5 minutes for rate limiting
                    logger.error(f"❌ Rate limited - waiting {rate_limit_delay}s before retry")
                    time.sleep(rate_limit_delay)
                else:
                    logger.error("❌ Persistent rate limiting - stopping bot")
                    break

            # Restart for other errors (network, temporary issues)
            else:
                logger.error(f"❌ Bot crashed: {e}")
                if restart_attempt < max_restart_attempts - 1:
                    logger.info(f"🔄 Restarting in {restart_delay} seconds...")
                    time.sleep(restart_delay)
                    restart_delay = min(restart_delay * 1.5, 300)
                else:
                    logger.error("❌ Max restart attempts exceeded")
