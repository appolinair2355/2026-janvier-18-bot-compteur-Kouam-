import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timedelta, timezone, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, SOURCE_CHANNEL_2_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY
)

# --- Configuration et Initialisation ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Vérifications minimales de la configuration
if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, SOURCE_CHANNEL_2={SOURCE_CHANNEL_2_ID}, PREDICTION_CHANNEL={PREDICTION_CHANNEL_ID}")

# Initialisation du client Telegram avec session string ou nouvelle session
session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# Variables Globales d'État
SUIT_CYCLE = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']

def get_rule1_suit(game_number: int) -> str | None:
    # Cette fonction est maintenant simplifiée car la logique de cycle est gérée dans process_prediction_logic
    if game_number < 6 or game_number > 1436 or game_number % 2 != 0 or game_number % 10 == 0:
        return None
    
    count_valid = 0
    for n in range(6, game_number + 1, 2):
        if n % 10 != 0:
            count_valid += 1
            
    if count_valid == 0: return None
    
    index = (count_valid - 1) % 8
    return SUIT_CYCLE[index]

scp_cooldown = 0
scp_history = []  # Historique des impositions SCP

pending_predictions = {}
queued_predictions = {}
processed_messages = set()
current_game_number = 0
last_source_game_number = 0
rule2_authorized_suit = None

stats_bilan = {
    'total': 0,
    'wins': 0,
    'losses': 0,
    'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
    'loss_details': {'❌': 0}
}
bilan_interval = 20
last_bilan_time = datetime.now()

source_channel_ok = False
prediction_channel_ok = False
transfer_enabled = True


# --- Fonctions d'Analyse ---

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message."""
    # Pattern plus flexible pour #N59 ou #N 59
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

def parse_stats_message(message: str):
    """Extrait les statistiques du canal source 2."""
    stats = {}
    # Pattern pour extraire : ♠️ : 9 (23.7 %)
    patterns = {
        '♠': r'♠️?\s*:\s*(\d+)',
        '♥': r'♥️?\s*:\s*(\d+)',
        '♦': r'♦️?\s*:\s*(\d+)',
        '♣': r'♣️?\s*:\s*(\d+)'
    }
    for suit, pattern in patterns.items():
        match = re.search(pattern, message)
        if match:
            stats[suit] = int(match.group(1))
    return stats

def extract_parentheses_groups(message: str):
    """Extrait le contenu entre parenthèses, y compris les emojis de cartes."""
    # Pattern pour capturer tout ce qui est entre parenthèses, y compris les caractères spéciaux et emojis
    # On cherche spécifiquement après un nombre (score)
    groups = re.findall(r"\d+\(([^)]*)\)", message)
    return groups

def normalize_suits(group_str: str) -> str:
    """Remplace les différentes variantes de symboles par un format unique (important pour la détection)."""
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def get_suits_in_group(group_str: str):
    """Liste toutes les couleurs (suits) présentes dans une chaîne."""
    normalized = normalize_suits(group_str)
    return [s for s in ALL_SUITS if s in normalized]

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si la couleur cible est présente dans le premier groupe du résultat."""
    normalized = normalize_suits(group_str)
    # Normalisation du symbole cible pour comparaison robuste
    target_normalized = normalize_suits(target_suit)
    
    logger.info(f"DEBUG Vérification: Groupe={normalized}, Cible={target_normalized}")
    
    # On vérifie si l'un des caractères de la cible est présent dans le groupe normalisé
    for char in target_normalized:
        if char in normalized:
            logger.info(f"DEBUG Vérification: MATCH TROUVÉ pour {char}")
            return True
    return False

def get_predicted_suit(missing_suit: str) -> str:
    """Applique le mapping personnalisé (couleur manquante -> couleur prédite)."""
    # Ce mapping est maintenant l'inverse : ♠️<->♣️ et ♥️<->♦️
    # Assurez-vous que SUIT_MAPPING dans config.py contient :
    # SUIT_MAPPING = {'♠': '♣', '♣': '♠', '♥': '♦', '♦': '♥'}
    return SUIT_MAPPING.get(missing_suit, missing_suit)
