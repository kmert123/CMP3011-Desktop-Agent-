#!/usr/bin/env python3
"""Verify Jarvis installation and configuration.

Run this script to check if your setup is complete and correct:
    python jarvis/verify_setup.py
"""

import sys
import os
from pathlib import Path

# ANSI colors for output
class Color:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def print_pass(msg):
    print(f"{Color.GREEN}✓ {msg}{Color.RESET}")

def print_fail(msg):
    print(f"{Color.RED}✗ {msg}{Color.RESET}")

def print_warn(msg):
    print(f"{Color.YELLOW}⚠ {msg}{Color.RESET}")

def print_info(msg):
    print(f"{Color.BLUE}ℹ {msg}{Color.RESET}")

def print_header(msg):
    print(f"\n{Color.BOLD}{msg}{Color.RESET}")

# Check Python version
print_header("1. Python Version")
if sys.version_info >= (3, 10):
    print_pass(f"Python {sys.version_info.major}.{sys.version_info.minor} (>= 3.10)")
else:
    print_fail(f"Python {sys.version_info.major}.{sys.version_info.minor} (need >= 3.10)")
    sys.exit(1)

# Check if running from correct directory
print_header("2. Project Structure")
jarvis_dir = Path(__file__).parent
if jarvis_dir.name == "jarvis":
    print_pass(f"Running from jarvis/ directory: {jarvis_dir}")
else:
    print_fail(f"Wrong directory. Should run from jarvis/ subdirectory")
    sys.exit(1)

# Check critical files exist
print_header("3. Required Files")
required_files = [
    ("config.py", "Configuration + .env loader"),
    ("main.py", "Application entry point"),
    ("requirements.txt", "Python dependencies"),
    (".env.example", "Environment template"),
    (".gitignore", "Git exclusions"),
    ("README.md", "Documentation"),
]

all_files_exist = True
for filename, description in required_files:
    filepath = jarvis_dir / filename
    if filepath.exists():
        print_pass(f"{filename}: {description}")
    else:
        print_fail(f"{filename}: {description} (MISSING)")
        all_files_exist = False

if not all_files_exist:
    sys.exit(1)

# Check subdirectories
print_header("4. Package Structure")
subdirs = [
    ("adapters", "Perception adapters (UIA, OCR, Vision)"),
    ("core", "Core abstractions (EventBus, SessionActor)"),
]

for dirname, description in subdirs:
    dirpath = jarvis_dir / dirname
    if dirpath.exists() and dirpath.is_dir():
        print_pass(f"{dirname}/: {description}")
    else:
        print_warn(f"{dirname}/: {description} (not found, may be optional)")

# Try to import config
print_header("5. Configuration Loading")
try:
    import config
    print_pass("config.py imported successfully")

    # Check critical constants
    constants = [
        ("GEMINI_MODEL", "Gemini model identifier"),
        ("WHISPER_MODEL", "Whisper model identifier"),
        ("SAMPLE_RATE", "Audio sample rate"),
        ("WINDOW_WIDTH", "UI window width"),
        ("WINDOW_HEIGHT", "UI window height"),
    ]

    for const_name, description in constants:
        if hasattr(config, const_name):
            value = getattr(config, const_name)
            print_pass(f"config.{const_name} = {repr(value)}")
        else:
            print_fail(f"config.{const_name} not defined")

except Exception as e:
    print_fail(f"Error loading config.py: {e}")
    sys.exit(1)

# Check .env configuration
print_header("6. Environment Secrets")
env_file = jarvis_dir / ".env"
env_example = jarvis_dir / ".env.example"

if env_example.exists():
    print_pass(".env.example exists")
else:
    print_fail(".env.example not found")

if env_file.exists():
    print_pass(".env exists")
    try:
        with open(env_file) as f:
            content = f.read()
            if "GEMINI_API_KEY" in content:
                # Check if it's not empty (simple heuristic)
                for line in content.split('\n'):
                    if line.startswith('GEMINI_API_KEY='):
                        if line.split('=', 1)[1].strip():
                            print_pass("GEMINI_API_KEY is set")
                        else:
                            print_warn("GEMINI_API_KEY is empty — you need to add your API key")
                        break
            else:
                print_warn("GEMINI_API_KEY not found in .env")
    except Exception as e:
        print_warn(f"Could not read .env: {e}")
