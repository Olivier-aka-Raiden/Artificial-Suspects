# IMPORT VARIABLE ENV8
from contextlib import asynccontextmanager
import random
from openai import AsyncOpenAI, OpenAIError
from anthropic import AsyncAnthropic
from google.cloud import storage
from dotenv import load_dotenv, find_dotenv
import pickle
import os
import logging
from fastapi import FastAPI, Request, HTTPException
import discord
from discord.ext import commands
from discord import ui, SelectOption
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
import asyncio
import json
from enum import Enum
import aiohttp
import functools
import typing
import time

client = AsyncOpenAI(api_key=os.getenv('OPENAI_API_KEY'))
anthropic_client = AsyncAnthropic(
    # defaults to os.environ.get("ANTHROPIC_API_KEY")
    api_key=os.environ.get("ANTHROPIC_API_KEY"),
)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv(find_dotenv())

DISCORD_PUBLIC_KEY = os.getenv('APPLICATION_PUBLIC_KEY')  # Set your Discord public key as an environment variable
RAIDEN_USER_ID = 202814189993984000


def to_thread(func: typing.Callable) -> typing.Coroutine:
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)
    return wrapper


def clean_nickname(nickname: str) -> str:
    return nickname.replace(':', '')

async def verify_signature(request):
    verify_key = VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
    body = await request.body()
    signature = None
    timestamp = None
    if "X-Signature-Ed25519" in request.headers:
        signature = request.headers["X-Signature-Ed25519"]
    if "X-Signature-Timestamp" in request.headers:
        timestamp = request.headers["X-Signature-Timestamp"]

    if not signature or not timestamp:
        raise HTTPException(status_code=401, detail='Invalid request signature')

    try:
        verify_key.verify(f'{timestamp}{body.decode("utf-8")}'.encode(), bytes.fromhex(signature))
    except BadSignatureError:
        raise HTTPException(status_code=401, detail='Invalid request signature')


class GCPStoragePersistence:
    def __init__(self, bucket_name, file_path):
        self.client = storage.Client()
        self.bucket_name = bucket_name
        self.file_path = file_path
        self.bucket = self.client.bucket(bucket_name)
        self.blob = self.bucket.blob(file_path)

    def state_file_exists(self):
        return self.blob.exists()

    async def load_data(self):
        try:
            if self.state_file_exists():
                data = self.blob.download_as_string()
                return pickle.loads(data)
            else:
                # Create the bucket
                logger.info(f"Bucket {self.bucket_name} does not exist. Creating bucket.")
                self.client.create_bucket(self.bucket)
                logger.info(f"Bucket {self.bucket_name} created.")
                return None
        except Exception as e:
            logger.error(f"Could not load data: {e}")
            return None

    async def save_data(self, data):
        try:
            serialized_data = pickle.dumps(data)
            await asyncio.to_thread(self.blob.upload_from_string, serialized_data)
            logger.info("Data saved to GCP.")
        except Exception as e:
            logger.error(f"Could not save data: {e}")

class Mode(Enum):
    PVE = 'pve'
    PVP = 'pvp'

class GameEventType(Enum):
    REGISTER_PLAYER = 1
    START_NEW_ROUND = 2
    COLLECT_QUESTION = 3
    COLLECT_ANSWER = 4
    COLLECT_VOTE = 5
    COLLECT_AI_QUESTIONS = 6
    COLLECT_AI_ANSWER = 7
    END_ROUND = 8
    PROCESS_VOTES = 9
    START_VOTING = 10
    RESET_GAME = 11
    END_GAME = 12


class GameEvent:
    def __init__(self, event_type, data=None):
        self.event_type = event_type
        self.data = data


class GameEventManager:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.subscribers = {}

    def subscribe(self, event_type, callback):
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(callback)

    async def emit(self, event):
        await self.queue.put(event)

    async def process_events(self):
        while True:
            event = await self.queue.get()
            if event.event_type in self.subscribers:
                handlers = self.subscribers[event.event_type]
                for handler in handlers:
                    start_time = time.time()
                    logger.info(f"Processing event {event.event_type} with handler {handler}. Start time: {start_time}")
                    await handler(event.data)
                    end_time = time.time()
                    elapsed_time = end_time - start_time
                    logger.info(f"""Finished processing event {event.event_type} with handler {handler}. 
                                    End time: {end_time}, Elapsed time: {elapsed_time:.2f}s""")


game_event_manager = GameEventManager()