# --- Logique de Prédiction et File d'Attente ---

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None):
    """Envoie la prédiction au canal de prédiction et l'ajoute aux prédictions actives."""
    try:
        # Le bot lance une nouvelle prédiction dès que le canal source arrive sur le numéro prédit.
        # On vérifie s'il y a une prédiction principale active pour un numéro futur.
        active_auto_predictions = [p for game, p in pending_predictions.items() if p.get('rattrapage', 0) == 0 and game > current_game_number]
        
        if rattrapage == 0 and len(active_auto_predictions) >= 1:
            logger.info(f"Une prédiction automatique pour un numéro futur est déjà active. En attente pour #{target_game}")
            return None

        # Si c'est un rattrapage, on ne crée pas un nouveau message, on garde la trace
        if rattrapage > 0:
            pending_predictions[target_game] = {
                'message_id': 0, # Pas de message pour le rattrapage lui-même
                'suit': predicted_suit,
                'base_game': base_game,
                'status': '🔮',
                'rattrapage': rattrapage,
                'original_game': original_game,
                'created_at': datetime.now().isoformat()
            }
            logger.info(f"Rattrapage {rattrapage} actif pour #{target_game} (Original #{original_game})")
            return 0

        # Nouveau format de message plus joli demandé par l'utilisateur
        prediction_msg = f"🔵{target_game}  🌀 {SUIT_DISPLAY.get(predicted_suit, predicted_suit)} : ⌛"
        msg_id = 0

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée au canal de prédiction {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction au canal: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible, prédiction non envoyée")

        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': predicted_suit,
            'base_game': base_game,
            'status': '⌛',
            'check_count': 0,
            'rattrapage': 0,
            'created_at': datetime.now().isoformat()
        }

        logger.info(f"Prédiction active: Jeu #{target_game} - {predicted_suit}")
        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

def queue_prediction(target_game: int, predicted_suit: str, base_game: int, rattrapage=0, original_game=None):
    """Met une prédiction en file d'attente pour un envoi différé."""
    # Vérification d'unicité
    if target_game in queued_predictions or (target_game in pending_predictions and rattrapage == 0):
        return False

    queued_predictions[target_game] = {
        'target_game': target_game,
        'predicted_suit': predicted_suit,
        'base_game': base_game,
        'rattrapage': rattrapage,
        'original_game': original_game,
        'queued_at': datetime.now().isoformat()
    }
    logger.info(f"📋 Prédiction #{target_game} mise en file d'attente (Rattrapage {rattrapage})")
    return True

async def check_and_send_queued_predictions(current_game: int):
    """Vérifie la file d'attente et envoie les prédictions dès que possible."""
    global current_game_number
    current_game_number = current_game

    sorted_queued = sorted(queued_predictions.keys())

    for target_game in sorted_queued:
        # On envoie si le numéro cible est supérieur au numéro actuel
        if target_game >= current_game:
            pred_data = queued_predictions.get(target_game)
            if not pred_data:
                continue
                
            # Tentative d'envoi
            result = await send_prediction_to_channel(
                pred_data['target_game'],
                pred_data['predicted_suit'],
                pred_data['base_game'],
                pred_data.get('rattrapage', 0),
                pred_data.get('original_game')
            )
            
            # Si l'envoi a réussi (ou si c'était un rattrapage qui ne crée pas de msg)
            if result is not None:
                queued_predictions.pop(target_game)

