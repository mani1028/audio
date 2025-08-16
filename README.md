# JamSync - Real-Time Collaborative Music Player 🎵

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.95+-green.svg)](https://fastapi.tiangolo.com)
[![WebSocket](https://img.shields.io/badge/WebSocket-Enabled-brightgreen.svg)](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API)

![App Screenshot](screenshot.png)

## ✨ Features

- **Real-time sync** - Perfectly synchronized playback for all participants
- **Host control** - One host manages playback for everyone
- **Shared playlist** - Collaborative song queue management
- **Live chat** - Built-in messaging during sessions
- **Responsive UI** - Works on desktop and mobile devices

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- pip package manager

### Installation
1. Clone the repository:
```bash
git clone https://github.com/yourusername/jamsync.git
cd jamsync
```

2. Install dependencies:
```bash
pip install fastapi uvicorn python-multipart
```

3. Run the server:
```bash
uvicorn app:app --reload
```

4. Access the application at:
```
http://localhost:8000
```

## 🛠 Tech Stack

### Frontend
- HTML5, CSS3, JavaScript
- Tailwind CSS
- Font Awesome icons
- Pako.js for compression

### Backend
- Python 3.9+
- FastAPI framework
- WebSockets with zlib compression
- Uvicorn ASGI server

## 📂 Project Structure

```
jamsync/
├── app.py                    # Main application file
├── hosted_songs_manifest.json # Default song database
├── README.md                 # This file
└── static/                   # Static assets (optional)
```

## 🌐 Deployment

### Production Setup
1. Install production requirements:
```bash
pip install gunicorn uvloop httptools
```

2. Run with Gunicorn:
```bash
gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b :8000 app:app
```

### Recommended Production Environment
- Nginx reverse proxy
- SSL/TLS encryption
- Proper user authentication
- Process manager (systemd/supervisor)

## 🤝 Contributing

We welcome contributions! Here's how:

1. Fork the repository
2. Create a new branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📜 License

Distributed under the MIT License. See `LICENSE` for more information.

## 📧 Contact

Your Name - chelamalla.manikanta28@gmail.com  
Project Link: [https://github.com/mani1028/audio](https://github.com/mani1028/audio)
