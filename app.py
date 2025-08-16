import os
import json
import uuid
import time
import zlib
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
MANIFEST_FILE = "hosted_songs_manifest.json"

# Jam Session Management
active_jams: Dict[str, Dict] = {}  # {jam_id: {host: {ws: WebSocket, name: str}, guests: List[{ws: WebSocket, name: str, join_time: str}], current_song: dict, playlist: List[dict], is_playing: bool, position: float, created_at: str}}

def load_songs():
    try:
        manifest_path = os.environ.get("SONG_MANIFEST", MANIFEST_FILE)
        
        with open(manifest_path, "r", encoding="utf-8") as f:
            songs = json.load(f)
        
        validated_songs = []
        for song in songs:
            if not isinstance(song, dict):
                continue
                
            if not song.get("id"):
                continue
            if not song.get("url"):
                continue
                
            song.setdefault("title", "Unknown Title")
            song.setdefault("artist", "Unknown Artist")
            song.setdefault("thumbnail", "https://placehold.co/128x128/CCCCCC/FFFFFF?text=MP3")
            song.setdefault("duration", 0)
            
            validated_songs.append(song)
        
        return validated_songs
    except FileNotFoundError:
        return {"error": f"Song manifest file not found: {MANIFEST_FILE}"}
    except json.JSONDecodeError:
        return {"error": f"Invalid JSON in {MANIFEST_FILE}"}
    except Exception as e:
        return {"error": str(e)}

async def send_compressed(websocket: WebSocket, data: dict):
    try:
        json_str = json.dumps(data)
        compressed = zlib.compress(json_str.encode('utf-8'))
        await websocket.send_bytes(compressed)
    except Exception as e:
        print(f"Error sending compressed data: {e}")

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=frontend_html, status_code=200)

@app.get("/get-songs")
async def get_songs():
    songs = load_songs()
    if isinstance(songs, dict) and "error" in songs:
        return JSONResponse(content=songs, status_code=404)
    return JSONResponse(content=songs, status_code=200)

@app.get("/get-jam-playlist/{jam_id}")
async def get_jam_playlist(jam_id: str):
    if jam_id not in active_jams:
        return JSONResponse({"error": "Jam session not found"}, status_code=404)
    
    jam_session = active_jams[jam_id]
    return JSONResponse({
        "current_song": jam_session["current_song"],
        "playlist": jam_session.get("playlist", []),
        "is_playing": jam_session["is_playing"],
        "position": jam_session["position"],
        "host": {"name": jam_session["host"]["name"]},
        "guests": [{"name": guest["name"], "join_time": guest["join_time"]} for guest in jam_session["guests"]],
        "created_at": jam_session["created_at"]
    })