async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le message de prédiction dans le canal et les statistiques."""
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']

        # Format plus joli pour le message mis à jour demandé par l'utilisateur
        updated_msg = f"🔵{game_number}  🌀 {SUIT_DISPLAY.get(suit, suit)} : {new_status}"

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")

        pred['status'] = new_status
        
        # Mise à jour des statistiques de bilan
        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '✅3️⃣']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][new_status if new_status != '✅3️⃣' else '✅2️⃣'] += 1
            # On ne supprime pas immédiatement si on a des prédictions en attente
            del pending_predictions[game_number]
            # Dès qu'une prédiction est terminée, on libère pour la suivante
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))
        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            stats_bilan['loss_details']['❌'] += 1
            del pending_predictions[game_number]
            # Dès qu'une prédiction est terminée, on libère pour la suivante
            asyncio.create_task(check_and_send_queued_predictions(current_game_number))

        return True
    except Exception as e:
        logger.error(f"Erreur update_status: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """Vérifie les résultats selon la séquence ✅0️⃣, ✅1️⃣, ✅2️⃣ ou ❌."""
    # Nettoyage et normalisation du groupe reçu
    first_group = normalize_suits(first_group)
    
    # On parcourt TOUTES les prédictions en attente pour voir si l'une d'elles doit être vérifiée maintenant
    for target_game, pred in list(pending_predictions.items()):
        # Cas 1 : Prédiction initiale (rattrapage 0) sur le numéro actuel
        if target_game == game_number and pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            if has_suit_in_group(first_group, target_suit):
                await update_prediction_status(game_number, '✅0️⃣')
                return
            else:
                # Échec N, on planifie le rattrapage 1 pour N+1
                next_target = game_number + 1
                queue_prediction(next_target, target_suit, pred['base_game'], rattrapage=1, original_game=game_number)
                logger.info(f"Échec # {game_number}, Rattrapage 1 planifié pour #{next_target}")
                return # ARRÊT sur cette prédiction pour ce tour
                
        # Cas 2 : Rattrapage (rattrapage 1 ou 2) sur le numéro actuel
        elif target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game')
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            
            if has_suit_in_group(first_group, target_suit):
                # Trouvé ! On met à jour le statut du message original
                if original_game is not None:
                    await update_prediction_status(original_game, f'✅{rattrapage_actuel}️⃣')
                # On supprime le rattrapage
                if target_game in pending_predictions:
                    del pending_predictions[target_game]
                return # ARRÊT sur cette prédiction
            else:
                # Échec du rattrapage actuel
                if rattrapage_actuel < 2: 
                    # On planifie le rattrapage suivant (+2)
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    queue_prediction(next_target, target_suit, pred['base_game'], rattrapage=next_rattrapage, original_game=original_game)
                    logger.info(f"Échec rattrapage {rattrapage_actuel} sur #{game_number}, Rattrapage {next_rattrapage} planifié pour #{next_target}")
                else:
                    # Échec final après +2
                    if original_game is not None:
                        await update_prediction_status(original_game, '❌')
                    logger.info(f"Échec final pour la prédiction originale #{original_game} après rattrapage +2")
                
                # Dans tous les cas d'échec de rattrapage, on supprime le rattrapage actuel
                if target_game in pending_predictions:
                    del pending_predictions[target_game]
                return # ARRÊT

async def process_stats_message(message_text: str):
    """Traite les statistiques du canal 2 pour l'imposition du Système Central."""
    global rule2_authorized_suit
    stats = parse_stats_message(message_text)
    if not stats:
        rule2_authorized_suit = None
        return

    # Miroirs : ♠️ <-> ♦️ | ❤️ <-> ♣️
    miroirs = [('♠', '♦'), ('♥', '♣')]
    
    selected_target_suit = None
    max_diff = 0
    
    for s1, s2 in miroirs:
        v1 = stats.get(s1, 0)
        v2 = stats.get(s2, 0)
        diff = abs(v1 - v2)
        
        if diff >= 6:
            if diff > max_diff:
                max_diff = diff
                # REGLE CORRIGEE : On prédit le plus FAIBLE parmi les miroirs
                selected_target_suit = s1 if v1 < v2 else s2
                
    if selected_target_suit:
        # Ici rule2_authorized_suit stockera directement le costume à prédire (le plus faible)
        rule2_authorized_suit = selected_target_suit
        logger.info(f"Système Central (Imposition) détecté : Écart de {max_diff} sur miroir. Cible faible : {selected_target_suit}")
    else:
        rule2_authorized_suit = None
        logger.info("Système Central (Imposition) : Aucun écart de 6 détecté sur les miroirs.")

