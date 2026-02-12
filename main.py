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
    handlers=[logging.StreamHandler(sys.stdout)]
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

# === SYSTÈME DE CYCLE DE PAUSE ===
PAUSE_CYCLE = [3, 5, 4]  # Cycle par défaut: 3min, 5min, 4min
pause_cycle_index = 0
current_prediction_count = 0
is_in_pause = False
pause_end_time = None
force_prediction_flag = False
last_prediction_time = None

# === RÈGLE 2: SYSTÈME CENTRAL ===
rule2_mirror_diff = 6  # Différence entre miroirs pour déclencher (configurable)
rule2_authorized_suit = None
rule2_is_active = False
rule2_game_target = None
rule2_last_trigger_time = None
rule2_consecutive_count = 0  # Compteur d'utilisations consécutives (max 2)
rule2_last_suit = None  # Dernière couleur utilisée par Règle 2

# === RÈGLE 1: ÉTAT ===
rule1_is_waiting = False
rule1_pending_game = None

# Structure: {game_number: {'message_id': int, 'suit': str, 'status': str, 'check_count': int}}
pending_predictions = {}
processed_messages = set()
current_game_number = 0
last_source_game_number = 0

stats_bilan = {
    'total': 0,
    'wins': 0,
    'losses': 0,
    'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0, '✅3️⃣': 0},
    'loss_details': {'❌': 0}
}
bilan_interval = 20
last_bilan_time = datetime.now()

source_channel_ok = False
prediction_channel_ok = False
transfer_enabled = True

# === ACCUMULATION DES DONNÉES POUR MAX GAPS ===
accumulated_stats = {
    'history': [],
    'last_max_gap_check': datetime.now(),
    'max_gap_interval': 5
}

def get_rule1_suit(game_number: int) -> str | None:
    """Calcule la couleur selon la règle 1 basée sur le cycle."""
    # Numéros pairs valides: 6-1436, pairs, ne finissant pas par 0
    if game_number < 6 or game_number > 1436 or game_number % 2 != 0 or game_number % 10 == 0:
        return None
    
    # Compter les numéros pairs valides jusqu'à game_number
    count_valid = 0
    for n in range(6, game_number + 1, 2):
        if n % 10 != 0:
            count_valid += 1
            
    if count_valid == 0: 
        return None
    
    index = (count_valid - 1) % 8
    return SUIT_CYCLE[index]

# --- Fonctions d'Analyse ---

