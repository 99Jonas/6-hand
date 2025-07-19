import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room
import string
import threading
import time
from queue import Queue
import random

app = Flask(__name__)
socketio = SocketIO(app, ping_timeout=300, ping_interval=10, async_mode='eventlet')

# Tracks room code -> data
rooms = {}  # room_code: { players, player_names, game_state }
user_sid_to_room = {}
user_sid_to_name = {}


class Card:
    __suit_to_symbol = {
        "spades": "♠",
        "clubs": "♣",
        "hearts": "♥",
        "diamonds": "♦"
    }
    __rank_to_symbol = {
        "9": "9",
        "10": "10",
        "jack": "J",
        "queen": "Q",
        "king": "K",
        "ace": "A"
    }

    def __init__(self, suit, rank):
        self.suit = suit.lower() if suit else ""
        self.rank = rank.lower() if rank else ""

        if self.rank in Card.__rank_to_symbol and self.suit in Card.__suit_to_symbol:
            self.val = f"{Card.__rank_to_symbol[self.rank]}{Card.__suit_to_symbol[self.suit]}"
        else:
            self.val = ""

    def __eq__(self, other):
        return isinstance(other, Card) and self.suit == other.suit and self.rank == other.rank

    def __hash__(self):
        return hash((self.suit, self.rank))


class Deck():
    def __init__(self):
        self.deck = []

    def initialize_deck(self):
        suits = ["diamonds", "hearts", "clubs", "spades"]
        ranks = ["9", "10", "jack", "queen", "king", "ace"]
        for _ in range(2):
            for suit in suits:
                for rank in range(6):
                    self.deck.append(Card(suit, ranks[rank]))
        return self

    def shuffle_deck(self):
        random.shuffle(self.deck)
        return self

    def draw_card(self):
        return self.deck.pop(0)


class Bid():
    def __init__(self, bid, suit):
        self.bid = bid
        self.suit = suit


def reset_game_state(room_code):
    rooms[room_code]["game_state"] = {
        "scores": rooms[room_code]["game_state"]["scores"],
        "tricks": {"Team_1": 0, "Team_2": 0},
        "length": [0, 0, 0, 0, 0, 0],
        "hands": [[], [], [], [], [], []],
        "plays": [None, None, None, None, None, None],
        "players": rooms[room_code]["game_state"]["players"],
        "bids": ["", "", "", "", "", ""]
    }

def reset_plays(room_code):
    rooms[room_code]["game_state"]["plays"] = [None, None, None, None, None, None]

def gen_card(val):
    symbol_to_suit = {
        "♠": "spades",
        "♣": "clubs",
        "♥": "hearts",
        "♦": "diamonds"
    }
    symbol_to_rank = {
        "9": "9",
        "10": "10",
        "J": "jack",
        "Q": "queen",
        "K": "king",
        "A": "ace"
    }
    suit_symbol = val[-1]
    rank_symbol = val[:-1]

    suit = symbol_to_suit[suit_symbol]
    rank = symbol_to_rank[rank_symbol.upper()]

    return Card(suit, rank)

def render(room_code):
    for sid in rooms[room_code]["players"]:
        r_g_s = rooms[room_code]["game_state"]
        indx = rooms[room_code]["order"].index(rooms[room_code]["player_names"][sid])
        game_state = {
            "scores": {"us": r_g_s["scores"]["Team_1"], "them": r_g_s["scores"]["Team_2"]} if indx % 2 == 0 else {"us": r_g_s["scores"]["Team_2"], "them": r_g_s["scores"]["Team_1"]},
            "tricks": {"us": r_g_s["tricks"]["Team_1"], "them": r_g_s["tricks"]["Team_2"]} if indx % 2 == 0 else {"us": r_g_s["tricks"]["Team_2"], "them": r_g_s["tricks"]["Team_1"]},
            "length": [],
            "plays": [],
            "players": [],
            "bids": []
        }
        game_state["hand"] = rooms[room_code]["game_state"]["hands"][indx]
        for x in range(6):
            x = (x + indx + 1) % 6
            if x != indx:
                game_state["length"].append(r_g_s["length"][x])
                play = r_g_s["plays"][x]
                game_state["plays"].append(play.val if play else "")
                game_state["players"].append(r_g_s["players"][x])
                game_state["bids"].append(r_g_s["bids"][x])
            else:
                play = r_g_s["plays"][x]
                game_state["plays"].append(play.val if play else "")
                game_state["bids"].append(r_g_s["bids"][x])

        socketio.emit('game_state', game_state, to=sid)

