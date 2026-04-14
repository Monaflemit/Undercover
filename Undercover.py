#cd "C:\Users\quewa\Documents\pyzo\Undercover"
#python -m streamlit run "Undercover.py"
from __future__ import annotations

import json
import os
import random
import re
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DATA_FILE = APP_DIR / "Paires.ods"
IMAGES_DIR = APP_DIR / "Images"
DEFAULT_IMAGE = IMAGES_DIR / "0.png"
STATE_FILE = APP_DIR / "undercover_state.json"
LOCK_FILE = APP_DIR / "undercover_state.lock"
LIVE_REFRESH_INTERVAL = "2s"

ODS_NS = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


def live_fragment(run_every: str | int | float | None = None):
    fragment_api = getattr(st, "fragment", None) or getattr(st, "experimental_fragment", None)

    def decorator(func):
        if fragment_api is None:
            return func
        return fragment_api(run_every=run_every)(func)

    return decorator


def now_ts() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex


def normalize_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


@contextmanager
def file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+b") as handle:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def read_state_unlocked() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"rooms": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"rooms": {}}


def write_state_unlocked(data: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", dir=STATE_FILE.parent) as tmp:
        json.dump(data, tmp, ensure_ascii=False, indent=2)
        temp_name = tmp.name
    os.replace(temp_name, STATE_FILE)


def update_state(mutator):
    with file_lock(LOCK_FILE):
        data = read_state_unlocked()
        result = mutator(data)
        write_state_unlocked(data)
        return result


def get_state() -> dict[str, Any]:
    with file_lock(LOCK_FILE):
        return read_state_unlocked()


def list_joinable_rooms() -> list[dict[str, Any]]:
    state = get_state()
    rooms = []
    for room in state["rooms"].values():
        host = room["players"].get(room["host_id"])
        if host is None or host.get("removed"):
            continue
        if room.get("match") is not None:
            continue
        players = [player for player in room["players"].values() if not player.get("removed")]
        rooms.append(
            {
                "code": room["code"],
                "host_name": host["name"],
                "players_count": len(players),
                "created_at": room.get("created_at", 0),
            }
        )
    return sorted(rooms, key=lambda item: (item["host_name"].lower(), item["created_at"]))


def cell_texts(cell: ET.Element) -> list[str]:
    values = []
    for paragraph in cell.findall("text:p", ODS_NS):
        text = "".join(paragraph.itertext()).strip()
        if text:
            values.append(text)
    return values


def iter_sheet_rows(sheet: ET.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in sheet.findall("table:table-row", ODS_NS):
        values: list[str] = []
        for cell in row.findall("table:table-cell", ODS_NS):
            repeat = int(
                cell.attrib.get("{urn:oasis:names:tc:opendocument:xmlns:table:1.0}number-columns-repeated", "1")
            )
            value = " ".join(cell_texts(cell))
            values.extend([value] * repeat)
        while values and not values[-1].strip():
            values.pop()
        rows.append(values)
    return rows


@st.cache_data(show_spinner=False)
def load_game_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"Fichier introuvable: {DATA_FILE}")

    with zipfile.ZipFile(DATA_FILE) as archive:
        root = ET.fromstring(archive.read("content.xml"))

    pairs: list[list[str]] = []
    titles: dict[str, str] = {}

    for sheet in root.findall(".//table:table", ODS_NS):
        name = sheet.attrib.get("{urn:oasis:names:tc:opendocument:xmlns:table:1.0}name", "")
        rows = iter_sheet_rows(sheet)

        if name.lower() == "paires":
            for row in rows:
                items = [item.strip() for item in row if item.strip()]
                if len(items) >= 2 and not all(item.isdigit() for item in items):
                    pairs.append(items)

        if name.lower() == "titres":
            for row in rows:
                items = [item.strip() for item in row if item.strip()]
                if len(items) < 2:
                    continue
                if any(item.lower() == "personnage" for item in items) and any(item.lower() == "titre" for item in items):
                    continue
                character, title = items[-1], items[-2]
                if title.lower() == "titre" or character.lower() == "personnage":
                    continue
                titles[character] = title

    image_lookup: dict[str, Path] = {}
    if IMAGES_DIR.exists():
        for image_path in IMAGES_DIR.iterdir():
            if image_path.is_file() and image_path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                image_lookup[normalize_name(image_path.stem)] = image_path

    if not pairs:
        raise ValueError("Aucune paire exploitable n'a été trouvée dans la feuille Paires.")

    return {"pairs": pairs, "titles": titles, "images": image_lookup}


def resolve_image(character: str, game_data: dict[str, Any]) -> Path:
    normalized = normalize_name(character)
    return game_data["images"].get(normalized, DEFAULT_IMAGE)


def generate_room_code(data: dict[str, Any]) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(random.choice(alphabet) for _ in range(5))
        if code not in data["rooms"]:
            return code


def current_vote_status(room: dict[str, Any]) -> str:
    vote_mode = room.get("settings", {}).get("vote_mode", "all_players")
    return "vote" if vote_mode == "all_players" else "host_vote"


def initialize_match(room: dict[str, Any], game_data: dict[str, Any]) -> None:
    active_players = [player_id for player_id, player in room["players"].items() if not player.get("removed")]
    if len(active_players) < 3:
        raise ValueError("Il faut au moins 3 joueurs pour lancer une partie.")

    pair_group = random.choice([group for group in game_data["pairs"] if len(group) >= 2])
    civilian_word, undercover_word = random.sample(pair_group, 2)
    undercover_id = random.choice(active_players)

    room["match"] = {
        "status": current_vote_status(room),
        "round_index": 1,
        "civilian_word": civilian_word,
        "undercover_word": undercover_word,
        "undercover_id": undercover_id,
        "alive_ids": active_players[:],
        "eliminated_ids": [],
        "assignments": {
            player_id: {
                "role": "undercover" if player_id == undercover_id else "civil",
                "word": undercover_word if player_id == undercover_id else civilian_word,
            }
            for player_id in active_players
        },
        "votes": {},
        "last_eliminated_id": None,
        "winner": None,
        "winner_reason": "",
        "created_at": now_ts(),
    }


def compute_scores(room: dict[str, Any]) -> None:
    match = room["match"]
    winner = match["winner"]
    if winner == "civils":
        for player_id, assignment in match["assignments"].items():
            if assignment["role"] == "civil":
                room["players"][player_id]["score"] += 1
    elif winner == "undercover":
        civilians_count = sum(1 for assignment in match["assignments"].values() if assignment["role"] == "civil")
        points = max(1, 5 - civilians_count)
        room["players"][match["undercover_id"]]["score"] += points


def resolve_vote(room: dict[str, Any]) -> None:
    match = room["match"]
    alive_ids = match["alive_ids"]
    tally: dict[str, int] = {}
    for target_id in match["votes"].values():
        tally[target_id] = tally.get(target_id, 0) + 1

    if not tally:
        match["status"] = "discussion"
        return

    max_votes = max(tally.values())
    finalists = [player_id for player_id, total in tally.items() if total == max_votes]

    if len(finalists) != 1:
        match["status"] = "result"
        match["last_eliminated_id"] = None
        match["winner"] = None
        match["winner_reason"] = "Égalité des votes: personne n'est éliminé."
        return

    eliminated_id = finalists[0]
    match["alive_ids"] = [player_id for player_id in alive_ids if player_id != eliminated_id]
    match["eliminated_ids"].append(eliminated_id)
    match["last_eliminated_id"] = eliminated_id

    if eliminated_id == match["undercover_id"]:
        match["winner"] = "civils"
        match["winner_reason"] = "Le traître a été identifié."
        compute_scores(room)
    elif len(match["alive_ids"]) <= 2:
        match["winner"] = "undercover"
        match["winner_reason"] = "Le traître a survécu jusqu'à la fin."
        compute_scores(room)
    else:
        match["winner"] = None
        match["winner_reason"] = "Un civil a été éliminé. La discussion continue."

    match["status"] = "result"


def reset_for_next_vote_round(room: dict[str, Any]) -> None:
    match = room["match"]
    match["status"] = current_vote_status(room)
    match["round_index"] += 1
    match["votes"] = {}
    match["last_eliminated_id"] = None
    match["winner"] = None
    match["winner_reason"] = ""


def create_room(host_name: str) -> tuple[str, str]:
    player_id = new_id()

    def mutator(data: dict[str, Any]) -> tuple[str, str]:
        room_code = generate_room_code(data)
        data["rooms"][room_code] = {
            "code": room_code,
            "host_id": player_id,
            "created_at": now_ts(),
            "settings": {
                "vote_mode": "all_players",
            },
            "players": {
                player_id: {
                    "id": player_id,
                    "name": host_name,
                    "score": 0,
                    "joined_at": now_ts(),
                    "removed": False,
                }
            },
            "match": None,
            "chat_log": [],
        }
        return room_code, player_id

    return update_state(mutator)


def join_room(room_code: str, player_name: str) -> tuple[bool, str | None]:
    player_id = new_id()

    def mutator(data: dict[str, Any]) -> tuple[bool, str | None]:
        room = data["rooms"].get(room_code)
        if room is None:
            return False, None
        for existing in room["players"].values():
            if existing["name"].strip().lower() == player_name.strip().lower() and not existing.get("removed"):
                return False, None
        room["players"][player_id] = {
            "id": player_id,
            "name": player_name,
            "score": 0,
            "joined_at": now_ts(),
            "removed": False,
        }
        return True, player_id

    return update_state(mutator)


def remove_player(room_code: str, player_id: str) -> None:
    def mutator(data: dict[str, Any]) -> None:
        room = data["rooms"].get(room_code)
        if room is None:
            return
        player = room["players"].get(player_id)
        if player is None:
            return
        player["removed"] = True
        match = room.get("match")
        if match and player_id in match.get("alive_ids", []):
            match["alive_ids"] = [pid for pid in match["alive_ids"] if pid != player_id]
            match["votes"].pop(player_id, None)
            for voter_id, target_id in list(match["votes"].items()):
                if target_id == player_id:
                    match["votes"].pop(voter_id, None)
        remaining = [pid for pid, info in room["players"].items() if not info.get("removed")]
        if remaining and room["host_id"] == player_id:
            room["host_id"] = remaining[0]

    update_state(mutator)


def set_query_player(room_code: str | None, player_id: str | None) -> None:
    if room_code and player_id:
        st.query_params["room"] = room_code
        st.query_params["player"] = player_id
    else:
        st.query_params.clear()


def current_room_and_player() -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None, str | None]:
    room_code = st.query_params.get("room")
    player_id = st.query_params.get("player")
    if not room_code or not player_id:
        return None, None, None, None
    state = get_state()
    room = state["rooms"].get(room_code)
    if room is None:
        return None, None, room_code, player_id
    player = room["players"].get(player_id)
    if player is None or player.get("removed"):
        return room, None, room_code, player_id
    return room, player, room_code, player_id


