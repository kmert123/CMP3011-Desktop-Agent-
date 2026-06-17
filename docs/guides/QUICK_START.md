# Jarvis Quick Start Guide

**Get Jarvis running in 5 minutes** (or troubleshoot if something goes wrong)

---

## 🚀 Ultra-Quick Setup (5 minutes)

### 1️⃣ Prerequisites
- Python 3.10+
- Internet connection (first run downloads ~175MB)
- Microphone
- Google account (for Gemini API)

### 2️⃣ Setup (Copy & Paste)

**Windows PowerShell:**
```powershell
cd c:\KOD\Projects-something-something\comp_vision_project
python -m venv .venv
.venv\Scripts\activate
pip install -r jarvis/requirements.txt
cp jarvis/.env.example jarvis/.env
# Then edit jarvis/.env and add GEMINI_API_KEY
```

**macOS/Linux Terminal:**
```bash
cd comp_vision_project
python3 -m venv .venv
source .venv/bin/activate
brew install portaudio  # macOS only!
pip install -r jarvis/requirements.txt
cp jarvis/.env.example jarvis/.env
# Then edit jarvis/.env and add GEMINI_API_KEY
```

### 3️⃣ Get API Key (2 minutes)
1. Visit https://aistudio.google.com
2. Click "Create API key"
3. Copy the key
4. Paste into `jarvis/.env`:
   ```env
   GEMINI_API_KEY=your_key_here
   ```

### 4️⃣ Run
```bash
python jarvis/main.py
```

**First run:** Wait 2-5 minutes while models download (one-time only)

### 5️⃣ Use
- Say: **"Jarvis"** (to trigger wake word)
- Ask: **"What's on my screen?"**
- Follow-up: Type in the chat window

---

## ✅ Verify Setup

**Run this to check everything:**
```bash
python jarvis/verify_setup.py
```

**Output should show:**
- ✓ Python 3.10+
- ✓ All required files
- ✓ All dependencies installed
- ✓ GEMINI_API_KEY set

---

## 🔧 Troubleshooting

### "No wake word triggers"
```bash
# Check microphone permissions (Windows Settings or macOS System Preferences)
# Test wake word:
python jarvis/wake_word.py
# Say "Hey Jarvis" multiple times
```

### "PyAudio import fails on macOS"
```bash
brew install portaudio
pip install --force-reinstall pyaudio
```

### "GEMINI_API_KEY not set"
```bash
# Check .env file:
cat jarvis/.env  # macOS/Linux: type jarvis\.env  # Windows

# Should see:
GEMINI_API_KEY=your_actual_key_here
```

### "Gemini API errors from Turkey"
**Use local fallback:**
```bash
# 1. Install Ollama: https://ollama.com
# 2. Pull a model:
ollama pull qwen2.5vl:7b

# 3. Edit config.py (line 98):
VISION_BACKEND = "local"  # was "gemini"

# 4. Run:
python jarvis/main.py
```

### "Whisper is slow"
- **First run:** 2-5 minutes (downloads ~150MB)
- **Subsequent runs:** ~1 second (model cached)

### "Window appears but no response"
- Speak clearly and wait for silence
- Check internet connection
- Verify API key is valid

---

## 📁 File Organization

```
jarvis/
├── main.py              ← Run this
├── config.py            ← Settings are here
├── .env                 ← API key goes here
├── .env.example         ← Template
├── requirements.txt     ← Dependencies
├── README.md            ← Full docs
├── adapters/            ← Pluggable perception
└── core/                ← Core logic
```

---

## 📚 Full Documentation

| Document | For What? |
|----------|-----------|
| **README.md** | Setup + detailed usage |
| **PROJECT_DOCUMENTATION.md** | Architecture deep dive |
| **SETUP_VERIFICATION.md** | Setup audit + checklist |
| **QUALITY_ASSURANCE.md** | QA report |
| **JARVIS_SPEC.md** | Original MVP spec |

---

## 🧪 Test Individual Modules

**Test wake word:**
```bash
python jarvis/wake_word.py
```

**Test screenshot:**
```bash
python jarvis/capture.py
```

**Test voice recording:**
```bash
python jarvis/voice.py
```

**Test Gemini (requires API key):**
```bash
python jarvis/gemini.py
```

---

## 🎯 Common Commands

```bash
# Verify setup
python jarvis/verify_setup.py

# Run main app
python jarvis/main.py

# Update dependencies
pip install -r jarvis/requirements.txt --upgrade

# Deactivate virtual environment
deactivate

# Activate virtual environment again
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # macOS
```

---

## 💡 Tips

- **First run takes time:** Models download (~175MB) — this is one-time
- **Microphone volume matters:** Adjust OS microphone input level if wake word doesn't trigger
- **Internet required:** For Gemini API and first-run model downloads
- **API key cost:** Free tier is very generous (~1M tokens/month)
- **Kill hotkey:** Ctrl+Alt+Esc (customizable in config.py)

---

## 📞 Getting Help

1. Check troubleshooting section above
2. Run `python jarvis/verify_setup.py` to diagnose
3. Read **README.md** for detailed troubleshooting
4. Check **PROJECT_DOCUMENTATION.md** for architecture
5. Review **SETUP_VERIFICATION.md** for validation checklist

---

**Status:** Ready to use ✅  
**Last Updated:** 2026-06-17  
**Python Version:** 3.10+
