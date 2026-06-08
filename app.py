#!/usr/bin/env python3
import hashlib
import json
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
    "slots": {"name": "水果机", "icon": "🍒", "cost": 5, "kind": "slots"},
    "pinball": {"name": "弹珠台", "icon": "🔮", "cost": 8, "kind": "pinball"},
    "wheel": {"name": "幸运转盘", "icon": "🎡", "cost": 10, "kind": "wheel"},
}

SUPER_PRIZES = [1000000, 100000, 10000]  # legend, diamond, super


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
    with db() as conn:
        for k, v in updates.items():
            if v is None or (isinstance(v, str) and v.strip() == ""):
                conn.execute("DELETE FROM admin_config WHERE key=?", (k,))
            else:
                conn.execute(
                    "INSERT OR REPLACE INTO admin_config(key, value) VALUES(?, ?)",
                    (k, str(v)),
                )


# ---------- auth ----------
def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def player_dict(row, include_private=True):
    raw_seen = json.loads(row["seen"] or "[]")
    valid_seen = [s for s in raw_seen if s in TYPES]
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


# ---------- payout engine ----------
def roll_payout(ticket_type, streak, player_public_id=None):
    """Server-authoritative payout with configurable tiers and per-game/per-user overrides."""
    cfg = get_config()
    cost = TYPES[ticket_type]["cost"]
    pity = min(cfg.get("pity_max", 0.04), streak * cfg.get("pity_step", 0.002))

    # Per-game win rate override
    game_rate_key = f"game_{ticket_type}_winrate"
    game_rate = cfg.get(game_rate_key)
    if game_rate is not None:
        try:
            game_rate = float(game_rate)
        except (ValueError, TypeError):
            game_rate = None

    # Per-user rate multiplier
    user_mult = 1.0
    if player_public_id:
        user_mult = float(cfg.get(f"user_{player_public_id}_rate", "1.0"))

    if game_rate is not None:
        # game_rate is total win probability (0-1), distribute proportionally
        base_win = 1.0 - cfg.get("lose", 0.614) + pity
        scale = game_rate / max(base_win, 0.001) if base_win > 0 else 1.0
        lose_p = 1.0 - game_rate
    else:
        scale = 1.0
        lose_p = cfg.get("lose", 0.614) - pity

    # Apply user multiplier to win chances
    if user_mult != 1.0:
        scale *= user_mult
        lose_p = max(0.05, lose_p - (user_mult - 1.0) * 0.1)

    roll = random.random()
    be_s = cfg.get("break_even", 0.23) * scale
    sm_s = cfg.get("small", 0.10) * scale
    md_s = cfg.get("medium", 0.04) * scale
    bg_s = cfg.get("big", 0.009) * scale
    sp_s = cfg.get("super", 0.0008) * scale
    dm_s = cfg.get("diamond", 0.00015) * scale
    lg_s = cfg.get("legend", 0.00005) * scale

    be_p = lose_p + be_s
    small_p = be_p + sm_s
    medium_p = small_p + md_s
    big_p = medium_p + bg_s
    super_p = big_p + sp_s
    diamond_p = super_p + dm_s
    legend_p = min(1.0, diamond_p + lg_s)

    if roll < lose_p:
        return 0, "未中奖"
    if roll < be_p:
        return cost, "回本"
    if roll < small_p:
        return cost * 2, "小奖"
    if roll < medium_p:
        return cost * 5, "中奖"
    if roll < big_p:
        return cost * 10, "大奖"
    if roll < super_p:
        return 10000, "超级大奖"
    if roll < diamond_p:
        return 100000, "钻石大奖"
    if roll < legend_p:
        return 1000000, "传说大奖"
    return 0, "未中奖"