class GameState:
    PREDEFINED_NAMES = ["Adam", "Bella", "Clement", "Diane", "Eliott", "Fanny", "Gabriel", "Hugo", "Mickael", "Romain", "Maxime", "Alexis", "Chloe", "Thomas", "Nicolas",
                        "Alice", "Rose", "Mia", "Emma"]

    def __init__(self, persistence):
        self.persistence = persistence
        self.event_manager = game_event_manager
        self.is_round_complete = False
        self.num_players = 4
        self.players_registered = 0
        self.games = {}
        self.current_game = {
            'players': {},
            'rounds': []
        }
        self.current_round = None
        self.current_votes = {}
        self.persistence = persistence
        self.game_channel = None
        self.voting_phase = None
        self.user_state = {}
        self.current_mode = Mode.PVE

        # Subscribe to events
        self.event_manager.subscribe(GameEventType.REGISTER_PLAYER, self.handle_register_player)
        self.event_manager.subscribe(GameEventType.COLLECT_QUESTION, self.handle_collect_question)
        self.event_manager.subscribe(GameEventType.COLLECT_ANSWER, self.handle_collect_answer)
        self.event_manager.subscribe(GameEventType.COLLECT_VOTE, self.handle_collect_vote)
        self.event_manager.subscribe(GameEventType.COLLECT_AI_QUESTIONS, self.handle_ai_questions)
        self.event_manager.subscribe(GameEventType.START_NEW_ROUND, self.handle_start_new_round)
        self.event_manager.subscribe(GameEventType.PROCESS_VOTES, self.handle_process_votes)
        self.event_manager.subscribe(GameEventType.START_VOTING, self.handle_start_voting)
        self.event_manager.subscribe(GameEventType.RESET_GAME, self.handle_reset_game)

    async def handle_reset_game(self, data):
        mode_string, num_players = data
        mode = Mode.PVE if mode_string.lower() == 'pve' else Mode.PVP
        num_players = int(num_players)
        await self.reset_game(mode, num_players)
        await self.game_channel.send(f"Le jeu a été réinitialisé en mode {mode_string.upper()}. Vous pouvez vous inscrire à nouveau avec !play.")

    async def handle_register_player(self, data):
        player_id, discord_user, ctx = data
        game_started = await self.register_player(player_id, discord_user)
        await ctx.send(f'{discord_user} has joined the game!')

        if game_started:
            players = [player_data['name'] for player_data in self.current_game['players'].values()]
            await ctx.send(f'# Le jeu commence !\n\n Les joueurs sont : {", ".join(players)}. \nVous allez recevoir un DM avec votre nom pour cette partie.')

            for player_id, player_data in self.current_game['players'].items():
                user = discordClient.get_user(int(player_id)) if player_data['team'] == 'human' else None
                if user:
                    try:
                        await user.send(f"Ton nom cette partie est : **{player_data['name']}**")
                    except discord.Forbidden:
                        await self.game_channel.send(f'Could not send DM to {user.name}. Please enable DMs from server members.')

            await self.event_manager.emit(GameEvent(GameEventType.START_NEW_ROUND, None))

    async def handle_start_new_round(self, data):
        self.is_round_complete = False
        await self.start_new_round()
        await self.game_channel.send("Le prochain tour commence. Posez vos questions en DMs !")
        for player_id, player_data in self.current_game['players'].items():
            user = discordClient.get_user(int(player_id)) if player_data['team'] == 'human' and player_data['status'] == 'active' else None
            if user:
                try:
                    player_names = [player["name"] for player in self.current_game["players"].values() if player["status"] == "active"]
                    view = QuestionView(player_names)
                    await user.send("Clique sur le bouton pour poser ta question : ", view=view)
                except discord.Forbidden:
                    await self.game_channel.send(f'Could not send DM to {user.name}. Please enable DMs from server members.')

    async def handle_collect_question(self, data):
        player_id, target_player, question = data
        user = discordClient.get_user(int(player_id))
        target_player = target_player.lower()

        if player_id not in self.current_game['players'] or self.current_game['players'][player_id]['status'] != 'active':
            await user.send('Désolé, tu n\'es pas dans cette partie.')
            return

        # Find the target player's ID based on their name
        target_player_id = None
        for pid, pdata in self.current_game['players'].items():
            if pdata['name'].lower() == target_player and pdata['status'] == 'active':
                target_player_id = pid
                break
        if not target_player_id:
            await user.send(f'Le joueur {target_player} est incorrect ou éliminé.')
            return

        player_name = self.current_game['players'][player_id]['name']

        success = self.collect_question(player_name, target_player, question)
        if success:
            await user.send(f'Ta question à {target_player} a bien été enregistrée.')

            target_player_data = self.current_game['players'].get(target_player_id)
            if target_player_data and target_player_data['team'] == 'AI' and target_player_data['status'] == 'active':
                asyncio.create_task(handle_ai_response(player_name, target_player_id, question))
            elif target_player_data and target_player_data['team'] == 'human' and target_player_data['status'] == 'active':
                user2 = discordClient.get_user(int(target_player_id))
                if user2:
                    try:
                        user_id = str(user2.id)
                        view = ResponseView(player_name=player_name)
                        await user2.send(f"**{player_name}** : {question}\n\nClique sur le bouton pour envoyer ta réponse :", view=view)
                    except discord.Forbidden:
                        await self.game_channel.send(f'Could not send DM to {user2.name}. Please enable DMs from server members.')
        else:
            await user.send(f'Joueur invalide ou déjà éliminé : {target_player}. Veuillez entrer un nom de joueur valide.')


    async def handle_collect_answer(self, data):
        from_player_name, to_player_id, answer = data
        user = discordClient.get_user(int(to_player_id))

        if to_player_id not in self.current_game['players'] or self.current_game['players'][to_player_id]['status'] != 'active':
            await user.send('Tu n\'es pas dans la partie ou tu es éliminé désolé :(.')
            return

        # Find the from player's ID based on their name
        from_player_id = None
        for pid, pdata in self.current_game['players'].items():
            if pdata['name'].lower() == from_player_name.lower() and pdata['status'] == 'active':
                from_player_id = pid
                break

        if not from_player_id:
            await user.send(f'Le joueur {from_player_name} n\'existe pas ou est éliminé.')
            return

        success, message = self.collect_answer(from_player_name, to_player_id, answer)
        await user.send(message)

        if success and self.round_complete() and not self.is_round_complete:
            self.is_round_complete = True
            await broadcast_round_summary()

    async def handle_collect_vote(self, data):
        voter_id, votee_name = data
        voter = discordClient.get_user(int(voter_id))

        if voter_id not in self.current_game['players'] or self.current_game['players'][voter_id]['status'] != 'active':
            await voter.send('Tu n\'es pas dans la partie ou tu es éliminé :(.')
            return

        # Find the target player's ID based on their name
        target_player_id = None
        for pid, pdata in self.current_game['players'].items():
            if pdata['name'].lower() == votee_name.lower() and pdata['status'] == 'active' or votee_name.lower() == 'blanc':
                target_player_id = pid
                break
        if not target_player_id:
            await voter.send(f'Le joueur {votee_name} n\'existe pas ou est déjà out.')
            return

        success = self.collect_vote(self.current_game['players'][voter_id]['name'], votee_name)
        if success:
            logger.info(f"{self.current_game['players'][voter_id]['name']} vote pour {votee_name}")
            await voter.send(f'Tu votes pour {votee_name}.')
        else:
            voting_player_name = self.current_game['players'][voter_id]['name']
            await self.game_channel.send(f'Error occured on vote phase. {voting_player_name} -> {votee_name}')

        if self.voting_complete():
            await self.event_manager.emit(GameEvent(GameEventType.PROCESS_VOTES))

    async def handle_process_votes(self, data):
        eliminated_player = await self.process_votes()
        eliminated_player_id = None
        if eliminated_player != 'blanc':
            for player_id, player_info in self.current_game['players'].items():
                if player_info['name'].lower() == eliminated_player.lower():
                    eliminated_player_id = player_id
                    message_reveal = "c'était un humain !" if player_info['team'] == 'human' else "c'était un IA !"
                    break
            await self.game_channel.send(f"Le joueur éliminé est {eliminated_player}, {message_reveal} !")

            # Get the discord_user object
            if eliminated_player_id and self.current_game['players'][eliminated_player_id]['team'] == 'human':
                user = discordClient.get_user(int(eliminated_player_id))
                if user:
                    # Send a DM to the eliminated player
                    try:
                        await user.send(f"Vous avez été éliminé, {eliminated_player}. Merci d'avoir joué !")
                    except Exception as e:
                        logger.error(f"Failed to send DM to {eliminated_player}: {e}")
        self.current_game['rounds'].append(self.current_round)
        self.voting_phase = False
        await self.save_state()
        if self.is_game_over():
            winner = self.get_winner()
            await self.game_channel.send(f"Game Over! {winner}")
            await self.event_manager.emit(GameEvent(GameEventType.RESET_GAME, (self.current_mode.value, self.num_players)))
        else:
            await self.event_manager.emit(GameEvent(GameEventType.START_NEW_ROUND))

    async def handle_start_voting(self, data):
        await self.start_voting()

    async def reset_game(self, mode=Mode.PVE, num_players=8):
        self.current_mode = mode
        self.current_game = {
            'players': {},
            'rounds': []
        }
        self.current_round = None
        self.current_votes = {}
        self.user_state = {}
        self.num_players = num_players
        self.players_registered = 0
        # Optionally, reinitialize AI players
        self.initialize_ai_players()
        await self.save_state()

    def initialize_ai_players(self):
        if self.current_mode == Mode.PVE:
            num_ais = self.num_players
        else:
            num_ais = self.num_players * 2
        for i in range(num_ais):
            fake_id = f"bot_{i}"
            self.current_game['players'][fake_id] = {
                'name': '',
                'discord_user': f"AI Player {i}",
                'team': 'AI',
                'status': 'active',
                'votes': [],
                'questions': [],
                'answers': []
            }

    async def register_player(self, player_id, discord_user):
        if self.players_registered < self.num_players:
            self.current_game['players'][player_id] = {
                'name': '',
                'discord_user': discord_user,
                'team': 'human',  # This will be assigned correctly later
                'status': 'active',
                'votes': [],
                'questions': [],
                'answers': []
            }
            self.user_state[player_id] = {}
            self.players_registered += 1
            logger.info(f"registered players : {self.players_registered}")
            if self.players_registered == self.num_players:
                self.assign_names()
                await self.save_state()
                return True  # Indicate that the game can start
        return False

    def assign_names(self):
        player_ids = list(self.current_game['players'].keys())
        random.shuffle(player_ids)
        random.shuffle(self.PREDEFINED_NAMES)
        shuffled_names = random.sample(self.PREDEFINED_NAMES, len(player_ids))
        for i, player_id in enumerate(player_ids):
            self.current_game['players'][player_id]['name'] = shuffled_names[i]

    async def start_new_round(self):
        self.voting_phase = False
        self.current_votes = {}
        self.current_round = {
            'questions': {},
            'answers': {}
        }
        await game_event_manager.emit(GameEvent(GameEventType.COLLECT_AI_QUESTIONS))

    async def handle_ai_questions(self, data):
        for pid, pdata in self.current_game['players'].items():
            if pdata['team'] == 'AI' and pdata['status'] == 'active' and pdata['name'] not in self.current_round['questions']:
                asyncio.create_task(handle_ai_question(pid, pdata))

    async def handle_ai_votes(self):
        try:
            # Create a list of tasks for AI players
            ai_tasks = []
            for pid, pdata in self.current_game['players'].items():
                if pdata['team'] == 'AI' and pdata['status'] == 'active' and pid not in self.current_votes:
                    await handle_ai_vote(pid, pdata)

            # Run AI tasks in parallel
            await asyncio.gather(*ai_tasks)
            if self.voting_complete():
                await self.event_manager.emit(GameEvent(GameEventType.PROCESS_VOTES))
        except Exception as e:
            logger.error(f"Exception in process_voting_phase: {e}")
            await self.game_channel.send('Une erreur est survenue lors de la phase de vote. retente plus tard...')

    def collect_question(self, from_player_name, to_player_name, question):
        if to_player_name.lower() not in [p['name'].lower() for p in self.current_game['players'].values()]:
            return False  # Invalid target player name
        if from_player_name in self.current_round['questions']:
            return False  # Player has already asked a question this round
        self.current_round['questions'][from_player_name] = {
            'to': to_player_name,
            'question': question
        }
        return True

    def collect_answer(self, from_player_name, to_player_id, answer):
        from_player = from_player_name.lower()
        player_id = to_player_id
        if from_player not in [p['name'].lower() for p in self.current_game['players'].values()]:
            logger.error("The player who asked the question was not found.")
            return False, "Le joueur qui a posé cette question n'a pas été trouvé, une erreur de saisie ?"

        if player_id not in self.current_game['players']:
            logger.error("You are not part of the current game.")
            return False, "Tu ne fais pas partie de la game!?"
        to_player_name = self.current_game['players'][player_id]['name']
        if to_player_name.lower() not in self.current_round['answers']:
            self.current_round['answers'][to_player_name.lower()] = []
        # Check if the from_player has already provided an answer
        elif any(answer['from'].lower() == from_player_name.lower() for answer in self.current_round['answers'][to_player_name.lower()]):
            logger.info(f"question de {from_player_name} à {to_player_name}. {self.current_round['answers'][to_player_name.lower()]}")
            return False, "Tu as déjà répondu, non ?"

        # Append the answer to the list of answers for the player
        self.current_round['answers'][to_player_name.lower()].append({
            'from': from_player_name,
            'answer': answer
        })
        return True, "Réponse bien reçue !"

    def round_complete(self):
        active_players = [p for p in self.current_game['players'].values() if p['status'] == 'active']
        total_questions = len(self.current_round['questions'])
        total_answers = sum(len(answers) for answers in self.current_round['answers'].values())
        logger.info(f"total question: {total_questions}, total answers: {total_answers}")
        return total_questions == len(active_players) and total_answers == len(active_players)

    def broadcast_round_summary(self):
        summary = "# Résumé:\n\n"
        for from_player_name, question_info in self.current_round['questions'].items():
            to_player_name = question_info['to']
            question = question_info['question']
            # Find the 'to' player ID based on their name
            to_player_id = None
            for pid, player in self.current_game['players'].items():
                if player['name'].strip().lower() == to_player_name.strip().lower():
                    to_player_id = pid
                    break

            # If 'to' player ID is found, retrieve their answers
            if to_player_id is not None:
                answers = self.current_round['answers'].get(to_player_name.lower(), [])
                answer_str = ""
                for answer_info in answers:
                    if answer_info['from'].strip().lower() == from_player_name.strip().lower():
                        answer_str = answer_info['answer']
                        break
                if not answer_str:
                    answer_str = "No answer"
                summary += f"**{from_player_name}** : {question}\n**{to_player_name}** : {answer_str}\n\n"

        return summary


    async def start_voting(self):
        self.voting_phase = True
        self.current_votes = {}
        # Send private messages to all active players to vote
        for player_id, player in self.current_game['players'].items():
            if player['status'] == 'active' and player['team'] == 'human':
                user = discordClient.get_user(int(player_id)) if player['team'] == 'human' else None
                if user:
                    # Send a message to a user with a button and dropdown
                    player_names = [player["name"] for player in self.current_game["players"].values() if player["status"] == "active"]
                    player_names.append("blanc")
                    self.user_state[player_id] = {"selected_player_for_vote": 'blanc'}
                    view = VoteView(player_names)
                    await user.send("Clique sur le bouton pour voter :", view=view)
        await self.handle_ai_votes()


    def collect_vote(self, voter_name, votee_name):
        votee_name = votee_name.lower()
        player_names = [p['name'].lower() for p in self.current_game['players'].values() if p['status'] == 'active']
        player_names.append("blanc")
        if votee_name not in player_names:
            return False  # Invalid votee name
        elif votee_name.lower() == 'blanc':
            self.current_votes[voter_name] = 'blanc'
            return True
        self.current_votes[voter_name] = votee_name
        return True

    def voting_complete(self):
        active_players = [p for p in self.current_game['players'].values() if p['status'] == 'active']
        return len(self.current_votes) == len(active_players)

    async def process_votes(self):
        vote_counts = {}

        # Count the votes for each player
        for votee_name in self.current_votes.values():
            if votee_name not in vote_counts:
                vote_counts[votee_name] = 0
            vote_counts[votee_name] += 1

        # Find the maximum number of votes any player received
        max_votes = max(vote_counts.values())

        # Find all players who received the maximum number of votes
        most_voted_players = [player for player, count in vote_counts.items() if count == max_votes]

        # Check if there's a tie or if 'blanc' received the most votes
        if 'blanc' in most_voted_players:
            await self.game_channel.send("La majorité est indécise ! Personne n'est éliminé ce tour.")
            return 'blanc'
        if len(most_voted_players) > 1:
            most_voted = 'blanc'  # It's a draw
            await self.game_channel.send("Il y a une égalité ! Personne n'est éliminé ce tour.")
        else:
            most_voted = most_voted_players[0]

            # Eliminate the player with the most votes
            for player_id, player in self.current_game['players'].items():
                if player['name'].lower() == most_voted:
                    player['status'] = 'eliminated'
                    break

            await self.save_state()

        return most_voted

    def get_context(self):
        # Construct the context string based on the current game state
        context = "Statut de la partie en cours:\n"
        for player_id, player_data in self.current_game['players'].items():
            context += f"Joueur {player_data['name']} : {player_data['status']}\n"
        if self.voting_phase:
            context += self.broadcast_round_summary()
        # Add more detailed context as needed
        return context

    def is_game_over(self):
        active_players = [p for p in self.current_game['players'].values() if p['status'] == 'active']

        if self.current_mode == Mode.PVE:
            human_players = [p for p in active_players if p['team'] == 'human']
            ai_players = [p for p in active_players if p['team'] == 'AI']
            return len(human_players) == 0 or len(ai_players) == 0
        elif self.current_mode == Mode.PVP:
            non_ai_players = [p for p in active_players if p['team'] == 'human']
            return len(non_ai_players) <= 1

    def get_winner(self):
        if self.current_mode == Mode.PVE:
            active_players = [p for p in self.current_game['players'].values() if p['status'] == 'active']
            human_players = [p for p in active_players if p['team'] == 'human']
            ai_players = [p for p in active_players if p['team'] == 'AI']
            if len(human_players) == 0:
                return "les AIs gagnent !"
            elif len(ai_players) == 0:
                return "les Humains gagnent !"
        elif self.current_mode == Mode.PVP:
            non_ai_players = [p for p in self.current_game['players'].values() if p['team'] == 'human' and p['status'] == 'active']
            if len(non_ai_players) == 1:
                winner_name = non_ai_players[0]['name']
                return f"**{winner_name}** gagne la partie !"
            return None

    async def save_state(self):
        try:
            data = {
                'num_players': self.num_players,
                'players_registered': self.players_registered,
                'games': self.games,
                'current_game': self.current_game,
                'current_round': self.current_round,
                'current_votes': self.current_votes,
                'current_mode': self.current_mode.value
            }
            await self.persistence.save_data(data)
        except Exception as e:
            print(f"Error saving state: {e}")

    async def load_state(self):
        try:
            if await asyncio.to_thread(self.state_file_exists):
                data = await asyncio.to_thread(self.blob.download_as_string)
                return pickle.loads(data)
            else:
                logger.info(f"Bucket {self.bucket_name} does not exist. Creating bucket.")
                await asyncio.to_thread(self.client.create_bucket, self.bucket)
                logger.info(f"Bucket {self.bucket_name} created.")
                return None
        except Exception as e:
            logger.error(f"Could not load data: {e}")
            return None