async def send_bilan():
    """Envoie le bilan des prédictions."""
    if stats_bilan['total'] == 0:
        return

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100
    loss_rate = (stats_bilan['losses'] / stats_bilan['total']) * 100
    
    msg = (
        "📊 **BILAN DES PRÉDICTIONS**\n\n"
        f"✅ Taux de réussite : {win_rate:.1f}%\n"
        f"❌ Taux de perte : {loss_rate:.1f}%\n\n"
        "**Détails :**\n"
        f"✅0️⃣ : {stats_bilan['win_details']['✅0️⃣']}\n"
        f"✅1️⃣ : {stats_bilan['win_details']['✅1️⃣']}\n"
        f"✅2️⃣ : {stats_bilan['win_details']['✅2️⃣']}\n"
        f"❌ : {stats_bilan['loss_details']['❌']}\n"
        f"\nTotal prédictions : {stats_bilan['total']}"
    )
    
    if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
        try:
            await client.send_message(PREDICTION_CHANNEL_ID, msg)
            logger.info("✅ Bilan envoyé au canal.")
        except Exception as e:
            logger.error(f"❌ Erreur envoi bilan: {e}")

async def auto_bilan_task():
    """Tâche périodique pour envoyer le bilan."""
    global last_bilan_time
    logger.info(f"Démarrage de la tâche auto_bilan (Intervalle: {bilan_interval} minutes)")
    while True:
        try:
            await asyncio.sleep(60) # Vérifie chaque minute
            now = datetime.now()
            next_bilan_time = last_bilan_time + timedelta(minutes=bilan_interval)
            
            if now >= next_bilan_time:
                logger.info("Déclenchement automatique du bilan...")
                await send_bilan()
                last_bilan_time = now
        except Exception as e:
            logger.error(f"Erreur dans auto_bilan_task: {e}")
            await asyncio.sleep(10)

def is_message_finalized(message_text: str) -> bool:
    """Vérifie si le message contient le mot 'Finalisé', 🔰 ou ✅."""
    # Un message finalisé contient 🔰 ou ✅. 
    # S'il contient ⏰, il n'est pas encore finalisé, on doit attendre.
    return "Finalisé" in message_text or "🔰" in message_text or "✅" in message_text

