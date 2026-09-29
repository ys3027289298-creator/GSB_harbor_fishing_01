import os
import random
import tempfile
import unittest

from harbor.core import (
    DEFAULT_FUEL,
    MAX_BYCATCH,
    NET_CAPACITY,
    SAIL_FUEL_COST,
    TIDE_WINDOWS,
    Game,
)

SEED = 42


def make_game(seed=SEED, **kwargs):
    game = Game(seed=seed, **kwargs)
    game.add_boat("A")
    game.add_boat("B")
    game.add_net("n1")
    return game


class TideTest(unittest.TestCase):
    def test_tide_window_end_is_exclusive(self):
        game = make_game()
        start, end = TIDE_WINDOWS[0]
        game.time = end
        self.assertFalse(game.tide_open())
        self.assertEqual(game.sail("A"), "tide closed")
        self.assertEqual(game.boats["A"].position, "harbor")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL)

    def test_tide_window_start_is_inclusive(self):
        game = make_game()
        game.time = TIDE_WINDOWS[0][0]
        self.assertTrue(game.tide_open())
        self.assertEqual(game.sail("A"), "sailed")

    def test_tide_last_open_hour(self):
        game = make_game()
        game.time = TIDE_WINDOWS[0][1] - 1
        self.assertEqual(game.sail("A"), "sailed")


class FuelTest(unittest.TestCase):
    def test_fuel_deducted_once_not_per_boat(self):
        game = make_game()
        game.sail("A")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL - SAIL_FUEL_COST)
        self.assertEqual(game.boats["B"].fuel, DEFAULT_FUEL)

    def test_repeated_sail_does_not_double_charge(self):
        game = make_game()
        game.sail("A")
        self.assertEqual(game.sail("A"), "already at sea")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL - SAIL_FUEL_COST)

    def test_zero_fuel_cannot_sail(self):
        game = make_game()
        game.boats["A"].fuel = 0
        self.assertEqual(game.sail("A"), "insufficient fuel")
        self.assertEqual(game.boats["A"].position, "harbor")
        self.assertEqual(game.boats["A"].fuel, 0)

    def test_fuel_exactly_cost_sails_and_ends_at_zero(self):
        game = make_game()
        game.boats["A"].fuel = SAIL_FUEL_COST
        self.assertEqual(game.sail("A"), "sailed")
        self.assertEqual(game.boats["A"].fuel, 0)


class NetTest(unittest.TestCase):
    def test_damaged_net_cannot_catch(self):
        game = make_game()
        game.add_net("broken", damaged=True)
        game.quota = 100
        game.set_fish((1, 1), 10)
        game.sail("A")
        self.assertEqual(game.fish("A", "broken", (1, 1)), "net damaged")
        self.assertEqual(game.boats["A"].cargo, 0)
        self.assertEqual(game.fish_map[(1, 1)], 10)
        self.assertEqual(game.quota, 100)


class MigrationTest(unittest.TestCase):
    def test_migration_uses_pre_migration_map(self):
        # 2x1 grid: each cell's only neighbor is the other cell, so a
        # correct migration swaps the two schools exactly once. If the
        # already-mutated map were used, the school landing on (1, 0)
        # would be moved a second time and everything would pile up on
        # (0, 0).
        game = Game(seed=SEED, width=2, height=1)
        game.set_fish((0, 0), 3)
        game.set_fish((1, 0), 2)
        game.migrate()
        self.assertEqual(game.fish_map, {(0, 0): 2, (1, 0): 3})

    def test_migration_conserves_total_fish(self):
        game = make_game()
        game.set_fish((0, 0), 4)
        game.set_fish((2, 2), 6)
        game.set_fish((4, 4), 1)
        game.migrate()
        self.assertEqual(sum(game.fish_map.values()), 11)


class QuotaTest(unittest.TestCase):
    def test_quota_counts_catch_and_bycatch(self):
        game = make_game()
        game.quota = 100
        game.set_fish((1, 1), 10)
        game.sail("A")
        game.fish("A", "n1", (1, 1))
        cargo = game.boats["A"].cargo
        expected_bycatch = random.Random(SEED).randint(0, MAX_BYCATCH)
        self.assertGreater(expected_bycatch, 0)
        self.assertEqual(cargo, NET_CAPACITY + expected_bycatch)
        self.assertEqual(game.quota, 100 - cargo)

    def test_quota_exactly_exhausted(self):
        game = make_game()
        game.set_fish((1, 1), 10)
        bycatch = random.Random(SEED).randint(0, MAX_BYCATCH)
        game.quota = NET_CAPACITY + bycatch
        game.sail("A")
        game.fish("A", "n1", (1, 1))
        self.assertEqual(game.quota, 0)
        self.assertEqual(game.fish("A", "n1", (1, 1)), "quota exhausted")
        self.assertEqual(game.boats["A"].cargo, NET_CAPACITY + bycatch)