else:
    print_warn(".env not found (copy from .env.example and add your API key)")

# Check if GEMINI_API_KEY is loaded
print_header("7. API Configuration")
if config.GEMINI_API_KEY:
    api_key_sample = config.GEMINI_API_KEY[:10] + "..." if len(config.GEMINI_API_KEY) > 10 else "***"
    print_pass(f"GEMINI_API_KEY loaded: {api_key_sample}")
else:
    print_fail("GEMINI_API_KEY not set — add to jarvis/.env")

# Try to import key modules
print_header("8. Core Modules")
modules_to_check = [
    ("wake_word", "Wake word listener"),
    ("voice", "Audio recording"),
    ("transcription", "Speech transcription"),
    ("capture", "Screenshot capture"),
    ("cv_pipeline", "CV pipeline"),
    ("gemini", "Gemini API client"),
    ("ui", "CustomTkinter UI"),
]

failed_imports = []
for module_name, description in modules_to_check:
    try:
        __import__(module_name)
        print_pass(f"{module_name}: {description}")
    except ImportError as e:
        print_fail(f"{module_name}: {description} (import failed: {e})")
        failed_imports.append(module_name)
    except Exception as e:
        # Some modules may fail for other reasons (hardware, etc.)
        print_warn(f"{module_name}: {description} (error: {e})")

# Check Python dependencies
print_header("9. Python Dependencies")
packages_to_check = [
    ("openwakeword", "Wake word detection"),
    ("pyaudio", "Audio recording"),
    ("whisper", "Speech transcription"),
    ("mss", "Screenshot capture"),
    ("cv2", "Computer vision (OpenCV)"),
    ("numpy", "Numerical arrays"),
    ("google.genai", "Gemini API"),
    ("customtkinter", "Modern Tkinter UI"),
    ("dotenv", "Environment variable loading"),
    ("PIL", "Image processing (Pillow)"),
]

missing_packages = []
for package_name, description in packages_to_check:
    try:
        __import__(package_name)
        print_pass(f"{package_name}: {description}")
    except ImportError:
        print_fail(f"{package_name}: {description} (NOT INSTALLED)")
        missing_packages.append(package_name)

if missing_packages:
    print_warn(f"\nMissing packages: {', '.join(missing_packages)}")
    print_info(f"Install with: pip install -r requirements.txt")

# Check system dependencies (Windows-specific)
print_header("10. System Dependencies")
if sys.platform == "win32":
    print_info("Windows detected")

    # Check for pywin32 (needed for focus detection)
    try:
        import win32gui
        print_pass("pywin32: Windows GUI utilities (installed)")
    except ImportError:
        print_fail("pywin32: Windows GUI utilities (NOT INSTALLED)")
        print_info("  Install with: pip install pywin32")

    # Check for Tesseract (optional)
    tesseract_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if Path(tesseract_path).exists():
        print_pass(f"Tesseract: OCR engine (found at {tesseract_path})")
    else:
        print_warn("Tesseract: OCR engine (not found at default location)")
        print_info(f"  Optional. Download from: https://github.com/UB-Mannheim/tesseract/wiki")
        print_info(f"  Install to: {tesseract_path}")

elif sys.platform == "darwin":
    print_info("macOS detected")
    print_warn("macOS-specific verification not fully implemented")
    print_info("Manually verify: brew install portaudio (already done?)")

else:
    print_info(f"Platform: {sys.platform}")

# Summary and next steps
print_header("Summary")
if failed_imports:
    print_fail(f"Setup is INCOMPLETE: {len(failed_imports)} module(s) failed to import")
    print_info("Next step: Install dependencies with: pip install -r requirements.txt")
elif missing_packages:
    print_fail(f"Setup is INCOMPLETE: {len(missing_packages)} package(s) not installed")
    print_info("Next step: Install dependencies with: pip install -r requirements.txt")
elif not config.GEMINI_API_KEY:
    print_warn("Setup is INCOMPLETE: GEMINI_API_KEY not set")
    print_info("Next step: Get API key from https://aistudio.google.com and add to jarvis/.env")
else:
    print_pass("Setup is COMPLETE! Ready to run Jarvis")
    print_info("Next step: python jarvis/main.py")
    if sys.platform == "win32":
        print_info("  First run will download ~175MB of models (2-5 minutes)")

print()  # Blank line for readability