async def process_prediction_logic(message_text: str, chat_id: int):
    """Lance la prédiction dès réception du message, sans attendre la finalisation."""
    global last_source_game_number, current_game_number, scp_cooldown
    if chat_id != SOURCE_CHANNEL_ID:
        return
        
    game_number = extract_game_number(message_text)
    if game_number is None:
        return
        
    logger.info(f"Analyse SCP pour le message reçu (Jeu #{game_number})")
    
    # Gestion du cycle : s'arrête à 1436, reprend à 6 quand le 4 apparaît
    next_game = None
    if game_number == 1436:
        logger.info("Jeu #1436 atteint. Fin du cycle. Attente du jeu #4 pour reprendre.")
        return
    elif game_number == 4:
        logger.info("Jeu #4 détecté. Reprise du cycle avec la prédiction du jeu #6.")
        next_game = 6
    else:
        # Logique standard pour trouver le prochain numéro PAIR valide
        candidate = game_number + 1
        while candidate % 2 != 0 or candidate % 10 == 0:
            candidate += 1
        next_game = candidate

        # On ne prédit plus si on dépasse 1436 dans ce cycle
        if next_game > 1436:
            logger.info(f"Prochain jeu théorique #{next_game} > 1436. Pas de prédiction.")
            return

        # Vérification de l'écart standard
        if next_game != game_number + 2:
            logger.info(f"SCP : Écart de {next_game - game_number} détecté. Attente du numéro intermédiaire.")
            return
    
    # 1. Calcul de la Règle 1
    # On utilise le cycle direct car la normalisation est gérée ici par l'attente du #4
    rule1_suit = None
    if next_game:
        count_valid = 0
        for n in range(6, next_game + 1, 2):
            if n % 10 != 0:
                count_valid += 1
        if count_valid > 0:
            index = (count_valid - 1) % 8
            rule1_suit = SUIT_CYCLE[index]
            # Forçage spécifique pour le jeu #6 si demandé
            if next_game == 6:
                rule1_suit = '♥'
    
    # 2. Imposition du Système Central (basé sur les stats du canal 2)
    scp_imposition_suit = None
    if rule2_authorized_suit:
        if scp_cooldown <= 0:
            # Le Système Central a déjà identifié le costume le plus FAIBLE
            scp_imposition_suit = rule2_authorized_suit
            logger.info(f"SCP : Système Central s'impose sur #{next_game}. Cible faible détectée: {scp_imposition_suit}")
        else:
            logger.info(f"SCP : Imposition en pause (Cooldown: {scp_cooldown})")

    # Logique de décision
    final_suit = None
    if scp_imposition_suit:
        # Le Système Central s'impose s'il y a un écart de 6 entre miroirs
        # On vérifie si on a déjà fait une prédiction règle 1 depuis la dernière imposition
        if scp_cooldown <= 0:
            final_suit = scp_imposition_suit
            logger.info(f"SCP : Système Central s'impose pour #{next_game} -> {final_suit}")
            
            # Enregistrement dans l'historique
            scp_history.append({
                'game': next_game,
                'suit': final_suit,
                'time': datetime.now().strftime('%H:%M:%S'),
                'reason': "Écart détecté"
            })
            if len(scp_history) > 10: scp_history.pop(0)

            # On active le cooldown : le Système Central doit attendre que la Règle 1 soit utilisée
            scp_cooldown = 1
            
            # Comparaison avec la règle 1 pour la notification
            if final_suit == rule1_suit:
                logger.info(f"SCP : L'imposition confirme la Règle 1 ({final_suit}). Pas de notification admin.")
            elif ADMIN_ID != 0 and final_suit:
                try:
                    await client.send_message(ADMIN_ID, f"⚠️ **Imposition SCP**\nLe Système Central impose le costume {SUIT_DISPLAY.get(final_suit, final_suit)} pour le jeu #{next_game} (Règle 1 {SUIT_DISPLAY.get(rule1_suit, rule1_suit) if rule1_suit else 'None'} ignorée).")
                except Exception as e:
                    logger.error(f"Erreur notification imposition: {e}")
        else:
            logger.info(f"SCP : Système Central a déjà imposé récemment. Attente d'une prédiction Règle 1.")
    
    # Règle 1 seulement si le Système Central ne s'est PAS imposé pour cette prédiction
    if not final_suit and rule1_suit:
        final_suit = rule1_suit
        logger.info(f"SCP : Règle 1 sélectionnée pour #{next_game} -> {final_suit}")
        # Une fois la Règle 1 utilisée, on réinitialise le cooldown pour permettre une future imposition
        if scp_cooldown > 0:
            scp_cooldown = 0
            logger.info("SCP : Règle 1 utilisée, le Système Central pourra s'imposer à nouveau.")

    if final_suit:
        queue_prediction(next_game, final_suit, game_number)
    else:
        logger.info(f"SCP : Aucune règle applicable pour #{next_game}")

    # Envoi immédiat si possible
    await check_and_send_queued_predictions(game_number)

async def process_finalized_message(message_text: str, chat_id: int):
    """Traite uniquement la vérification des résultats quand le message est finalisé."""
    global current_game_number
    try:
        if chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            return

        if not is_message_finalized(message_text):
            return

        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number
        groups = extract_parentheses_groups(message_text)
        first_group = groups[0] if groups else ""

        # Vérification des résultats (seulement quand finalisé)
        if groups:
            await check_prediction_result(game_number, groups[0])

    except Exception as e:
        logger.error(f"Erreur Finalisé: {e}")

