import os
import json
import uuid
import time
import zlib
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yt_dlp
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# ----------------------------
# Logging & App setup
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("uvicorn.error")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    asyncio.create_task(cleanup_inactive_sessions())
    yield
    # Shutdown would go here

# Create app WITH lifespan
app = FastAPI(title="Synq Music Player", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Config / State
# ----------------------------
MANIFEST_FILE = "hosted_songs_manifest.json"
active_jams: Dict[str, Dict] = {}  # in-memory jam sessions

# Improved YouTube DL options for better audio stability
YDL_OPTS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    # Use these options for better audio stability
    'extract_flat': False,
    'nocheckcertificate': True,
    'ignoreerrors': True,  # Changed to True to continue on errors
    'logtostderr': False,
    'prefer_ffmpeg': True,
    'geo_bypass': True,
    'geo_bypass_country': 'US',
    # Audio format options for better compatibility
    'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '192',
    }],
}

# ----------------------------
# Utilities
# ----------------------------
def load_songs():
    try:
        manifest_path = os.environ.get("SONG_MANIFEST", MANIFEST_FILE)
        with open(manifest_path, "r", encoding="utf-8") as f:
            songs = json.load(f)
        validated = []
        for s in songs:
            if not isinstance(s, dict):
                continue
            if not s.get("id") or not s.get("url"):
                continue
            s.setdefault("title", "Unknown Title")
            s.setdefault("artist", "Unknown Artist")
            s.setdefault("thumbnail", "https://placehold.co/128x128/CCCCCC/FFFFFF?text=MP3")
            s.setdefault("duration", 0)
            validated.append(s)
        return validated
    except FileNotFoundError:
        logger.warning(f"Manifest file {MANIFEST_FILE} not found")
        return []
    except Exception as e:
        logger.exception("Failed to load songs manifest")
        return []

async def send_compressed(ws: WebSocket, data: dict):
    """Send compressed JSON (clients use pako to decompress)."""
    try:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        compressed = zlib.compress(payload)
        await ws.send_bytes(compressed)
    except Exception:
        # best-effort, swallow errors (caller may prune socket)
        logger.debug("send_compressed failed", exc_info=True)

def validate_username(name: str) -> bool:
    if not name or len(name) < 3 or len(name) > 20:
        return False
    return all(c.isalnum() or c in (" ", "-", "_") for c in name)

def validate_message(msg: str) -> bool:
    if not msg or len(msg.strip()) == 0:
        return False
    return len(msg) <= 500

# ----------------------------
# Routes
# ----------------------------
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # return 204 so browser stops requesting or showing errors
    return Response(status_code=204)

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=frontend_html, status_code=200)

@app.get("/get-songs")
async def get_songs():
    songs = load_songs()
    return JSONResponse(content=songs, status_code=200)

@app.get("/get-jam-playlist/{jam_id}")
async def get_jam_playlist(jam_id: str):
    jam = active_jams.get(jam_id)
    if not jam:
        return JSONResponse({"error": "Jam not found"}, status_code=404)
    return JSONResponse({
        "current_song": jam.get("current_song"),
        "playlist": jam.get("playlist", []),
        "is_playing": jam.get("is_playing", False),
        "position": jam.get("position", 0.0),
        "volume": jam.get("volume", 1.0),
        "host": {"name": jam.get("host", {}).get("name")},
        "guests": [{"name": g["name"], "join_time": g["join_time"]} for g in jam.get("guests", [])],
        "created_at": jam.get("created_at")
    })

@app.get("/load-audio")
async def load_audio(path: str):
    if path.startswith(("http://", "https://")):
        return JSONResponse({"url": path})
    
    # Prevent directory traversal
    if ".." in path or path.startswith("/"):
        raise HTTPException(status_code=400, detail="Invalid path")
    
    p = Path(path)
    if p.exists() and p.is_file():
        return FileResponse(p)
    return JSONResponse({"error": "File not found"}, status_code=404)

@app.get("/create-jam")
async def create_jam(name: str = Query("Host", min_length=1, max_length=20)):
    if not validate_username(name):
        return JSONResponse({"error": "Invalid username"}, status_code=400)
    jam_id = str(uuid.uuid4())[:8]
    active_jams[jam_id] = {
        "host": {"ws": None, "name": name},
        "guests": [],
        "current_song": None,
        "playlist": [],
        "is_playing": False,
        "position": 0.0,
        "volume": 1.0,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_update_time": 0.0,
        "last_heartbeat": time.time()
    }
    return {"jam_id": jam_id, "host_name": name, "created_at": active_jams[jam_id]["created_at"]}

