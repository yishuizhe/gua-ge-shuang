#!/usr/bin/env python3
import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import time
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("SCRATCH_DB", ROOT / "data" / "scratch_game.db"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8089"))

TYPES = {
    "xiangfeng": {"name": "喜相逢", "icon": "🏮", "cost": 10, "kind": "match", "jackpot": 1000},
    "seven": {"name": "数字 7", "icon": "7️⃣", "cost": 15, "kind": "numbers", "jackpot": 3000},
    "jinyu": {"name": "金玉满堂", "icon": "🧧", "cost": 20, "kind": "triple", "jackpot": 2000},
    "ten": {"name": "好运十倍", "icon": "⚡", "cost": 30, "kind": "multi", "jackpot": 5000},
    "koi": {"name": "锦鲤驾到", "icon": "🐟", "cost": 50, "kind": "koi", "jackpot": 10000},
    "twentyfour": {"name": "24点挑战", "icon": "🧠", "cost": 10, "kind": "twentyfour", "jackpot": 50},
    "pusher": {"name": "推币机", "icon": "🪙", "cost": 10, "kind": "pusher", "jackpot": 1000},
    "claw": {"name": "抓娃娃机", "icon": "🕹️", "cost": 20, "kind": "claw", "jackpot": 2000},
}

PUZZLES_24 = [
    ([3, 3, 8, 8], ["8 ÷ (3 - 8 ÷ 3)", "(8 - 3) × (8 - 3)", "8 + 8 + 3 + 3", "8 × 3 + 8 ÷ 3"], 0),
    ([1, 5, 5, 5], ["5 × 5 - 5 ÷ 1", "5 × (5 - 1 ÷ 5)", "(5 - 1) × 5 + 5", "5 + 5 + 5 + 1"], 1),
    ([1, 3, 4, 6], ["6 ÷ (1 - 3 ÷ 4)", "(6 - 1) × 4 + 3", "6 × 4 + 3 - 1", "(6 + 3 - 1) × 4"], 0),
    ([2, 3, 4, 6], ["6 ÷ 2 × (4 + 3)", "6 × 4 ÷ (3 - 2)", "(6 - 2) × (4 + 3)", "6 + 4 × 3 + 2"], 1),
    ([2, 3, 3, 8], ["8 × 3 × (3 - 2)", "8 × 3 + 3 - 2", "(8 - 2) × (3 + 3)", "8 + 3 × 3 + 2"], 0),
    ([2, 2, 5, 10], ["(10 - 2) × (5 - 2)", "(10 + 2) × (5 - 2)", "10 ÷ 2 × 5 - 2", "10 + 5 × 2 + 2"], 0),
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
            CREATE INDEX IF NOT EXISTS idx_records_player ON records(player_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_players_rank ON players(level DESC, xp DESC, best DESC);
            """
        )


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def player_dict(row, include_private=True):
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
        "seen": json.loads(row["seen"] or "[]"),
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
        }
    return data


def records_for(conn, player_id):
    rows = conn.execute(
        "SELECT ticket_type, win, created_at FROM records WHERE player_id=? ORDER BY id DESC LIMIT 8",
        (player_id,),
    ).fetchall()
    return [
        {
            "icon": TYPES[row["ticket_type"]]["icon"],
            "name": TYPES[row["ticket_type"]]["name"],
            "win": row["win"],
            "time": time.strftime("%H:%M", time.localtime(row["created_at"])),
        }
        for row in rows
    ]


def roll_payout(ticket_type, streak):
    """Return a server-authoritative payout.

    Base distribution: 62% lose, 23% break even, 10% 2x, 4% 5x,
    0.9% 10x and 0.1% jackpot. Streak only shifts up to 4 percentage
    points from losing tickets into the break-even tier.
    """
    config = TYPES[ticket_type]
    cost = config["cost"]
    pity = min(0.04, streak * 0.002)
    roll = random.random()
    if roll < 0.62 - pity:
        return 0, "未中奖"
    if roll < 0.85:
        return cost, "回本"
    if roll < 0.95:
        return cost * 2, "小奖"
    if roll < 0.99:
        return cost * 5, "中奖"
    if roll < 0.999:
        return cost * 10, "大奖"
    return config["jackpot"], "最高奖"


def generate_ticket(ticket_type, streak):
    kind = TYPES[ticket_type]["kind"]
    cells, winning = [], []
    if kind == "twentyfour":
        numbers, options, answer = random.choice(PUZZLES_24)
        return {
            "mode": "twentyfour", "numbers": numbers, "options": options,
            "_answer": answer, "resultText": "算式等于 24 即挑战成功",
            "prizeTier": "技巧奖", "maxPrize": TYPES[ticket_type]["jackpot"],
        }, 50
    win, prize_tier = roll_payout(ticket_type, streak)
    if kind == "pusher":
        return {
            "mode": "pusher", "drop": random.randint(1, 5),
            "resultText": "推板前进，看看有多少爽币落袋",
            "prizeTier": prize_tier, "maxPrize": TYPES[ticket_type]["jackpot"],
        }, win
    if kind == "claw":
        toys = ["🐼", "🦁", "🐰", "🐻", "🐸"]
        winning_slot = random.randrange(len(toys)) if win else -1
        return {
            "mode": "claw", "toys": toys, "_winningSlot": winning_slot,
            "resultText": "选中娃娃，启动机械爪",
            "prizeTier": prize_tier, "maxPrize": TYPES[ticket_type]["jackpot"],
        }, win
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
            if prize_tier == "最高奖":
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
            if prize_tier == "最高奖":
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
        # Keep every non-winning symbol below three occurrences.
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
        target, multiplier = random.randint(1, 12), 2
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
    else:
        cells = [{"symbol": random.choice(["🌊", "🫧", "🪷", "🐚"]), "tag": "好运池", "prize": 0} for _ in range(9)]
        if win:
            if prize_tier == "最高奖":
                first, second = random.sample(range(9), 2)
                cells[first] = {"symbol": "🐟", "tag": "锦鲤", "prize": win // 4}
                cells[second] = {"symbol": "🐟", "tag": "锦鲤", "prize": win // 4}
            else:
                cells[random.randrange(9)] = {"symbol": "🐟", "tag": "锦鲤", "prize": win}
        result_text = "双锦鲤驾到，奖金再翻倍" if prize_tier == "最高奖" else "锦鲤上岸，好运到家" if win else "锦鲤游走了，下一池再见"
    return {
        "cells": cells,
        "winning": winning,
        "resultText": result_text,
        "prizeTier": prize_tier,
        "maxPrize": TYPES[ticket_type]["jackpot"],
    }, win


class Handler(SimpleHTTPRequestHandler):
    server_version = "ScratchGame/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")

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

    def auth_player(self, conn):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return conn.execute("SELECT * FROM players WHERE token_hash=?", (token_hash(header[7:]),)).fetchone()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self.send_json({"ok": True})
        if path == "/api/leaderboard":
            with db() as conn:
                rows = conn.execute(
                    "SELECT * FROM players ORDER BY level DESC, xp DESC, best DESC, wins DESC LIMIT 20"
                ).fetchall()
            return self.send_json({"leaderboard": [player_dict(row, False) for row in rows]})
        if path == "/api/me":
            with db() as conn:
                player = self.auth_player(conn)
                if not player:
                    return self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return self.send_json({"player": player_dict(player), "records": records_for(conn, player["id"])})
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        data = self.read_json()
        if data is None:
            return self.send_json({"error": "invalid_json"}, HTTPStatus.BAD_REQUEST)
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
            return self.send_json({"token": token, "player": player_dict(player), "records": []}, HTTPStatus.CREATED)
        if path == "/api/tickets":
            ticket_type = str(data.get("type", ""))
            if ticket_type not in TYPES:
                return self.send_json({"error": "unknown_ticket"}, HTTPStatus.BAD_REQUEST)
            with db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                player = self.auth_player(conn)
                if not player:
                    return self.send_json({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                cost = TYPES[ticket_type]["cost"]
                coins = player["coins"]
                gift = 0
                if coins < cost:
                    gift = 100
                    coins += gift
                payload, win = generate_ticket(ticket_type, player["streak"])
                ticket_id = secrets.token_urlsafe(18)
                conn.execute("UPDATE players SET coins=?,updated_at=? WHERE id=?", (coins - cost, int(time.time()), player["id"]))
                conn.execute(
                    "INSERT INTO tickets(id,player_id,ticket_type,payload,win,created_at) VALUES(?,?,?,?,?,?)",
                    (ticket_id, player["id"], ticket_type, json.dumps(payload, ensure_ascii=False), win, int(time.time())),
                )
                player = conn.execute("SELECT * FROM players WHERE id=?", (player["id"],)).fetchone()
            public_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
            return self.send_json({"ticketId": ticket_id, "ticket": public_payload, "player": player_dict(player), "gift": gift})
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
                        try:
                            answer = int(data.get("answer", -1))
                        except (TypeError, ValueError):
                            answer = -1
                        if answer != payload["_answer"]:
                            win = 0
                    elif payload.get("mode") == "claw":
                        try:
                            slot = int(data.get("slot", -1))
                        except (TypeError, ValueError):
                            slot = -1
                        if slot != payload["_winningSlot"]:
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
                    conn.execute("UPDATE tickets SET settled=1,settled_at=?,win=? WHERE id=?", (now, win, ticket_row["id"]))
                    conn.execute(
                        "INSERT INTO records(player_id,ticket_type,win,created_at) VALUES(?,?,?,?)",
                        (player["id"], ticket_row["ticket_type"], win, now),
                    )
                player = conn.execute("SELECT * FROM players WHERE id=?", (player["id"],)).fetchone()
                ticket_row = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_row["id"],)).fetchone()
                records = records_for(conn, player["id"])
            return self.send_json({
                "win": ticket_row["win"],
                "player": player_dict(player),
                "records": records,
                "challengeReward": challenge_reward if "challenge_reward" in locals() else 0,
            })
        return self.send_json({"error": "not_found"}, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    init_db()
    print(f"Scratch Game listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