def update_hands(room_code):
    hands = rooms[room_code]["game_hands"]
    val_hands = []
    for hand in hands:
        val_hand = []
        for card in hand:
            val_hand.append(card.val)
        val_hands.append(val_hand)
    rooms[room_code]["game_state"]["hands"] = val_hands
    rooms[room_code]["game_state"]["length"] = [len(hand) for hand in val_hands]

def sort_hand(hand):
    new_hand = []
    suits = ["diamonds", "hearts", "clubs", "spades"]
    ranks = ["9", "10", "jack", "queen", "king", "ace"]
    for suit in suits:
        suit_hand = []
        for card in hand:
            if card.suit == suit:
                suit_hand.append(card)
        for rank in ranks:
            for card in suit_hand:
                if card.rank == rank:
                    new_hand.append(card)
    return new_hand

def sort_hands(hands):
    new_hands = []
    for hand in hands:
        new_hands.append(sort_hand(hand))
    return new_hands

def get_trump(room_code, start):
    dic = {"♦": "diamonds", "♥": "hearts", "♣": "clubs", "♠": "spades", "↓": "low", "↑": "high", "Pass": "pass", "Shoot": "shoot", "Double Shoot": "shoot", "Triple Shoot": "shoot"}
    bids = []
    shoots = ["Shoot", "Double Shoot", " Triple Shoot"]
    highest = 0
    already_shot = False
    shooot = 0

    for x in range(6):
        bider = (x + start) % 6
        nts = {v: k for k, v in rooms[room_code]["player_names"].items()}
        sid = nts[rooms[room_code]["game_state"]["players"][bider]]
        if x == 5 and highest < 4:
            socketio.emit("bid_now", {"highest": highest, "alreadyShot": already_shot, "mustBid": True}, to=sid)
        else:
            socketio.emit("bid_now", {"highest": highest, "alreadyShot": already_shot, "mustBid": False}, to=sid)
        bid = rooms[room_code]["queues"]["bids"].get()[1]
        if bid["bid"] > highest:
            highest = bid["bid"]
        if bid["bid"] < 9:
            bids.append(Bid(bid["bid"], dic[bid["suit"]]))
        else:
            shooot += 1
            bids.append(Bid(bid["bid"] + shooot, dic[bid["suit"]]))
        if bid["bid"] > 8:
            rooms[room_code]["game_state"]["bids"][bider] = f"{shoots[bid['bid'] - 9]}"
        elif bid["bid"] == 0:
            rooms[room_code]["game_state"]["bids"][bider] = "Pass"
        else:
            rooms[room_code]["game_state"]["bids"][bider] = f"{bid['bid']} {bid['suit']}"
        render(room_code)
    return bids

def get_plays(room_code, start, high, shoots=None, shoot=False):
    plays = []
    first = None
    for x in range(6):
        y = (start - 6) + x
        if shoot:
            if rooms[room_code]["order"][y] in shoots:
                continue
        nts = {v: k for k, v in rooms[room_code]["player_names"].items()}
        sid = nts[rooms[room_code]["game_state"]["players"][y]]
        socketio.emit("play_now", {"x": x, "high": {"bid": high.bid, "suit": high.suit}, "first": first}, to=sid)
        card = rooms[room_code]["queues"]["play_cards"].get()[1]
        if x == 0:
            first = card
        plays.append(gen_card(f"{card['rank']}{card['suit']}"))
        rooms[room_code]["game_hands"][y].remove(gen_card(f"{card['rank']}{card['suit']}"))
        rooms[room_code]["game_state"]["plays"][y] = gen_card(f"{card['rank']}{card['suit']}")
        update_hands(room_code)
        render(room_code)
    time.sleep(3)
    return plays

