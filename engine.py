#!/usr/bin/env python3
"""
深层游戏 / Deep Layer Game
AI-powered horror text adventure engine.

State-machine engine that processes commands and returns JSON results.
Narrative generation is the AI DM's job. This engine handles game logic only.
"""

import json
import sys
import os
import random
import copy
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ENGINE_DIR = Path(__file__).resolve().parent
EPISODES_DIR = ENGINE_DIR / "episodes"
STATE_FILE = ENGINE_DIR / "game_state.json"
SAVE_FILE = ENGINE_DIR / "game_save.json"

TIME_COSTS = {
    "move": 5,
    "look": 0,
    "look_target": 2,
    "investigate": 10,
    "take": 1,
    "use": 3,
    "talk": 5,
    "hide": 2,
    "run": 3,
    "rest": 30,
    "monster_action": 1,
}

STAMINA_COSTS = {
    "move": 5,
    "run": 20,
    "hide": 5,
    "investigate": 3,
    "rest": -40,  # negative = recovery
}

RUN_SUCCESS_BASE = 0.6
HALLUCINATION_THRESHOLD = 60
HALLUCINATION_CHANCE = 0.15
REST_DANGER_CHANCE = 0.25

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_response(success, command, result=None, state_update=None,
                  monster_updates=None, events_triggered=None,
                  encounter=None, warnings=None, game_over=False,
                  game_result=None, error=None):
    """Build the standard JSON response envelope."""
    resp = {
        "success": success,
        "command": command,
        "result": result or {},
        "state_update": state_update or {
            "time": None,
            "hp_change": 0,
            "san_change": 0,
            "stamina_change": 0,
            "items_gained": [],
            "items_lost": [],
            "clues_found": [],
            "rules_discovered": [],
        },
        "monster_updates": monster_updates or [],
        "events_triggered": events_triggered or [],
        "encounter": encounter,
        "warnings": warnings or [],
        "game_over": game_over,
    }
    if game_result is not None:
        resp["game_result"] = game_result
    if error is not None:
        resp["error"] = error
    return resp


def parse_time(t):
    """Parse 'HH:MM' into total minutes from midnight."""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def format_time(total_minutes):
    """Format total minutes from midnight into 'HH:MM'."""
    total_minutes = total_minutes % (24 * 60)
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def advance_time(current_time_str, minutes):
    """Advance game clock by given minutes. Returns new time string."""
    return format_time(parse_time(current_time_str) + minutes)


def normalize_overnight(minutes, start_minutes):
    """Convert a time to a linear scale relative to start, handling overnight wrap.
    For a game starting at 23:00 (1380 min): 23:05 -> 5, 00:00 -> 60, 06:00 -> 420."""
    if minutes < start_minutes:
        return minutes + 24 * 60 - start_minutes
    return minutes - start_minutes


def time_past_end(current_time_str, end_time_str, start_time_str="23:00"):
    """Check if current time has reached or passed the end time.
    Handles overnight spans (e.g. 23:00 start, 06:00 end)."""
    start = parse_time(start_time_str)
    cur = normalize_overnight(parse_time(current_time_str), start)
    end = normalize_overnight(parse_time(end_time_str), start)
    return cur >= end


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Episode Loader
# ---------------------------------------------------------------------------

