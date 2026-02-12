import os
import asyncio
import re
import logging
import sys
import json
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import EditBannedRequest
from telethon.tl.types import ChatBannedRights
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    PORT, SUIT_DISPLAY, SOURCE_CHANNEL_2_ID,
    DEFAULT_MIRROR_THRESHOLD, RULE2_CONFIG_FILE
)

USERS_FILE = "users_data.json"
PAUSE_CONFIG_FILE = "pause_config.json"
CHANNELS_CONFIG_FILE = "channels_config.json"
TRIAL_CONFIG_FILE = "trial_config.json"

# Configuration par dÃ©faut des canaux
DEFAULT_SOURCE_CHANNEL_ID = -1002682552255
DEFAULT_PREDICTION_CHANNEL_ID = -1003329818758
DEFAULT_VIP_CHANNEL_ID = -1003329818758
DEFAULT_VIP_CHANNEL_LINK = "https://t.me/+s3y7GejUVHU0YjE0"

# --- Configuration Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# --- Variables Globales ---
channels_config = {
    'source_channel_id': DEFAULT_SOURCE_CHANNEL_ID,
    'prediction_channel_id': DEFAULT_PREDICTION_CHANNEL_ID,
    'vip_channel_id': DEFAULT_VIP_CHANNEL_ID,
    'vip_channel_link': DEFAULT_VIP_CHANNEL_LINK
}

# Cycle de pause par dÃ©faut: 3min, 5min, 4min
DEFAULT_PAUSE_CYCLE = [180, 300, 240]
pause_config = {
    'cycle': DEFAULT_PAUSE_CYCLE.copy(),
    'current_index': 0,
    'predictions_count': 0,
    'is_paused': False,
    'pause_end_time': None,
    'just_resumed': False
}

DEFAULT_TRIAL_DURATION = 1440
trial_config = {
    'duration_minutes': DEFAULT_TRIAL_DURATION
}

# Configuration RÃ¨gle 2
rule2_config = {
    'enabled': True,
    'threshold': DEFAULT_MIRROR_THRESHOLD
}

# Ã‰tat global
users_data = {}
current_game_number = 0
last_source_game_number = 0
last_predicted_number = None
predictions_enabled = True
already_predicted_games = set()

# Ã‰tat de vÃ©rification
verification_state = {
    'predicted_number': None,
    'predicted_suit': None,
    'current_check': 0,
    'message_id': None,
    'channel_id': None,
    'status': None,
    'base_game': None
}

SUIT_CYCLE = ['â™¥', 'â™¦', 'â™£', 'â™ ', 'â™¦', 'â™¥', 'â™ ', 'â™£']

stats_bilan = {
    'total': 0, 'wins': 0, 'losses': 0,
    'win_details': {'âœ…0ï¸âƒ£': 0, 'âœ…1ï¸âƒ£': 0, 'âœ…2ï¸âƒ£': 0, 'âœ…3ï¸âƒ£': 0},
    'loss_details': {'âŒ': 0}
}

# Ã‰tats conversation
user_conversation_state = {}
admin_setting_time = {}
watch_state = {}

# ============================================================
# FONCTIONS DE CHARGEMENT/SAUVEGARDE
# ============================================================

def load_json(file_path, default=None):
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Erreur chargement {file_path}: {e}")
    return default or {}

def save_json(file_path, data):
    try:
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erreur sauvegarde {file_path}: {e}")

def load_all_configs():
    global channels_config, pause_config, trial_config, users_data, rule2_config
    channels_config.update(load_json(CHANNELS_CONFIG_FILE, channels_config))
    pause_config.update(load_json(PAUSE_CONFIG_FILE, pause_config))
    trial_config.update(load_json(TRIAL_CONFIG_FILE, trial_config))
    users_data.update(load_json(USERS_FILE, {}))
    rule2_config.update(load_json(RULE2_CONFIG_FILE, rule2_config))
    logger.info("Configurations chargÃ©es")

def save_all_configs():
    save_json(CHANNELS_CONFIG_FILE, channels_config)
    save_json(PAUSE_CONFIG_FILE, pause_config)
    save_json(TRIAL_CONFIG_FILE, trial_config)
    save_json(USERS_FILE, users_data)
    save_json(RULE2_CONFIG_FILE, rule2_config)

# ============================================================
# GESTION NUMÃ‰ROS ET COSTUMES
# ============================================================

def get_valid_even_numbers():
    """GÃ©nÃ¨re la liste des pairs valides: 6-1436, pairs, ne finissant pas par 0"""
    valid = []
    for num in range(6, 1437):
        if num % 2 == 0 and num % 10 != 0:
            valid.append(num)
    return valid

VALID_EVEN_NUMBERS = get_valid_even_numbers()
logger.info(f"ðŸ“Š Pairs valides: {len(VALID_EVEN_NUMBERS)} numÃ©ros")

def get_suit_for_number(number):
    """Retourne le costume pour un numÃ©ro pair valide"""
    if number not in VALID_EVEN_NUMBERS:
        logger.error(f"âŒ NumÃ©ro {number} non valide")
        return None
    idx = VALID_EVEN_NUMBERS.index(number) % len(SUIT_CYCLE)
    return SUIT_CYCLE[idx]

def is_trigger_number(number):
    """DÃ©clencheur: impair finissant par 1,3,5,7 ET suivant est pair valide"""
    if number % 2 == 0:
        return False

    last_digit = number % 10
    if last_digit not in [1, 3, 5, 7]:
        return False

    next_num = number + 1
    is_valid = next_num in VALID_EVEN_NUMBERS

    if is_valid:
        logger.info(f"ðŸ”¥ DÃ‰CLENCHEUR #{number} (suivant: #{next_num})")

    return is_valid

def get_trigger_target(number):
    """Retourne le numÃ©ro pair Ã  prÃ©dire"""
    if not is_trigger_number(number):
        return None
    return number + 1

# ============================================================
# GESTION CANAUX
# ============================================================