def get_winner(plays, high):
    high = high.suit.lower()
    dic = {"diamonds": "hearts", "hearts": "diamonds", "clubs": "spades", "spades": "clubs"}
    def get_rank(card, high):
        all_ranks = [["9", "10", "jack", "queen", "king", "ace"], ["9", "10", "queen", "king", "ace", "jack"]]
        if card.suit == high:
            rank = all_ranks[1].index(card.rank)
        else:
            rank = all_ranks[0].index(card.rank)
        return rank
    highest = plays[0]
    high_index = 0
    if high == "low":
        for card in plays[1::]:
            if get_rank(highest, high) > get_rank(card, high) and card.suit == highest.suit:
                highest = card
                high_index = plays.index(card)
            else:
               continue
    elif high == "high":
        for card in plays[1::]:
            if get_rank(highest, high) < get_rank(card, high) and card.suit == highest.suit:
                highest = card
                high_index = plays.index(card)
            else:
                continue
    else:
        for card in plays[1::]:
            if highest.suit != high and card.suit == high:
                if dic[high] == highest.suit and highest.rank == "jack" and card.rank != "jack":
                    continue
                else:
                    highest = card
                    high_index = plays.index(card)
            elif highest.suit == high and card.suit != high:
                if dic[high] == card.suit and card.rank == "jack" and highest.rank != "jack":
                    highest = card
                    high_index = plays.index(card)
                else:
                    continue
            elif highest.suit == high and card.suit == high:
                if get_rank(highest, high) >= get_rank(card, high):
                    continue
                else:
                    highest = card
                    high_index = plays.index(card)
            else:
                if dic[high] == highest.suit and highest.rank == "jack":
                    continue
                else:
                    if dic[high] == card.suit and card.rank == "jack":
                        highest = card
                        high_index = plays.index(card)
                    else:
                        if get_rank(highest, high) >= get_rank(card, high):
                            continue
                        else:
                            highest = card
                            high_index = plays.index(card)
    return high_index

def change_cards(room_code, bid_index):
    symbol_to_suit = {
        "♠": "spades",
        "♣": "clubs",
        "♥": "hearts",
        "♦": "diamonds",
        "↑": "high",
        "↓": "low"
    }
    nts = {v: k for k, v in rooms[room_code]["player_names"].items()}
    sid = nts[rooms[room_code]["game_state"]["players"][bid_index]]
    team_1 = rooms[room_code]["game_state"]["players"][(bid_index + 2) % 6]
    team_2 = rooms[room_code]["game_state"]["players"][(bid_index + 4) % 6]
    socketio.emit("shoot_now", {"teammates": [team_1, team_2]}, to=sid)
    answer = rooms[room_code]["queues"]["shoots"].get()[1]
    high = Bid(9, symbol_to_suit[answer["trump"]])
    teammate_1 = answer[team_1]
    teammate_2 = answer[team_2]
    for card in answer["rid"]:
        rooms[room_code]["game_hands"][bid_index].remove(gen_card(f"{card['rank']}{card['suit']}"))
    rooms[room_code]["game_state"]["bids"][bid_index] = answer["trump"]
    rooms[room_code]["game_state"]["bids"][(bid_index + 2) % 6] = teammate_1
    rooms[room_code]["game_state"]["bids"][(bid_index + 4) % 6] = teammate_2
    update_hands(room_code)
    render(room_code)
    for _ in range(teammate_1):
        sid = nts[rooms[room_code]["game_state"]["players"][(bid_index + 2) % 6]]
        socketio.emit("give_shoot", to=sid)
        card = rooms[room_code]["queues"]["give"].get()[1]
        rooms[room_code]["game_hands"][(bid_index + 2) % 6].remove(gen_card(f"{card['rank']}{card['suit']}"))
        rooms[room_code]["game_hands"][bid_index].append(gen_card(f"{card['rank']}{card['suit']}"))
        rooms[room_code]["game_hands"][bid_index] = sort_hand(rooms[room_code]["game_hands"][bid_index])
        update_hands(room_code)
        render(room_code)
    for _ in range(teammate_2):
        sid = nts[rooms[room_code]["game_state"]["players"][(bid_index + 4) % 6]]
        socketio.emit("give_shoot", to=sid)
        card = rooms[room_code]["queues"]["give"].get()[1]
        rooms[room_code]["game_hands"][(bid_index + 4) % 6].remove(gen_card(f"{card['rank']}{card['suit']}"))
        rooms[room_code]["game_hands"][bid_index].append(gen_card(f"{card['rank']}{card['suit']}"))
        rooms[room_code]["game_hands"][bid_index] = sort_hand(rooms[room_code]["game_hands"][bid_index])
        update_hands(room_code)
        render(room_code)
    rooms[room_code]["game_state"]["bids"] = ["" if x != bid_index else rooms[room_code]["game_state"]["bids"][x] for x in range(6)]
    render(room_code)

    return high

