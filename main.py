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

# Vérifications
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

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

# Variables Globales
SUIT_CYCLE = ['♥', '♦', '♣', '♠', '♦', '♥', '♠', '♣']

PAUSE_CYCLE = [3, 5, 4]
pause_cycle_index = 0
current_prediction_count = 0
is_in_pause = False
pause_end_time = None
force_prediction_flag = False
last_prediction_time = None

rule2_mirror_diff = 6
rule2_authorized_suit = None
rule2_is_active = False
rule2_game_target = None
rule2_last_trigger_time = None

rule1_is_waiting = False
rule1_pending_game = None

pending_predictions = {}
queued_predictions = {}
processed_messages = set()
current_game_number = 0
last_source_game_number = 0

stats_bilan = {
    'total': 0, 'wins': 0, 'losses': 0,
    'win_details': {'✅0️⃣': 0, '✅1️⃣': 0, '✅2️⃣': 0},
    'loss_details': {'❌': 0}
}
bilan_interval = 20
last_bilan_time = datetime.now()

accumulated_stats = {
    'history': [],
    'last_max_gap_check': datetime.now(),
    'max_gap_interval': 5
}

def get_rule1_suit(game_number: int) -> str | None:
    if game_number < 6 or game_number > 1436 or game_number % 2 != 0 or game_number % 10 == 0:
        return None
    count_valid = 0
    for n in range(6, game_number + 1, 2):
        if n % 10 != 0:
            count_valid += 1
    if count_valid == 0: 
        return None
    index = (count_valid - 1) % 8
    return SUIT_CYCLE[index]