def extract_game_number(message: str):
    """Extrait le numéro de jeu du message."""
    # Chercher #N suivi de chiffres
    match = re.search(r"#N\s*(\d+)", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    
    # Autres patterns possibles
    patterns = [
        r"^#(\d+)",
        r"N\s*(\d+)",
        r"Numéro\s*(\d+)",
        r"Game\s*(\d+)",
        r"(\d+)\s*\("  # Numéro suivi de parenthèse
    ]
    
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None

def parse_stats_message(message: str):
    """Extrait les statistiques du canal source 2."""
    stats = {}
    patterns = {
        '♠': r'♠️?\s*:?\s*(\d+)',
        '♥': r'♥️?\s*:?\s*(\d+)',
        '♦': r'♦️?\s*:?\s*(\d+)',
        '♣': r'♣️?\s*:?\s*(\d+)'
    }
    
    for suit, pattern in patterns.items():
        match = re.search(pattern, message)
        if match:
            stats[suit] = int(match.group(1))
    
    return stats if stats else None

def extract_parentheses_groups(message: str):
    """Extrait le contenu entre parenthèses."""
    groups = re.findall(r"\(([^)]*)\)", message)
    return groups

def normalize_suits(group_str: str) -> str:
    """Normalise les symboles de cartes."""
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    """Vérifie si la couleur cible est présente dans le groupe."""
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    
    for char in target_normalized:
        if char in normalized:
            return True
    return False

def is_message_finalized(message_text: str) -> bool:
    """Vérifie si le message est finalisé (contient ✅ ou 🔰)."""
    return "Finalisé" in message_text or "🔰" in message_text or "✅" in message_text

def is_message_editing(message_text: str) -> bool:
    """Vérifie si le message est en cours d'édition (commence par ⏰)."""
    return message_text.strip().startswith('⏰')

# === GESTION DU CYCLE DE PAUSE ===

async def start_pause_period():
    """Démarre une période de pause selon le cycle configuré."""
    global is_in_pause, pause_end_time, pause_cycle_index, current_prediction_count
    global rule2_consecutive_count, rule2_last_suit
    
    pause_duration = PAUSE_CYCLE[pause_cycle_index]
    is_in_pause = True
    pause_end_time = datetime.now() + timedelta(minutes=pause_duration)
    
    logger.info(f"⏸️ PAUSE DÉMARRÉE: {pause_duration} minutes")
    
    # Reset compteur Règle 2 après pause
    rule2_consecutive_count = 0
    rule2_last_suit = None
    
    if PREDICTION_CHANNEL_ID:
        try:
            pause_msg = f"⏸️ **PAUSE**\n⏱️ {pause_duration} minutes..."
            await client.send_message(PREDICTION_CHANNEL_ID, pause_msg)
            logger.info("✅ Message de pause envoyé")
        except Exception as e:
            logger.error(f"Erreur envoi message pause: {e}")
    
    pause_cycle_index = (pause_cycle_index + 1) % len(PAUSE_CYCLE)
    current_prediction_count = 0
    
    # Attendre la fin de la pause
    await asyncio.sleep(pause_duration * 60)
    
    is_in_pause = False
    pause_end_time = None
    
    logger.info("⏸️ PAUSE TERMINÉE - Prêt à reprendre")

async def can_launch_prediction() -> bool:
    """Vérifie si une prédiction peut être lancée."""
    global is_in_pause
    
    # Vérifier si pas déjà en pause
    if is_in_pause:
        logger.info("⏸️ En pause - pas de lancement")
        return False
    
    # Vérifier si on a atteint 4 prédictions (déclencher pause)
    if current_prediction_count >= 4:
        logger.info("📊 4 prédictions atteintes - déclenchement pause")
        asyncio.create_task(start_pause_period())
        return False
    
    return True

# === RÈGLE 2: SYSTÈME CENTRAL ===

async def process_stats_message(message_text: str):
    """Traite les statistiques du canal 2 pour la Règle 2."""
    global rule2_authorized_suit, rule2_last_trigger_time
    global rule2_consecutive_count, rule2_last_suit
    
    # Accumuler les données pour max gaps
    accumulated_stats['history'].append({
        'timestamp': datetime.now(),
        'message': message_text
    })
    if len(accumulated_stats['history']) > 50:
        accumulated_stats['history'].pop(0)
    
    stats = parse_stats_message(message_text)
    if not stats:
        return
    
    logger.info(f"📊 Stats reçues: {stats}")
    
    # Analyse des miroirs
    miroirs = [('♠', '♦'), ('♥', '♣')]
    
    selected_suit = None
    max_gap_found = 0
    
    for s1, s2 in miroirs:
        v1 = stats.get(s1, 0)
        v2 = stats.get(s2, 0)
        
        if v1 == 0 and v2 == 0:
            continue
            
        gap = abs(v1 - v2)
        logger.info(f"📊 Miroir {s1}/{s2}: {s1}={v1}, {s2}={v2}, Écart={gap}")
        
        if gap >= rule2_mirror_diff:
            if gap > max_gap_found:
                max_gap_found = gap
                selected_suit = s1 if v1 < v2 else s2
                logger.info(f"🎯 Écart {gap} >= {rule2_mirror_diff}! Cible: {selected_suit}")
    
    if selected_suit:
        if is_in_pause:
            logger.info("⏸️ Règle 2 détectée mais en pause - ignorée")
            return
        
        # Vérifier changement de couleur
        if rule2_last_suit is not None and selected_suit != rule2_last_suit:
            logger.info(f"🔄 Changement couleur: {rule2_last_suit} → {selected_suit}, reset compteur")
            rule2_consecutive_count = 0
        
        rule2_authorized_suit = selected_suit
        rule2_last_trigger_time = datetime.now()
        rule2_last_suit = selected_suit
        
        logger.info(f"🎯 RÈGLE 2 PRÊTE: {selected_suit} (utilisation {rule2_consecutive_count + 1}/2)")

# === PRÉDICTIONS ===

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, 
                                     forced=False, rule="Règle 1"):
    """Envoie la prédiction au canal avec le format simple."""
    global current_prediction_count, last_prediction_time
    global rule2_is_active, rule2_game_target, rule2_consecutive_count
    
    try:
        # Vérifier si prédiction déjà en cours pour ce numéro
        if target_game in pending_predictions:
            logger.info(f"⛔ Prédiction #{target_game} déjà en cours")
            return None
        
        # Si c'est une prédiction Règle 2, incrémenter compteur
        if rule == "Règle 2":
            rule2_consecutive_count += 1
            rule2_is_active = True
            rule2_game_target = target_game
            logger.info(f"🎯 Règle 2 utilisée ({rule2_consecutive_count}/2) pour #{target_game}")
            
            # Si on atteint 2 utilisations, désactiver pour la prochaine
            if rule2_consecutive_count >= 2:
                logger.info("🎯 Règle 2 atteint 2 utilisations, prochaine sera Règle 1")
                rule2_authorized_suit = None
        
        # Format SIMPLE (3 lignes)
        prediction_msg = f"""🎰 **PRÉDICTION #{target_game}**
🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
⏳ Statut: EN ATTENTE DU RÉSULTAT..."""

        msg_id = 0
        if PREDICTION_CHANNEL_ID:
            try:
                entity = await client.get_input_entity(PREDICTION_CHANNEL_ID)
                pred_msg = await client.send_message(entity, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ PRÉDICTION ENVOYÉE: #{target_game} - {predicted_suit} ({rule})")
                prediction_channel_ok = True
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction: {e}")
                return None

        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': predicted_suit,
            'base_game': base_game,
            'status': '⏳',
            'check_count': 0,
            'created_at': datetime.now().isoformat(),
            'forced': forced,
            'rule': rule
        }
        
        current_prediction_count += 1
        last_prediction_time = datetime.now()
        
        logger.info(f"📊 Compteur: {current_prediction_count}/4")
        
        # Vérifier si pause nécessaire
        if current_prediction_count >= 4 and not is_in_pause:
            asyncio.create_task(start_pause_period())

        return msg_id

    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None

