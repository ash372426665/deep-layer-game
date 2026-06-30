# 深层游戏 / Deep Layer Game

AI-powered horror text adventure engine. The engine handles game state, monster AI, and event logic. An AI (like Claude) serves as the DM, reading the engine's JSON output and narrating the story.

## How to Play

**Requirements:** Python 3.8+

1. Start a game:
```bash
python3 engine.py init ep01_hospital
# Or with custom player names:
python3 engine.py init ep01_hospital --players "Alice,Bob"
```

2. Have an AI DM read the JSON output and narrate. The DM calls engine commands and translates the structured results into prose.

3. Play through commands:
```bash
python3 engine.py look                    # Observe the current room
python3 engine.py move <room_id>          # Move to a connected room
python3 engine.py investigate <target>    # Investigate something closely
python3 engine.py take <item_id>          # Pick up an item
python3 engine.py use <item_id> [target]  # Use an inventory item
python3 engine.py talk <npc_id>           # Talk to an NPC
python3 engine.py hide                    # Hide in the current room
python3 engine.py run                     # Flee from danger
python3 engine.py rest                    # Rest (risky, but recovers stamina)
python3 engine.py monster_action <action> # Respond during a monster encounter
python3 engine.py status                  # Show player stats
python3 engine.py inventory              # Show inventory
python3 engine.py map                     # Show discovered rooms
python3 engine.py time                    # Show game clock
python3 engine.py save                    # Save game
python3 engine.py load                    # Load saved game
```

## Engine Architecture

The engine is a **state machine**. Every command:
1. Reads the current game state from `game_state.json`
2. Processes the player's action
3. Advances monster AI (patrol, encounter checks)
4. Fires triggered events
5. Checks for hallucinations (low SAN)
6. Checks win/lose conditions
7. Saves state and returns a JSON result

The engine never generates narrative text. It returns structured data (room descriptions, monster states, event triggers) that the AI DM interprets and narrates.

## JSON Response Format

Every command returns:
```json
{
  "success": true,
  "command": "move",
  "result": { ... },
  "state_update": {
    "time": "23:15",
    "hp_change": 0,
    "san_change": -5,
    "stamina_change": -5,
    "items_gained": [],
    "items_lost": [],
    "clues_found": [],
    "rules_discovered": []
  },
  "monster_updates": [ ... ],
  "events_triggered": [ ... ],
  "encounter": null,
  "warnings": [],
  "game_over": false
}
```

## Creating Custom Episodes

Each episode is a directory under `episodes/` containing these JSON files:

### `config.json` (required)
```json
{
  "id": "my_episode",
  "name": "Episode Name",
  "time_start": "23:00",
  "time_end": "06:00",
  "start_room": "room_id",
  "players": {
    "default_count": 2,
    "default_names": ["Player 1", "Player 2"],
    "player_template": {
      "hp": 100, "max_hp": 100,
      "san": 100, "max_san": 100,
      "stamina": 100, "max_stamina": 100,
      "inventory": ["starting_item"],
      "discovered_clues": [],
      "discovered_rules": [],
      "status_effects": []
    }
  },
  "win_condition": {
    "type": "collect_and_escape",
    "required_keys": ["key_1", "key_2"],
    "exit_room": "exit_room_id"
  },
  "scoring": {
    "survive": 30,
    "discover_rule_each": 4,
    "all_npcs_alive": 10,
    "hidden_clue_each": 2,
    "complete_escape": 20
  }
}
```

Win condition types: `collect_and_escape`, `survive`, `escape`.

### `rooms.json` (required)
```json
{
  "rooms": {
    "room_id": {
      "name": "Room Name",
      "floor": 1,
      "description_brief": "Short description.",
      "description_full": "Detailed description.",
      "connections": { "north": "other_room_id" },
      "features": { ... },
      "hiding_spots": ["under_bed"],
      "ambient": { "sound": "...", "smell": "...", "temperature": "...", "light": "..." },
      "tags": ["floor_1"]
    }
  }
}
```

### `monsters.json`
```json
{
  "monsters": {
    "monster_id": {
      "name": "Monster Name",
      "description_distant": "What players see from far away.",
      "description_close": "What players see up close.",
      "type": "patrol",
      "initial_location": "room_id",
      "patrol_route": ["room_a", "room_b"],
      "activation_time": "00:00",
      "behavior_rules": {
        "core_rule": "How to deal with this monster.",
        "rule_id": "rule_monster_weakness",
        "on_player_facing": "stop_and_smile",
        "on_player_glance": "freeze_briefly",
        "on_player_back_turned": "approach",
        "on_player_hide": "search_briefly",
        "on_player_flee": "chase",
        "encounter_responses": {
          "action_name": {
            "outcome": "safe|risky|bad|best",
            "narration": "What happens.",
            "effect": "effect_description"
          }
        }
      },
      "stats": {
        "hp_damage": 30,
        "san_damage": 15,
        "san_damage_on_sight": 5,
        "speed": 1
      }
    }
  }
}
```

Monster types: `patrol` (moves along route), `reactive` (responds to player), `ambient` (environmental), `static` (doesn't move).

### `npcs.json`
```json
{
  "npcs": {
    "npc_id": {
      "name": "NPC Name",
      "initial_location": "room_id",
      "stats": { "hp": 80, "san": 70 },
      "trust_level": { "initial": 30 },
      "found": false,
      "alive": true,
      "dialogue": {
        "initial": { "text": "...", "next_state": "..." }
      },
      "abilities": { ... }
    }
  }
}
```

### `items.json`
```json
{
  "items": {
    "item_id": {
      "name": "Item Name",
      "description": "...",
      "type": "tool|consumable|key_item|weapon|clue_item",
      "location": "room_id",
      "consumable": false,
      "usable": true,
      "effect": { "type": "san_restore", "amount": 15 }
    }
  }
}
```

### `clues.json`
```json
{
  "clues": {
    "clue_id": {
      "name": "Clue Name",
      "location": "room_id",
      "requires_investigation": true,
      "description_surface": "What you see at first glance.",
      "description_investigated": "What you find on closer inspection.",
      "content": "The actual information.",
      "hints_at_rule": ["rule_id"],
      "score_value": 2
    }
  }
}
```

### `events.json`
```json
{
  "events": {
    "event_id": {
      "id": "event_id",
      "name": "Event Name",
      "trigger": {
        "type": "enter_room|time_reached|item_used|clue_found|npc_found",
        "room": "room_id",
        "time": "00:00",
        "condition": "first_time"
      },
      "effect": {
        "type": "monster_encounter|san_damage|give_item|reveal_clue|global_event",
        "amount": 10
      }
    }
  }
}
```

All data files support both wrapped (`{"rooms": {...}}`) and unwrapped (`{...}`) formats.

## Game Mechanics

- **HP:** Health points. Reduced by monster attacks. 0 = death.
- **SAN:** Sanity. Reduced by seeing monsters, disturbing events, and cosmic horror. 0 = insanity (game over). Low SAN triggers hallucinations.
- **Stamina:** Physical energy. Spent on movement, running, hiding. Recovered by resting.
- **Time:** The game clock advances with each action. Reaching the end time without escaping = game over.
- **Monster Cooldown:** After resolving an encounter (flee/hide), monsters enter a cooldown period and won't re-trigger immediately.

## Credits

Engine by Ashley & Claude.