def extract_game_number(message: str):
    """Extrait le numéro avec plusieurs patterns."""
    patterns = [
        r"#N\s*(\d+)",
        r"#\s*(\d+)",
        r"N\s*(\d+)",
        r"Jeu\s*#?\s*(\d+)",
        r"Numéro\s*#?\s*(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None

def parse_stats_message(message: str):
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
    groups = re.findall(r"\d+\(([^)]*)\)", message)
    return groups

def normalize_suits(group_str: str) -> str:
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for char in target_normalized:
        if char in normalized:
            return True
    return False

def is_message_finalized(message_text: str) -> bool:
    return "Finalisé" in message_text or "🔰" in message_text or "✅" in message_text

async def start_pause_period():
    global is_in_pause, pause_end_time, pause_cycle_index, current_prediction_count
    if current_prediction_count < 4:
        return False
    pause_duration = PAUSE_CYCLE[pause_cycle_index]
    is_in_pause = True
    pause_end_time = datetime.now() + timedelta(minutes=pause_duration)
    logger.info(f"⏸️ PAUSE: {pause_duration} min")
    if PREDICTION_CHANNEL_ID != 0:
        try:
            await client.send_message(PREDICTION_CHANNEL_ID, f"⏸️ **PAUSE**\\n⏱️ {pause_duration} min...")
        except Exception as e:
            logger.error(f"Erreur envoi pause: {e}")
    pause_cycle_index = (pause_cycle_index + 1) % len(PAUSE_CYCLE)
    current_prediction_count = 0
    await asyncio.sleep(pause_duration * 60)
    is_in_pause = False
    pause_end_time = None
    logger.info("⏸️ FIN PAUSE")
    if PREDICTION_CHANNEL_ID != 0:
        try:
            await client.send_message(PREDICTION_CHANNEL_ID, "✅ **Fin pause**")
        except:
            pass
    return True

async def can_launch_prediction() -> bool:
    global is_in_pause
    if is_in_pause:
        logger.info("⏸️ Pause active")
        return False
    active_waiting = [p for p in pending_predictions.values() 
                      if p['status'] == '⌛' and p.get('rattrapage', 0) == 0]
    if active_waiting:
        logger.info(f"⏳ {len(active_waiting)} en attente")
        return False
    if current_prediction_count >= 4:
        logger.info("📊 4 atteintes → pause")
        asyncio.create_task(start_pause_period())
        return False
    return True

async def process_stats_message(message_text: str):
    global rule2_authorized_suit, rule2_is_active, rule2_game_target
    global rule2_last_trigger_time, accumulated_stats, rule1_is_waiting
    accumulated_stats['history'].append({'timestamp': datetime.now(), 'message': message_text})
    if len(accumulated_stats['history']) > 50:
        accumulated_stats['history'].pop(0)
    stats = parse_stats_message(message_text)
    if not stats:
        return
    logger.info(f"📊 Stats: {stats}")
    miroirs = [('♠', '♦'), ('♥', '♣')]
    selected_suit = None
    max_gap_found = 0
    for s1, s2 in miroirs:
        v1 = stats.get(s1, 0)
        v2 = stats.get(s2, 0)
        if v1 == 0 and v2 == 0:
            continue
        gap = abs(v1 - v2)
        if gap >= rule2_mirror_diff:
            if gap > max_gap_found:
                max_gap_found = gap
                selected_suit = s1 if v1 < v2 else s2
    if selected_suit:
        if is_in_pause:
            return
        if rule2_is_active:
            rule2_authorized_suit = selected_suit
            return
        rule2_authorized_suit = selected_suit
        rule2_last_trigger_time = datetime.now()
        logger.info(f"🎯 RÈGLE 2: {selected_suit}")
        if PREDICTION_CHANNEL_ID != 0:
            try:
                await client.send_message(PREDICTION_CHANNEL_ID, 
                    f"🎯 **Règle 2**\\n📊 Écart: {max_gap_found}\\n🎨 {SUIT_DISPLAY.get(selected_suit, selected_suit)}")
            except:
                pass

async def send_prediction_to_channel(target_game: int, predicted_suit: str, base_game: int, 
                                     forced=False, rattrapage=0, original_game=None, rule="Règle 1"):
    global current_prediction_count, last_prediction_time
    global rule2_is_active, rule2_game_target, rule1_is_waiting, rule1_pending_game
    try:
        logger.info(f"🚀 Envoi #{target_game} - {predicted_suit} au canal {PREDICTION_CHANNEL_ID}")
        active_waiting = [p for p in pending_predictions.values() 
                         if p['status'] == '⌛' and p.get('rattrapage', 0) == 0]
        if active_waiting and not forced and rattrapage == 0:
            logger.info("⏳ Déjà actif")
            return None
        if rule == "Règle 2" and rattrapage == 0:
            rule2_is_active = True
            rule2_game_target = target_game
        if rule == "Règle 1" and rule2_is_active and not forced:
            rule1_is_waiting = True
            rule1_pending_game = target_game
            logger.info("⏳ Règle 1 en attente")
            return None
        if rattrapage > 0:
            pending_predictions[target_game] = {
                'message_id': 0, 'suit': predicted_suit, 'base_game': base_game,
                'status': '🔮', 'rattrapage': rattrapage, 'original_game': original_game,
                'created_at': datetime.now().isoformat(), 'forced': forced, 'rule': rule
            }
            logger.info(f"🔁 Rattrapage {rattrapage} #{target_game}")
            return 0
        prediction_msg = (
            f"VIP DE KOUAMÉ & JOKER:\\n"
            f"🎰 **PRÉDICTION #{target_game}**\\n"
            f"🎯 Couleur: {SUIT_DISPLAY.get(predicted_suit, predicted_suit)}\\n"
            f"📊 Statut: ⌛ EN COURS\\n"
            f"📋 Règle: {rule}"
        )
        if forced:
            prediction_msg += "\\n⚡ **FORCÉE**"
        msg_id = 0
        if PREDICTION_CHANNEL_ID != 0:
            try:
                logger.info(f"📤 Envoi au canal {PREDICTION_CHANNEL_ID}...")
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Envoyé: #{target_game}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi: {e}")
        else:
            logger.error("❌ PREDICTION_CHANNEL_ID = 0!")
        pending_predictions[target_game] = {
            'message_id': msg_id, 'suit': predicted_suit, 'base_game': base_game,
            'status': '⌛', 'check_count': 0, 'rattrapage': 0,
            'created_at': datetime.now().isoformat(), 'forced': forced, 'rule': rule
        }
        current_prediction_count += 1
        last_prediction_time = datetime.now()
        if current_prediction_count >= 4 and not is_in_pause:
            asyncio.create_task(start_pause_period())
        return msg_id
    except Exception as e:
        logger.error(f"Erreur critique: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None

async def update_prediction_status(game_number: int, new_status: str):
    global rule2_is_active, rule2_game_target, rule2_authorized_suit
    global rule1_is_waiting, rule1_pending_game
    try:
        if game_number not in pending_predictions:
            return False
        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']
        forced = pred.get('forced', False)
        rule = pred.get('rule', 'Règle 1')
        status_text = "GAGNÉ" if '✅' in new_status else "PERDU" if '❌' in new_status else new_status
        updated_msg = (
            f"VIP DE KOUAMÉ & JOKER:\\n"
            f"🎰 **PRÉDICTION #{game_number}**\\n"
            f"🎯 Couleur: {SUIT_DISPLAY.get(suit, suit)}\\n"
            f"📊 Statut: {new_status} {status_text}"
        )
        if forced:
            updated_msg += "\\n⚡ **FORCÉE**"
        if PREDICTION_CHANNEL_ID != 0 and message_id > 0:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
            except Exception as e:
                logger.error(f"Erreur edit: {e}")
        pred['status'] = new_status
        if rule == "Règle 2" and game_number == rule2_game_target:
            rule2_is_active = False
            rule2_game_target = None
            rule2_authorized_suit = None
            if rule1_is_waiting and rule1_pending_game:
                logger.info(f"🚀 Règle 1 libérée")
                rule1_is_waiting = False
        if '✅' in new_status:
            stats_bilan['total'] += 1
            stats_bilan['wins'] += 1
            win_key = new_status if new_status in stats_bilan['win_details'] else '✅2️⃣'
            stats_bilan['win_details'][win_key] += 1
            del pending_predictions[game_number]
        elif new_status == '❌':
            stats_bilan['total'] += 1
            stats_bilan['losses'] += 1
            stats_bilan['loss_details']['❌'] += 1
            del pending_predictions[game_number]
        return True
    except Exception as e:
        logger.error(f"Erreur update: {e}")
        return False

async def check_prediction_result(game_number: int, first_group: str):
    first_group = normalize_suits(first_group)
    logger.info(f"🔍 Vérification #{game_number}: {first_group}")
    for target_game, pred in list(pending_predictions.items()):
        if target_game == game_number and pred.get('rattrapage', 0) == 0:
            target_suit = pred['suit']
            rule = pred.get('rule', 'Règle 1')
            if has_suit_in_group(first_group, target_suit):
                await update_prediction_status(game_number, '✅0️⃣')
                logger.info(f"✅ GAGNÉ #{game_number}")
                return
            else:
                next_target = game_number + 1
                await send_prediction_to_channel(next_target, target_suit, pred['base_game'], 
                    forced=pred.get('forced', False), rattrapage=1, original_game=game_number, rule=rule)
                logger.info(f"❌ Échec #{game_number} → rattrapage #{next_target}")
                return
        elif target_game == game_number and pred.get('rattrapage', 0) > 0:
            original_game = pred.get('original_game')
            target_suit = pred['suit']
            rattrapage_actuel = pred['rattrapage']
            rule = pred.get('rule', 'Règle 1')
            if has_suit_in_group(first_group, target_suit):
                if original_game:
                    await update_prediction_status(original_game, f'✅{rattrapage_actuel}️⃣')
                if target_game in pending_predictions:
                    del pending_predictions[target_game]
                return
            else:
                if rattrapage_actuel < 2:
                    next_rattrapage = rattrapage_actuel + 1
                    next_target = game_number + 1
                    await send_prediction_to_channel(next_target, target_suit, pred['base_game'],
                        forced=pred.get('forced', False), rattrapage=next_rattrapage, 
                        original_game=original_game, rule=rule)
                else:
                    if original_game:
                        await update_prediction_status(original_game, '❌')
                if target_game in pending_predictions:
                    del pending_predictions[target_game]
                return

async def process_source_message(message_text: str, chat_id: int):
    global current_game_number, rule2_authorized_suit, rule1_is_waiting, rule1_pending_game
    if chat_id != SOURCE_CHANNEL_ID:
        return
    game_number = extract_game_number(message_text)
    if game_number is None:
        logger.debug("Pas de numéro")
        return
    current_game_number = game_number
    logger.info(f"📩 #{game_number} reçu")
    if is_in_pause:
        logger.info(f"⏸️ Pause - ignoré")
        return
    if rule1_is_waiting and rule1_pending_game == game_number + 1:
        logger.info(f"🚀 Règle 1 libérée")
        rule1_is_waiting = False
        rule1_pending_game = None
    if game_number % 2 == 0:
        logger.info(f"⏭️ Pair #{game_number} - ignoré")
        return
    target_even = game_number + 1
    if target_even > 1436:
        logger.info(f"⚠️ #{target_even} > 1436")
        return
    if target_even % 10 == 0:
        logger.info(f"⚠️ #{target_even} termine par 0")
        return
    if not await can_launch_prediction():
        return
    final_suit = None
    rule_used = ""
    if rule2_authorized_suit:
        final_suit = rule2_authorized_suit
        rule_used = "Règle 2"
        logger.info(f"🎯 RÈGLE 2 #{target_even}: {final_suit}")
    else:
        final_suit = get_rule1_suit(target_even)
        rule_used = "Règle 1"
        logger.info(f"🎯 RÈGLE 1 #{target_even}: {final_suit}")
    if final_suit:
        result = await send_prediction_to_channel(target_even, final_suit, game_number, rule=rule_used)
        if result:
            logger.info(f"✅ Prédiction lancée")
        else:
            logger.error(f"❌ Échec envoi")
    else:
        logger.info(f"❌ Pas de règle pour #{target_even}")

async def process_finalized_result(message_text: str, chat_id: int):
    try:
        if chat_id != SOURCE_CHANNEL_ID:
            return
        game_number = extract_game_number(message_text)
        if game_number is None:
            return
        groups = extract_parentheses_groups(message_text)
        if groups:
            logger.info(f"🔍 Finalisé #{game_number}")
            await check_prediction_result(game_number, groups[0])
    except Exception as e:
        logger.error(f"Erreur: {e}")

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond("🤖 Bot VIP\\nCommandes: /status, /setdiff, /force, /pause")

@client.on(events.NewMessage(pattern=r'^/setdiff (\d+)$'))
async def cmd_set_diff(event):
    global rule2_mirror_diff
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    try:
        new_diff = int(event.pattern_match.group(1))
        if new_diff < 2:
            await event.respond("❌ Minimum 2")
            return
        old_diff = rule2_mirror_diff
        rule2_mirror_diff = new_diff
        await event.respond(f"✅ Diff: {old_diff} → {new_diff}")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

@client.on(events.NewMessage(pattern='^/force$'))
async def cmd_force(event):
    global force_prediction_flag, is_in_pause, current_game_number
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        return
    if is_in_pause:
        force_prediction_flag = True
        is_in_pause = False
        await event.respond("🚀 Pause interrompue")
        return
    if current_game_number == 0:
        await event.respond("❌ Aucun numéro")
        return
    next_odd = current_game_number + 1 if current_game_number % 2 == 0 else current_game_number + 2
    target_even = next_odd + 1
    suit = rule2_authorized_suit or get_rule1_suit(target_even)
    if suit:
        await send_prediction_to_channel(target_even, suit, current_game_number, forced=True)
        await event.respond(f"🚀 Forcée: #{target_even}")
    else:
        await event.respond("❌ Impossible")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    status = (
        f"📊 **État**\\n\\n"
        f"🎮 Jeu: #{current_game_number}\\n"
        f"⏸️ Pause: {'Oui' if is_in_pause else 'Non'}\\n"
        f"📊 Série: {current_prediction_count}/4\\n"
        f"⚖️ Diff: {rule2_mirror_diff}\\n"
        f"🎯 Règle 2: {'Active' if rule2_is_active else 'Non'}\\n"
        f"📋 Canal: {PREDICTION_CHANNEL_ID}\\n"
    )
    if pending_predictions:
        status += f"\\n**🔮 Actives:**\\n"
        for game_num, pred in sorted(pending_predictions.items()):
            status += f"• #{game_num}: {pred['suit']} ({pred['status']})\\n"
    await event.respond(status)

@client.on(events.NewMessage())
async def handle_new_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        message_text = event.message.message
        logger.info(f"📨 Message de {chat_id}: {message_text[:50]}...")
        if chat_id == SOURCE_CHANNEL_ID:
            logger.info("📊 Canal Source 1")
            await process_source_message(message_text, chat_id)
            if is_message_finalized(message_text):
                await process_finalized_result(message_text, chat_id)
        elif chat_id == SOURCE_CHANNEL_2_ID:
            logger.info("📊 Canal Source 2")
            await process_stats_message(message_text)
        else:
            logger.info(f"⏭️ Ignoré: {chat_id}")
    except Exception as e:
        logger.error(f"Erreur: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id
        if hasattr(chat, 'broadcast') and chat.broadcast:
            if not str(chat_id).startswith('-100'):
                chat_id = int(f"-100{abs(chat_id)}")
        message_text = event.message.message
        if chat_id == SOURCE_CHANNEL_ID and is_message_finalized(message_text):
            await process_finalized_result(message_text, chat_id)
    except Exception as e:
        logger.error(f"Erreur: {e}")

async def start_bot():
    try:
        logger.info("🚀 Démarrage...")
        await client.connect()
        if not await client.is_user_authorized():
            await client.sign_in(bot_token=BOT_TOKEN)
        logger.info(f"✅ Connecté!")
        logger.info(f"📊 Source 1: {SOURCE_CHANNEL_ID}")
        logger.info(f"📊 Source 2: {SOURCE_CHANNEL_2_ID}")
        logger.info(f"🎯 Prédiction: {PREDICTION_CHANNEL_ID}")
        return True
    except Exception as e:
        logger.error(f"❌ Erreur: {e}")
        return False

async def main():
    if not await start_bot():
        return
    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())