@app.get("/load-audio")
async def load_audio(path: str):
    if path.startswith(("http://", "https://")):
        return JSONResponse({"url": path})
    
    try:
        path = Path(path)
        if path.exists():
            return FileResponse(path)
        return JSONResponse({"error": "File not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/create-jam")
async def create_jam(name: str = Query("Host", min_length=1, max_length=20)):
    jam_id = str(uuid.uuid4())[:8]
    active_jams[jam_id] = {
        "host": {"ws": None, "name": name},
        "guests": [],
        "current_song": None,
        "playlist": [],
        "is_playing": False,
        "position": 0,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_update_time": 0
    }
    return {
        "jam_id": jam_id,
        "host_name": name,
        "created_at": active_jams[jam_id]["created_at"]
    }

@app.websocket("/ws/jam/{jam_id}")
async def websocket_jam(
    websocket: WebSocket, 
    jam_id: str, 
    username: str = "Guest"
):
    await websocket.accept()
    
    if jam_id not in active_jams:
        await websocket.close(code=1008, reason="Jam session not found")
        return
    
    jam_session = active_jams[jam_id]
    is_host = False
    
    try:
        # Set as host or add as guest
        if jam_session["host"]["ws"] is None:
            # First connection becomes host
            jam_session["host"]["ws"] = websocket
            jam_session["host"]["name"] = username
            is_host = True
        else:
            # Add as guest
            guest = {
                "ws": websocket, 
                "name": username,
                "join_time": datetime.now().strftime("%H:%M:%S")
            }
            jam_session["guests"].append(guest)
        
        # Send initial sync data with compressed WebSocket
        await send_compressed(websocket, {
            "type": "initial_sync",
            "current_song": jam_session["current_song"],
            "playlist": jam_session["playlist"],
            "is_playing": jam_session["is_playing"],
            "position": jam_session["position"],
            "host": {"name": jam_session["host"]["name"]},
            "guests": [{"name": guest["name"], "join_time": guest["join_time"]} for guest in jam_session["guests"]],
            "session_created": jam_session["created_at"],
            "you_are_host": is_host
        })
        
        # Broadcast participant update
        await broadcast_participants_update(jam_id)
        
        # Main message loop
        while True:
            data = await websocket.receive_json()
            
            if data["type"] == "host_update":
                if websocket == jam_session["host"]["ws"]:
                    current_time = time.time()
                    # Throttle updates to prevent flooding
                    if current_time - jam_session["last_update_time"] < 0.1:  # 100ms throttle
                        continue
                    jam_session["last_update_time"] = current_time
                    
                    jam_session["is_playing"] = data["is_playing"]
                    jam_session["position"] = data["position"]
                    await broadcast_to_guests(jam_id, {
                        "type": "sync",
                        "is_playing": data["is_playing"],
                        "position": data["position"]
                    })
            
            elif data["type"] == "song_change":
                if websocket == jam_session["host"]["ws"]:
                    jam_session["current_song"] = data["song"]
                    jam_session["is_playing"] = True
                    jam_session["position"] = 0
                    await broadcast_to_guests(jam_id, {
                        "type": "song_change",
                        "song": data["song"],
                        "is_playing": True,
                        "position": 0
                    })
            
            elif data["type"] == "playlist_update":
                if websocket == jam_session["host"]["ws"]:
                    jam_session["playlist"] = data["playlist"]
                    await broadcast_to_guests(jam_id, {
                        "type": "playlist_update",
                        "playlist": data["playlist"]
                    })
            
            elif data["type"] == "chat_message":
                await broadcast_chat_message(jam_id, {
                    "sender": username,
                    "message": data["message"],
                    "timestamp": datetime.now().strftime("%H:%M")
                })
    
    except WebSocketDisconnect:
        if websocket == jam_session["host"]["ws"]:
            # Host disconnected - end session
            await broadcast_to_guests(jam_id, {
                "type": "jam_ended",
                "reason": f"Host {jam_session['host']['name']} left the session"
            })
            await close_all_guests(jam_id)
            del active_jams[jam_id]
        else:
            # Guest disconnected
            jam_session["guests"] = [
                guest for guest in jam_session["guests"] 
                if guest["ws"] != websocket
            ]
            await broadcast_participants_update(jam_id)

async def broadcast_to_guests(jam_id: str, message: dict):
    if jam_id not in active_jams:
        return
        
    for guest in active_jams[jam_id]["guests"]:
        try:
            await send_compressed(guest["ws"], message)
        except:
            continue

async def close_all_guests(jam_id: str):
    if jam_id not in active_jams:
        return
        
    for guest in active_jams[jam_id]["guests"]:
        try:
            await guest["ws"].close()
        except:
            continue

async def broadcast_participants_update(jam_id: str):
    if jam_id not in active_jams:
        return
        
    jam_session = active_jams[jam_id]
    update = {
        "type": "participants_update",
        "host": {"name": jam_session["host"]["name"]},
        "guests": [{"name": guest["name"], "join_time": guest["join_time"]} for guest in jam_session["guests"]]
    }
    
    # Send to host
    if jam_session["host"]["ws"]:
        try:
            await send_compressed(jam_session["host"]["ws"], update)
        except:
            pass
    
    # Send to all guests
    await broadcast_to_guests(jam_id, update)

async def broadcast_chat_message(jam_id: str, message: dict):
    if jam_id not in active_jams:
        return
        
    jam_session = active_jams[jam_id]
    chat_msg = {
        "type": "chat_message",
        "sender": message["sender"],
        "message": message["message"],
        "timestamp": message["timestamp"],
        "is_host": message["sender"] == jam_session["host"]["name"]
    }
    
    # Send to host
    if jam_session["host"]["ws"]:
        try:
            await send_compressed(jam_session["host"]["ws"], chat_msg)
        except:
            pass
    
    # Send to all guests
    await broadcast_to_guests(jam_id, chat_msg)


frontend_html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Audio Player</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pako/2.1.0/pako.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        body {
            font-family: 'Inter', sans-serif;
            background-color: #f0f2f5;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
            box-sizing: border-box;
            gap: 20px;
            flex-wrap: wrap;
        }
        audio {
            display: none;
        }
        input[type="range"] {
            -webkit-appearance: none;
            appearance: none;
            width: 100%;
            height: 8px;
            background: #d1d5db;
            outline: none;
            opacity: 0.7;
            transition: opacity .2s;
            border-radius: 5px;
        }
        input[type="range"]:hover {
            opacity: 1;
        }
        input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            appearance: none;
            width: 20px;
            height: 20px;
            background: #4F46E5;
            cursor: pointer;
            border-radius: 50%;
            box-shadow: 0 0 5px rgba(0, 0, 0, 0.2);
            margin-top: -6px;
        }
        .progress-bar-container input[type="range"] {
            background: linear-gradient(to right, #4F46E5 var(--progress, 0%), #d1d5db var(--progress, 0%));
        }
        .volume-bar-container input[type="range"] {
            background: linear-gradient(to right, #4F46E5 var(--volume, 100%), #d1d5db var(--volume, 100%));
        }
        .playlist-item.current-song {
            background-color: #e0e7ff;
            color: #4F46E5;
            font-weight: 600;
            box-shadow: 0 2px 5px rgba(0, 0, 0, 0.1);
        }
        .participant-badge {
            display: inline-flex;
            align-items: center;
            background-color: #e0e7ff;
            color: #4f46e5;
            padding: 0.25rem 0.5rem;
            border-radius: 9999px;
            font-size: 0.75rem;
            margin-right: 0.25rem;
            margin-bottom: 0.25rem;
        }
        .host-badge {
            background-color: #d1fae5;
            color: #065f46;
        }
        .chat-container {
            max-height: 200px;
            overflow-y: auto;
            border: 1px solid #e5e7eb;
            border-radius: 0.5rem;
            padding: 0.5rem;
            background-color: #f9fafb;
            margin-top: 1rem;
        }
        .chat-message {
            margin-bottom: 0.5rem;
            padding: 0.5rem;
            border-radius: 0.375rem;
            background-color: white;
            box-shadow: 0 1px 2px rgba(0,0,0,0.05);
        }
        .host-message {
            border-left: 3px solid #10b981;
        }
        .guest-message {
            border-left: 3px solid #3b82f6;
        }
        .message-sender {
            font-weight: 600;
            margin-right: 0.5rem;
        }
        .message-time {
            font-size: 0.75rem;
            color: #6b7280;
        }
        @media (max-width: 768px) {
            body {
                flex-direction: column;
                align-items: center;
                gap: 20px;
            }
        }
    </style>
</head>
<body class="bg-gradient-to-br from-indigo-50 to-purple-100 min-h-screen flex justify-center items-center p-4 relative">
    <div class="audio-player-card bg-white shadow-xl rounded-2xl p-6 md:p-8 w-full max-w-sm border border-gray-100">
        <h2 class="text-2xl md:text-3xl font-extrabold text-center text-gray-800 mb-6 tracking-tight">
            Music Player
        </h2>
        
        <!-- Jam Session UI -->
        <div id="jam-container" class="mb-6 p-4 bg-gray-50 rounded-lg border border-gray-200">
            <div class="flex items-center justify-between mb-3">
                <div class="flex items-center">
                    <span class="relative flex h-3 w-3 mr-2">
                        <span id="jam-status-indicator" class="animate-ping absolute inline-flex h-full w-full rounded-full bg-gray-400 opacity-75"></span>
                        <span id="jam-status-indicator-solid" class="relative inline-flex rounded-full h-3 w-3 bg-gray-500"></span>
                    </span>
                    <span id="jam-status-text" class="text-sm font-medium">Jam Mode: Off</span>
                </div>
                <button id="jam-toggle" class="px-3 py-1 bg-indigo-600 text-white text-xs rounded-md hover:bg-indigo-700 transition-colors duration-200">
                    Start Jam
                </button>
            </div>
            
            <div id="jam-host-controls" class="hidden">
                <div class="flex items-center space-x-2 mb-2">
                    <input type="text" id="jam-link-input" readonly class="flex-grow px-2 py-1 text-xs border border-gray-300 rounded-md bg-gray-100">
                    <button id="jam-copy-link" class="px-2 py-1 bg-gray-200 text-gray-700 text-xs rounded-md hover:bg-gray-300 transition-colors duration-200">
                        <i class="fas fa-copy"></i>
                    </button>
                </div>
                <p class="text-xs text-gray-500">Share this link to invite others</p>
            </div>
            
            <div id="jam-guest-info" class="hidden">
                <p class="text-xs text-gray-600">Connected to host's session</p>
            </div>
            
            <div id="participants-container" class="mt-3 hidden">
                <div class="text-xs text-gray-500 mb-1">Participants:</div>
                <div id="participants-list" class="flex flex-wrap"></div>
            </div>
            
            <div id="chat-section" class="mt-4 hidden">
                <div class="flex items-center mb-2">
                    <input type="text" id="chat-input" placeholder="Type a message..." 
                           class="flex-grow px-3 py-2 border border-gray-300 rounded-l-md focus:ring-indigo-500 focus:border-indigo-500 text-sm">
                    <button id="send-chat-button" class="px-3 py-2 bg-indigo-600 text-white rounded-r-md hover:bg-indigo-700">
                        <i class="fas fa-paper-plane"></i>
                    </button>
                </div>
                <div id="chat-container" class="chat-container"></div>
            </div>
        </div>

        <div id="album-art-container" class="w-24 h-24 md:w-32 md:h-32 mx-auto mb-6 bg-gray-200 rounded-xl overflow-hidden shadow-md flex items-center justify-center">
            <img id="album-art" src="https://placehold.co/128x128/4F46E5/FFFFFF?text=Album+Art"
                 alt="Album Art" class="w-full h-full object-cover">
        </div>

        <div class="text-center mb-6">
            <h3 id="track-title" class="text-lg md:text-xl font-bold text-gray-900 truncate">Song Title Goes Here</h3>
            <p id="artist-name" class="text-xs md:text-sm text-gray-600 truncate">Artist Name</p>
        </div>

        <audio id="audio-player"></audio>

        <div class="progress-bar-container w-full mb-4">
            <input type="range" id="progress-bar" value="0" min="0" max="100" class="w-full h-2 rounded-lg appearance-none cursor-pointer bg-gray-200">
            <div class="flex justify-between text-xs text-gray-600 mt-2">
                <span id="current-time">0:00</span>
                <span id="total-time">0:00</span>
            </div>
        </div>

        <div class="flex items-center justify-center space-x-4 mb-6">
            <button id="rewind-button" class="text-gray-700 hover:text-indigo-600 focus:outline-none transition-transform duration-200 ease-in-out active:scale-95">
                <i class="fas fa-backward"></i>
            </button>

            <button id="play-pause-button" class="w-14 h-14 md:w-16 md:h-16 bg-indigo-600 text-white rounded-full flex items-center justify-center shadow-lg hover:bg-indigo-700 focus:outline-none transition-all duration-300 ease-in-out">
                <i id="play-pause-icon" class="fas fa-play text-xl md:text-2xl"></i>
            </button>

            <button id="forward-button" class="text-gray-700 hover:text-indigo-600 focus:outline-none transition-transform duration-200 ease-in-out active:scale-95">
                <i class="fas fa-forward"></i>
            </button>

            <button id="next-button" class="text-gray-700 hover:text-indigo-600 focus:outline-none transition-transform duration-200 ease-in-out active:scale-95">
                <i class="fas fa-forward-step"></i>
            </button>
        </div>

        <div class="volume-bar-container flex items-center space-x-3 w-full">
            <i class="fas fa-volume-down text-gray-600 text-base"></i>
            <input type="range" id="volume-bar" value="100" min="0" max="100" class="flex-grow h-2 rounded-lg appearance-none cursor-pointer bg-gray-200">
            <i class="fas fa-volume-up text-gray-600 text-base"></i>
        </div>
        
        <div class="flex justify-center mt-6">
            <button id="play-random-hosted-songs-button" class="px-4 py-2 bg-purple-600 text-white rounded-lg shadow hover:bg-purple-700 transition-colors duration-200 text-sm">
                <i class="fas fa-random mr-2"></i>Play Random Songs
            </button>
        </div>
    </div>

    <div class="playlist-card bg-white shadow-xl rounded-2xl p-6 md:p-8 w-full max-w-sm border border-gray-100">
        <div class="flex justify-between items-center mb-6">
            <h2 class="text-2xl md:text-3xl font-extrabold text-gray-800 tracking-tight"> Playlist </h2>
        </div>
        <div class="flex justify-center space-x-4 mb-6 flex-wrap">
            <button id="show-add-options-button" class="px-4 py-2 bg-green-600 text-white rounded-lg shadow hover:bg-green-700 transition-colors duration-200 text-sm">
                <i class="fas fa-plus mr-2"></i>Add Songs
            </button>
            <button id="manage-playlist-button" class="px-4 py-2 bg-gray-200 text-gray-700 rounded-lg shadow hover:bg-gray-300 transition-colors duration-200 text-sm">
                <i class="fas fa-edit mr-2"></i>Manage Playlist
            </button>
        </div>
        <ul id="playlist-container" class="space-y-3 max-h-80 overflow-y-auto pr-2 mt-6 border-t border-gray-200 pt-6">
        </ul>
    </div>

    <!-- Hosted MP3 Search Modal -->
    <div id="hosted-mp3-search-modal" class="fixed top-0 left-0 w-full h-full bg-black bg-opacity-50 flex items-center justify-center hidden">
        <div class="bg-white rounded-lg p-6 w-full max-w-md">
            <button class="absolute top-4 right-4 text-gray-500 hover:text-gray-700" onclick="closeHostedMp3SearchModal()">×</button>
            <h3 class="text-2xl font-bold text-gray-800 mb-6 text-center">Add MP3 Songs</h3>
            <div class="mb-6 pb-4">
                <div class="flex space-x-2 mb-3">
                    <input type="text" id="hosted-mp3-search-input" placeholder="Search your songs by name/artist" class="flex-grow px-3 py-2 border border-gray-300 rounded-md focus:ring-indigo-500 focus:border-indigo-500">
                    <button id="hosted-mp3-search-button" class="px-4 py-2 bg-indigo-600 text-white rounded-md hover:bg-indigo-700 transition-colors duration-200">Search</button>
                </div>
                <div id="hosted-mp3-search-results" class="space-y-2 max-h-48 overflow-y-auto border border-gray-200 rounded-md p-2">
                    <p class="text-gray-500 text-center">Start typing to search for songs...</p>
                </div>
                <div class="mt-4 flex justify-end">
                    <button onclick="closeHostedMp3SearchModal()" class="px-4 py-2 bg-gray-300 text-gray-800 rounded-md hover:bg-gray-400">Done Adding</button>
                </div>
            </div>
        </div>
    </div>

    <script>
        // Get DOM elements
        const audioPlayer = document.getElementById('audio-player');
        const playPauseButton = document.getElementById('play-pause-button');
        const playPauseIcon = document.getElementById('play-pause-icon');
        const progressBar = document.getElementById('progress-bar');
        const currentTimeSpan = document.getElementById('current-time');
        const totalTimeSpan = document.getElementById('total-time');
        const volumeBar = document.getElementById('volume-bar');
        const trackTitle = document.getElementById('track-title');
        const artistName = document.getElementById('artist-name');
        const albumArt = document.getElementById('album-art');
        const playlistContainer = document.getElementById('playlist-container');
        const nextButton = document.getElementById('next-button');
        const rewindButton = document.getElementById('rewind-button');
        const forwardButton = document.getElementById('forward-button');
        const playRandomHostedSongsButton = document.getElementById('play-random-hosted-songs-button');
        const showAddOptionsButton = document.getElementById('show-add-options-button');
        const managePlaylistButton = document.getElementById('manage-playlist-button');
        const hostedMp3SearchModal = document.getElementById('hosted-mp3-search-modal');
        const hostedMp3SearchInput = document.getElementById('hosted-mp3-search-input');
        const hostedMp3SearchResults = document.getElementById('hosted-mp3-search-results');
        const hostedMp3SearchButton = document.getElementById('hosted-mp3-search-button');
        
        // Jam Session elements
        const jamContainer = document.getElementById('jam-container');
        const jamToggle = document.getElementById('jam-toggle');
        const jamStatusText = document.getElementById('jam-status-text');
        const jamStatusIndicator = document.getElementById('jam-status-indicator');
        const jamStatusIndicatorSolid = document.getElementById('jam-status-indicator-solid');
        const jamHostControls = document.getElementById('jam-host-controls');
        const jamLinkInput = document.getElementById('jam-link-input');
        const jamCopyLink = document.getElementById('jam-copy-link');
        const jamGuestInfo = document.getElementById('jam-guest-info');
        const participantsContainer = document.getElementById('participants-container');
        const participantsList = document.getElementById('participants-list');
        
        // Chat elements
        const chatSection = document.getElementById('chat-section');
        const chatInput = document.getElementById('chat-input');
        const sendChatButton = document.getElementById('send-chat-button');
        const chatContainer = document.getElementById('chat-container');

        let currentPlaylist = [];
        let currentSongIndex = -1;
        let isPlaying = false;
        let hostedSongs = [];
        let jamSocket = null;
        let isHost = false;
        let jamId = null;
        let lastSyncTime = 0;
        let syncInterval;
        let username = "Guest";

        // --- Audio Player Logic ---
        function playSong(song, seekTime = 0) {
            if (!song || !song.url) {
                console.warn("Attempted to play null or invalid song object.");
                return;
            }
            
            audioPlayer.src = song.url;
            audioPlayer.currentSong = song;
            trackTitle.textContent = song.title;
            artistName.textContent = song.artist || 'Unknown Artist';
            albumArt.src = song.thumbnail || "https://placehold.co/128x128/4F46E5/FFFFFF?text=Album+Art";

            audioPlayer.load();
            audioPlayer.onloadedmetadata = () => {
                if (seekTime > 0 && seekTime < audioPlayer.duration) {
                    audioPlayer.currentTime = seekTime;
                }
                
                if (isHost && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                    // Broadcast song change to guests
                    jamSocket.send(JSON.stringify({
                        type: "song_change",
                        song: song,
                        is_playing: true,
                        position: seekTime
                    }));
                }
                
                if (!isHost) {
                    // Guests wait for host's play command
                    return;
                }
                
                audioPlayer.play().catch(error => {
                    console.error("Error playing audio:", error);
                    playPauseIcon.classList.remove('fa-pause');
                    playPauseIcon.classList.add('fa-play');
                    isPlaying = false;
                });
                updateProgressBar();
                updateTotalTime();
            };

            playPauseIcon.classList.remove('fa-play');
            playPauseIcon.classList.add('fa-pause');
            isPlaying = true;

            // Highlight the current song in the playlist
            document.querySelectorAll('.playlist-item').forEach(item => {
                item.classList.remove('current-song');
            });
            const currentPlaylistItem = document.querySelector(`[data-song-id="${song.id}"]`);
            if (currentPlaylistItem) {
                currentPlaylistItem.classList.add('current-song');
                currentPlaylistItem.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
            }
        }

        function pauseSong() {
            audioPlayer.pause();
            playPauseIcon.classList.remove('fa-pause');
            playPauseIcon.classList.add('fa-play');
            isPlaying = false;
            
            if (isHost && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({
                    type: "host_update",
                    is_playing: false,
                    position: audioPlayer.currentTime
                }));
            }
        }

        function togglePlayPause() {
            if (audioPlayer.currentSong) {
                if (isPlaying) {
                    pauseSong();
                } else {
                    audioPlayer.play().catch(error => {
                        console.error("Error resuming playback:", error);
                        playPauseIcon.classList.remove('fa-pause');
                        playPauseIcon.classList.add('fa-play');
                        isPlaying = false;
                    });
                    playPauseIcon.classList.remove('fa-play');
                    playPauseIcon.classList.add('fa-pause');
                    isPlaying = true;
                    
                    if (isHost && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                        jamSocket.send(JSON.stringify({
                            type: "host_update",
                            is_playing: true,
                            position: audioPlayer.currentTime
                        }));
                    }
                }
            }
        }

        function updateProgressBar() {
            progressBar.value = (audioPlayer.currentTime / audioPlayer.duration) * 100 || 0;
            progressBar.style.setProperty('--progress', `${progressBar.value}%`);
            currentTimeSpan.textContent = formatTime(audioPlayer.currentTime);
            
            // Sync with host every 5 seconds if guest
            if (!isHost && jamSocket && jamSocket.readyState === WebSocket.OPEN && Date.now() - lastSyncTime > 5000) {
                lastSyncTime = Date.now();
                jamSocket.send(JSON.stringify({
                    type: "sync_request"
                }));
            }
        }

        function updateTotalTime() {
            totalTimeSpan.textContent = formatTime(audioPlayer.duration);
        }

        function formatTime(seconds) {
            const minutes = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return `${minutes}:${secs < 10 ? '0' : ''}${secs}`;
        }

        // --- Chat Functions ---
        function addChatMessage(sender, message, timestamp, isHost) {
            const messageDiv = document.createElement('div');
            messageDiv.className = `chat-message ${isHost ? 'host-message' : 'guest-message'}`;
            messageDiv.innerHTML = `
                <div class="flex justify-between items-baseline">
                    <span class="message-sender">${sender}</span>
                    <span class="message-time">${timestamp}</span>
                </div>
                <div class="message-text">${message}</div>
            `;
            chatContainer.appendChild(messageDiv);
            chatContainer.scrollTop = chatContainer.scrollHeight;
        }

        // --- Participant Management ---
        function updateParticipantsDisplay(host, guests) {
            participantsList.innerHTML = '';
            
            // Add host
            const hostBadge = document.createElement('span');
            hostBadge.className = 'participant-badge host-badge';
            hostBadge.innerHTML = `<i class="fas fa-crown mr-1"></i>${host.name}`;
            participantsList.appendChild(hostBadge);
            
            // Add guests
            guests.forEach(guest => {
                const guestBadge = document.createElement('span');
                guestBadge.className = 'participant-badge';
                guestBadge.textContent = guest.name;
                participantsList.appendChild(guestBadge);
            });
        }

        // --- Jam Session Functions ---
        async function startJamSession() {
            username = prompt("Enter your name to host the jam session:", "Host") || "Host";
            if (!username) return;
            
            try {
                const response = await fetch(`/create-jam?name=${encodeURIComponent(username)}`);
                if (!response.ok) throw new Error('Failed to create jam session');
                const data = await response.json();
                jamId = data.jam_id;
                
                // Connect as host
                connectWebSocket(true);
                
                // Update UI
                jamToggle.textContent = 'End Jam';
                jamStatusText.textContent = 'Jam Mode: Hosting';
                jamStatusIndicator.classList.remove('bg-gray-400');
                jamStatusIndicator.classList.add('bg-green-400');
                jamStatusIndicatorSolid.classList.remove('bg-gray-500');
                jamStatusIndicatorSolid.classList.add('bg-green-500');
                
                // Show host controls
                jamHostControls.classList.remove('hidden');
                participantsContainer.classList.remove('hidden');
                chatSection.classList.remove('hidden');
                jamLinkInput.value = `${window.location.origin}/?jam=${jamId}`;
                
                isHost = true;
                
                // Start sync interval
                syncInterval = setInterval(() => {
                    if (isPlaying && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                        jamSocket.send(JSON.stringify({
                            type: "host_update",
                            is_playing: true,
                            position: audioPlayer.currentTime
                        }));
                    }
                }, 5000);
                
            } catch (error) {
                console.error("Error starting jam session:", error);
                alert("Failed to start jam session");
            }
        }

        function endJamSession() {
            if (jamSocket) {
                jamSocket.close();
                jamSocket = null;
            }
            
            clearInterval(syncInterval);
            
            // Update UI
            jamToggle.textContent = 'Start Jam';
            jamStatusText.textContent = 'Jam Mode: Off';
            jamStatusIndicator.classList.remove('bg-green-400', 'bg-blue-400');
            jamStatusIndicator.classList.add('bg-gray-400');
            jamStatusIndicatorSolid.classList.remove('bg-green-500', 'bg-blue-500');
            jamStatusIndicatorSolid.classList.add('bg-gray-500');
            
            jamHostControls.classList.add('hidden');
            jamGuestInfo.classList.add('hidden');
            participantsContainer.classList.add('hidden');
            chatSection.classList.add('hidden');
            
            isHost = false;
            jamId = null;
        }

        function joinJamSession(jamIdToJoin) {
            username = prompt("Enter your name to join the jam session:", "Guest") || "Guest";
            if (!username) return;
            
            jamId = jamIdToJoin;
            connectWebSocket(false);
            
            // Update UI
            jamToggle.textContent = 'Leave Jam';
            jamStatusText.textContent = 'Jam Mode: Connected';
            jamStatusIndicator.classList.remove('bg-gray-400');
            jamStatusIndicator.classList.add('bg-blue-400');
            jamStatusIndicatorSolid.classList.remove('bg-gray-500');
            jamStatusIndicatorSolid.classList.add('bg-blue-500');
            
            jamGuestInfo.classList.remove('hidden');
            participantsContainer.classList.remove('hidden');
            chatSection.classList.remove('hidden');
        }

        function connectWebSocket(asHost) {
            const protocol = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
            const wsUrl = `${protocol}${window.location.host}/ws/jam/${jamId}?username=${encodeURIComponent(username)}`;
            
            jamSocket = new WebSocket(wsUrl);
            jamSocket.binaryType = "arraybuffer"; // <-- Add this line

            jamSocket.onopen = () => {
                console.log("WebSocket connected as", username);
                if (asHost) {
                    // Send initial state if we have a current song
                    if (audioPlayer.currentSong) {
                        jamSocket.send(JSON.stringify({
                            type: "host_init",
                            song: audioPlayer.currentSong,
                            playlist: currentPlaylist,
                            is_playing: isPlaying,
                            position: audioPlayer.currentTime
                        }));
                    }
                } else {
                    // Request initial sync as guest
                    fetch(`/get-jam-playlist/${jamId}`)
                        .then(response => response.json())
                        .then(data => {
                            if (data.error) {
                                console.error(data.error);
                                return;
                            }
                            
                            // Update participants display
                            updateParticipantsDisplay(data.host, data.guests);
                            
                            // Update playlist
                            if (data.playlist && data.playlist.length > 0) {
                                currentPlaylist = data.playlist;
                                renderPlaylist();
                            }
                            
                            // Sync current song
                            if (data.current_song) {
                                playSong(data.current_song, data.position);
                                if (data.is_playing) {
                                    audioPlayer.play().catch(console.error);
                                }
                            }
                        })
                        .catch(console.error);
                }
            };
            
            jamSocket.onmessage = async (event) => {
                let data;
                try {
                    if (event.data instanceof ArrayBuffer || event.data instanceof Blob) {
                        // Handle compressed binary data
                        let arrayBuffer;
                        if (event.data instanceof Blob) {
                            arrayBuffer = await event.data.arrayBuffer();
                        } else {
                            arrayBuffer = event.data;
                        }
                        const decompressed = new TextDecoder().decode(pako.inflate(new Uint8Array(arrayBuffer)));
                        data = JSON.parse(decompressed);
                    } else if (typeof event.data === "string") {
                        // Handle plain JSON string
                        data = JSON.parse(event.data);
                    } else {
                        console.warn("Unknown WebSocket message type", event.data);
                        return;
                    }

                    if (data.type === "sync") {
                        // Sync with host's playback
                        if (!isHost) {
                            if (data.song && (!audioPlayer.currentSong || data.song.id !== audioPlayer.currentSong.id)) {
                                playSong(data.song, data.position);
                            }
                            
                            if (data.is_playing !== isPlaying) {
                                if (data.is_playing) {
                                    audioPlayer.play().catch(console.error);
                                    playPauseIcon.classList.remove('fa-play');
                                    playPauseIcon.classList.add('fa-pause');
                                    isPlaying = true;
                                } else {
                                    audioPlayer.pause();
                                    playPauseIcon.classList.remove('fa-pause');
                                    playPauseIcon.classList.add('fa-play');
                                    isPlaying = false;
                                }
                            }
                            
                            if (Math.abs(audioPlayer.currentTime - data.position) > 1) {
                                audioPlayer.currentTime = data.position;
                            }
                        }
                    }
                    else if (data.type === "song_change") {
                        if (!isHost) {
                            playSong(data.song, data.position);
                        }
                    }
                    else if (data.type === "playlist_update") {
                        if (!isHost && data.playlist) {
                            currentPlaylist = data.playlist;
                            renderPlaylist();
                        }
                    }
                    else if (data.type === "participants_update") {
                        updateParticipantsDisplay(data.host, data.guests);
                    }
                    else if (data.type === "chat_message") {
                        addChatMessage(
                            data.sender, 
                            data.message, 
                            data.timestamp,
                            data.is_host
                        );
                    }
                    else if (data.type === "jam_ended") {
                        alert(data.reason || "Host has ended the jam session");
                        endJamSession();
                    }
                } catch (error) {
                    console.error("Error processing WebSocket message:", error);
                }
            };
            
            jamSocket.onclose = () => {
                console.log("WebSocket disconnected");
                if (isHost) {
                    endJamSession();
                }
            };
            
            jamSocket.onerror = (error) => {
                console.error("WebSocket error:", error);
                endJamSession();
            };
        }

        function syncPlaylistWithGuests() {
            if (isHost && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({
                    type: "playlist_update",
                    playlist: currentPlaylist
                }));
            }
        }

        // --- Playlist Management ---
        function renderPlaylist() {
            playlistContainer.innerHTML = '';
            if (currentPlaylist.length === 0) {
                playlistContainer.innerHTML = '<p class="text-gray-500 text-center py-4">Playlist is empty. Add some songs!</p>';
                resetPlayerUI();
                return;
            }
            
            currentPlaylist.forEach((song, index) => {
                const listItem = document.createElement('li');
                listItem.className = `playlist-item flex items-center justify-between p-3 rounded-lg shadow-sm mb-2 cursor-pointer transition-all duration-200 ease-in-out
                                     ${index === currentSongIndex ? 'current-song' : 'bg-gray-50 hover:bg-gray-100'}`;
                listItem.dataset.songId = song.id;

                listItem.innerHTML = `
                    <div class="flex items-center flex-grow min-w-0">
                        <img src="${song.thumbnail || 'https://placehold.co/40x40/CCCCCC/FFFFFF?text=MP3'}" alt="Thumbnail" class="w-10 h-10 rounded-md mr-3 object-cover">
                        <div class="min-w-0 flex-grow">
                            <p class="font-medium text-sm truncate">${song.title}</p>
                            <p class="text-xs text-gray-500 truncate">${song.artist || 'Unknown Artist'}</p>
                        </div>
                    </div>
                    <button class="remove-song-button text-gray-400 hover:text-red-600 ml-3 focus:outline-none" data-song-id="${song.id}">
                        <i class="fas fa-times"></i>
                    </button>
                `;
                playlistContainer.appendChild(listItem);

                listItem.addEventListener('click', (event) => {
                    if (event.target.closest('.remove-song-button')) {
                        return;
                    }
                    if (currentSongIndex !== index) {
                        currentSongIndex = index;
                        playSong(currentPlaylist[currentSongIndex]);
                        renderPlaylist();
                    } else if (!isPlaying) {
                        togglePlayPause();
                    }
                });
            });

            document.querySelectorAll('.remove-song-button').forEach(button => {
                button.addEventListener('click', (event) => {
                    event.stopPropagation();
                    const songIdToRemove = event.currentTarget.dataset.songId;
                    removeSongFromPlaylist(songIdToRemove);
                });
            });
        }

        function addSongToPlaylist(song) {
            currentPlaylist.push(song);
            renderPlaylist();
            syncPlaylistWithGuests();
        }

        function removeSongFromPlaylist(songId) {
            const songIndexToRemove = currentPlaylist.findIndex(song => song.id === songId);
            if (songIndexToRemove === -1) return;

            currentPlaylist.splice(songIndexToRemove, 1);
            
            if (currentSongIndex === songIndexToRemove) {
                pauseSong();
                if (currentPlaylist.length > 0) {
                    currentSongIndex = Math.min(currentSongIndex, currentPlaylist.length - 1);
                    playSong(currentPlaylist[currentSongIndex]);
                } else {
                    currentSongIndex = -1;
                    resetPlayerUI();
                }
            } else if (currentSongIndex > songIndexToRemove) {
                currentSongIndex--;
            }

            renderPlaylist();
            syncPlaylistWithGuests();
        }

        function playNextSong() {
            if (currentPlaylist.length === 0) {
                resetPlayerUI();
                return;
            }
            currentSongIndex = (currentSongIndex + 1) % currentPlaylist.length;
            playSong(currentPlaylist[currentSongIndex]);
        }

        function resetPlayerUI() {
            pauseSong();
            audioPlayer.src = '';
            audioPlayer.currentSong = null;
            progressBar.value = 0;
            progressBar.style.setProperty('--progress', '0%');
            currentTimeSpan.textContent = '0:00';
            totalTimeSpan.textContent = '0:00';
            trackTitle.textContent = "No song loaded";
            artistName.textContent = "";
            albumArt.src = "https://placehold.co/128x128/CCCCCC/FFFFFF?text=No+Track";
        }

        // --- Hosted MP3 Search Functionality ---
        async function fetchHostedSongs() {
            try {
                const response = await fetch('/get-songs');
                if (!response.ok) throw new Error('Failed to load songs');
                hostedSongs = await response.json();
            } catch (error) {
                console.error("Error fetching hosted songs:", error);
                hostedSongs = [];
            }
        }

        function renderHostedSearchResults(songs) {
            hostedMp3SearchResults.innerHTML = '';
            if (songs.length === 0) {
                hostedMp3SearchResults.innerHTML = '<p class="text-gray-500 text-center py-2">No songs found matching your search.</p>';
                return;
            }
            
            songs.forEach(song => {
                const songItem = document.createElement('div');
                songItem.className = 'flex items-center justify-between p-2 bg-gray-100 rounded-md mb-2';
                songItem.innerHTML = `
                    <div class="flex items-center min-w-0 flex-grow">
                        <img src="${song.thumbnail || 'https://placehold.co/40x40/CCCCCC/FFFFFF?text=MP3'}" alt="Thumbnail" class="w-8 h-8 rounded-md mr-2 object-cover">
                        <div class="min-w-0 flex-grow">
                            <p class="text-sm font-medium truncate">${song.title}</p>
                            <p class="text-xs text-gray-500 truncate">${song.artist || 'Unknown Artist'}</p>
                        </div>
                    </div>
                    <button class="add-to-playlist-button ml-3 px-3 py-1 bg-indigo-500 text-white text-xs rounded-md hover:bg-indigo-600 transition-colors duration-200"
                            data-song-id="${song.id}">Add</button>
                `;
                hostedMp3SearchResults.appendChild(songItem);
            });

            document.querySelectorAll('.add-to-playlist-button').forEach(button => {
                button.addEventListener('click', (event) => {
                    const songId = event.target.dataset.songId;
                    const songToAdd = hostedSongs.find(s => s.id === songId);
                    if (songToAdd) {
                        addSongToPlaylist(songToAdd);
                        event.target.textContent = 'Added';
                        event.target.disabled = true;
                        event.target.classList.remove('bg-indigo-500', 'hover:bg-indigo-600');
                        event.target.classList.add('bg-gray-400', 'cursor-not-allowed');
                    }
                });
            });
        }

        function openHostedMp3SearchModal() {
            hostedMp3SearchModal.classList.remove('hidden');
            hostedMp3SearchInput.value = '';
            hostedMp3SearchResults.innerHTML = '<p class="text-gray-500 text-center">Start typing to search for songs...</p>';
            if (hostedSongs.length === 0) {
                fetchHostedSongs();
            }
        }

        function closeHostedMp3SearchModal() {
            hostedMp3SearchModal.classList.add('hidden');
        }

        // --- Event Listeners ---
        playPauseButton.addEventListener('click', togglePlayPause);
        audioPlayer.addEventListener('timeupdate', updateProgressBar);
        audioPlayer.addEventListener('ended', playNextSong);
        audioPlayer.addEventListener('volumechange', () => {
            volumeBar.style.setProperty('--volume', `${audioPlayer.volume * 100}%`);
        });

        progressBar.addEventListener('input', () => {
            const seekTime = (progressBar.value / 100) * audioPlayer.duration;
            audioPlayer.currentTime = seekTime;
            
            if (isHost && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({
                    type: "host_update",
                    is_playing: isPlaying,
                    position: audioPlayer.currentTime
                }));
            }
        });

        volumeBar.addEventListener('input', (e) => {
            audioPlayer.volume = e.target.value / 100;
        });

        nextButton.addEventListener('click', playNextSong);

        rewindButton.addEventListener('click', () => {
            audioPlayer.currentTime = Math.max(0, audioPlayer.currentTime - 10);
            
            if (isHost && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({
                    type: "host_update",
                    is_playing: isPlaying,
                    position: audioPlayer.currentTime
                }));
            }
        });

        forwardButton.addEventListener('click', () => {
            audioPlayer.currentTime = Math.min(audioPlayer.duration, audioPlayer.currentTime + 10);
            
            if (isHost && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({
                    type: "host_update",
                    is_playing: isPlaying,
                    position: audioPlayer.currentTime
                }));
            }
        });

        playRandomHostedSongsButton.addEventListener('click', () => {
            if (hostedSongs.length > 0) {
                currentPlaylist = [];
                const shuffled = [...hostedSongs].sort(() => 0.5 - Math.random());
                const randomSongs = shuffled.slice(0, Math.min(5, hostedSongs.length));
                currentPlaylist.push(...randomSongs);
                currentSongIndex = 0;
                renderPlaylist();
                playSong(currentPlaylist[currentSongIndex]);
            } else {
                alert("No hosted songs available to play randomly. Try adding some.");
            }
        });

        showAddOptionsButton.addEventListener('click', openHostedMp3SearchModal);

        managePlaylistButton.addEventListener('click', () => {
            alert("Manage Playlist functionality coming soon!");
        });

        hostedMp3SearchButton.addEventListener('click', () => {
            const searchTerm = hostedMp3SearchInput.value.toLowerCase();
            const filteredSongs = hostedSongs.filter(song =>
                song.title.toLowerCase().includes(searchTerm) ||
                (song.artist && song.artist.toLowerCase().includes(searchTerm))
            );
            renderHostedSearchResults(filteredSongs);
        });

        hostedMp3SearchInput.addEventListener('input', () => {
            const searchTerm = hostedMp3SearchInput.value.toLowerCase();
            const filteredSongs = hostedSongs.filter(song =>
                song.title.toLowerCase().includes(searchTerm) ||
                (song.artist && song.artist.toLowerCase().includes(searchTerm))
            );
            renderHostedSearchResults(filteredSongs);
        });

        // Chat event listener
        sendChatButton.addEventListener('click', () => {
            const message = chatInput.value.trim();
            if (message && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({
                    type: "chat_message",
                    message: message
                }));
                chatInput.value = '';
            }
        });

        chatInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                sendChatButton.click();
            }
        });

        // Jam Session Toggle
        jamToggle.addEventListener('click', () => {
            if (jamId) {
                endJamSession();
            } else {
                startJamSession();
            }
        });

        // Copy Jam Link
        jamCopyLink.addEventListener('click', () => {
            jamLinkInput.select();
            document.execCommand('copy');
            jamCopyLink.innerHTML = '<i class="fas fa-check"></i>';
            setTimeout(() => {
                jamCopyLink.innerHTML = '<i class="fas fa-copy"></i>';
            }, 2000);
        });

        // Initialize
        document.addEventListener('DOMContentLoaded', () => {
            fetchHostedSongs();
            resetPlayerUI();
            renderPlaylist();
            
            // Check for jam ID in URL
            const urlParams = new URLSearchParams(window.location.search);
            const jamParam = urlParams.get('jam');
            
            if (jamParam) {
                joinJamSession(jamParam);
            }
        });
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)