@app.get("/youtube/search")
async def youtube_search(query: str = Query(..., min_length=1)):
    """Search YouTube for videos"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': True,
            'skip_download': True,
            'default_search': 'ytsearch',
            'ignoreerrors': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch10:{query}", download=False)
            if not info or 'entries' not in info:
                return JSONResponse({"results": []})
            results = []
            for entry in info['entries']:
                if not entry:
                    continue
                results.append({
                    "id": entry.get('id'),
                    "title": entry.get('title', 'Unknown Title'),
                    "duration": entry.get('duration', 0),
                    "thumbnail": entry.get('thumbnail'),
                    "artist": entry.get('uploader', 'Unknown Artist'),
                    "source": "youtube"
                })
            return JSONResponse({"results": results})
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        return JSONResponse({"error": "Search failed"}, status_code=500)

@app.get("/youtube/stream/{video_id}")
async def youtube_stream(video_id: str):
    """Get audio-only streaming URL for YouTube video"""
    try:
        # Use different format selection for better stability
        ydl_opts_alt = {
            'format': 'bestaudio[ext=m4a]/bestaudio/best',
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'source_address': '0.0.0.0',
            'nocheckcertificate': True,
            'ignoreerrors': True,  # Changed to True to continue on errors
            'logtostderr': False,
            'prefer_ffmpeg': True,
            'geo_bypass': True,
            'geo_bypass_country': 'US',
            'extract_flat': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts_alt) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", 
                download=False
            )
            
            if not info:
                raise HTTPException(status_code=404, detail="Video not found")
            
            # Get the best audio URL - try multiple approaches
            audio_url = None
            
            # First try: Direct URL from info
            if 'url' in info:
                audio_url = info['url']
            
            # Second try: Find the best audio format
            if not audio_url and 'formats' in info:
                # Prefer m4a format for better stability
                audio_formats = [f for f in info['formats'] 
                               if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
                
                # Sort by quality/bitrate
                audio_formats.sort(key=lambda x: x.get('abr', 0) or 0, reverse=True)
                
                if audio_formats:
                    audio_url = audio_formats[0]['url']
            
            # Third try: Fallback to any format with audio
            if not audio_url and 'formats' in info:
                audio_formats = [f for f in info['formats'] if f.get('acodec') != 'none']
                if audio_formats:
                    audio_formats.sort(key=lambda x: x.get('abr', 0) or 0, reverse=True)
                    audio_url = audio_formats[0]['url']
            
            if not audio_url:
                raise HTTPException(status_code=404, detail="No audio stream found")
            
            # Add cache busting parameter to URL to prevent stale connections
            if '?' in audio_url:
                audio_url += f'&_={int(time.time())}'
            else:
                audio_url += f'?_={int(time.time())}'
            
            return JSONResponse({
                "url": audio_url,
                "title": info.get('title', 'Unknown Title'),
                "duration": info.get('duration', 0),
                "thumbnail": info.get('thumbnail'),
                "artist": info.get('uploader', 'Unknown Artist'),
                "source": "youtube"
            })
            
    except Exception as e:
        logger.error(f"YouTube audio stream error: {e}")
        # Try one more time with different options
        try:
            with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}", 
                    download=False
                )
                
                if info and 'url' in info:
                    audio_url = info['url']
                    if '?' in audio_url:
                        audio_url += f'&_={int(time.time())}'
                    else:
                        audio_url += f'?_={int(time.time())}'
                        
                    return JSONResponse({
                        "url": audio_url,
                        "title": info.get('title', 'Unknown Title'),
                        "duration": info.get('duration', 0),
                        "thumbnail": info.get('thumbnail'),
                        "artist": info.get('uploader', 'Unknown Artist'),
                        "source": "youtube"
                    })
        except Exception as retry_error:
            logger.error(f"YouTube audio stream retry also failed: {retry_error}")
        
        raise HTTPException(status_code=500, detail="Failed to get audio stream")

# ----------------------------
# WebSocket: Jam endpoint (fixed)
# ----------------------------
@app.websocket("/ws/simple-test")
async def simple_test(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"status": "connected"})
    await websocket.close()

@app.websocket("/ws/jam/{jam_id}")
async def websocket_jam_endpoint(websocket: WebSocket, jam_id: str):
    await websocket.accept()
    params = dict(websocket.query_params)
    username = params.get("username", "Guest")
    if not validate_username(username):
        await websocket.close(code=1008, reason="Invalid username")
        return
    if jam_id not in active_jams:
        await websocket.close(code=1008, reason="Jam session not found")
        return

    jam = active_jams[jam_id]
    is_host = False

    try:
        # assign host if absent, else guest
        if jam["host"]["ws"] is None:
            jam["host"]["ws"] = websocket
            jam["host"]["name"] = username
            is_host = True
            logger.info(f"Host connected: {username} to jam {jam_id}")
        else:
            # Check if username is already taken by host or other guests
            all_names = [g["name"] for g in jam["guests"]] + [jam["host"]["name"]]
            if username in all_names:
                await websocket.close(code=1008, reason="Username already taken")
                return
                
            guest = {"ws": websocket, "name": username, "join_time": datetime.now().strftime("%H:%M:%S"), "last_heartbeat": time.time()}
            jam["guests"].append(guest)
            logger.info(f"Guest connected: {username} to jam {jam_id}")

        jam["last_heartbeat"] = time.time()

        # send initial sync to the connecting socket (compressed)
        await send_compressed(websocket, {
            "type": "initial_sync",
            "current_song": jam.get("current_song"),
            "playlist": jam.get("playlist", []),
            "is_playing": jam.get("is_playing", False),
            "position": jam.get("position", 0.0),
            "volume": jam.get("volume", 1.0),
            "host": {"name": jam.get("host", {}).get("name")},
            "guests": [{"name": g["name"], "join_time": g["join_time"]} for g in jam.get("guests", [])],
            "session_created": jam.get("created_at"),
            "you_are_host": is_host
        })

        # broadcast participants
        await broadcast_participants_update(jam_id)

        # Main receive loop
        while True:
            try:
                text = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # keepalive ping; if this fails, socket likely closed
                try:
                    await websocket.send_json({"type": "heartbeat"})
                except Exception:
                    raise WebSocketDisconnect()
                continue

            # parse incoming JSON
            try:
                data = json.loads(text)
            except Exception:
                # ignore malformed
                continue

            # update heartbeat timestamps
            jam["last_heartbeat"] = time.time()
            if not is_host:
                for g in jam["guests"]:
                    if g["ws"] == websocket:
                        g["last_heartbeat"] = time.time()
                        break

            typ = data.get("type")

            # --- Refactored: Actions anyone can perform ---
            # Throttle frequent updates
            if typ == "player_state_update":
                nowt = time.time()
                if nowt - jam.get("last_update_time", 0) < 0.05:
                    continue
                jam["last_update_time"] = nowt
                jam["is_playing"] = data.get("is_playing", jam["is_playing"])
                jam["position"] = float(data.get("position", jam["position"] or 0.0))
                if "volume" in data:
                    jam["volume"] = float(data.get("volume", jam.get("volume", 1.0)))
                # Broadcast sync to others
                await broadcast_to_all(jam_id, {
                    "type": "sync",
                    "song": jam.get("current_song"),
                    "is_playing": jam["is_playing"],
                    "position": jam["position"],
                    "volume": jam.get("volume", 1.0)
                }, exclude_ws=websocket)

            elif typ == "song_change":
                jam["current_song"] = data.get("song")
                jam["is_playing"] = True
                jam["position"] = 0.0
                await broadcast_to_all(jam_id, {
                    "type": "song_change",
                    "song": jam["current_song"],
                    "is_playing": True,
                    "position": 0.0
                }, exclude_ws=websocket)

            elif typ == "playlist_update":
                jam["playlist"] = data.get("playlist", jam.get("playlist", []))
                await broadcast_to_all(jam_id, {"type": "playlist_update", "playlist": jam["playlist"]}, exclude_ws=websocket)

            elif typ == "seek":
                jam["position"] = float(data.get("position", jam.get("position", 0.0)))
                await broadcast_to_all(jam_id, {"type": "seek", "position": jam["position"]}, exclude_ws=websocket)

            elif typ == "song_ended":
                # Advance to next in playlist
                if jam.get("playlist"):
                    current = jam.get("current_song")
                    next_index = 0
                    try:
                        idx = next((i for i, s in enumerate(jam["playlist"]) if s.get("id") == (current or {}).get("id")), -1)
                        if idx != -1:
                            next_index = (idx + 1) % len(jam["playlist"])
                        # If song not found, just play from the start
                    except Exception:
                        next_index = 0 # Fallback
                    
                    if jam["playlist"]:
                        next_song = jam["playlist"][next_index]
                        jam["current_song"] = next_song
                        jam["is_playing"] = True
                        jam["position"] = 0.0
                        # Broadcast to everyone since the server is changing the song
                        await broadcast_to_all(jam_id, {
                            "type": "song_change",
                            "song": jam["current_song"],
                            "is_playing": jam["is_playing"],
                            "position": 0.0
                        })

            elif typ == "sync_request":
                # A client is asking for the current state
                await send_compressed(websocket, {
                    "type": "sync",
                    "song": jam.get("current_song"),
                    "is_playing": jam.get("is_playing", False),
                    "position": jam.get("position", 0.0),
                    "volume": jam.get("volume", 1.0)
                })

            elif typ == "chat_message":
                msg = data.get("message", "")
                if validate_message(msg):
                    await broadcast_chat_message(jam_id, {
                        "sender": username,
                        "message": msg,
                        "timestamp": datetime.now().strftime("%H:%M")
                    })
            
            # This is sent by the host upon reconnecting to sync the session
            elif is_host and typ == "host_init":
                jam["current_song"] = data.get("song")
                jam["playlist"] = data.get("playlist", jam.get("playlist", []))
                jam["is_playing"] = data.get("is_playing", False)
                jam["position"] = float(data.get("position", 0.0))
                jam["volume"] = float(data.get("volume", jam.get("volume", 1.0)))
                # After host re-initializes, inform everyone of the state
                await broadcast_to_all(jam_id, {
                    "type": "initial_sync",
                     "current_song": jam.get("current_song"),
                    "playlist": jam.get("playlist", []),
                    "is_playing": jam.get("is_playing", False),
                    "position": jam.get("position", 0.0),
                    "volume": jam.get("volume", 1.0),
                    "host": {"name": jam.get("host", {}).get("name")},
                    "guests": [{"name": g["name"], "join_time": g["join_time"]} for g in jam.get("guests", [])],
                }, exclude_ws=websocket)


    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected: {username} (host={is_host})")
        # cleanup
        if jam_id in active_jams:
            jam_local = active_jams[jam_id]
            if is_host:
                # Host left, end the session for everyone
                logger.info(f"Host {username} left jam {jam_id}. Terminating session.")
                await broadcast_to_all(jam_id, {"type": "jam_ended", "reason": f"Host {jam_local.get('host', {}).get('name', 'Host')} left the session"})
                for g in jam_local.get("guests", []):
                    try: await g["ws"].close(code=1000, reason="Host disconnected")
                    except Exception: pass
                active_jams.pop(jam_id, None)
            else:
                # Guest left, update participant list for others
                jam_local["guests"] = [g for g in jam_local.get("guests", []) if g["ws"] != websocket]
                await broadcast_participants_update(jam_id)
    except Exception:
        logger.exception("Unexpected WebSocket error")
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except Exception:
            pass

# ----------------------------
# Broadcast helpers
# ----------------------------
async def broadcast_to_all(jam_id: str, message: dict, exclude_ws: Optional[WebSocket] = None):
    if jam_id not in active_jams:
        return
    jam = active_jams[jam_id]
    
    # Send to host if they exist and are not excluded
    host_ws = jam.get("host", {}).get("ws")
    if host_ws and host_ws != exclude_ws:
        try:
            await send_compressed(host_ws, message)
        except Exception:
            logger.debug(f"Failed to send to host of jam {jam_id}")

    # Send to guests, tracking who is still connected
    alive_guests = []
    for guest in jam.get("guests", []):
        guest_ws = guest.get("ws")
        if guest_ws and guest_ws != exclude_ws:
            try:
                await send_compressed(guest_ws, message)
                alive_guests.append(guest)
            except Exception:
                # Drop guest on failure
                logger.debug(f"Dropping disconnected guest from jam {jam_id}")
                continue
        else:
             alive_guests.append(guest) # Keep the excluded guest in the list
    jam["guests"] = alive_guests

async def broadcast_participants_update(jam_id: str):
    if jam_id not in active_jams:
        return
    jam = active_jams[jam_id]
    update = {
        "type": "participants_update",
        "host": {"name": jam.get("host", {}).get("name")},
        "guests": [{"name": g["name"], "join_time": g["join_time"]} for g in jam.get("guests", [])]
    }
    await broadcast_to_all(jam_id, update)

async def broadcast_chat_message(jam_id: str, message: dict):
    if jam_id not in active_jams:
        return
    jam = active_jams[jam_id]
    chat = {
        "type": "chat_message",
        "sender": message["sender"],
        "message": message["message"],
        "timestamp": message["timestamp"],
        "is_host": message["sender"] == jam.get("host", {}).get("name")
    }
    await broadcast_to_all(jam_id, chat)

# ----------------------------
# Cleanup task
# ----------------------------
async def cleanup_inactive_sessions():
    while True:
        await asyncio.sleep(60)
        nowt = time.time()
        to_delete = []
        for jam_id, jam in list(active_jams.items()):
            # A jam is inactive if the last heartbeat is older than 5 minutes
            if nowt - jam.get("last_heartbeat", 0) > 300:
                to_delete.append(jam_id)
        
        for j in to_delete:
            logger.info(f"Cleaning inactive jam {j}")
            jam_to_clean = active_jams.get(j)
            if jam_to_clean:
                # Notify and close all connections before deleting
                await broadcast_to_all(j, {"type": "jam_ended", "reason": "Session timed out due to inactivity"})
                
                # Close host WebSocket
                host_ws = jam_to_clean.get("host", {}).get("ws")
                if host_ws:
                    try: await host_ws.close(code=1000, reason="Session timeout")
                    except Exception: pass
                
                # Close guest WebSockets
                for g in jam_to_clean.get("guests", []):
                    try: await g["ws"].close(code=1000, reason="Session timeout")
                    except Exception: pass

            active_jams.pop(j, None)

# ----------------------------
# Frontend HTML (updated with synchronization fixes)
# ----------------------------
frontend_html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Synq Music Player</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/pako/2.1.0/pako.min.js"></script>
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-8346311897787343"
     crossorigin="anonymous"></script>
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
        .reconnecting {
            animation: pulse 2s infinite;
        }
        .youtube-badge {
            background-color: #ff0000;
            color: white;
            font-size: 0.7rem;
            padding: 0.1rem 0.4rem;
            border-radius: 0.25rem;
            margin-left: 0.5rem;
        }
        .autoplay-toggle {
            background-color: #e5e7eb;
            color: #4b5563;
            padding: 0.5rem;
            border-radius: 0.375rem;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        .autoplay-toggle.active {
            background-color: #10b981;
            color: white;
        }
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
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
            Synq Player
        </h2>
        
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
            
            <div id="reconnect-container" class="hidden mb-2">
                <div class="flex items-center text-yellow-600 text-sm">
                    <i class="fas fa-sync-alt animate-spin mr-2"></i>
                    <span>Reconnecting...</span>
                    <button id="cancel-reconnect" class="ml-2 text-xs text-gray-500 hover:text-gray-700">Cancel</button>
                </div>
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
                <p class="text-xs text-gray-600">Connected to a shared session!</p>
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

            <button id="autoplay-toggle" class="autoplay-toggle text-gray-700 hover:text-indigo-600 focus:outline-none transition-transform duration-200 ease-in-out active:scale-95" title="Toggle Autoplay">
                <i class="fas fa-infinity"></i>
            </button>

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

    <div id="unified-search-modal" class="fixed top-0 left-0 w-full h-full bg-black bg-opacity-50 flex items-center justify-center hidden z-50">
        <div class="bg-white rounded-lg p-6 w-full max-w-md max-h-[80vh] overflow-hidden flex flex-col">
            <div class="flex justify-between items-center mb-4">
                <h3 class="text-2xl font-bold text-gray-800">Search Songs</h3>
                <button id="close-search-modal" class="text-gray-500 hover:text-gray-700 text-xl">
                    <i class="fas fa-times"></i>
                </button>
            </div>
            
            <div class="mb-4">
                <div class="flex space-x-2">
                    <input type="text" id="unified-search-input" placeholder="Search for songs or YouTube videos..." 
                           class="flex-grow px-3 py-2 border border-gray-300 rounded-md focus:ring-indigo-500 focus:border-indigo-500">
                    <button id="unified-search-button" class="px-4 py-2 bg-indigo-600 text-white rounded-md hover:bg-indigo-700 transition-colors duration-200">
                        <i class="fas fa-search"></i>
                    </button>
                </div>
            </div>
            
            <div class="flex-grow overflow-y-auto">
                <div id="unified-search-results" class="space-y-2">
                    <p class="text-gray-500 text-center py-4">Start typing to search for songs...</p>
                </div>
            </div>
            
            <div class="mt-4 pt-4 border-t border-gray-200">
                <button id="done-search-button" class="w-full px-4 py-2 bg-gray-300 text-gray-800 rounded-md hover:bg-gray-400">
                    Done
                </button>
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
        const autoplayToggle = document.getElementById('autoplay-toggle');
        const playRandomHostedSongsButton = document.getElementById('play-random-hosted-songs-button');
        const showAddOptionsButton = document.getElementById('show-add-options-button');
        const managePlaylistButton = document.getElementById('manage-playlist-button');
        
        // Unified search elements
        const unifiedSearchModal = document.getElementById('unified-search-modal');
        const unifiedSearchInput = document.getElementById('unified-search-input');
        const unifiedSearchResults = document.getElementById('unified-search-results');
        const unifiedSearchButton = document.getElementById('unified-search-button');
        const closeSearchModal = document.getElementById('close-search-modal');
        const doneSearchButton = document.getElementById('done-search-button');
        
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
        const reconnectContainer = document.getElementById('reconnect-container');
        const cancelReconnectButton = document.getElementById('cancel-reconnect');
        
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
        let heartbeatInterval;
        let reconnectAttempts = 0;
        let maxReconnectAttempts = 5;
        let reconnectTimeout = null;
        let username = "Guest";
        let autoplayEnabled = false;
        let isRotationMode = false; // <-- New state for random rotation mode
        let isSeeking = false; // Flag to prevent sending seek updates while user is dragging

        // --- Audio Player Logic ---
        function playSong(song, seekTime = 0) {
            if (!song || !song.url) {
                console.warn("Attempted to play null or invalid song object.");
                return;
            }
            
            // In a jam, send song change message to server, which will then broadcast
            if (jamId && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({
                    type: "song_change",
                    song: song
                }));
            }
            
            // Reset the audio player completely first
            audioPlayer.pause();
            audioPlayer.src = '';
            audioPlayer.load();
            
            // Add cache busting parameter to prevent stale connections
            let audioUrl = song.url;
            audioUrl += (audioUrl.includes('?') ? '&' : '?') + '_=' + Date.now();
            
            // Set up event listeners BEFORE setting the source
            audioPlayer.onerror = (e) => {
                console.error("Audio loading error:", e, audioUrl);
                playPauseIcon.classList.remove('fa-pause');
                playPauseIcon.classList.add('fa-play');
                isPlaying = false;
                
                // For YouTube songs, try to refresh the stream URL
                if (song.source === 'youtube') {
                    console.log("Attempting to refresh YouTube stream URL...");
                    const videoId = song.id.replace('yt_', '');
                    refreshYouTubeStream(videoId)
                        .then(refreshedSong => {
                            if (refreshedSong) {
                                // Update the song in playlist and try again
                                const songIndex = currentPlaylist.findIndex(s => s.id === song.id);
                                if (songIndex !== -1) {
                                    currentPlaylist[songIndex] = refreshedSong;
                                    syncPlaylistWithServer();
                                    if (songIndex === currentSongIndex) {
                                        playSong(refreshedSong, seekTime);
                                    }
                                }
                            }
                        })
                        .catch(err => {
                            console.error("Failed to refresh YouTube stream:", err);
                            if (autoplayEnabled) setTimeout(() => playNextSong(), 1000);
                        });
                } else if (autoplayEnabled) {
                    setTimeout(() => playNextSong(), 1000);
                }
            };

            
            audioPlayer.onloadedmetadata = () => {
                console.log("Audio metadata loaded, duration:", audioPlayer.duration);
                if (seekTime > 0 && !isNaN(audioPlayer.duration) && seekTime < audioPlayer.duration) {
                    audioPlayer.currentTime = seekTime;
                }
                
                // For guests, we wait for a 'sync' or 'song_change' message from the host to play.
                // For hosts or solo players, we can play immediately.
                if (!jamId || isHost) {
                     audioPlayer.play().catch(error => {
                        console.error("Error playing audio:", error);
                        playPauseIcon.classList.remove('fa-pause');
                        playPauseIcon.classList.add('fa-play');
                        isPlaying = false;
                    });
                }
                updateProgressBar();
                updateTotalTime();
            };
            
            // Now set the source and metadata
            audioPlayer.src = audioUrl;
            audioPlayer.currentSong = song;
            trackTitle.textContent = song.title;
            artistName.textContent = song.artist || 'Unknown Artist';
            albumArt.src = song.thumbnail || "https://placehold.co/128x128/4F46E5/FFFFFF?text=Album+Art";
            
            // Load the audio
            audioPlayer.load();

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
            
            if (jamId && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({
                    type: "player_state_update",
                    is_playing: false,
                    position: audioPlayer.currentTime,
                    volume: audioPlayer.volume
                }));
            }
        }

        function togglePlayPause() {
            if (!audioPlayer.currentSong) return;

            if (isPlaying) {
                pauseSong();
            } else {
                audioPlayer.play().catch(error => console.error("Error resuming playback:", error));
                playPauseIcon.classList.remove('fa-play');
                playPauseIcon.classList.add('fa-pause');
                isPlaying = true;
                
                if (jamId && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                    jamSocket.send(JSON.stringify({
                        type: "player_state_update",
                        is_playing: true,
                        position: audioPlayer.currentTime,
                        volume: audioPlayer.volume
                    }));
                }
            }
        }

        function updateProgressBar() {
            if (isSeeking) return; // Don't update UI if user is dragging the slider
            const dur = audioPlayer.duration || 0;
            const cur = audioPlayer.currentTime || 0;
            progressBar.value = dur ? (cur / dur) * 100 : 0;
            progressBar.style.setProperty('--progress', progressBar.value + '%');
            currentTimeSpan.textContent = formatTime(cur);
        }

        function updateTotalTime() {
            totalTimeSpan.textContent = isNaN(audioPlayer.duration) ? '0:00' : formatTime(audioPlayer.duration);
        }

        function formatTime(seconds) {
            if (!seconds || isNaN(seconds) || seconds < 0) return "0:00";
            const minutes = Math.floor(seconds / 60);
            const secs = Math.floor(seconds % 60);
            return minutes + ':' + (secs < 10 ? '0' : '') + secs;
        }

        // --- Autoplay Functions ---
        function toggleAutoplay() {
            autoplayEnabled = !autoplayEnabled;
            if (autoplayEnabled) {
                autoplayToggle.classList.add('active');
                autoplayToggle.title = 'Autoplay: ON';
            } else {
                autoplayToggle.classList.remove('active');
                autoplayToggle.title = 'Autoplay: OFF';
            }
            localStorage.setItem('autoplayEnabled', autoplayEnabled.toString());
        }

        // --- Chat Functions ---
        function addChatMessage(sender, message, timestamp, isHostFlag) {
            const messageDiv = document.createElement('div');
            messageDiv.className = `chat-message ${isHostFlag ? 'host-message' : 'guest-message'}`;
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
            if(host && host.name) {
                const hostBadge = document.createElement('span');
                hostBadge.className = 'participant-badge host-badge';
                hostBadge.innerHTML = `<i class="fas fa-crown mr-1"></i>${host.name}`;
                participantsList.appendChild(hostBadge);
            }
            
            // Add guests
            (guests || []).forEach(guest => {
                const guestBadge = document.createElement('span');
                guestBadge.className = 'participant-badge';
                guestBadge.textContent = guest.name;
                participantsList.appendChild(guestBadge);
            });
        }

        // --- Reconnection Logic ---
        function attemptReconnect() {
            if (reconnectAttempts >= maxReconnectAttempts) {
                alert("Failed to reconnect to jam session. Please try joining again.");
                endJamSession();
                return;
            }
            
            reconnectContainer.classList.remove('hidden');
            jamStatusText.textContent = 'Jam Mode: Reconnecting...';
            jamStatusIndicator.classList.add('reconnecting');
            jamStatusIndicatorSolid.classList.add('reconnecting');
            
            const delay = Math.min(1000 * Math.pow(2, reconnectAttempts), 30000);
            reconnectAttempts++;
            reconnectTimeout = setTimeout(() => {
                connectWebSocket();
            }, delay);
        }

        function cancelReconnect() {
            if (reconnectTimeout) clearTimeout(reconnectTimeout);
            endJamSession();
        }

        // --- Heartbeat Mechanism ---
        function startHeartbeat() {
            clearInterval(heartbeatInterval);
            heartbeatInterval = setInterval(() => {
                if (jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                    jamSocket.send(JSON.stringify({ type: "heartbeat" }));
                }
            }, 25000);
        }

        // --- Jam Session Functions ---
        async function startJamSession() {
            username = prompt("Enter your name to host the jam session (3-20 characters):", "Host") || "Host";
            if (!username || username.length < 3 || username.length > 20 || !/^[a-zA-Z0-9\s_-]+$/.test(username)) {
                alert("Username must be 3-20 alphanumeric characters (spaces, hyphens, and underscores allowed).");
                return;
            }
            try {
                const response = await fetch(`/create-jam?name=${encodeURIComponent(username)}`);
                if (!response.ok) throw new Error('Failed to create jam session');
                const data = await response.json();
                jamId = data.jam_id;
                isHost = true;
                connectWebSocket();
            } catch (err) {
                console.error("Failed to start jam:", err);
                alert("Failed to start jam session.");
            }
        }

        function endJamSession() {
            if (jamSocket) {
                try { jamSocket.close(1000); } catch (e) {}
                jamSocket = null;
            }
            clearInterval(syncInterval);
            clearInterval(heartbeatInterval);
            if (reconnectTimeout) clearTimeout(reconnectTimeout);
            jamToggle.textContent = 'Start Jam';
            jamStatusText.textContent = 'Jam Mode: Off';
            jamStatusIndicator.classList.remove('bg-green-400', 'bg-blue-400', 'reconnecting');
            jamStatusIndicator.classList.add('bg-gray-400');
            jamStatusIndicatorSolid.classList.remove('bg-green-500', 'bg-blue-500', 'reconnecting');
            jamStatusIndicatorSolid.classList.add('bg-gray-500');
            jamHostControls.classList.add('hidden');
            jamGuestInfo.classList.add('hidden');
            participantsContainer.classList.add('hidden');
            chatSection.classList.add('hidden');
            reconnectContainer.classList.add('hidden');
            isHost = false;
            jamId = null;
            reconnectAttempts = 0;
            isRotationMode = false;
        }

        function joinJamSession(jamIdToJoin) {
            username = prompt("Enter your name to join the jam session (3-20 characters):", "Guest") || "Guest";
            if (!username || username.length < 3 || username.length > 20 || !/^[a-zA-Z0-9\s_-]+$/.test(username)) {
                alert("Username must be 3-20 alphanumeric characters (spaces, hyphens, and underscores allowed).");
                return;
            }
            jamId = jamIdToJoin;
            isHost = false;
            connectWebSocket();
        }

        function connectWebSocket() {
            if (!jamId) {
                console.error("connectWebSocket called without a jamId.");
                return;
            }
            const proto = window.location.protocol === 'https:' ? 'wss://' : 'ws://';
            const url = `${proto}${window.location.host}/ws/jam/${jamId}?username=${encodeURIComponent(username)}`;
            try {
                jamSocket = new WebSocket(url);
            } catch (err) {
                console.error("WebSocket connect failed", err);
                attemptReconnect();
                return;
            }
            jamSocket.binaryType = 'arraybuffer';

            jamSocket.onopen = () => {
                console.log("WebSocket open as", username);
                reconnectContainer.classList.add('hidden');
                reconnectAttempts = 0;
                if (isHost) {
                    // Send initial state to the server
                     jamSocket.send(JSON.stringify({
                        type: "host_init",
                        song: audioPlayer.currentSong,
                        playlist: currentPlaylist,
                        is_playing: isPlaying,
                        position: audioPlayer.currentTime,
                        volume: audioPlayer.volume
                    }));
                }
                startHeartbeat();

                // Set up UI for jam session
                jamToggle.textContent = 'Leave Jam';
                jamStatusText.textContent = isHost ? 'Jam Mode: Hosting' : 'Jam Mode: Connected';
                jamStatusIndicator.classList.remove('bg-gray-400');
                jamStatusIndicator.classList.add(isHost ? 'bg-green-400' : 'bg-blue-400');
                jamStatusIndicatorSolid.classList.remove('bg-gray-500');
                jamStatusIndicatorSolid.classList.add(isHost ? 'bg-green-500' : 'bg-blue-500');
                if(isHost) jamHostControls.classList.remove('hidden');
                else jamGuestInfo.classList.remove('hidden');
                participantsContainer.classList.remove('hidden');
                chatSection.classList.remove('hidden');
                jamLinkInput.value = `${window.location.origin}/?jam=${jamId}`;
            };

            jamSocket.onmessage = async (ev) => {
                let data = null;
                try {
                    if (ev.data instanceof ArrayBuffer) {
                        const inflated = pako.inflate(new Uint8Array(ev.data));
                        const text = new TextDecoder().decode(inflated);
                        data = JSON.parse(text);
                    } else if (typeof ev.data === 'string') {
                        data = JSON.parse(ev.data);
                    } else {
                        return;
                    }
                } catch (err) {
                    console.error("WS parse error", err, ev.data);
                    return;
                }

                if (!data || !data.type) return;
                
                console.log("Received WS message:", data.type, data);
                
                switch(data.type) {
                    case 'heartbeat':
                        // This is a ping from server, we do nothing. Our client sends its own heartbeat.
                        break;
                        
                    case 'sync':
                        handleSyncMessage(data);
                        break;
                        
                    case 'song_change':
                        handleSongChange(data);
                        break;
                        
                    case 'playlist_update':
                        currentPlaylist = data.playlist || [];
                        renderPlaylist();
                        break;
                        
                    case 'initial_sync':
                        handleInitialSync(data);
                        updateParticipantsDisplay(data.host, data.guests);
                        break;
                        
                    case 'participants_update':
                        updateParticipantsDisplay(data.host, data.guests);
                        break;
                        
                    case 'chat_message':
                        addChatMessage(data.sender, data.message, data.timestamp, data.is_host);
                        break;
                        
                    case 'seek':
                        if (data.position !== undefined) {
                            audioPlayer.currentTime = data.position;
                        }
                        break;
                        
                    case 'jam_ended':
                        alert("Jam session ended: " + (data.reason || "Host disconnected"));
                        endJamSession();
                        break;
                }
            };

            jamSocket.onclose = (ev) => {
                console.log("WS closed", ev.code, ev.reason);
                if (ev.code === 1008) { // Policy Violation
                    alert("Connection closed: " + ev.reason);
                    endJamSession();
                } else if (ev.code !== 1000 && jamId) {
                    attemptReconnect();
                } else {
                    endJamSession();
                }
            };

            jamSocket.onerror = (err) => {
                console.error("WS error", err);
            };
        }
        
        // This function sends frequent player state updates to the server.
        function sendPlayerStateUpdate() {
            if (jamId && jamSocket && jamSocket.readyState === WebSocket.OPEN && isPlaying) {
                jamSocket.send(JSON.stringify({
                    type: "player_state_update",
                    is_playing: isPlaying,
                    position: audioPlayer.currentTime,
                    volume: audioPlayer.volume
                }));
            }
        }

        function handleSyncMessage(data) {
            // If we receive a song that's different from the current one, treat it as a song change.
            if (data.song && (!audioPlayer.currentSong || data.song.id !== audioPlayer.currentSong.id)) {
                handleSongChange(data);
                return;
            }
            
            if (data.is_playing && audioPlayer.paused) {
                audioPlayer.play().catch(err => {});
                playPauseIcon.classList.remove('fa-play');
                playPauseIcon.classList.add('fa-pause');
                isPlaying = true;
            } else if (!data.is_playing && !audioPlayer.paused) {
                audioPlayer.pause();
                playPauseIcon.classList.remove('fa-pause');
                playPauseIcon.classList.add('fa-play');
                isPlaying = false;
            }
            
            // Only seek if the difference is significant, to avoid jitter
            if (Math.abs((audioPlayer.currentTime || 0) - (data.position || 0)) > 2.0) {
                audioPlayer.currentTime = data.position || 0;
            }
            
            if (typeof data.volume !== 'undefined' && Math.abs(audioPlayer.volume - data.volume) > 0.05) {
                audioPlayer.volume = data.volume;
                volumeBar.value = Math.round(audioPlayer.volume * 100);
                volumeBar.style.setProperty('--volume', volumeBar.value + '%');
            }
        }

        function handleSongChange(data) {
            // Stop current playback
            audioPlayer.pause();
            audioPlayer.src = '';
            
            if (!data.song) {
                resetPlayerUI();
                return;
            }

            // Update UI with new song info
            audioPlayer.currentSong = data.song;
            trackTitle.textContent = data.song.title;
            artistName.textContent = data.song.artist || 'Unknown Artist';
            albumArt.src = data.song.thumbnail || "https://placehold.co/128x128/4F46E5/FFFFFF?text=Album+Art";

            // Set the new source
            let audioUrl = data.song.url;
            audioUrl += (audioUrl.includes('?') ? '&' : '?') + '_=' + Date.now();
            audioPlayer.src = audioUrl;

            // Load and play
            audioPlayer.load();
            if (data.is_playing) {
                audioPlayer.play().catch(e => console.error("Autoplay failed after song change:", e));
            }
            
            isPlaying = data.is_playing;
            playPauseIcon.className = `fas ${isPlaying ? 'fa-pause' : 'fa-play'} text-xl md:text-2xl`;
            
            renderPlaylist(); // To update the highlight
        }

        function handleInitialSync(data) {
            if (data.playlist) {
                currentPlaylist = data.playlist;
                renderPlaylist();
            }
            if (data.current_song) {
                handleSongChange({
                    song: data.current_song,
                    is_playing: data.is_playing,
                    position: data.position
                });
                
                // Set initial position and volume
                audioPlayer.onloadedmetadata = () => {
                    if (data.position > 0) audioPlayer.currentTime = data.position;
                    updateTotalTime();
                };
            }
            if (typeof data.volume !== 'undefined') {
                audioPlayer.volume = data.volume;
                volumeBar.value = Math.round(audioPlayer.volume * 100);
                volumeBar.style.setProperty('--volume', volumeBar.value + '%');
            }
        }

        function syncPlaylistWithServer() {
            if (jamId && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({ type: 'playlist_update', playlist: currentPlaylist }));
            }
        }

        // --- Playlist management ---
        function renderPlaylist() {
            playlistContainer.innerHTML = '';
            if (!currentPlaylist.length) {
                playlistContainer.innerHTML = '<p class="text-gray-500 text-center py-4">Playlist is empty.</p>';
                managePlaylistButton.disabled = true;
                managePlaylistButton.classList.add('opacity-50','cursor-not-allowed');
                return;
            }
            managePlaylistButton.disabled = false;
            managePlaylistButton.classList.remove('opacity-50','cursor-not-allowed');
            
            const currentSongId = audioPlayer.currentSong ? audioPlayer.currentSong.id : null;
            currentSongIndex = currentSongId ? currentPlaylist.findIndex(s => s.id === currentSongId) : -1;

            currentPlaylist.forEach((song, idx) => {
                const li = document.createElement('li');
                li.className = `playlist-item flex items-center justify-between p-3 rounded-lg shadow-sm mb-2 cursor-pointer transition-all duration-200 ease-in-out ${idx === currentSongIndex ? 'current-song' : 'bg-gray-50 hover:bg-gray-100'}`;
                li.dataset.songId = song.id;
                li.innerHTML = `
                    <div class="flex items-center flex-grow min-w-0">
                        <img src="${song.thumbnail || 'https://placehold.co/40x40/CCCCCC/FFFFFF?text=MP3'}" alt="Thumb" class="w-10 h-10 rounded-md mr-3 object-cover">
                        <div class="min-w-0 flex-grow">
                            <p class="font-medium text-sm truncate">${song.title}</p>
                            <p class="text-xs text-gray-500 truncate">${song.artist || 'Unknown Artist'}</p>
                        </div>
                        ${song.source === 'youtube' ? '<span class="youtube-badge">YT</span>' : ''}
                    </div>
                    <button class="remove-song-button text-gray-400 hover:text-red-600 ml-3 focus:outline-none" data-song-id="${song.id}">
                        <i class="fas fa-times"></i>
                    </button>
                `;
                playlistContainer.appendChild(li);

                li.addEventListener('click', (e) => {
                    if (e.target.closest('.remove-song-button')) return;
                     if (currentSongIndex !== idx) {
                        currentSongIndex = idx;
                        isRotationMode = false;
                        playSong(currentPlaylist[currentSongIndex]);
                    } else if (!isPlaying) {
                         togglePlayPause();
                     }
                });
            });

            document.querySelectorAll('.remove-song-button').forEach(btn => {
                btn.addEventListener('click', (e) => {
                    e.stopPropagation();
                    const id = e.currentTarget.dataset.songId;
                    removeSongFromPlaylist(id);
                });
            });
        }

        function addSongToPlaylist(song) {
            if (currentPlaylist.some(s => s.id === song.id)) return;
            isRotationMode = false;
            currentPlaylist.push(song);
            renderPlaylist();
            syncPlaylistWithServer();
        }

        function removeSongFromPlaylist(songId) {
            const idx = currentPlaylist.findIndex(s => s.id === songId);
            if (idx === -1) return;

            const isRemovingCurrent = (currentSongIndex === idx);
            currentPlaylist.splice(idx, 1);

            if (isRemovingCurrent) {
                pauseSong();
                if (currentPlaylist.length > 0) {
                    currentSongIndex = idx % currentPlaylist.length;
                    playSong(currentPlaylist[currentSongIndex]);
                } else {
                    currentSongIndex = -1;
                    resetPlayerUI();
                }
            } else if (currentSongIndex > idx) {
                currentSongIndex--;
            }
            
            renderPlaylist();
            syncPlaylistWithServer();
        }
        
        function playNextSong() {
            if (!currentPlaylist.length) { 
                resetPlayerUI(); 
                return; 
            }
            if(isRotationMode) {
                 playNextAndRotate();
            } else {
                currentSongIndex = (currentSongIndex + 1) % currentPlaylist.length;
                playSong(currentPlaylist[currentSongIndex]);
                renderPlaylist();
            }
        }

        function playNextAndRotate() {
            if (!currentPlaylist.length) return resetPlayerUI();

            const lastPlayedIndex = currentSongIndex;
            currentPlaylist.splice(lastPlayedIndex, 1);

            const availableSongs = hostedSongs.filter(song => !currentPlaylist.some(pSong => pSong.id === song.id));
            if (availableSongs.length > 0) {
                const newSong = availableSongs[Math.floor(Math.random() * availableSongs.length)];
                currentPlaylist.push(newSong);
            }

            if (currentPlaylist.length === 0) return resetPlayerUI();

            currentSongIndex = (lastPlayedIndex >= currentPlaylist.length) ? 0 : lastPlayedIndex;
            playSong(currentPlaylist[currentSongIndex]);
            renderPlaylist();
            syncPlaylistWithServer();
        }
        
        function resetPlayerUI() {
            audioPlayer.pause();
            audioPlayer.src = '';
            audioPlayer.currentSong = null;
            progressBar.value = 0;
            progressBar.style.setProperty('--progress', '0%');
            currentTimeSpan.textContent = '0:00';
            totalTimeSpan.textContent = '0:00';
            trackTitle.textContent = 'No song loaded';
            artistName.textContent = '';
            albumArt.src = 'https://placehold.co/128x128/CCCCCC/FFFFFF?text=No+Track';
            playPauseIcon.classList.remove('fa-pause');
            playPauseIcon.classList.add('fa-play');
            isPlaying = false;
            if(jamId) syncPlaylistWithServer();
        }

        // --- YouTube Audio Streaming Functions ---
        async function getYouTubeStream(videoId) {
            const response = await fetch(`/youtube/stream/${videoId}`);
            if (!response.ok) throw new Error('Failed to get YouTube stream');
            return await response.json();
        }

        async function searchYouTube(query) {
            const response = await fetch(`/youtube/search?query=${encodeURIComponent(query)}`);
            if (!response.ok) throw new Error('YouTube search failed');
            return (await response.json()).results || [];
        }

        async function playYouTubeAudio(videoId, immediate = false) {
            try {
                showLoadingIndicator(true);
                const streamInfo = await getYouTubeStream(videoId);
                
                const youtubeSong = {
                    id: `yt_${videoId}`,
                    title: streamInfo.title,
                    artist: streamInfo.artist || 'YouTube Artist',
                    url: streamInfo.url,
                    thumbnail: streamInfo.thumbnail || 'https://placehold.co/128x128/FF0000/FFFFFF?text=YouTube',
                    duration: streamInfo.duration,
                    source: 'youtube'
                };
                
                isRotationMode = false;
                if (immediate) {
                    currentPlaylist.splice(currentSongIndex + 1, 0, youtubeSong);
                    currentSongIndex++;
                    renderPlaylist();
                    playSong(youtubeSong);
                    syncPlaylistWithServer();
                } else {
                    addSongToPlaylist(youtubeSong);
                }
                
            } catch (error) {
                alert('Failed to play YouTube audio: ' + error.message);
            } finally {
                showLoadingIndicator(false);
            }
        }
        
        async function refreshYouTubeStream(videoId) {
            const streamInfo = await getYouTubeStream(videoId);
            return {
                id: `yt_${videoId}`,
                title: streamInfo.title,
                artist: streamInfo.artist || 'YouTube Artist',
                url: streamInfo.url,
                thumbnail: streamInfo.thumbnail || 'https://placehold.co/128x128/FF0000/FFFFFF?text=YouTube',
                duration: streamInfo.duration,
                source: 'youtube'
            };
        }

        function showLoadingIndicator(show) {
            let loader = document.getElementById('youtube-loader');
            if (show) {
                if (!loader) {
                    loader = document.createElement('div');
                    loader.id = 'youtube-loader';
                    loader.innerHTML = '<div class="fixed top-0 left-0 w-full h-full bg-black bg-opacity-50 flex items-center justify-center z-50"><div class="bg-white p-4 rounded-lg"><i class="fas fa-spinner fa-spin text-2xl text-indigo-600"></i><p class="mt-2">Loading audio...</p></div></div>';
                    document.body.appendChild(loader);
                }
            } else {
                if (loader) loader.remove();
            }
        }

        // --- Unified Search Functions ---
        function openUnifiedSearchModal() {
            unifiedSearchModal.classList.remove('hidden');
            unifiedSearchInput.value = '';
            unifiedSearchResults.innerHTML = '<p class="text-gray-500 text-center py-4">Start typing to search...</p>';
            if (!hostedSongs.length) fetchHostedSongs();
            unifiedSearchInput.focus();
        }

        function closeUnifiedSearchModal() {
            unifiedSearchModal.classList.add('hidden');
        }

        async function performUnifiedSearch() {
            const query = unifiedSearchInput.value.trim();
            if (!query) return;
            unifiedSearchResults.innerHTML = '<p class="text-center py-4"><i class="fas fa-spinner fa-spin mr-2"></i>Searching...</p>';
            
            try {
                const localResults = hostedSongs.filter(song => 
                    song.title.toLowerCase().includes(query.toLowerCase()) || 
                    (song.artist && song.artist.toLowerCase().includes(query.toLowerCase()))
                ).slice(0, 5);
                const youtubeResults = await searchYouTube(query);
                
                unifiedSearchResults.innerHTML = '';
                if (!localResults.length && !youtubeResults.length) {
                    unifiedSearchResults.innerHTML = '<p class="text-center py-4">No results found</p>';
                    return;
                }
                
                if (localResults.length) {
                    const header = document.createElement('div');
                    header.className = 'text-sm font-semibold text-gray-700 mb-2';
                    header.textContent = 'Local Songs';
                    unifiedSearchResults.appendChild(header);
                    localResults.forEach(song => unifiedSearchResults.appendChild(createSearchResultItem(song, 'local')));
                }
                
                if (youtubeResults.length) {
                    const header = document.createElement('div');
                    header.className = 'text-sm font-semibold text-gray-700 mb-2 mt-4';
                    header.textContent = 'YouTube Results';
                    unifiedSearchResults.appendChild(header);
                    youtubeResults.forEach(video => unifiedSearchResults.appendChild(createSearchResultItem(video, 'youtube')));
                }
            } catch (error) {
                unifiedSearchResults.innerHTML = '<p class="text-red-500 text-center py-4">Search failed.</p>';
            }
        }

        function createSearchResultItem(item, source) {
            const resultDiv = document.createElement('div');
            resultDiv.className = 'search-result flex items-center justify-between p-3 bg-gray-100 rounded-md mb-2 hover:bg-gray-200 transition-colors';
            
            resultDiv.innerHTML = `
                <div class="flex items-center min-w-0 flex-grow cursor-pointer">
                    <img src="${item.thumbnail || 'https://placehold.co/40x40/CCCCCC/FFFFFF?text=MP3'}" class="w-10 h-10 rounded-md mr-3 object-cover">
                    <div class="min-w-0 flex-grow">
                        <p class="font-medium text-sm truncate">${item.title}</p>
                        <p class="text-xs text-gray-500 truncate">${item.artist || 'Unknown Artist'}</p>
                    </div>
                    ${source === 'youtube' ? '<span class="youtube-badge">YT</span>' : ''}
                </div>
                <button class="add-search-result ml-3 px-3 py-1 bg-indigo-500 text-white text-xs rounded-md hover:bg-indigo-600">Add</button>
            `;
            
            const addButton = resultDiv.querySelector('.add-search-result');
            addButton.addEventListener('click', async (e) => {
                e.stopPropagation();
                addButton.textContent = 'Adding...';
                addButton.disabled = true;

                try {
                    if (source === 'local') {
                        addSongToPlaylist(item);
                    } else if (source === 'youtube') {
                        showLoadingIndicator(true);
                        const streamInfo = await getYouTubeStream(item.id);
                        const youtubeSong = {
                            id: `yt_${item.id}`, title: streamInfo.title, artist: streamInfo.artist,
                            url: streamInfo.url, thumbnail: streamInfo.thumbnail, duration: streamInfo.duration, source: 'youtube'
                        };
                        addSongToPlaylist(youtubeSong);
                    }
                    addButton.textContent = 'Added';
                    addButton.classList.replace('bg-indigo-500', 'bg-gray-400');
                } catch (error) {
                    alert('Failed to add song: ' + error.message);
                    addButton.textContent = 'Add';
                    addButton.disabled = false;
                } finally {
                    showLoadingIndicator(false);
                }
            });
            
            resultDiv.querySelector('.min-w-0').addEventListener('click', () => {
                 isRotationMode = false;
                 if (source === 'local') {
                    addSongToPlaylist(item);
                    if (!audioPlayer.currentSong) {
                        currentSongIndex = currentPlaylist.length - 1;
                        playSong(item);
                    }
                    closeUnifiedSearchModal();
                } else {
                    playYouTubeAudio(item.id, true);
                    closeUnifiedSearchModal();
                }
            });
            return resultDiv;
        }

        // --- Hosted songs functions ---
        async function fetchHostedSongs() {
            try {
                hostedSongs = await (await fetch('/get-songs')).json();
            } catch (e) {
                console.error('fetchHostedSongs failed', e);
            }
        }

        function playRandomSongsWithRotation() {
            if (!hostedSongs.length) return alert('No hosted songs available.');
            
            isRotationMode = true;
            const shuffled = [...hostedSongs].sort(() => 0.5 - Math.random());
            currentPlaylist = shuffled.slice(0, Math.min(5, hostedSongs.length));
            currentSongIndex = 0;
            renderPlaylist();
            syncPlaylistWithServer();
            playSong(currentPlaylist[currentSongIndex]);
        }

        // --- Event listeners ---
        playPauseButton.addEventListener('click', togglePlayPause);
        audioPlayer.addEventListener('timeupdate', updateProgressBar);
        
        audioPlayer.addEventListener('ended', () => {
            if (autoplayEnabled && currentPlaylist.length > 0) {
                 if (jamId && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                    // In a jam, only one client (usually the first to finish) should send this.
                    // The server will handle advancing the playlist for everyone.
                    jamSocket.send(JSON.stringify({ type: 'song_ended' }));
                } else if (!jamId) {
                    // Solo mode
                    playNextSong();
                }
            } else {
                pauseSong();
            }
        });

        audioPlayer.addEventListener('volumechange', () => {
            volumeBar.style.setProperty('--volume', audioPlayer.volume * 100 + '%');
            // Volume changes are local unless the user is the host and explicitly syncs
        });

        progressBar.addEventListener('mousedown', () => { isSeeking = true; });
        progressBar.addEventListener('mouseup', () => { isSeeking = false; });
        progressBar.addEventListener('input', () => {
            if (!isSeeking || isNaN(audioPlayer.duration)) return;
            const seekTime = (progressBar.value / 100) * audioPlayer.duration;
            currentTimeSpan.textContent = formatTime(seekTime);
        });
        progressBar.addEventListener('change', () => { // 'change' fires after mouse up
            if (isNaN(audioPlayer.duration)) return;
            const seekTime = (progressBar.value / 100) * audioPlayer.duration;
            audioPlayer.currentTime = seekTime;
            if (jamId && jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({ type: 'seek', position: seekTime }));
            }
        });

        volumeBar.addEventListener('input', (e) => audioPlayer.volume = e.target.value / 100);

        nextButton.addEventListener('click', playNextSong);
        autoplayToggle.addEventListener('click', toggleAutoplay);
        rewindButton.addEventListener('click', () => {
            const newTime = Math.max(0, audioPlayer.currentTime - 10);
            audioPlayer.currentTime = newTime;
            if (jamId) jamSocket.send(JSON.stringify({ type: 'seek', position: newTime }));
        });
        forwardButton.addEventListener('click', () => {
            const newTime = Math.min(audioPlayer.duration || 0, audioPlayer.currentTime + 10);
            audioPlayer.currentTime = newTime;
            if (jamId) jamSocket.send(JSON.stringify({ type: 'seek', position: newTime }));
        });

        playRandomHostedSongsButton.addEventListener('click', playRandomSongsWithRotation);
        showAddOptionsButton.addEventListener('click', openUnifiedSearchModal);

        // Unified search event listeners
        unifiedSearchButton.addEventListener('click', performUnifiedSearch);
        unifiedSearchInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') performUnifiedSearch(); });
        closeSearchModal.addEventListener('click', closeUnifiedSearchModal);
        doneSearchButton.addEventListener('click', closeUnifiedSearchModal);

        sendChatButton.addEventListener('click', () => {
            const msg = chatInput.value.trim();
            if (!msg || msg.length > 500) return;
            if (jamSocket && jamSocket.readyState === WebSocket.OPEN) {
                jamSocket.send(JSON.stringify({ type: 'chat_message', message: msg }));
                chatInput.value = '';
            }
        });

        chatInput.addEventListener('keypress', (e) => { if (e.key === 'Enter') sendChatButton.click(); });
        jamCopyLink.addEventListener('click', () => {
            navigator.clipboard.writeText(jamLinkInput.value)
                .then(() => {
                    jamCopyLink.innerHTML = '<i class="fas fa-check"></i>';
                    setTimeout(()=>{ jamCopyLink.innerHTML = '<i class="fas fa-copy"></i>'; }, 2000);
                });
        });
        cancelReconnectButton.addEventListener('click', cancelReconnect);
        jamToggle.addEventListener('click', () => jamId ? endJamSession() : startJamSession());

        document.addEventListener('DOMContentLoaded', () => {
            fetchHostedSongs();
            resetPlayerUI();
            renderPlaylist();
            volumeBar.value = Math.round((audioPlayer.volume || 1) * 100);
            volumeBar.style.setProperty('--volume', volumeBar.value + '%');
            const savedAutoplay = localStorage.getItem('autoplayEnabled');
            if (savedAutoplay === 'true') {
                autoplayEnabled = true;
                autoplayToggle.classList.add('active');
            }
            const params = new URLSearchParams(window.location.search);
            const jamParam = params.get('jam');
            if (jamParam) joinJamSession(jamParam);
            
            // Start interval to send player state updates
            syncInterval = setInterval(sendPlayerStateUpdate, 1000); // Send updates every second
        });
    </script>
</body>
</html>
"""

# ----------------------------
# Run with Uvicorn when executed directly
# ----------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    
    # This is the correct way to run uvicorn programmatically
    uvicorn.run("app:app",port=port, reload=True)