"""
Configuration du bot Telegram de prédiction Baccarat
"""
import os

def parse_channel_id(env_var: str, default: str) -> int:
    value = os.getenv(env_var) or default
    channel_id = int(value)
    # Convertit l'ID positif en format ID de canal Telegram négatif si nécessaire
    if channel_id > 0 and len(str(channel_id)) >= 10:
        channel_id = -channel_id
    return channel_id

# ID du canal source (inchangé)
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')

# ID du canal source 2 (Statistiques)
SOURCE_CHANNEL_2_ID = parse_channel_id('SOURCE_CHANNEL_2_ID', '-1002674389383')

# NOUVEL ID DU CANAL DE PRÉDICTION
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1002935867322')

ADMIN_ID = int(os.getenv('ADMIN_ID') or '0')

API_ID = int(os.getenv('API_ID') or '29177661')
API_HASH = os.getenv('API_HASH') or 'a8639172fa8d35dbfd8ea46286d349ab'
BOT_TOKEN = os.getenv('BOT_TOKEN') or '7573497633:AAHk9K15yTCiJP-zruJrc9v8eK8I9XhjyH4'
ADMIN_ID=1190237801
PORT = int(os.getenv('PORT') or '10000')  # Port 10000 for Render.com

# NOUVEAU MAPPING : Miroirs selon les instructions utilisateur
SUIT_MAPPING = {
    '♦': '♠',  # Miroir Carreau <-> Pique
    '♠': '♦',
    '♥': '♣',  # Miroir Cœur <-> Trèfle
    '♣': '♥',
}

ALL_SUITS = ['♠', '♥', '♦', '♣']
SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}