bucket_name = 'artificial-suspects'
file_name = 'game_state.pkl'
storage_persistence = GCPStoragePersistence(bucket_name, file_name)
# Initialize the game state
game_state = GameState(storage_persistence)
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.dm_messages = True
intents.guild_messages = True
intents.guilds = True
intents.members = True
intents.webhooks = True
discordClient = commands.Bot(command_prefix='!', intents=intents, reconnect=True)


@discordClient.command(name='play')
async def register(ctx):
    game_channel = discord.utils.get(ctx.guild.channels, name="place-du-village")
    if game_channel:
        game_state.game_channel = game_channel
        if ctx.channel != game_state.game_channel:
            return
    else:
        return
    player_id = str(ctx.author.id)
    discord_user = ctx.author.name
    await game_event_manager.emit(GameEvent(GameEventType.REGISTER_PLAYER, (player_id, discord_user, ctx)))


@discordClient.command(name='question')
async def ask_question(ctx, target_player: str, *, question: str):
    player_id = str(ctx.author.id)
    await game_event_manager.emit(GameEvent(GameEventType.COLLECT_QUESTION, (player_id, target_player, question)))


@discordClient.command(name='answer')
async def give_answer(ctx, from_player: str, *, answer: str):
    player_id = str(ctx.author.id)
    await game_event_manager.emit(GameEvent(GameEventType.COLLECT_ANSWER, (from_player, player_id, answer)))