def start_match(room_code: str, game_data: dict[str, Any]) -> None:
    def mutator(data: dict[str, Any]) -> None:
        room = data["rooms"][room_code]
        initialize_match(room, game_data)

    update_state(mutator)


def update_vote_mode(room_code: str, vote_mode: str) -> None:
    def mutator(data: dict[str, Any]) -> None:
        room = data["rooms"][room_code]
        if room.get("match") is not None:
            return
        room.setdefault("settings", {})
        room["settings"]["vote_mode"] = vote_mode

    update_state(mutator)


def advance_to_discussion(room_code: str) -> None:
    def mutator(data: dict[str, Any]) -> None:
        match = data["rooms"][room_code]["match"]
        if match and match["status"] == "reveal":
            match["status"] = "discussion"

    update_state(mutator)


def mark_ready(room_code: str, player_id: str) -> None:
    def mutator(data: dict[str, Any]) -> None:
        room = data["rooms"][room_code]
        match = room["match"]
        if match["status"] != "reveal":
            return
        if player_id not in match["ready_ids"]:
            match["ready_ids"].append(player_id)
        if set(match["ready_ids"]) >= set(match["alive_ids"]):
            match["status"] = "discussion"

    update_state(mutator)


def open_vote_phase(room_code: str) -> None:
    def mutator(data: dict[str, Any]) -> None:
        room = data["rooms"][room_code]
        match = room["match"]
        if match:
            vote_mode = room.get("settings", {}).get("vote_mode", "all_players")
            match["status"] = "vote" if vote_mode == "all_players" else "host_vote"
            match["votes"] = {}

    update_state(mutator)