def get_source_channel_id():
    return channels_config.get('source_channel_id', DEFAULT_SOURCE_CHANNEL_ID)

def get_prediction_channel_id():
    return channels_config.get('prediction_channel_id', DEFAULT_PREDICTION_CHANNEL_ID)

def get_vip_channel_id():
    return channels_config.get('vip_channel_id', DEFAULT_VIP_CHANNEL_ID)

def get_vip_channel_link():
    return channels_config.get('vip_channel_link', DEFAULT_VIP_CHANNEL_LINK)

def set_channels(source_id=None, prediction_id=None, vip_id=None, vip_link=None):
    if source_id:
        channels_config['source_channel_id'] = source_id
    if prediction_id:
        channels_config['prediction_channel_id'] = prediction_id
    if vip_id:
        channels_config['vip_channel_id'] = vip_id
    if vip_link:
        channels_config['vip_channel_link'] = vip_link
    save_json(CHANNELS_CONFIG_FILE, channels_config)
    logger.info(f"Canaux mis Ã  jour")

# ============================================================
# GESTION UTILISATEURS
# ============================================================

def get_user(user_id: int) -> dict:
    user_id_str = str(user_id)
    if user_id_str not in users_data:
        users_data[user_id_str] = {
            'registered': False, 'nom': None, 'prenom': None, 'pays': None,
            'trial_started': None, 'trial_used': False, 'trial_joined_at': None,
            'subscription_end': None, 'vip_expires_at': None, 'is_in_channel': False,
            'total_time_added': 0
        }
        save_json(USERS_FILE, users_data)
    return users_data[user_id_str]

def update_user(user_id: int, data: dict):
    users_data[str(user_id)].update(data)
    save_json(USERS_FILE, users_data)

