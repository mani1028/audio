# JamSync - Collaborative Music Player 🎵

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.95+-green.svg)](https://fastapi.tiangolo.com)
[![WebSocket](https://img.shields.io/badge/WebSocket-Enabled-brightgreen.svg)](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API)

A real-time collaborative music player that allows multiple users to sync playback, manage playlists, and chat together.

![Screenshot](screenshot.png) *(Add your screenshot here)*

## ✨ Features

- **Real-time synchronization** of playback position and state
- **Host/Guest system** with permission controls
- **Shared playlist management** (add/remove songs)
- **Live chat** during music sessions
- **Responsive UI** that works on desktop and mobile
- **Compressed WebSocket** communication for efficiency

## 🛠 Tech Stack

| Component       | Technology |
|-----------------|------------|
| Frontend        | HTML5, CSS3, JavaScript, Tailwind CSS |
| Backend         | Python, FastAPI |
| Real-Time Comm  | WebSockets with zlib compression |
| Audio Handling  | HTML5 Audio API |
| Dependencies    | Pako.js, Font Awesome |

## 🚀 Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/jamsync.git
   cd jamsync