class OrderTest(unittest.TestCase):
    def test_same_order_cannot_be_delivered_twice(self):
        game = make_game()
        game.add_order("o1", need=3, reward=100)
        game.boats["A"].cargo = 5
        self.assertEqual(game.deliver("o1", "A"), "delivered")
        self.assertEqual(game.money, 100)
        self.assertEqual(game.deliver("o1", "A"), "already delivered")
        self.assertEqual(game.deliver("o1", "A", op_id="retry"), "already delivered")
        self.assertEqual(game.money, 100)
        self.assertEqual(game.boats["A"].cargo, 2)


class StormTest(unittest.TestCase):
    def test_storm_cancel_refunds_fuel(self):
        game = make_game()
        game.sail("A")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL - SAIL_FUEL_COST)
        game.storm_cancel()
        self.assertEqual(game.boats["A"].position, "harbor")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL)

    def test_storm_cancel_is_idempotent(self):
        game = make_game()
        game.sail("A")
        game.storm_cancel()
        game.storm_cancel()
        game.storm_cancel(op_id="storm-again")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL)
        self.assertEqual(game.boats["A"].position, "harbor")


class PersistenceTest(unittest.TestCase):
    def test_save_load_restores_position_and_fuel(self):
        game = make_game()
        game.quota = 50
        game.set_fish((1, 1), 10)
        game.add_order("o1", need=2, reward=40)
        game.sail("A")
        game.fish("A", "n1", (1, 1))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            game.save(path)
            loaded = Game()
            loaded.load(path)
        self.assertEqual(loaded.boats["A"].position, "sea")
        self.assertEqual(loaded.boats["A"].fuel, DEFAULT_FUEL - SAIL_FUEL_COST)
        self.assertEqual(loaded.boats["A"].cargo, game.boats["A"].cargo)
        self.assertEqual(loaded.boats["B"].position, "harbor")
        self.assertEqual(loaded.boats["B"].fuel, DEFAULT_FUEL)
        self.assertEqual(loaded.quota, game.quota)
        self.assertEqual(loaded.fish_map, game.fish_map)
        self.assertEqual(loaded.orders, game.orders)

    def test_save_load_preserves_deterministic_rng(self):
        game = make_game()
        game.set_fish((1, 1), 5)
        game.set_fish((2, 2), 5)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "save.json")
            game.save(path)
            loaded = Game()
            loaded.load(path)
        game.migrate()
        loaded.migrate()
        self.assertEqual(loaded.fish_map, game.fish_map)


class IdempotencyTest(unittest.TestCase):
    def test_op_id_makes_sail_idempotent(self):
        game = make_game()
        self.assertEqual(game.sail("A", op_id="s1"), "sailed")
        self.assertEqual(game.sail("A", op_id="s1"), "sailed")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL - SAIL_FUEL_COST)

    def test_op_id_makes_deliver_idempotent(self):
        game = make_game()
        game.add_order("o1", need=2, reward=50)
        game.boats["A"].cargo = 4
        self.assertEqual(game.deliver("o1", "A", op_id="d1"), "delivered")
        self.assertEqual(game.deliver("o1", "A", op_id="d1"), "delivered")
        self.assertEqual(game.money, 50)
        self.assertEqual(game.boats["A"].cargo, 2)

    def test_op_id_makes_storm_cancel_idempotent(self):
        game = make_game()
        game.sail("A")
        first = game.storm_cancel(op_id="c1")
        self.assertEqual(game.storm_cancel(op_id="c1"), first)
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL)


class RollbackTest(unittest.TestCase):
    def test_rollback_restores_all_state(self):
        game = make_game()
        game.quota = 50
        game.set_fish((1, 1), 10)
        game.add_order("o1", need=2, reward=40)
        snap = game.snapshot()
        game.sail("A")
        game.fish("A", "n1", (1, 1))
        game.dock("A")
        game.deliver("o1", "A")
        game.advance(3)
        game.rollback(snap)
        self.assertEqual(game.time, 8)
        self.assertEqual(game.boats["A"].position, "harbor")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL)
        self.assertEqual(game.boats["A"].cargo, 0)
        self.assertEqual(game.quota, 50)
        self.assertFalse(game.orders["o1"]["delivered"])
        self.assertEqual(game.money, 0)
        self.assertEqual(game.fish_map, {(1, 1): 10})


class TerminalTest(unittest.TestCase):
    def test_execute_roundtrip(self):
        game = make_game()
        self.assertEqual(game.execute("sail A #op1"), "sailed")
        self.assertEqual(game.execute("sail A #op1"), "sailed")
        self.assertEqual(game.boats["A"].fuel, DEFAULT_FUEL - SAIL_FUEL_COST)
        self.assertEqual(game.execute("dock A"), "docked")
        self.assertIn("fuel=", game.execute("status"))
        self.assertEqual(game.execute("bogus"), "unknown command")


if __name__ == "__main__":
    unittest.main()