def submit_vote(room_code: str, player_id: str, target_id: str) -> None:
    def mutator(data: dict[str, Any]) -> None:
        room = data["rooms"][room_code]
        match = room["match"]
        if match["status"] != "vote":
            return
        if player_id not in match["alive_ids"] or target_id not in match["alive_ids"]:
            return
        if player_id == target_id:
            return
        match["votes"][player_id] = target_id
        if set(match["votes"]) >= set(match["alive_ids"]):
            resolve_vote(room)

    update_state(mutator)


def continue_after_result(room_code: str, game_data: dict[str, Any]) -> None:
    def mutator(data: dict[str, Any]) -> None:
        room = data["rooms"][room_code]
        match = room["match"]
        if match["winner"]:
            initialize_match(room, game_data)
        else:
            reset_for_next_vote_round(room)

    update_state(mutator)


def host_eliminate_player(room_code: str, eliminated_id: str | None) -> None:
    def mutator(data: dict[str, Any]) -> None:
        room = data["rooms"][room_code]
        match = room["match"]
        if match["status"] != "host_vote":
            return
        match["votes"] = {}
        if eliminated_id is None or eliminated_id not in match["alive_ids"]:
            return

        match["alive_ids"] = [player_id for player_id in match["alive_ids"] if player_id != eliminated_id]
        match["eliminated_ids"].append(eliminated_id)
        match["last_eliminated_id"] = eliminated_id

        if eliminated_id == match["undercover_id"]:
            match["winner"] = "civils"
            match["winner_reason"] = "Le traître a été identifié."
            compute_scores(room)
        elif len(match["alive_ids"]) <= 2:
            match["winner"] = "undercover"
            match["winner_reason"] = "Le traître a survécu jusqu'à la fin."
            compute_scores(room)
        else:
            match["winner"] = None
            match["winner_reason"] = "Un joueur a été éliminé. La discussion continue."

        match["status"] = "result"

    update_state(mutator)