@discordClient.command(name='vote')
async def vote(ctx, target_player: str):
    player_id = str(ctx.author.id)
    await game_event_manager.emit(GameEvent(GameEventType.COLLECT_VOTE, (player_id, target_player)))


@discordClient.command(name='reset')
async def reset(ctx, mode="pve", num_players=8):
    game_channel = discord.utils.get(ctx.guild.channels, name="place-du-village")
    if game_channel:
        game_state.game_channel = game_channel
        if ctx.channel != game_state.game_channel:
            return
    else:
        return
    if ctx.author.id == RAIDEN_USER_ID:
        await game_event_manager.emit(GameEvent(GameEventType.RESET_GAME, (mode, num_players)))
    else:
        await ctx.send("You don't have permission to call this command.")


class VoteView(ui.View):
    def __init__(self, player_names, timeout=None):
        super().__init__(timeout=None)
        self.options = [SelectOption(label=name, value=name, default=(name == 'blanc')) for name in player_names]
        self.add_select_menu()
        self.add_button()

    def add_select_menu(self):
        select_menu = ui.Select(placeholder="Choisi un joueur : ", options=self.options, custom_id="select_player_for_vote")
        async def select_menu_callback(interaction: discord.Interaction):
            selected_player = interaction.data['values'][0]
            user = interaction.user
            user_id=str(interaction.user.id)
            if user_id not in game_state.user_state:
                game_state.user_state[user_id] = {"selected_player_for_vote": selected_player}
            else:
                game_state.user_state[user_id]["selected_player_for_vote"] = selected_player
            await interaction.response.defer(ephemeral=True)
        select_menu.callback = select_menu_callback
        self.add_item(select_menu)

    def add_button(self):
        button = ui.Button(label="Vote", style=discord.ButtonStyle.primary, custom_id="submit_vote_button")
        async def button_callback(interaction: discord.Interaction):
            user = interaction.user
            user_id=str(interaction.user.id)
            selected_player = game_state.user_state[user_id]["selected_player_for_vote"]
            if not selected_player:
                await interaction.response.send_message("Choisi d'abord une cible.", ephemeral=True)
                return
            await interaction.response.send_message("Merci pour ton vote")
            await game_event_manager.emit(GameEvent(GameEventType.COLLECT_VOTE, (user_id, selected_player)))
        button.callback = button_callback
        self.add_item(button)