class EpisodeData:
    """Holds all static data for an episode."""

    def __init__(self, episode_id):
        self.episode_id = episode_id
        self.base_dir = EPISODES_DIR / episode_id
        if not self.base_dir.is_dir():
            raise FileNotFoundError(f"Episode directory not found: {self.base_dir}")

        self.config = self._load("config.json")
        self.rooms = self._unwrap(self._load("rooms.json"), "rooms")
        self.monsters = self._unwrap(self._load("monsters.json", default={}), "monsters")
        self.npcs = self._unwrap(self._load("npcs.json", default={}), "npcs")
        self.items = self._unwrap(self._load("items.json", default={}), "items")
        self.clues = self._unwrap(self._load("clues.json", default={}), "clues")
        raw_events = self._load("events.json", default=[])
        unwrapped = self._unwrap(raw_events, "events") if isinstance(raw_events, dict) else raw_events
        if isinstance(unwrapped, dict):
            self.events = [{"id": k, **v} for k, v in unwrapped.items()]
        else:
            self.events = unwrapped

    @staticmethod
    def _unwrap(data, key):
        if isinstance(data, dict) and len(data) == 1 and key in data:
            return data[key]
        return data

    def _load(self, filename, default=None):
        path = self.base_dir / filename
        if not path.exists():
            if default is not None:
                return default
            raise FileNotFoundError(f"Required episode file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Game State
# ---------------------------------------------------------------------------

def new_game_state(episode, player_names=None):
    """Create a fresh game state from episode config.

    Args:
        episode: EpisodeData instance.
        player_names: list of player name strings. If None, uses config
                      defaults or falls back to ["Player 1", "Player 2"].
    """
    cfg = episode.config
    start = cfg["start_room"]

    # Determine player names
    players_cfg = cfg.get("players", {})
    default_count = players_cfg.get("default_count", 2)
    template = players_cfg.get("player_template", {})

    if player_names is None:
        default_names = players_cfg.get("default_names")
        if default_names:
            player_names = default_names
        else:
            player_names = [f"Player {i+1}" for i in range(default_count)]

    # Build player dicts from template or defaults
    players = {}
    for i, name in enumerate(player_names):
        player = {
            "hp": template.get("hp", 100),
            "max_hp": template.get("max_hp", 100),
            "san": template.get("san", 100),
            "max_san": template.get("max_san", 100),
            "stamina": template.get("stamina", 100),
            "max_stamina": template.get("max_stamina", 100),
            "location": start,
            "inventory": list(template.get("inventory", [])) if i == 0 else [],
            "discovered_clues": list(template.get("discovered_clues", [])),
            "discovered_rules": list(template.get("discovered_rules", [])),
            "status_effects": list(template.get("status_effects", [])),
        }
        # First player gets starting items from episode config if template has none
        if i == 0 and not player["inventory"]:
            player_start = cfg.get("player_start", {})
            player["inventory"] = list(player_start.get("items", []))
        players[name] = player

    state = {
        "episode_id": cfg["id"],
        "game_time": cfg["time_start"],
        "turn": 0,
        "phase": "exploration",
        "player_names": list(player_names),
        "players": players,
        "npcs": {},
        "monsters": {},
        "rooms_visited": [start],
        "events_triggered": [],
        "keys_found": [],
        "score": 0,
        "active_encounter": None,
        "game_over": False,
        "game_result": None,
        "rng_seed": random.randint(0, 2**31),
        # runtime item/room tracking (mutable copies)
        "room_items": {},
        "room_clues": {},
    }

    # seed NPCs into state
    for npc_id, npc in episode.npcs.items():
        # NPC stats may be nested under "stats" or at top level
        npc_stats = npc.get("stats", {})
        # initial_location takes priority, then location, then start room
        npc_location = npc.get("initial_location", npc.get("location", start))
        # trust may be nested under trust_level.initial or at top level
        npc_trust = npc.get("trust", 50)
        trust_level = npc.get("trust_level", {})
        if trust_level:
            npc_trust = trust_level.get("initial", npc_trust)
        state["npcs"][npc_id] = {
            "hp": npc_stats.get("hp", npc.get("hp", 80)),
            "san": npc_stats.get("san", npc.get("san", 70)),
            "location": npc_location,
            "alive": npc.get("alive", True),
            "found": False,
            "trust": npc_trust,
            "dialogue_state": npc.get("dialogue_state", "initial"),
        }

    # seed Monsters into state
    for m_id, m in episode.monsters.items():
        act_time = m.get("activation_time", "00:00")
        if act_time == "always":
            is_active = True
        else:
            is_active = normalize_overnight(parse_time(act_time),
                                            parse_time(cfg["time_start"])) == 0
        state["monsters"][m_id] = {
            "active": is_active,
            "location": m.get("initial_location"),
            "patrol_index": 0,
            "alert": False,
            "stare_counter": 0,
            "frozen_turns": 0,
            "cooldown_turns": 0,
        }

    # seed item locations
    for item_id, item in episode.items.items():
        loc = item.get("location")
        if loc:
            state["room_items"].setdefault(loc, []).append(item_id)

    # seed clue availability per room
    for clue_id, clue in episode.clues.items():
        loc = clue.get("location")
        if loc:
            state["room_clues"].setdefault(loc, []).append(clue_id)

    return state


def load_state():
    """Load game state from disk."""
    if not STATE_FILE.exists():
        return None
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    """Persist game state to disk."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Player helpers (operate on both players jointly)
# ---------------------------------------------------------------------------

def _first_player(state):
    """Return the name of the first (primary) player."""
    names = state.get("player_names")
    if names:
        return names[0]
    # Fallback: first key in players dict
    return next(iter(state["players"]))


def player_location(state):
    """Both players share a location. Return it."""
    return state["players"][_first_player(state)]["location"]


def set_player_location(state, room_id):
    for p in state["players"].values():
        p["location"] = room_id


def apply_hp(state, amount):
    for p in state["players"].values():
        p["hp"] = clamp(p["hp"] + amount, 0, p["max_hp"])


def apply_san(state, amount):
    for p in state["players"].values():
        p["san"] = clamp(p["san"] + amount, 0, p["max_san"])


def apply_stamina(state, amount):
    for p in state["players"].values():
        p["stamina"] = clamp(p["stamina"] + amount, 0, p["max_stamina"])


def get_san(state):
    return state["players"][_first_player(state)]["san"]


def get_stamina(state):
    return state["players"][_first_player(state)]["stamina"]


def get_hp(state):
    return state["players"][_first_player(state)]["hp"]


def get_inventory(state):
    """Merged inventory of all players."""
    inv = []
    for p in state["players"].values():
        for item in p["inventory"]:
            if item not in inv:
                inv.append(item)
    return inv


def add_item(state, item_id, holder=None):
    if holder is None:
        holder = _first_player(state)
    if item_id not in state["players"][holder]["inventory"]:
        state["players"][holder]["inventory"].append(item_id)


def remove_item(state, item_id):
    for p in state["players"].values():
        if item_id in p["inventory"]:
            p["inventory"].remove(item_id)
            return True
    return False


def has_item(state, item_id):
    return item_id in get_inventory(state)


def add_clue(state, clue_id):
    for p in state["players"].values():
        if clue_id not in p["discovered_clues"]:
            p["discovered_clues"].append(clue_id)


def has_clue(state, clue_id):
    return clue_id in state["players"][_first_player(state)]["discovered_clues"]


def add_rule(state, rule_id):
    for p in state["players"].values():
        if rule_id not in p["discovered_rules"]:
            p["discovered_rules"].append(rule_id)


def has_rule(state, rule_id):
    return rule_id in state["players"][_first_player(state)]["discovered_rules"]


def add_status(state, effect):
    for p in state["players"].values():
        if effect not in p["status_effects"]:
            p["status_effects"].append(effect)


def remove_status(state, effect):
    for p in state["players"].values():
        if effect in p["status_effects"]:
            p["status_effects"].remove(effect)


def has_status(state, effect):
    return effect in state["players"][_first_player(state)]["status_effects"]


# ---------------------------------------------------------------------------
# RNG
# ---------------------------------------------------------------------------

def get_rng(state):
    """Return a seeded Random instance, advancing the seed for next call."""
    rng = random.Random(state["rng_seed"])
    state["rng_seed"] = rng.randint(0, 2**31)
    return rng


# ---------------------------------------------------------------------------
# Monster AI
# ---------------------------------------------------------------------------

def tick_monsters(state, episode, rng):
    """Advance all active monsters one step. Returns list of update dicts."""
    updates = []
    loc = player_location(state)
    current_minutes = parse_time(state["game_time"])

    for m_id, m_state in state["monsters"].items():
        m_def = episode.monsters.get(m_id)
        if m_def is None:
            continue

        # activation check
        if not m_state["active"]:
            start_m = parse_time(episode.config.get("time_start", "23:00"))
            act_norm = normalize_overnight(parse_time(m_def.get("activation_time", "00:00")), start_m)
            cur_norm = normalize_overnight(current_minutes, start_m)
            if cur_norm >= act_norm:
                m_state["active"] = True
            else:
                continue

        # frozen countdown
        if m_state["frozen_turns"] > 0:
            m_state["frozen_turns"] -= 1
            updates.append({
                "monster_id": m_id,
                "action": "frozen",
                "location": m_state["location"],
                "turns_remaining": m_state["frozen_turns"],
            })
            continue

        # cooldown countdown (after resolved encounter, monster won't re-trigger)
        if m_state.get("cooldown_turns", 0) > 0:
            m_state["cooldown_turns"] -= 1

        # patrol movement
        if m_def.get("type") == "patrol" and m_state["location"] != loc:
            route = m_def.get("patrol_route", [])
            if route:
                idx = m_state["patrol_index"]
                speed = m_def.get("stats", {}).get("speed", 1)
                for _ in range(speed):
                    idx = (idx + 1) % len(route)
                m_state["patrol_index"] = idx
                old_loc = m_state["location"]
                m_state["location"] = route[idx]
                updates.append({
                    "monster_id": m_id,
                    "action": "patrol",
                    "from": old_loc,
                    "to": m_state["location"],
                })

        # check if monster now shares room with player (respect cooldown)
        if (m_state["location"] == loc
                and state["active_encounter"] is None
                and m_state.get("cooldown_turns", 0) <= 0):
            updates.append({
                "monster_id": m_id,
                "action": "encounter_start",
                "location": loc,
                "description_distant": m_def.get("description_distant", ""),
            })

    return updates


def check_encounter_start(state, episode, monster_updates):
    """If a monster entered the player's room, set up encounter state."""
    loc = player_location(state)
    for upd in monster_updates:
        if upd.get("action") == "encounter_start":
            m_id = upd["monster_id"]
            m_def = episode.monsters.get(m_id, {})
            state["active_encounter"] = {
                "monster_id": m_id,
                "monster_name": m_def.get("name", m_id),
                "distance": "far",
                "turn_count": 0,
                "stare_counter": 0,
                "description_distant": m_def.get("description_distant", ""),
                "description_close": m_def.get("description_close", ""),
                "ambush": False,
            }
            state["phase"] = "encounter"
            return state["active_encounter"]
    return None


# ---------------------------------------------------------------------------
# Event system
# ---------------------------------------------------------------------------

def check_events(state, episode, context=None):
    """Check and fire any events whose triggers are met. Returns list of fired events."""
    fired = []
    loc = player_location(state)
    current_time = state["game_time"]

    for evt in episode.events:
        evt_id = evt["id"]
        if evt_id in state["events_triggered"]:
            continue

        trigger = evt.get("trigger", {})
        t_type = trigger.get("type")
        matched = False

        if t_type == "enter_room":
            if context == "enter_room" and trigger.get("room") == loc:
                cond = trigger.get("condition")
                if cond == "first_time":
                    # the room was just added to rooms_visited this turn
                    matched = True
                elif cond is None:
                    matched = True

        elif t_type == "time_reached":
            target = trigger.get("time")
            if target:
                start_time = episode.config.get("time_start", "23:00")
                start_m = parse_time(start_time)
                cur_norm = normalize_overnight(parse_time(current_time), start_m)
                tgt_norm = normalize_overnight(parse_time(target), start_m)
                if cur_norm >= tgt_norm:
                    matched = True

        elif t_type == "item_used":
            if context == "item_used" and trigger.get("item") == trigger.get("item"):
                matched = True

        elif t_type == "clue_found":
            if trigger.get("clue") in state["players"][_first_player(state)]["discovered_clues"]:
                matched = True

        elif t_type == "npc_found":
            npc_id = trigger.get("npc")
            if npc_id and state["npcs"].get(npc_id, {}).get("found"):
                matched = True

        if matched:
            state["events_triggered"].append(evt_id)
            fired.append(evt)

    return fired


def apply_event_effects(state, episode, fired_events, rng):
    """Apply side effects from fired events. Returns processed event summaries + encounter info."""
    summaries = []
    encounter = None

    for evt in fired_events:
        effect = evt.get("effect", {})
        e_type = effect.get("type")
        summary = {"event_id": evt["id"], "type": e_type}

        if e_type == "monster_encounter":
            m_id = effect.get("monster")
            m_def = episode.monsters.get(m_id, {})
            ambush = effect.get("ambush", False)
            # place monster in player room
            if m_id in state["monsters"]:
                state["monsters"][m_id]["location"] = player_location(state)
                state["monsters"][m_id]["active"] = True
            state["active_encounter"] = {
                "monster_id": m_id,
                "monster_name": m_def.get("name", m_id),
                "distance": "close" if ambush else "far",
                "turn_count": 0,
                "stare_counter": 0,
                "description_distant": m_def.get("description_distant", ""),
                "description_close": m_def.get("description_close", ""),
                "ambush": ambush,
            }
            state["phase"] = "encounter"
            encounter = state["active_encounter"]
            summary["monster_id"] = m_id
            summary["ambush"] = ambush
            summary["description_hint"] = effect.get("description_hint", "")

        elif e_type == "activate_monster":
            m_id = effect.get("monster")
            if m_id in state["monsters"]:
                state["monsters"][m_id]["active"] = True
            summary["monster_id"] = m_id

        elif e_type == "global_event":
            san_dmg = effect.get("san_damage", 0)
            if san_dmg:
                apply_san(state, -san_dmg)
                summary["san_damage"] = san_dmg
            summary["description"] = effect.get("description", "")
            if effect.get("monster_boost"):
                for ms in state["monsters"].values():
                    if ms["active"]:
                        ms["alert"] = True
                summary["monster_boost"] = True

        elif e_type == "san_damage":
            amt = effect.get("amount", 0)
            apply_san(state, -amt)
            summary["san_damage"] = amt

        elif e_type == "hp_damage":
            amt = effect.get("amount", 0)
            apply_hp(state, -amt)
            summary["hp_damage"] = amt

        elif e_type == "give_item":
            item_id = effect.get("item")
            if item_id:
                add_item(state, item_id)
                summary["item"] = item_id

        elif e_type == "reveal_clue":
            clue_id = effect.get("clue")
            if clue_id:
                add_clue(state, clue_id)
                summary["clue"] = clue_id

        summaries.append(summary)

    return summaries, encounter


# ---------------------------------------------------------------------------
# Hallucination system
# ---------------------------------------------------------------------------

def check_hallucination(state, rng):
    """Low SAN can cause hallucination events."""
    san = get_san(state)
    if san >= HALLUCINATION_THRESHOLD:
        return None

    # lower SAN = higher chance
    adjusted_chance = HALLUCINATION_CHANCE * (1 + (HALLUCINATION_THRESHOLD - san) / HALLUCINATION_THRESHOLD)
    if rng.random() >= adjusted_chance:
        return None

    options = [
        {"type": "phantom_sound", "description": "幻听", "detail": "footsteps_behind"},
        {"type": "phantom_sight", "description": "幻视", "detail": "shadow_movement"},
        {"type": "phantom_whisper", "description": "耳语", "detail": "name_called"},
        {"type": "false_monster", "description": "虚假怪物", "detail": "figure_in_doorway"},
        {"type": "distortion", "description": "空间扭曲", "detail": "corridor_stretching"},
    ]
    if san < 30:
        options.append({"type": "severe_hallucination", "description": "严重幻觉", "detail": "walls_bleeding"})

    chosen = rng.choice(options)
    san_cost = rng.randint(1, 3)
    apply_san(state, -san_cost)
    chosen["san_cost"] = san_cost
    return chosen


# ---------------------------------------------------------------------------
# Win/lose condition checks
# ---------------------------------------------------------------------------

def check_game_over(state, episode):
    """Check if the game has ended."""
    if state["game_over"]:
        return True, state["game_result"]

    # death
    if get_hp(state) <= 0:
        state["game_over"] = True
        state["game_result"] = "death"
        return True, "death"

    # insanity
    if get_san(state) <= 0:
        state["game_over"] = True
        state["game_result"] = "insanity"
        return True, "insanity"

    # time up
    start_time = episode.config.get("time_start", "23:00")
    end_time = episode.config.get("time_end", "06:00")
    if time_past_end(state["game_time"], end_time, start_time):
        state["game_over"] = True
        state["game_result"] = "time_up"
        return True, "time_up"

    return False, None


def check_win_condition(state, episode):
    """Check if win condition is met."""
    win = episode.config.get("win_condition", {})
    w_type = win.get("type")

    if w_type == "collect_and_escape":
        required = win.get("required_keys", [])
        exit_room = win.get("exit_room")
        if all(k in state["keys_found"] for k in required):
            if player_location(state) == exit_room:
                return True
    elif w_type == "survive":
        # checked via time_up
        pass
    elif w_type == "escape":
        exit_room = win.get("exit_room")
        if player_location(state) == exit_room:
            return True

    return False


def calculate_score(state, episode):
    """Calculate final score."""
    scoring = episode.config.get("scoring", {})
    score = state["score"]

    if state["game_result"] in ("win", "survive"):
        score += scoring.get("survive", 30)
        score += scoring.get("complete", 20)

    # rules discovered
    primary = _first_player(state)
    rules_found = len(state["players"][primary]["discovered_rules"])
    score += rules_found * scoring.get("discover_rule", 4)

    # NPCs alive
    all_alive = all(n.get("alive", True) for n in state["npcs"].values())
    if all_alive and state["npcs"]:
        score += scoring.get("all_npcs_alive", 10)

    # hidden clues
    clues_found = len(state["players"][primary]["discovered_clues"])
    score += clues_found * scoring.get("hidden_clue", 2)

    return score


# ---------------------------------------------------------------------------
# Command Handlers
# ---------------------------------------------------------------------------

def cmd_init(args):
    """Initialize a new game from an episode."""
    if not args:
        return make_response(False, "init", error="Usage: init <episode_id> [--players \"Name1,Name2\"]")

    episode_id = args[0]

    # Parse --players option
    player_names = None
    for i, arg in enumerate(args[1:], 1):
        if arg == "--players" and i + 1 < len(args):
            player_names = [n.strip() for n in args[i + 1].split(",") if n.strip()]
            break

    try:
        episode = EpisodeData(episode_id)
    except FileNotFoundError as e:
        return make_response(False, "init", error=str(e))

    state = new_game_state(episode, player_names=player_names)
    save_state(state)

    start_room = episode.rooms.get(episode.config["start_room"], {})
    names = state["player_names"]
    return make_response(True, "init", result={
        "episode_id": episode_id,
        "episode_name": episode.config.get("name", episode_id),
        "start_room": episode.config["start_room"],
        "start_room_name": start_room.get("name", ""),
        "start_room_description": start_room.get("description_full", start_room.get("description_brief", "")),
        "start_time": episode.config["time_start"],
        "end_time": episode.config["time_end"],
        "player_names": names,
        "player_count": len(names),
        "message": f"Game initialized. Players: {', '.join(names)}.",
    })


def cmd_look(state, episode, args):
    """Observe current room or examine a specific target."""
    loc = player_location(state)
    room = episode.rooms.get(loc)
    if room is None:
        return make_response(False, "look", error=f"Current room '{loc}' not found in episode data")

    # look at specific target
    if args:
        target = args[0]
        time_cost = TIME_COSTS["look_target"]
        state["game_time"] = advance_time(state["game_time"], time_cost)
        state["turn"] += 1

        # check features
        features = room.get("features", [])
        if target in features:
            return make_response(True, "look", result={
                "type": "feature",
                "target": target,
                "room": loc,
                "room_name": room.get("name", loc),
                "found": True,
            }, state_update={
                "time": state["game_time"],
                "hp_change": 0, "san_change": 0, "stamina_change": 0,
                "items_gained": [], "items_lost": [],
                "clues_found": [], "rules_discovered": [],
            })

        # check clues (surface look)
        room_clues = state["room_clues"].get(loc, [])
        for clue_id in room_clues:
            clue = episode.clues.get(clue_id, {})
            if clue_id == target or clue.get("name", "") == target:
                desc = clue.get("description_surface", "")
                clues_found = []
                if not clue.get("requires_investigation", True) and not has_clue(state, clue_id):
                    add_clue(state, clue_id)
                    clues_found.append(clue_id)
                    state["score"] += clue.get("score_value", 0)
                return make_response(True, "look", result={
                    "type": "clue",
                    "target": clue_id,
                    "name": clue.get("name", clue_id),
                    "description": desc,
                    "content": clue.get("content", "") if not clue.get("requires_investigation", True) else "",
                    "needs_investigation": clue.get("requires_investigation", True),
                }, state_update={
                    "time": state["game_time"],
                    "hp_change": 0, "san_change": 0, "stamina_change": 0,
                    "items_gained": [], "items_lost": [],
                    "clues_found": clues_found, "rules_discovered": [],
                })

        # check items on ground
        room_items = state["room_items"].get(loc, [])
        for item_id in room_items:
            item = episode.items.get(item_id, {})
            if item_id == target or item.get("name", "") == target:
                return make_response(True, "look", result={
                    "type": "item",
                    "target": item_id,
                    "name": item.get("name", item_id),
                    "description": item.get("description", ""),
                })

        # check NPCs
        for npc_id, npc_state in state["npcs"].items():
            npc_def = episode.npcs.get(npc_id, {})
            if (npc_id == target or npc_def.get("name", "") == target) and npc_state["location"] == loc:
                return make_response(True, "look", result={
                    "type": "npc",
                    "target": npc_id,
                    "name": npc_def.get("name", npc_id),
                    "description": npc_def.get("description", ""),
                    "alive": npc_state["alive"],
                    "trust": npc_state["trust"],
                })

        return make_response(False, "look", result={
            "type": "not_found",
            "target": target,
            "room": loc,
        }, error=f"Cannot see '{target}' here")

    # general look
    room_items = state["room_items"].get(loc, [])
    items_visible = []
    for item_id in room_items:
        item = episode.items.get(item_id, {})
        items_visible.append({"id": item_id, "name": item.get("name", item_id)})

    clues_visible = []
    for clue_id in state["room_clues"].get(loc, []):
        clue = episode.clues.get(clue_id, {})
        clues_visible.append({
            "id": clue_id,
            "name": clue.get("name", clue_id),
            "description_surface": clue.get("description_surface", ""),
        })

    npcs_here = []
    for npc_id, npc_state in state["npcs"].items():
        if npc_state["location"] == loc and npc_state["alive"]:
            npc_def = episode.npcs.get(npc_id, {})
            npcs_here.append({
                "id": npc_id,
                "name": npc_def.get("name", npc_id),
                "found": npc_state["found"],
            })

    monsters_sensed = []
    for m_id, m_state in state["monsters"].items():
        if not m_state["active"]:
            continue
        m_def = episode.monsters.get(m_id, {})
        m_loc = m_state["location"]
        # adjacent room check
        connections = room.get("connections", {})
        adjacent_rooms = list(connections.values())
        if m_loc in adjacent_rooms:
            monsters_sensed.append({
                "monster_id": m_id,
                "proximity": "adjacent",
                "direction": next((d for d, r in connections.items() if r == m_loc), None),
            })
        elif m_loc == loc:
            monsters_sensed.append({
                "monster_id": m_id,
                "proximity": "same_room",
                "description": m_def.get("description_distant", ""),
            })

    return make_response(True, "look", result={
        "room_id": loc,
        "room_name": room.get("name", loc),
        "floor": room.get("floor"),
        "description_brief": room.get("description_brief", ""),
        "description_full": room.get("description_full", ""),
        "connections": room.get("connections", {}),
        "items": items_visible,
        "clues": clues_visible,
        "features": room.get("features", []),
        "hiding_spots": room.get("hiding_spots", []),
        "npcs": npcs_here,
        "monsters_sensed": monsters_sensed,
        "ambient": room.get("ambient", {}),
        "tags": room.get("tags", []),
    })


def cmd_move(state, episode, args, rng):
    """Move to a connected room."""
    if not args:
        return make_response(False, "move", error="Usage: move <room_id>")

    target_room = args[0]
    loc = player_location(state)
    room = episode.rooms.get(loc, {})
    connections = room.get("connections", {})

    # target can be a direction or a room_id
    if target_room in connections:
        # it's a direction name
        actual_room = connections[target_room]
    elif target_room in connections.values():
        actual_room = target_room
    else:
        return make_response(False, "move", error=f"Cannot go to '{target_room}' from here. Available: {connections}")

    # check if locked
    target_room_def = episode.rooms.get(actual_room, {})
    lock = target_room_def.get("locked_by")
    if lock and lock not in state["keys_found"]:
        return make_response(False, "move", result={
            "blocked": True,
            "room": actual_room,
            "locked_by": lock,
        }, error=f"This way is locked. Requires: {lock}")

    # stamina check
    stamina_cost = STAMINA_COSTS.get("move", 5)
    if get_stamina(state) < abs(stamina_cost):
        return make_response(False, "move", error="Too exhausted to move. Rest first.",
                             warnings=["stamina_depleted"])

    # execute move
    time_cost = TIME_COSTS["move"]
    state["game_time"] = advance_time(state["game_time"], time_cost)
    apply_stamina(state, -stamina_cost)
    state["turn"] += 1
    set_player_location(state, actual_room)

    first_visit = actual_room not in state["rooms_visited"]
    if first_visit:
        state["rooms_visited"].append(actual_room)

    # discover NPCs in room
    npcs_discovered = []
    for npc_id, npc_state in state["npcs"].items():
        if npc_state["location"] == actual_room and not npc_state["found"] and npc_state["alive"]:
            npc_state["found"] = True
            npc_def = episode.npcs.get(npc_id, {})
            npcs_discovered.append({
                "npc_id": npc_id,
                "name": npc_def.get("name", npc_id),
                "description": npc_def.get("description", ""),
            })

    new_room = episode.rooms.get(actual_room, {})
    result = {
        "moved_to": actual_room,
        "room_name": new_room.get("name", actual_room),
        "floor": new_room.get("floor"),
        "description_brief": new_room.get("description_brief", ""),
        "first_visit": first_visit,
        "connections": new_room.get("connections", {}),
        "ambient": new_room.get("ambient", {}),
        "npcs_discovered": npcs_discovered,
    }

    su = {
        "time": state["game_time"],
        "hp_change": 0, "san_change": 0,
        "stamina_change": -stamina_cost,
        "items_gained": [], "items_lost": [],
        "clues_found": [], "rules_discovered": [],
    }

    # check win condition on enter
    if check_win_condition(state, episode):
        state["game_over"] = True
        state["game_result"] = "win"
        state["score"] = calculate_score(state, episode)
        result["win"] = True
        result["final_score"] = state["score"]
        return make_response(True, "move", result=result, state_update=su,
                             game_over=True, game_result="win")

    return make_response(True, "move", result=result, state_update=su)


def cmd_investigate(state, episode, args, rng):
    """Deep investigation of a target."""
    if not args:
        return make_response(False, "investigate", error="Usage: investigate <target>")

    target = args[0]
    loc = player_location(state)

    time_cost = TIME_COSTS["investigate"]
    stamina_cost = STAMINA_COSTS.get("investigate", 3)
    state["game_time"] = advance_time(state["game_time"], time_cost)
    apply_stamina(state, -stamina_cost)
    state["turn"] += 1

    su = {
        "time": state["game_time"],
        "hp_change": 0, "san_change": 0,
        "stamina_change": -stamina_cost,
        "items_gained": [], "items_lost": [],
        "clues_found": [], "rules_discovered": [],
    }

    # search clues
    room_clues = state["room_clues"].get(loc, [])
    for clue_id in room_clues:
        clue = episode.clues.get(clue_id, {})
        if clue_id == target or clue.get("name", "") == target:
            clues_found = []
            rules_discovered = []

            if not has_clue(state, clue_id):
                add_clue(state, clue_id)
                clues_found.append(clue_id)
                state["score"] += clue.get("score_value", 0)

            # check if this clue reveals a rule
            rule_hint = clue.get("hints_at_rule")
            if rule_hint and not has_rule(state, rule_hint):
                add_rule(state, rule_hint)
                rules_discovered.append(rule_hint)

            su["clues_found"] = clues_found
            su["rules_discovered"] = rules_discovered

            return make_response(True, "investigate", result={
                "type": "clue",
                "target": clue_id,
                "name": clue.get("name", clue_id),
                "description_investigated": clue.get("description_investigated", ""),
                "content": clue.get("content", ""),
                "reveals": clue.get("reveals", ""),
                "rule_discovered": rule_hint if rules_discovered else None,
            }, state_update=su)

    # search features
    room = episode.rooms.get(loc, {})
    features = room.get("features", [])
    if target in features:
        # features might hide items
        hidden_items = []
        for item_id, item_def in episode.items.items():
            if item_def.get("hidden_in") == target and item_def.get("location") == loc:
                room_items = state["room_items"].get(loc, [])
                if item_id not in room_items:
                    state["room_items"].setdefault(loc, []).append(item_id)
                    hidden_items.append(item_id)

        return make_response(True, "investigate", result={
            "type": "feature",
            "target": target,
            "room": loc,
            "hidden_items_revealed": hidden_items,
        }, state_update=su)

    # search general room
    return make_response(True, "investigate", result={
        "type": "general",
        "target": target,
        "room": loc,
        "found": False,
        "message": f"Thorough search of '{target}' yields nothing new.",
    }, state_update=su)


def cmd_take(state, episode, args):
    """Pick up an item from the current room."""
    if not args:
        return make_response(False, "take", error="Usage: take <item_id>")

    item_id = args[0]
    loc = player_location(state)
    room_items = state["room_items"].get(loc, [])

    if item_id not in room_items:
        return make_response(False, "take", error=f"Item '{item_id}' not found in this room")

    item_def = episode.items.get(item_id, {})
    time_cost = TIME_COSTS["take"]
    state["game_time"] = advance_time(state["game_time"], time_cost)
    state["turn"] += 1

    room_items.remove(item_id)
    add_item(state, item_id)

    # check if it's a key item
    if item_def.get("type") == "key" or item_id.startswith("key_"):
        if item_id not in state["keys_found"]:
            state["keys_found"].append(item_id)

    return make_response(True, "take", result={
        "item_id": item_id,
        "name": item_def.get("name", item_id),
        "description": item_def.get("description", ""),
        "type": item_def.get("type", "misc"),
    }, state_update={
        "time": state["game_time"],
        "hp_change": 0, "san_change": 0, "stamina_change": 0,
        "items_gained": [item_id], "items_lost": [],
        "clues_found": [], "rules_discovered": [],
    })


def cmd_use(state, episode, args, rng):
    """Use an inventory item, optionally on a target."""
    if not args:
        return make_response(False, "use", error="Usage: use <item_id> [target]")

    item_id = args[0]
    target = args[1] if len(args) > 1 else None

    if not has_item(state, item_id):
        return make_response(False, "use", error=f"You don't have '{item_id}'")

    item_def = episode.items.get(item_id, {})
    if not item_def.get("usable", False):
        return make_response(False, "use", error=f"'{item_def.get('name', item_id)}' cannot be used")

    time_cost = TIME_COSTS["use"]
    state["game_time"] = advance_time(state["game_time"], time_cost)
    state["turn"] += 1

    effect = item_def.get("effect", {})
    e_type = effect.get("type")
    hp_change = 0
    san_change = 0
    result_detail = {}

    if e_type == "san_restore":
        amt = effect.get("amount", 0)
        apply_san(state, amt)
        san_change = amt
        result_detail["san_restored"] = amt

    elif e_type == "hp_restore":
        amt = effect.get("amount", 0)
        apply_hp(state, amt)
        hp_change = amt
        result_detail["hp_restored"] = amt

    elif e_type == "illuminate":
        add_status(state, "illuminated")
        result_detail["status_added"] = "illuminated"
        result_detail["bonus"] = effect.get("bonus", "")

    elif e_type == "unlock":
        unlock_target = effect.get("target", target)
        if unlock_target:
            if unlock_target not in state["keys_found"]:
                state["keys_found"].append(unlock_target)
            result_detail["unlocked"] = unlock_target

    elif e_type == "weapon":
        if state["active_encounter"]:
            m_id = state["active_encounter"]["monster_id"]
            m_def = episode.monsters.get(m_id, {})
            weakness = m_def.get("weakness", "")
            damage = effect.get("damage", 0)
            result_detail["used_in_combat"] = True
            result_detail["damage"] = damage
            result_detail["effective"] = item_id == weakness or e_type in str(weakness)

    elif e_type == "repel_monster":
        if state["active_encounter"]:
            m_id = state["active_encounter"]["monster_id"]
            state["monsters"][m_id]["frozen_turns"] = effect.get("duration", 3)
            state["active_encounter"] = None
            state["phase"] = "exploration"
            result_detail["monster_repelled"] = True
            result_detail["duration"] = effect.get("duration", 3)

    else:
        result_detail["generic_use"] = True
        result_detail["effect_type"] = e_type

    # consume if consumable
    consumed = False
    if item_def.get("consumable", False):
        remove_item(state, item_id)
        consumed = True

    return make_response(True, "use", result={
        "item_id": item_id,
        "name": item_def.get("name", item_id),
        "target": target,
        "consumed": consumed,
        "effect": result_detail,
    }, state_update={
        "time": state["game_time"],
        "hp_change": hp_change, "san_change": san_change, "stamina_change": 0,
        "items_gained": [], "items_lost": [item_id] if consumed else [],
        "clues_found": [], "rules_discovered": [],
    })


def cmd_talk(state, episode, args):
    """Talk to an NPC in the same room."""
    if not args:
        return make_response(False, "talk", error="Usage: talk <npc_id>")

    npc_id = args[0]
    loc = player_location(state)
    npc_state = state["npcs"].get(npc_id)
    npc_def = episode.npcs.get(npc_id)

    if npc_state is None or npc_def is None:
        return make_response(False, "talk", error=f"NPC '{npc_id}' does not exist")

    if npc_state["location"] != loc:
        return make_response(False, "talk", error=f"'{npc_def.get('name', npc_id)}' is not here")

    if not npc_state["alive"]:
        return make_response(False, "talk", error=f"'{npc_def.get('name', npc_id)}' is dead")

    time_cost = TIME_COSTS["talk"]
    state["game_time"] = advance_time(state["game_time"], time_cost)
    state["turn"] += 1

    if not npc_state["found"]:
        npc_state["found"] = True

    # advance dialogue state
    old_state = npc_state["dialogue_state"]
    dialogue_tree = npc_def.get("dialogue", {})
    current_dialogue = dialogue_tree.get(old_state, {})

    # advance to next dialogue state if defined
    next_state = current_dialogue.get("next_state", old_state)
    npc_state["dialogue_state"] = next_state

    # trust change
    trust_change = current_dialogue.get("trust_change", 0)
    npc_state["trust"] = clamp(npc_state["trust"] + trust_change, 0, 100)

    # items or clues given
    gives_item = current_dialogue.get("gives_item")
    gives_clue = current_dialogue.get("gives_clue")
    items_gained = []
    clues_found = []

    if gives_item and not has_item(state, gives_item):
        add_item(state, gives_item)
        items_gained.append(gives_item)

    if gives_clue and not has_clue(state, gives_clue):
        add_clue(state, gives_clue)
        clues_found.append(gives_clue)

    return make_response(True, "talk", result={
        "npc_id": npc_id,
        "name": npc_def.get("name", npc_id),
        "personality": npc_def.get("personality", ""),
        "trust": npc_state["trust"],
        "dialogue_state": npc_state["dialogue_state"],
        "dialogue_content": current_dialogue.get("text", ""),
        "dialogue_hints": current_dialogue.get("hints", ""),
        "abilities": npc_def.get("abilities", []),
        "san": npc_state["san"],
    }, state_update={
        "time": state["game_time"],
        "hp_change": 0, "san_change": 0, "stamina_change": 0,
        "items_gained": items_gained, "items_lost": [],
        "clues_found": clues_found, "rules_discovered": [],
    })


def cmd_hide(state, episode, rng):
    """Hide in the current room."""
    loc = player_location(state)
    room = episode.rooms.get(loc, {})
    hiding_spots = room.get("hiding_spots", [])

    if not hiding_spots:
        return make_response(False, "hide", error="No hiding spots in this room")

    stamina_cost = STAMINA_COSTS.get("hide", 5)
    if get_stamina(state) < abs(stamina_cost):
        return make_response(False, "hide", error="Too exhausted to hide")

    time_cost = TIME_COSTS["hide"]
    state["game_time"] = advance_time(state["game_time"], time_cost)
    apply_stamina(state, -stamina_cost)
    state["turn"] += 1

    spot = rng.choice(hiding_spots)
    add_status(state, "hidden")

    # if in encounter, hiding may work
    escaped_encounter = False
    if state["active_encounter"]:
        m_id = state["active_encounter"]["monster_id"]
        m_def = episode.monsters.get(m_id, {})
        flee_rule = m_def.get("behavior_rules", {}).get("on_player_hide", "search_briefly")

        if flee_rule == "ignore" or flee_rule == "lose_interest":
            # Set cooldown and move monster away
            state["monsters"][m_id]["cooldown_turns"] = 3
            m_def_hide = episode.monsters.get(m_id, {})
            patrol_route = m_def_hide.get("patrol_route", [])
            if patrol_route:
                safe_spots = [r for r in patrol_route if r != loc]
                if safe_spots:
                    state["monsters"][m_id]["location"] = rng.choice(safe_spots)
            state["active_encounter"] = None
            state["phase"] = "exploration"
            escaped_encounter = True
        elif flee_rule == "search_briefly":
            # 60% chance to avoid detection
            if rng.random() < 0.6:
                # Set cooldown and move monster away
                state["monsters"][m_id]["cooldown_turns"] = 3
                m_def_hide = episode.monsters.get(m_id, {})
                patrol_route = m_def_hide.get("patrol_route", [])
                if patrol_route:
                    safe_spots = [r for r in patrol_route if r != loc]
                    if safe_spots:
                        state["monsters"][m_id]["location"] = rng.choice(safe_spots)
                state["active_encounter"] = None
                state["phase"] = "exploration"
                escaped_encounter = True
        # else monster finds you

    return make_response(True, "hide", result={
        "hiding_spot": spot,
        "hidden": True,
        "escaped_encounter": escaped_encounter,
        "available_spots": hiding_spots,
    }, state_update={
        "time": state["game_time"],
        "hp_change": 0, "san_change": 0,
        "stamina_change": -stamina_cost,
        "items_gained": [], "items_lost": [],
        "clues_found": [], "rules_discovered": [],
    })


def cmd_run(state, episode, rng):
    """Flee from current location or encounter."""
    stamina_cost = STAMINA_COSTS.get("run", 20)
    if get_stamina(state) < abs(stamina_cost):
        return make_response(False, "run", error="Too exhausted to run")

    time_cost = TIME_COSTS["run"]
    state["game_time"] = advance_time(state["game_time"], time_cost)
    apply_stamina(state, -stamina_cost)
    state["turn"] += 1
    remove_status(state, "hidden")

    loc = player_location(state)
    room = episode.rooms.get(loc, {})
    connections = room.get("connections", {})

    if not connections:
        return make_response(False, "run", error="Nowhere to run. No exits.",
                             state_update={
                                 "time": state["game_time"],
                                 "hp_change": 0, "san_change": 0,
                                 "stamina_change": -stamina_cost,
                                 "items_gained": [], "items_lost": [],
                                 "clues_found": [], "rules_discovered": [],
                             })

    # pick a random exit (not back toward monster if possible)
    safe_exits = []
    for direction, room_id in connections.items():
        # avoid rooms with active monsters
        has_monster = any(
            ms["location"] == room_id and ms["active"]
            for ms in state["monsters"].values()
        )
        if not has_monster:
            safe_exits.append((direction, room_id))

    if not safe_exits:
        safe_exits = list(connections.items())

    direction, flee_room = rng.choice(safe_exits)

    # success check
    success_chance = RUN_SUCCESS_BASE
    if state["active_encounter"]:
        m_id = state["active_encounter"]["monster_id"]
        m_def = episode.monsters.get(m_id, {})
        flee_rule = m_def.get("behavior_rules", {}).get("on_player_flee", "chase")
        if flee_rule == "do_not_chase":
            success_chance = 0.95
        elif flee_rule == "chase_slowly":
            success_chance = 0.75
        # default chase: 0.6

    success = rng.random() < success_chance
    hp_change = 0
    san_change = 0

    if success:
        set_player_location(state, flee_room)
        if flee_room not in state["rooms_visited"]:
            state["rooms_visited"].append(flee_room)
        # Always clear encounter on successful flee
        if state["active_encounter"]:
            m_id = state["active_encounter"]["monster_id"]
            m_def = episode.monsters.get(m_id, {})
            # Set cooldown so monster won't immediately re-trigger
            state["monsters"][m_id]["cooldown_turns"] = 3
            # Move monster away from player's new location
            patrol_route = m_def.get("patrol_route", [])
            if patrol_route:
                safe_spots = [r for r in patrol_route if r != flee_room]
                if safe_spots:
                    state["monsters"][m_id]["location"] = rng.choice(safe_spots)
            state["active_encounter"] = None
            state["phase"] = "exploration"
    else:
        # failed to flee, take damage
        if state["active_encounter"]:
            m_id = state["active_encounter"]["monster_id"]
            m_def = episode.monsters.get(m_id, {})
            dmg = m_def.get("stats", {}).get("damage", 20) // 2
            apply_hp(state, -dmg)
            hp_change = -dmg
            san_dmg = m_def.get("stats", {}).get("san_damage_on_attack", 10) // 2
            apply_san(state, -san_dmg)
            san_change = -san_dmg

    new_room = episode.rooms.get(player_location(state), {})
    return make_response(True, "run", result={
        "success": success,
        "fled_to": player_location(state) if success else loc,
        "room_name": new_room.get("name", player_location(state)),
        "direction": direction if success else None,
        "caught": not success,
        "encounter_ended": success and state["active_encounter"] is None,
    }, state_update={
        "time": state["game_time"],
        "hp_change": hp_change, "san_change": san_change,
        "stamina_change": -stamina_cost,
        "items_gained": [], "items_lost": [],
        "clues_found": [], "rules_discovered": [],
    })


def cmd_rest(state, episode, rng):
    """Rest to recover stamina. Risky."""
    if state["active_encounter"]:
        return make_response(False, "rest", error="Cannot rest during an encounter")

    time_cost = TIME_COSTS["rest"]
    stamina_recovery = -STAMINA_COSTS.get("rest", -40)  # negative of negative = positive
    state["game_time"] = advance_time(state["game_time"], time_cost)
    apply_stamina(state, stamina_recovery)
    state["turn"] += 1

    # resting is dangerous
    danger = rng.random() < REST_DANGER_CHANCE
    danger_detail = None
    san_change = 0
    hp_change = 0

    if danger:
        # check if any monster is near
        loc = player_location(state)
        room = episode.rooms.get(loc, {})
        connections = room.get("connections", {})
        adjacent = list(connections.values())

        nearby_monster = None
        for m_id, m_state in state["monsters"].items():
            if m_state["active"] and (m_state["location"] in adjacent or m_state["location"] == loc):
                nearby_monster = m_id
                break

        if nearby_monster:
            # monster finds you while resting
            m_def = episode.monsters.get(nearby_monster, {})
            state["monsters"][nearby_monster]["location"] = loc
            state["active_encounter"] = {
                "monster_id": nearby_monster,
                "monster_name": m_def.get("name", nearby_monster),
                "distance": "close",
                "turn_count": 0,
                "stare_counter": 0,
                "description_distant": m_def.get("description_distant", ""),
                "description_close": m_def.get("description_close", ""),
                "ambush": True,
            }
            state["phase"] = "encounter"
            san_dmg = m_def.get("stats", {}).get("san_damage_on_sight", 5)
            apply_san(state, -san_dmg)
            san_change = -san_dmg
            danger_detail = {
                "type": "monster_ambush",
                "monster_id": nearby_monster,
                "monster_name": m_def.get("name", nearby_monster),
            }
        else:
            # unnerving experience
            san_dmg = rng.randint(2, 5)
            apply_san(state, -san_dmg)
            san_change = -san_dmg
            danger_detail = {
                "type": "nightmare",
                "description": "disturbing_dream",
            }

    return make_response(True, "rest", result={
        "rested": True,
        "stamina_recovered": stamina_recovery,
        "danger_occurred": danger,
        "danger_detail": danger_detail,
    }, state_update={
        "time": state["game_time"],
        "hp_change": hp_change, "san_change": san_change,
        "stamina_change": stamina_recovery,
        "items_gained": [], "items_lost": [],
        "clues_found": [], "rules_discovered": [],
    }, encounter=state["active_encounter"] if danger and state["active_encounter"] else None)


def cmd_monster_action(state, episode, args, rng):
    """Process player's response during a monster encounter."""
    if not state["active_encounter"]:
        return make_response(False, "monster_action", error="No active encounter")

    if not args:
        return make_response(False, "monster_action", error="Usage: monster_action <response>")

    response = args[0]
    enc = state["active_encounter"]
    m_id = enc["monster_id"]
    m_def = episode.monsters.get(m_id, {})
    rules = m_def.get("behavior_rules", {})
    stats = m_def.get("stats", {})

    time_cost = TIME_COSTS["monster_action"]
    state["game_time"] = advance_time(state["game_time"], time_cost)
    enc["turn_count"] += 1
    state["turn"] += 1

    result = {
        "monster_id": m_id,
        "monster_name": enc["monster_name"],
        "player_action": response,
        "distance": enc["distance"],
        "outcome": None,
        "monster_reaction": None,
        "damage_taken": 0,
        "san_lost": 0,
        "encounter_ended": False,
        "rule_discovered": None,
    }

    hp_change = 0
    san_change = 0
    rules_discovered = []

    # SAN cost for seeing monster
    if response in ("look", "stare"):
        san_cost = stats.get("san_damage_on_sight", 5)
        apply_san(state, -san_cost)
        san_change -= san_cost
        result["san_lost"] += san_cost

    elif response == "glance":
        san_cost = max(1, stats.get("san_damage_on_sight", 5) // 3)
        apply_san(state, -san_cost)
        san_change -= san_cost
        result["san_lost"] += san_cost

    # process response against behavior rules
    if response in ("look", "stare"):
        on_facing = rules.get("on_player_facing", "approach")
        result["monster_reaction"] = on_facing

        if on_facing == "stop_and_smile":
            # monster stops but staring too long is dangerous
            enc["stare_counter"] += 1
            threshold = rules.get("on_player_stare_duration", 3)
            if enc["stare_counter"] >= threshold:
                # stared too long
                on_stare = rules.get("on_stare_complete", "attack")
                if on_stare == "attack":
                    dmg = stats.get("damage", 30)
                    apply_hp(state, -dmg)
                    hp_change -= dmg
                    san_dmg = stats.get("san_damage_on_attack", 15)
                    apply_san(state, -san_dmg)
                    san_change -= san_dmg
                    result["damage_taken"] = dmg
                    result["san_lost"] += san_dmg
                    result["outcome"] = "attacked_stare_too_long"
                    # encounter continues unless player dies
            else:
                result["outcome"] = "monster_stopped"
                result["stare_counter"] = enc["stare_counter"]
                result["stare_threshold"] = threshold

        elif on_facing == "retreat":
            enc["distance"] = "far"
            result["outcome"] = "monster_retreated"

        elif on_facing == "freeze":
            result["outcome"] = "monster_frozen"

        elif on_facing == "approach":
            if enc["distance"] == "far":
                enc["distance"] = "medium"
            elif enc["distance"] == "medium":
                enc["distance"] = "close"
            elif enc["distance"] == "close":
                dmg = stats.get("damage", 30)
                apply_hp(state, -dmg)
                hp_change -= dmg
                result["damage_taken"] = dmg
                result["outcome"] = "attacked"
            else:
                result["outcome"] = "monster_approaching"

    elif response == "glance":
        on_glance = rules.get("on_player_glance", "freeze_briefly")
        result["monster_reaction"] = on_glance

        if on_glance == "freeze_briefly":
            enc["stare_counter"] = 0
            result["outcome"] = "monster_froze_briefly"
            # this is often the correct response
            weakness = m_def.get("weakness", "")
            if weakness == "brief_eye_contact":
                rule_id = m_def.get("rule_id")
                if rule_id and not has_rule(state, rule_id):
                    add_rule(state, rule_id)
                    rules_discovered.append(rule_id)
                    result["rule_discovered"] = rule_id
                # weakness exploited: monster freezes, encounter ends
                state["monsters"][m_id]["frozen_turns"] = 2
                state["monsters"][m_id]["cooldown_turns"] = 3
                state["active_encounter"] = None
                state["phase"] = "exploration"
                result["outcome"] = "monster_neutralized_briefly"
                result["encounter_ended"] = True

        elif on_glance == "ignore":
            result["outcome"] = "monster_unaffected"

    elif response == "look_away":
        on_back = rules.get("on_player_back_turned", "approach")
        result["monster_reaction"] = on_back
        enc["stare_counter"] = 0

        if on_back == "approach":
            if enc["distance"] == "far":
                enc["distance"] = "medium"
            elif enc["distance"] == "medium":
                enc["distance"] = "close"
            elif enc["distance"] == "close":
                dmg = stats.get("damage", 30)
                apply_hp(state, -dmg)
                hp_change -= dmg
                san_dmg = stats.get("san_damage_on_attack", 15)
                apply_san(state, -san_dmg)
                san_change -= san_dmg
                result["damage_taken"] = dmg
                result["san_lost"] += san_dmg
                result["outcome"] = "attacked_from_behind"
            if not result.get("outcome"):
                result["outcome"] = "monster_approached"

        elif on_back == "vanish":
            state["active_encounter"] = None
            state["phase"] = "exploration"
            result["outcome"] = "monster_vanished"
            result["encounter_ended"] = True

    elif response == "hide":
        return cmd_hide(state, episode, rng)

    elif response == "run":
        return cmd_run(state, episode, rng)

    elif response == "crouch":
        on_crouch = rules.get("on_player_crouch", "ignore")
        result["monster_reaction"] = on_crouch
        if on_crouch == "ignore" or on_crouch == "pass_by":
            result["outcome"] = "monster_passed"
            state["active_encounter"] = None
            state["phase"] = "exploration"
            result["encounter_ended"] = True
        elif on_crouch == "investigate":
            if enc["distance"] != "close":
                enc["distance"] = "close"
            result["outcome"] = "monster_investigating"
        else:
            result["outcome"] = "no_effect"

    elif response == "investigate" or response == "check_hands":
        # investigating the monster during encounter -- risky but can reveal rules
        # First check encounter_responses for a specific response
        enc_responses = rules.get("encounter_responses", {})
        matched_response = enc_responses.get(response)
        if not matched_response:
            # Try aliases: investigate -> point_out_hands for nurse, etc.
            for key, val in enc_responses.items():
                aliases = key.split("/")
                if response in aliases:
                    matched_response = val
                    break

        if matched_response:
            outcome = matched_response.get("outcome", "unknown")
            effect = matched_response.get("effect", "")
            result["outcome"] = outcome
            result["narration_hint"] = matched_response.get("narration", "")

            # Process common effects
            if "drops_item" in effect or "best" == outcome:
                rule_id = rules.get("rule_id")
                if rule_id and not has_rule(state, rule_id):
                    add_rule(state, rule_id)
                    rules_discovered.append(rule_id)
                    result["rule_discovered"] = rule_id
                # Monster retreats/freezes
                state["monsters"][m_id]["frozen_turns"] = 3
                state["monsters"][m_id]["cooldown_turns"] = 5
                state["active_encounter"] = None
                state["phase"] = "exploration"
                result["encounter_ended"] = True
            elif "attack" in effect or outcome == "bad":
                dmg = stats.get("hp_damage", stats.get("damage", 25))
                san_dmg_amt = stats.get("san_damage", 15)
                apply_hp(state, -dmg)
                apply_san(state, -san_dmg_amt)
                hp_change -= dmg
                san_change -= san_dmg_amt
                result["damage_taken"] = dmg
                result["san_lost"] += san_dmg_amt
            elif "nothing" in effect or "safe" in outcome:
                state["active_encounter"] = None
                state["phase"] = "exploration"
                result["encounter_ended"] = True
            elif "danger" in effect or outcome == "dangerous" or outcome == "risky":
                # Risky outcome: chance of bad result
                if rng.random() < 0.7:
                    san_dmg_amt = stats.get("san_damage", 10)
                    apply_san(state, -san_dmg_amt)
                    san_change -= san_dmg_amt
                    result["san_lost"] += san_dmg_amt
                    result["outcome"] = "dangerous_outcome"
                else:
                    result["outcome"] = "lucky_safe"
                state["active_encounter"] = None
                state["phase"] = "exploration"
                result["encounter_ended"] = True
        else:
            # Generic investigate behavior
            san_cost = stats.get("san_damage_on_sight", 5) * 2
            apply_san(state, -san_cost)
            san_change -= san_cost
            result["san_lost"] += san_cost

            rule_id = rules.get("rule_id", m_def.get("rule_id"))
            if rule_id and not has_rule(state, rule_id):
                if rng.random() < 0.5:
                    add_rule(state, rule_id)
                    rules_discovered.append(rule_id)
                    result["rule_discovered"] = rule_id
                    result["outcome"] = "rule_observed"
                else:
                    result["outcome"] = "no_rule_found"
            else:
                result["outcome"] = "already_known"

    else:
        # Check encounter_responses for this monster before falling back
        enc_responses = rules.get("encounter_responses", {})
        matched_response = enc_responses.get(response)
        if not matched_response:
            # Try matching response against keys (some use underscore variants)
            for key, val in enc_responses.items():
                # Support slash-separated aliases in keys
                aliases = key.split("/")
                if response in aliases:
                    matched_response = val
                    break

        if matched_response:
            outcome = matched_response.get("outcome", "unknown")
            effect = matched_response.get("effect", "")
            result["outcome"] = outcome
            result["narration_hint"] = matched_response.get("narration", "")

            # Process effects based on outcome type
            if outcome == "best" or "drops_item" in effect or "disappears" in effect:
                rule_id = rules.get("rule_id")
                if rule_id and not has_rule(state, rule_id):
                    add_rule(state, rule_id)
                    rules_discovered.append(rule_id)
                    result["rule_discovered"] = rule_id
                state["monsters"][m_id]["frozen_turns"] = 3
                state["monsters"][m_id]["cooldown_turns"] = 5
                state["active_encounter"] = None
                state["phase"] = "exploration"
                result["encounter_ended"] = True
            elif outcome in ("safe", "safe_usually"):
                state["active_encounter"] = None
                state["phase"] = "exploration"
                result["encounter_ended"] = True
            elif outcome == "bad" or "attack" in effect:
                dmg = stats.get("hp_damage", stats.get("damage", 25))
                san_dmg_amt = stats.get("san_damage", 15)
                apply_hp(state, -dmg)
                apply_san(state, -san_dmg_amt)
                hp_change -= dmg
                san_change -= san_dmg_amt
                result["damage_taken"] = dmg
                result["san_lost"] += san_dmg_amt
            elif outcome in ("risky", "dangerous", "variable"):
                if rng.random() < 0.7:
                    san_dmg_amt = stats.get("san_damage", 10)
                    apply_san(state, -san_dmg_amt)
                    san_change -= san_dmg_amt
                    result["san_lost"] += san_dmg_amt
                    result["outcome"] = "dangerous_outcome"
                else:
                    result["outcome"] = "lucky_safe"
                state["active_encounter"] = None
                state["phase"] = "exploration"
                result["encounter_ended"] = True
            elif outcome in ("helpful_mixed", "interesting", "useful", "revealing"):
                # Informational outcome, no damage, encounter may continue
                san_cost = max(1, stats.get("san_damage_on_sight", 3))
                apply_san(state, -san_cost)
                san_change -= san_cost
                result["san_lost"] += san_cost
            else:
                # Default: treat as neutral
                pass
        else:
            # Truly unknown response, monster reacts to confusion
            result["outcome"] = "unknown_response"
            result["monster_reaction"] = "confused_approach"
            if enc["distance"] == "far":
                enc["distance"] = "medium"
            elif enc["distance"] != "close":
                enc["distance"] = "close"

    # update distance in result
    result["distance"] = enc["distance"]

    # check if encounter should auto-end
    if enc["distance"] == "far" and enc["turn_count"] >= 3 and not result.get("encounter_ended"):
        on_flee = rules.get("on_player_flee", "chase")
        if on_flee == "do_not_chase" or on_flee == "lose_interest":
            state["active_encounter"] = None
            state["phase"] = "exploration"
            result["encounter_ended"] = True

    su = {
        "time": state["game_time"],
        "hp_change": hp_change,
        "san_change": san_change,
        "stamina_change": 0,
        "items_gained": [],
        "items_lost": [],
        "clues_found": [],
        "rules_discovered": rules_discovered,
    }

    return make_response(True, "monster_action", result=result, state_update=su,
                         encounter=state["active_encounter"])


def cmd_status(state, episode):
    """Show player stats."""
    cfg = episode.config
    primary = _first_player(state)

    players_status = {}
    for name, pdata in state["players"].items():
        players_status[name] = {
            "hp": pdata["hp"], "max_hp": pdata["max_hp"],
            "san": pdata["san"], "max_san": pdata["max_san"],
            "stamina": pdata["stamina"], "max_stamina": pdata["max_stamina"],
            "location": pdata["location"],
            "status_effects": pdata["status_effects"],
        }

    return make_response(True, "status", result={
        "game_time": state["game_time"],
        "time_remaining": _time_remaining(state["game_time"], cfg.get("time_end", "06:00")),
        "turn": state["turn"],
        "phase": state["phase"],
        "players": players_status,
        "score": state["score"],
        "keys_found": state["keys_found"],
        "clues_found": len(state["players"][primary]["discovered_clues"]),
        "rules_discovered": state["players"][primary]["discovered_rules"],
        "active_encounter": state["active_encounter"] is not None,
        "game_over": state["game_over"],
    })


def _time_remaining(current, end):
    """Calculate minutes remaining, handling overnight."""
    cur = parse_time(current)
    e = parse_time(end)
    if e < cur:
        # overnight: add 24h to end
        e += 24 * 60
    return max(0, e - cur)


def cmd_inventory(state, episode):
    """Show inventory."""
    items = []
    for holder, pdata in state["players"].items():
        for item_id in pdata["inventory"]:
            item_def = episode.items.get(item_id, {})
            items.append({
                "id": item_id,
                "name": item_def.get("name", item_id),
                "description": item_def.get("description", ""),
                "type": item_def.get("type", "misc"),
                "usable": item_def.get("usable", False),
                "held_by": holder,
            })

    return make_response(True, "inventory", result={
        "items": items,
        "count": len(items),
    })


def cmd_map(state, episode):
    """Show discovered rooms."""
    rooms = []
    current = player_location(state)

    for room_id in state["rooms_visited"]:
        room_def = episode.rooms.get(room_id, {})
        rooms.append({
            "id": room_id,
            "name": room_def.get("name", room_id),
            "floor": room_def.get("floor"),
            "connections": room_def.get("connections", {}),
            "is_current": room_id == current,
        })

    return make_response(True, "map", result={
        "current_room": current,
        "current_room_name": episode.rooms.get(current, {}).get("name", current),
        "discovered_rooms": rooms,
        "total_rooms": len(episode.rooms),
        "explored_percentage": round(len(state["rooms_visited"]) / max(1, len(episode.rooms)) * 100),
    })


def cmd_time(state, episode):
    """Show in-game time info."""
    cfg = episode.config
    return make_response(True, "time", result={
        "current_time": state["game_time"],
        "start_time": cfg.get("time_start", "23:00"),
        "end_time": cfg.get("time_end", "06:00"),
        "minutes_remaining": _time_remaining(state["game_time"], cfg.get("time_end", "06:00")),
        "turn": state["turn"],
    })


def cmd_save(state):
    """Save current game to save slot."""
    save_data = copy.deepcopy(state)
    with open(SAVE_FILE, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)

    return make_response(True, "save", result={
        "saved": True,
        "save_file": str(SAVE_FILE),
        "game_time": state["game_time"],
        "turn": state["turn"],
    })


def cmd_load():
    """Load saved game."""
    if not SAVE_FILE.exists():
        return make_response(False, "load", error="No save file found")

    with open(SAVE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    save_state(state)

    return make_response(True, "load", result={
        "loaded": True,
        "game_time": state["game_time"],
        "turn": state["turn"],
        "location": player_location(state),
    })


# ---------------------------------------------------------------------------
# Main turn pipeline
# ---------------------------------------------------------------------------

def run_turn_pipeline(state, episode, command, args):
    """
    Main turn pipeline:
    1. Process player command
    2. Advance monsters
    3. Check events
    4. Check hallucinations
    5. Check game over
    6. Save state
    7. Return combined result
    """

    rng = get_rng(state)

    # --- Phase gate: if in encounter, only certain commands allowed ---
    encounter_only = {"monster_action", "hide", "run", "use", "status", "inventory", "save"}
    if state["active_encounter"] and command not in encounter_only:
        return make_response(False, command,
                             error=f"In encounter with {state['active_encounter']['monster_name']}. "
                                   f"Use: monster_action, hide, run, use, status, inventory, save",
                             encounter=state["active_encounter"])

    # --- Game over gate ---
    info_only = {"status", "inventory", "map", "time", "save", "load"}
    if state["game_over"] and command not in info_only:
        return make_response(False, command, error="Game is over.",
                             game_over=True, game_result=state["game_result"])

    # --- Dispatch command ---
    if command == "look":
        response = cmd_look(state, episode, args)
    elif command == "move":
        response = cmd_move(state, episode, args, rng)
    elif command == "investigate":
        response = cmd_investigate(state, episode, args, rng)
    elif command == "take":
        response = cmd_take(state, episode, args)
    elif command == "use":
        response = cmd_use(state, episode, args, rng)
    elif command == "talk":
        response = cmd_talk(state, episode, args)
    elif command == "hide":
        response = cmd_hide(state, episode, rng)
    elif command == "run":
        response = cmd_run(state, episode, rng)
    elif command == "rest":
        response = cmd_rest(state, episode, rng)
    elif command == "monster_action":
        response = cmd_monster_action(state, episode, args, rng)
    elif command == "status":
        response = cmd_status(state, episode)
    elif command == "inventory":
        response = cmd_inventory(state, episode)
    elif command == "map":
        response = cmd_map(state, episode)
    elif command == "time":
        response = cmd_time(state, episode)
    elif command == "save":
        response = cmd_save(state)
    elif command == "load":
        response = cmd_load()
        # reload state from file after load
        state = load_state()
        save_state(state)
        return response
    else:
        return make_response(False, command, error=f"Unknown command: {command}")

    if not response["success"]:
        save_state(state)
        return response

    # --- Read-only commands skip the rest of the pipeline ---
    read_only = {"status", "inventory", "map", "time", "save"}
    if command in read_only:
        save_state(state)
        return response

    # --- Monster AI tick (only for action commands) ---
    monster_updates = tick_monsters(state, episode, rng)

    # check if a monster wandered into player room
    new_encounter = check_encounter_start(state, episode, monster_updates)
    if new_encounter and not response.get("encounter"):
        response["encounter"] = new_encounter

    if monster_updates:
        response["monster_updates"] = monster_updates

    # --- Event checks ---
    context = None
    if command == "move":
        context = "enter_room"
    elif command == "use":
        context = "item_used"

    # also check time-based events
    fired = check_events(state, episode, context)
    if not context:
        fired += check_events(state, episode, None)

    if fired:
        event_summaries, evt_encounter = apply_event_effects(state, episode, fired, rng)
        response["events_triggered"] = event_summaries
        if evt_encounter and not response.get("encounter"):
            response["encounter"] = evt_encounter

    # --- Hallucination check ---
    hallucination = check_hallucination(state, rng)
    if hallucination:
        response.setdefault("warnings", []).append({
            "type": "hallucination",
            "detail": hallucination,
        })

    # --- Game over check ---
    is_over, result = check_game_over(state, episode)
    if is_over:
        response["game_over"] = True
        response["game_result"] = result
        if result == "win":
            state["score"] = calculate_score(state, episode)
        elif result in ("death", "insanity", "time_up"):
            state["score"] = calculate_score(state, episode)
        response["result"]["final_score"] = state["score"]

    # --- Update state_update time if not already set ---
    if response.get("state_update") and response["state_update"].get("time") is None:
        response["state_update"]["time"] = state["game_time"]

    # --- Persist ---
    save_state(state)
    return response


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        usage = {
            "commands": [
                "init <episode_id>", "look [target]", "move <room_id>",
                "investigate <target>", "take <item_id>", "use <item_id> [target]",
                "talk <npc_id>", "hide", "run", "rest",
                "monster_action <response>", "status", "inventory", "map", "time",
                "save", "load",
            ]
        }
        print(json.dumps(make_response(False, "none", result=usage,
                                       error="Usage: engine.py <command> [args...]"),
                         ensure_ascii=False, indent=2))
        sys.exit(1)

    command = sys.argv[1].lower()
    args = sys.argv[2:]

    # init is special: no existing state needed
    if command == "init":
        result = cmd_init(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["success"] else 1)

    # load is also special
    if command == "load":
        result = cmd_load()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result["success"] else 1)

    # all other commands need existing state
    state = load_state()
    if state is None:
        result = make_response(False, command,
                               error="No game in progress. Use 'init <episode_id>' first.")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1)

    # load episode data
    episode_id = state.get("episode_id")
    try:
        episode = EpisodeData(episode_id)
    except FileNotFoundError as e:
        result = make_response(False, command, error=f"Cannot load episode data: {e}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(1)

    # run the turn
    result = run_turn_pipeline(state, episode, command, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