def player_label(room: dict[str, Any], player_id: str) -> str:
    player = room["players"][player_id]
    return player["name"]


def render_player_card(room: dict[str, Any], player_id: str, game_data: dict[str, Any]) -> None:
    match = room["match"]
    assignment = match["assignments"][player_id]
    word = assignment["word"]
    title = game_data["titles"].get(word)
    image_path = resolve_image(word, game_data)

    st.subheader("Votre mot")
    st.image(str(image_path), width=220)
    st.markdown(f"### {word}")
    if title:
        st.caption(title)


def render_lobby(room: dict[str, Any], player_id: str, game_data: dict[str, Any]) -> None:
    host_id = room["host_id"]
    active_players = [player for player in room["players"].values() if not player.get("removed")]
    st.subheader("Lobby")
    st.write(f"Partie hébergée par **{room['players'][host_id]['name']}**")
    st.write("Joueurs connectés :")
    for player in sorted(active_players, key=lambda item: item["joined_at"]):
        suffix = " (hôte)" if player["id"] == host_id else ""
        st.write(f"- {player['name']}{suffix}")

    st.info("La partie démarre à partir de 3 joueurs. Chacun garde ensuite sa page ouverte et clique sur Rafraîchir si nécessaire.")

    if player_id == host_id:
        vote_mode = room.get("settings", {}).get("vote_mode", "all_players")
        options = {
            "Vote dans l'application": "all_players",
            "Vote à main levée, résultat saisi par l'hôte": "host_only",
        }
        selected_label = st.radio(
            "Mode de vote",
            list(options.keys()),
            index=0 if vote_mode == "all_players" else 1,
        )
        selected_mode = options[selected_label]
        if selected_mode != vote_mode:
            update_vote_mode(room["code"], selected_mode)
            st.rerun()
        if st.button("Lancer la partie", type="primary", disabled=len(active_players) < 3):
            start_match(room["code"], game_data)
            st.rerun()