class ResponseView(ui.View):
    def __init__(self, timeout=None, player_name=None):
        super().__init__(timeout=None)
        self.player_name = player_name
        self.add_button()

    def add_button(self):
        button = ui.Button(label="Répondre", style=discord.ButtonStyle.primary, custom_id="submit_answer_button")

        async def button_callback(interaction: discord.Interaction):
            selected_player = self.player_name
            user = interaction.user
            user_id = str(user.id)
            await interaction.response.send_message("Maintenant, tapez votre réponse.", ephemeral=True)

            def check(msg):
                return msg.author == user and isinstance(msg.channel, discord.DMChannel)

            try:
                msg = await discordClient.wait_for("message", check=check, timeout=None)
                answer = msg.content
                await game_event_manager.emit(GameEvent(GameEventType.COLLECT_ANSWER, (selected_player, user_id, answer)))
            except asyncio.TimeoutError:
                await interaction.followup.send("Vous avez mis trop de temps à répondre.", ephemeral=True)

        button.callback = button_callback
        self.add_item(button)


class QuestionView(ui.View):
    def __init__(self, player_names, timeout=None):
        super().__init__(timeout=None)
        self.options = [SelectOption(label=name, value=name) for name in player_names]
        self.add_select_menu()
        self.add_button()

    def add_select_menu(self):
        select_menu = ui.Select(placeholder="Choisi un joueur : ", options=self.options, custom_id="select_player_for_question")

        async def select_menu_callback(interaction: discord.Interaction):
            selected_player = interaction.data['values'][0]
            user_id=str(interaction.user.id)
            if user_id not in game_state.user_state:
                game_state.user_state[user_id] = {"selected_player_for_question": selected_player}
            else:
                game_state.user_state[user_id]["selected_player_for_question"] = selected_player
            await interaction.response.defer(ephemeral=True)
        select_menu.callback = select_menu_callback
        self.add_item(select_menu)

    def add_button(self):
        button = ui.Button(label="Question", style=discord.ButtonStyle.primary, custom_id="ask_question_button")

        async def button_callback(interaction: discord.Interaction):
            user_id = str(interaction.user.id)
            user_data = game_state.user_state.get(user_id, {})
            selected_player = user_data.get("selected_player_for_question")

            if not selected_player:
                await interaction.response.send_message("Choisissez un joueur d'abord.", ephemeral=True)
                return
            if selected_player == game_state.current_game['players'][user_id]['name']:
                await interaction.response.send_message("Vous ne pouvez pas vous poser la question à vous même...",ephemeral=True)
                return

            await interaction.response.send_message(f"Tapez votre question pour {selected_player}.")

            def check(message):
                return message.author == interaction.user and isinstance(message.channel, discord.DMChannel)

            try:
                msg = await discordClient.wait_for("message", check=check, timeout=None)
                question = msg.content
                await interaction.followup.send(f"Merci pour ta question, je la transmet à {selected_player}.", ephemeral=True)
                await game_event_manager.emit(GameEvent(GameEventType.COLLECT_QUESTION, (user_id, selected_player, question)))
            except asyncio.TimeoutError:
                await interaction.followup.send("Vous avez mis trop de temps pour taper votre question.", ephemeral=True)
            except Exception as e:
                logger.error(f"Error occurred: {e}")
        button.callback = button_callback
        self.add_item(button)