async def update_prediction_status(game_number: int, new_status: str):
    """Met à jour le statut avec le format simple."""
    global rule2_is_active, rule2_game_target
    
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']
        rule = pred.get('rule', 'Règle 1')

        # Format simple pour résultat
        if '✅' in new_status:
            status_text = f"{new_status} GAGNÉ"
        elif '❌' in new_status:
            status_text = "❌ PERDU"
        else:
            status_text = new_status
        
        updated_msg = f"""🎰 **PRÉDICTION #{game_number}**
🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}
📊 Statut: {status_text}"""

        if PREDICTION_CHANNEL_ID and message_id > 0:
            try:
                entity = await client.get_input_entity(PREDICTION_CHANNEL_ID)
                await client.edit_message(entity, message_id, updated_msg)
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")

        pred['status'] = new_status
        
        # Si c'était Règle 2, marquer comme terminée
        if rule == "Règle 2" and game_number == rule2_game_target:
            rule2_is_active = False
            rule2_game_target = None
        
        # Stats
        if '✅' in new_status:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            win_key = new_status if new_status in stats_bilan['win_details'] else '✅3️⃣'
            stats_bilan['win_details'][win_key] = stats_bilan['win_details'].get(win_key, 0) + 1
            del pending_predictions[game_number]
            
        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            stats_bilan['loss_details']['❌'] = stats_bilan['loss_details'].get('❌', 0) + 1
            del pending_predictions[game_number]

        return True
    except Exception as e:
        logger.error(f"Erreur update_status: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    """Vérifie les résultats d'une prédiction sur 4 étapes."""
    first_group = normalize_suits(first_group)
    
    logger.info(f"🔍 Vérification pour #{game_number}, groupe: {first_group}")
    
    # Chercher quelle prédiction attend ce numéro
    for target_game, pred in list(pending_predictions.items()):
        predicted_suit = pred['suit']
        check_count = pred.get('check_count', 0)
        
        # Vérifier si c'est le numéro attendu pour ce check
        expected_number = target_game + check_count
        
        if game_number != expected_number:
            continue
        
        logger.info(f"🔍 Match: prédiction #{target_game} attendait check {check_count} sur #{expected_number}")
        
        if has_suit_in_group(first_group, predicted_suit):
            # Gagné
            status = f"✅{check_count}️⃣"
            await update_prediction_status(target_game, status)
            logger.info(f"✅ GAGNÉ #{target_game} au check {check_count}!")
            return True
        else:
            # Pas trouvé, passer au check suivant
            if check_count < 3:
                pred['check_count'] = check_count + 1
                next_num = target_game + pred['check_count']
                logger.info(f"❌ Check {check_count} échoué, prochain: #{next_num}")
                return False
            else:
                # Perdu après 4 vérifications
                await update_prediction_status(target_game, '❌')
                logger.info(f"❌ PERDU #{target_game} après 4 vérifications")
                return True
    
    return False

# === LANCEMENT AUTOMATIQUE DES PRÉDICTIONS ===

async def process_source_message(message_text: str, chat_id: int, is_edit: bool = False):
    """Traite le message du canal source."""
    global current_game_number, rule2_authorized_suit
    
    # Vérifier canal
    if chat_id != SOURCE_CHANNEL_ID:
        return
    
    game_number = extract_game_number(message_text)
    if game_number is None:
        logger.warning(f"⚠️ Numéro non extrait du message: {message_text[:50]}...")
        return
    
    current_game_number = game_number
    logger.info(f"📩 Message {'édité' if is_edit else 'reçu'}: Jeu #{game_number}")
    
    # Si message en édition, ignorer (attendre finalisation)
    if is_message_editing(message_text):
        logger.info(f"⏳ Message #{game_number} en édition, ignoré")
        return
    
    # === ÉTAPE 1: VÉRIFICATION RÉSULTAT (si finalisé et prédiction en cours) ===
    if is_message_finalized(message_text):
        groups = extract_parentheses_groups(message_text)
        if groups:
            result = await check_prediction_result(game_number, groups[0])
            if result:
                logger.info(f"✅ Résultat traité pour #{game_number}")
    
    # === ÉTAPE 2: LANCEMENT NOUVELLE PRÉDICTION (si impair et pas en pause) ===
    
    # Vérifier pause
    if is_in_pause:
        logger.info(f"⏸️ En pause - pas de lancement pour #{game_number}")
        return
    
    # Vérifier si impair (déclencheur)
    if game_number % 2 == 0:
        logger.info(f"⏭️ Numéro pair #{game_number} - pas de déclenchement")
        return
    
    target_even = game_number + 1
    
    # Vérifier validité du numéro cible
    if target_even > 1436 or target_even % 10 == 0 or target_even % 2 != 0:
        logger.info(f"⚠️ Cible #{target_even} invalide (hors range ou finissant par 0)")
        return
    
    # Vérifier si on peut lancer
    if not await can_launch_prediction():
        return
    
    # === DÉCISION RÈGLE 1 vs RÈGLE 2 ===
    final_suit = None
    rule_used = ""
    
    # RÈGLE 2 si active ET compteur < 2
    if rule2_authorized_suit and rule2_consecutive_count < 2:
        final_suit = rule2_authorized_suit
        rule_used = "Règle 2"
        logger.info(f"🎯 RÈGLE 2 sélectionnée: {final_suit} ({rule2_consecutive_count + 1}/2)")
    else:
        # RÈGLE 1 (par défaut ou si Règle 2 épuisée)
        if rule2_consecutive_count >= 2:
            logger.info("🔄 Règle 2 épuisée (2/2), passage à Règle 1")
            rule2_authorized_suit = None
        
        final_suit = get_rule1_suit(target_even)
        rule_used = "Règle 1"
        logger.info(f"🎯 RÈGLE 1 sélectionnée: {final_suit}")
    
    if final_suit:
        result = await send_prediction_to_channel(target_even, final_suit, game_number, rule=rule_used)
        if result:
            logger.info(f"🚀 Prédiction lancée avec succès")
        else:
            logger.error(f"❌ Échec lancement prédiction")
    else:
        logger.warning(f"❌ Aucune couleur déterminée pour #{target_even}")

# === MAX GAPS ===

async def send_max_gaps():
    """Envoie l'analyse des max gaps à l'admin."""
    if not accumulated_stats['history']:
        return
    
    all_stats = {}
    for entry in accumulated_stats['history']:
        stats = parse_stats_message(entry['message'])
        if stats:
            for suit, count in stats.items():
                if suit not in all_stats:
                    all_stats[suit] = []
                all_stats[suit].append(count)
    
    if not all_stats:
        return
    
    miroirs = [('♠', '♦'), ('♥', '♣')]
    gaps_info = []
    
    for s1, s2 in miroirs:
        if s1 in all_stats and s2 in all_stats:
            max_s1 = max(all_stats[s1])
            max_s2 = max(all_stats[s2])
            current_gap = abs(max_s1 - max_s2)
            gaps_info.append({
                'pair': f"{s1}/{s2}",
                'gap': current_gap,
                'details': f"{s1}={max_s1}, {s2}={max_s2}"
            })
    
    if gaps_info and ADMIN_ID:
        msg = "📊 **MAX GAPS**\n\n"
        for info in sorted(gaps_info, key=lambda x: x['gap'], reverse=True):
            alert = " 🚨" if info['gap'] >= rule2_mirror_diff else ""
            msg += f"{info['pair']}: {info['gap']}{alert} ({info['details']})\n"
        
        try:
            await client.send_message(ADMIN_ID, msg)
        except Exception as e:
            logger.error(f"Erreur envoi max gaps: {e}")

async def max_gap_monitor_task():
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        interval = timedelta(minutes=accumulated_stats['max_gap_interval'])
        
        if now - accumulated_stats['last_max_gap_check'] >= interval:
            await send_max_gaps()
            accumulated_stats['last_max_gap_check'] = now

# === COMMANDES ===

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond(
        "🤖 **Bot VIP Baccarat**\n\n"
        "📋 **Commandes:**\n"
        "`/status` - État du système\n"
        "`/setcycle 3,5,4` - Modifier cycle pause\n"
        "`/setdiff 6` - Différence miroirs Règle 2\n"
        "`/setgap 5` - Intervalle max gaps\n"
        "`/force` - Forcer prédiction\n"
        "`/pause` - État pause\n"
        "`/bilan` - Bilan\n\n"
        "🎯 **Fonctionnement:**\n"
        "• 4 prédictions → pause\n"
        "• Règle 2 max 2x consécutives\n"
        "• Vérification sur 4 numéros"
    )

@client.on(events.NewMessage(pattern=r'^/setcycle ([\d,]+)$'))
async def cmd_set_cycle(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    global PAUSE_CYCLE, pause_cycle_index, current_prediction_count
    
    try:
        cycle_str = event.pattern_match.group(1)
        new_cycle = [int(x.strip()) for x in cycle_str.split(',')]
        
        if len(new_cycle) < 1 or any(x <= 0 for x in new_cycle):
            await event.respond("❌ Format: `/setcycle 3,5,4`")
            return
        
        PAUSE_CYCLE = new_cycle
        pause_cycle_index = 0
        current_prediction_count = 0
        
        await event.respond(f"✅ **Cycle**: {', '.join([str(x)+'min' for x in PAUSE_CYCLE])}")
        logger.info(f"Nouveau cycle: {PAUSE_CYCLE}")
        
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'^/setdiff (\d+)$'))
async def cmd_set_diff(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    global rule2_mirror_diff
    
    try:
        new_diff = int(event.pattern_match.group(1))
        if new_diff < 2:
            await event.respond("❌ Minimum 2")
            return
        
        old_diff = rule2_mirror_diff
        rule2_mirror_diff = new_diff
        
        await event.respond(f"✅ **Différence**: {old_diff} → {rule2_mirror_diff}")
        logger.info(f"Différence modifiée: {old_diff} -> {new_diff}")
        
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'^/setgap (\d+)$'))
async def cmd_set_gap(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    global accumulated_stats
    try:
        minutes = int(event.pattern_match.group(1))
        if minutes < 1:
            await event.respond("❌ Minimum 1 minute")
            return
        
        accumulated_stats['max_gap_interval'] = minutes
        await event.respond(f"✅ Intervalle max gaps: {minutes}min")
        
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='^/force$'))
async def cmd_force(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    global force_prediction_flag, is_in_pause, current_game_number
    
    if is_in_pause:
        force_prediction_flag = True
        is_in_pause = False
        await event.respond("🚀 **Pause interrompue**")
        return
    
    if current_game_number == 0:
        await event.respond("❌ Aucun numéro reçu")
        return
    
    # Calculer prochain pair
    if current_game_number % 2 == 0:
        next_odd = current_game_number + 1
    else:
        next_odd = current_game_number + 2
    
    target_even = next_odd + 1
    
    # Déterminer règle
    if rule2_authorized_suit and rule2_consecutive_count < 2:
        suit = rule2_authorized_suit
        rule = "Règle 2"
    else:
        suit = get_rule1_suit(target_even)
        rule = "Règle 1"
    
    if suit:
        await send_prediction_to_channel(target_even, suit, current_game_number, forced=True, rule=rule)
        await event.respond(f"🚀 **Prédiction forcée**: #{target_even} - {SUIT_DISPLAY.get(suit, suit)}")
    else:
        await event.respond("❌ Impossible de forcer")

@client.on(events.NewMessage(pattern='^/pause$'))
async def cmd_pause(event):
    if event.is_group or event.is_channel:
        return
    
    if is_in_pause and pause_end_time:
        remaining = int((pause_end_time - datetime.now()).total_seconds() / 60)
        await event.respond(f"⏸️ **PAUSE**\n⏱️ Restant: ~{remaining}min\n📊 Série: {current_prediction_count}/4")
    else:
        await event.respond(f"✅ **ACTIF**\n📊 Série: {current_prediction_count}/4\n⏱️ Cycle: {', '.join([str(x)+'min' for x in PAUSE_CYCLE])}")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    
    status = (
        f"📊 **État Bot**\n\n"
        f"🎮 Jeu: #{current_game_number}\n"
        f"⏸️ Pause: {'Oui' if is_in_pause else 'Non'}\n"
        f"📊 Série: {current_prediction_count}/4\n"
        f"⚖️ Diff miroirs: {rule2_mirror_diff}\n"
        f"🎯 Règle 2: {rule2_consecutive_count}/2\n\n"
    )
    
    if rule2_authorized_suit:
        status += f"🎯 Règle 2 prête: {rule2_authorized_suit}\n"
    
    if pending_predictions:
        status += f"\n**🔮 En cours ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            check = pred.get('check_count', 0)
            rule = pred.get('rule', 'R1')
            status += f"• #{game_num}: {pred['suit']} (check {check}/3, {rule})\n"
    else:
        status += "\n**🔮 Aucune prédiction active**"

    await event.respond(status)

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    
    if stats_bilan['total'] == 0:
        await event.respond("📊 Aucune statistique")
        return
    
    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100
    
    msg = (
        f"📊 **BILAN**\n\n"
        f"✅ {win_rate:.1f}% | ❌ {100-win_rate:.1f}%\n\n"
        f"✅0️⃣: {stats_bilan['win_details'].get('✅0️⃣', 0)} "
        f"✅1️⃣: {stats_bilan['win_details'].get('✅1️⃣', 0)} "
        f"✅2️⃣: {stats_bilan['win_details'].get('✅2️⃣', 0)} "
        f"✅3️⃣: {stats_bilan['win_details'].get('✅3️⃣', 0)}\n"
        f"❌: {stats_bilan['loss_details'].get('❌', 0)}\n"
        f"Total: {stats_bilan['total']}"
    )
    
    await event.respond(msg)

# === GESTION DES MESSAGES ===

@client.on(events.NewMessage())
async def handle_new_message(event):
    """Gère tous les nouveaux messages."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        
        # Normaliser l'ID du canal
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        
        message_text = event.message.message
        
        logger.debug(f"Message de {chat_id}: {message_text[:50]}...")
        
        # CANAL SOURCE 1
        if chat_id == SOURCE_CHANNEL_ID:
            await process_source_message(message_text, chat_id, is_edit=False)
        
        # CANAL SOURCE 2
        elif chat_id == SOURCE_CHANNEL_2_ID:
            await process_stats_message(message_text)
            
    except Exception as e:
        logger.error(f"Erreur handle_new_message: {e}")

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    """Gère les messages édités."""
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        
        message_text = event.message.message
        
        if chat_id == SOURCE_CHANNEL_ID:
            await process_source_message(message_text, chat_id, is_edit=True)
            
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")

# === SERVEUR WEB ===

async def index(request):
    html = f"""<!DOCTYPE html>
    <html><head><title>Bot VIP</title></head>
    <body>
        <h1>🎯 Bot VIP Baccarat</h1>
        <p>Jeu: #{current_game_number}</p>
        <p>Pause: {'Oui' if is_in_pause else 'Non'}</p>
        <p>Série: {current_prediction_count}/4</p>
        <p>Actives: {len(pending_predictions)}</p>
        <p>Règle 2: {rule2_consecutive_count}/2</p>
    </body></html>"""
    return web.Response(text=html, content_type='text/html')

async def health_check(request):
    return web.Response(text="OK", status=200)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()

async def schedule_daily_reset():
    wat_tz = timezone(timedelta(hours=1))
    reset_time = time(0, 59, tzinfo=wat_tz)
    
    while True:
        now = datetime.now(wat_tz)
        target = datetime.combine(now.date(), reset_time, tzinfo=wat_tz)
        if now >= target:
            target += timedelta(days=1)
        
        await asyncio.sleep((target - now).total_seconds())
        
        logger.warning("🚨 RESET QUOTIDIEN")
        
        global pending_predictions, accumulated_stats
        global current_prediction_count, pause_cycle_index, is_in_pause
        global rule2_authorized_suit, rule2_is_active, rule2_game_target
        global rule2_consecutive_count, rule2_last_suit
        global stats_bilan
        
        pending_predictions.clear()
        accumulated_stats['history'].clear()
        current_prediction_count = 0
        pause_cycle_index = 0
        is_in_pause = False
        rule2_authorized_suit = None
        rule2_is_active = False
        rule2_game_target = None
        rule2_consecutive_count = 0
        rule2_last_suit = None
        
        stats_bilan = {
            'total': 0, 'wins': 0, 'losses': 0,
            'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0, '✅3️⃣': 0},
            'loss_details': {'❌': 0}
        }

async def start_bot():
    try:
        logger.info("🚀 Démarrage Bot VIP Baccarat...")
        
        max_retries = 5
        for attempt in range(max_retries):
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.sign_in(bot_token=BOT_TOKEN)
                break
            except Exception as e:
                if "wait of" in str(e).lower():
                    match = re.search(r"wait of (\d+)", str(e))
                    wait = int(match.group(1)) + 5 if match else 30
                    logger.warning(f"FloodWait: attente {wait}s")
                    await asyncio.sleep(wait)
                else:
                    raise
        
        logger.info("✅ Bot connecté!")
        logger.info(f"📊 Cycle: {PAUSE_CYCLE} (4 prédictions)")
        logger.info(f"⚖️ Diff miroirs: {rule2_mirror_diff}")
        logger.info(f"🎯 Règle 2 max: 2 consécutives")
        logger.info(f"📺 Source: {SOURCE_CHANNEL_ID}")
        logger.info(f"🎯 Prédiction: {PREDICTION_CHANNEL_ID}")
        
        return True
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    try:
        await start_web_server()
        
        if not await start_bot():
            return
        
        asyncio.create_task(schedule_daily_reset())
        asyncio.create_task(max_gap_monitor_task())
        
        logger.info("🤖 Bot opérationnel!")
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