def render_discussion(room: dict[str, Any]) -> None:
    match = room["match"]
    alive_names = [player_label(room, player_id) for player_id in match["alive_ids"]]
    st.subheader(f"Discussion - manche {match['round_index']}")
    st.write("Joueurs encore en lice : " + ", ".join(alive_names))
    st.info("Discutez ensemble puis l'hôte ouvre la phase de vote.")


def render_vote(room: dict[str, Any], player_id: str) -> None:
    match = room["match"]
    alive_ids = match["alive_ids"]
    st.subheader(f"Vote - manche {match['round_index']}")
    st.write("Choisissez la personne à éliminer.")

    if player_id not in alive_ids:
        st.warning("Vous avez été éliminé et vous ne votez plus.")
        return

    options = {
        room["players"][target_id]["name"]: target_id
        for target_id in alive_ids
        if target_id != player_id
    }
    if not options:
        st.warning("Aucune cible disponible.")
        return

    target_name = st.radio("Votre vote", list(options.keys()), key=f"vote-{match['round_index']}")
    already_voted = match["votes"].get(player_id)
    if already_voted:
        st.caption(f"Vote enregistré pour : {room['players'][already_voted]['name']}")
    if st.button("Valider mon vote", type="primary"):
        submit_vote(room["code"], player_id, options[target_name])
        st.rerun()

    st.progress(len(match["votes"]) / max(1, len(alive_ids)), text=f"Votes reçus : {len(match['votes'])}/{len(alive_ids)}")


def render_host_vote(room: dict[str, Any], player_id: str) -> None:
    match = room["match"]
    host_id = room["host_id"]
    st.subheader(f"Vote à main levée - manche {match['round_index']}")
    if player_id != host_id:
        st.info("Votez dans la pièce puis attendez que l'hôte saisisse le résultat.")
        return

    st.write("Sélectionnez le joueur éliminé après le vote à main levée.")
    options = {}
    for target_id in match["alive_ids"]:
        options[room["players"][target_id]["name"]] = target_id

    choice = st.radio("Résultat du vote", list(options.keys()), key=f"host-vote-{match['round_index']}")
    if st.button("Enregistrer le résultat", type="primary"):
        host_eliminate_player(room["code"], options[choice])
        st.rerun()


def render_result(room: dict[str, Any]) -> None:
    match = room["match"]
    st.subheader("Résultat")
    if match["last_eliminated_id"]:
        eliminated_name = room["players"][match["last_eliminated_id"]]["name"]
        st.write(f"Joueur éliminé : **{eliminated_name}**")
    else:
        st.write("Aucun joueur éliminé.")

    st.write(match["winner_reason"])
    if match["winner"] == "civils":
        st.success("Victoire des civils")
    elif match["winner"] == "undercover":
        st.error("Victoire du traître")
    else:
        st.warning("La partie continue")

    if match["winner"]:
        undercover_name = room["players"][match["undercover_id"]]["name"]
        st.write(f"Traître : **{undercover_name}**")
        st.write(f"Mot des civils : **{match['civilian_word']}**")
        st.write(f"Mot du traître : **{match['undercover_word']}**")


def render_scoreboard(room: dict[str, Any]) -> None:
    active_players = [player for player in room["players"].values() if not player.get("removed")]
    ranking = sorted(active_players, key=lambda item: (-item["score"], item["name"].lower()))
    st.subheader("Scores")
    for player in ranking:
        st.write(f"- {player['name']} : {player['score']} point(s)")


@live_fragment(run_every=LIVE_REFRESH_INTERVAL)
def render_live_sidebar() -> None:
    room, player, room_code, player_id = current_room_and_player()
    st.header("Navigation")
    if st.button("Rafraîchir"):
        st.rerun()
    if room and player:
        st.write(f"Connecté en tant que **{player['name']}**")
        st.write(f"Salle : `{room['code']}`")
        if st.button("Quitter la salle"):
            remove_player(room["code"], player["id"])
            set_query_player(None, None)
            st.rerun()
    if room:
        render_scoreboard(room)
    else:
        st.caption("Aucune salle rejointe.")