def generate_room_code(length=5):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('create_room')
def handle_create_room(data):
    room_code = generate_room_code()
    rooms[room_code] = {
        "host_sid": request.sid,
        "players": set(),
        "player_names": {},  # sid -> username
        "order": [],
        "game_hands": [],
        "game_state": {
            "scores": {"Team_1": [0], "Team_2": [0]},
            "tricks": {"Team_1": 0, "Team_2": 0},
            "length": [0, 0, 0, 0, 0, 0],
            "hands": [],
            "plays": [Card("", ""), Card("", ""), Card("", ""), Card("", ""), Card("", ""), Card("", "")],
            "players": [],
            "bids": ["", "", "", "", "", ""]
        },
        "queues": {
            "bids": Queue(),
            "play_cards": Queue(),
            "shoots": Queue(),
            "give": Queue()
        }
    }

    join_room(room_code)
    user_sid_to_room[request.sid] = room_code
    rooms[room_code]["players"].add(request.sid)
    username = data.get('username')

    # Assign host a default username
    user_sid_to_name[request.sid] = username
    rooms[room_code]["player_names"][request.sid] = username  # Add this!

    # Emit join_success to host with is_host True
    emit('join_success', {
        "room_code": room_code,
        "username": user_sid_to_name[request.sid],
        "is_host": True
    }, room=request.sid)

@socketio.on('player_bid')
def handle_bid(data):
    sid = request.sid
    room_code = user_sid_to_room.get(sid)
    if not room_code or room_code not in rooms:
        return

    player = user_sid_to_name.get(sid)
    bid = data

    # 👇 Feed the card back into the game loop
    rooms[room_code]["queues"]["bids"].put((player, bid))

@socketio.on("signal")
def on_signal(data):
    target = data["target"]
    emit("signal", {
        "from": request.sid,
        "signal": data["signal"]
    }, to=target)

@socketio.on('shoot_ans')
def handle_shoot(data):
    sid = request.sid
    room_code = user_sid_to_room.get(sid)
    if not room_code or room_code not in rooms:
        return

    player = user_sid_to_name.get(sid)
    shoot = data

    # 👇 Feed the card back into the game loop
    rooms[room_code]["queues"]["shoots"].put((player, shoot))

@socketio.on('give_card')
def handle_play_card(data):
    sid = request.sid
    room_code = user_sid_to_room.get(sid)
    if not room_code or room_code not in rooms:
        return

    player = user_sid_to_name.get(sid)
    card = data

    # 👇 Feed the card back into the game loop
    rooms[room_code]["queues"]["give"].put((player, card))

@socketio.on('play_card')
def handle_play_card(data):
    sid = request.sid
    room_code = user_sid_to_room.get(sid)
    if not room_code or room_code not in rooms:
        return

    player = user_sid_to_name.get(sid)
    card = data

    # 👇 Feed the card back into the game loop
    rooms[room_code]["queues"]["play_cards"].put((player, card))

@socketio.on('start_game')
def handle_start_game(data):
    sid = request.sid
    room_code = user_sid_to_room.get(sid)
    if not room_code or room_code not in rooms:
        return
    rooms[room_code]["order"].append(rooms[room_code]["player_names"][rooms[room_code]["host_sid"]])
    for point in data:
        rooms[room_code]["order"].append(point)
    rooms[room_code]["order"].append(rooms[room_code]["order"].pop(0))
    rooms[room_code]["game_state"]["players"] = rooms[room_code]["order"]
    emit("game_started", room=room_code)
    threading.Thread(target=game_logic, args=(room_code,)).start()

@socketio.on('join_room')
def handle_join(data):
    room_code = data.get('room_code')
    username = data.get('username')

    if not username:
        emit('join_failed', {"error": "No username provided"})
        return

    if room_code in rooms:
        if username in rooms[room_code]["player_names"].values():
            emit('join_failed', {"error": "Username already taken in this room"})
            return

        join_room(room_code)
        user_sid_to_room[request.sid] = room_code
        user_sid_to_name[request.sid] = username

        rooms[room_code]["players"].add(request.sid)
        rooms[room_code]["player_names"][request.sid] = username

        # socketio.emit('game_state', game_state, to=request.sid)
        emit('join_success', {"room_code": room_code, "username": username})

        # 🔁 Send existing user list to the new joiner (for peer connections)
        existing_users = [
            sid for sid in rooms[room_code]["players"]
            if sid != request.sid
        ]
        emit('existing_users', {"users": existing_users}, to=request.sid)

        # 🔔 Inform existing users that someone joined (for reverse connections)
        for sid in existing_users:
            emit('user_joined', {"sid": request.sid}, to=sid)

        # 👑 Update the host with the full player name list (for dropdowns)
        host_sid = rooms[room_code]["host_sid"]
        all_names = [
            name for sid, name in rooms[room_code]["player_names"].items()
            if sid != host_sid
        ]
        emit('player_list', all_names, to=host_sid)

    else:
        emit('join_failed', {"error": "Room not found"})


