import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("ADMIN_PASS", "test-admin-password")

import app


class GameLogicTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.tempdir.name) / "game.db"
        app.init_db()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_generated_visuals_match_server_payout(self):
        for game_id in ("xiangfeng", "seven", "jinyu", "ten", "koi", "slots", "pinball", "wheel", "ssq"):
            for _ in range(250):
                payload, win = app.generate_ticket(game_id, 0, "GS-TEST")
                if game_id == "slots":
                    final = payload["_final"]
                    has_match = len(set(final)) < 3
                    self.assertEqual(bool(win), has_match)
                elif game_id == "pinball":
                    self.assertEqual(payload["slots"][payload["targetSlot"]]["payout"], win)
                elif game_id == "wheel":
                    self.assertEqual(payload["segments"][payload["targetSegment"]]["payout"], win)
                elif game_id == "ssq":
                    matches = payload["_redMatches"]
                    blue = payload["_blueMatch"]
                    self.assertEqual(bool(win), matches >= 3 or blue)

    def test_redpacket_lucky_packet_is_private_and_consistent(self):
        for _ in range(250):
            payload, win = app.generate_ticket("redpacket", 0, "GS-TEST")
            self.assertEqual(len(payload["packets"]), 12)
            self.assertEqual(len({packet["id"] for packet in payload["packets"]}), 12)
            public_payload = {key: value for key, value in payload.items() if not key.startswith("_")}
            self.assertNotIn("_luckyIndex", public_payload)
            if win:
                self.assertIn(payload["_luckyIndex"], range(12))
            else:
                self.assertEqual(payload["_luckyIndex"], -1)

    def test_probability_config_rejects_more_than_one_hundred_percent(self):
        with self.assertRaises(ValueError):
            app.set_config({"lose": 0.8, "break_even": 0.5})

    def test_roll_payout_is_bounded_with_large_user_multiplier(self):
        app.set_config({"user_GS-TEST_rate": 10})
        wins = sum(app.roll_payout("xiangfeng", 999, "GS-TEST")[0] > 0 for _ in range(5000))
        self.assertLessEqual(wins / 5000, 0.97)

    def test_twentyfour_validation_uses_every_card_exactly_once(self):
        self.assertEqual(app.validate_24_answer("(1+2+3)*4", [1, 2, 3, 4]), (True, "回答正确"))
        self.assertFalse(app.validate_24_answer("6*4", [1, 2, 3, 4])[0])
        self.assertFalse(app.validate_24_answer("__import__('os')", [1, 2, 3, 4])[0])

    def test_player_achievements_are_derived_from_stats(self):
        with app.db() as conn:
            conn.execute(
                """INSERT INTO players(public_id,nickname,token_hash,coins,xp,level,played,wins,best,streak,seen,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "GS-ACH",
                    "成就测试",
                    app.token_hash("token"),
                    1200,
                    500,
                    5,
                    12,
                    4,
                    1500,
                    3,
                    json.dumps(list(app.TYPES)[:5]),
                    1,
                    1,
                ),
            )
            player = conn.execute("SELECT * FROM players WHERE public_id='GS-ACH'").fetchone()
        data = app.player_dict(player)
        unlocked = {achievement["id"] for achievement in data["achievements"]}
        self.assertTrue({"first_ticket", "first_win", "collector", "hot_streak", "big_winner", "level_five", "coin_stack"} <= unlocked)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(cls.tempdir.name) / "api.db"
        app.init_db()
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.tempdir.cleanup()

    @classmethod
    def request(cls, path, data=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"http://127.0.0.1:{cls.port}{path}",
            data=None if data is None else json.dumps(data).encode(),
            headers=headers,
            method="GET" if data is None else "POST",
        )
        with cls.opener.open(request) as response:
            return response.status, json.load(response)

    def test_identity_daily_gift_and_idempotent_settlement(self):
        _, registered = self.request("/api/register", {"nickname": "测试玩家"})
        token = registered["token"]
        self.assertTrue(registered["player"]["dailyGiftAvailable"])

        _, gift = self.request("/api/daily-gift", {}, token)
        self.assertEqual(gift["gift"], 80)
        self.assertFalse(gift["player"]["dailyGiftAvailable"])
        with self.assertRaises(urllib.error.HTTPError) as duplicate:
            self.request("/api/daily-gift", {}, token)
        self.assertEqual(duplicate.exception.code, 409)

        _, issued = self.request("/api/tickets", {"type": "xiangfeng"}, token)
        ticket_id = issued["ticketId"]
        _, first = self.request(f"/api/tickets/{ticket_id}/finish", {}, token)
        _, second = self.request(f"/api/tickets/{ticket_id}/finish", {}, token)
        self.assertEqual(first["player"]["played"], 1)
        self.assertEqual(second["player"]["played"], 1)
        self.assertEqual(first["player"]["coins"], second["player"]["coins"])

    def test_private_game_results_are_not_sent_before_choice(self):
        for index, (game_id, forbidden) in enumerate({
            "blackjack": ("_deck", "_dealerHand", "dealerHand"),
            "dice": ("_dice", "dice"),
            "baccarat": ("_result", "result", "playerHand", "bankerHand"),
            "twentyfour": ("_numbers",),
            "pusher": ("_targetX", "_tolerance"),
            "claw": ("_winningSlot", "isWinner"),
            "redpacket": ("_luckyIndex", "luckyPacket"),
        }.items()):
            _, registered = self.request("/api/register", {"nickname": f"保密测试{index}"})
            token = registered["token"]
            _, issued = self.request("/api/tickets", {"type": game_id}, token)
            for key in forbidden:
                self.assertNotIn(key, issued["ticket"])

    def test_catalog_contains_restored_arcade_games(self):
        _, catalog = self.request("/api/catalog")
        game_ids = {game["id"] for game in catalog["games"]}
        self.assertTrue({"twentyfour", "pusher", "claw", "redpacket"} <= game_ids)


if __name__ == "__main__":
    unittest.main()
