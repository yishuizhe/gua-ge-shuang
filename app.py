#!/usr/bin/env python3
import ast
import hashlib
import json
import operator
import os
import random
import re
import secrets
import sqlite3
import time
from collections import defaultdict
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote_plus

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SCRATCH_DB", ROOT / "data" / "scratch_game.db"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8089"))
APP_VERSION = "2.5.0"

# ---------- admin credentials ----------
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", secrets.token_urlsafe(16))
_admin_salt = secrets.token_hex(16)
_admin_hash = hashlib.pbkdf2_hmac("sha256", ADMIN_PASS.encode(), _admin_salt.encode(), 200_000).hex()
_admin_sessions = {}  # token -> expiry timestamp
if not os.environ.get("ADMIN_PASS"):
    print(f"[!] ADMIN_PASS not set, using random: {ADMIN_PASS}")

# ---------- default payout config ----------
DEFAULT_PAYOUT = {
    "lose": 0.614,
    "break_even": 0.23,
    "small": 0.10,
    "medium": 0.04,
    "big": 0.009,
    "super": 0.0008,   # 1万
    "diamond": 0.00015,  # 10万
    "legend": 0.00005,   # 100万
    "pity_max": 0.04,
    "pity_step": 0.002,
}

# ---------- game types ----------
TYPES = {
    "xiangfeng": {"name": "喜相逢", "icon": "🏮", "cost": 10, "kind": "match"},
    "seven": {"name": "数字 7", "icon": "7️⃣", "cost": 15, "kind": "numbers"},
    "jinyu": {"name": "金玉满堂", "icon": "🧧", "cost": 20, "kind": "triple"},
    "ten": {"name": "好运十倍", "icon": "⚡", "cost": 30, "kind": "multi"},
    "koi": {"name": "锦鲤驾到", "icon": "🐟", "cost": 50, "kind": "koi"},
    "blackjack": {"name": "21点", "icon": "🃏", "cost": 20, "kind": "blackjack"},
    "ssq": {"name": "双色球", "icon": "🔴", "cost": 2, "kind": "ssq"},
    "baccarat": {"name": "百家乐", "icon": "🎴", "cost": 25, "kind": "baccarat"},
    "dice": {"name": "猜大小", "icon": "🎲", "cost": 10, "kind": "dice"},
    "twentyfour": {"name": "24点挑战", "icon": "🧠", "cost": 10, "kind": "twentyfour"},
    "pusher": {"name": "推币机", "icon": "🪙", "cost": 10, "kind": "pusher"},
    "claw": {"name": "抓娃娃机", "icon": "🕹️", "cost": 20, "kind": "claw"},
    "slots": {"name": "水果机", "icon": "🍒", "cost": 5, "kind": "slots"},
    "pinball": {"name": "弹珠台", "icon": "🔮", "cost": 8, "kind": "pinball"},
    "wheel": {"name": "幸运转盘", "icon": "🎡", "cost": 10, "kind": "wheel"},
    "redpacket": {"name": "红包雨", "icon": "🧧", "cost": 12, "kind": "redpacket"},
}

SUPER_PRIZES = [1000000, 100000, 10000]  # legend, diamond, super

ACHIEVEMENTS = [
    {
        "id": "first_ticket",
        "name": "初来好运街",
        "icon": "🎟️",
        "desc": "完成第 1 张票",
        "check": lambda row, seen: row["played"] >= 1,
    },
    {
        "id": "first_win",
        "name": "开门见喜",
        "icon": "🏅",
        "desc": "赢得任意一张票",
        "check": lambda row, seen: row["wins"] >= 1,
    },
    {
        "id": "collector",
        "name": "玩法收藏家",
        "icon": "🧩",
        "desc": "体验 5 种玩法",
        "check": lambda row, seen: len(seen) >= 5,
    },
    {
        "id": "arcade_master",
        "name": "街机熟手",
        "icon": "🕹️",
        "desc": "体验全部玩法",
        "check": lambda row, seen: len(seen) >= len(TYPES),
    },
    {
        "id": "hot_streak",
        "name": "手气连红",
        "icon": "🔥",
        "desc": "达成 3 连中",
        "check": lambda row, seen: row["streak"] >= 3,
    },
    {
        "id": "big_winner",
        "name": "大奖猎手",
        "icon": "💎",
        "desc": "单票赢得 1000+ 爽币",
        "check": lambda row, seen: row["best"] >= 1000,
    },
    {
        "id": "level_five",
        "name": "好运常客",
        "icon": "⭐",
        "desc": "达到 5 级",
        "check": lambda row, seen: row["level"] >= 5,
    },
    {
        "id": "coin_stack",
        "name": "小金库",
        "icon": "🪙",
        "desc": "持有 1000+ 爽币",
        "check": lambda row, seen: row["coins"] >= 1000,
    },
]