@live_fragment(run_every=LIVE_REFRESH_INTERVAL)
def render_live_main(game_data: dict[str, Any]) -> None:
    room, player, room_code, player_id = current_room_and_player()

    if room is None or player is None:
        st.subheader("Créer ou rejoindre une partie")
        available_rooms = list_joinable_rooms()
        create_col, join_col = st.columns(2)

        with create_col:
            with st.form("create-room"):
                host_name = st.text_input("Votre nom", key="host-name")
                create_submitted = st.form_submit_button("Créer une partie", type="primary")
                if create_submitted:
                    cleaned = host_name.strip()
                    if not cleaned:
                        st.error("Entrez un nom pour créer la partie.")
                    else:
                        new_room_code, new_player_id = create_room(cleaned)
                        set_query_player(new_room_code, new_player_id)
                        st.rerun()

        with join_col:
            join_name = st.text_input("Votre nom pour rejoindre", key="join-name")
            st.write("Parties ouvertes :")
            if not available_rooms:
                st.caption("Aucune partie en attente pour le moment.")
            for room_info in available_rooms:
                label = f"Rejoindre {room_info['host_name']} ({room_info['players_count']} joueur(s))"
                if st.button(label, key=f"join-{room_info['code']}"):
                    if not join_name.strip():
                        st.error("Entrez votre nom avant de rejoindre une partie.")
                    else:
                        success, joined_player_id = join_room(room_info["code"], join_name.strip())
                        if not success or not joined_player_id:
                            st.error("Impossible de rejoindre cette partie. Choisissez un nom unique.")
                        else:
                            set_query_player(room_info["code"], joined_player_id)
                            st.rerun()

        if room_code and player_id:
            st.warning("La partie ou le joueur demandé n'existe plus.")
            if st.button("Réinitialiser la session"):
                set_query_player(None, None)
                st.rerun()
        return

    match = room.get("match")
    host_id = room["host_id"]
    is_host = player["id"] == host_id

    left_col, right_col = st.columns([1.2, 1], gap="large")

    with left_col:
        if match is None:
            render_lobby(room, player["id"], game_data)
        else:
            render_player_card(room, player["id"], game_data)

            if match["status"] == "discussion":
                render_discussion(room)

            elif match["status"] == "vote":
                st.info("Discutez librement puis votez quand vous êtes prêts.")
                render_vote(room, player["id"])

            elif match["status"] == "host_vote":
                st.info("Discutez librement puis l'hôte saisit directement le résultat du vote.")
                render_host_vote(room, player["id"])

            elif match["status"] == "result":
                render_result(room)

    with right_col:
        st.subheader("État de la partie")
        st.write(f"Hôte : **{room['players'][host_id]['name']}**")

        if match is None:
            st.write("La salle attend le lancement de la partie.")
        else:
            phase_labels = {
                "vote": "vote",
                "host_vote": "vote à main levée",
                "result": "résultat",
            }
            st.write(f"Phase actuelle : **{phase_labels.get(match['status'], match['status'])}**")
            st.write(f"Manche : **{match['round_index']}**")
            st.write("Joueurs en vie :")
            for alive_id in match["alive_ids"]:
                st.write(f"- {room['players'][alive_id]['name']}")

        if is_host and match is not None:
            st.divider()
            st.subheader("Contrôles hôte")
            if match["status"] == "host_vote":
                st.write("L'hôte doit saisir le résultat du vote à main levée.")

            elif match["status"] == "result":
                label = "Relancer immédiatement" if match["winner"] else "Manche suivante"
                if st.button(label, type="primary"):
                    continue_after_result(room["code"], game_data)
                    st.rerun()


def main() -> None:
    st.set_page_config(page_title="Undercover", page_icon="🕵️", layout="wide")
    st.title("Undercover multijoueur")
    st.caption("Application Streamlit pour créer une salle, distribuer les rôles et gérer les votes.")

    try:
        game_data = load_game_data()
    except Exception as exc:
        st.error(f"Impossible de charger les données du jeu : {exc}")
        st.stop()

    with st.sidebar:
        render_live_sidebar()
    render_live_main(game_data)


if __name__ == "__main__":
    main()