def is_user_subscribed(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    user = get_user(user_id)
    if not user.get('subscription_end'):
        return False
    try:
        return datetime.now() < datetime.fromisoformat(user['subscription_end'])
    except:
        return False

def is_trial_active(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    user = get_user(user_id)
    if user.get('trial_used') or not user.get('trial_joined_at'):
        return False
    try:
        trial_end = datetime.fromisoformat(user['trial_joined_at']) + timedelta(minutes=trial_config['duration_minutes'])
        return datetime.now() < trial_end
    except:
        return False

def format_time_remaining(expiry_iso: str) -> str:
    try:
        expiry = datetime.fromisoformat(expiry_iso)
        remaining = expiry - datetime.now()
        if remaining.total_seconds() <= 0:
            return "ExpirÃ©"
        total_seconds = int(remaining.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if seconds > 0 or not parts:
            parts.append(f"{seconds}s")
        return " ".join(parts)
    except:
        return "Inconnu"

def format_seconds(seconds: int) -> str:
    if seconds <= 0:
        return "ExpirÃ©"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or (hours == 0 and minutes == 0):
        parts.append(f"{secs}s")
    return " ".join(parts)

def get_remaining_time(user_id: int) -> str:
    if user_id == ADMIN_ID:
        return "âˆž"
    user = get_user(user_id)
    if is_user_subscribed(user_id):
        return format_time_remaining(user['subscription_end'])
    elif is_trial_active(user_id):
        trial_end = datetime.fromisoformat(user['trial_joined_at']) + timedelta(minutes=trial_config['duration_minutes'])
        remaining = int((trial_end - datetime.now()).total_seconds())
        return format_seconds(remaining)
    return "ExpirÃ©"

def parse_duration(input_str: str) -> int:
    input_str = input_str.strip().lower()
    if input_str.isdigit():
        return int(input_str)
    if input_str.endswith('h'):
        try:
            return int(float(input_str[:-1]) * 60)
        except:
            return 0
    if input_str.endswith('m'):
        try:
            return int(input_str[:-1])
        except:
            return 0
    return 0

# ============================================================
# GESTION VIP
# ============================================================

async def delete_message_after_delay(chat_id: int, message_id: int, delay_seconds: int):
    await asyncio.sleep(delay_seconds)
    try:
        await client.delete_messages(chat_id, [message_id])
    except:
        pass

async def add_user_to_vip(user_id: int, duration_minutes: int, is_trial: bool = False):
    """Ajoute un utilisateur au VIP avec lien qui disparaÃ®t en 10s"""
    if user_id == ADMIN_ID:
        return True

    try:
        now = datetime.now()
        expires_at = now + timedelta(minutes=duration_minutes)

        update_data = {
            'vip_joined_at': now.isoformat(),
            'vip_expires_at': expires_at.isoformat(),
            'subscription_end': expires_at.isoformat(),
            'is_in_channel': True,
            'total_time_added': get_user(user_id).get('total_time_added', 0) + duration_minutes
        }

        if is_trial:
            update_data['trial_joined_at'] = now.isoformat()
        else:
            update_data['trial_used'] = True

        update_user(user_id, update_data)

        time_str = format_time_remaining(expires_at.isoformat())
        vip_link = get_vip_channel_link()

        link_msg = await client.send_message(user_id, f"""ðŸŽ‰ **{'ESSAI GRATUIT' if is_trial else 'ABONNEMENT'} ACTIVÃ‰!** ðŸŽ‰

âœ… **AccÃ¨s VIP confirmÃ©!**
â³ **Temps restant:** {time_str}
ðŸ“… **Expire le:** {expires_at.strftime('%d/%m/%Y Ã  %H:%M')}

ðŸ”— **Lien du canal VIP:**
{vip_link}

âš ï¸ **CE LIEN DISPARAÃŽT DANS 10 SECONDES!**
ðŸš¨ **REJOIGNEZ IMMÃ‰DIATEMENT!**

Vous serez retirÃ© automatiquement Ã  l'expiration.""")

        asyncio.create_task(delete_message_after_delay(user_id, link_msg.id, 10))

        user = get_user(user_id)
        await client.send_message(ADMIN_ID, f"""âœ… **{'ESSAI' if is_trial else 'UTILISATEUR'} ACTIVÃ‰**

ðŸ†” `{user_id}`
ðŸ‘¤ {user.get('prenom', '')} {user.get('nom', '')}
ðŸŒ {user.get('pays', 'N/A')}
â±ï¸ {duration_minutes} minutes
â³ Expire: {time_str}
ðŸ“Š Total: {user.get('total_time_added', 0)} min""")

        asyncio.create_task(auto_kick_user(user_id, duration_minutes * 60))

        logger.info(f"âœ… Utilisateur {user_id} ajoutÃ© au VIP pour {duration_minutes}min")
        return True

    except Exception as e:
        logger.error(f"âŒ Erreur ajout VIP {user_id}: {e}")
        return False

async def extend_user_time(user_id: int, additional_minutes: int):
    """Prolonge le temps d'un utilisateur"""
    try:
        user = get_user(user_id)

        if is_user_subscribed(user_id) or is_trial_active(user_id):
            current_end = datetime.fromisoformat(user.get('subscription_end') or user.get('vip_expires_at'))
            new_end = current_end + timedelta(minutes=additional_minutes)
        else:
            new_end = datetime.now() + timedelta(minutes=additional_minutes)

        update_user(user_id, {
            'subscription_end': new_end.isoformat(),
            'vip_expires_at': new_end.isoformat(),
            'total_time_added': user.get('total_time_added', 0) + additional_minutes,
            'is_in_channel': True
        })

        time_str = format_time_remaining(new_end.isoformat())

        await client.send_message(user_id, f"""â±ï¸ **TEMPS AJOUTÃ‰!**

âœ… {additional_minutes} minutes ajoutÃ©es!
ðŸ“… Nouvelle fin: {new_end.strftime('%d/%m/%Y Ã  %H:%M')}
â³ Temps restant: {time_str}

ðŸš€ Profitez bien!""")

        await client.send_message(ADMIN_ID, f"""âœ… **TEMPS PROLONGÃ‰**

ðŸ†” `{user_id}`
ðŸ‘¤ {user.get('prenom', '')} {user.get('nom', '')}
â±ï¸ AjoutÃ©: {additional_minutes} minutes
â³ Nouveau total: {time_str}
ðŸ“… Expire: {new_end.strftime('%d/%m/%Y %H:%M')}""")

        remaining_seconds = int((new_end - datetime.now()).total_seconds())
        asyncio.create_task(auto_kick_user(user_id, remaining_seconds))

        logger.info(f"âœ… Temps prolongÃ© pour {user_id}: +{additional_minutes}min")
        return True

    except Exception as e:
        logger.error(f"âŒ Erreur prolongation {user_id}: {e}")
        return False

async def auto_kick_user(user_id: int, delay_seconds: int):
    """Expulse automatiquement aprÃ¨s le dÃ©lai"""
    if user_id == ADMIN_ID:
        return

    await asyncio.sleep(delay_seconds)

    try:
        if is_user_subscribed(user_id):
            logger.info(f"Utilisateur {user_id} a renouvelÃ©, annulation expulsion")
            return

        user = get_user(user_id)
        entity = await client.get_input_entity(get_vip_channel_id())

        await client.kick_participant(entity, user_id)
        await client(EditBannedRequest(
            channel=entity, participant=user_id,
            banned_rights=ChatBannedRights(until_date=None, view_messages=False)
        ))

        update_user(user_id, {
            'vip_expires_at': None, 'subscription_end': None,
            'is_in_channel': False, 'trial_used': True
        })

        await client.send_message(user_id, """â° **VOTRE ACCÃˆS EST TERMINÃ‰**

âœ… Pour rÃ©intÃ©grer le canal:
/start""")

        await client.send_message(ADMIN_ID, f"""ðŸš« **UTILISATEUR RETIRÃ‰**

ðŸ†” `{user_id}`
ðŸ‘¤ {user.get('prenom', '')} {user.get('nom', '')}""")

        logger.info(f"ðŸš« Utilisateur {user_id} expulsÃ©")

    except Exception as e:
        logger.error(f"Erreur expulsion {user_id}: {e}")

# ============================================================
# SYSTÃˆME DE PRÃ‰DICTION ET VÃ‰RIFICATION
# ============================================================

async def send_prediction(target_game: int, predicted_suit: str, base_game: int, rule_name: str = "RÃ¨gle 1"):
    """Envoie une prÃ©diction au canal configurÃ©"""
    global verification_state, last_predicted_number

    if not predictions_enabled:
        logger.warning("â›” PrÃ©dictions dÃ©sactivÃ©es")
        return False

    if verification_state['predicted_number'] is not None:
        logger.error(f"â›” BLOQUÃ‰: PrÃ©diction #{verification_state['predicted_number']} en cours!")
        return False

    try:
        prediction_channel_id = get_prediction_channel_id()
        entity = await client.get_input_entity(prediction_channel_id)

        prediction_text = f"""ðŸŽ° **PRÃ‰DICTION #{target_game}**
ðŸŽ¯ Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
ðŸ“‹ RÃ¨gle: {rule_name}
â³ Statut: EN ATTENTE DU RÃ‰SULTAT..."""

        sent_msg = await client.send_message(entity, prediction_text)

        verification_state = {
            'predicted_number': target_game,
            'predicted_suit': predicted_suit,
            'current_check': 0,
            'message_id': sent_msg.id,
            'channel_id': prediction_channel_id,
            'status': 'pending',
            'base_game': base_game
        }

        last_predicted_number = target_game

        logger.info(f"ðŸš€ PRÃ‰DICTION #{target_game} ({predicted_suit}) LANCÃ‰E via {rule_name}")
        logger.info(f"ðŸ” Attente vÃ©rification: #{target_game} (check 0/3)")

        return True

    except Exception as e:
        logger.error(f"âŒ Erreur envoi prÃ©diction: {e}")
        return False

async def update_prediction_status(status: str):
    """Met Ã  jour le statut de la prÃ©diction"""
    global verification_state, stats_bilan

    if verification_state['predicted_number'] is None:
        logger.error("âŒ Aucune prÃ©diction Ã  mettre Ã  jour")
        return False

    try:
        predicted_num = verification_state['predicted_number']
        predicted_suit = verification_state['predicted_suit']

        if status == "âŒ":
            status_text = "âŒ PERDU"
        else:
            status_text = f"{status} GAGNÃ‰"

        updated_text = f"""ðŸŽ° **PRÃ‰DICTION #{predicted_num}**
ðŸŽ¯ Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}
ðŸ“Š Statut: {status_text}"""

        await client.edit_message(
            verification_state['channel_id'],
            verification_state['message_id'],
            updated_text
        )

        if status in ['âœ…0ï¸âƒ£', 'âœ…1ï¸âƒ£', 'âœ…2ï¸âƒ£', 'âœ…3ï¸âƒ£']:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            stats_bilan['win_details'][status] = stats_bilan['win_details'].get(status, 0) + 1
            logger.info(f"ðŸŽ‰ #{predicted_num} GAGNÃ‰ ({status})")
        elif status == 'âŒ':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            logger.info(f"ðŸ’” #{predicted_num} PERDU")

        logger.info(f"ðŸ”“ SYSTÃˆME LIBÃ‰RÃ‰ - Nouvelle prÃ©diction possible")

        verification_state = {
            'predicted_number': None, 'predicted_suit': None,
            'current_check': 0, 'message_id': None,
            'channel_id': None, 'status': None, 'base_game': None
        }

        return True

    except Exception as e:
        logger.error(f"âŒ Erreur mise Ã  jour statut: {e}")
        return False

# ============================================================
# COMMANDES UTILISATEURS
# ============================================================

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return

    user_id = event.sender_id

    if user_id == ADMIN_ID:
        await event.respond("""ðŸ‘‘ **ADMINISTRATEUR**

Commandes:
/stop /resume /forcestop - ContrÃ´le
/predictinfo - Statut systÃ¨me
/clearverif - DÃ©bloquer
/users /monitor /watch - Utilisateurs
/setchannel - Canaux
/pausecycle - Cycle pause (ex: 3,5,4)
/extend - Prolonger temps
/bilan - Stats
/reset - Reset stats
/rule2 - Config RÃ¨gle 2
/help - Aide complÃ¨te""")
        return

    user = get_user(user_id)

    if user.get('registered'):
        await event.respond(f"""ðŸ‘‹ Bonjour {user.get('prenom', '')}!

ðŸ“Š Statut: {'âœ… AbonnÃ©' if is_user_subscribed(user_id) else 'ðŸŽ Essai' if is_trial_active(user_id) else 'âŒ Inactif'}
â³ Temps: {get_remaining_time(user_id)}

ðŸ’¡ /help pour aide""")
        return

    user_conversation_state[user_id] = 'awaiting_nom'
    await event.respond("""ðŸ‘‹ **Bienvenue sur le Bot Baccarat!**

ðŸŽ° SystÃ¨me de prÃ©dictions automatiques

ðŸ“ **Ã‰tape 1/3:** Votre nom de famille?""")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    user_id = event.sender_id

    if user_id == ADMIN_ID:
        await event.respond("""ðŸ“– **AIDE ADMINISTRATEUR**

**ContrÃ´le:**
/stop - ArrÃªter prÃ©dictions
/resume - Reprendre prÃ©dictions  
/forcestop - Forcer arrÃªt immÃ©diat (dÃ©blocage)

**Monitoring:**
/predictinfo - Statut systÃ¨me prÃ©diction
/clearverif - Effacer vÃ©rification bloquÃ©e
/users - Liste tous les utilisateurs
/monitor - Voir temps restant
/watch - Surveillance temps rÃ©el auto
/stopwatch - ArrÃªter surveillance

**Configuration:**
/setchannel source ID - Canal source
/setchannel prediction ID - Canal prÃ©diction  
/setchannel vip ID LIEN - Canal VIP
/pausecycle - Voir/modifier cycle pause (dÃ©faut: 3,5,4)

**RÃ¨gle 2 (Miroirs):**
/rule2 - Voir statut et config
/rule2 on/off - Activer/dÃ©sactiver
/rule2 threshold N - Changer seuil (dÃ©faut: 6)

**Gestion:**
/extend ID durÃ©e - Prolonger temps abonnÃ©/essai
/bilan - Statistiques prÃ©dictions
/reset - Reset stats (garde utilisateurs)

**Support:** @Kouamappoloak""")
        return

    await event.respond("""ðŸ“– **AIDE UTILISATEUR**

/start - Inscription / Voir statut
/status - Temps restant
/help - Cette aide

**Comment Ã§a marche:**
1ï¸âƒ£ Inscrivez-vous avec /start
2ï¸âƒ£ Recevez 15min d'essai gratuit
3ï¸âƒ£ Rejoignez le canal VIP rapidement (lien 10s)

Le bot prÃ©dit automatiquement les numÃ©ros avec 2 rÃ¨gles:
â€¢ RÃ¨gle 1: NumÃ©ros pairs valides
â€¢ RÃ¨gle 2: Analyse des miroirs (Canal Stats)

**Support:** @Kouamappoloak""")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return

    user_id = event.sender_id
    user = get_user(user_id)

    if not user.get('registered'):
        await event.respond("âŒ /start pour vous inscrire")
        return

    status = "ðŸ‘‘ ADMIN" if user_id == ADMIN_ID else "âœ… AbonnÃ©" if is_user_subscribed(user_id) else "ðŸŽ Essai actif" if is_trial_active(user_id) else "âŒ Inactif"

    await event.respond(f"""ðŸ“Š **VOTRE STATUT**

ðŸ‘¤ {user.get('prenom', '')} {user.get('nom', '')}
ðŸŒ {user.get('pays', 'N/A')}
ðŸ“Š {status}
â³ {get_remaining_time(user_id)}

ðŸ’¡ /help pour l'aide""")

# ============================================================
# COMMANDES ADMIN
# ============================================================

@client.on(events.NewMessage(pattern='/stop'))
async def cmd_stop(event):
    if event.sender_id != ADMIN_ID:
        return
    global predictions_enabled
    predictions_enabled = False
    await event.respond("ðŸ›‘ **PRÃ‰DICTIONS ARRÃŠTÃ‰ES**")

@client.on(events.NewMessage(pattern='/forcestop'))
async def cmd_forcestop(event):
    """Force l'arrÃªt complet et dÃ©bloque le systÃ¨me"""
    if event.sender_id != ADMIN_ID:
        return

    global predictions_enabled, verification_state, already_predicted_games

    predictions_enabled = False
    old_pred = verification_state['predicted_number']

    verification_state = {
        'predicted_number': None, 'predicted_suit': None,
        'current_check': 0, 'message_id': None,
        'channel_id': None, 'status': None, 'base_game': None
    }

    already_predicted_games.clear()

    msg = "ðŸš¨ **ARRÃŠT FORCÃ‰**\n\n"
    msg += f"ðŸ›‘ PrÃ©dictions dÃ©sactivÃ©es\n"
    msg += f"ðŸ”“ SystÃ¨me dÃ©bloquÃ©"
    if old_pred:
        msg += f"\nðŸ—‘ï¸ PrÃ©diction #{old_pred} effacÃ©e"

    await event.respond(msg)

@client.on(events.NewMessage(pattern='/resume'))
async def cmd_resume(event):
    if event.sender_id != ADMIN_ID:
        return
    global predictions_enabled
    predictions_enabled = True
    await event.respond("ðŸš€ **PRÃ‰DICTIONS REPRISES**")

@client.on(events.NewMessage(pattern='/predictinfo'))
async def cmd_predictinfo(event):
    if event.sender_id != ADMIN_ID:
        return

    verif_info = "Aucune"
    if verification_state['predicted_number']:
        next_check = verification_state['predicted_number'] + verification_state['current_check']
        verif_info = f"""#{verification_state['predicted_number']} ({verification_state['predicted_suit']})
Check: {verification_state['current_check']}/3
Attend: #{next_check}"""

    cycle_mins = [x//60 for x in pause_config['cycle']]
    current_idx = pause_config['current_index'] % len(pause_config['cycle'])
    next_pause_idx = (pause_config['current_index']) % len(pause_config['cycle'])

    rule2_status = "ðŸŸ¢ ON" if rule2_config.get('enabled', True) else "ðŸ”´ OFF"
    rule2_threshold = rule2_config.get('threshold', DEFAULT_MIRROR_THRESHOLD)

    await event.respond(f"""ðŸ“Š **STATUT SYSTÃˆME**

ðŸŽ¯ Source: #{current_game_number}
ðŸ” VÃ©rification: {verif_info}
ðŸŸ¢ PrÃ©dictions: {'ON' if predictions_enabled else 'OFF'}

â¸ï¸ **CYCLE DE PAUSE:**
â€¢ Actif: {'Oui' if pause_config['is_paused'] else 'Non'}
â€¢ Compteur: {pause_config['predictions_count']}/5
â€¢ Cycle: {cycle_mins} minutes
â€¢ Position: {current_idx + 1}/{len(cycle_mins)}
â€¢ Prochaine pause: {cycle_mins[next_pause_idx]} min

ðŸ”„ **RÃˆGLE 2 (Miroirs):**
â€¢ Statut: {rule2_status}
â€¢ Seuil: {rule2_threshold}
â€¢ Canal: {SOURCE_CHANNEL_2_ID}

ðŸ’¡ /pausecycle pour modifier
ðŸ’¡ /rule2 pour config RÃ¨gle 2
ðŸ’¡ /clearverif si bloquÃ©
ðŸ’¡ /forcestop pour dÃ©bloquer""")

@client.on(events.NewMessage(pattern='/clearverif'))
async def cmd_clearverif(event):
    if event.sender_id != ADMIN_ID:
        return

    global verification_state
    old = verification_state['predicted_number']

    verification_state = {
        'predicted_number': None, 'predicted_suit': None,
        'current_check': 0, 'message_id': None,
        'channel_id': None, 'status': None, 'base_game': None
    }

    await event.respond(f"âœ… **{'VÃ©rification #' + str(old) + ' effacÃ©e' if old else 'Aucune vÃ©rification'}**\nðŸš€ SystÃ¨me libÃ©rÃ©")

@client.on(events.NewMessage(pattern=r'^/pausecycle(\s*[\d\s,]*)?$'))
async def cmd_pausecycle(event):
    """Configure le cycle de pause"""
    if event.sender_id != ADMIN_ID:
        return

    message_text = event.message.message.strip()
    parts = message_text.split()

    # Afficher configuration actuelle
    if len(parts) == 1:
        cycle_mins = [x//60 for x in pause_config['cycle']]
        current_idx = pause_config['current_index'] % len(cycle_mins)

        # Calculer prochaines pauses
        next_pauses = []
        for i in range(3):
            idx = (pause_config['current_index'] + i) % len(cycle_mins)
            next_pauses.append(f"{cycle_mins[idx]}min")

        await event.respond(f"""â¸ï¸ **CONFIGURATION CYCLE DE PAUSE**

**Cycle configurÃ©:** {cycle_mins} minutes
**Ordre d'exÃ©cution:** {' â†’ '.join([f'{m}min' for m in cycle_mins])} â†’ recommence

**Ã‰tat actuel:**
â€¢ Position: {current_idx + 1}/{len(cycle_mins)}
â€¢ Compteur: {pause_config['predictions_count']}/5 prÃ©dictions
â€¢ Prochaines pauses: {' â†’ '.join(next_pauses)}

**Modifier le cycle:**
`/pausecycle 3,5,4` (minutes, sÃ©parÃ©es par virgule)
`/pausecycle 5,10,7,3` (autant de valeurs que voulu)

**Fonctionnement:**
AprÃ¨s chaque 5 prÃ©dictions â†’ pause selon le cycle configurÃ©""")
        return

    # Modifier le cycle
    try:
        cycle_str = ' '.join(parts[1:])
        cycle_str = cycle_str.replace(' ', '').replace(',', ',')
        new_cycle_mins = [int(x.strip()) for x in cycle_str.split(',') if x.strip()]

        if not new_cycle_mins or any(x <= 0 for x in new_cycle_mins):
            await event.respond("âŒ Le cycle doit contenir des nombres positifs (minutes)")
            return

        # Convertir en secondes et sauvegarder
        new_cycle = [x * 60 for x in new_cycle_mins]
        pause_config['cycle'] = new_cycle
        pause_config['current_index'] = 0  # Reset position
        save_json(PAUSE_CONFIG_FILE, pause_config)

        await event.respond(f"""âœ… **CYCLE MIS Ã€ JOUR**

**Nouveau cycle:** {new_cycle_mins} minutes
**Ordre:** {' â†’ '.join([f'{m}min' for m in new_cycle_mins])} â†’ recommence

ðŸ”„ Prochaine sÃ©rie: 5 prÃ©dictions puis {new_cycle_mins[0]} minutes de pause""")

    except Exception as e:
        await event.respond(f"âŒ Erreur: {e}\n\nFormat: `/pausecycle 3,5,4`")

@client.on(events.NewMessage(pattern=r'^/rule2(\s+.+)?$'))
async def cmd_rule2(event):
    """Configure la RÃ¨gle 2 (analyse des miroirs)"""
    if event.sender_id != ADMIN_ID:
        return

    parts = event.message.message.strip().split()

    # Afficher configuration actuelle
    if len(parts) == 1:
        current_threshold = rule2_config.get('threshold', DEFAULT_MIRROR_THRESHOLD)
        is_enabled = rule2_config.get('enabled', True)

        await event.respond(f"""ðŸ”„ **CONFIGURATION RÃˆGLE 2 (MIROIRS)**

**Statut:** {'ðŸŸ¢ ACTIVÃ‰E' if is_enabled else 'ðŸ”´ DÃ‰SACTIVÃ‰E'}
**Seuil actuel:** {current_threshold}
**Canal Source 2:** {SOURCE_CHANNEL_2_ID}

**Fonctionnement:**
Analyse les stats du Canal 2 et dÃ©clenche si:
â€¢ |â™  - â™¦| â‰¥ seuil  OU  |â™¥ - â™£| â‰¥ seuil

PrÃ©dit: dernier_numÃ©ro + 1 avec costume le plus faible

**Commandes:**
`/rule2 on` - Activer
`/rule2 off` - DÃ©sactiver  
`/rule2 threshold 6` - Changer seuil (dÃ©faut: 6)

**Note:** La RÃ¨gle 2 respecte les pauses comme la RÃ¨gle 1""")
        return

    # Modifier configuration
    try:
        cmd = parts[1].lower()

        if cmd in ['on', 'enable', 'activer']:
            rule2_config['enabled'] = True
            save_json(RULE2_CONFIG_FILE, rule2_config)
            await event.respond("âœ… **RÃ¨gle 2 ACTIVÃ‰E**\nðŸ”„ Surveillance du Canal Source 2 active")

        elif cmd in ['off', 'disable', 'desactiver']:
            rule2_config['enabled'] = False
            save_json(RULE2_CONFIG_FILE, rule2_config)
            await event.respond("ðŸ”´ **RÃ¨gle 2 DÃ‰SACTIVÃ‰E**\nâ¸ï¸ Surveillance du Canal Source 2 arrÃªtÃ©e")

        elif cmd == 'threshold' or cmd == 'seuil':
            if len(parts) < 3:
                await event.respond("âŒ Fournissez une valeur\nEx: `/rule2 threshold 6`")
                return

            try:
                new_threshold = int(parts[2])
                if new_threshold < 1:
                    await event.respond("âŒ Le seuil doit Ãªtre â‰¥ 1")
                    return

                rule2_config['threshold'] = new_threshold
                save_json(RULE2_CONFIG_FILE, rule2_config)
                await event.respond(f"âœ… **Seuil RÃ¨gle 2 mis Ã  jour:** {new_threshold}")

            except ValueError:
                await event.respond("âŒ Valeur invalide")
        else:
            await event.respond("âŒ Commande invalide. Utilisez: on, off, threshold")

    except Exception as e:
        await event.respond(f"âŒ Erreur: {e}")

@client.on(events.NewMessage(pattern='/watch'))
async def cmd_watch(event):
    if event.sender_id != ADMIN_ID:
        return

    msg = await event.respond("â±ï¸ **SURVEILLANCE TEMPS RÃ‰EL**\nDÃ©marrage...")
    watch_state[event.sender_id] = {'msg_id': msg.id, 'active': True}
    asyncio.create_task(watch_loop(event.sender_id))

async def watch_loop(admin_id):
    while watch_state.get(admin_id, {}).get('active', False):
        await asyncio.sleep(30)
        try:
            lines = ["â±ï¸ **SURVEILLANCE TEMPS RÃ‰EL**\n"]

            for uid_str, info in users_data.items():
                uid = int(uid_str)
                if uid == ADMIN_ID:
                    continue
                if is_user_subscribed(uid) or is_trial_active(uid):
                    name = f"{info.get('prenom', '')} {info.get('nom', '')}".strip() or "N/A"
                    lines.append(f"`{uid}` | {name[:15]} | {get_remaining_time(uid)}")

            if len(lines) == 1:
                lines.append("Aucun utilisateur actif")

            lines.append(f"\nðŸ”„ {datetime.now().strftime('%H:%M:%S')} | /stopwatch")

            await client.edit_message(admin_id, watch_state[admin_id]['msg_id'], "\n".join(lines[:35]))
        except:
            break

@client.on(events.NewMessage(pattern='/stopwatch'))
async def cmd_stopwatch(event):
    if event.sender_id != ADMIN_ID:
        return
    watch_state[event.sender_id] = {'active': False}
    await event.respond("âœ… Surveillance arrÃªtÃ©e")

@client.on(events.NewMessage(pattern=r'^/setchannel(\s+.+)?$'))
async def cmd_setchannel(event):
    if event.sender_id != ADMIN_ID:
        return

    parts = event.message.message.strip().split()

    if len(parts) < 3:
        await event.respond(f"""ðŸ“º **CONFIGURATION CANAUX**

**Actuel:**
â€¢ Source 1: `{get_source_channel_id()}`
â€¢ Source 2 (Stats): `{SOURCE_CHANNEL_2_ID}`
â€¢ PrÃ©diction: `{get_prediction_channel_id()}`
â€¢ VIP: `{get_vip_channel_id()}`
â€¢ Lien VIP: {get_vip_channel_link()}

**Modifier:**
`/setchannel source -1001234567890`
`/setchannel prediction -1001234567890`  
`/setchannel vip -1001234567890 https://t.me/...`""")
        return

    try:
        ctype = parts[1].lower()
        cid = int(parts[2])

        if ctype == 'source':
            set_channels(source_id=cid)
            await event.respond(f"âœ… **Canal source:**\n`{cid}`")

        elif ctype == 'prediction':
            set_channels(prediction_id=cid)
            await event.respond(f"âœ… **Canal prÃ©diction:**\n`{cid}`\n\nðŸŽ¯ Les prÃ©dictions seront envoyÃ©es ici")

        elif ctype == 'vip':
            if len(parts) < 4:
                await event.respond("âŒ Fournissez aussi le lien du canal VIP\nFormat: `/setchannel vip ID https://t.me/...`")
                return
            set_channels(vip_id=cid, vip_link=parts[3])
            await event.respond(f"""âœ… **Canal VIP mis Ã  jour**

ID: `{cid}`
Lien: {parts[3]}

âš ï¸ Ce lien sera envoyÃ© aux nouveaux abonnÃ©s (disparaÃ®t en 10s)""")
        else:
            await event.respond("âŒ Type invalide. Utilisez: source, prediction, ou vip")

    except Exception as e:
        await event.respond(f"âŒ Erreur: {e}")

@client.on(events.NewMessage(pattern=r'^/extend(\s+\d+)?(\s+.+)?$'))
async def cmd_extend(event):
    """Prolonge le temps d'un abonnÃ© ou essai"""
    if event.sender_id != ADMIN_ID:
        return

    parts = event.message.message.strip().split()

    if len(parts) < 3:
        await event.respond("""â±ï¸ **PROLONGER TEMPS**

**Usage:** `/extend ID_UTILISATEUR DURÃ‰E`

**Exemples:**
â€¢ `/extend 123456789 60` â†’ +60 minutes
â€¢ `/extend 123456789 2h` â†’ +2 heures
â€¢ `/extend 123456789 30m` â†’ +30 minutes

**Note:** Fonctionne pour abonnÃ©s ET pÃ©riodes d'essai""")
        return

    try:
        target_id = int(parts[1])
        duration_str = parts[2]

        if str(target_id) not in users_data:
            await event.respond(f"âŒ Utilisateur `{target_id}` non trouvÃ©")
            return

        additional_minutes = parse_duration(duration_str)

        if additional_minutes < 1:
            await event.respond("âŒ DurÃ©e invalide (minimum 1 minute)")
            return

        success = await extend_user_time(target_id, additional_minutes)

        if success:
            await event.respond(f"âœ… **Temps ajoutÃ©:** {additional_minutes} minutes pour `{target_id}`")
        else:
            await event.respond(f"âŒ Erreur lors de l'ajout")

    except ValueError:
        await event.respond("âŒ ID invalide")
    except Exception as e:
        await event.respond(f"âŒ Erreur: {e}")

@client.on(events.NewMessage(pattern='/bilan'))
async def cmd_bilan(event):
    if event.sender_id != ADMIN_ID:
        return

    if stats_bilan['total'] == 0:
        await event.respond("ðŸ“Š Aucune prÃ©diction enregistrÃ©e")
        return

    win_rate = (stats_bilan['wins'] / stats_bilan['total']) * 100

    await event.respond(f"""ðŸ“Š **BILAN PRÃ‰DICTIONS**

ðŸŽ¯ **Total:** {stats_bilan['total']}
âœ… **Victoires:** {stats_bilan['wins']} ({win_rate:.1f}%)
âŒ **DÃ©faites:** {stats_bilan['losses']}

**DÃ©tails victoires:**
â€¢ ImmÃ©diat (N): {stats_bilan['win_details'].get('âœ…0ï¸âƒ£', 0)}
â€¢ 2Ã¨me chance (N+1): {stats_bilan['win_details'].get('âœ…1ï¸âƒ£', 0)}
â€¢ 3Ã¨me chance (N+2): {stats_bilan['win_details'].get('âœ…2ï¸âƒ£', 0)}
â€¢ 4Ã¨me chance (N+3): {stats_bilan['win_details'].get('âœ…3ï¸âƒ£', 0)}""")

@client.on(events.NewMessage(pattern='/reset'))
async def cmd_reset(event):
    """Reset uniquement les stats, garde les utilisateurs"""
    if event.sender_id != ADMIN_ID:
        return

    global stats_bilan, already_predicted_games, verification_state

    nb_users = len([u for u in users_data if int(u) != ADMIN_ID])

    stats_bilan = {
        'total': 0, 'wins': 0, 'losses': 0,
        'win_details': {'âœ…0ï¸âƒ£': 0, 'âœ…1ï¸âƒ£': 0, 'âœ…2ï¸âƒ£': 0, 'âœ…3ï¸âƒ£': 0},
        'loss_details': {'âŒ': 0}
    }

    already_predicted_games.clear()

    old_pred = verification_state['predicted_number']
    verification_state = {
        'predicted_number': None, 'predicted_suit': None,
        'current_check': 0, 'message_id': None,
        'channel_id': None, 'status': None, 'base_game': None
    }

    await event.respond(f"""ðŸš¨ **RESET STATS EFFECTUÃ‰**

âœ… **ConservÃ©:**
â€¢ {nb_users} utilisateurs enregistrÃ©s
â€¢ Abonnements et essais actifs
â€¢ Configuration canaux
â€¢ Cycle de pause configurÃ©
â€¢ Configuration RÃ¨gle 2

ðŸ—‘ï¸ **RÃ©initialisÃ©:**
â€¢ Statistiques prÃ©dictions
â€¢ Historique prÃ©dictions{f" (#{old_pred})" if old_pred else ""}

ðŸ’¡ Les utilisateurs gardent leur accÃ¨s!""")

# ============================================================
# GESTION MESSAGES ET INSCRIPTION
# ============================================================

@client.on(events.NewMessage)
async def handle_messages(event):
    # Canal source 1
    if event.is_group or event.is_channel:
        if event.chat_id == get_source_channel_id():
            await process_source_message(event)
        # Canal source 2 (statistiques pour RÃ¨gle 2)
        elif event.chat_id == SOURCE_CHANNEL_2_ID:
            await process_source2_message(event)
        return

    # Commandes ignorÃ©es
    if event.message.message.startswith('/'):
        return

    user_id = event.sender_id

    # Inscription conversation
    if user_id in user_conversation_state:
        state = user_conversation_state[user_id]
        text = event.message.message.strip()

        if state == 'awaiting_nom':
            update_user(user_id, {'nom': text})
            user_conversation_state[user_id] = 'awaiting_prenom'
            await event.respond("âœ… **Ã‰tape 2/3:** Votre prÃ©nom?")
            return

        elif state == 'awaiting_prenom':
            update_user(user_id, {'prenom': text})
            user_conversation_state[user_id] = 'awaiting_pays'
            await event.respond("âœ… **Ã‰tape 3/3:** Votre pays?")
            return

        elif state == 'awaiting_pays':
            update_user(user_id, {
                'pays': text, 'registered': True,
                'trial_started': datetime.now().isoformat()
            })
            del user_conversation_state[user_id]

            await add_user_to_vip(user_id, trial_config['duration_minutes'], is_trial=True)
            await event.respond(f"""ðŸŽ‰ **Inscription rÃ©ussie!**
â³ Essai gratuit: {trial_config['duration_minutes']} minutes

âš ï¸ Rejoignez vite le canal, le lien disparaÃ®t en 10 secondes!""")
            return

@client.on(events.MessageEdited)
async def handle_edit(event):
    if event.is_group or event.is_channel:
        if event.chat_id == get_source_channel_id():
            await process_source_message(event, is_edit=True)

# ============================================================
# SERVEUR WEB
# ============================================================

async def web_index(request):
    cycle_mins = [x//60 for x in pause_config['cycle']]
    current_idx = pause_config['current_index'] % len(cycle_mins)

    html = f"""<!DOCTYPE html>
<html>
<head><title>Bot Baccarat</title>
<style>
body {{ font-family: Arial; background: linear-gradient(135deg, #1e3c72, #2a5298); color: white; text-align: center; padding: 50px; }}
.status {{ background: rgba(255,255,255,0.1); padding: 20px; border-radius: 10px; display: inline-block; margin: 10px; min-width: 120px; }}
.number {{ font-size: 2em; color: #ffd700; font-weight: bold; }}
.label {{ font-size: 0.9em; opacity: 0.8; margin-bottom: 5px; }}
</style></head>
<body>
<h1>ðŸŽ° Bot Baccarat</h1>
<div class="status"><div class="label">Jeu Actuel</div><div class="number">#{current_game_number}</div></div>
<div class="status"><div class="label">Utilisateurs</div><div class="number">{len([u for u in users_data if int(u) != ADMIN_ID])}</div></div>
<div class="status"><div class="label">VÃ©rification</div><div class="number">{verification_state['predicted_number'] or 'Libre'}</div></div>
<div class="status"><div class="label">PrÃ©dictions</div><div class="number">{'ðŸŸ¢ ON' if predictions_enabled else 'ðŸ”´ OFF'}</div></div>
<div class="status"><div class="label">Pause</div><div class="number">{pause_config['predictions_count']}/5</div></div>
<p style="margin-top: 30px; opacity: 0.8;">
â¸ï¸ Cycle: {cycle_mins} min | Position: {current_idx + 1}/{len(cycle_mins)} | {'â¸ï¸ EN PAUSE' if pause_config['is_paused'] else 'â–¶ï¸ ACTIF'}
</p>
<p>ðŸ”„ {datetime.now().strftime('%H:%M:%S')}</p>
</body></html>"""
    return web.Response(text=html, content_type='text/html')

async def start_web():
    app = web.Application()
    app.router.add_get('/', web_index)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()

# ============================================================
# DÃ‰MARRAGE
# ============================================================

async def main():
    load_all_configs()
    await start_web()
    await client.start(bot_token=BOT_TOKEN)

    cycle_mins = [x//60 for x in pause_config['cycle']]
    rule2_threshold = rule2_config.get('threshold', DEFAULT_MIRROR_THRESHOLD)
    rule2_enabled = rule2_config.get('enabled', True)

    logger.info("=" * 60)
    logger.info("ðŸš€ BOT BACCARAT DÃ‰MARRÃ‰")
    logger.info(f"ðŸ‘‘ Admin ID: {ADMIN_ID}")
    logger.info(f"ðŸ“º Source 1: {get_source_channel_id()}")
    logger.info(f"ðŸ“º Source 2 (Stats): {SOURCE_CHANNEL_2_ID}")
    logger.info(f"ðŸŽ¯ PrÃ©diction: {get_prediction_channel_id()}")
    logger.info(f"â­ VIP: {get_vip_channel_id()}")
    logger.info(f"â¸ï¸ Cycle pause: {cycle_mins} min")
    logger.info(f"ðŸ”„ RÃ¨gle 2: {'ON' if rule2_enabled else 'OFF'} (seuil: {rule2_threshold})")
    logger.info("=" * 60)

    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