def db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_id TEXT NOT NULL UNIQUE,
                nickname TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                coins INTEGER NOT NULL DEFAULT 200,
                xp INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                played INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                best INTEGER NOT NULL DEFAULT 0,
                streak INTEGER NOT NULL DEFAULT 0,
                seen TEXT NOT NULL DEFAULT '[]',
                last_ticket_at REAL NOT NULL DEFAULT 0,
                last_gift_day TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                player_id INTEGER NOT NULL REFERENCES players(id),
                ticket_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                win INTEGER NOT NULL,
                settled INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                settled_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id INTEGER NOT NULL REFERENCES players(id),
                ticket_type TEXT NOT NULL,
                win INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_records_player ON records(player_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_players_rank ON players(level DESC, xp DESC, best DESC);
            """
        )
        # migrations for existing databases
        try:
            conn.execute("ALTER TABLE players ADD COLUMN last_ticket_at REAL NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE players ADD COLUMN last_gift_day TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        # ensure default config exists
        for k, v in DEFAULT_PAYOUT.items():
            conn.execute(
                "INSERT OR IGNORE INTO admin_config(key, value) VALUES(?, ?)",
                (k, str(v)),
            )


# ---------- config helpers ----------
def get_config():
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM admin_config").fetchall()
    cfg = {}
    for r in rows:
        v = r["value"]
        if v is None or v == "":
            continue
        try:
            cfg[r["key"]] = float(v)
        except ValueError:
            cfg[r["key"]] = v
    return cfg


def set_config(updates):
    allowed = set(DEFAULT_PAYOUT)
    allowed.update(
        f"game_{game_id}_{suffix}"
        for game_id in TYPES
        for suffix in ("enabled", "cost", "winrate")
    )
    allowed.update(k for k in updates if k.startswith("user_GS-") and k.endswith("_rate"))
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"未知配置项: {', '.join(sorted(unknown))}")

    normalized = {}
    for key, value in updates.items():
        if value is None or (isinstance(value, str) and value.strip() == ""):
            normalized[key] = None
            continue
        if key.endswith("_enabled"):
            normalized[key] = "1" if str(value) in ("1", "true", "True") else "0"
        elif key.endswith("_cost"):
            normalized[key] = str(max(1, min(10000, int(float(value)))))
        elif key.endswith("_winrate"):
            normalized[key] = str(max(0.0, min(1.0, float(value))))
        elif key.startswith("user_") and key.endswith("_rate"):
            normalized[key] = str(max(0.0, min(10.0, float(value))))
        else:
            normalized[key] = str(max(0.0, min(1.0, float(value))))

    probability_keys = ("lose", "break_even", "small", "medium", "big", "super", "diamond", "legend")
    prospective = get_config()
    prospective.update({k: float(v) for k, v in normalized.items() if k in probability_keys and v is not None})
    total = sum(float(prospective.get(k, DEFAULT_PAYOUT[k])) for k in probability_keys)
    if total > 1.000001:
        raise ValueError(f"奖池概率合计不能超过 100%，当前为 {total * 100:.3f}%")

    with db() as conn:
        for k, v in normalized.items():
            if v is None:
                conn.execute("DELETE FROM admin_config WHERE key=?", (k,))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO admin_config(key, value) VALUES(?, ?)",
                    (k, str(v)),
                )


# ---------- auth ----------
def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def achievements_for(row, seen):
    unlocked = []
    for achievement in ACHIEVEMENTS:
        if achievement["check"](row, seen):
            unlocked.append({
                "id": achievement["id"],
                "name": achievement["name"],
                "icon": achievement["icon"],
                "desc": achievement["desc"],
            })
    return unlocked


def player_dict(row, include_private=True):
    raw_seen = json.loads(row["seen"] or "[]")
    valid_seen = [s for s in raw_seen if s in TYPES]
    achievements = achievements_for(row, valid_seen)
    data = {
        "playerId": row["public_id"],
        "nickname": row["nickname"],
        "coins": row["coins"],
        "xp": row["xp"],
        "level": row["level"],
        "played": row["played"],
        "wins": row["wins"],
        "best": row["best"],
        "streak": row["streak"],
        "seen": valid_seen,
        "totalTypes": len(TYPES),
        "dailyGiftAvailable": row["last_gift_day"] != china_day(),
        "achievements": achievements,
        "achievementCount": len(achievements),
        "totalAchievements": len(ACHIEVEMENTS),
    }
    if not include_private:
        return {
            "playerId": data["playerId"],
            "nickname": data["nickname"],
            "level": data["level"],
            "xp": data["xp"],
            "played": data["played"],
            "wins": data["wins"],
            "best": data["best"],
            "coins": data["coins"],
        }
    return data


# ---------- paginated records ----------
def records_for(conn, player_id, page=1, limit=10):
    offset = (page - 1) * limit
    rows = conn.execute(
        "SELECT ticket_type, win, created_at FROM records WHERE player_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
        (player_id, limit, offset),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) FROM records WHERE player_id=?", (player_id,)
    ).fetchone()[0]
    return [
        {
            "icon": TYPES[row["ticket_type"]]["icon"],
            "name": TYPES[row["ticket_type"]]["name"],
            "win": row["win"],
            "time": time.strftime("%H:%M", time.gmtime(row["created_at"] + 8 * 3600)),
        }
        for row in rows
    ], total


# ---------- rate limiting ----------
RATE_IP = defaultdict(list)  # ip -> [timestamps]


def check_rate_limit(player_row, client_ip):
    """Returns (allowed, reason) tuple."""
    now = time.time()

    # per-player cooldown: 2 seconds between tickets
    last = player_row["last_ticket_at"]
    if last and (now - last) < 2.0:
        remaining = round(2.0 - (now - last), 1)
        return False, f"请稍候 {remaining} 秒后再购买"

    # per-IP rate limit: 30 requests per minute
    cutoff = now - 60
    RATE_IP[client_ip] = [t for t in RATE_IP.get(client_ip, []) if t > cutoff]
    if len(RATE_IP.get(client_ip, [])) >= 30:
        return False, "请求过于频繁，请稍后再试"

    RATE_IP.setdefault(client_ip, []).append(now)
    return True, ""


def china_day(timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    return time.strftime("%Y-%m-%d", time.gmtime(timestamp + 8 * 3600))


# ---------- 24-point expression validation ----------
ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
}


def _eval_ast(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in ALLOWED_OPS:
        return ALLOWED_OPS[type(node.op)](_eval_ast(node.left), _eval_ast(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in ALLOWED_OPS:
        return ALLOWED_OPS[type(node.op)](_eval_ast(node.operand))
    raise ValueError("不支持的算式")


def validate_24_answer(expr, numbers):
    expr = expr.strip()
    if not expr or not re.fullmatch(r"[\d\s+\-*/()]+", expr):
        return False, "只可使用牌面数字、括号和加减乘除"
    used = sorted(int(value) for value in re.findall(r"\d+", expr))
    expected = sorted(numbers)
    if used != expected:
        return False, f"必须恰好使用这四张牌：{expected}"
    try:
        result = _eval_ast(ast.parse(expr, mode="eval").body)
    except (SyntaxError, ValueError, ZeroDivisionError):
        return False, "算式无法计算，请检查括号和运算符"
    if abs(result - 24) > 0.001:
        return False, f"算式结果是 {result:g}，还不是 24"
    return True, "回答正确"


# ---------- payout engine ----------
def roll_payout(ticket_type, streak, player_public_id=None, cost_override=None):
    """Server-authoritative payout with bounded, normalized probabilities."""
    cfg = get_config()
    cost = int(cost_override if cost_override is not None else TYPES[ticket_type]["cost"])
    pity = min(cfg.get("pity_max", 0.04), streak * cfg.get("pity_step", 0.002))
    tier_names = ("break_even", "small", "medium", "big", "super", "diamond", "legend")
    tier_weights = [max(0.0, float(cfg.get(name, DEFAULT_PAYOUT[name]))) for name in tier_names]
    base_win_rate = sum(tier_weights)
    game_rate = cfg.get(f"game_{ticket_type}_winrate")
    win_rate = float(game_rate) if game_rate is not None else base_win_rate + pity
    user_mult = float(cfg.get(f"user_{player_public_id}_rate", 1.0)) if player_public_id else 1.0
    win_rate = max(0.0, min(0.95, win_rate * user_mult))

    if random.random() >= win_rate or base_win_rate <= 0:
        return 0, "未中奖"

    tier = random.choices(tier_names, weights=tier_weights, k=1)[0]
    payouts = {
        "break_even": (cost, "回本"),
        "small": (cost * 2, "小奖"),
        "medium": (cost * 5, "中奖"),
        "big": (cost * 10, "大奖"),
        "super": (10000, "超级大奖"),
        "diamond": (100000, "钻石大奖"),
        "legend": (1000000, "传说大奖"),
    }
    return payouts[tier]


# ---------- ticket generators ----------
def generate_ticket(ticket_type, streak, player_public_id=None, cost_override=None):
    kind = TYPES[ticket_type]["kind"]
    cost = int(cost_override if cost_override is not None else TYPES[ticket_type]["cost"])
    cells, winning = [], []

    if kind == "twentyfour":
        solvable_sets = [
            [1, 2, 3, 4], [1, 3, 5, 6], [2, 3, 4, 6], [3, 3, 8, 8],
            [1, 5, 5, 5], [2, 2, 5, 10], [2, 3, 3, 8], [1, 3, 4, 6],
            [2, 4, 6, 8], [3, 4, 5, 6], [4, 4, 7, 7], [2, 5, 7, 8],
        ]
        numbers = random.choice(solvable_sets)[:]
        random.shuffle(numbers)
        cards = [
            {"value": number, "display": "A" if number == 1 else str(number),
             "suit": random.choice(["♠", "♥", "♣", "♦"])}
            for number in numbers
        ]
        return {
            "mode": "twentyfour",
            "cards": cards,
            "_numbers": numbers,
            "resultText": "四张牌都要使用一次，用加减乘除凑出 24",
            "prizeTier": "技巧奖",
            "maxPrize": cost * 5,
        }, cost * 5

    if kind == "pusher":
        win, prize_tier = roll_payout(ticket_type, streak, player_public_id, cost)
        target_x = round(random.uniform(0.12, 0.88), 3)
        coins = [
            {
                "x": round(random.uniform(0.05, 0.95), 3),
                "y": round(random.uniform(0.08, 0.88), 3),
                "size": random.choice([0.8, 1.0, 1.2]),
            }
            for _ in range(random.randint(20, 32))
        ]
        return {
            "mode": "pusher",
            "coins": coins,
            "_targetX": target_x,
            "_tolerance": 0.16,
            "resultText": "瞄准币堆密集处投币，落点会影响结算",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, win

    if kind == "claw":
        win, prize_tier = roll_payout(ticket_type, streak, player_public_id, cost)
        toy_types = [
            ("🐼", "熊猫", 1.0), ("🦁", "狮子", 1.1), ("🐰", "兔子", 0.9),
            ("🐻", "小熊", 1.0), ("🐸", "青蛙", 0.85), ("🐱", "猫咪", 0.9),
            ("🦊", "狐狸", 0.95), ("🐨", "考拉", 0.9),
        ]
        chosen = random.sample(toy_types, 6)
        toys = [
            {
                "emoji": emoji,
                "name": name,
                "size": size,
                "x": round(0.1 + index * 0.16 + random.uniform(-0.015, 0.015), 3),
            }
            for index, (emoji, name, size) in enumerate(chosen)
        ]
        return {
            "mode": "claw",
            "toys": toys,
            "_winningSlot": random.randrange(len(toys)) if win else -1,
            "resultText": "移动机械爪瞄准娃娃，抓取结果由服务器结算",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, win

    if kind == "redpacket":
        win, prize_tier = roll_payout(ticket_type, streak, player_public_id, cost)
        packet_count = 12
        lucky_index = random.randrange(packet_count) if win else -1
        packets = [
            {
                "id": index,
                "x": round(0.08 + (index % 4) * 0.28 + random.uniform(-0.035, 0.035), 3),
                "delay": round((index // 4) * 0.22 + random.uniform(0, 0.16), 2),
                "speed": round(random.uniform(0.72, 1.15), 2),
                "size": random.choice([0.9, 1.0, 1.1, 1.2]),
                "label": random.choice(["福", "喜", "财", "旺", "顺"]),
            }
            for index in range(packet_count)
        ]
        random.shuffle(packets)
        return {
            "mode": "redpacket",
            "packets": packets,
            "_luckyIndex": lucky_index,
            "resultText": "红包雨落下，点中本局福袋即可中奖",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, win

    # ---- Blackjack (21点) ----
    if kind == "blackjack":
        deck = [1,2,3,4,5,6,7,8,9,10,10,10,10] * 4
        random.shuffle(deck)
        player_hand = [deck.pop(), deck.pop()]
        dealer_hand = [deck.pop(), deck.pop()]
        def hand_value(h):
            total = sum(h)
            if 1 in h and total + 10 <= 21:
                return total + 10
            return total
        player_val = hand_value(player_hand)
        dealer_val = hand_value(dealer_hand)
        # Fixed prizes: win=2x, push=1x(refund), lose=0, blackjack=2.5x
        is_blackjack = player_val == 21 and len(player_hand) == 2
        if is_blackjack:
            win_amount = int(cost * 2.5)
            prize_tier = "Blackjack!"
        elif player_val == 21:
            win_amount = cost * 2
            prize_tier = "21点"
        else:
            win_amount = cost * 2  # base win amount if player beats dealer
            prize_tier = "技巧奖"
        return {
            "mode": "blackjack",
            "playerHand": player_hand,
            "dealerUpCard": dealer_hand[0],
            "_dealerHand": dealer_hand,
            "_deck": deck,
            "_isBlackjack": is_blackjack,
            "_win": win_amount,
            "resultText": f"你的点数: {player_val}，击败庄家赢 {cost*2} 爽币",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, 0  # win determined at finish

    # ---- existing scratch card types ----
    win, prize_tier = roll_payout(ticket_type, streak, player_public_id, cost)

    if kind == "match":
        ordinary = [
            ("🏮", "灯笼"), ("💰", "元宝"), ("🪄", "如意"), ("🍑", "寿桃"),
            ("🪷", "荷花"), ("🧧", "红包"), ("🪙", "铜钱"), ("🪭", "团扇"),
            ("☁️", "彩云"), ("🎁", "礼盒"),
        ]
        for _ in range(30):
            symbol, tag = random.choice(ordinary)
            cells.append({"symbol": symbol, "tag": tag, "prize": random.choice([5, 5, 8, 10, 10, 15, 20, 30])})
        if win:
            if win >= 10000:
                cells[random.randrange(30)] = {"symbol": "囍", "tag": "双喜", "prize": win // 2}
            else:
                cells[random.randrange(30)] = {"symbol": "喜", "tag": "喜事", "prize": win}
        result_text = "喜事相逢，双喜还可再翻一倍" if win else "缘分未到，下一张喜事连连"
    elif kind == "numbers":
        for _ in range(20):
            number = random.randint(1, 99)
            while "7" in str(number):
                number = random.randint(1, 99)
            cells.append({"symbol": str(number).zfill(2), "prize": random.choice([5, 10, 10, 15, 20, 30, 50])})
        if win:
            if win >= 10000:
                cells[random.randrange(20)] = {"symbol": "777", "prize": win // 3, "jackpot": True}
            else:
                cells[random.randrange(20)] = {
                    "symbol": random.choice(["07", "17", "27", "37", "47", "57", "67", "70", "71", "72", "73", "74", "75", "76", "78", "79"]),
                    "prize": win,
                }
        result_text = "幸运数字 7 出现，777 奖金三倍" if win else "这张没有 7，下一张继续追"
    elif kind == "triple":
        treasures = [("金元宝", "💰"), ("翡翠", "💎"), ("如意", "🪄"), ("红包", "🧧"), ("铜钱", "🪙")]
        target_treasure = random.choice(treasures) if win else None
        safe_treasures = [item for item in treasures if item != target_treasure]
        spots = random.sample(range(9), 3) if win else []
        pool = safe_treasures * 2
        random.shuffle(pool)
        for index in range(9):
            if index in spots:
                cells.append(None)
            else:
                tag, symbol = pool.pop()
                cells.append({"symbol": symbol, "tag": tag, "prize": random.choice([10, 15, 20, 30, 50])})
        if win:
            tag, symbol = target_treasure
            for index, spot in enumerate(spots):
                cells[spot] = {"symbol": symbol, "tag": tag, "prize": win if index == 0 else 0}
        result_text = "三宝同堂，福气装满口袋" if win else "宝物还没聚齐，再来一张"
    elif kind == "multi":
        target, multiplier = random.randint(1, 12), random.choice([2, 2, 3, 3, 5, 10])
        winning = [{"symbol": target, "tag": "幸运数字"}, {"symbol": f"×{multiplier}", "tag": "全票倍数"}]
        cells = []
        for _ in range(9):
            symbol = random.randint(1, 12)
            while symbol == target:
                symbol = random.randint(1, 12)
            cells.append({"symbol": symbol, "prize": random.choice([5, 8, 10, 15, 20])})
        if win:
            cells[random.randrange(9)] = {"symbol": target, "prize": win // multiplier}
        result_text = f"幸运数字命中，爽币 ×{multiplier}" if win else "倍数已就位，只差一次命中"
    elif kind == "koi":
        cells = [{"symbol": random.choice(["🌊", "🫧", "🪷", "🐚"]), "tag": "好运池", "prize": 0} for _ in range(9)]
        if win:
            if win >= 10000:
                first, second = random.sample(range(9), 2)
                cells[first] = {"symbol": "🐟", "tag": "锦鲤", "prize": win // 4}
                cells[second] = {"symbol": "🐟", "tag": "锦鲤", "prize": win // 4}
            else:
                cells[random.randrange(9)] = {"symbol": "🐟", "tag": "锦鲤", "prize": win}
        result_text = "双锦鲤驾到，奖金再翻倍" if win >= 10000 else "锦鲤上岸，好运到家" if win else "锦鲤游走了，下一池再见"

    # ---- new: SSQ (双色球) ----
    elif kind == "ssq":
        player_reds = sorted(random.sample(range(1, 34), 6))
        player_blue = random.randint(1, 16)
        red_pool = [n for n in range(1, 34) if n not in player_reds]
        if not win:
            match_count, blue_match = random.choice([(0, False), (1, False), (2, False)])
        elif prize_tier == "回本":
            match_count, blue_match = 3, True
        elif prize_tier == "小奖":
            match_count, blue_match = 4, False
        elif prize_tier == "中奖":
            match_count, blue_match = 4, True
        elif prize_tier == "大奖":
            match_count, blue_match = 5, False
        elif prize_tier == "超级大奖":
            match_count, blue_match = 5, True
        elif prize_tier == "钻石大奖":
            match_count, blue_match = 6, False
        else:
            match_count, blue_match = 6, True
        server_reds = sorted(random.sample(player_reds, match_count) + random.sample(red_pool, 6 - match_count))
        if blue_match:
            server_blue = player_blue
        else:
            server_blue = random.choice([n for n in range(1, 17) if n != player_blue])
        red_matches = len(set(player_reds) & set(server_reds))
        return {
            "mode": "ssq",
            "playerReds": player_reds,
            "playerBlue": player_blue,
            "serverReds": server_reds,
            "serverBlue": server_blue,
            "_redMatches": red_matches,
            "_blueMatch": blue_match,
            "_win": win,
            "resultText": f"红球 {len(set(player_reds) & set(server_reds))} 个匹配，蓝球{'匹配' if blue_match else '未匹配'}",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, win

    # ---- new: baccarat ----
    elif kind == "baccarat":
        player_hand = [random.randint(1, 9), random.randint(1, 9)]
        banker_hand = [random.randint(1, 9), random.randint(1, 9)]
        player_total = sum(player_hand) % 10
        banker_total = sum(banker_hand) % 10
        if player_total > banker_total:
            result = "闲"
        elif banker_total > player_total:
            result = "庄"
        else:
            result = "和"
        return {
            "mode": "baccarat",
            "_playerHand": player_hand,
            "_bankerHand": banker_hand,
            "_playerTotal": player_total,
            "_bankerTotal": banker_total,
            "_result": result,
            "resultText": "选择闲、庄或和，再揭晓牌面",
            "prizeTier": "技巧奖",
            "maxPrize": 1000000,
        }, 0

    # ---- new: dice (猜大小) ----
    elif kind == "dice":
        dice = [random.randint(1, 6) for _ in range(3)]
        dice_total = sum(dice)
        is_big = dice_total >= 11
        is_triple = dice[0] == dice[1] == dice[2]
        return {
            "mode": "dice",
            "_dice": dice,
            "_total": dice_total,
            "_isBig": is_big,
            "_isTriple": is_triple,
            "resultText": "下注后由服务器揭晓三颗骰子",
            "prizeTier": "技巧奖",
            "maxPrize": 1000000,
        }, win

    # ---- new: slots ----
    elif kind == "slots":
        fruits = ["🍒", "🍋", "🍊", "🍇", "💎", "7️⃣", "⭐"]
        weights = [30, 25, 20, 12, 6, 4, 3]
        reels = []
        for _ in range(3):
            reel = []
            for _ in range(12):
                reel.append(random.choices(fruits, weights=weights)[0])
            reels.append(reel)
        if win:
            symbol = random.choice(fruits)
            if prize_tier in ("超级大奖", "钻石大奖", "传说大奖"):
                final = [symbol, symbol, symbol]
            else:
                other = random.choice([f for f in fruits if f != symbol])
                final = [symbol, symbol, other]
                random.shuffle(final)
        else:
            final = random.sample(fruits, 3)
        for index, symbol in enumerate(final):
            reels[index][5] = symbol
        return {
            "mode": "slots",
            "reels": reels,
            "_final": final,
            "resultText": "转轮停止，看看你的运气",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, win

    # ---- new: pinball (with pegs) ----
    elif kind == "pinball":
        slot_payouts = [0, cost, cost * 2, 0, cost * 5, cost, cost * 10, 0]
        slot_labels = ["空", "回本", "小奖", "空", "中奖", "回本", "大奖", "空"]
        target_slot = random.choice([0, 3, 7]) if not win else random.randrange(len(slot_payouts))
        if win:
            slot_payouts[target_slot] = win
            slot_labels[target_slot] = prize_tier
        # Generate random peg bounce path
        pegs = []
        rows = 8
        for row in range(rows):
            peg_row = []
            cols = 4 + row
            for col in range(cols):
                if random.random() > 0.15:
                    peg_row.append({"x": col, "y": row})
            pegs.append(peg_row)
        return {
            "mode": "pinball",
            "slots": [{"label": l, "payout": p} for l, p in zip(slot_labels, slot_payouts)],
            "targetSlot": target_slot,
            "resultText": "弹珠落下，看看落入哪个奖池",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, win

    # ---- new: wheel (10 segments) ----
    elif kind == "wheel":
        segments = [
            {"label": "空", "payout": 0, "color": "#7f8c8d"},
            {"label": "空", "payout": 0, "color": "#95a5a6"},
            {"label": "回本", "payout": cost, "color": "#f39c12"},
            {"label": "回本", "payout": cost, "color": "#e67e22"},
            {"label": "小奖", "payout": cost * 2, "color": "#3498db"},
            {"label": "小奖", "payout": cost * 2, "color": "#2980b9"},
            {"label": "中奖", "payout": cost * 5, "color": "#9b59b6"},
            {"label": "大奖", "payout": cost * 10, "color": "#e74c3c"},
            {"label": "超级", "payout": cost * 50, "color": "#f1c40f"},
            {"label": "传说", "payout": cost * 100, "color": "#ff6348"},
        ]
        target_seg = random.choice([0, 1]) if not win else random.randrange(2, len(segments))
        if win:
            segments[target_seg]["payout"] = win
            segments[target_seg]["label"] = prize_tier
        return {
            "mode": "wheel",
            "segments": segments,
            "targetSegment": target_seg,
            "resultText": "转盘旋转，指针停在哪里？",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, win

    else:
        cells = [{"symbol": random.choice(["🌊", "🫧", "🪷", "🐚"]), "tag": "好运池", "prize": 0} for _ in range(9)]
        if win:
            if win >= 10000:
                first, second = random.sample(range(9), 2)
                cells[first] = {"symbol": "🐟", "tag": "锦鲤", "prize": win // 4}
                cells[second] = {"symbol": "🐟", "tag": "锦鲤", "prize": win // 4}
            else:
                cells[random.randrange(9)] = {"symbol": "🐟", "tag": "锦鲤", "prize": win}
        result_text = "双锦鲤驾到，奖金再翻倍" if win >= 10000 else "锦鲤上岸，好运到家" if win else "锦鲤游走了，下一池再见"

    return {
        "cells": cells,
        "winning": winning,
        "resultText": result_text,
        "prizeTier": prize_tier,
        "maxPrize": 1000000 if win >= 10000 else TYPES[ticket_type].get("jackpot", cost * 10),
    }, win


# ---------- HTTP Handler ----------
class Handler(SimpleHTTPRequestHandler):
    server_version = f"ScratchGame/{APP_VERSION}"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 64_000:
                raise ValueError("request too large")
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return None

    @property
    def client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0]

    def auth_player(self, conn):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return conn.execute("SELECT * FROM players WHERE token_hash=?", (token_hash(header[7:]),)).fetchone()

    # ---------- GET ----------
    def do_GET(self):
        path = urlparse(self.path).path
        qs = urlparse(self.path).query
        params = {}
        if qs:
            for pair in qs.split("&"):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    params[k] = unquote_plus(v)

        if path == "/api/health":
            return self.send_json({"ok": True, "version": APP_VERSION})

        if path == "/api/catalog":
            cfg = get_config()
            games = []
            for game_id, game in TYPES.items():
                games.append({
                    "id": game_id,
                    "cost": int(cfg.get(f"game_{game_id}_cost", game["cost"])),
                    "enabled": str(cfg.get(f"game_{game_id}_enabled", "1")) not in ("0", "0.0"),
                })
            return self.send_json({"games": games})

        # ---- admin GET routes ----
        if path == "/admin/config":
            admin_token = self.headers.get("X-Admin-Token", "")
            if admin_token not in _admin_sessions or _admin_sessions[admin_token] < time.time():
                return self.send_json({"error": "admin_unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return self.send_json({"config": get_config()})

        if path == "/admin/stats":
            admin_token = self.headers.get("X-Admin-Token", "")
            if admin_token not in _admin_sessions or _admin_sessions[admin_token] < time.time():
                return self.send_json({"error": "admin_unauthorized"}, HTTPStatus.UNAUTHORIZED)
            with db() as conn:
                total_players = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
                total_tickets = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
                total_settled = conn.execute("SELECT COUNT(*) FROM tickets WHERE settled=1").fetchone()[0]
                total_payout = conn.execute("SELECT COALESCE(SUM(win), 0) FROM tickets WHERE settled=1").fetchone()[0]
                total_coins = conn.execute("SELECT COALESCE(SUM(coins), 0) FROM players").fetchone()[0]
            return self.send_json({
                "totalPlayers": total_players,
                "totalTickets": total_tickets,
                "totalSettled": total_settled,
                "totalPayout": total_payout,
                "totalCoins": total_coins,
            })

        if path == "/admin/users":
            admin_token = self.headers.get("X-Admin-Token", "")
            if admin_token not in _admin_sessions or _admin_sessions[admin_token] < time.time():
                return self.send_json({"error": "admin_unauthorized"}, HTTPStatus.UNAUTHORIZED)
            q = params.get("q", "").strip()
            with db() as conn:
                if q:
                    rows = conn.execute(
                        "SELECT * FROM players WHERE nickname LIKE ? OR public_id LIKE ? ORDER BY level DESC LIMIT 20",
                        (f"%{q}%", f"%{q}%"),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM players ORDER BY level DESC LIMIT 20"
                    ).fetchall()
            cfg = get_config()
            return self.send_json({
                "users": [{
                    **player_dict(row, True),
                    "rateMultiplier": float(cfg.get(f"user_{row['public_id']}_rate", "1.0")),
                } for row in rows],
            })

        # ---- leaderboard with pagination ----
        if path == "/api/leaderboard":
            page = max(1, int(params.get("page", "1")))
            limit = min(50, max(5, int(params.get("limit", "10"))))
            sort = params.get("sort", "level")
            offset = (page - 1) * limit
            if sort == "coins":
                order = "coins DESC, level DESC"
            else:
                order = "level DESC, xp DESC, best DESC, wins DESC"
            with db() as conn:
                rows = conn.execute(
                    f"SELECT * FROM players ORDER BY {order} LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                total = conn.execute("SELECT COUNT(*) FROM players").fetchone()[0]
            pages = max(1, (total + limit - 1) // limit)
            return self.send_json({
                "leaderboard": [player_dict(row, False) for row in rows],
                "total": total, "page": page, "pages": pages, "sort": sort,
            })

        # ---- player data with paginated records ----
        if path == "/api/me":
            records_page = max(1, int(params.get("records_page", "1")))
            records_limit = min(50, max(5, int(params.get("records_limit", "10"))))
            with db() as conn:
                player = self.auth_player(conn)
                if not player:
                    return self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                recs, recs_total = records_for(conn, player["id"], records_page, records_limit)
                return self.send_json({
                    "player": player_dict(player),
                    "records": recs,
                    "records_total": recs_total,
                    "records_page": records_page,
                    "records_pages": max(1, (recs_total + records_limit - 1) // records_limit),
                })

        return super().do_GET()

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_json()
        if data is None:
            return self.send_json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)

        # ---- admin routes ----
        if path == "/admin/login":
            username = str(data.get("username", ""))
            password = str(data.get("password", ""))
            if username != ADMIN_USER:
                return self.send_json({"error": "用户名错误"}, HTTPStatus.UNAUTHORIZED)
            h = hashlib.pbkdf2_hmac("sha256", password.encode(), _admin_salt.encode(), 200_000).hex()
            if h != _admin_hash:
                return self.send_json({"error": "密码错误"}, HTTPStatus.UNAUTHORIZED)
            token = secrets.token_urlsafe(32)
            _admin_sessions[token] = time.time() + 3600  # 1-hour session
            # Clean expired sessions
            now = time.time()
            for k in list(_admin_sessions):
                if _admin_sessions[k] < now:
                    del _admin_sessions[k]
            return self.send_json({"token": token})

        if path == "/admin/config":
            admin_token = self.headers.get("X-Admin-Token", "")
            if admin_token not in _admin_sessions or _admin_sessions[admin_token] < time.time():
                return self.send_json({"error": "admin_unauthorized"}, HTTPStatus.UNAUTHORIZED)
            if data:
                try:
                    set_config(data)
                except (TypeError, ValueError) as exc:
                    return self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return self.send_json({"config": get_config()})

        # admin stats handled in do_GET

        # ---- admin user management ----
        user_match = re.fullmatch(r"/admin/users/(GS-[A-F0-9]+)/coins", path)
        if user_match:
            admin_token = self.headers.get("X-Admin-Token", "")
            if admin_token not in _admin_sessions or _admin_sessions[admin_token] < time.time():
                return self.send_json({"error": "admin_unauthorized"}, HTTPStatus.UNAUTHORIZED)
            public_id = user_match.group(1)
            amount = int(data.get("amount", 0))
            with db() as conn:
                conn.execute("UPDATE players SET coins=coins+?, updated_at=? WHERE public_id=?",
                             (amount, int(time.time()), public_id))
                player = conn.execute("SELECT * FROM players WHERE public_id=?", (public_id,)).fetchone()
            if not player:
                return self.send_json({"error": "user_not_found"}, HTTPStatus.NOT_FOUND)
            return self.send_json({"player": player_dict(player)})

        user_match = re.fullmatch(r"/admin/users/(GS-[A-F0-9]+)/winrate", path)
        if user_match:
            admin_token = self.headers.get("X-Admin-Token", "")
            if admin_token not in _admin_sessions or _admin_sessions[admin_token] < time.time():
                return self.send_json({"error": "admin_unauthorized"}, HTTPStatus.UNAUTHORIZED)
            public_id = user_match.group(1)
            rate = float(data.get("rate", 1.0))
            rate = max(0.0, min(10.0, rate))
            set_config({f"user_{public_id}_rate": str(rate)})
            return self.send_json({"playerId": public_id, "rateMultiplier": rate})

        # ---- register ----
        if path == "/api/register":
            nickname = re.sub(r"\s+", " ", str(data.get("nickname", "")).strip())
            if not 2 <= len(nickname) <= 16 or any(ord(c) < 32 for c in nickname):
                return self.send_json({"error": "昵称需要 2-16 个字符"}, HTTPStatus.BAD_REQUEST)
            token = secrets.token_urlsafe(32)
            now = int(time.time())
            with db() as conn:
                for _ in range(10):
                    public_id = "GS-" + secrets.token_hex(3).upper()
                    try:
                        cur = conn.execute(
                            "INSERT INTO players(public_id,nickname,token_hash,created_at,updated_at) VALUES(?,?,?,?,?)",
                            (public_id, nickname, token_hash(token), now, now),
                        )
                        player = conn.execute("SELECT * FROM players WHERE id=?", (cur.lastrowid,)).fetchone()
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    return self.send_json({"error": "create_failed"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return self.send_json({"token": token, "player": player_dict(player), "records": [], "records_total": 0}, HTTPStatus.CREATED)

        if path == "/api/daily-gift":
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                player = self.auth_player(conn)
                if not player:
                    return self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                today = china_day()
                if player["last_gift_day"] == today:
                    return self.send_json({"error": "今日好运金已经领取"}, HTTPStatus.CONFLICT)
                now = int(time.time())
                conn.execute(
                    "UPDATE players SET coins=coins+80,last_gift_day=?,updated_at=? WHERE id=?",
                    (today, now, player["id"]),
                )
                player = conn.execute("SELECT * FROM players WHERE id=?", (player["id"],)).fetchone()
            return self.send_json({"gift": 80, "player": player_dict(player)})

        # ---- buy ticket ----
        if path == "/api/tickets":
            ticket_type = str(data.get("type", ""))
            if ticket_type not in TYPES:
                return self.send_json({"error": "unknown_ticket"}, HTTPStatus.BAD_REQUEST)
            # Check if game is enabled
            cfg = get_config()
            if str(cfg.get(f"game_{ticket_type}_enabled", "1")) in ("0", "0.0"):
                return self.send_json({"error": "该玩法暂未开放"}, HTTPStatus.BAD_REQUEST)
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                player = self.auth_player(conn)
                if not player:
                    return self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)

                # rate limiting
                allowed, reason = check_rate_limit(player, self.client_ip)
                if not allowed:
                    return self.send_json({"error": reason}, HTTPStatus.TOO_MANY_REQUESTS)

                cost = TYPES[ticket_type]["cost"]
                # Allow custom cost override
                custom_cost = cfg.get(f"game_{ticket_type}_cost")
                if custom_cost is not None:
                    cost = int(custom_cost)
                coins = player["coins"]
                if coins < cost:
                    return self.send_json(
                        {"error": "爽币不足，请领取每日好运金或选择低票价玩法", "code": "insufficient_coins"},
                        HTTPStatus.PAYMENT_REQUIRED,
                    )

                payload, win = generate_ticket(ticket_type, player["streak"], player["public_id"], cost)
                payload["_cost"] = cost
                ticket_id = secrets.token_urlsafe(18)
                now = int(time.time())

                conn.execute(
                    "UPDATE players SET coins=?, updated_at=?, last_ticket_at=? WHERE id=?",
                    (coins - cost, now, time.time(), player["id"]),
                )
                conn.execute(
                    "INSERT INTO tickets(id,player_id,ticket_type,payload,win,created_at) VALUES(?,?,?,?,?,?)",
                    (ticket_id, player["id"], ticket_type, json.dumps(payload, ensure_ascii=False), win, now),
                )
                player = conn.execute("SELECT * FROM players WHERE id=?", (player["id"],)).fetchone()

            public_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
            return self.send_json({
                "ticketId": ticket_id,
                "ticket": public_payload,
                "player": player_dict(player),
                "cost": cost,
            })

        # ---- finish ticket ----
        match = re.fullmatch(r"/api/tickets/([A-Za-z0-9_-]+)/finish", path)
        if match:
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                player = self.auth_player(conn)
                if not player:
                    return self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                ticket_row = conn.execute(
                    "SELECT * FROM tickets WHERE id=? AND player_id=?", (match.group(1), player["id"])
                ).fetchone()
                if not ticket_row:
                    return self.send_json({"error": "ticket_not_found"}, HTTPStatus.NOT_FOUND)
                if not ticket_row["settled"]:
                    payload = json.loads(ticket_row["payload"])
                    win = ticket_row["win"]

                    if payload.get("mode") == "twentyfour":
                        valid, message = validate_24_answer(
                            str(data.get("answer", "")),
                            payload["_numbers"],
                        )
                        payload["_validationMessage"] = message
                        if not valid:
                            win = 0

                    elif payload.get("mode") == "pusher":
                        try:
                            drop_x = float(data.get("dropX", -1))
                        except (TypeError, ValueError):
                            drop_x = -1
                        if abs(drop_x - payload["_targetX"]) > payload["_tolerance"]:
                            win = 0

                    elif payload.get("mode") == "claw":
                        try:
                            slot = int(data.get("slot", -1))
                        except (TypeError, ValueError):
                            slot = -1
                        if slot != payload["_winningSlot"]:
                            win = 0

                    elif payload.get("mode") == "redpacket":
                        try:
                            packet = int(data.get("packet", -1))
                        except (TypeError, ValueError):
                            packet = -1
                        payload["_pickedPacket"] = packet
                        if packet != payload["_luckyIndex"]:
                            win = 0

                    # blackjack: handle hit/stand
                    elif payload.get("mode") == "blackjack":
                        action = str(data.get("action", "stand"))
                        deck = payload["_deck"]
                        player_hand = payload["playerHand"][:]
                        dealer_hand = payload["_dealerHand"][:]

                        def hv(h):
                            t = sum(h)
                            return t + 10 if 1 in h and t + 10 <= 21 else t

                        player_val = hv(player_hand)

                        if action == "hit":
                            player_hand.append(deck.pop(0))
                            player_val = hv(player_hand)
                            if player_val > 21:
                                win = 0  # bust
                            elif player_val < 21:
                                # still can continue, don't settle yet
                                # return current state so frontend can show updated cards
                                payload["playerHand"] = player_hand
                                payload["_deck"] = deck
                                conn.execute(
                                    "UPDATE tickets SET payload=? WHERE id=?",
                                    (json.dumps(payload, ensure_ascii=False), ticket_row["id"]),
                                )
                                conn.execute("UPDATE players SET updated_at=? WHERE id=?",
                                             (int(time.time()), player["id"]))
                                player = conn.execute("SELECT * FROM players WHERE id=?", (player["id"],)).fetchone()
                                return self.send_json({
                                    "win": 0,
                                    "hitOngoing": True,
                                    "playerHand": player_hand,
                                    "playerVal": player_val,
                                    "dealerUpCard": dealer_hand[0],
                                    "player": player_dict(player),
                                })

                        # Stand or bust or hit-to-21: resolve the hand
                        # Dealer plays
                        while hv(dealer_hand) < 17:
                            dealer_hand.append(deck.pop(0))
                        dealer_val = hv(dealer_hand)
                        cost = int(payload.get("_cost", TYPES[ticket_row["ticket_type"]]["cost"]))

                        if player_val > 21:
                            win = 0  # bust
                        elif payload.get("_isBlackjack"):
                            win = int(cost * 2.5)  # natural blackjack
                        elif dealer_val > 21:
                            win = cost * 2  # dealer bust
                        elif player_val > dealer_val:
                            win = cost * 2  # higher hand
                        elif player_val == dealer_val:
                            win = cost  # push = refund
                        else:
                            win = 0  # dealer higher

                        payload["playerHand"] = player_hand
                        payload["_dealerHand"] = dealer_hand

                    # dice: check bet
                    elif payload.get("mode") == "dice":
                        bet = str(data.get("bet", ""))
                        if payload["_isTriple"]:
                            correct = (bet == "triple")
                        else:
                            correct = (bet == "big" and payload["_isBig"]) or (bet == "small" and not payload["_isBig"])
                        cost = int(payload.get("_cost", TYPES[ticket_row["ticket_type"]]["cost"]))
                        if correct:
                            win = cost * (6 if bet == "triple" else 2)
                        else:
                            win = 0

                    # ssq - already server-determined
                    elif payload.get("mode") == "ssq":
                        win = payload["_win"]

                    # baccarat: player chooses before cards are revealed
                    elif payload.get("mode") == "baccarat":
                        bet = str(data.get("bet", ""))
                        result = payload["_result"]
                        cost = int(payload.get("_cost", TYPES[ticket_row["ticket_type"]]["cost"]))
                        if bet == result:
                            win = cost * (8 if result == "和" else 2)
                        else:
                            win = 0

                    elif payload.get("mode") == "pinball":
                        try:
                            slot = int(data.get("slot", -1))
                        except (TypeError, ValueError):
                            slot = -1
                        if slot != payload["targetSlot"]:
                            win = 0

                    played = player["played"] + 1
                    wins = player["wins"] + (1 if win else 0)
                    streak = player["streak"] + 1 if win else 0
                    best = max(player["best"], win)
                    coins = player["coins"] + win
                    xp = player["xp"] + 20 + min(win, 50)
                    level = player["level"]
                    while xp >= level * 100:
                        level += 1
                        coins += 50
                    seen = json.loads(player["seen"] or "[]")
                    challenge_reward = 0
                    if ticket_row["ticket_type"] not in seen:
                        seen.append(ticket_row["ticket_type"])
                        if len(seen) == len(TYPES):
                            challenge_reward = 100
                            coins += challenge_reward
                    now = int(time.time())
                    conn.execute(
                        """UPDATE players SET coins=?,xp=?,level=?,played=?,wins=?,best=?,streak=?,seen=?,updated_at=?
                           WHERE id=?""",
                        (coins, xp, level, played, wins, best, streak, json.dumps(seen), now, player["id"]),
                    )
                    conn.execute(
                        "UPDATE tickets SET settled=1,settled_at=?,win=?,payload=? WHERE id=?",
                        (now, win, json.dumps(payload, ensure_ascii=False), ticket_row["id"]),
                    )
                    conn.execute(
                        "INSERT INTO records(player_id,ticket_type,win,created_at) VALUES(?,?,?,?)",
                        (player["id"], ticket_row["ticket_type"], win, now),
                    )
                player = conn.execute("SELECT * FROM players WHERE id=?", (player["id"],)).fetchone()
                ticket_row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_row["id"],)).fetchone()
                records, _ = records_for(conn, player["id"], 1, 10)
            resp = {
                "win": ticket_row["win"],
                "player": player_dict(player),
                "records": records,
                "challengeReward": challenge_reward if "challenge_reward" in locals() else 0,
            }
            # Include final hands for blackjack
            final_payload = json.loads(ticket_row["payload"])
            if final_payload.get("mode") == "blackjack":
                resp["playerHand"] = final_payload.get("playerHand", [])
                resp["dealerHand"] = final_payload.get("_dealerHand", [])
            elif final_payload.get("mode") == "twentyfour":
                resp["validationMessage"] = final_payload.get("_validationMessage", "")
            elif final_payload.get("mode") == "dice":
                resp["dice"] = final_payload.get("_dice", [])
                resp["total"] = final_payload.get("_total", 0)
                resp["isTriple"] = final_payload.get("_isTriple", False)
            elif final_payload.get("mode") == "baccarat":
                resp["playerHand"] = final_payload.get("_playerHand", [])
                resp["bankerHand"] = final_payload.get("_bankerHand", [])
                resp["playerTotal"] = final_payload.get("_playerTotal", 0)
                resp["bankerTotal"] = final_payload.get("_bankerTotal", 0)
                resp["result"] = final_payload.get("_result", "")
            elif final_payload.get("mode") == "redpacket":
                resp["pickedPacket"] = final_payload.get("_pickedPacket", -1)
                resp["luckyPacket"] = final_payload.get("_luckyIndex", -1)
            return self.send_json(resp)

        return self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    init_db()
    print(f"Scratch Game v{APP_VERSION} listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