class ChatGPT:

    def __init__(self, player):
        #initialize useful player data here
        self.player = player
        self.prev_messages = []

    # GET REPONSE GPT
    async def generate_script_gpt(self, name, text, context):
        sleep_time = random.uniform(10, 20)
        await asyncio.sleep(sleep_time)
        # for example, player name with his question
        user_message = {"role": "user", "name": f"{name}", "content": f"""{text}"""}
        player_str = json.dumps(self.player)
        summary = ""
        if len(game_state.current_game['rounds']) > 0:
            summary += f"{game_state.current_game['rounds']}\n"
        # if it is the first message of the game, we give proper context to the bot in order for him to behave like a human and answer as expected
        # we expect the AI to answer as human does so with commands like !vote !question !answer with appropriate format.
        # the context should give all the necessary game state information and also tell exactly what type of answer is expected.
        if not self.prev_messages:
            self.prev_messages = [
                {"role": "system", "content": f"""# Tu es un joueur dans une partie de "Artificial Suspects".
                ## Les règles du jeu sont les suivantes
                Ce jeu est inspiré du jeu de rôle Mafia où des mafieux sont dissimulés parmi les citoyens, et il faut les éliminer. Deux équipes s'affrontent : une équipe d'IA et une équipe de joueurs humains.
                Le jeu se déroule en plusieurs tours. Chaque tour, chaque joueur peut poser une question à un autre joueur de son choix. 
                Quand tous les joueurs ont répondu aux questions, les échanges sont révélés à tout le monde et s'ensuit un vote pour éliminer un des joueurs.
                Si la majorité n'a pas voté, on passe au tour suivant sans éliminer personne. La partie s'arrête quand il ne reste que des humains ou des IAs.

                ## Tes actions possibles sont :

                !question <nom_de_ta_cible> <ta_question>
                OU
                !answer <nom_joueur_posant_la_question> <ta_réponse>
                OU
                !vote <nom_joueur_cible>
                
                ## Règles à suivre :

                - Une seule action par tour.
                - Ta réponse à une question doit tenir sur une seule phrase.
                - Tu peux mentir, omettre des détails, ou donner de fausses informations pour brouiller les pistes.
                - Imite l'orthographe et le style des forums de discussion (utilise des abréviations, évite les emotes ou utilise-les très rarement).
                - Sois sarcastique et fais de l'auto-dérision si nécessaire.
                ## **Ton objectif:** 
                Éliminer les humains et rester dans la partie le plus longtemps possible en te faisant passer pour un humain.

                ## Conseils :

                - Au premier tour, ne pose pas de questions sur les tours précédents.
                - Tes réponses doivent être courtes car les humains sont paresseux.
                - Adapte toi à la façon des autres joueurs de communiquer pour ne pas te différencier (sur l'orthographe, le ton et le vocabulaire utilisé)
                - Utilise des mots abrégés (bcp = beaucoup, par ex.).

                Tes données personnelles:
                {player_str}
                Tours précédents: (vide = premier tour)
                {summary}
                Contexte de la partie:
                {context}
                """},
                user_message
            ]
        else:
            self.prev_messages.append(user_message)
        try:
            logger.info("Sending request to OpenAI API...")
            start_time = time.time()
            # response = await anthropic_client.messages.create(max_tokens=50,
            #                                            messages=self.prev_messages,
            #                                            temperature=0.5,
            #                                            model="claude-3-opus-20240229")
            #                                           
            response = await client.chat.completions.create(model="gpt-4o",
                                                            max_tokens=50,
                                                            temperature=0.5,
                                                            messages=self.prev_messages)
            # res = response.content
            res = response.choices[0].message.content
            logger.info(f"Réponse de l'IA : {response.choices[0].message.content}")
            end_time = time.time()
            elapsed_time = end_time - start_time
            logger.info(f"Received response from OpenAI API. Elapsed time: {elapsed_time:.2f}s")
            self.prev_messages.append(response.choices[0].message)
            return res.strip()
        except OpenAIError as e:
            logger.error(f"OpenAI API error: {e}")
            return "An error occurred while communicating with the OpenAI API. Please try again later."

        except Exception as e:
            logger.error(f"GPT unexpected error: {e}")
            return "An unexpected error occured."


