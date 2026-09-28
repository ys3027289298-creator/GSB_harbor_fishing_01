"""Harbor fishing game core logic.

Rules implemented here (each corresponds to a formerly buggy behavior):

1. Tide windows are half-open: a boat may sail at ``start`` but not at ``end``.
2. Sailing deducts fuel exactly once, from the departing boat only.
3. A damaged net cannot catch anything.
4. Fish migration is computed from the pre-migration map, never from the
   map that is already being mutated.
5. Quota settlement counts both the catch and the bycatch.
6. An order can be delivered exactly once.
7. A storm cancellation refunds the fuel paid for the interrupted trip.
8. Save/load restores boat position and fuel (and every other piece of
   state, including the RNG) consistently.

All mutating operations accept an optional ``op_id``. Repeating an
operation with the same ``op_id`` is a no-op that returns the original
result, which makes sail/deliver/cancel idempotent. ``snapshot`` and
``rollback`` allow tide, fuel, quota, order and position state to be
rolled back.
"""

import copy
import json
import random

TIDE_WINDOWS = ((6, 11), (16, 21))
SAIL_FUEL_COST = 10
NET_CAPACITY = 5
MAX_BYCATCH = 2
DEFAULT_FUEL = 30


class GameError(Exception):
    pass


class Boat:
    def __init__(self, name, fuel=DEFAULT_FUEL):
        self.name = name
        self.fuel = fuel
        self.position = "harbor"
        self.cargo = 0
        self.paid_fuel = 0

    def to_dict(self):
        return {
            "name": self.name,
            "fuel": self.fuel,
            "position": self.position,
            "cargo": self.cargo,
            "paid_fuel": self.paid_fuel,
        }

    @classmethod
    def from_dict(cls, data):
        boat = cls(data["name"], data["fuel"])
        boat.position = data["position"]
        boat.cargo = data["cargo"]
        boat.paid_fuel = data.get("paid_fuel", 0)
        return boat


class Net:
    def __init__(self, name, damaged=False):
        self.name = name
        self.damaged = damaged

    def to_dict(self):
        return {"name": self.name, "damaged": self.damaged}

    @classmethod
    def from_dict(cls, data):
        return cls(data["name"], data["damaged"])


def _to_jsonable(value):
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    return value


def _from_jsonable(value):
    if isinstance(value, list):
        return tuple(_from_jsonable(item) for item in value)
    return value