async def handle_message(event):
    """Gère les nouveaux messages dans les canaux sources."""
    try:
        sender = await event.get_sender()
        sender_id = getattr(sender, 'id', event.sender_id)
        
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
            
        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            # Prédiction immédiate sans attendre finalisation
            await process_prediction_logic(message_text, chat_id)
            
            # Commande /info pour l'admin
            if message_text.startswith('/info'):
                active_preds = len(pending_predictions)
                history_text = "\n".join([f"🔹 #{h['game']} ({h['suit']}) à {h['time']}" for h in scp_history]) if scp_history else "Aucune imposition récente."
                
                info_msg = (
                    "ℹ️ **ÉTAT DU SYSTÈME**\n\n"
                    f"🎮 Jeu actuel: #{current_game_number}\n"
                    f"🔮 Prédictions actives: {active_preds}\n"
                    f"⏳ Cooldown SCP: {'Actif' if scp_cooldown > 0 else 'Prêt'}\n\n"
                    "📌 **DERNIÈRES IMPOSITIONS SCP :**\n"
                    f"{history_text}\n\n"
                    "📈 Le bot suit le cycle de la Règle 1 par défaut."
                )
                await event.respond(info_msg)
                return

            # Vérification si finalisé
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)
            
        if sender_id == ADMIN_ID:
            if event.message.message.startswith('/'):
                logger.info(f"Commande admin reçue: {event.message.message}")

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")

async def handle_edited_message(event):
    """Gère les messages édités dans les canaux sources."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            # Relancer prédiction si besoin
            await process_prediction_logic(message_text, chat_id)
            
            if is_message_finalized(message_text):
                await process_finalized_message(message_text, chat_id)
        
        elif chat_id == SOURCE_CHANNEL_2_ID:
            message_text = event.message.message
            await process_stats_message(message_text)
            await check_and_send_queued_predictions(current_game_number)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

# --- Gestion des Messages (Hooks Telethon) ---

client.add_event_handler(handle_message, events.NewMessage())
client.add_event_handler(handle_edited_message, events.MessageEdited())

# --- Commandes Administrateur ---

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel: return
    await event.respond("🤖 **Bot de Prédiction Baccarat**\n\nCommandes: `/status`, `/help`, `/tim <min>`, `/bilan`")

@client.on(events.NewMessage(pattern=r'^/tim (\d+)$'))
async def cmd_set_tim(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    
    global bilan_interval
    try:
        bilan_interval = int(event.pattern_match.group(1))
        await event.respond(f"✅ Intervalle de bilan mis à jour : {bilan_interval} minutes\nProchain bilan automatique dans environ {bilan_interval} minutes.")
        logger.info(f"Intervalle de bilan modifié à {bilan_interval} min par l'admin.")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    await send_bilan()
    await event.respond("✅ Bilan manuel envoyé au canal.")

@client.on(events.NewMessage(pattern=r'^/a (\d+)$'))
async def cmd_set_a_shortcut(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Valeur de 'a' mise à jour : {USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'^/set_a (\d+)$'))
async def cmd_set_a(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0: return
    
    global USER_A
    try:
        val = int(event.pattern_match.group(1))
        USER_A = val
        await event.respond(f"✅ Valeur de 'a' mise à jour : {USER_A}\nLes prochaines prédictions seront sur le jeu N+{USER_A}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='/info'))
async def cmd_info(event):
    if event.is_group or event.is_channel: return
    
    active_preds = len(pending_predictions)
    history_text = "\n".join([f"🔹 #{h['game']} ({h['suit']}) à {h['time']}" for h in scp_history]) if scp_history else "Aucune imposition récente."
    
    info_msg = (
        "ℹ️ **ÉTAT DU SYSTÈME**\n\n"
        f"🎮 Jeu actuel: #{current_game_number}\n"
        f"🔮 Prédictions actives: {active_preds}\n"
        f"⏳ Cooldown SCP: {'Actif' if scp_cooldown > 0 else 'Prêt'}\n\n"
        "📌 **DERNIÈRES IMPOSITIONS SCP :**\n"
        f"{history_text}\n\n"
        "📈 Le bot suit le cycle de la Règle 1 par défaut."
    )
    await event.respond(info_msg)

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel: return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 **État du Bot:**\n\n"
    status_msg += f"🎮 Jeu actuel (Source 1): #{current_game_number}\n\n"
    
    if pending_predictions:
        status_msg += f"**🔮 Actives ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            ratt = f" (R{pred['rattrapage']})" if pred.get('rattrapage', 0) > 0 else ""
            status_msg += f"• #{game_num}{ratt}: {pred['suit']} - {pred['status']} (dans {distance})\n"
    else: status_msg += "**🔮 Aucune prédiction active**\n"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel: return
    await event.respond(f"""📖 **Aide - Bot de Prédiction V2**