@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    room_code = user_sid_to_room.get(sid)

    if room_code and room_code in rooms:
        rooms[room_code]["players"].discard(sid)
        rooms[room_code]["player_names"].pop(sid, None)

        if not rooms[room_code]["players"]:
            del rooms[room_code]
        else:
            host_sid = rooms[room_code]["host_sid"]

            # Exclude host by comparing usernames
            all_names = [
                name for sid, name in rooms[room_code]["player_names"].items()
                if sid != host_sid
            ]
            emit('player_list', all_names[1:], to=host_sid)

    user_sid_to_room.pop(sid, None)
    user_sid_to_name.pop(sid, None)

def game_logic(room_code):
    team_1 = 0
    team_2 = 0
    end = False
    begin = 0
    while not end:
        team_1_t = 0
        team_2_t = 0
        deck = Deck().initialize_deck().shuffle_deck()
        hands = sort_hands([[deck.draw_card() for _ in range(8)] for _ in range(6)])
        rooms[room_code]["game_hands"] = hands
        update_hands(room_code)
        render(room_code)
        bids = get_trump(room_code, begin)
        highest_bid = 0
        high = None
        for bid in bids:
            if bid.bid > highest_bid:
                highest_bid = bid.bid
                high = bid
        bid_index = (bids.index(high) + begin) % 6
        rooms[room_code]["game_state"]["bids"] = ["" if x != bid_index else rooms[room_code]["game_state"]["bids"][x] for x in range(6)]
        render(room_code)
        start = bid_index
        if high.bid > 8:
            high = change_cards(room_code, bid_index)
            shots = []
            shots.append(rooms[room_code]["order"][(2 + start) % 6])
            shots.append(rooms[room_code]["order"][(4 + start) % 6])
            while team_1_t + team_2_t != 8:
                plays = get_plays(room_code, start, high, shoots=shots, shoot=True)
                win_index = get_winner(plays, high)
                ind = win_index + start
                ind = ind % 6
                if (ind) % 2 == 0:
                    rooms[room_code]["game_state"]["tricks"]["Team_1"] += 1
                    team_1_t += 1
                else:
                    rooms[room_code]["game_state"]["tricks"]["Team_2"] += 1
                    team_2_t += 1
                reset_plays(room_code)
                render(room_code)
                start = ind
            if (bid_index) % 2 == 0:
                if team_1_t == 8:
                    if high.bid < 11:
                        team_1 += 16 * (high.bid - 8)
                    else:
                        team_1 += 64
                else:
                    if high.bid < 11:
                        team_1 -= 16 * (high.bid - 8)
                        team_2 += team_2_t
                    else:
                        team_1 -= 64
                        team_2 += team_2_t
            else:
                if team_2_t == 8:
                    if high.bid < 11:
                        team_2 += 16 * (high.bid - 8)
                    else:
                        team_2 += 64
                else:
                    if high.bid < 11:
                        team_2 -= 16 * (high.bid - 8)
                        team_1 += team_1_t
                    else:
                        team_2 -= 64
                        team_1 += team_1_t
        else:
            while team_1_t + team_2_t != 8:
                plays = get_plays(room_code, start, high)
                win_index = get_winner(plays, high)
                ind = win_index + start
                ind = ind % 6
                if (ind) % 2 == 0:
                    rooms[room_code]["game_state"]["tricks"]["Team_1"] += 1
                    team_1_t += 1
                else:
                    rooms[room_code]["game_state"]["tricks"]["Team_2"] += 1
                    team_2_t += 1
                reset_plays(room_code)
                render(room_code)
                start = ind
            if (bid_index) % 2 == 0:
                if team_1_t < high.bid:
                    team_1 -= high.bid
                    team_2 += team_2_t
                else:
                    team_1 += team_1_t
                    team_2 += team_2_t
            else:
                if team_2_t < high.bid:
                    team_2 -= high.bid
                    team_1 += team_1_t
                else:
                    team_2 += team_2_t
                    team_1 += team_1_t

        rooms[room_code]["game_state"]["scores"]["Team_1"].append(team_1)
        rooms[room_code]["game_state"]["scores"]["Team_2"].append(team_2)
        reset_game_state(room_code)
        render(room_code)
        begin += 1
        if team_1 >= 52 or team_2 >= 52 or abs(team_2 - team_1) > 100:
            if team_1 != team_2:
                end = True
                rooms[room_code]["game_state"]["scores"] = {"Team_1": [0], "Team_2": [0]}
                socketio.to(room_code).emit("winner", {"winner": "Team 1" if team_1 > team_2 else "Team 2"})

if __name__ == '__main__':
    socketio.run(app, host="0.0.0.0", port=5000)