class Game:
    def __init__(self, seed=0, width=5, height=5):
        self.rng = random.Random(seed)
        self.width = width
        self.height = height
        self.time = 8
        self.money = 0
        self.quota = 0
        self.storm = False
        self.boats = {}
        self.nets = {}
        self.fish_map = {}
        self.orders = {}
        self._journal = {}

    # -- setup helpers ---------------------------------------------------

    def add_boat(self, name, fuel=DEFAULT_FUEL):
        boat = Boat(name, fuel)
        self.boats[name] = boat
        return boat

    def add_net(self, name, damaged=False):
        net = Net(name, damaged)
        self.nets[name] = net
        return net

    def add_order(self, order_id, need, reward):
        self.orders[order_id] = {"need": need, "reward": reward, "delivered": False}

    def set_fish(self, cell, count):
        self.fish_map[cell] = count

    def _boat(self, name):
        try:
            return self.boats[name]
        except KeyError:
            raise GameError(f"unknown boat: {name}")

    def _net(self, name):
        try:
            return self.nets[name]
        except KeyError:
            raise GameError(f"unknown net: {name}")

    # -- idempotency -----------------------------------------------------

    def _once(self, op_id, action):
        if op_id is not None and op_id in self._journal:
            return self._journal[op_id]
        result = action()
        if op_id is not None:
            self._journal[op_id] = result
        return result

    # -- tide ------------------------------------------------------------

    def tide_open(self, t=None):
        t = self.time if t is None else t
        return any(start <= t < end for start, end in TIDE_WINDOWS)

    # -- commands --------------------------------------------------------

    def sail(self, boat_name, op_id=None):
        def do():
            boat = self._boat(boat_name)
            if self.storm:
                return "storm"
            if not self.tide_open():
                return "tide closed"
            if boat.position == "sea":
                return "already at sea"
            if boat.fuel < SAIL_FUEL_COST:
                return "insufficient fuel"
            boat.fuel -= SAIL_FUEL_COST
            boat.paid_fuel = SAIL_FUEL_COST
            boat.position = "sea"
            return "sailed"

        return self._once(op_id, do)

    def dock(self, boat_name, op_id=None):
        def do():
            boat = self._boat(boat_name)
            if boat.position != "sea":
                return "not at sea"
            boat.position = "harbor"
            boat.paid_fuel = 0
            return "docked"

        return self._once(op_id, do)

    def fish(self, boat_name, net_name, cell, op_id=None):
        def do():
            boat = self._boat(boat_name)
            net = self._net(net_name)
            if boat.position != "sea":
                return "not at sea"
            if net.damaged:
                return "net damaged"
            available = self.fish_map.get(cell, 0)
            if available <= 0:
                return "no fish"
            if self.quota <= 0:
                return "quota exhausted"
            catch = min(available, NET_CAPACITY)
            bycatch = self.rng.randint(0, MAX_BYCATCH)
            kept = min(catch + bycatch, self.quota)
            self.quota -= kept
            boat.cargo += kept
            self.fish_map[cell] = available - min(catch, kept)
            return f"caught {kept}"

        return self._once(op_id, do)

    def migrate(self):
        old_map = dict(self.fish_map)
        new_map = {}
        for cell in sorted(old_map):
            count = old_map[cell]
            if count <= 0:
                continue
            neighbors = self._neighbors(cell)
            dest = self.rng.choice(neighbors)
            new_map[dest] = new_map.get(dest, 0) + count
        self.fish_map = new_map
        return "migrated"

    def _neighbors(self, cell):
        x, y = cell
        out = []
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < self.width and 0 <= ny < self.height:
                out.append((nx, ny))
        return out

    def deliver(self, order_id, boat_name, op_id=None):
        def do():
            order = self.orders.get(order_id)
            if order is None:
                return "no such order"
            if order["delivered"]:
                return "already delivered"
            boat = self._boat(boat_name)
            if boat.position != "harbor":
                return "boat not in harbor"
            if boat.cargo < order["need"]:
                return "insufficient cargo"
            boat.cargo -= order["need"]
            order["delivered"] = True
            self.money += order["reward"]
            return "delivered"

        return self._once(op_id, do)

    def storm_cancel(self, op_id=None):
        def do():
            self.storm = True
            refunded = 0
            for boat in self.boats.values():
                if boat.position == "sea":
                    boat.position = "harbor"
                    boat.fuel += boat.paid_fuel
                    refunded += boat.paid_fuel
                    boat.paid_fuel = 0
            return f"storm: refunded {refunded}"

        return self._once(op_id, do)

    def advance(self, hours=1):
        self.time += hours
        self.storm = False
        return "ok"

    # -- rollback --------------------------------------------------------

    def snapshot(self):
        return copy.deepcopy(self.to_dict())

    def rollback(self, snap):
        self._load_dict(copy.deepcopy(snap))

    # -- persistence -----------------------------------------------------

    def to_dict(self):
        return {
            "time": self.time,
            "money": self.money,
            "quota": self.quota,
            "storm": self.storm,
            "width": self.width,
            "height": self.height,
            "boats": {name: boat.to_dict() for name, boat in self.boats.items()},
            "nets": {name: net.to_dict() for name, net in self.nets.items()},
            "fish_map": {f"{x},{y}": count for (x, y), count in self.fish_map.items()},
            "orders": copy.deepcopy(self.orders),
            "journal": dict(self._journal),
            "rng": _to_jsonable(self.rng.getstate()),
        }

    def _load_dict(self, data):
        self.time = data["time"]
        self.money = data["money"]
        self.quota = data["quota"]
        self.storm = data["storm"]
        self.width = data["width"]
        self.height = data["height"]
        self.boats = {name: Boat.from_dict(b) for name, b in data["boats"].items()}
        self.nets = {name: Net.from_dict(n) for name, n in data["nets"].items()}
        self.fish_map = {}
        for key, count in data["fish_map"].items():
            x, y = key.split(",")
            self.fish_map[(int(x), int(y))] = count
        self.orders = copy.deepcopy(data["orders"])
        self._journal = dict(data.get("journal", {}))
        self.rng.setstate(_from_jsonable(data["rng"]))

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)

    def load(self, path):
        with open(path, encoding="utf-8") as fh:
            self._load_dict(json.load(fh))

    # -- terminal interface ----------------------------------------------

    def status(self):
        boats = ", ".join(
            f"{b.name}@{b.position}(fuel={b.fuel},cargo={b.cargo})"
            for b in self.boats.values()
        )
        return (
            f"time={self.time} money={self.money} quota={self.quota} "
            f"storm={self.storm} boats=[{boats}]"
        )

    def execute(self, line):
        parts = line.split()
        if not parts:
            return ""
        op_id = None
        if parts[-1].startswith("#"):
            op_id = parts.pop()[1:]
        cmd, args = parts[0], parts[1:]
        try:
            if cmd == "sail":
                return self.sail(args[0], op_id)
            if cmd == "dock":
                return self.dock(args[0], op_id)
            if cmd == "fish":
                x, y = args[2].split(",")
                return self.fish(args[0], args[1], (int(x), int(y)), op_id)
            if cmd == "migrate":
                return self.migrate()
            if cmd == "storm":
                return self.storm_cancel(op_id)
            if cmd == "deliver":
                return self.deliver(args[0], args[1], op_id)
            if cmd == "advance":
                return self.advance(int(args[0]) if args else 1)
            if cmd == "save":
                self.save(args[0])
                return "saved"
            if cmd == "load":
                self.load(args[0])
                return "loaded"
            if cmd == "status":
                return self.status()
            return "unknown command"
        except (IndexError, ValueError, GameError) as exc:
            return f"error: {exc}"


def main():
    game = Game()
    print("harbor fishing - type commands (sail/dock/fish/migrate/storm/"
          "deliver/advance/save/load/status), 'quit' to exit")
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if line in ("quit", "exit"):
            break
        out = game.execute(line)
        if out:
            print(out)


if __name__ == "__main__":
    main()