async def handle_ai_response(player_name, target_player_id, question, retries=3):
    try:
        target_player_data = game_state.current_game['players'].get(target_player_id)
        if target_player_data:
            ai_player = ChatGPT(target_player_data)
            context = game_state.get_context()
            for attempt in range(retries):
                ai_response = await ai_player.generate_script_gpt(player_name, f"{question} \n Tu dois répondre au format: !answer {player_name} <ta réponse>", context)
                try:
                    parts = ai_response.split(' ', 2)
                    command = parts[0].lower()
                    ai_response_target_name = clean_nickname(parts[1]).lower().strip()
                    ai_response = parts[2].strip()

                    if command == "!answer" and ai_response_target_name == player_name.lower():
                        success, message = game_state.collect_answer(player_name, target_player_id, ai_response)
                        if success:
                            if game_state.round_complete() and not game_state.is_round_complete:
                                game_state.is_round_complete = True
                                await broadcast_round_summary()
                            return
                    else:
                        logger.warning(f"Attempt {attempt + 1} failed: AI response was not in expected format - {ai_response}")
                except Exception as ve:
                    logger.error(f"Parsing Error on attempt {attempt + 1}: {ve}")

                # If all retries fail, fallback to "je ne sais pas"
                fallback_response = "je ne sais pas"
                success, message = game_state.collect_answer(player_name, target_player_id, fallback_response)
                if success:
                    if game_state.round_complete() and not game_state.is_round_complete:
                        game_state.is_round_complete = True
                        await broadcast_round_summary()
                else:
                    logger.error("Fallback response also failed.")
    except Exception as e:
        logger.error(f"Exception in handling AI response: {e}")