# ---------- ticket generators ----------
def generate_ticket(ticket_type, streak, player_public_id=None):
    kind = TYPES[ticket_type]["kind"]
    cost = TYPES[ticket_type]["cost"]
    cells, winning = [], []

    # ---- Blackjack (21点) ----
    if kind == "blackjack":
        cost = TYPES[ticket_type]["cost"]
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
            "dealerHand": dealer_hand,
            "deck": deck,
            "_isBlackjack": is_blackjack,
            "_win": win_amount,
            "resultText": f"你的点数: {player_val}，击败庄家赢 {cost*2} 爽币",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, 0  # win determined at finish

    # ---- existing scratch card types ----
    win, prize_tier = roll_payout(ticket_type, streak, player_public_id)

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
        win, prize_tier = roll_payout(ticket_type, streak, player_public_id)
        # Generate player numbers and winning numbers
        player_reds = sorted(random.sample(range(1, 34), 6))
        player_blue = random.randint(1, 16)
        server_reds = sorted(random.sample(range(1, 34), 6))
        server_blue = random.randint(1, 16)
        red_matches = len(set(player_reds) & set(server_reds))
        blue_match = player_blue == server_blue
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
        win, prize_tier = roll_payout(ticket_type, streak, player_public_id)
        # Simulate a hand
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
            "playerHand": player_hand,
            "bankerHand": banker_hand,
            "playerTotal": player_total,
            "bankerTotal": banker_total,
            "result": result,
            "_win": win,
            "resultText": f"{result}赢{' (和局)' if result == '和' else ''}",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, win

    # ---- new: dice (猜大小) ----
    elif kind == "dice":
        dice = [random.randint(1, 6) for _ in range(3)]
        dice_total = sum(dice)
        is_big = dice_total >= 11
        is_triple = dice[0] == dice[1] == dice[2]
        bet_type = "big" if is_big else "small"
        if is_triple:
            bet_type = "triple"
        win, prize_tier = roll_payout(ticket_type, streak, player_public_id)
        if not win:
            # force wrong bet hint
            bet_type = ""
        return {
            "mode": "dice",
            "dice": dice,
            "total": dice_total,
            "_isBig": is_big,
            "_isTriple": is_triple,
            "_betType": bet_type,
            "resultText": f"骰子点数: {dice_total} {'大' if is_big else '小'}",
            "prizeTier": prize_tier,
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
        # Determine win from final positions (middle row)
        final = [reel[5] for reel in reels]
        if final[0] == final[1] == final[2]:
            multiplier = 8
        elif final[0] == final[1] or final[1] == final[2]:
            multiplier = 3
        else:
            multiplier = 0
        actual_win = win if multiplier else 0
        return {
            "mode": "slots",
            "reels": reels,
            "_final": final,
            "_multiplier": multiplier,
            "resultText": "转轮停止，看看你的运气",
            "prizeTier": prize_tier,
            "maxPrize": 1000000,
        }, actual_win

    # ---- new: pinball (with pegs) ----
    elif kind == "pinball":
        slot_payouts = [0, cost, cost, cost * 2, cost * 2, cost * 3, cost * 5, cost * 10]
        slot_labels = ["空", "回本", "回本", "小奖", "小奖", "中奖", "大奖", "超级"]
        win, prize_tier = roll_payout(ticket_type, streak, player_public_id)
        target_slot = random.choices(
            range(len(slot_payouts)),
            weights=[40, 20, 15, 10, 7, 5, 2, 1]
        )[0]
        if win:
            paying_slots = [i for i, p in enumerate(slot_payouts) if p > 0]
            target_slot = random.choice(paying_slots)
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
            "_targetSlot": target_slot,
            "_pegs": pegs,
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
        weights = [25, 23, 15, 12, 8, 7, 5, 3, 1.5, 0.5]
        win, prize_tier = roll_payout(ticket_type, streak, player_public_id)
        target_seg = random.choices(range(len(segments)), weights=weights)[0]
        if not win:
            target_seg = random.choice([0, 1])  # force lose to empty
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
    server_version = "ScratchGame/2.0"

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
            return self.send_json({"ok": True})

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
                set_config({k: str(v) for k, v in data.items()})
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

        # ---- buy ticket ----
        if path == "/api/tickets":
            ticket_type = str(data.get("type", ""))
            if ticket_type not in TYPES:
                return self.send_json({"error": "unknown_ticket"}, HTTPStatus.BAD_REQUEST)
            # Check if game is enabled
            cfg = get_config()
            if cfg.get(f"game_{ticket_type}_enabled", "1") == "0":
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
                gift = 0
                if coins < cost:
                    gift = 100
                    coins += gift

                payload, win = generate_ticket(ticket_type, player["streak"], player["public_id"])
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
                "gift": gift,
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

                    # blackjack: handle hit/stand
                    if payload.get("mode") == "blackjack":
                        action = str(data.get("action", "stand"))
                        deck = payload["deck"]
                        player_hand = payload["playerHand"][:]
                        dealer_hand = payload["dealerHand"][:]

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
                                payload["deck"] = deck
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
                                    "dealerHand": dealer_hand,
                                    "player": player_dict(player),
                                })

                        # Stand or bust or hit-to-21: resolve the hand
                        # Dealer plays
                        while hv(dealer_hand) < 17:
                            dealer_hand.append(deck.pop(0))
                        dealer_val = hv(dealer_hand)
                        cost = TYPES[ticket_row["ticket_type"]]["cost"]

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
                        payload["dealerHand"] = dealer_hand

                    # dice: check bet
                    elif payload.get("mode") == "dice":
                        bet = str(data.get("bet", ""))
                        if payload["_isTriple"]:
                            correct = (bet == "triple")
                        else:
                            correct = (bet == "big" and payload["_isBig"]) or (bet == "small" and not payload["_isBig"])
                        cost = TYPES[ticket_row["ticket_type"]]["cost"]
                        if correct:
                            win = cost * 2  # 猜中 = 2倍票价
                        else:
                            win = 0

                    # ssq - already server-determined
                    elif payload.get("mode") == "ssq":
                        win = payload["_win"]

                    # baccarat / slots / pinball / wheel - win already set
                    elif payload.get("mode") == "baccarat":
                        win = payload["_win"]

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
                    conn.execute("UPDATE tickets SET settled=1,settled_at=?,win=? WHERE id=?", (now, win, ticket_row["id"]))
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
                resp["dealerHand"] = final_payload.get("dealerHand", [])
            return self.send_json(resp)

        return self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    init_db()
    print(f"Scratch Game v2.0 listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