**Règles de prédiction :**
1. Surveille le **Canal Source 2** (Stats).
2. Si un décalage d'au moins **6 jeux** existe entre deux cartes :
   - Prédit la carte en avance.
   - Cible le jeu : **Dernier numéro Source 1 + a**.
3. **Rattrapages :** Si la carte ne sort pas au jeu cible, le bot retente sur les **3 jeux suivants** (3 rattrapages).

**Commandes :**
- `/status` : Affiche l'état actuel.
- `/set_a <valeur>` : Modifie l'entier 'a' (par défaut 1).
- `/debug` : Infos techniques.
""")


# --- Serveur Web et Démarrage ---

async def index(request):
    html = f"""<!DOCTYPE html><html><head><title>Bot Prédiction Baccarat</title></head><body><h1>🎯 Bot de Prédiction Baccarat</h1><p>Le bot est en ligne et surveille les canaux.</p><p><strong>Jeu actuel:</strong> #{current_game_number}</p></body></html>"""
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    """Démarre le serveur web pour la vérification de l'état (health check)."""
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start() 

async def schedule_daily_reset():
    """Tâche planifiée pour la réinitialisation quotidienne des stocks de prédiction à 00h59 WAT."""
    wat_tz = timezone(timedelta(hours=1)) 
    reset_time = time(0, 59, tzinfo=wat_tz)

    logger.info(f"Tâche de reset planifiée pour {reset_time} WAT.")

    while True:
        now = datetime.now(wat_tz)
        target_datetime = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target_datetime:
            target_datetime += timedelta(days=1)
            
        time_to_wait = (target_datetime - now).total_seconds()

        logger.info(f"Prochain reset dans {timedelta(seconds=time_to_wait)}")
        await asyncio.sleep(time_to_wait)

        logger.warning("🚨 RESET QUOTIDIEN À 00h59 WAT DÉCLENCHÉ!")
        
        global pending_predictions, queued_predictions, processed_messages, last_transferred_game, current_game_number, last_source_game_number, stats_bilan
        
        pending_predictions.clear()
        queued_predictions.clear()
        processed_messages.clear()
        last_transferred_game = None
        current_game_number = 0
        last_source_game_number = 0
        
        # Reset des statistiques de bilan aussi au reset quotidien
        stats_bilan = {
            'total': 0,
            'wins': 0,
            'losses': 0,
            'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
            'loss_details': {'❌': 0}
        }
        
        logger.warning("✅ Toutes les données de prédiction ont été effacées.")

async def start_bot():
    """Démarre le client Telegram et les vérifications initiales."""
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("Démarrage du bot...")
        
        # Tentative de connexion avec retry pour gérer les FloodWait
        max_retries = 5
        for attempt in range(max_retries):
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.sign_in(bot_token=BOT_TOKEN)
                break
            except Exception as e:
                err_str = str(e).lower()
                if "wait of" in err_str:
                    match = re.search(r"wait of (\d+)", err_str)
                    wait_seconds = int(match.group(1)) + 5 if match else 30
                    logger.warning(f"FloodWait détecté: Attente de {wait_seconds} secondes (Essai {attempt + 1}/{max_retries})")
                    await asyncio.sleep(wait_seconds)
                else:
                    raise e
        
        source_channel_ok = True
        prediction_channel_ok = True 
        logger.info("Bot connecté et canaux marqués comme accessibles.")
        return True
    except Exception as e:
        logger.error(f"Erreur démarrage du client Telegram: {e}")
        return False

async def main():
    """Fonction principale pour lancer le serveur web, le bot et la tâche de reset."""
    try:
        await start_web_server()

        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage du bot")
            return

        # Lancement des tâches en arrière-plan
        asyncio.create_task(schedule_daily_reset())
        asyncio.create_task(auto_bilan_task())
        
        logger.info("Bot complètement opérationnel - En attente de messages...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur dans main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        if client and client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