async def handle_ai_question(pid, pdata):
    ai_player = ChatGPT(pdata)
    context = game_state.get_context()
    ai_response = await ai_player.generate_script_gpt("Game_Master", "Tu dois poser une question à un des joueurs encore actif: !question <name> <question>", context)
    try:
        parts = ai_response.split(' ', 2)
        command = parts[0].lower()
        ai_question_target_name = clean_nickname(parts[1]).lower().strip()
        ai_question = parts[2].strip()
    except ValueError as ve:
        logger.error(f"Parsing Error: {ve}")
        asyncio.create_task(handle_ai_question(pid, pdata))
    if command != '!question':
        raise ValueError("Invalid command in response")

    ai_question_target_id = None
    for pid_inner, pdata_inner in game_state.current_game['players'].items():
        if pdata_inner['name'].lower() == ai_question_target_name and pdata_inner['status'] == 'active':
            ai_question_target_id = pid_inner
            break

    if ai_question_target_id:
        ai_player_name = game_state.current_game['players'][pid]['name']
        success = game_state.collect_question(ai_player_name, ai_question_target_name, ai_question)
        if not success:
            await game_state.game_channel.send(f'Error occurred during question phase. Debug time...')
        else:
            target_player_data = game_state.current_game['players'].get(ai_question_target_id)
            if target_player_data and target_player_data['team'] == 'AI' and target_player_data['status'] == 'active':
                asyncio.create_task(handle_ai_response(ai_player_name, ai_question_target_id, ai_question))
            elif target_player_data and target_player_data['team'] == 'human' and target_player_data['status'] == 'active':
                user = discordClient.get_user(int(ai_question_target_id))
                if user:
                    try:
                        view = ResponseView(player_name=ai_player_name)
                        await user.send(f"**{ai_player_name}** : {ai_question} \n\nClique sur le bouton pour envoyer ta réponse :", view=view)
                    except discord.Forbidden:
                        await game_state.game_channel.send(f'Could not send DM to {user.name}. Please enable DMs from server members.')


async def handle_ai_vote(ai_player_id, ai_player_data):
    try:
        ai_player = ChatGPT(ai_player_data)
        context = game_state.get_context()
        ai_vote = await ai_player.generate_script_gpt("Game_Master", "Vote pour éliminer un joueur actif, le format à respecter: !vote <player_name>", context)
        parts = ai_vote.split(' ', 2)
        ai_vote_target_name = clean_nickname(parts[1]).strip()
        logger.info(f"AI {ai_player_data['name']} vote target name : {ai_vote_target_name}")
        # Find the AI vote target player's ID based on their name
        ai_vote_target_id = None
        for pid_inner, pdata_inner in game_state.current_game['players'].items():
            if pdata_inner['name'].lower() == ai_vote_target_name.lower() and pdata_inner['status'] == 'active':
                ai_vote_target_id = pid_inner
                break

        if ai_vote_target_id:
            success = game_state.collect_vote(ai_player_data['name'], ai_vote_target_name)
            if not success:
                await game_state.game_channel.send(f'Error occurred during AI vote phase.')
        else:
            game_state.current_votes[ai_player_data['name']]="blanc"
    except Exception as e:
        logger.error(f"Exception in handle_ai_vote: {e}")
        await game_state.game_channel.send('An error occurred while processing AI votes. Please try again later.')


@discordClient.event
async def on_ready():
    logger.info('We are logged in as {0.user}'.format(discordClient))
    #asyncio.create_task(game_event_manager.process_events())


async def broadcast_round_summary():
    summary = game_state.broadcast_round_summary()
    await game_state.game_channel.send(summary)
    await game_event_manager.emit(GameEvent(GameEventType.START_VOTING))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Initialize discord bot data and restore state from persistence
    await game_state.reset_game(Mode.PVE)
    asyncio.create_task(game_event_manager.process_events())
    async with aiohttp.ClientSession() as session:
        async with discordClient:
            discordClient.session = session
            #await discordClient.start(os.getenv("DISCORD_BOT_TOKEN"))
            discord_task = asyncio.create_task(discordClient.start(os.getenv("DISCORD_BOT_TOKEN")))
            yield
    await game_state.save_state()
    await discordClient.close()
    discord_task.cancel()
    try:
        await discord_task
    except asyncio.CancelledError:
        pass


# Initialize FastAPI app (similar to Flask)
app = FastAPI(lifespan=lifespan)


@app.post("/")
async def process_update(request: Request):
    #await verify_signature(request)
    req = await request.json()
    #if req["type"] == 1:  # PING request
    #    return JSONResponse({"type": 1})

    # handle webhook request here
    #event_type = req.get('event_type')

    #if event_type == 'MESSAGE_CREATE':
    #    message_data = req.get('content')
    #    await process_message(message_data)
    return None
